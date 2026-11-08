"""Built-in synthetic inbox used by the offline demo. Not a customer export."""

from __future__ import annotations

OBSERVED_AT = "2026-11-08T13:33:00+08:00"

OLD_MESSAGE = {
    "conversation_key": "conv-sim-1",
    "identity_verified": True,
    "message_id": "msg-sim-old",
    "direction": "buyer",
    "message_time": "2026-11-08T10:00:00+08:00",
    "time_precision": "second",
    "list_time_text": "10:00",
    "body": "【模拟】更早的历史消息，建立基线，不生成草稿。",
    "reply_status": "unreplied",
    "reply_status_source": "fixture_field",
    "reply_status_reliability": "reliable",
}

NEW_MESSAGE = {
    "conversation_key": "conv-sim-1",
    "identity_verified": True,
    "message_id": "msg-sim-new",
    "direction": "buyer",
    "message_time": "2026-11-08T13:32:44+08:00",
    "time_precision": "second",
    "list_time_text": "13:32",
    "body": "【模拟】买家询问发货时间，非真实客户。",
    "reply_status": "unreplied",
    "reply_status_source": "fixture_field",
    "reply_status_reliability": "reliable",
    "unread_count": 0,
    "todo_count": 0,
    "preview": "【模拟】买家询问",
}


def snapshot(messages: list[dict], *, observed_at: str = OBSERVED_AT, page_state: str = "inbox") -> dict:
    return {
        "source_kind": "generic_fixture",
        "page_state": page_state,
        "message_list_complete": page_state == "inbox",
        "observed_at": observed_at,
        "todo_count": 0,
        "unread_count": 0,
        "messages": messages,
    }


BASELINE_SNAPSHOT = snapshot([OLD_MESSAGE])
NEW_SNAPSHOT = snapshot([OLD_MESSAGE, NEW_MESSAGE], observed_at="2026-11-08T13:33:05+08:00")
