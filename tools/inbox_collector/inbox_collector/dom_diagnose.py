"""Read-only structure of the page this process already owns.

The export does not navigate, click, read storage, or invent selectors.
data-v-* and tabindex are recorded only as unstable names, never as candidates.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

_SAFE_CLASS = re.compile(r"^[A-Za-z][A-Za-z_-]{0,48}$")
_UNSTABLE_NAMES = frozenset({"tabindex"})
# Fixture-only names. Their absence on a merchant page is not a collection verdict.
_FIXTURE_NAMES = frozenset({"data-message-id", "data-direction", "data-message-time"})
# Class tokens seen on the live page. They are landmarks for diagnosis, not selectors.
_OBSERVED_SHELL_CLASSES = frozenset({"chat-list-box", "chatWindowHeader"})
_TIME_ATTRIBUTE_NAMES = frozenset({"msgtimeformate", "threeminutesthreshold"})
_TEXT_CARRIER_NAMES = frozenset({"title", "aria-label", "href", "data-clipboard-text"})

# Fixed script. It returns tags, attribute names, and safe class tokens only.
_EXPORT_JS = """
() => {
  const root = document.body;
  if (!root) return { error: "no_body" };
  const nodes = [];
  const walk = (el, depth, ancestorPath) => {
    if (!el || depth > 8 || nodes.length >= 300) return;
    const attributes = [];
    let conversationSelected = null;
    for (const attr of Array.from(el.attributes || [])) {
      attributes.push(String(attr.name));
      if (attr.name === "data-conversation-selected" && (attr.value === "true" || attr.value === "false")) {
        conversationSelected = attr.value === "true";
      }
    }
    const classStructure = [];
    const classValue = el.getAttribute("class");
    if (classValue) {
      for (const token of classValue.split(/\\s+/)) {
        if (/^[A-Za-z][A-Za-z_-]{0,48}$/.test(token)) classStructure.push(token);
      }
    }
    const path = ancestorPath.concat(classStructure.slice(0, 2)).slice(-4);
    nodes.push({
      tag: String(el.tagName || "").toLowerCase(),
      attributes,
      class_structure: classStructure,
      path,
      conversation_selected: conversationSelected,
      depth,
      child_element_count: el.children ? el.children.length : 0
    });
    const nextPath = path;
    for (const child of Array.from(el.children || [])) walk(child, depth + 1, nextPath);
  };
  walk(root, 0, []);
  return { nodes, truncated: nodes.length >= 300 };
}
"""


def export_rendered_dom(page: Any, *, timeout_seconds: float = 2.0) -> dict[str, Any]:
    """Export structure from an already-open page. It does not call goto or click."""
    if timeout_seconds <= 0:
        return _error("timeout_invalid")
    try:
        page.set_default_timeout(int(timeout_seconds * 1000))
        raw = page.evaluate(_EXPORT_JS)
    except Exception:
        return _error("evaluate_failed")
    if not isinstance(raw, dict):
        return _error("evaluate_invalid")
    if raw.get("error"):
        return _error(str(raw.get("error")))
    nodes = raw.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return _error("no_rendered_elements")
    return _classify(nodes, truncated=bool(raw.get("truncated")))


def service_diagnose_request(page: Any, data_dir: Path, *, timeout_seconds: float = 2.0) -> None:
    """If the owning process sees a request file, write one report and keep the page."""
    request = data_dir / "dom-diagnose.request"
    if not request.is_file():
        return
    try:
        request.unlink()
    except OSError:
        return
    result = export_rendered_dom(page, timeout_seconds=timeout_seconds)
    target = data_dir / "dom-diagnose.result.json"
    temporary = data_dir / ".dom-diagnose.result.tmp"
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)
    os.chmod(target, 0o600)


def wait_for_diagnose_result(data_dir: Path, *, timeout_seconds: float) -> dict[str, Any]:
    """Ask the already-running owner to export. This function does not launch a browser."""
    import time

    result_path = data_dir / "dom-diagnose.result.json"
    if result_path.exists():
        result_path.unlink()
    request = data_dir / "dom-diagnose.request"
    request.write_text("{}\n", encoding="utf-8")
    os.chmod(request, 0o600)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if result_path.is_file():
            try:
                payload = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return _error("result_unreadable")
            if isinstance(payload, dict):
                return payload
            return _error("result_invalid")
        time.sleep(0.05)
    return _error("listener_not_running")


def _classify(nodes: list[Any], *, truncated: bool = False) -> dict[str, Any]:
    cleaned: list[dict[str, Any]] = []
    ignored: set[str] = set()
    conversation_flag_seen = False
    conversation_selected = None
    fixture_names_present = False
    shell_classes: set[str] = set()
    attribute_names: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict):
            continue
        names = [str(name) for name in node.get("attributes") or [] if isinstance(name, str)]
        kept_names: list[str] = []
        for name in names:
            if name == "tabindex" or name.startswith("data-v-"):
                ignored.add(name if name == "tabindex" else "data-v-")
                continue
            kept_names.append(name)
            attribute_names.add(name)
            if name in _FIXTURE_NAMES:
                fixture_names_present = True
        selected = node.get("conversation_selected")
        if selected is True or selected is False:
            conversation_flag_seen = True
            conversation_selected = selected
        class_structure = [
            token
            for token in node.get("class_structure") or []
            if isinstance(token, str) and _SAFE_CLASS.fullmatch(token)
        ]
        path = [
            token
            for token in node.get("path") or []
            if isinstance(token, str) and _SAFE_CLASS.fullmatch(token)
        ]
        shell_classes.update(token for token in class_structure if token in _OBSERVED_SHELL_CLASSES)
        cleaned.append(
            {
                "tag": str(node.get("tag") or ""),
                "attributes": kept_names,
                "class_structure": class_structure,
                "path": path,
                "depth": int(node.get("depth") or 0),
                "child_element_count": int(node.get("child_element_count") or 0),
            }
        )
    if not cleaned:
        return _error("no_rendered_elements")
    repeated = _repeated_rows(cleaned)
    identity = _identity_report(attribute_names, fixture_names_present, shell_classes)
    merchant_hint = bool(shell_classes or attribute_names & (_TIME_ATTRIBUTE_NAMES | {"data-clipboard-text"}))
    if conversation_flag_seen and conversation_selected is False:
        status = "conversation_not_selected"
        reason = "conversation_flag_false"
    elif fixture_names_present:
        status = "structure_observed"
        reason = "fixture_semantic_names_present"
    elif merchant_hint:
        status = "identity_unproven"
        reason = "merchant_structure_present_identity_unproven"
    else:
        status = "fields_absent"
        reason = "no_known_message_structure"
    return {
        "status": status,
        "reason": reason,
        "nodes": cleaned,
        "repeated_structures": repeated,
        "identity": identity,
        "observed_shell_classes": sorted(shell_classes),
        "fixture_names_required": False,
        "ignored_unstable_attributes": sorted(ignored),
        "selector_candidates": [],
        "selectors_inferred": False,
        "production_mapping_validated": False,
        "collection_enabled": False,
        "truncated": truncated,
        "platform_unsupported": False,
        "text_included": False,
        "values_included": False,
        "real_store_connected": False,
    }


def _repeated_rows(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[tuple[str, tuple[str, ...], tuple[str, ...]], int] = {}
    for node in nodes:
        key = (
            str(node["tag"]),
            tuple(node["class_structure"]),
            tuple(node["path"][-3:]),
        )
        counts[key] = counts.get(key, 0) + 1
    repeated = []
    for (tag, classes, path), count in counts.items():
        if count < 2 or not classes:
            continue
        repeated.append(
            {
                "tag": tag,
                "class_structure": list(classes),
                "path": list(path),
                "count": count,
                "row_identity": "unproven",
            }
        )
    repeated.sort(key=lambda item: (-int(item["count"]), str(item["class_structure"])))
    return repeated[:20]


def _identity_report(
    attribute_names: set[str],
    fixture_names_present: bool,
    shell_classes: set[str],
) -> dict[str, str]:
    return {
        "stable_message_id": "unproven",
        "direction": "present_as_fixture_name" if "data-direction" in attribute_names else "unproven",
        "message_time": "unproven",
        "reply_status": "unproven",
        "fixture_attribute_names": "present" if fixture_names_present else "absent",
        "id_attribute_name": "present_value_unread" if "id" in attribute_names else "absent",
        "time_attribute_name": (
            "present_value_unread"
            if attribute_names & _TIME_ATTRIBUTE_NAMES
            else "absent"
        ),
        "text_carrier_names": (
            "present_value_unread"
            if attribute_names & _TEXT_CARRIER_NAMES
            else "absent"
        ),
        "merchant_shell": "present" if shell_classes else "absent",
    }


def _error(reason: str) -> dict[str, Any]:
    return {
        "status": "diagnostic_error",
        "reason": reason,
        "nodes": None,
        "ignored_unstable_attributes": [],
        "selector_candidates": [],
        "selectors_inferred": False,
        "platform_unsupported": False,
        "text_included": False,
        "values_included": False,
        "real_store_connected": False,
        "production_mapping_validated": False,
        "collection_enabled": False,
    }
