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

# Values are classified inside the page. Only counts and safe class tokens leave it.
_EXPORT_JS = """
() => {
  const root = document.body;
  if (!root) return { error: "no_body" };
  const nodes = [];
  const safeClass = (token) => /^[A-Za-z][A-Za-z_-]{0,48}$/.test(token);
  const classesOf = (el) => {
    const out = [];
    for (const token of String(el.getAttribute("class") || "").split(/\\s+/)) {
      if (token && safeClass(token)) out.push(token);
    }
    return out;
  };
  const isHidden = (el) => {
    if (!el || el === root) return false;
    if (el.hidden || el.getAttribute("aria-hidden") === "true") return true;
    const style = window.getComputedStyle(el);
    return style.display === "none" || style.visibility === "hidden";
  };
  const idShape = (value) => {
    if (!value) return "empty";
    if (/^(app|root|header|footer|main|content|app-root)$/i.test(value)) return "generic_shell";
    if (value.length > 64) return "long";
    if (/^\\d{6,}$/.test(value)) return "long_numeric";
    return "other_shape";
  };
  const timeKind = (value) => {
    const text = String(value || "").trim();
    if (!text) return "empty";
    const template = /yyyy|YYYY|MM|dd|DD|HH|mm|ss/.test(text) && !/^\\d{4}-\\d{2}-\\d{2}/.test(text);
    if (template) return "format_description";
    if (/^\\d{4}-\\d{2}-\\d{2}(?:[T\\s]\\d{2}:\\d{2}(?::\\d{2})?)?/.test(text)) return "parseable_time";
    if (/^\\d{1,2}:\\d{2}(?::\\d{2})?$/.test(text)) return "clock_without_date";
    return "other_redacted";
  };
  const labelKind = (value) => {
    const text = String(value || "").trim();
    if (!text) return "empty";
    if (text === "买家") return "buyer";
    if (text === "商家") return "merchant";
    if (text === "已回复") return "replied";
    if (text === "未回复") return "unreplied";
    return "other_redacted";
  };
  const bump = (bag, key) => { bag[key] = (bag[key] || 0) + 1; };
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
    const classStructure = classesOf(el);
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
    for (const child of Array.from(el.children || [])) walk(child, depth + 1, path);
  };
  walk(root, 0, []);
  const groups = [];
  const seenRows = new Set();
  const visit = (parent) => {
    const buckets = new Map();
    for (const child of Array.from(parent.children || [])) {
      const signature = child.tagName + "|" + classesOf(child).join(".");
      if (!buckets.has(signature)) buckets.set(signature, []);
      buckets.get(signature).push(child);
      visit(child);
    }
    for (const rows of buckets.values()) {
      if (rows.length < 2 || classesOf(rows[0]).length === 0) continue;
      const visible = rows.filter((row) => !isHidden(row));
      const hiddenCount = rows.length - visible.length;
      for (const row of rows) seenRows.add(row);
      const seenIds = new Map();
      const idShapes = { generic_shell: 0, other_shape: 0, long_numeric: 0, long: 0, empty: 0 };
      let present = 0;
      let duplicated = 0;
      for (const row of visible) {
        const idValue = row.getAttribute("id");
        if (!idValue) { idShapes.empty += 1; continue; }
        present += 1;
        idShapes[idShape(idValue)] += 1;
        seenIds.set(idValue, (seenIds.get(idValue) || 0) + 1);
      }
      for (const count of seenIds.values()) if (count > 1) duplicated += 1;
      let uniqueness = "insufficient";
      if (present === 0) uniqueness = "not_on_repeated_rows";
      else if (idShapes.generic_shell === present) uniqueness = "generic_shell";
      else if (duplicated > 0) uniqueness = "duplicated";
      else if (seenIds.size === present && present >= 2) uniqueness = "unique_in_this_snapshot";
      const time = { format_description: 0, parseable_time: 0, clock_without_date: 0, other_redacted: 0, empty: 0 };
      const labels = { buyer: 0, merchant: 0, replied: 0, unreplied: 0, other_redacted: 0, empty: 0 };
      for (const row of visible) {
        bump(time, timeKind(row.getAttribute("msgtimeformate")));
        bump(labels, labelKind(row.getAttribute("title")));
        bump(labels, labelKind(row.getAttribute("aria-label")));
      }
      const declaredRaw = parent.getAttribute("aria-rowcount");
      const declared = declaredRaw && /^\\d{1,6}$/.test(declaredRaw) ? Number(declaredRaw) : null;
      groups.push({
        tag: String(rows[0].tagName || "").toLowerCase(),
        class_structure: classesOf(rows[0]),
        path: classesOf(parent).slice(0, 3),
        rendered_count: rows.length,
        visible_count: visible.length,
        hidden_count: hiddenCount,
        virtual_list: declared !== null && declared > rows.length,
        declared_row_count: declared,
        id: {
          present: present,
          distinct: seenIds.size,
          duplicated,
          shapes: idShapes,
          uniqueness,
          stability: "unproven_single_snapshot"
        },
        time,
        labels
      });
    }
  };
  visit(root);
  const shell = { generic_shell: 0, other_shape: 0, long_numeric: 0, long: 0 };
  const shellWalk = (el) => {
    if (!seenRows.has(el)) {
      const idValue = el.getAttribute && el.getAttribute("id");
      if (idValue) shell[idShape(idValue)] += 1;
    }
    for (const child of Array.from(el.children || [])) shellWalk(child);
  };
  shellWalk(root);
  return { nodes, truncated: nodes.length >= 300, value_assessment: { groups, shell_ids: shell } };
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
    report = _classify(nodes, truncated=bool(raw.get("truncated")))
    report["value_assessment"] = _value_decisions(raw.get("value_assessment"))
    return report


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


_ENUMS = frozenset(
    {
        "unique_in_this_snapshot",
        "duplicated",
        "not_on_repeated_rows",
        "generic_shell",
        "insufficient",
        "unproven_single_snapshot",
        "other_shape",
        "long_numeric",
        "long",
        "empty",
        "format_description",
        "parseable_time",
        "clock_without_date",
        "other_redacted",
        "buyer",
        "merchant",
        "replied",
        "unreplied",
    }
)


def _clean_token(value: Any) -> str | None:
    if isinstance(value, str) and (_SAFE_CLASS.fullmatch(value) or value in _ENUMS):
        return value
    return None


def _clean_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if 0 <= value <= 1_000_000 else 0


def _value_decisions(raw: Any) -> dict[str, Any]:
    groups_in = raw.get("groups") if isinstance(raw, dict) else None
    shell_in = raw.get("shell_ids") if isinstance(raw, dict) else None
    groups: list[dict[str, Any]] = []
    if isinstance(groups_in, list):
        for item in groups_in:
            if not isinstance(item, dict):
                continue
            classes = [token for token in item.get("class_structure") or [] if _clean_token(token)]
            path = [token for token in item.get("path") or [] if _clean_token(token)]
            if not classes:
                continue
            identity = item.get("id") if isinstance(item.get("id"), dict) else {}
            uniqueness = identity.get("uniqueness")
            groups.append(
                {
                    "tag": _clean_token(item.get("tag")) or "unknown",
                    "class_structure": classes,
                    "path": path,
                    "rendered_count": _clean_int(item.get("rendered_count")),
                    "visible_count": _clean_int(item.get("visible_count")),
                    "hidden_count": _clean_int(item.get("hidden_count")),
                    "virtual_list": item.get("virtual_list") is True,
                    "declared_row_count": _clean_int(item.get("declared_row_count")) or None,
                    "id": {
                        "present": _clean_int(identity.get("present")),
                        "distinct": _clean_int(identity.get("distinct")),
                        "duplicated": _clean_int(identity.get("duplicated")),
                        "shapes": {
                            key: _clean_int((identity.get("shapes") or {}).get(key))
                            for key in ("generic_shell", "other_shape", "long_numeric", "long", "empty")
                        },
                        "uniqueness": uniqueness if uniqueness in _ENUMS else "insufficient",
                        "stability": "unproven_single_snapshot",
                    },
                    "time": {
                        key: _clean_int((item.get("time") or {}).get(key))
                        for key in (
                            "format_description",
                            "parseable_time",
                            "clock_without_date",
                            "other_redacted",
                            "empty",
                        )
                    },
                    "labels": {
                        key: _clean_int((item.get("labels") or {}).get(key))
                        for key in ("buyer", "merchant", "replied", "unreplied", "other_redacted", "empty")
                    },
                    "row_identity": "unproven",
                }
            )
    shell = {
        key: _clean_int((shell_in or {}).get(key))
        for key in ("generic_shell", "other_shape", "long_numeric", "long")
    }
    time_totals = {key: 0 for key in ("format_description", "parseable_time", "clock_without_date", "other_redacted")}
    label_totals = {key: 0 for key in ("buyer", "merchant", "replied", "unreplied", "other_redacted")}
    hidden = 0
    virtual = False
    unique_groups = 0
    duplicated_groups = 0
    missing_id_groups = 0
    generic_groups = 0
    for group in groups:
        hidden += group["hidden_count"]
        virtual = virtual or group["virtual_list"]
        uniqueness = group["id"]["uniqueness"]
        if uniqueness == "unique_in_this_snapshot":
            unique_groups += 1
        elif uniqueness == "duplicated":
            duplicated_groups += 1
        elif uniqueness == "not_on_repeated_rows":
            missing_id_groups += 1
        elif uniqueness == "generic_shell":
            generic_groups += 1
        for key, value in group["time"].items():
            if key in time_totals:
                time_totals[key] += value
        for key, value in group["labels"].items():
            if key in label_totals:
                label_totals[key] += value
    human: list[str] = []
    if not groups:
        id_evidence = "insufficient_no_repeated_rows"
        human.append("还没有可比较的重复行。请只确认屏幕上哪一块是会话或消息列表，不要复制 id。")
    elif virtual:
        id_evidence = "incomplete_virtual_list"
        human.append("列表像是虚拟滚动，当前只渲染了一部分。不能用这一屏证明 id 唯一。")
    elif duplicated_groups:
        id_evidence = "duplicated"
    elif generic_groups and generic_groups == len(groups):
        id_evidence = "generic_shell_not_message_id"
        human.append("重复行上的 id 是外壳节点常用名，不能当成消息 id。")
    elif missing_id_groups and missing_id_groups == len(groups):
        id_evidence = "not_on_repeated_rows"
        if any(shell.values()):
            human.append("重复行上没有 id。外壳节点上的 id 只说明页面骨架，不能当成消息 id。")
    elif unique_groups and unique_groups == len(groups):
        id_evidence = "unique_in_this_snapshot"
        human.append("这次采样里重复行的 id 没有撞车，但还不能证明换一条消息后仍然稳定。不要把 id 原文发出来。")
    else:
        id_evidence = "insufficient"
        human.append("重复行的 id 形状不一致，或证据不够。不能据此做消息去重。")
    present_times = [key for key, count in time_totals.items() if count]
    if present_times == ["format_description"]:
        time_evidence = "format_description_not_a_time"
    elif present_times == ["parseable_time"]:
        time_evidence = "parseable_shape_needs_human_meaning_check"
        human.append("msgtimeformate 的形状像日期时间。请在屏幕上确认它是不是该条消息的发送时间，不要把原文贴进工具。")
    elif present_times == ["clock_without_date"]:
        time_evidence = "clock_without_date"
        human.append("只看到钟点、没有日期，不能当作完整消息时间。")
    elif present_times == ["other_redacted"]:
        time_evidence = "unreadable_redacted"
        human.append("msgtimeformate 不是格式说明，也不是可解析的日期时间。原文没有写出。")
    elif present_times:
        time_evidence = "mixed_or_unreadable"
        human.append("时间属性里有不止一种形状。不能把它当成消息时间。")
    else:
        time_evidence = "absent"
    if label_totals["other_redacted"]:
        role_evidence = "non_allowlist_text_redacted"
        human.append("title 或 aria-label 含有买家、商家、已回复、未回复以外的文字。请只回答是否恰好是这四个词之一。")
    elif label_totals["buyer"] and label_totals["merchant"]:
        role_evidence = "exact_role_labels_on_sampled_rows"
    elif label_totals["replied"] or label_totals["unreplied"]:
        role_evidence = "reply_labels_without_direction"
        human.append("只看到已回复或未回复，还不能区分买家和商家。")
    else:
        role_evidence = "absent"
    return {
        "groups": groups,
        "shell_ids": shell,
        "hidden_row_count": hidden,
        "virtual_list": virtual,
        "id_evidence": id_evidence,
        "id_stability": "unproven_single_snapshot",
        "can_use_for_dedupe": False,
        "time_evidence": time_evidence,
        "can_treat_time_as_message_time": False,
        "role_evidence": role_evidence,
        "can_treat_labels_as_reply_status": False,
        "human_checks": human,
        "values_included": False,
        "production_mapping_validated": False,
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
