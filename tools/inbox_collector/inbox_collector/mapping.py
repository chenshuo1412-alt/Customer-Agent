"""Site mapping. An unvalidated mapping cannot drive a real store."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

REQUIRED_SELECTOR_KEYS = (
    "conversation_key",
    "message_id",
    "direction",
    "message_time",
    "time_precision",
    "reply_status",
    "reply_status_source",
    "reply_status_reliability",
    "page_state_inbox",
    "page_state_login",
    "page_state_captcha",
    "page_state_account_elsewhere",
    "message_list_complete",
)


def load_mapping(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("mapping_not_object")
    data["_path"] = str(path)
    return data


def mapping_problems(mapping: dict[str, Any] | None, *, open_conversation: bool = False) -> list[str]:
    if not isinstance(mapping, dict):
        return ["mapping_missing"]
    problems: list[str] = []
    if mapping.get("validated") is not True:
        problems.append("mapping_not_validated")
    if mapping.get("validation_status") != "verified_against_live_dom":
        problems.append("live_dom_not_verified")
    selectors = mapping.get("selectors")
    if not isinstance(selectors, dict):
        problems.append("selectors_invalid")
        selectors = {}
    for key in REQUIRED_SELECTOR_KEYS:
        if not str(selectors.get(key) or "").strip():
            problems.append(f"selector_missing:{key}")
    if selectors.get("parser") == "generic_fixture":
        problems.append("fixture_parser_cannot_map_real_site")
    if mapping.get("list_fields_sufficient") is not True:
        problems.append("list_fields_insufficient")
        if not open_conversation:
            problems.append("open_conversation_required_for_incomplete_list")
        else:
            # Opening a thread can mark it read. That still needs verified selectors.
            problems.append("open_conversation_selectors_unverified")
    return problems


def mapping_validated(mapping: dict[str, Any] | None) -> bool:
    """True only for a merchant mapping verified against a live DOM.

    This build has no such mapping. Local fixture mappings use a separate check.
    """
    return mapping_problems(mapping) == []


_LOCAL_ORIGINS = frozenset({"file", "http://127.0.0.1", "http://localhost"})
_MERCHANT_SUFFIXES = ("pinduoduo.com", "yangkeduo.com")
_LOCAL_EXTRA_SELECTORS = ("page_root", "message", "identity_verified", "list_time")


def local_fixture_problems(mapping: dict[str, Any] | None) -> list[str]:
    """Problems that block the local headed-browser fixture, not a live shop."""
    if not isinstance(mapping, dict):
        return ["mapping_missing"]
    problems: list[str] = []
    if mapping.get("scope") != "local_fixture":
        problems.append("not_a_local_fixture_mapping")
    if mapping.get("validated") is not True:
        problems.append("mapping_not_validated")
    if mapping.get("validation_status") != "verified_local_fixture_only":
        problems.append("local_fixture_not_verified")
    if mapping.get("list_fields_sufficient") is not True:
        problems.append("list_fields_insufficient")
    origins = mapping.get("allowed_origins")
    if not isinstance(origins, list) or not origins:
        problems.append("allowed_origins_missing")
    else:
        for origin in origins:
            if origin not in _LOCAL_ORIGINS:
                problems.append("local_origin_not_allowed")
                break
    selectors = mapping.get("selectors")
    if not isinstance(selectors, dict):
        problems.append("selectors_invalid")
        selectors = {}
    for key in REQUIRED_SELECTOR_KEYS + _LOCAL_EXTRA_SELECTORS:
        if not str(selectors.get(key) or "").strip():
            problems.append(f"selector_missing:{key}")
    if selectors.get("parser") == "generic_fixture":
        problems.append("fixture_parser_cannot_map_real_site")
    return problems


def local_entry_problems(url: str) -> list[str]:
    """Reject merchant and remote URLs before any browser is started."""
    if not url:
        return ["entry_url_missing"]
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().rstrip(".")
    if any(host == suffix or host.endswith("." + suffix) for suffix in _MERCHANT_SUFFIXES):
        return ["merchant_origin_refused"]
    if parts.scheme == "file":
        return []
    if parts.scheme == "http" and host in {"127.0.0.1", "localhost"}:
        return []
    return ["entry_url_not_local"]


def entry_allowed_by_mapping(url: str, mapping: dict[str, Any] | None) -> list[str]:
    problems = local_entry_problems(url)
    if problems or not isinstance(mapping, dict):
        return problems
    parts = urlsplit(url.strip())
    if parts.scheme == "file":
        origin = "file"
    else:
        origin = f"http://{(parts.hostname or '').lower()}"
    allowed = mapping.get("allowed_origins")
    if not isinstance(allowed, list) or origin not in allowed:
        return ["entry_origin_not_allowed"]
    return []
