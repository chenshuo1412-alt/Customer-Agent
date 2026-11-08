"""Auto-reply gates and the injected Ollama contract. These tests do not contact a Mac."""

from __future__ import annotations

import unittest
from tempfile import TemporaryDirectory

from inbox_collector.auto_reply import (
    VERIFIED_MODEL,
    AutoReplyError,
    LoopbackGenerator,
    gate_report,
    require_loopback,
    run_simulated_cycle,
)
from inbox_collector.cli import main
from inbox_collector.store import Store


TAGS = {
    "models": [
        {"name": VERIFIED_MODEL, "size": 1117321521, "details": {"family": "qwen2"}}
    ]
}


class _Page:
    def __init__(self, url: str = "http://127.0.0.1:9/inbox.html", snapshot: dict | None = None) -> None:
        self.url = url
        self.snapshot = snapshot or {
            "ok": True,
            "page_state": "inbox",
            "open_conversation": "conv-sim-1",
            "box_conversation": "conv-sim-1",
            "composer_conversation": "conv-sim-1",
            "has_composer": True,
            "has_button": True,
            "messages": [
                {
                    "id": "m-1",
                    "direction": "buyer",
                    "body": "【模拟】你好，在吗",
                    "hidden": False,
                    "read_mark": False,
                    "human_reply": "",
                    "receipt": "",
                }
            ],
        }
        self.located: list[str] = []

    def evaluate(self, script: str, *args: object) -> object:
        if args:
            return False
        return self.snapshot

    def locator(self, selector: str) -> object:
        self.located.append(selector)
        raise AssertionError(selector)


class AutoReplyGateTest(unittest.TestCase):
    def test_switch_does_not_open_production_or_claim_the_mac(self) -> None:
        report = gate_report({"auto_reply": {"enabled": True, "scope": "production"}})
        self.assertFalse(report["production_send_allowed"])
        self.assertEqual(report["scope"], "refused")
        self.assertIn("mapping_not_validated", report["blockers"])
        self.assertIn("official_composer_not_verified", report["blockers"])
        self.assertFalse(report["switch_bypasses_gates"])
        self.assertFalse(report["mac_ollama_contacted"])
        self.assertFalse(report["customer_service_quality_verified"])
        self.assertFalse(report["routine_is_sync_generator"])
        self.assertFalse(report["real_buyer_send"])
        configured = gate_report({"auto_reply": {"enabled": True, "scope": "local_simulation"}})
        self.assertTrue(configured["local_simulation_configured"])
        self.assertFalse(configured["production_send_allowed"])

    def test_loopback_rejects_other_origins_without_a_request(self) -> None:
        for url in (
            "https://api.openai.com/v1",
            "http://127.0.0.1:11435",
            "http://localhost:11434",
            "http://127.0.0.1:11434/api/generate",
            "http://user:pass@127.0.0.1:11434",
        ):
            with self.assertRaises(AutoReplyError):
                require_loopback(url)
        self.assertEqual(require_loopback("http://127.0.0.1:11434"), "http://127.0.0.1:11434")

    def test_merchant_page_is_not_read(self) -> None:
        page = _Page("https://mms.pinduoduo.com/chat-merchant/index.html")
        page.evaluate = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("read"))  # type: ignore[method-assign]
        with TemporaryDirectory() as tmp:
            store = Store(__import__("pathlib").Path(tmp))
            try:
                report = run_simulated_cycle(
                    page,
                    store,
                    LoopbackGenerator(lambda *args: (500, {}), transport_label="injected_test_double"),
                    now=1,
                )
            finally:
                store.close()
        self.assertEqual(report["status"], "refused_not_local")
        self.assertEqual(page.located, [])
        self.assertFalse(report["mac_ollama_contacted"])

    def test_model_output_that_promises_a_refund_is_not_committed(self) -> None:
        def transport(method: str, path: str, body: dict | None, timeout: int) -> tuple[int, dict]:
            if path == "/api/tags":
                return 200, TAGS
            return 200, {"response": "可以退款成功", "done": True, "done_reason": "stop"}

        page = _Page()
        with TemporaryDirectory() as tmp:
            store = Store(__import__("pathlib").Path(tmp))
            try:
                report = run_simulated_cycle(
                    page,
                    store,
                    LoopbackGenerator(transport, transport_label="injected_test_double"),
                    min_interval=0,
                    now=1,
                )
            finally:
                store.close()
        self.assertEqual(report["status"], "ollama_output_rejected")
        self.assertEqual(page.located, [])
        self.assertFalse(report["customer_service_quality_verified"])
        self.assertFalse(report["mac_ollama_contacted"])

    def test_missing_verified_model_does_not_call_generate(self) -> None:
        paths: list[str] = []

        def transport(method: str, path: str, body: dict | None, timeout: int) -> tuple[int, dict]:
            paths.append(path)
            return 200, {"models": [{"name": "other:latest"}]}

        page = _Page()
        with TemporaryDirectory() as tmp:
            store = Store(__import__("pathlib").Path(tmp))
            try:
                report = run_simulated_cycle(
                    page,
                    store,
                    LoopbackGenerator(transport, transport_label="injected_test_double"),
                    min_interval=0,
                    now=1,
                )
            finally:
                store.close()
        self.assertEqual(report["status"], "ollama_model_missing")
        self.assertEqual(paths, ["/api/tags"])
        self.assertEqual(page.located, [])

    def test_rate_limit_and_stop_do_not_read_the_page(self) -> None:
        page = _Page()
        page.evaluate = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("read"))  # type: ignore[method-assign]
        with TemporaryDirectory() as tmp:
            store = Store(__import__("pathlib").Path(tmp))
            try:
                store.set_meta("auto_reply_window_start", "0")
                store.set_meta("auto_reply_window_count", "3")
                limited = run_simulated_cycle(
                    page,
                    store,
                    LoopbackGenerator(lambda *args: (500, {}), transport_label="injected_test_double"),
                    max_per_hour=3,
                    now=10,
                )
                self.assertEqual(limited["status"], "refused_rate_limit")
                store.set_meta("auto_reply_stopped", "1")
                stopped = run_simulated_cycle(
                    page,
                    store,
                    LoopbackGenerator(lambda *args: (500, {}), transport_label="injected_test_double"),
                    now=10,
                )
                self.assertEqual(stopped["status"], "refused_stop")
            finally:
                store.close()

    def test_gates_command_does_not_contact_a_generator(self) -> None:
        with TemporaryDirectory() as tmp:
            path = __import__("pathlib").Path(tmp) / "config.json"
            path.write_text('{"auto_reply": {"enabled": true, "scope": "local_simulation"}}', encoding="utf-8")
            code = main(["auto-reply-gates", "--config", str(path)])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
