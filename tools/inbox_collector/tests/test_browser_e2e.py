"""Headed Chromium against a local fixture page. No merchant site is opened."""

from __future__ import annotations

import json
import os
import subprocess
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from unittest import mock

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
ARTIFACTS = Path("/opt/cursor/artifacts")


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
        body = self.server.html_path.read_bytes()  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        return


class BrowserE2ETest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._xvfb: subprocess.Popen[bytes] | None = None
        if not os.environ.get("DISPLAY"):
            cls._xvfb = subprocess.Popen(
                ["Xvfb", ":99", "-screen", "0", "1280x800x24"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            os.environ["DISPLAY"] = ":99"
            time.sleep(0.4)

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
        pid = int(runner.store.get_meta("browser_pid") or "0")
        self.assertGreater(pid, 0)
        command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="replace")
        self.assertNotIn("remote-" + "debugging-" + "port", command)
        self.assertNotIn("--headless", command)
        self.assertIn(str(self.data / "own-browser-profile"), command)
        self.assertNotIn("pinduoduo", command)
        page = runner.source.browser.page
        self.assertTrue(page.url.startswith(f"http://127.0.0.1:{self.port}/"))
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(ARTIFACTS / "browser-e2e-inbox.png"))
        spool = json.loads(next((self.data / "spool").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(spool["draft_text"], CANNED_DRAFT_TEXT)
        self.assertTrue(spool["synthetic"])
        self.assertFalse(spool["sent_to_buyer"])
        runner.close()
        self.assertFalse(Path(f"/proc/{pid}").exists())
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
        }
        (ARTIFACTS / "browser-e2e-evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

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
