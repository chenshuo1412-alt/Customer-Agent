"""Local fixture input only. This module does not open a shop or a browser."""

from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

_BLOCKED_PATH_PARTS = (
    "google/chrome",
    "google\\chrome",
    "chromium",
    "user-data-dir",
    "cookies",
    "user_data",
)


class CollectFailure(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _GenericHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.section: dict[str, str] | None = None
        self.articles: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key: value or "" for key, value in attrs}
        if tag == "section" and attr.get("data-fixture") == "generic-inbox":
            self.section = attr
        elif tag == "article" and self.section is not None:
            self._current = attr
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "article" and self._current is not None:
            self._current["body"] = "".join(self._text).strip()
            self.articles.append(self._current)
            self._current = None


def parse_generic_fixture_html(text: str) -> dict[str, Any]:
    """Parse the local synthetic inbox. Real merchant HTML will not match."""
    parser = _GenericHTMLParser()
    parser.feed(text)
    if parser.section is None:
        raise CollectFailure("parse_ambiguous")
    section = parser.section
    messages = []
    for article in parser.articles:
        messages.append(
            {
                "conversation_key": article.get("data-conversation-key", ""),
                "identity_verified": article.get("data-identity-verified") == "true",
                "message_id": article.get("data-message-id", ""),
                "direction": article.get("data-direction", ""),
                "message_time": article.get("data-message-time", ""),
                "time_precision": article.get("data-time-precision", ""),
                "list_time_text": article.get("data-list-time") or None,
                "body": article.get("body", ""),
                "reply_status": article.get("data-reply-status"),
                "reply_status_source": article.get("data-reply-source"),
                "reply_status_reliability": article.get("data-reply-reliability"),
            }
        )
    return {
        "source_kind": "generic_fixture",
        "page_state": section.get("data-page-state") or "inbox",
        "message_list_complete": section.get("data-message-list-complete") == "true",
        "observed_at": section.get("data-observed-at") or "",
        "messages": messages,
    }


def assert_local_fixture_path(path: Path) -> None:
    text = str(path)
    if "://" in text:
        raise CollectFailure("remote_fixture_refused")
    lowered = text.lower()
    if any(part in lowered for part in _BLOCKED_PATH_PARTS):
        raise CollectFailure("profile_or_cookie_path_refused")


class FixtureSource:
    def __init__(self, path: Path) -> None:
        assert_local_fixture_path(path)
        self.path = path

    def collect(self) -> dict[str, Any]:
        if not self.path.is_file():
            raise CollectFailure("fixture_missing")
        text = self.path.read_text(encoding="utf-8")
        if self.path.suffix.lower() == ".html":
            return parse_generic_fixture_html(text)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CollectFailure("parse_ambiguous") from exc
        if not isinstance(data, dict):
            raise CollectFailure("parse_ambiguous")
        data.setdefault("source_kind", "generic_fixture")
        return data


class SequenceSource:
    """Test double that replays in-memory snapshots and then repeats the last one."""

    def __init__(self, snapshots: list[dict[str, Any]]) -> None:
        if not snapshots:
            raise ValueError("empty_sequence")
        self.snapshots = snapshots
        self.index = 0
        self.calls = 0

    def collect(self) -> dict[str, Any]:
        self.calls += 1
        position = min(self.index, len(self.snapshots) - 1)
        if self.index < len(self.snapshots):
            self.index += 1
        return self.snapshots[position]
