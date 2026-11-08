"""Draft providers. The default path has no network and invents no customer answer."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable

from inbox_collector.safety import (
    CANNED_DRAFT_TEXT,
    DISCLAIMER,
    MODEL_UNVERIFIED_REASON,
    PENDING_REASON,
    assert_draft_status,
)

Transport = Callable[[str, bytes, dict[str, str]], int]
WEBHOOK_NETWORK_ENV = "INBOX_COLLECTOR_WEBHOOK_NETWORK"


class OutboundDisabled(RuntimeError):
    pass


def sign_body(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def urllib_transport(url: str, body: bytes, headers: dict[str, str]) -> int:
    """Real HTTPS POST. Refuses to run unless a process env gate is set.

    The response body is discarded. An HTTP 200 is only transport acceptance.
    """
    if os.environ.get(WEBHOOK_NETWORK_ENV) != "1":
        raise OutboundDisabled("webhook_network_env_disabled")
    if not url.startswith("https://"):
        raise OutboundDisabled("webhook_url_not_https")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


class MockProvider:
    name = "mock"

    def build(self, conversation_key: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        del conversation_key, messages
        return {
            "status": assert_draft_status("ready_for_review"),
            "draft_text": CANNED_DRAFT_TEXT,
            "reason": None,
            "synthetic": True,
            "disclaimer": DISCLAIMER,
        }


class NoneProvider:
    name = "none"

    def build(self, conversation_key: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        del conversation_key, messages
        return {
            "status": assert_draft_status("pending_generation"),
            "draft_text": None,
            "reason": PENDING_REASON,
            "synthetic": False,
            "disclaimer": DISCLAIMER,
        }


class UnverifiedModelProvider:
    """Configured model channels stay pending. This build does not call them."""

    name = "model"

    def build(self, conversation_key: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        del conversation_key, messages
        return {
            "status": assert_draft_status("pending_generation"),
            "draft_text": None,
            "reason": MODEL_UNVERIFIED_REASON,
            "synthetic": False,
            "disclaimer": DISCLAIMER,
        }


class WebhookProvider:
    """Queue a signed inbound event. Transport success is not a returned draft."""

    name = "webhook"

    def __init__(
        self,
        url: str,
        secret_env: str,
        *,
        allow_network: bool,
        transport: Transport | None = None,
    ) -> None:
        self.url = url
        self.secret_env = secret_env
        self.allow_network = allow_network
        self.transport = transport if transport is not None else urllib_transport

    def network_allowed(self) -> bool:
        if not self.allow_network or not self.url.startswith("https://"):
            return False
        if os.environ.get(WEBHOOK_NETWORK_ENV) != "1":
            return False
        return bool(os.environ.get(self.secret_env))

    def payload(self, event_id: str, conversation_key: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "event_id": event_id,
            "event_type": "inbound_review_requested",
            "conversation_key": conversation_key,
            "messages": messages,
            "note": "Transport acceptance is not a draft receipt. No buyer send is requested.",
            "real_store_connected": False,
        }

    def transmit(self, payload: dict[str, Any]) -> tuple[str, str | None]:
        if not self.network_allowed():
            return "webhook_held", "network_disabled"
        secret = os.environ.get(self.secret_env) or ""
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-Inbox-Collector-Signature": sign_body(secret, body),
        }
        try:
            status = self.transport(self.url, body, headers)
        except Exception:
            return "webhook_transport_failed", "transport_error"
        if status == 200:
            return "webhook_transport_accepted_awaiting_receipt", None
        return "webhook_transport_failed", f"http_{status}"


def provider_for(name: str, *, webhook: WebhookProvider | None = None) -> Any:
    if name == "mock":
        return MockProvider()
    if name == "none":
        return NoneProvider()
    if name == "model":
        return UnverifiedModelProvider()
    if name == "webhook":
        if webhook is None:
            raise OutboundDisabled("webhook_not_configured")
        return webhook
    raise OutboundDisabled("unknown_provider")
