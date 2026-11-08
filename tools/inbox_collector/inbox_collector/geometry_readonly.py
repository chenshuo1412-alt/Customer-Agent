"""Bounded geometry read for one visible test phrase.

The request op must be geometry-readonly. Unknown operations are rejected
instead of falling through to a full-page export. Nothing here reads storage,
cookies, network, or framework maps, and it does not click or type.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

_ALLOWED_FIELDS = frozenset({"op", "probe_phrase", "keep_local_detail"})
_ROLE_LABELS = frozenset({"买家", "商家", "客服"})
_SHORT_INDEX = re.compile(r"^\d{1,6}$")
_LONG_NUMBER = re.compile(r"^\d{7,}$")

_GEOMETRY_JS = """
async (phrase) => {
  const MARK = "GEOMETRY_READONLY";
  const norm = (value) => String(value || "").replace(/\\s+/g, "");
  const wanted = norm(phrase);
  let nodeTruncated = false;
  const hidden = (el) => {
    if (!el || el.nodeType !== 1) return true;
    if (el.hidden || el.getAttribute("aria-hidden") === "true") return true;
    const view = el.ownerDocument && el.ownerDocument.defaultView;
    if (!view) return false;
    const style = view.getComputedStyle(el);
    return style.display === "none" || style.visibility === "hidden";
  };
  const rectOf = (el) => {
    if (!el || !el.getBoundingClientRect) return null;
    const rect = el.getBoundingClientRect();
    return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
  };
  const mean = (samples, key) => {
    let total = 0;
    for (const sample of samples) total += Number(sample[key] || 0);
    return Math.round((total / samples.length) * 10) / 10;
  };
  const side = (bubble, container) => {
    if (!bubble || !container || bubble.width <= 0 || container.width <= 0) return "unproven";
    const bubbleMid = bubble.x + bubble.width / 2;
    const containerMid = container.x + container.width / 2;
    if (bubbleMid < containerMid - 8) return "left";
    if (bubbleMid > containerMid + 8) return "right";
    return "center";
  };
  const avatarSide = (avatar, bubble) => {
    if (!avatar) return "absent";
    if (!bubble || avatar.width <= 0 || bubble.width <= 0) return "unproven";
    return (avatar.x + avatar.width / 2) < (bubble.x + bubble.width / 2) ? "left_of_bubble" : "right_of_bubble";
  };
  const exactRole = (el) => {
    const text = String(el && el.textContent || "").trim();
    return text === "买家" || text === "商家" || text === "客服" ? text : "";
  };
  const roleOf = (row) => {
    const found = new Set();
    const local = row.querySelectorAll ? row.querySelectorAll("span, em, i, b, strong, label, div") : [];
    if (local.length > 24) nodeTruncated = true;
    for (const node of Array.from(local).slice(0, 24)) {
      const text = exactRole(node);
      if (text) found.add(text);
    }
    let source = "row_allowlist_label";
    if (!found.size) {
      source = "ancestor_allowlist_label";
      let parent = row.parentElement;
      let depth = 0;
      while (parent && depth < 3 && !found.size) {
        for (const child of Array.from(parent.children || [])) {
          if (child === row || (child.contains && child.contains(row))) continue;
          const text = exactRole(child);
          if (text) found.add(text);
        }
        parent = parent.parentElement;
        depth += 1;
      }
    }
    if (found.size === 1) return { role_label: Array.from(found)[0], role_source: source };
    if (found.size > 1) return { role_label: "conflict", role_source: source };
    return { role_label: "unknown", role_source: "no_visible_role_evidence" };
  };
  const readId = (el, name) => {
    if (!el || !el.getAttribute) return "";
    return String(el.getAttribute(name) || "");
  };
  const locateAttr = (el, name) => {
    const own = readId(el, name);
    if (own) return { value: own, location: "on_row" };
    const pending = Array.from(el.children || []);
    let seen = 0;
    while (pending.length && seen < 12) {
      const node = pending.shift();
      seen += 1;
      const value = readId(node, name);
      if (value) return { value, location: "on_descendant" };
      for (const child of Array.from(node.children || [])) pending.push(child);
    }
    let parent = el.parentElement;
    let depth = 0;
    while (parent && depth < 4) {
      const value = readId(parent, name);
      if (value) return { value, location: "on_ancestor" };
      parent = parent.parentElement;
      depth += 1;
    }
    return { value: "", location: "absent" };
  };
  const safeClasses = (el) => String(el && el.getAttribute && el.getAttribute("class") || "").split(/\\s+/).filter((token) => /^[A-Za-z_][A-Za-z0-9_-]{0,48}$/.test(token)).slice(0, 4);
  const outline = (el, depth) => ({
    tag: String(el && el.tagName || "").toLowerCase() || "other",
    classes: safeClasses(el),
    level: depth
  });
  const ancestorsOf = (el) => {
    const out = [];
    let parent = el ? el.parentElement : null;
    let depth = 1;
    while (parent && depth <= 4 && out.length < 4) {
      out.push(outline(parent, depth));
      parent = parent.parentElement;
      depth += 1;
    }
    return out;
  };
  const findBubble = (row) => {
    const named = row.querySelector(".msg-content-box, .msg-content");
    if (named && !hidden(named)) return { el: named, found: "named_content" };
    const rowRect = rectOf(row);
    const smaller = Array.from(row.querySelectorAll ? row.querySelectorAll("div") : []).filter((el) => {
      if (hidden(el)) return false;
      const rect = rectOf(el);
      return rect && rowRect && rect.width > 0 && rect.height > 0 && rect.width < rowRect.width - 8;
    }).slice(0, 8);
    if (smaller.length === 1) return { el: smaller[0], found: "nested_smaller" };
    if (smaller.length > 1) return { el: null, found: "ambiguous" };
    return { el: null, found: "absent" };
  };
  const sampleRow = (row) => {
    const bubbleInfo = findBubble(row);
    const bubble = bubbleInfo.el;
    const avatar = row.querySelector("img");
    const samples = [];
    for (let index = 0; index < 8; index += 1) {
      const box = rectOf(bubble);
      const container = rectOf(row);
      const face = avatar ? rectOf(avatar) : null;
      samples.push({ box, container, face });
    }
    const zero = samples.some((sample) => !sample.container || sample.container.width <= 0 || sample.container.height <= 0 || (bubble && (!sample.box || sample.box.width <= 0 || sample.box.height <= 0)));
    const unstable = !zero && bubble && samples.some((sample) => sample.box && (Math.abs(sample.box.x - samples[0].box.x) > 2 || Math.abs(sample.box.width - samples[0].box.width) > 2));
    const view = row.ownerDocument.defaultView;
    const style = bubble && view ? view.getComputedStyle(bubble) : null;
    const boxes = samples.map((sample) => sample.box).filter(Boolean);
    const containers = samples.map((sample) => sample.container).filter(Boolean);
    const idInfo = locateAttr(row, "id");
    const uidInfo = locateAttr(row, "currentuid");
    return {
      samples: 8,
      zero_rectangle: zero,
      geometry_unstable: unstable,
      bubble_found: bubbleInfo.found,
      bubble_tag: bubble ? String(bubble.tagName || "").toLowerCase() : null,
      bubble_classes: bubble ? safeClasses(bubble) : [],
      bubble: boxes.length ? { x: mean(boxes, "x"), y: mean(boxes, "y"), width: mean(boxes, "width"), height: mean(boxes, "height") } : null,
      container: containers.length ? { x: mean(containers, "x"), y: mean(containers, "y"), width: mean(containers, "width"), height: mean(containers, "height") } : null,
      css_float: style ? String(style.float || style.cssFloat || "none") : null,
      text_align: style ? String(style.textAlign || "start") : null,
      bubble_horizontal: side(samples[0].box, samples[0].container),
      avatar_side: avatarSide(samples[0].face, samples[0].box),
      id_raw: idInfo.value,
      id_location: idInfo.location,
      currentuid_raw: uidInfo.value,
      currentuid_location: uidInfo.location,
      classes: safeClasses(row)
    };
  };
  const blank = {
    samples: 0,
    zero_rectangle: null,
    geometry_unstable: null,
    bubble: null,
    container: null,
    css_float: null,
    text_align: null,
    bubble_horizontal: null,
    avatar_side: null,
    role_label: null,
    role_source: null,
    id_raw: "",
    id_location: "absent",
    currentuid_raw: "",
    currentuid_location: "absent",
    bubble_found: "absent",
    classes: []
  };
  const highlightedAll = Array.from(document.querySelectorAll(".active, [aria-selected='true'], [aria-current='true'], [aria-current='page']")).filter((row) => !hidden(row));
  const highlightedTruncated = highlightedAll.length > 8;
  const selected = highlightedAll.slice(0, 8);
  const base = {
    marker: MARK,
    handler: "geometry-readonly",
    op: "geometry-readonly",
    match_count: 0,
    selected_count: selected.length,
    matched: blank,
    following: blank,
    selected_id_raw: selected.length === 1 ? locateAttr(selected[0], "id").value : "",
    selected_id_location: selected.length === 1 ? locateAttr(selected[0], "id").location : "absent",
    selected_currentuid_raw: selected.length === 1 ? locateAttr(selected[0], "currentuid").value : "",
    selected_currentuid_location: selected.length === 1 ? locateAttr(selected[0], "currentuid").location : "absent",
    highlighted_truncated: highlightedTruncated,
    highlighted_observed: highlightedAll.length,
    production_mapping_validated: false,
    production_send_allowed: false,
    values_included: false
  };
  if (!wanted) return Object.assign(base, { status: "phrase_absent", error: "phrase_absent" });
  const rows = Array.from(document.querySelectorAll(".content-box li.onemsg")).filter((row) => !hidden(row));
  const rowHasPhrase = (row) => {
    const named = row.querySelector(".msg-content-box, .msg-content");
    if (named && norm(named.textContent) === wanted) return true;
    const nodes = Array.from(row.querySelectorAll("div, span, p"));
    if (nodes.length > 48) nodeTruncated = true;
    return nodes.slice(0, 48).some((node) => norm(node.textContent) === wanted);
  };
  const hits = [];
  for (const row of rows) {
    if (rowHasPhrase(row)) hits.push(row);
  }
  base.match_count = hits.length;
  if (hits.length === 0) return Object.assign(base, { status: nodeTruncated ? "phrase_search_truncated" : "phrase_absent", error: nodeTruncated ? "node_cap" : "phrase_absent" });
  if (hits.length !== 1) return Object.assign(base, { status: "phrase_not_unique", error: "phrase_not_unique" });
  const row = hits[0];
  const visible = rows.filter((item) => item.closest && row.closest && item.closest(".content-box") === row.closest(".content-box"));
  const index = visible.indexOf(row);
  let following = null;
  let sibling = row.nextElementSibling;
  let guard = 0;
  while (sibling && guard < 5) {
    if (!hidden(sibling)) { following = sibling; break; }
    sibling = sibling.nextElementSibling;
    guard += 1;
  }
  const matched = sampleRow(row);
  const role = roleOf(row);
  matched.role_label = role.role_label;
  matched.role_source = role.role_source;
  const follow = following ? sampleRow(following) : blank;
  if (following) {
    const followRole = roleOf(following);
    follow.role_label = followRole.role_label;
    follow.role_source = followRole.role_source;
  }
  const panel = row.closest(".content-box") || row.parentElement;
  const headerScopes = [panel, panel && panel.parentElement].filter(Boolean).slice(0, 2);
  let header = null;
  for (const scope of headerScopes) {
    if (header) break;
    for (const child of Array.from(scope.children || []).slice(0, 8)) {
      if (hidden(child) || child === row || (child.contains && child.contains(row))) continue;
      const tag = String(child.tagName || "").toLowerCase();
      if (/^h[1-3]$/.test(tag) || safeClasses(child).some((token) => /header/i.test(token))) {
        header = child;
        break;
      }
    }
  }
  const described = selected.slice(0, 4).map((item) => ({
    tag: String(item.tagName || "").toLowerCase(),
    classes: safeClasses(item),
    ancestors: ancestorsOf(item),
    id_location: locateAttr(item, "id").location,
    currentuid_location: locateAttr(item, "currentuid").location
  }));
  const structure = {
    limits: { depth: 4, nodes: 48, candidates: 8 },
    truncated: highlightedTruncated || nodeTruncated,
    truncation_reason: highlightedTruncated ? "candidate_cap" : nodeTruncated ? "node_cap" : null,
    matched_classes: safeClasses(row),
    matched_ancestors: ancestorsOf(row),
    following_tag: following ? String(following.tagName || "").toLowerCase() : null,
    following_classes: following ? safeClasses(following) : [],
    panel: panel ? outline(panel, 0) : null,
    header: header ? outline(header, 1) : { tag: null, classes: [], level: null, collected: false },
    highlighted: described,
    assumptions_not_evidence: ["literal_role_label_required", "currentuid_only_on_li", "active_only_li_chat_item"]
  };
  let status = "geometry_ready";
  let error = null;
  const followingMeasured = following && follow.bubble_found !== "absent";
  if (matched.zero_rectangle || (followingMeasured && follow.zero_rectangle)) {
    status = "zero_rectangle";
    error = "zero_rectangle";
  } else if (matched.geometry_unstable || (followingMeasured && follow.geometry_unstable)) {
    status = "geometry_unstable";
    error = "geometry_unstable";
  }
  return Object.assign(base, { status, error, matched, following: follow, following_present: !!following, structure });
}
"""


def _format_identifier(value: str) -> str:
    if not value:
        return "empty"
    if _SHORT_INDEX.fullmatch(value):
        return "short_index_not_uid"
    if _LONG_NUMBER.fullmatch(value):
        return "long_numeric"
    if re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", value):
        return "token"
    return "other"


def _hash_identifier(value: str) -> str | None:
    if _format_identifier(value) in {"empty", "short_index_not_uid"}:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _identifier(value: str) -> dict[str, Any]:
    return {"format": _format_identifier(value), "sha256": _hash_identifier(value)}


def _public_row(row: dict[str, Any] | None) -> dict[str, Any]:
    source = row or {}
    identifier = _identifier(str(source.get("id_raw") or ""))
    currentuid = _identifier(str(source.get("currentuid_raw") or ""))
    return {
        "samples": int(source.get("samples") or 0),
        "zero_rectangle": source.get("zero_rectangle"),
        "geometry_unstable": source.get("geometry_unstable"),
        "bubble": source.get("bubble"),
        "container": source.get("container"),
        "css_float": source.get("css_float"),
        "text_align": source.get("text_align"),
        "bubble_horizontal": source.get("bubble_horizontal"),
        "avatar_side": source.get("avatar_side"),
        "role_label": source.get("role_label") if source.get("role_label") in {"买家", "商家", "客服", "unknown", "conflict"} else None,
        "role_source": source.get("role_source") if source.get("role_source") in {"row_allowlist_label", "ancestor_allowlist_label", "no_visible_role_evidence", "visible_allowlist_label"} else None,
        "bubble_found": source.get("bubble_found") if source.get("bubble_found") in {"named_content", "nested_smaller", "ambiguous", "absent"} else None,
        "bubble_tag": source.get("bubble_tag") if isinstance(source.get("bubble_tag"), str) else None,
        "bubble_classes": [token for token in source.get("bubble_classes") or [] if isinstance(token, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,48}", token)][:4],
        "classes": [token for token in source.get("classes") or [] if isinstance(token, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,48}", token)][:4],
        "id": identifier,
        "id_location": source.get("id_location") if source.get("id_location") in {"on_row", "on_descendant", "on_ancestor", "absent"} else "absent",
        "currentuid": currentuid,
        "currentuid_location": source.get("currentuid_location") if source.get("currentuid_location") in {"on_row", "on_descendant", "on_ancestor", "absent"} else "absent",
    }


def _id_observation(left: str | None, right: str | None) -> str:
    if not left or not right:
        return "not_comparable"
    return "id_hash_equal" if left == right else "id_hash_differ"


def _link(matched: dict[str, Any], selected_id: str, selected_uid: str, selected_count: int) -> str:
    if selected_count == 0:
        return "selection_missing"
    if selected_count != 1:
        return "selection_ambiguous"
    left = matched.get("currentuid", {}).get("sha256")
    right = _hash_identifier(selected_uid)
    if not left or not right:
        return "not_comparable"
    return "hash_match" if left == right else "uid_mismatch"


def _status(raw: dict[str, Any], link: str, role: str | None) -> tuple[str, str | None]:
    status = str(raw.get("status") or "geometry_unavailable")
    error = raw.get("error")
    if status in {"phrase_absent", "phrase_not_unique", "phrase_search_truncated", "zero_rectangle", "geometry_unstable"}:
        return status, str(error or status)
    if raw.get("highlighted_truncated") is True:
        return "selection_truncated", "candidate_cap"
    if link == "uid_mismatch":
        return "uid_mismatch", "uid_mismatch"
    if link == "selection_ambiguous":
        return "selection_ambiguous", "selection_ambiguous"
    return "structure_ready", None


def _empty_report(status: str, error: str | None) -> dict[str, Any]:
    blank = _public_row(None)
    return {
        "handler": "geometry-readonly",
        "op": "geometry-readonly",
        "status": status,
        "error": error,
        "match_count": 0,
        "selected_count": 0,
        "samples": 0,
        "matched": blank,
        "following": blank,
        "following_present": False,
        "selected": {"id": _identifier(""), "currentuid": _identifier("")},
        "conversation_link": "selection_missing",
        "raw_detail_written": False,
        "values_included": False,
        "production_mapping_validated": False,
        "production_send_allowed": False,
    }


def _safe_tokens(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [token for token in value if isinstance(token, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,48}", token)][:4]


def _clean_structure(raw: Any) -> dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    highlighted = []
    for item in source.get("highlighted") or []:
        if not isinstance(item, dict) or len(highlighted) >= 4:
            continue
        highlighted.append(
            {
                "tag": item.get("tag") if isinstance(item.get("tag"), str) else "other",
                "classes": _safe_tokens(item.get("classes")),
                "ancestors": [
                    {
                        "tag": ancestor.get("tag") if isinstance(ancestor, dict) else "other",
                        "classes": _safe_tokens(ancestor.get("classes") if isinstance(ancestor, dict) else []),
                        "level": ancestor.get("level") if isinstance(ancestor, dict) else None,
                    }
                    for ancestor in (item.get("ancestors") or [])[:4]
                    if isinstance(ancestor, dict)
                ],
                "id_location": item.get("id_location") if item.get("id_location") in {"on_row", "on_descendant", "on_ancestor", "absent"} else "absent",
                "currentuid_location": item.get("currentuid_location") if item.get("currentuid_location") in {"on_row", "on_descendant", "on_ancestor", "absent"} else "absent",
            }
        )
    panel = source.get("panel") if isinstance(source.get("panel"), dict) else None
    header = source.get("header") if isinstance(source.get("header"), dict) else None
    return {
        "limits": {"depth": 4, "nodes": 48, "candidates": 8},
        "truncated": source.get("truncated") is True,
        "truncation_reason": source.get("truncation_reason") if source.get("truncation_reason") in {"candidate_cap", "node_cap", "depth_cap"} else None,
        "matched_classes": _safe_tokens(source.get("matched_classes")),
        "matched_ancestors": [
            {"tag": item.get("tag"), "classes": _safe_tokens(item.get("classes")), "level": item.get("level")}
            for item in (source.get("matched_ancestors") or [])[:4]
            if isinstance(item, dict)
        ],
        "following_tag": source.get("following_tag") if isinstance(source.get("following_tag"), str) else None,
        "following_classes": _safe_tokens(source.get("following_classes")),
        "panel": None
        if panel is None
        else {"tag": panel.get("tag"), "classes": _safe_tokens(panel.get("classes")), "level": panel.get("level")},
        "header": None
        if header is None
        else {"tag": header.get("tag"), "classes": _safe_tokens(header.get("classes")), "level": header.get("level"), "collected": header.get("collected") is not False},
        "highlighted": highlighted,
        "assumptions_not_evidence": [
            "literal_role_label_required",
            "currentuid_only_on_li",
            "active_only_li_chat_item",
        ],
    }


def _structure_hash(raw: Any) -> str:
    cleaned = _clean_structure(raw)
    material = json.dumps(
        {
            "matched": cleaned["matched_classes"],
            "ancestors": cleaned["matched_ancestors"],
            "following": cleaned["following_classes"],
            "panel": cleaned["panel"],
            "highlighted": [item["classes"] for item in cleaned["highlighted"]],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _write_detail(data_dir: Path, sensitive: dict[str, str]) -> None:
    path = data_dir / "geometry-readonly-detail.json"
    path.write_text(json.dumps(sensitive, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def run_geometry(
    page: Any,
    data_dir: Path,
    phrase: str,
    *,
    keep_local_detail: bool = False,
) -> dict[str, Any]:
    raw = page.evaluate(_GEOMETRY_JS, phrase)
    if not isinstance(raw, dict) or raw.get("marker") != "GEOMETRY_READONLY":
        return _empty_report("geometry_unavailable", "geometry_unavailable")
    matched_raw = raw.get("matched") if isinstance(raw.get("matched"), dict) else {}
    following_raw = raw.get("following") if isinstance(raw.get("following"), dict) else {}
    matched = _public_row(matched_raw)
    following = _public_row(following_raw)
    selected_id = str(raw.get("selected_id_raw") or "")
    selected_uid = str(raw.get("selected_currentuid_raw") or "")
    link = _link(matched, selected_id, selected_uid, int(raw.get("selected_count") or 0))
    role = matched.get("role_label")
    status, error = _status(raw, link, role if raw.get("match_count") == 1 else None)
    if status in {"phrase_absent", "phrase_not_unique"}:
        link = "not_comparable" if int(raw.get("selected_count") or 0) == 1 else _link(matched, selected_id, selected_uid, int(raw.get("selected_count") or 0))
    detail_written = False
    if keep_local_detail and raw.get("match_count") == 1:
        _write_detail(
            data_dir,
            {
                "matched_id": str(matched_raw.get("id_raw") or ""),
                "matched_currentuid": str(matched_raw.get("currentuid_raw") or ""),
                "selected_id": selected_id,
                "selected_currentuid": selected_uid,
            },
        )
        detail_written = True
    report = {
        "handler": "geometry-readonly",
        "op": "geometry-readonly",
        "status": status,
        "error": error,
        "match_count": int(raw.get("match_count") or 0),
        "selected_count": int(raw.get("selected_count") or 0),
        "samples": int(matched.get("samples") or 0),
        "matched": matched,
        "following": following,
        "following_present": raw.get("following_present") is True,
        "selected": {
            "id": _identifier(selected_id),
            "id_location": raw.get("selected_id_location") if raw.get("selected_id_location") in {"on_row", "on_descendant", "on_ancestor", "absent"} else "absent",
            "currentuid": _identifier(selected_uid),
            "currentuid_location": raw.get("selected_currentuid_location") if raw.get("selected_currentuid_location") in {"on_row", "on_descendant", "on_ancestor", "absent"} else "absent",
        },
        "conversation_link": link,
        "identifier_observation": _id_observation(matched.get("id", {}).get("sha256"), _hash_identifier(selected_id)),
        "shared_identifier_refused": link != "hash_match",
        "selectors_inferred": False,
        "highlighted_observed": int(raw.get("highlighted_observed") or raw.get("selected_count") or 0),
        "selection_status": "truncated" if raw.get("highlighted_truncated") is True else link if link in {"selection_missing", "selection_ambiguous"} else "one",
        "structure": _clean_structure(raw.get("structure")),
        "session_pairing": {
            "method": "visible_snapshot_only",
            "durable": False,
            "invalid_if": ["dom_structure_changes", "process_restart", "conversation_switch"],
            "shared_identifier": link,
            "ephemeral_structure_sha256": _structure_hash(raw.get("structure")),
            "note": "本页结构哈希只能在这次渲染里人工对照。它不是平台 UID，进程重启或页面结构变化后即失效。",
        },
        "assumptions_not_evidence": [
            "literal_role_label_required",
            "currentuid_only_on_li",
            "active_only_li_chat_item",
        ],
        "raw_detail_written": detail_written,
        "values_included": False,
        "production_mapping_validated": False,
        "production_send_allowed": False,
    }
    if phrase and phrase in json.dumps(report, ensure_ascii=False):
        return _empty_report("phrase_leaked", "phrase_leaked")
    return report


def reject_request(payload: Any) -> dict[str, Any] | None:
    """Return an error report, or None when the payload may be dispatched."""
    if not isinstance(payload, dict):
        report = _empty_report("request_rejected", "request_rejected")
        report["handler"] = "request-router"
        return report
    op = payload.get("op")
    if op is None:
        extra = set(payload) - {"probe_phrase"}
        if extra:
            report = _empty_report("field_rejected", "field_rejected")
            report["handler"] = "request-router"
            report["rejected_fields"] = sorted(str(name) for name in extra)
            return report
        return None
    allowed = {
        "probe": {"op", "probe_phrase"},
        "test-send": {"op", "probe_phrase"},
        "confirmed-text": {"op", "probe_phrase"},
        "input-state-readonly": {"op", "probe_phrase", "candidate_reply"},
        "geometry-readonly": _ALLOWED_FIELDS,
    }
    if op not in allowed:
        report = _empty_report("op_rejected", "op_rejected")
        report["handler"] = "request-router"
        report["rejected_op"] = str(op)[:40]
        report["allowed_ops"] = sorted(allowed)
        return report
    extra = set(payload) - allowed[str(op)]
    if extra:
        report = _empty_report("field_rejected", "field_rejected")
        report["handler"] = "request-router"
        report["rejected_fields"] = sorted(str(name) for name in extra)
        return report
    if op in {"geometry-readonly", "confirmed-text", "input-state-readonly"} and not isinstance(payload.get("probe_phrase"), str):
        report = _empty_report("phrase_absent", "phrase_absent")
        return report
    if op == "input-state-readonly" and "candidate_reply" in payload and not isinstance(payload.get("candidate_reply"), str):
        report = _empty_report("field_rejected", "field_rejected")
        report["handler"] = "request-router"
        report["rejected_fields"] = ["candidate_reply"]
        return report
    if "keep_local_detail" in payload and payload.get("keep_local_detail") is not True and payload.get("keep_local_detail") is not False:
        report = _empty_report("field_rejected", "field_rejected")
        report["rejected_fields"] = ["keep_local_detail"]
        return report
    return None
