"""Fake HTTP coverage for the Grok routine bearer mode. No live webhook is called."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from inbox_collector.keychain import MemorySecretStore
from inbox_collector.providers import WEBHOOK_NETWORK_ENV
from inbox_collector.redact import RedactFilter
from inbox_collector.routine import (
    RoutineBearerProvider,
    import_routine_result,
    save_routine_setup,
)
from inbox_collector.store import Store
import logging


class FakeHttp:
    def __init__(self, status: int, body: bytes = b'{"draft_text":"forged"}') -> None:
        self.status = status
        self.body = body
        self.calls: list[tuple[str, bytes, dict[str, str]]] = []

    def __call__(self, url: str, body: bytes, headers: dict[str, str]) -> int:
        self.calls.append((url, body, dict(headers)))
        return self.status


def provider(status: int, store: MemorySecretStore | None = None) -> tuple[RoutineBearerProvider, FakeHttp, MemorySecretStore]:
    secrets = store or MemorySecretStore()
    secrets.set("simulated-routine-key")
    http = FakeHttp(status)
    client = RoutineBearerProvider(
        "https://routine.example.invalid/hook",
        secrets,
        allow_network=True,
        transport=http,
    )
    return client, http, secrets


class RoutineBearerTest(unittest.TestCase):
    def setUp(self) -> None:
        os.environ[WEBHOOK_NETWORK_ENV] = "1"
        self.tmp = TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))

    def tearDown(self) -> None:
        os.environ.pop(WEBHOOK_NETWORK_ENV, None)
        self.store.close()
        self.tmp.cleanup()

    def test_bearer_header_and_200_is_only_accepted(self) -> None:
        client, http, _secrets = provider(200)
        payload = client.payload("event-sim-1", "conv-sim", [{"body": "【模拟】本地事件"}])
        status, error = client.transmit(payload)
        self.assertEqual(status, "accepted")
        self.assertIsNone(error)
        headers = http.calls[0][2]
        self.assertEqual(headers["Authorization"], "Bearer simulated-routine-key")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertNotIn("X-Inbox-Collector-Signature", headers)
        self.assertNotIn(b"forged", http.calls[0][1])
        self.assertTrue(json.loads(http.calls[0][1])["synthetic"])
        self.assertNotEqual(status, "draft_ready")

    def test_401_and_429_fail_without_retry(self) -> None:
        for code, reason in ((401, "unauthorized"), (429, "rate_limited_no_auto_retry")):
            with self.subTest(code=code):
                client, http, _secrets = provider(code)
                first, first_reason = client.transmit(client.payload("event-sim", "conv", []))
                second, second_reason = client.transmit(client.payload("event-sim", "conv", []))
                self.assertEqual(first, "failed")
                self.assertEqual(first_reason, reason)
                self.assertEqual(second, "failed")
                self.assertEqual(second_reason, reason)
                # The provider itself does not loop. A caller must not retry 429.
                if code == 429:
                    self.assertEqual(len(http.calls), 2)
                    self.assertIn("no_auto_retry", reason)

    def test_pipeline_does_not_retry_rate_limit_and_keeps_one_event(self) -> None:
        from inbox_collector.pipeline import Pipeline

        client, http, _secrets = provider(429)
        pipeline = Pipeline(self.store, client)
        pipeline.store.immediate  # noqa: B018 - keep store used
        event = client.payload("event-sim-rate", "conv-sim", [])
        with self.store.immediate():
            self.store.insert_outbox(
                {
                    "event_id": "event-sim-rate",
                    "batch_key": "batch-sim",
                    "status": "pending",
                    "attempts": 0,
                    "last_error": None,
                    "payload_json": json.dumps(event),
                    "created_at": "2026-11-08T00:00:00+00:00",
                    "updated_at": "2026-11-08T00:00:00+00:00",
                }
            )
        pipeline.transmit_pending()
        pipeline.transmit_pending()
        self.assertEqual(len(http.calls), 1)
        row = self.store.outbox_rows()[0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["last_error"], "rate_limited_no_auto_retry")
        self.assertEqual(row["event_id"], "event-sim-rate")

    def test_secret_is_redacted_and_not_written_by_setup(self) -> None:
        record = logging.LogRecord(
            "inbox_collector",
            logging.INFO,
            __file__,
            1,
            "Authorization: Bearer simulated-routine-key url=https://example.invalid/hook?key=simulated-routine-key",
            (),
            None,
        )
        self.assertTrue(RedactFilter(["simulated-routine-key"]).filter(record))
        self.assertNotIn("simulated-routine-key", record.getMessage())
        self.assertIn("Bearer [redacted]", record.getMessage())
        config_path = Path(self.tmp.name) / "config.routine.json"
        result = save_routine_setup(
            url="https://routine.example.invalid/hook",
            secret="simulated-routine-key",
            store=MemorySecretStore(),
            config_path=str(config_path),
        )
        self.assertTrue(result["ok"])
        rendered = config_path.read_text(encoding="utf-8")
        self.assertNotIn("simulated-routine-key", rendered)
        self.assertIn("routine_bearer", rendered)
        self.assertIn('"allow_network": false', rendered)
        self.assertFalse(result["production_running"])

    def test_import_requires_accepted_event_and_is_idempotent(self) -> None:
        with self.store.immediate():
            self.store.insert_outbox(
                {
                    "event_id": "event-sim-import",
                    "batch_key": "batch-import",
                    "status": "pending",
                    "attempts": 0,
                    "last_error": None,
                    "payload_json": "{}",
                    "created_at": "2026-11-08T00:00:00+00:00",
                    "updated_at": "2026-11-08T00:00:00+00:00",
                }
            )
            self.store.insert_draft(
                {
                    "batch_key": "batch-import",
                    "conversation_key": "conv-sim",
                    "status": "pending",
                    "provider": "routine_bearer",
                    "synthetic": 1,
                    "draft_text": None,
                    "reason": None,
                    "created_at": "2026-11-08T00:00:00+00:00",
                    "updated_at": "2026-11-08T00:00:00+00:00",
                },
                [],
            )
        early = import_routine_result(
            self.store,
            {"event_id": "event-sim-import", "status": "draft_ready", "draft_text": "【模拟】导入"},
        )
        self.assertEqual(early["error"], "import_requires_accepted")
        with self.store.immediate():
            self.store.update_outbox("event-sim-import", "accepted", 1, None)
        forged = import_routine_result(
            self.store,
            {
                "event_id": "event-sim-import",
                "status": "draft_ready",
                "draft_text": "【模拟】导入",
                "real_store_connected": True,
            },
        )
        self.assertEqual(forged["error"], "import_unexpected_field")
        imported = import_routine_result(
            self.store,
            {"event_id": "event-sim-import", "status": "draft_ready", "draft_text": "【模拟】导入"},
        )
        self.assertTrue(imported["ok"])
        self.assertTrue(imported["synthetic"])
        again = import_routine_result(
            self.store,
            {"event_id": "event-sim-import", "status": "draft_ready", "draft_text": "【模拟】导入"},
        )
        self.assertTrue(again["idempotent"])
        conflict = import_routine_result(
            self.store,
            {"event_id": "event-sim-import", "status": "draft_ready", "draft_text": "另一份"},
        )
        self.assertEqual(conflict["error"], "import_conflict")
        unknown = import_routine_result(
            self.store,
            {"event_id": "missing-event", "status": "draft_ready", "draft_text": "【模拟】导入"},
        )
        self.assertEqual(unknown["error"], "import_event_unknown")

    def test_setup_parser_has_no_key_argument(self) -> None:
        from inbox_collector.cli import build_parser

        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["routine-setup", "--url", "https://routine.example.invalid/hook", "--key", "nope"])
