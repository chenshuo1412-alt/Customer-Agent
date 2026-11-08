"""One explicit test send on the page login-prep already owns.

This is not store-wide auto-reply. Production mapping stays false.
The only buyer target is the operator-declared test phrase, and only when
that phrase is the unique visible text in the open message area.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable

from inbox_collector.auto_reply import (
    VERIFIED_MODEL,
    AutoReplyError,
    LoopbackGenerator,
    output_allowed,
    transport_audit,
    urllib_loopback_transport,
)
from inbox_collector.dom_diagnose import export_rendered_dom

_OPT_IN = "single-test.opt-in.json"
_STOP = "auto-reply.stop"
_SCOPE = "single_test_conversation"
_FORBIDDEN = ("价格", "物流", "规格", "安装", "售后", "退款", "改价", "补偿", "赔")

_ACT_JS = """
async (replyText) => {
  const norm = (value) => String(value || "").replace(/\\s+/g, "");
  const labelKind = (el) => {
    if (el && el.querySelector && el.querySelector("textarea, input, button")) return "not_a_leaf_control";
    let raw = "";
    try { raw = el && el.innerText != null ? String(el.innerText) : ""; } catch (e) { raw = ""; }
    const compact = raw.replace(/\\s+/g, "");
    if (!compact) return "empty";
    const lower = compact.toLowerCase();
    if (compact === "发送" || lower === "send") return "send";
    if (/^(?:发送|send)(?:[(（]?(?:enter|ctrl\\+enter|meta\\+enter)[)）]?)?$/i.test(compact)) return "send_with_shortcut";
    return "other_redacted";
  };
  const isAllow = (el) => {
    const kind = labelKind(el);
    return kind === "send" || kind === "send_with_shortcut";
  };
  const hidden = (el) => {
    if (!el || el.nodeType !== 1) return true;
    if (el.hidden || el.getAttribute("aria-hidden") === "true") return true;
    const style = el.ownerDocument.defaultView.getComputedStyle(el);
    return style.display === "none" || style.visibility === "hidden";
  };
  const phrase = norm(replyText && replyText.phrase);
  const reply = String(replyText && replyText.reply || "");
  const boxes = Array.from(document.querySelectorAll(".content-box"));
  const matches = [];
  for (const box of boxes) {
    for (const row of Array.from(box.querySelectorAll("li.onemsg"))) {
      if (hidden(row)) continue;
      const content = row.querySelector(".msg-content-box, .msg-content");
      if (norm(content ? content.textContent : "") === phrase) matches.push(row);
    }
  }
  if (matches.length !== 1) return { status: "probe_not_unique", match_count: matches.length, clicked: false };
  const row = matches[0];
  const box = row.closest(".content-box");
  const visible = Array.from(box.querySelectorAll("li.onemsg")).filter((item) => !hidden(item));
  if (visible.indexOf(row) !== visible.length - 1) return { status: "refused_newer_or_reply_present", clicked: false };
  const inputs = Array.from(document.querySelectorAll("textarea")).filter((el) => !hidden(el) && !el.disabled && !el.readOnly);
  const inRegion = inputs.filter((el) => !!(el.closest && el.closest(".content-box")));
  const field = inRegion.length === 1 ? inRegion[0] : null;
  if (!field) return { status: "input_not_verified", input_count: inRegion.length, clicked: false };
  const containsRows = (el) => !!(el && el.querySelector && el.querySelector("li.onemsg"));
  const chain = [field];
  let cursor = field;
  for (let step = 0; step < 8; step += 1) {
    const parent = cursor.parentElement;
    if (!parent || containsRows(parent)) break;
    chain.push(parent);
    cursor = parent;
  }
  const composer = chain[chain.length - 1];
  const seeds = [composer];
  const above = composer.parentElement;
  if (above && containsRows(above)) {
    for (const sib of Array.from(above.children || [])) {
      if (sib !== composer && !containsRows(sib)) seeds.push(sib);
    }
  }
  const senders = [];
  const visited = new Set();
  const walk = (el, depth) => {
    if (!el || el.nodeType !== 1 || visited.has(el) || depth > 6 || senders.length > 4) return;
    if (containsRows(el) && el !== composer) return;
    visited.add(el);
    const tag = String(el.tagName || "").toLowerCase();
    const role = String(el.getAttribute && el.getAttribute("role") || "");
    const type = String(el.getAttribute && el.getAttribute("type") || "");
    const semantic = tag === "button" || tag === "a" || role === "button" || type === "button" || type === "submit";
    if (el !== field && !hidden(el) && !el.disabled && semantic && isAllow(el)) senders.push(el);
    for (const child of Array.from(el.children || [])) walk(child, depth + 1);
  };
  seeds.forEach((seed, index) => walk(seed, index === 0 ? 0 : 1));
  if (senders.length !== 1) return { status: "send_control_not_verified", sender_count: senders.length, clicked: false };
  field.value = reply;
  field.dispatchEvent(new Event("input", { bubbles: true }));
  senders[0].click();
  const seen = () => Array.from(box.querySelectorAll("li.onemsg")).some((item) => !hidden(item) && norm(item.textContent).indexOf(norm(reply)) >= 0);
  for (let attempt = 0; attempt < 4; attempt += 1) {
    if (seen()) return { status: "test_sent", clicked: true, receipt: "matched" };
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  return { status: "paused_uncertain", clicked: true, receipt: "absent" };
}
"""


def phrase_digest(phrase: str) -> str:
    return hashlib.sha256(phrase.encode("utf-8")).hexdigest()


def write_opt_in(data_dir: Path, phrase: str) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "scope": _SCOPE,
        "phrase_sha256": phrase_digest(phrase),
        "max_sends": 1,
        "sends_used": 0,
        "phrase_role": "buyer_test",
    }
    path = data_dir / _OPT_IN
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _opt_in(data_dir: Path, phrase: str) -> str | None:
    path = data_dir / _OPT_IN
    if not path.is_file():
        return "opt_in_missing"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "opt_in_unreadable"
    if not isinstance(payload, dict) or payload.get("scope") != _SCOPE:
        return "opt_in_scope_refused"
    if payload.get("phrase_sha256") != phrase_digest(phrase):
        return "opt_in_phrase_mismatch"
    if int(payload.get("sends_used") or 0) >= 1:
        return "send_cap_reached"
    return None


def _mark_used(data_dir: Path, status: str) -> None:
    path = data_dir / _OPT_IN
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["sends_used"] = 1
    payload["status"] = status
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def greeting_allowed(text: str) -> bool:
    cleaned = text.strip()
    if not cleaned or len(cleaned) > 40 or not output_allowed(cleaned):
        return False
    return not any(term in cleaned for term in _FORBIDDEN)


def _base(status: str, **extra: Any) -> dict[str, Any]:
    report = {
        "status": status,
        "scope": _SCOPE,
        "storewide_auto_reply": False,
        "production_mapping_validated": False,
        "production_send_allowed": False,
        "list_binding": "not_proven_no_shared_identity",
        "real_buyer_send": False,
        "customer_service_quality_verified": False,
        "values_included": False,
        "resend_allowed": False,
    }
    report.update(extra)
    return report


def _probe_gate(probe: dict[str, Any]) -> str | None:
    phrase = probe.get("phrase_probe") if isinstance(probe.get("phrase_probe"), dict) else {}
    if phrase.get("unique") is not True:
        return "probe_not_unique"
    row = phrase.get("row") if isinstance(phrase.get("row"), dict) else {}
    if row.get("later_visible_rows", 1) != 0:
        return "refused_newer_or_reply_present"
    if phrase.get("controls_verified_for_test") is not True:
        return "controls_not_verified"
    if phrase.get("list_binding") != "not_proven_no_shared_identity":
        return "list_binding_unexpected"
    return None


def run_owned_test_send(
    page: Any,
    data_dir: Path,
    phrase: str,
    *,
    transport: Callable[[str, str, dict[str, Any] | None, int], tuple[int, dict[str, Any]]] | None = None,
    transport_label: str = "urllib_loopback",
) -> dict[str, Any]:
    """One opted-in test reply. Historical rows are not targets."""
    if (data_dir / _STOP).is_file():
        return _base("refused_stop")
    problem = _opt_in(data_dir, phrase)
    if problem:
        return _base(problem)
    if transport is None:
        transport = urllib_loopback_transport
        transport_label = "urllib_loopback"
    elif transport_label == "urllib_loopback":
        raise AutoReplyError("injected_transport_must_be_labeled")
    urllib_loopback_transport.facts.clear()
    generator = LoopbackGenerator(transport, transport_label=transport_label)
    probe = export_rendered_dom(page, timeout_seconds=5, probe_phrase=phrase)
    gate = _probe_gate(probe)
    if gate:
        return _base(gate, phrase_probe=probe.get("phrase_probe"), **transport_audit(generator))
    try:
        reply = generator.generate_greeting()
    except AutoReplyError as exc:
        return _base(exc.code, **transport_audit(generator))
    if not greeting_allowed(reply) or phrase in reply:
        return _base("model_output_rejected", **transport_audit(generator))
    again = export_rendered_dom(page, timeout_seconds=5, probe_phrase=phrase)
    if _probe_gate(again):
        return _base("refused_page_changed_before_input", **transport_audit(generator))
    _mark_used(data_dir, "committing")
    try:
        acted = page.evaluate(_ACT_JS, {"phrase": phrase, "reply": reply})
    except Exception as exc:
        (data_dir / _STOP).write_text("paused\n", encoding="utf-8")
        hint = str(exc).replace(phrase, "")[:180]
        return _base("evaluate_failed", clicked=False, error_type=type(exc).__name__, error_hint=hint, **transport_audit(generator))
    if not isinstance(acted, dict):
        (data_dir / _STOP).write_text("paused\n", encoding="utf-8")
        return _base("paused_uncertain", **transport_audit(generator))
    status = str(acted.get("status") or "paused_uncertain")
    if status != "test_sent":
        (data_dir / _STOP).write_text("paused\n", encoding="utf-8")
        _mark_used(data_dir, "paused_uncertain")
        return _base(
            "paused_uncertain" if acted.get("clicked") else status,
            clicked=acted.get("clicked") is True,
            receipt=acted.get("receipt") if acted.get("receipt") in {"matched", "absent"} else "absent",
            model=VERIFIED_MODEL,
            **transport_audit(generator),
        )
    _mark_used(data_dir, "test_sent")
    report = _base(
        "test_sent",
        clicked=True,
        receipt="matched",
        model=VERIFIED_MODEL,
        reply_kind="greeting_confirmation",
        **transport_audit(generator),
    )
    if phrase in json.dumps(report, ensure_ascii=False):
        return _base("phrase_leaked")
    return report
