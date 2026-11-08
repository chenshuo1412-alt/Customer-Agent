"""Observation objects. Badges and previews are not message identity."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


class SnapshotRejected(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def parse_aware_time(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise SnapshotRejected("message_time_invalid")
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise SnapshotRejected("message_time_invalid") from exc
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise SnapshotRejected("message_time_missing_timezone")
    return parsed


@dataclass(frozen=True)
class ObservedMessage:
    conversation_key: str
    message_id: str
    direction: str
    message_time: str
    time_precision: str
    list_time_text: str | None
    body: str
    reply_status: str | None
    reply_status_source: str | None
    reply_status_reliability: str | None

    def identity_material(self) -> str:
        return f"{self.conversation_key}\n{self.message_id}\n{self.direction}"


@dataclass(frozen=True)
class Snapshot:
    page_state: str
    message_list_complete: bool
    messages: tuple[ObservedMessage, ...]
    observed_at: str
    source_kind: str


_DIRECTIONS = {"buyer", "merchant"}
_PRECISION = {"second", "minute", "hour", "day"}
_BUYER_STATUS = {"unreplied", "human_replied", "platform_bot_replied"}
_RELIABILITY = {"reliable", "unreliable"}
_LIVE_SOURCES = {"pinduoduo_live", "live"}


def _message(raw: object) -> ObservedMessage:
    if not isinstance(raw, dict):
        raise SnapshotRejected("message_not_object")
    conversation_key = str(raw.get("conversation_key") or "").strip()
    if not conversation_key or raw.get("identity_verified") is not True:
        raise SnapshotRejected("conversation_identity_unverified")
    message_id = str(raw.get("message_id") or "").strip()
    if not message_id:
        raise SnapshotRejected("stable_message_id_missing")
    direction = str(raw.get("direction") or "").strip()
    if direction not in _DIRECTIONS:
        raise SnapshotRejected("direction_ambiguous")
    precision = str(raw.get("time_precision") or "").strip()
    if precision not in _PRECISION:
        raise SnapshotRejected("time_precision_ambiguous")
    message_time = str(raw.get("message_time") or "").strip()
    parse_aware_time(message_time)
    body = raw.get("body")
    if not isinstance(body, str) or body == "":
        raise SnapshotRejected("message_body_missing")
    status = raw.get("reply_status")
    source = raw.get("reply_status_source")
    reliability = raw.get("reply_status_reliability")
    if direction == "buyer":
        if status not in _BUYER_STATUS:
            raise SnapshotRejected("reply_status_missing")
        if reliability not in _RELIABILITY:
            raise SnapshotRejected("reply_status_reliability_missing")
        if not isinstance(source, str) or not source.strip():
            raise SnapshotRejected("reply_status_source_missing")
        status_text: str | None = str(status)
        source_text: str | None = source.strip()
        reliability_text: str | None = str(reliability)
    else:
        status_text = None
        source_text = None
        reliability_text = None
    list_time = raw.get("list_time_text")
    return ObservedMessage(
        conversation_key=conversation_key,
        message_id=message_id,
        direction=direction,
        message_time=message_time,
        time_precision=precision,
        list_time_text=None if list_time is None else str(list_time),
        body=body,
        reply_status=status_text,
        reply_status_source=source_text,
        reply_status_reliability=reliability_text,
    )


def snapshot_from_dict(raw: object) -> Snapshot:
    if not isinstance(raw, dict):
        raise SnapshotRejected("snapshot_not_object")
    source_kind = str(raw.get("source_kind") or "").strip()
    if source_kind in _LIVE_SOURCES:
        raise SnapshotRejected("live_source_refused")
    if not source_kind:
        source_kind = "generic_fixture"
    page_state = str(raw.get("page_state") or "").strip()
    if not page_state:
        raise SnapshotRejected("page_state_missing")
    observed_at = str(raw.get("observed_at") or "").strip()
    parse_aware_time(observed_at)
    if page_state != "inbox":
        return Snapshot(
            page_state=page_state,
            message_list_complete=False,
            messages=tuple(),
            observed_at=observed_at,
            source_kind=source_kind,
        )
    if raw.get("message_list_complete") is not True:
        raise SnapshotRejected("message_list_incomplete")
    messages_raw = raw.get("messages")
    if not isinstance(messages_raw, list):
        raise SnapshotRejected("messages_not_list")
    messages = tuple(_message(item) for item in messages_raw)
    seen: set[str] = set()
    for message in messages:
        material = message.identity_material()
        if material in seen:
            raise SnapshotRejected("duplicate_identity_in_snapshot")
        seen.add(material)
    return Snapshot(
        page_state="inbox",
        message_list_complete=True,
        messages=messages,
        observed_at=observed_at,
        source_kind=source_kind,
    )


def ignored_badge_keys() -> tuple[str, ...]:
    """Fields that must never decide whether a message is new."""
    return ("unread_count", "todo_count", "badge", "preview", "red_dot")
