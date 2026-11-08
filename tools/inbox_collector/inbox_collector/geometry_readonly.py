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
  const roleOf = (row, phraseText) => {
    const found = new Set();
    const nodes = row.querySelectorAll("span, em, i, b, strong, label");
    for (const node of nodes) {
      const text = String(node.textContent || "").trim();
      if (text === phraseText) continue;
      if (text === "买家" || text === "商家" || text === "客服") found.add(text);
    }
    if (found.size === 1) return { role_label: Array.from(found)[0], role_source: "visible_allowlist_label" };
    if (found.size > 1) return { role_label: "conflict", role_source: "visible_allowlist_label" };
    return { role_label: "unproven", role_source: "no_allowlist_label" };
  };
  const readId = (el, name) => {
    if (!el || !el.getAttribute) return "";
    return String(el.getAttribute(name) || "");
  };
  const sampleRow = (row) => {
    const bubble = row.querySelector(".msg-content-box, .msg-content") || row;
    const avatar = row.querySelector("img");
    const samples = [];
    for (let index = 0; index < 8; index += 1) {
      const box = rectOf(bubble);
      const container = rectOf(row);
      const face = avatar ? rectOf(avatar) : null;
      samples.push({ box, container, face });
    }
    const zero = samples.some((sample) => !sample.box || !sample.container || sample.box.width <= 0 || sample.box.height <= 0 || sample.container.width <= 0 || sample.container.height <= 0);
    const unstable = !zero && samples.some((sample) => Math.abs(sample.box.x - samples[0].box.x) > 2 || Math.abs(sample.box.width - samples[0].box.width) > 2);
    const view = row.ownerDocument.defaultView;
    const style = view.getComputedStyle(bubble);
    const boxes = samples.map((sample) => sample.box);
    const containers = samples.map((sample) => sample.container);
    return {
      samples: 8,
      zero_rectangle: zero,
      geometry_unstable: unstable,
      bubble: { x: mean(boxes, "x"), y: mean(boxes, "y"), width: mean(boxes, "width"), height: mean(boxes, "height") },
      container: { x: mean(containers, "x"), y: mean(containers, "y"), width: mean(containers, "width"), height: mean(containers, "height") },
      css_float: String(style.float || style.cssFloat || "none"),
      text_align: String(style.textAlign || "start"),
      bubble_horizontal: side(samples[0].box, samples[0].container),
      avatar_side: avatarSide(samples[0].face, samples[0].box),
      id_raw: readId(row, "id"),
      currentuid_raw: readId(row, "currentuid")
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
    currentuid_raw: ""
  };
  const selected = Array.from(document.querySelectorAll("li.chat-item")).filter((row) => {
    if (hidden(row)) return false;
    const classes = String(row.getAttribute("class") || "").split(/\\s+/);
    const current = row.getAttribute("aria-current");
    return classes.indexOf("active") >= 0 || row.getAttribute("aria-selected") === "true" || current === "true" || current === "page";
  });
  const base = {
    marker: MARK,
    handler: "geometry-readonly",
    op: "geometry-readonly",
    match_count: 0,
    selected_count: selected.length,
    matched: blank,
    following: blank,
    selected_id_raw: selected.length === 1 ? readId(selected[0], "id") : "",
    selected_currentuid_raw: selected.length === 1 ? readId(selected[0], "currentuid") : "",
    production_mapping_validated: false,
    production_send_allowed: false,
    values_included: false
  };
  if (!wanted) return Object.assign(base, { status: "phrase_absent", error: "phrase_absent" });
  const rows = Array.from(document.querySelectorAll(".content-box li.onemsg")).filter((row) => !hidden(row));
  const hits = [];
  for (const row of rows) {
    const content = row.querySelector(".msg-content-box, .msg-content");
    if (norm(content ? content.textContent : "") === wanted) hits.push(row);
  }
  base.match_count = hits.length;
  if (hits.length === 0) return Object.assign(base, { status: "phrase_absent", error: "phrase_absent" });
  if (hits.length !== 1) return Object.assign(base, { status: "phrase_not_unique", error: "phrase_not_unique" });
  const row = hits[0];
  const visible = rows.filter((item) => item.closest && row.closest && item.closest(".content-box") === row.closest(".content-box"));
  const index = visible.indexOf(row);
  const following = index >= 0 && index + 1 < visible.length ? visible[index + 1] : null;
  const matched = sampleRow(row);
  const role = roleOf(row, wanted);
  matched.role_label = role.role_label;
  matched.role_source = role.role_source;
  const follow = following ? sampleRow(following) : blank;
  if (following) {
    const followRole = roleOf(following, wanted);
    follow.role_label = followRole.role_label;
    follow.role_source = followRole.role_source;
  }
  let status = "geometry_ready";
  let error = null;
  if (matched.zero_rectangle || (following && follow.zero_rectangle)) {
    status = "zero_rectangle";
    error = "zero_rectangle";
  } else if (matched.geometry_unstable || (following && follow.geometry_unstable)) {
    status = "geometry_unstable";
    error = "geometry_unstable";
  }
  return Object.assign(base, { status, error, matched, following: follow, following_present: !!following });
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
        "role_label": source.get("role_label"),
        "role_source": source.get("role_source"),
        "id": identifier,
        "currentuid": currentuid,
    }


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
    if status in {"phrase_absent", "phrase_not_unique", "zero_rectangle", "geometry_unstable"}:
        return status, str(error or status)
    if link == "uid_mismatch":
        return "uid_mismatch", "uid_mismatch"
    if role in {None, "unproven", "conflict"}:
        return "role_unproven", "role_unproven"
    return "geometry_ready", None


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
        "selected": {"id": _identifier(selected_id), "currentuid": _identifier(selected_uid)},
        "conversation_link": link,
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
    if op == "geometry-readonly" and not isinstance(payload.get("probe_phrase"), str):
        report = _empty_report("phrase_absent", "phrase_absent")
        return report
    if "keep_local_detail" in payload and payload.get("keep_local_detail") is not True and payload.get("keep_local_detail") is not False:
        report = _empty_report("field_rejected", "field_rejected")
        report["rejected_fields"] = ["keep_local_detail"]
        return report
    return None
