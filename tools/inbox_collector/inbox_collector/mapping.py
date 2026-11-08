"""Site mapping. An unvalidated mapping cannot drive a real store."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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
    return mapping_problems(mapping) == []
