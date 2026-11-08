"""Read-only DOM collection. Selectors come only from the supplied mapping."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from inbox_collector.browser_plan import BrowserLaunchRefused
from inbox_collector.browser_session import DedicatedBrowser
from inbox_collector.mapping import local_entry_problems
from inbox_collector.sources import CollectFailure


def _now(timezone_name: str) -> str:
    return datetime.now(ZoneInfo(timezone_name)).isoformat()


def _state(page: Any, selectors: dict[str, str]) -> str:
    if page.query_selector(selectors["page_state_captcha"]) is not None:
        return "captcha"
    if page.query_selector(selectors["page_state_account_elsewhere"]) is not None:
        return "account_elsewhere"
    if page.query_selector(selectors["page_state_login"]) is not None:
        return "login"
    inbox = page.query_selector(selectors["page_state_inbox"]) is not None
    complete = page.query_selector(selectors["message_list_complete"]) is not None
    if inbox and complete:
        return "inbox"
    return ""


def _messages(page: Any, selectors: dict[str, str]) -> list[dict[str, Any]]:
    root = page.query_selector(selectors["page_root"])
    if root is None:
        raise CollectFailure("parse_ambiguous")
    messages: list[dict[str, Any]] = []
    for node in page.query_selector_all(selectors["message"]):
        def attr(key: str) -> str | None:
            value = node.get_attribute(selectors[key])
            return None if value is None else str(value)

        verified = attr("identity_verified") == "true"
        messages.append(
            {
                "conversation_key": attr("conversation_key") or "",
                "identity_verified": verified,
                "message_id": attr("message_id") or "",
                "direction": attr("direction") or "",
                "message_time": attr("message_time") or "",
                "time_precision": attr("time_precision") or "",
                "list_time_text": attr("list_time"),
                "body": node.inner_text().strip(),
                "reply_status": attr("reply_status"),
                "reply_status_source": attr("reply_status_source"),
                "reply_status_reliability": attr("reply_status_reliability"),
            }
        )
    return messages


class BrowserDomSource:
    """Reload one local page and return a snapshot. It does not click or type."""

    def __init__(
        self,
        browser: DedicatedBrowser,
        entry_url: str,
        mapping: dict[str, Any],
        *,
        timezone_name: str,
        login_wait_seconds: int,
        on_launched: Callable[[DedicatedBrowser], None] | None = None,
    ) -> None:
        self.browser = browser
        self.entry_url = entry_url
        self.mapping = mapping
        self.timezone_name = timezone_name
        self.login_wait_seconds = login_wait_seconds
        self.on_launched = on_launched

    def collect(self) -> dict[str, Any]:
        blocked = local_entry_problems(self.entry_url)
        if blocked:
            raise CollectFailure(blocked[0])
        try:
            self.browser.start()
        except BrowserLaunchRefused as exc:
            raise CollectFailure(exc.code) from None
        except Exception:
            raise CollectFailure("browser_launch_failed") from None
        if self.on_launched is not None:
            self.on_launched(self.browser)
        page = self.browser.page
        try:
            page.goto(self.entry_url, wait_until="domcontentloaded", timeout=15000)
        except Exception:
            raise CollectFailure("network") from None
        if local_entry_problems(page.url):
            raise CollectFailure("merchant_origin_refused")
        selectors = self.mapping["selectors"]
        state = self._wait_for_page(page, selectors)
        observed_at = _now(self.timezone_name)
        if state in {"login", "captcha", "account_elsewhere"}:
            return {
                "source_kind": "browser_local",
                "page_state": state,
                "message_list_complete": False,
                "observed_at": observed_at,
                "messages": [],
            }
        if state != "inbox":
            raise CollectFailure("parse_ambiguous")
        return {
            "source_kind": "browser_local",
            "page_state": "inbox",
            "message_list_complete": True,
            "observed_at": observed_at,
            "messages": _messages(page, selectors),
        }

    def close(self) -> None:
        self.browser.close()

    def _wait_for_page(self, page: Any, selectors: dict[str, str]) -> str:
        deadline = time.monotonic() + self.login_wait_seconds
        while True:
            state = _state(page, selectors)
            if state == "inbox":
                return state
            if state in {"captcha", "account_elsewhere"}:
                return state
            if state != "login":
                return ""
            if time.monotonic() >= deadline:
                return "login"
            time.sleep(0.2)
