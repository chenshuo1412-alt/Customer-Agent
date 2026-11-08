"""Grok Bot routine delivery. HTTP 200 means accepted, not a draft."""

from __future__ import annotations

import json
import os
from typing import Any, Callable

from inbox_collector.keychain import ACCOUNT_NAME, SERVICE_NAME, SecretStore
from inbox_collector.providers import WEBHOOK_NETWORK_ENV, urllib_transport
from inbox_collector.safety import DISCLAIMER, assert_draft_status
from inbox_collector.store import Store, utc_now

Transport = Callable[[str, bytes, dict[str, str]], int]

ROUTINE_LIMIT_NOTE = (
    "每个 routine 每小时最多 200 次，账号每小时最多 300 次。"
    "限流失败不会自动重试，也没有公开的草稿查询接口。"
)


class RoutineBearerProvider:
    """POST with Authorization: Bearer. HMAC is a different mode and is not added."""

    name = "routine_bearer"

    def __init__(
        self,
        url: str,
        secret_store: SecretStore,
        *,
        allow_network: bool,
        transport: Transport | None = None,
    ) -> None:
        self.url = url
        self.secret_store = secret_store
        self.allow_network = allow_network
        self.transport = transport if transport is not None else urllib_transport

    def network_allowed(self) -> bool:
        if not self.allow_network or not self.url.startswith("https://"):
            return False
        if os.environ.get(WEBHOOK_NETWORK_ENV) != "1":
            return False
        return bool(self.secret_store.get())

    def payload(self, event_id: str, conversation_key: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "event_id": event_id,
            "event_type": "inbound_review_requested",
            "conversation_key": conversation_key,
            "messages": messages,
            "synthetic": True,
            "label": "模拟",
            "real_store_connected": False,
            "result_channel": "bot_chat_only",
            "note": "HTTP 200 only means the routine was accepted. No buyer send is requested.",
        }

    def transmit(self, payload: dict[str, Any]) -> tuple[str, str | None]:
        if not self.network_allowed():
            reason = "routine_key_missing" if not self.secret_store.get() else "network_disabled"
            return "pending", reason
        secret = self.secret_store.get() or ""
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + secret,
        }
        try:
            status = self.transport(self.url, body, headers)
        except Exception:
            return "failed", "transport_error"
        if status == 200:
            return "accepted", None
        if status == 401:
            return "failed", "unauthorized"
        if status == 429:
            return "failed", "rate_limited_no_auto_retry"
        return "failed", f"http_{status}"


def import_routine_result(store: Store, document: dict[str, Any]) -> dict[str, Any]:
    """Record a draft a person copied from Bot chat. This does not call the network."""
    allowed = {"event_id", "status", "draft_text"}
    if set(document) != allowed:
        return {"ok": False, "error": "import_unexpected_field"}
    if document.get("status") != "draft_ready":
        return {"ok": False, "error": "import_status_refused"}
    event_id = document.get("event_id")
    draft_text = document.get("draft_text")
    if not isinstance(event_id, str) or not event_id:
        return {"ok": False, "error": "import_event_invalid"}
    if not isinstance(draft_text, str) or not draft_text.strip():
        return {"ok": False, "error": "import_draft_missing"}
    row = store.conn.execute("SELECT * FROM outbox WHERE event_id=?", (event_id,)).fetchone()
    if row is None:
        return {"ok": False, "error": "import_event_unknown"}
    if row["status"] == "draft_ready":
        current = store.conn.execute(
            "SELECT draft_text FROM drafts WHERE batch_key=?",
            (row["batch_key"],),
        ).fetchone()
        if current is not None and current["draft_text"] == draft_text:
            return {"ok": True, "idempotent": True, "event_id": event_id}
        return {"ok": False, "error": "import_conflict"}
    if row["status"] != "accepted":
        return {"ok": False, "error": "import_requires_accepted"}
    status = assert_draft_status("draft_ready")
    with store.immediate():
        store.update_outbox(event_id, status, int(row["attempts"]), None)
        store.conn.execute(
            "UPDATE drafts SET status=?, draft_text=?, synthetic=1, reason=?, updated_at=? "
            "WHERE batch_key=?",
            (
                status,
                draft_text,
                "manual_import_from_bot_chat",
                utc_now(),
                row["batch_key"],
            ),
        )
    return {
        "ok": True,
        "idempotent": False,
        "event_id": event_id,
        "status": status,
        "synthetic": True,
        "draft_source": "manual_import",
        "disclaimer": DISCLAIMER,
        "result_channel": "bot_chat_only",
    }


def save_routine_setup(
    *,
    url: str,
    secret: str,
    store: SecretStore,
    config_path: str,
    config_text: str | None = None,
) -> dict[str, Any]:
    """Save the URL in config and the key in the secret store. The key is not written to disk."""
    from inbox_collector.config import urlsplit_has_secret

    if not url.startswith("https://") or urlsplit_has_secret(url) or secret and secret in url:
        return {"ok": False, "error": "routine_url_refused"}
    if not secret or secret.strip() != secret or "\n" in secret:
        return {"ok": False, "error": "routine_key_refused"}
    store.set(secret)
    import json
    from pathlib import Path

    path = Path(config_path)
    if config_text is None and path.is_file():
        config_text = path.read_text(encoding="utf-8")
    payload: dict[str, Any]
    if config_text:
        payload = json.loads(config_text)
        if not isinstance(payload, dict):
            return {"ok": False, "error": "config_not_object"}
        if payload.get("mode") == "login_prep":
            return {"ok": False, "error": "login_prep_webhook_forbidden"}
    else:
        payload = {
            "mode": "fixture",
            "timezone": "Asia/Shanghai",
            "sample_interval_seconds": 120,
            "max_retries": 2,
            "max_consecutive_failures": 3,
            "open_conversation": False,
            "allow_browser_launch": False,
            "mapping_path": "mappings/pinduoduo_chat_merchant.json",
            "fixture_path": "fixtures/sample_snapshot.json",
            "data_dir": "data",
            "retention_days": 30,
            "alert_sink": {"enabled": False},
            "model": {"enabled": False},
        }
    payload["draft_provider"] = "routine_bearer"
    payload["webhook"] = {
        "enabled": True,
        "auth_mode": "routine_bearer",
        "url": url,
        "key_source": "keychain",
        "keychain_service": SERVICE_NAME,
        "keychain_account": ACCOUNT_NAME,
        "allow_network": False,
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if secret in rendered:
        return {"ok": False, "error": "secret_leaked_into_config"}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(rendered, encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    return {
        "ok": True,
        "key_stored": True,
        "key_source": "keychain",
        "allow_network": False,
        "production_running": False,
        "note": ROUTINE_LIMIT_NOTE,
    }
