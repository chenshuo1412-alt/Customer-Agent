"""Baseline, dedupe, and draft records. Message text is data, never a command."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from inbox_collector.models import ObservedMessage, Snapshot, parse_aware_time
from inbox_collector.providers import WebhookProvider
from inbox_collector.safety import DISCLAIMER, assert_draft_status
from inbox_collector.store import Store, utc_now

_PAGE_ALERTS = {
    "login": "login_page",
    "captcha": "captcha",
    "account_elsewhere": "account_elsewhere",
    "stale": "page_stale",
    "network": "network",
}


def identity_hash(message: ObservedMessage) -> str:
    return hashlib.sha256(message.identity_material().encode("utf-8")).hexdigest()


def batch_key_for(hashes: list[str]) -> str:
    return hashlib.sha256("\n".join(hashes).encode("utf-8")).hexdigest()


def _as_utc(value: str) -> str:
    return parse_aware_time(value).astimezone(timezone.utc).isoformat()


def _latest_instant(value: str, precision: str) -> datetime:
    parsed = parse_aware_time(value)
    if precision == "second":
        return parsed
    if precision == "minute":
        return parsed.replace(second=59, microsecond=999999)
    if precision == "hour":
        return parsed.replace(minute=59, second=59, microsecond=999999)
    return parsed.replace(hour=23, minute=59, second=59, microsecond=999999)


class Pipeline:
    def __init__(self, store: Store, provider: object, *, retention_days: int = 30) -> None:
        self.store = store
        self.provider = provider
        self.retention_days = retention_days

    def observe(self, snapshot: Snapshot, *, failure_ceiling: int) -> str:
        with self.store.immediate():
            self.store.set_meta("last_source_kind", snapshot.source_kind)
            if snapshot.page_state != "inbox":
                code = _PAGE_ALERTS.get(snapshot.page_state, "page_unusable")
                return self._pause_locked(code, failure_ceiling)
            self.store.redact_expired(self.retention_days)
            seen_at = _as_utc(snapshot.observed_at)
            if not self.store.baseline_done():
                for message in snapshot.messages:
                    self.store.insert_message(self._row(message, snapshot, seen_at, baseline=1))
                self.store.mark_baseline()
                self.store.touch_success()
                return "baseline"
            created_inbound = False
            for message in snapshot.messages:
                inserted = self.store.insert_message(self._row(message, snapshot, seen_at, baseline=0))
                if not inserted or message.direction != "buyer":
                    continue
                status = self._inbound_status(message)
                if self.store.insert_inbound(identity_hash(message), message.conversation_key, status, seen_at):
                    created_inbound = True
            if created_inbound:
                self.store.note_inbound()
            self.store.touch_success()
            return "collected"

    def generate(self) -> int:
        created = 0
        with self.store.immediate():
            rows = self.store.undrafted_inbounds()
            individuals = [row for row in rows if row["status"] != "eligible"]
            grouped: dict[str, list] = {}
            for row in rows:
                if row["status"] == "eligible":
                    grouped.setdefault(str(row["conversation_key"]), []).append(row)
            for row in individuals:
                reason = self._review_reason(row)
                status = str(row["status"])
                if status == "skipped_already_replied":
                    draft_status = "skipped_already_replied"
                else:
                    draft_status = "needs_human_review"
                self._write_draft(
                    conversation_key=str(row["conversation_key"]),
                    hashes=[str(row["identity_hash"])],
                    status=draft_status,
                    provider="none",
                    synthetic=0,
                    draft_text=None,
                    reason=reason,
                    event_payload=None,
                )
                created += 1
            for conversation_key, group in grouped.items():
                hashes = [str(row["identity_hash"]) for row in group]
                earliest = min(parse_aware_time(str(row["message_time"])) for row in group)
                blocked = self._merchant_block(conversation_key, earliest)
                messages = [self._public_message(row) for row in group]
                if blocked:
                    self._write_draft(
                        conversation_key=conversation_key,
                        hashes=hashes,
                        status="invalidated",
                        provider="none",
                        synthetic=0,
                        draft_text=None,
                        reason=blocked,
                        event_payload=None,
                    )
                    created += 1
                    continue
                if isinstance(self.provider, WebhookProvider):
                    event_id = str(uuid.uuid4())
                    payload = self.provider.payload(event_id, conversation_key, messages)
                    reason = None if self.provider.network_allowed() else "network_disabled"
                    self._write_draft(
                        conversation_key=conversation_key,
                        hashes=hashes,
                        status="webhook_held",
                        provider="webhook",
                        synthetic=0,
                        draft_text=None,
                        reason=reason,
                        event_payload=(event_id, payload, reason),
                    )
                    created += 1
                    continue
                built = self.provider.build(conversation_key, messages)
                self._write_draft(
                    conversation_key=conversation_key,
                    hashes=hashes,
                    status=assert_draft_status(str(built["status"])),
                    provider=str(self.provider.name),
                    synthetic=1 if built.get("synthetic") else 0,
                    draft_text=built.get("draft_text"),
                    reason=built.get("reason"),
                    event_payload=None,
                )
                created += 1
        self.transmit_pending()
        self.export_spool()
        return created

    def transmit_pending(self) -> None:
        if not isinstance(self.provider, WebhookProvider) or not self.provider.network_allowed():
            return
        pending = [
            row
            for row in self.store.outbox_rows()
            if row["status"] in {"webhook_held", "webhook_transport_failed"}
        ]
        for row in pending:
            payload = json.loads(str(row["payload_json"]))
            status, error = self.provider.transmit(payload)
            status = assert_draft_status(status)
            attempts = int(row["attempts"]) + 1
            with self.store.immediate():
                self.store.update_outbox(str(row["event_id"]), status, attempts, error)
                self.store.update_draft_status(str(row["batch_key"]), status, error)
        self.export_spool()

    def export_spool(self) -> None:
        spool = self.store.data_dir / "spool"
        spool.mkdir(parents=True, exist_ok=True)
        os.chmod(spool, 0o700)
        for draft in self.store.drafts():
            messages = [self._public_message(row) for row in self.store.draft_messages(int(draft["id"]))]
            outbox = self.store.get_outbox_by_batch(str(draft["batch_key"]))
            synthetic = bool(draft["synthetic"])
            payload = {
                "label": "模拟草稿" if synthetic else "本地复核记录",
                "synthetic": synthetic,
                "real_store_connected": False,
                "sent_to_buyer": False,
                "prompt_instructions_executed": False,
                "disclaimer": DISCLAIMER,
                "status": draft["status"],
                "provider": draft["provider"],
                "conversation_key": draft["conversation_key"],
                "batch_key": draft["batch_key"],
                "reason": draft["reason"],
                "draft_text": draft["draft_text"],
                "event_id": None if outbox is None else outbox["event_id"],
                "webhook_is_draft_receipt": False,
                "untrusted_messages": messages,
            }
            self._write_json(spool / f"{draft['batch_key']}.json", payload)

    def _pause_locked(self, code: str, failure_ceiling: int) -> str:
        self.store.open_alert(code)
        streak = self.store.increment_failure()
        if streak >= failure_ceiling:
            self.store.set_state("stopped", "failure_ceiling")
            return "stopped"
        self.store.set_state("paused", code)
        return "paused"

    def _merchant_block(self, conversation_key: str, earliest_buyer: datetime) -> str | None:
        for row in self.store.merchant_messages(conversation_key):
            latest = _latest_instant(str(row["message_time"]), str(row["time_precision"]))
            if latest >= earliest_buyer:
                if row["time_precision"] != "second":
                    return "merchant_time_ambiguous"
                return "merchant_reply_before_draft_released"
        return None

    @staticmethod
    def _inbound_status(message: ObservedMessage) -> str:
        if message.time_precision != "second" or message.reply_status_reliability != "reliable":
            return "needs_human_review"
        if message.reply_status in {"human_replied", "platform_bot_replied"}:
            return "skipped_already_replied"
        return "eligible"

    @staticmethod
    def _review_reason(row: object) -> str:
        if row["time_precision"] != "second":
            return "message_time_not_second_precision"
        if row["reply_status_reliability"] != "reliable":
            return "reply_status_unreliable"
        if row["status"] == "skipped_already_replied":
            return f"already_replied:{row['reply_status']}"
        return "not_actionable"

    @staticmethod
    def _public_message(row: object) -> dict[str, object]:
        return {
            "untrusted": True,
            "message_id": row["message_id"],
            "direction": row["direction"],
            "message_time": row["message_time"],
            "time_precision": row["time_precision"],
            "list_time_text": row["list_time_text"],
            "body": row["body"],
            "reply_status": row["reply_status"],
            "reply_status_reliability": row["reply_status_reliability"],
        }

    def _row(self, message: ObservedMessage, snapshot: Snapshot, seen_at: str, *, baseline: int) -> dict[str, object]:
        return {
            "identity_hash": identity_hash(message),
            "conversation_key": message.conversation_key,
            "message_id": message.message_id,
            "direction": message.direction,
            "message_time": message.message_time,
            "time_precision": message.time_precision,
            "list_time_text": message.list_time_text,
            "body": message.body,
            "reply_status": message.reply_status,
            "reply_status_source": message.reply_status_source,
            "reply_status_reliability": message.reply_status_reliability,
            "baseline": baseline,
            "source_kind": snapshot.source_kind,
            "first_seen_at": seen_at,
        }

    def _write_draft(
        self,
        *,
        conversation_key: str,
        hashes: list[str],
        status: str,
        provider: str,
        synthetic: int,
        draft_text: str | None,
        reason: str | None,
        event_payload: tuple[str, dict[str, object], str | None] | None,
    ) -> None:
        status = assert_draft_status(status)
        now = utc_now()
        key = batch_key_for(hashes)
        self.store.insert_draft(
            {
                "batch_key": key,
                "conversation_key": conversation_key,
                "status": status,
                "provider": provider,
                "synthetic": synthetic,
                "draft_text": draft_text,
                "reason": reason,
                "created_at": now,
                "updated_at": now,
            },
            hashes,
        )
        if event_payload is not None:
            event_id, payload, error = event_payload
            self.store.insert_outbox(
                {
                    "event_id": event_id,
                    "batch_key": key,
                    "status": status,
                    "attempts": 0,
                    "last_error": error,
                    "payload_json": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    "created_at": now,
                    "updated_at": now,
                }
            )

    @staticmethod
    def _write_json(path: Path, payload: dict[str, object]) -> None:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
