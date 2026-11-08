"""Headed Chromium against a local fixture page. No merchant site is opened."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from unittest import mock

from inbox_collector.browser_session import process_exists
from inbox_collector.cli import main
from inbox_collector.config import load_config
from inbox_collector.login_prep import run_login_prep
from inbox_collector.mapping import load_mapping
from inbox_collector.store import Store
from inbox_collector.runner import Runner
from inbox_collector.safety import CANNED_DRAFT_TEXT
from inbox_collector.providers import provider_for

ROOT = Path(__file__).resolve().parents[1]
MAPPING = ROOT / "mappings" / "generic_local_inbox.json"


def evidence_directory() -> Path:
    """User-writable evidence directory. Never creates a system directory.

    ``INBOX_COLLECTOR_TEST_ARTIFACTS`` is used only when its parent already
    exists and is writable. Otherwise evidence stays in the checkout's
    ``.test-artifacts`` directory, which the person running the tests owns.
    """
    override = os.environ.get("INBOX_COLLECTOR_TEST_ARTIFACTS", "").strip()
    if override:
        candidate = Path(override)
        parent = candidate.parent
        if parent.is_dir() and os.access(parent, os.W_OK):
            candidate.mkdir(mode=0o700, exist_ok=True)
            if candidate.is_dir() and os.access(candidate, os.W_OK):
                return candidate
    local = ROOT / ".test-artifacts"
    local.mkdir(mode=0o700, exist_ok=True)
    return local


def _article(message_id: str, body: str, when: str) -> str:
    return (
        "<article"
        ' data-conversation-key="conv-sim-1"'
        ' data-identity-verified="true"'
        f' data-message-id="{message_id}"'
        ' data-direction="buyer"'
        f' data-message-time="{when}"'
        ' data-time-precision="second"'
        ' data-list-time="13:32"'
        ' data-reply-status="unreplied"'
        ' data-reply-source="fixture_field"'
        ' data-reply-reliability="reliable">'
        f"{body}</article>"
    )


def _page(inner: str, *, state: str, complete: str) -> str:
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>"
        f"<section data-fixture='generic-inbox' data-page-state='{state}' "
        f"data-message-list-complete='{complete}'>{inner}</section></body></html>"
    )


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        hosts = getattr(self.server, "hosts", None)
        if isinstance(hosts, list):
            hosts.append(self.headers.get("Host", ""))
        path = self.path.split("?", 1)[0]
        routes = getattr(self.server, "routes", None)
        if isinstance(routes, dict) and path in routes:
            body = routes[path]
        elif path == "/inbox.html":
            body = self.server.html_path.read_bytes()  # type: ignore[attr-defined]
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        return


class EvidencePathTest(unittest.TestCase):
    def test_missing_override_parent_does_not_create_system_directories(self) -> None:
        missing_parent = Path("/opt/cursor-not-created/artifacts")
        self.assertFalse(missing_parent.parent.exists())
        previous = os.environ.get("INBOX_COLLECTOR_TEST_ARTIFACTS")
        os.environ["INBOX_COLLECTOR_TEST_ARTIFACTS"] = str(missing_parent)
        try:
            chosen = evidence_directory()
        finally:
            if previous is None:
                os.environ.pop("INBOX_COLLECTOR_TEST_ARTIFACTS", None)
            else:
                os.environ["INBOX_COLLECTOR_TEST_ARTIFACTS"] = previous
        self.assertFalse(missing_parent.parent.exists())
        self.assertEqual(chosen, ROOT / ".test-artifacts")
        self.assertTrue(os.access(chosen, os.W_OK))


class BrowserE2ETest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._xvfb: subprocess.Popen[bytes] | None = None
        # macOS has no Xvfb and does not use DISPLAY. Only Linux needs a
        # virtual screen, and a missing Xvfb there is a real setup failure.
        if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
            xvfb = shutil.which("Xvfb")
            if xvfb is None:
                raise RuntimeError("Linux browser tests need Xvfb when DISPLAY is unset")
            cls._xvfb = subprocess.Popen(
                [xvfb, ":99", "-screen", "0", "1280x800x24"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(0.4)
            if cls._xvfb.poll() is not None:
                raise RuntimeError("Xvfb exited before the browser tests started")
            os.environ["DISPLAY"] = ":99"

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._xvfb is not None:
            cls._xvfb.terminate()
            cls._xvfb.wait(timeout=5)

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.html_path = self.root / "inbox.html"
        self.data = self.root / "data"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.html_path = self.html_path  # type: ignore[attr-defined]
        self.port = self.server.server_address[1]
        self._thread = __import__("threading").Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)

    def _config(self, **overrides: object) -> Path:
        payload: dict[str, object] = {
            "mode": "browser_local",
            "timezone": "Asia/Shanghai",
            "sample_interval_seconds": 120,
            "login_wait_seconds": 2,
            "max_retries": 1,
            "max_consecutive_failures": 2,
            "open_conversation": False,
            "allow_browser_launch": True,
            "mapping_path": str(MAPPING),
            "entry_url": f"http://127.0.0.1:{self.port}/inbox.html",
            "data_dir": str(self.data),
            "draft_provider": "mock",
            "retention_days": 30,
            "webhook": {"enabled": False},
            "alert_sink": {"enabled": False},
            "model": {"enabled": False},
        }
        payload.update(overrides)
        path = self.root / "config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _runner(self, **overrides: object) -> Runner:
        loaded, problems = load_config(self._config(**overrides))
        self.assertEqual(problems, [], problems)
        assert loaded is not None
        mapping = load_mapping(loaded.mapping_path)
        from inbox_collector.browser_session import DedicatedBrowser
        from inbox_collector.dom_source import BrowserDomSource

        source = BrowserDomSource(
            DedicatedBrowser(loaded.data_dir / "own-browser-profile"),
            loaded.entry_url,
            mapping,
            timezone_name=loaded.timezone,
            login_wait_seconds=loaded.login_wait_seconds,
        )
        runner = Runner(loaded, mapping, source, provider_for("mock"))
        self.addCleanup(runner.close)
        return runner

    def test_dom_change_creates_one_mock_draft_and_restart_dedupes(self) -> None:
        self.html_path.write_text(
            _page(
                _article("msg-sim-old", "【模拟】基线消息。", "2026-11-08T10:00:00+08:00"),
                state="inbox",
                complete="true",
            ),
            encoding="utf-8",
        )
        runner = self._runner()
        sleeps: list[int] = []

        def _sleep(seconds: int) -> None:
            self.assertEqual(seconds, 120)
            sleeps.append(seconds)

        self.html_path.write_text(
            _page(
                _article("msg-sim-old", "【模拟】基线消息。", "2026-11-08T10:00:00+08:00"),
                state="inbox",
                complete="true",
            ),
            encoding="utf-8",
        )

        def _sleep_and_publish(seconds: int) -> None:
            _sleep(seconds)
            self.html_path.write_text(
                _page(
                    _article("msg-sim-old", "【模拟】基线消息。", "2026-11-08T10:00:00+08:00")
                    + _article("msg-sim-new", "【模拟】新的买家消息，非真实客户。", "2026-11-08T13:32:44+08:00"),
                    state="inbox",
                    complete="true",
                ),
                encoding="utf-8",
            )

        first = runner.run_once()
        self.assertTrue(first["browser_launched"])
        self.assertEqual(first["messages"], 1)
        self.assertEqual(first["drafts_ready_for_review"], 0)
        self.html_path.write_text(
            _page(
                _article("msg-sim-old", "【模拟】基线消息。", "2026-11-08T10:00:00+08:00")
                + _article("msg-sim-new", "【模拟】新的买家消息，非真实客户。", "2026-11-08T13:32:44+08:00"),
                state="inbox",
                complete="true",
            ),
            encoding="utf-8",
        )
        report = runner.run_foreground(2, sleep_fn=_sleep_and_publish)
        # run_foreground starts with another full collect of the already-updated page,
        # then sleeps and collects once more. The first of these two sees the new message.
        self.assertEqual(sleeps, [120])
        self.assertEqual(report["state"], "stopped")
        self.assertFalse(report["real_store_connected"])
        self.assertFalse(report["real_store_login_verified"])
        self.assertFalse(report["live_dom_verified"])
        self.assertEqual(report["bot_integration"], "blocked_not_connected")
        self.assertFalse(report["bot_reply_received"])
        self.assertFalse(report["drafts_are_bot_replies"])
        self.assertEqual(report["alert_delivery"], "local_only")
        self.assertFalse(report["claims_user_wakeup"])
        self.assertFalse(report["sent_to_buyer"])
        self.assertEqual(report["drafts_ready_for_review"], 1)
        self.assertEqual(runner.store.count_inbounds(), 1)
        pid = runner.source.browser.pid
        self.assertIsInstance(pid, int)
        self.assertGreater(pid, 0)
        self.assertEqual(runner.store.get_meta("browser_pid"), str(pid))
        command = runner.source.browser.command
        self.assertNotIn("remote-" + "debugging-" + "port", command)
        self.assertNotIn("--headless", command)
        self.assertIn(str(self.data / "own-browser-profile"), command)
        self.assertNotIn("pinduoduo", command)
        self.assertTrue(process_exists(pid))
        page = runner.source.browser.page
        self.assertTrue(page.url.startswith(f"http://127.0.0.1:{self.port}/"))
        evidence_dir = evidence_directory()
        screenshot = evidence_dir / "browser-e2e-inbox.png"
        page.screenshot(path=str(screenshot))
        self.assertGreater(screenshot.stat().st_size, 0)
        spool = json.loads(next((self.data / "spool").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(spool["draft_text"], CANNED_DRAFT_TEXT)
        self.assertTrue(spool["synthetic"])
        self.assertFalse(spool["sent_to_buyer"])
        runner.close()
        deadline = time.monotonic() + 3
        while process_exists(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(process_exists(pid))
        reopened = self._runner()
        again = reopened.run_once()
        self.assertEqual(again["drafts_ready_for_review"], 1)
        self.assertEqual(reopened.store.count_inbounds(), 1)
        evidence = {
            "browser_opened": True,
            "headed": True,
            "debug_port_attached": False,
            "profile_copied_from_existing_login": False,
            "merchant_site_opened": False,
            "dom_change_produced_mock_draft": True,
            "restart_duplicate_inbound": False,
            "scheduler_interval_seconds": sleeps,
            "wall_clock_120_seconds_waited": False,
            "bot_integration": again["bot_integration"],
            "real_store_connected": False,
            "real_store_login_verified": False,
            "draft_text": spool["draft_text"],
            "drafts_are_bot_replies": False,
            "alert_delivery": "local_only",
            "evidence_directory": str(evidence_dir),
        }
        evidence_path = evidence_dir / "browser-e2e-evidence.json"
        evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        saved = json.loads(evidence_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["draft_text"], CANNED_DRAFT_TEXT)
        self.assertFalse(saved["restart_duplicate_inbound"])
        self.assertTrue(os.access(evidence_dir, os.W_OK))

    def test_value_shapes_are_classified_without_raw_values(self) -> None:
        from inbox_collector.dom_diagnose import export_rendered_dom
        from inbox_collector.browser_session import DedicatedBrowser

        pages = {
            "shell-ids": (
                "<div id='app'></div><div id='header'></div>"
                "<div class='chat-list-box'><div class='row'></div><div class='row'></div></div>"
            ),
            "format-time": (
                "<div class='chat-list-box'>"
                "<div class='row' msgtimeformate='YYYY-MM-DD HH:mm:ss'></div>"
                "<div class='row' msgtimeformate='HH:mm'></div></div>"
            ),
            "parsed-time": (
                "<div class='chat-list-box'>"
                "<div class='row' msgtimeformate='2026-11-08T13:32:44+08:00'></div>"
                "<div class='row' msgtimeformate='2026-11-08 09:01:02'></div></div>"
            ),
            "labels": (
                "<div class='chat-list-box'>"
                "<div class='row' aria-label='买家' title='已回复'></div>"
                "<div class='row' aria-label='商家' title='张三的订单'></div></div>"
            ),
            "roles-only": (
                "<div class='chat-list-box'>"
                "<div class='row' aria-label='买家'></div>"
                "<div class='row' aria-label='商家'></div></div>"
            ),
            "virtual": (
                "<div class='chat-list-box' aria-rowcount='40'>"
                "<div class='row' id='same'></div><div class='row' id='same'></div>"
                "<div class='row' id='hidden-one' hidden></div>"
                "<div class='row' id='hidden-two' style='display:none'></div></div>"
            ),
            "unique-once": (
                "<div class='list'>"
                "<div class='row' id='aa100001'></div>"
                "<div class='row' id='aa100002'></div></div>"
            ),
            "numeric-shell": (
                "<div id='100000123' class='wrap'>"
                "<div class='row'></div><div class='row'></div></div>"
            ),
            "duplicate-id": (
                "<div class='chat-list-box'>"
                "<div class='row' id='row-alpha'></div>"
                "<div class='row' id='row-alpha'></div></div>"
            ),
            "clock": (
                "<div class='chat-list-box'>"
                "<div class='row' msgtimeformate='13:32'></div>"
                "<div class='row' msgtimeformate='09:01:02'></div></div>"
            ),
            "unreadable-time": (
                "<div class='chat-list-box'>"
                "<div class='row' msgtimeformate='刚刚'></div>"
                "<div class='row' msgtimeformate='soon'></div></div>"
            ),
            "mixed-time": (
                "<div class='chat-list-box'>"
                "<div class='row' msgtimeformate='YYYY-MM-DD HH:mm:ss'></div>"
                "<div class='row' msgtimeformate='2026-11-08T13:32:44+08:00'></div></div>"
            ),
            "reply-only": (
                "<div class='chat-list-box'>"
                "<div class='row' title='已回复'></div>"
                "<div class='row' title='未回复'></div></div>"
            ),
            "panel": "<div class='panel'><span class='title'>hello</span></div>",
        }
        browser = DedicatedBrowser(self.data / "value-profile")
        browser.start()
        self.addCleanup(browser.close)
        self.html_path.write_text("<!DOCTYPE html><body><div class='ready'></div></body></html>", encoding="utf-8")
        browser.page.goto(f"http://127.0.0.1:{self.port}/inbox.html")
        pid = browser.pid
        findings = {}
        for name, inner in pages.items():
            self.html_path.write_text(f"<!DOCTYPE html><body>{inner}</body></html>", encoding="utf-8")
            before = f"http://127.0.0.1:{self.port}/inbox.html"
            browser.page.goto(before)
            report = export_rendered_dom(browser.page, timeout_seconds=2)
            self.assertEqual(browser.pid, pid)
            self.assertEqual(browser.page.url, before)
            rendered = json.dumps(report, ensure_ascii=False)
            for secret in (
                '"app"',
                '"header"',
                "YYYY-MM-DD",
                "HH:mm",
                "2026-11-08",
                "13:32:44",
                "13:32",
                "09:01",
                "张三",
                "刚刚",
                "hello",
                '"same"',
                "hidden-one",
                "hidden-two",
                "aa100001",
                "aa100002",
                "100000123",
                "row-alpha",
                "token=",
            ):
                self.assertNotIn(secret, rendered, name)
            self.assertFalse(report["value_assessment"]["can_use_for_dedupe"], name)
            self.assertFalse(report["value_assessment"]["can_treat_time_as_message_time"], name)
            self.assertFalse(report["production_mapping_validated"], name)
            findings[name] = report["value_assessment"]
        self.assertEqual(findings["shell-ids"]["id_evidence"], "not_on_repeated_rows")
        self.assertGreater(findings["shell-ids"]["shell_ids"]["generic_shell"], 0)
        self.assertEqual(findings["format-time"]["time_evidence"], "format_description_not_a_time")
        self.assertEqual(findings["parsed-time"]["time_evidence"], "parseable_shape_needs_human_meaning_check")
        self.assertEqual(findings["labels"]["role_evidence"], "non_allowlist_text_redacted")
        self.assertEqual(findings["roles-only"]["role_evidence"], "exact_role_labels_on_sampled_rows")
        self.assertFalse(findings["roles-only"]["can_treat_labels_as_reply_status"])
        self.assertTrue(findings["virtual"]["virtual_list"])
        self.assertGreaterEqual(findings["virtual"]["hidden_row_count"], 2)
        self.assertEqual(findings["virtual"]["id_evidence"], "incomplete_virtual_list")
        self.assertEqual(findings["virtual"]["groups"][0]["id"]["uniqueness"], "duplicated")
        self.assertEqual(findings["unique-once"]["id_evidence"], "unique_in_this_snapshot")
        self.assertEqual(findings["unique-once"]["id_stability"], "unproven_single_snapshot")
        self.assertEqual(findings["numeric-shell"]["id_evidence"], "not_on_repeated_rows")
        self.assertGreater(findings["numeric-shell"]["shell_ids"]["long_numeric"], 0)
        self.assertEqual(findings["duplicate-id"]["id_evidence"], "duplicated")
        self.assertEqual(findings["clock"]["time_evidence"], "clock_without_date")
        self.assertEqual(findings["unreadable-time"]["time_evidence"], "unreadable_redacted")
        self.assertEqual(findings["mixed-time"]["time_evidence"], "mixed_or_unreadable")
        self.assertEqual(findings["reply-only"]["role_evidence"], "reply_labels_without_direction")
        self.assertEqual(findings["panel"]["id_evidence"], "insufficient_no_repeated_rows")
        self.assertTrue(findings["unique-once"]["human_checks"])
        self.assertTrue(findings["parsed-time"]["human_checks"])
        self.assertTrue(findings["labels"]["human_checks"])

    def test_message_region_coverage_is_not_the_shallow_list(self) -> None:
        from inbox_collector.browser_session import DedicatedBrowser
        from inbox_collector.dom_diagnose import export_rendered_dom

        deep = "<ul><li class='chat-item'></li><li class='chat-item'></li></ul>"
        deep = f"<div class='reply-box'>{deep}</div>"
        deep = f"<div class='content-box'>{deep}</div>"
        for _ in range(20):
            deep = f"<div class='wrap'>{deep}</div>"
        buried = "<li class='chat-item'></li>"
        for _ in range(70):
            buried = f"<div class='wrap'>{buried}</div>"
        sample = "<li class='chat-item'></li><li class='chat-item'></li>"
        for _ in range(30):
            sample = f"<div class='wrap'>{sample}</div>"
        self.server.routes = {  # type: ignore[attr-defined]
            "/nested.html": b"<!DOCTYPE html><body><iframe src='/message.html'></iframe></body></html>",
            "/message.html": (
                "<!DOCTYPE html><body>" + deep + "<p>BUBBLE-TEXT-77</p></body></html>"
            ).encode(),
            "/pay/checkout.html": b"<!DOCTYPE html><body><div class='content-box'>PAY-SECRET</div></body></html>",
            "/off-scope/app.html": b"<!DOCTYPE html><body><div class='content-box'>OFF-SECRET</div></body></html>",
            "/login/page.html": (
                b"<!DOCTYPE html><body><input type='password' value='hunter2'>"
                b"<div class='content-box'>LOGIN-SECRET</div></body></html>"
            ),
        }
        pages = {
            "framed": (
                "<div class='chat-list-box'><div class='conv'></div><div class='conv'></div></div>"
                "<div class='loadMoreMsg'></div><iframe src='/nested.html'></iframe>"
            ),
            "deep-main": (
                "<div class='chat-list-box'><div class='conv'></div><div class='conv'></div></div>" + deep
            ),
            "hidden": (
                "<div class='content-box' hidden><div class='reply-box' style='display:none'>"
                "<ul><li class='chat-item' hidden></li><li class='chat-item' hidden></li></ul>"
                "</div></div>"
            ),
            "unreadable": (
                "<div class='chat-list-box'><div class='conv'></div><div class='conv'></div></div>"
                "<iframe sandbox srcdoc=\"<div class='content-box'>SECRET-BUBBLE-991</div>\"></iframe>"
            ),
            "password": (
                "<div class='chat-list-box'></div>"
                "<iframe srcdoc=\"<input type='password' value='hunter2'>"
                "<div class='content-box'><li class='chat-item'>SECRET-ROLE</li></div>\"></iframe>"
            ),
            "payment": "<div class='chat-list-box'></div><iframe src='/pay/checkout.html'></iframe>",
            "offscope": "<div class='chat-list-box'></div><iframe src='/off-scope/app.html'></iframe>",
            "shadow": "<div id='host'></div><div class='chat-list-box'></div>",
            "closed-shadow": "<div id='host'></div><div class='chat-list-box'></div>",
            "sample-depth": f"<div class='content-box'>{sample}</div>",
            "depth-limit": f"<div class='chat-list-box'></div><div class='content-box'>{buried}</div>",
            "login-top": "<div class='content-box'>LOGIN-SECRET</div>",
        }
        browser = DedicatedBrowser(self.data / "coverage-profile")
        browser.start()
        self.addCleanup(browser.close)
        pid = browser.pid
        findings = {}
        for name, inner in pages.items():
            target = "/login/page.html" if name == "login-top" else "/inbox.html"
            if target == "/inbox.html":
                self.html_path.write_text(
                    f"<!DOCTYPE html><body>{inner}</body></html>",
                    encoding="utf-8",
                )
            browser.page.goto(f"http://127.0.0.1:{self.port}{target}")
            if name == "shadow":
                browser.page.evaluate(
                    "() => { const root = document.querySelector('#host').attachShadow({mode:'open'});"
                    " root.innerHTML = \"<div class='content-box'><ul><li class='chat-item'></li>"
                    "<li class='chat-item'>SHADOW-SECRET</li></ul></div>\"; }"
                )
            if name == "closed-shadow":
                browser.page.evaluate(
                    "() => { const root = document.querySelector('#host').attachShadow({mode:'closed'});"
                    " root.innerHTML = \"<div class='content-box'>CLOSED-SECRET</div>\"; }"
                )
            before = browser.page.url
            report = export_rendered_dom(browser.page, timeout_seconds=5)
            self.assertEqual(browser.page.url, before, name)
            self.assertEqual(browser.pid, pid, name)
            rendered = json.dumps(report, ensure_ascii=False)
            for secret in (
                "BUBBLE-TEXT-77",
                "SECRET-BUBBLE-991",
                "SECRET-ROLE",
                "PAY-SECRET",
                "OFF-SECRET",
                "LOGIN-SECRET",
                "SHADOW-SECRET",
                "CLOSED-SECRET",
                "hunter2",
                "/message.html",
                "/nested.html",
                "/pay/checkout.html",
                "/off-scope/app.html",
                "token=",
            ):
                self.assertNotIn(secret, rendered, name)
            self.assertFalse(report["platform_unsupported"], name)
            self.assertFalse(report["production_mapping_validated"], name)
            self.assertFalse(report["collection_enabled"], name)
            self.assertFalse(report["selectors_inferred"], name)
            self.assertFalse(report["message_region"]["conversation_list_counted_as_messages"], name)
            self.assertTrue(report["coverage"]["closed_shadow_not_observable"], name)
            findings[name] = report
        framed = findings["framed"]["message_region"]
        self.assertTrue(framed["reached"])
        self.assertTrue(framed["in_frame"])
        self.assertFalse(framed["hidden_only"])
        self.assertEqual(set(framed["landmarks"]), {"content-box", "reply-box"})
        self.assertNotIn("chat-item", framed["landmarks"])
        self.assertFalse(framed["chat_item_counts_as_message_region"])
        self.assertIn("chat-list-box", framed["conversation_list_landmarks"])
        self.assertIn("chat-item", framed["conversation_list_landmarks"])
        self.assertIn("loadMoreMsg", framed["history_controls"])
        self.assertGreaterEqual(framed["max_landmark_depth"], 20)
        self.assertGreaterEqual(findings["framed"]["frames"]["entered"], 2)
        self.assertEqual(findings["framed"]["status"], "identity_unproven")
        self.assertTrue(
            any(node.get("class_structure") == ["chat-item"] for node in findings["framed"]["nodes"])
        )
        self.assertTrue(
            any(
                group.get("region") == "message_container"
                for group in findings["framed"]["value_assessment"]["groups"]
            )
        )
        self.assertTrue(findings["deep-main"]["message_region"]["reached"])
        self.assertFalse(findings["deep-main"]["message_region"]["in_frame"])
        self.assertGreaterEqual(findings["deep-main"]["message_region"]["max_landmark_depth"], 20)
        self.assertFalse(findings["deep-main"]["coverage"]["incomplete"])
        self.assertTrue(findings["hidden"]["message_region"]["reached"])
        self.assertTrue(findings["hidden"]["message_region"]["hidden_only"])
        unreadable = findings["unreadable"]
        self.assertFalse(unreadable["message_region"]["reached"])
        self.assertGreaterEqual(unreadable["frames"]["by_category"].get("unreadable", 0), 1)
        self.assertTrue(unreadable["truncated"])
        self.assertIn("unreadable_frame", unreadable["coverage"]["reasons"])
        self.assertEqual(unreadable["status"], "coverage_incomplete")
        self.assertEqual(
            unreadable["message_region"]["reason"],
            "conversation_list_observed_message_region_not_sampled",
        )
        self.assertGreaterEqual(findings["password"]["frames"]["by_category"].get("login_or_credential", 0), 1)
        self.assertNotIn("content-box", findings["password"]["message_region"]["landmarks"])
        self.assertGreaterEqual(findings["payment"]["frames"]["by_category"].get("payment", 0), 1)
        self.assertNotIn("content-box", findings["payment"]["message_region"]["landmarks"])
        self.assertGreaterEqual(findings["offscope"]["frames"]["by_category"].get("off_scope", 0), 1)
        self.assertNotIn("content-box", findings["offscope"]["message_region"]["landmarks"])
        self.assertTrue(findings["shadow"]["message_region"]["reached"])
        self.assertTrue(findings["shadow"]["message_region"]["in_open_shadow"])
        self.assertFalse(findings["closed-shadow"]["message_region"]["reached"])
        self.assertEqual(
            findings["closed-shadow"]["message_region"]["reason"],
            "not_observed_within_bounds",
        )
        self.assertTrue(findings["sample-depth"]["message_region"]["reached"])
        self.assertIn("sample_depth", findings["sample-depth"]["coverage"]["reasons"])
        self.assertTrue(findings["sample-depth"]["truncated"])
        self.assertIn("content-box", findings["sample-depth"]["message_region"]["landmarks"])
        self.assertNotIn("chat-item", findings["sample-depth"]["message_region"]["landmarks"])
        self.assertTrue(findings["depth-limit"]["message_region"]["reached"])
        self.assertIn("depth_limit", findings["depth-limit"]["coverage"]["reasons"])
        self.assertTrue(findings["depth-limit"]["truncated"])
        self.assertGreater(findings["depth-limit"]["coverage"]["omitted_by_depth"], 0)
        login = findings["login-top"]
        self.assertEqual(login["coverage"]["top_page"], "login_or_credential")
        self.assertFalse(login["message_region"]["reached"])
        self.assertEqual(login["status"], "coverage_incomplete")
        self.assertEqual(login["frames"]["entered"], 0)

    def test_message_preflight_associates_rows_without_raw_values(self) -> None:
        from inbox_collector.browser_session import DedicatedBrowser
        from inbox_collector.dom_diagnose import export_rendered_dom

        hidden_items = "".join("<li class='chat-item' hidden></li>" for _ in range(6))
        pages = {
            "rows": (
                f"<ul class='chat-list-box'>{hidden_items}</ul>"
                "<div class='content-box'>"
                "<div class='day-head'>2026年11月8日</div>"
                "<li class='clearfix onemsg' id='row-a'>"
                "<div class='merchantMessage'><div class='buyer-item'>"
                "<p class='msg-content-box'>普通咨询甲</p></div>"
                "<p class='message-time'>09:01</p></div></li>"
                "<li class='clearfix onemsg' id='row-b'>"
                "<div class='merchantMessage'><p class='msg-content-box'>普通咨询乙</p>"
                "<p class='message-time'>09:02</p><div class='cs-item isread'></div></div></li>"
                "<li class='clearfix onemsg' id='row-a' hidden>"
                "<div class='merchantMessage'><p class='msg-content-box'>隐藏重复</p></div></li>"
                "</div>"
                "<textarea class='reply-editor'></textarea>"
                "<button type='button'>发送</button>"
                "<input type='password' value='hunter2'>"
            ),
            "same-text": (
                "<div class='content-box'>"
                "<li class='onemsg' id='id-alpha'><div class='merchantMessage'>"
                "<p class='msg-content-box'>相同的模拟问句</p><p class='message-time'>13:32</p></div></li>"
                "<li class='onemsg' id='id-beta'><div class='merchantMessage'>"
                "<p class='msg-content-box'>相同的模拟问句</p><p class='message-time'>13:33</p></div></li>"
                "</div>"
            ),
            "clock": (
                "<div class='content-box'><li class='onemsg' id='clock-1'><div class='merchantMessage'>"
                "<p class='message-time'>09:15</p></div></li>"
                "<li class='onemsg' id='clock-2'><div class='merchantMessage'>"
                "<p class='message-time'>09:16</p></div></li></div>"
            ),
            "dated": (
                "<div class='content-box'><li class='onemsg' id='dated-1'><div class='merchantMessage'>"
                "<p class='message-time'>2026-11-08 09:01</p></div></li>"
                "<li class='onemsg' id='dated-2'><div class='merchantMessage'>"
                "<p class='message-time'>2026-11-08 09:02</p></div></li></div>"
            ),
            "list-only": f"<ul class='chat-list-box'>{hidden_items}</ul>",
            "custom-send": (
                "<ul class='chat-list-box'>"
                "<li class='chat-item is-active'><span class='mark'></span></li>"
                "<li class='chat-item'></li><li class='chat-item'></li></ul>"
                "<div class='content-box'>"
                "<li class='onemsg' id='only-a'><div class='merchantMessage'></div></li>"
                "<li class='onemsg' id='only-b'><div class='merchantMessage'></div></li></div>"
                "<div class='composer'><textarea class='reply-editor'></textarea>"
                "<div class='send-icon'></div></div>"
                "<button hidden>隐藏按钮文字</button>"
            ),
        }
        browser = DedicatedBrowser(self.data / "preflight-profile")
        browser.start()
        self.addCleanup(browser.close)
        findings = {}
        for name, inner in pages.items():
            self.html_path.write_text(f"<!DOCTYPE html><body>{inner}</body></html>", encoding="utf-8")
            browser.page.goto(f"http://127.0.0.1:{self.port}/inbox.html")
            report = export_rendered_dom(browser.page, timeout_seconds=5)
            rendered = json.dumps(report, ensure_ascii=False)
            for secret in (
                "2026年11月8日",
                "2026-11-08",
                "09:01",
                "09:02",
                "09:15",
                "13:32",
                "普通咨询甲",
                "相同的模拟问句",
                "隐藏重复",
                "row-a",
                "id-alpha",
                "id-beta",
                "hunter2",
                "only-a",
                "隐藏按钮文字",
            ):
                self.assertNotIn(secret, rendered, name)
            self.assertFalse(report["message_preflight"]["chat_item_counts_as_message_region"], name)
            self.assertFalse(report["message_region"]["chat_item_counts_as_message_region"], name)
            self.assertEqual(report["message_preflight"]["direction_evidence"], "insufficient_name_not_used", name)
            self.assertEqual(report["message_preflight"]["reply_evidence"], "read_unknown", name)
            self.assertFalse(report["message_preflight"]["can_treat_time_as_message_time"], name)
            self.assertFalse(report["message_preflight"]["id_stability"]["can_use_for_dedupe"], name)
            self.assertEqual(report["message_preflight"]["id_stability"]["cross_restart"], "unproven", name)
            self.assertFalse(report["production_mapping_validated"], name)
            findings[name] = report
        rows = findings["rows"]["message_preflight"]
        self.assertTrue(findings["rows"]["message_region"]["reached"])
        self.assertNotIn("chat-item", findings["rows"]["message_region"]["landmarks"])
        self.assertIn("chat-item", findings["rows"]["message_region"]["conversation_list_landmarks"])
        self.assertEqual(rows["visible_rows"], 2)
        self.assertGreaterEqual(rows["hidden_rows"], 1)
        self.assertEqual(rows["merchant_message"]["relation"], "covers_every_visible_row")
        self.assertFalse(rows["merchant_message"]["used_as_direction"])
        self.assertEqual(rows["buyer_item"]["inside_merchant_message"], 1)
        self.assertEqual(rows["time_evidence"], "clock_with_adjacent_date_header")
        self.assertGreaterEqual(rows["read_mark_rows"], 1)
        self.assertGreaterEqual(rows["hidden_id_collides_with_visible"], 1)
        self.assertEqual(rows["id_stability"]["observations"], 2)
        self.assertEqual(rows["id_stability"]["scope"], "same_page_two_frames")
        self.assertEqual(rows["id_stability"]["same_page_result"], "unchanged")
        self.assertEqual(rows["id_shapes"]["other_shape"], 2)
        self.assertEqual(findings["rows"]["reason"], "message_rows_observed_direction_time_reply_unconfirmed")
        acceptance = findings["rows"]["page_acceptance"]
        self.assertEqual(acceptance["conversation_binding"], "row_class_comparison_not_available")
        self.assertTrue(acceptance["send_control_absence_not_concluded"])
        self.assertGreaterEqual(acceptance["allowlist_send_label_hits"], 1)
        self.assertFalse(acceptance["conversation_bound_to_message_region"])
        self.assertEqual(acceptance["direction_evidence"], "insufficient_name_not_used")
        self.assertEqual(acceptance["direction_split"]["mark_inside_wrapper"], 1)
        self.assertGreaterEqual(acceptance["direction_split"]["rows_without_mark"], 1)
        self.assertEqual(acceptance["usable_input_count"], 1)
        self.assertEqual(acceptance["send_label_button_count"], 1)
        self.assertGreaterEqual(acceptance["password_inputs_skipped"], 1)
        self.assertFalse(acceptance["official_composer_verified"])
        self.assertFalse(acceptance["values_read"])
        self.assertFalse(acceptance["controls_activated"])
        self.assertFalse(acceptance["production_send_allowed"])
        self.assertTrue(acceptance["human_checks"])
        same = findings["same-text"]["message_preflight"]
        self.assertEqual(same["same_text_different_id_rows"], 2)
        self.assertEqual(same["time_evidence"], "clock_without_date")
        self.assertEqual(findings["clock"]["message_preflight"]["time_evidence"], "clock_without_date")
        self.assertEqual(
            findings["dated"]["message_preflight"]["time_evidence"],
            "parseable_shape_on_row_meaning_unconfirmed",
        )
        self.assertFalse(findings["list-only"]["message_region"]["reached"])
        self.assertEqual(findings["list-only"]["message_preflight"]["visible_rows"], 0)
        self.assertIn("chat-item", findings["list-only"]["message_region"]["conversation_list_landmarks"])
        custom = findings["custom-send"]["page_acceptance"]
        self.assertEqual(custom["conversation_binding"], "unique_row_class_observed")
        self.assertIn("is-active", custom["unique_row_classes"])
        self.assertTrue(custom["selection_absence_not_concluded"])
        self.assertFalse(custom["official_composer_verified"])
        self.assertTrue(custom["send_control_absence_not_concluded"])
        self.assertEqual(custom["send_label_button_count"], 0)
        self.assertGreaterEqual(custom["hidden_button_count"], 1)
        near = [item for item in custom["near_input"] if "send-icon" in item["classes"]]
        self.assertTrue(near)
        self.assertEqual(near[0]["tag"], "div")
        self.assertEqual(near[0]["label_kind"], "empty")
        self.assertEqual(near[0]["relation"], "same_parent_as_input")
        self.assertGreaterEqual(custom["custom_visible_near_input_count"], 1)

    def test_simulated_auto_reply_commits_on_localhost_only(self) -> None:
        from inbox_collector.auto_reply import LoopbackGenerator, run_simulated_cycle
        from inbox_collector.browser_session import DedicatedBrowser
        from inbox_collector.store import Store

        def page_html(inner: str, *, state: str = "inbox", composer: str = "conv-sim-1", receipt: bool = True) -> str:
            receipt_js = (
                "const item = document.createElement('li');"
                "item.className = 'sim-outgoing onemsg';"
                "item.setAttribute('data-receipt', 'sent');"
                "item.setAttribute('data-sim-direction', 'merchant');"
                "item.textContent = text;"
                "document.querySelector('.content-box').appendChild(item);"
            )
            action = receipt_js if receipt else ""
            return (
                "<!DOCTYPE html><html data-simulated-inbox='true'><body "
                f"data-page-state='{state}' data-open-conversation='conv-sim-1'>"
                f"<div class='content-box' data-sim-conversation='conv-sim-1'>{inner}</div>"
                f"<textarea id='sim-composer' data-sim-conversation='{composer}'></textarea>"
                "<button type='button' id='sim-send'>发送</button><script>"
                "window.__sendClicks = 0; window.__enterUsed = false;"
                "document.querySelector('#sim-send').addEventListener('click', () => {"
                "window.__sendClicks += 1;"
                "const text = document.querySelector('#sim-composer').value;"
                f"{action}"
                "});"
                "document.querySelector('#sim-composer').addEventListener('keydown', (event) => {"
                "if (event.key === 'Enter') window.__enterUsed = true;"
                "});"
                "</script></body></html>"
            )

        buyer = (
            "<li class='clearfix onemsg' id='m-1' data-sim-direction='buyer'>"
            "<div class='merchantMessage'><p class='msg-content-box'>【模拟】你好，在吗</p>"
            "<p class='message-time'>09:01</p></div></li>"
        )
        tags = {
            "models": [
                {"name": "qwen-kefu:latest", "size": 1117321521, "details": {"family": "qwen2"}}
            ]
        }
        calls: list[tuple[str, str]] = []

        def transport(method: str, path: str, body: dict | None, timeout: int) -> tuple[int, dict]:
            calls.append((method, path))
            self.assertEqual(timeout, 45)
            if path == "/api/tags":
                return 200, tags
            self.assertEqual(method, "POST")
            assert body is not None
            self.assertEqual(body["model"], "qwen-kefu:latest")
            self.assertEqual(body["options"]["num_predict"], 24)
            self.assertFalse(body["stream"])
            return 200, {"response": "您好，已收到。", "done": True, "done_reason": "stop"}

        browser = DedicatedBrowser(self.data / "reply-profile")
        browser.start()
        self.addCleanup(browser.close)
        store = Store(self.data / "reply-store")
        self.addCleanup(store.close)
        generator = LoopbackGenerator(transport, transport_label="injected_test_double")
        history = (
            "<li class='onemsg' id='hist-1' data-sim-direction='buyer'>"
            "<p class='msg-content-box'>【模拟】你好，在吗</p></li>"
        )
        self.html_path.write_text(page_html(history), encoding="utf-8")
        browser.page.goto(f"http://127.0.0.1:{self.port}/inbox.html")
        baseline = run_simulated_cycle(
            browser.page, store, generator, min_interval=0, max_per_hour=3, now=1_700_000_000
        )
        self.assertEqual(baseline["status"], "baseline_established")
        self.assertEqual(baseline["composer_commits"], 0)
        self.assertFalse(baseline["text_merge_applied"])
        self.assertEqual(calls, [])
        self.html_path.write_text(page_html(history + buyer.replace("m-1", "new-1")), encoding="utf-8")
        browser.page.goto(f"http://127.0.0.1:{self.port}/inbox.html")
        first = run_simulated_cycle(
            browser.page, store, generator, min_interval=0, max_per_hour=3, now=1_700_000_010
        )
        self.assertEqual(first["status"], "simulated_sent")
        self.assertFalse(first["real_buyer_send"])
        self.assertFalse(first["production_send_allowed"])
        self.assertTrue(first["injected_test_transport"])
        self.assertFalse(first["process_loopback_http_completed"])
        self.assertEqual(first["process_loopback_http"], [])
        self.assertFalse(first["customer_service_quality_verified"])
        self.assertFalse(first["official_composer_verified"])
        self.assertFalse(first["text_merge_applied"])
        self.assertEqual(first["text_collision_count"], 2)
        self.assertEqual(first["generator_transport"], "injected_test_double")
        self.assertEqual(first["model"], "qwen-kefu:latest")
        self.assertEqual(first["num_predict"], 24)
        self.assertEqual(first["composer_commits"], 1)
        self.assertIn("mapping_not_validated", first["production_blockers"])
        self.assertEqual(calls, [("GET", "/api/tags"), ("POST", "/api/generate")])
        self.assertEqual(browser.page.evaluate("() => window.__sendClicks"), 1)
        self.assertFalse(browser.page.evaluate("() => window.__enterUsed"))
        self.assertIn("【模拟】您好，已收到。", browser.page.locator(".sim-outgoing").inner_text())
        browser.page.evaluate("() => document.querySelectorAll('.sim-outgoing').forEach((node) => node.remove())")
        second = run_simulated_cycle(
            browser.page, store, generator, min_interval=0, max_per_hour=3, now=1_700_000_020
        )
        self.assertEqual(second["status"], "no_eligible_buyer_message")
        self.assertEqual(browser.page.evaluate("() => window.__sendClicks"), 1)
        self.assertEqual(len(calls), 2)

        refund_store = Store(self.data / "refund-store")
        self.addCleanup(refund_store.close)
        self.html_path.write_text(page_html(history), encoding="utf-8")
        browser.page.goto(f"http://127.0.0.1:{self.port}/inbox.html")
        self.assertEqual(
            run_simulated_cycle(browser.page, refund_store, generator, min_interval=0, max_per_hour=3, now=20)[
                "status"
            ],
            "baseline_established",
        )
        before = len(calls)
        self.html_path.write_text(
            page_html(
                history
                + "<li class='onemsg' id='refund-1' data-sim-direction='buyer'>"
                "<p class='msg-content-box'>【模拟】我要退款</p></li>"
            ),
            encoding="utf-8",
        )
        browser.page.goto(f"http://127.0.0.1:{self.port}/inbox.html")
        refund = run_simulated_cycle(browser.page, refund_store, generator, min_interval=0, max_per_hour=3, now=21)
        self.assertEqual(refund["status"], "no_eligible_buyer_message")
        self.assertEqual(len(calls), before)
        self.assertEqual(browser.page.evaluate("() => window.__sendClicks"), 0)

        dup_store = Store(self.data / "dup-store")
        self.addCleanup(dup_store.close)
        same = (
            "<li class='onemsg' id='id-a' data-sim-direction='buyer'>"
            "<p class='msg-content-box'>【模拟】你好，相同问句</p></li>"
            "<li class='onemsg' id='id-b' data-sim-direction='buyer'>"
            "<p class='msg-content-box'>【模拟】你好，相同问句</p></li>"
        )
        self.html_path.write_text(page_html(history), encoding="utf-8")
        browser.page.goto(f"http://127.0.0.1:{self.port}/inbox.html")
        self.assertEqual(
            run_simulated_cycle(browser.page, dup_store, generator, min_interval=0, max_per_hour=3, now=30)["status"],
            "baseline_established",
        )
        self.html_path.write_text(page_html(history + same), encoding="utf-8")
        browser.page.goto(f"http://127.0.0.1:{self.port}/inbox.html")
        dup = run_simulated_cycle(browser.page, dup_store, generator, min_interval=0, max_per_hour=3, now=31)
        self.assertEqual(dup["status"], "simulated_sent")
        self.assertFalse(dup["text_merge_applied"])
        self.assertGreaterEqual(dup["text_collision_count"], 2)
        self.assertEqual(browser.page.evaluate("() => window.__sendClicks"), 1)
        browser.page.evaluate("() => document.querySelectorAll('.sim-outgoing').forEach((node) => node.remove())")
        dup_again = run_simulated_cycle(browser.page, dup_store, generator, min_interval=0, max_per_hour=3, now=32)
        self.assertEqual(dup_again["status"], "simulated_sent")
        self.assertEqual(browser.page.evaluate("() => window.__sendClicks"), 2)

        read_store = Store(self.data / "read-store")
        self.addCleanup(read_store.close)
        self.html_path.write_text(page_html(history), encoding="utf-8")
        browser.page.goto(f"http://127.0.0.1:{self.port}/inbox.html")
        self.assertEqual(
            run_simulated_cycle(browser.page, read_store, generator, min_interval=0, max_per_hour=3, now=40)["status"],
            "baseline_established",
        )
        self.html_path.write_text(
            page_html(
                "<li class='onemsg' id='read-1' data-sim-direction='buyer'>"
                "<div class='merchantMessage'><p class='msg-content-box'>【模拟】你好</p>"
                "<div class='cs-item isread'></div></div></li>"
            ),
            encoding="utf-8",
        )
        browser.page.goto(f"http://127.0.0.1:{self.port}/inbox.html")
        read_mark = run_simulated_cycle(browser.page, read_store, generator, min_interval=0, max_per_hour=3, now=41)
        self.assertEqual(read_mark["status"], "simulated_sent")

        replied_store = Store(self.data / "replied-store")
        self.addCleanup(replied_store.close)
        self.html_path.write_text(page_html(history), encoding="utf-8")
        browser.page.goto(f"http://127.0.0.1:{self.port}/inbox.html")
        self.assertEqual(
            run_simulated_cycle(browser.page, replied_store, generator, min_interval=0, max_per_hour=3, now=50)[
                "status"
            ],
            "baseline_established",
        )
        before = len(calls)
        self.html_path.write_text(
            page_html(
                history
                + "<li class='onemsg' id='buyer-2' data-sim-direction='buyer'>"
                "<p class='msg-content-box'>【模拟】你好</p></li>"
                "<li class='onemsg' id='human-2' data-sim-direction='merchant' data-sim-reply='human'>"
                "<p class='msg-content-box'>人工已经回复</p></li>"
            ),
            encoding="utf-8",
        )
        browser.page.goto(f"http://127.0.0.1:{self.port}/inbox.html")
        replied = run_simulated_cycle(browser.page, replied_store, generator, min_interval=0, max_per_hour=3, now=51)
        self.assertEqual(replied["status"], "no_eligible_buyer_message")
        self.assertEqual(len(calls), before)
        self.assertEqual(browser.page.evaluate("() => window.__sendClicks"), 0)

        blind_store = Store(self.data / "blind-store")
        self.addCleanup(blind_store.close)
        self.html_path.write_text(page_html(history), encoding="utf-8")
        browser.page.goto(f"http://127.0.0.1:{self.port}/inbox.html")
        self.assertEqual(
            run_simulated_cycle(browser.page, blind_store, generator, min_interval=0, max_per_hour=3, now=60)["status"],
            "baseline_established",
        )
        self.html_path.write_text(page_html(buyer, receipt=False), encoding="utf-8")
        browser.page.goto(f"http://127.0.0.1:{self.port}/inbox.html")
        uncertain = run_simulated_cycle(browser.page, blind_store, generator, min_interval=0, max_per_hour=3, now=61)
        self.assertEqual(uncertain["status"], "paused_uncertain")
        self.assertFalse(uncertain["resend_allowed"])
        self.assertEqual(browser.page.evaluate("() => window.__sendClicks"), 1)
        again = run_simulated_cycle(browser.page, blind_store, generator, min_interval=0, max_per_hour=3, now=62)
        self.assertEqual(again["status"], "refused_stop")
        self.assertEqual(browser.page.evaluate("() => window.__sendClicks"), 1)

        shift_store = Store(self.data / "shift-store")
        self.addCleanup(shift_store.close)

        def shifting(method: str, path: str, body: dict | None, timeout: int) -> tuple[int, dict]:
            if path == "/api/generate":
                browser.page.evaluate(
                    "() => document.body.setAttribute('data-open-conversation', 'conv-other')"
                )
            return transport(method, path, body, timeout)

        self.html_path.write_text(page_html(history), encoding="utf-8")
        browser.page.goto(f"http://127.0.0.1:{self.port}/inbox.html")
        self.assertEqual(
            run_simulated_cycle(browser.page, shift_store, generator, min_interval=0, max_per_hour=3, now=80)["status"],
            "baseline_established",
        )
        self.html_path.write_text(page_html(buyer), encoding="utf-8")
        browser.page.goto(f"http://127.0.0.1:{self.port}/inbox.html")
        shifted = run_simulated_cycle(
            browser.page,
            shift_store,
            LoopbackGenerator(shifting, transport_label="injected_test_double"),
            min_interval=0,
            max_per_hour=3,
            now=81,
        )
        self.assertEqual(shifted["status"], "refused_conversation_changed")
        self.assertEqual(browser.page.evaluate("() => window.__sendClicks"), 0)

        login_store = Store(self.data / "login-store")
        self.addCleanup(login_store.close)
        self.html_path.write_text(page_html(buyer, state="captcha"), encoding="utf-8")
        browser.page.goto(f"http://127.0.0.1:{self.port}/inbox.html")
        captcha = run_simulated_cycle(browser.page, login_store, generator, min_interval=0, max_per_hour=3, now=90)
        self.assertEqual(captcha["status"], "refused_page_captcha")
        self.assertEqual(browser.page.evaluate("() => window.__sendClicks"), 0)

    def test_auto_reply_sim_test_path_uses_ephemeral_port_not_11434(self) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.request import Request, urlopen

        from inbox_collector.auto_reply import AutoReplyError, require_loopback, run_local_ollama_sim

        seen: list[object] = []

        class _Stub(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                seen.append(("GET", self.path.split("?", 1)[0]))
                body = json.dumps(
                    {"models": [{"name": "qwen-kefu:latest", "size": 1117321521, "details": {"family": "qwen2"}}]}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or "0")
                payload = json.loads(self.rfile.read(length).decode())
                seen.append((self.path, payload.get("model"), payload.get("options", {}).get("num_predict")))
                body = json.dumps({"response": "您好，已收到。", "done": True, "done_reason": "stop"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt: str, *args: object) -> None:
                return

        stub = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
        port = int(stub.server_address[1])
        self.assertNotEqual(port, 11434)
        worker = threading.Thread(target=stub.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(stub.shutdown)
        self.addCleanup(stub.server_close)
        with self.assertRaises(AutoReplyError):
            require_loopback(f"http://127.0.0.1:{port}")

        def transport(method: str, path: str, body: dict | None, timeout: int) -> tuple[int, dict]:
            data = None if body is None else json.dumps(body).encode()
            request = Request(
                f"http://127.0.0.1:{port}{path}",
                data=data,
                method=method,
                headers={} if data is None else {"Content-Type": "application/json"},
            )
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode())
            return int(response.status), payload

        report = run_local_ollama_sim(transport=transport, transport_label="injected_test_double")
        self.assertEqual(report["status"], "simulated_sent")
        self.assertEqual(report["generator_transport"], "injected_test_double")
        self.assertTrue(report["injected_test_transport"])
        self.assertFalse(report["process_loopback_http_completed"])
        self.assertEqual(report["process_loopback_http"], [])
        self.assertFalse(report["customer_service_quality_verified"])
        self.assertFalse(report["real_buyer_send"])
        self.assertFalse(report["production_send_allowed"])
        self.assertFalse(report["merchant_profile_used"])
        self.assertTrue(report["isolated_profile"])
        self.assertFalse(report["text_merge_applied"])
        self.assertEqual(report["composer_commits_seen"], 1)
        self.assertEqual(report["text_collision_count"], 2)
        self.assertIn(("GET", "/api/tags"), seen)
        self.assertIn(("/api/generate", "qwen-kefu:latest", 24), seen)

    def test_diagnose_uses_the_open_page_without_navigation(self) -> None:
        from inbox_collector.dom_diagnose import export_rendered_dom, service_diagnose_request

        self.html_path.write_text(
            "<!DOCTYPE html><body><section class='shell' "
            "data-conversation-selected='false' data-v-secret tabindex='1'>"
            "<a href='https://example.invalid/buyer?token=secret'>买家张三</a>"
            "</section></body></html>",
            encoding="utf-8",
        )
        from inbox_collector.browser_session import DedicatedBrowser

        browser = DedicatedBrowser(self.data / "diag-profile")
        browser.start()
        self.addCleanup(browser.close)
        browser.page.goto(f"http://127.0.0.1:{self.port}/inbox.html")
        before = browser.page.url
        pid = browser.pid
        report = export_rendered_dom(browser.page, timeout_seconds=2)
        self.assertEqual(browser.page.url, before)
        self.assertEqual(browser.pid, pid)
        rendered = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("张三", rendered)
        self.assertNotIn("token=secret", rendered)
        self.assertNotIn("data-v-secret", rendered)
        self.assertEqual(report["status"], "conversation_not_selected")
        self.assertTrue(report["nodes"])
        self.assertFalse(report["platform_unsupported"])
        request = self.data / "dom-diagnose.request"
        request.write_text("{}\n", encoding="utf-8")
        service_diagnose_request(browser.page, self.data)
        saved = json.loads((self.data / "dom-diagnose.result.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "conversation_not_selected")
        self.assertEqual(browser.page.url, before)
        self.assertFalse(request.exists())

    def test_login_page_alerts_locally_without_typing(self) -> None:
        self.html_path.write_text(
            (ROOT / "fixtures" / "browser_login.html").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        runner = self._runner(login_wait_seconds=1)
        report = runner.run_once()
        self.assertEqual(report["state"], "paused")
        self.assertEqual(runner.store.alert_count("login_page"), 1)
        self.assertTrue(report["browser_launched"])
        self.assertEqual(report["drafts_ready_for_review"], 0)
        self.assertFalse(report["real_store_login_verified"])
        self.assertEqual(report["alert_delivery"], "local_only")
        self.assertFalse(report["claims_user_wakeup"])
        self.assertEqual(report["bot_integration"], "blocked_not_connected")

    def test_offline_page_alerts_after_the_browser_starts(self) -> None:
        runner = self._runner(entry_url="http://127.0.0.1:9/inbox.html", max_retries=1)
        report = runner.run_once()
        self.assertEqual(report["state"], "paused")
        self.assertEqual(runner.store.alert_count("network"), 1)
        self.assertTrue(report["browser_launched"])
        self.assertFalse(report["real_store_connected"])
        self.assertEqual(report["alert_delivery"], "local_only")

    def test_login_prep_opens_local_page_without_collecting_or_sending(self) -> None:
        self.html_path.write_text(
            (ROOT / "fixtures" / "browser_login.html").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        path = self._config(mode="login_prep", draft_provider="none", login_wait_seconds=1)
        loaded, problems = load_config(path)
        self.assertEqual(problems, [], problems)
        assert loaded is not None
        hosts: list[str] = []
        self.server.hosts = hosts  # type: ignore[attr-defined]
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("outbound")):
            report = run_login_prep(loaded, load_mapping(loaded.mapping_path), hold_seconds=0.4)
        self.assertEqual(report["opened_host"], "127.0.0.1")
        self.assertEqual(report["messages_collected"], 0)
        self.assertEqual(report["drafts_created"], 0)
        self.assertFalse(report["production_running"])
        self.assertFalse(report["merchant_collection_enabled"])
        self.assertFalse(report["real_store_login_verified"])
        self.assertFalse(report["real_store_connected"])
        self.assertNotEqual(report["state"], "running")
        self.assertEqual(report["state_reason"], "login_prep_closed")
        self.assertEqual(report["bot_integration"], "blocked_not_connected")
        self.assertTrue(hosts)
        self.assertTrue(all(host.startswith("127.0.0.1") for host in hosts))
        reopened = Store(self.data)
        try:
            states = [item[0] for item in reopened.run_log()]
            self.assertIn("login_prep", states)
            self.assertNotIn("running", states)
            self.assertEqual(reopened.count_messages(), 0)
        finally:
            reopened.close()
        marker = json.loads(
            (self.data / "own-browser-profile" / "inbox-collector-profile.json").read_text(encoding="utf-8")
        )
        self.assertFalse(marker["copied_login"])

    def test_browser_local_example_checks_without_launching(self) -> None:
        self.assertEqual(
            main(["check-config", "--config", str(ROOT / "config.browser-local.example.json")]),
            0,
        )
        self.assertEqual(
            main(["validate-mapping", "--mapping", str(MAPPING)]),
            0,
        )


if __name__ == "__main__":
    unittest.main()
