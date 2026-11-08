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
        self.assertEqual(set(framed["landmarks"]), {"content-box", "reply-box", "chat-item"})
        self.assertIn("chat-list-box", framed["conversation_list_landmarks"])
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
        self.assertIn("chat-item", findings["sample-depth"]["message_region"]["landmarks"])
        self.assertTrue(findings["depth-limit"]["message_region"]["reached"])
        self.assertIn("depth_limit", findings["depth-limit"]["coverage"]["reasons"])
        self.assertTrue(findings["depth-limit"]["truncated"])
        self.assertGreater(findings["depth-limit"]["coverage"]["omitted_by_depth"], 0)
        login = findings["login-top"]
        self.assertEqual(login["coverage"]["top_page"], "login_or_credential")
        self.assertFalse(login["message_region"]["reached"])
        self.assertEqual(login["status"], "coverage_incomplete")
        self.assertEqual(login["frames"]["entered"], 0)

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
