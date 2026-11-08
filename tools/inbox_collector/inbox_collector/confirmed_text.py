"""One operator-confirmed fixed reply for the already-open test conversation.

The model is not called. The reply text is the authorization file's exact
string. Highlighted list rows are not identity. A rendered context marker is not read.
A sealed ledger is never retried.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

_AUTH = "confirmed-text.auth.json"
_LEDGER = "confirmed-text.ledger.json"
_WINDOW = "login-prep.window.json"
_SCOPE = "single_confirmed_text"
_BUYER = "operator_confirmed_own_account"
_MAX_REPLY = 40
_FORBIDDEN = ("价格", "物流", "规格", "安装", "售后", "退款", "改价", "补偿", "赔")

_COMMON_JS = """
  const norm = (value) => String(value || "").replace(/\\s+/g, "");
  const hidden = (el) => {
    if (!el || el.nodeType !== 1) return true;
    if (el.hidden || el.getAttribute("aria-hidden") === "true") return true;
    const view = el.ownerDocument && el.ownerDocument.defaultView;
    if (!view) return false;
    const style = view.getComputedStyle(el);
    return style.display === "none" || style.visibility === "hidden";
  };
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
  const rowHas = (row, wanted) => {
    const named = row.querySelector(".msg-content-box, .msg-content");
    if (named && norm(named.textContent) === wanted) return true;
    const nodes = Array.from(row.querySelectorAll("div, span, p")).slice(0, 48);
    return nodes.some((node) => norm(node.textContent) === wanted);
  };
  const matchesOf = (wanted) => {
    const found = [];
    for (const box of Array.from(document.querySelectorAll(".content-box"))) {
      for (const row of Array.from(box.querySelectorAll("li.onemsg"))) {
        if (!hidden(row) && rowHas(row, wanted)) found.push(row);
      }
    }
    return found;
  };
  const ownText = (el) => {
    let text = "";
    for (const child of Array.from(el.childNodes || [])) {
      if (child.nodeType === 3) text += child.nodeValue || "";
    }
    return norm(text);
  };
  const replyHits = (box, replyNorm) => {
    if (!box || !replyNorm) return { count: 0, truncated: false };
    const nodes = Array.from(box.querySelectorAll("li.onemsg, .msg-content, .msg-content-box, div, span, p"));
    const truncated = nodes.length > 80;
    let count = 0;
    for (const node of nodes.slice(0, 80)) {
      if (hidden(node)) continue;
      if (node.closest && node.closest("textarea, input, button")) continue;
      if (node.querySelector && node.querySelector("textarea, input, button")) continue;
      if (norm(node.textContent) !== replyNorm) continue;
      let parent = node.parentElement;
      let covered = false;
      while (parent && parent !== box) {
        if (!hidden(parent) && norm(parent.textContent) === replyNorm) { covered = true; break; }
        parent = parent.parentElement;
      }
      if (!covered) count += 1;
    }
    return { count, truncated };
  };
  const replyCount = (box, replyNorm) => replyHits(box, replyNorm).count;
  const safeTokens = (el) => String(el && el.getAttribute && el.getAttribute("class") || "").split(/\\s+/).filter((token) => token !== "active" && /^[A-Za-z_][A-Za-z0-9_-]{0,48}$/.test(token)).sort().slice(0, 6);
  const part = (el) => (el ? String(el.tagName || "").toLowerCase() : "none") + "." + safeTokens(el).join(".");
  const usableId = (raw) => {
    const value = String(raw || "");
    if (!value || /^\\d{1,6}$/.test(value) || value.length > 128) return "";
    return value;
  };
  const locate = (wanted) => {
    const matches = matchesOf(wanted);
    if (matches.length !== 1) return { matches, row: null, box: null, field: null, composer: null, senders: [] };
    const row = matches[0];
    const box = row.closest(".content-box");
    const inputs = Array.from(box.querySelectorAll("textarea")).filter((el) => !hidden(el) && !el.disabled && !el.readOnly);
    const field = inputs.length === 1 ? inputs[0] : null;
    const containsRows = (el) => !!(el && el.querySelector && el.querySelector("li.onemsg"));
    let composer = null;
    const senders = [];
    if (field) {
      const chain = [field];
      let cursor = field;
      for (let step = 0; step < 8; step += 1) {
        const parent = cursor.parentElement;
        if (!parent || containsRows(parent)) break;
        chain.push(parent);
        cursor = parent;
      }
      composer = chain[chain.length - 1];
      const seeds = [composer];
      const above = composer.parentElement;
      if (above && containsRows(above)) {
        for (const sib of Array.from(above.children || [])) {
          if (sib !== composer && !containsRows(sib)) seeds.push(sib);
        }
      }
      const visited = new Set();
      const walk = (el, depth) => {
        if (!el || el.nodeType !== 1 || visited.has(el) || depth > 6 || senders.length > 4) return;
        if (containsRows(el) && el !== composer) return;
        visited.add(el);
        const tag = String(el.tagName || "").toLowerCase();
        const role = String(el.getAttribute && el.getAttribute("role") || "");
        const type = String(el.getAttribute && el.getAttribute("type") || "");
        const semantic = tag === "button" || tag === "a" || role === "button" || type === "button" || type === "submit";
        const kind = labelKind(el);
        if (el !== field && !hidden(el) && !el.disabled && semantic && (kind === "send" || kind === "send_with_shortcut")) senders.push(el);
        for (const child of Array.from(el.children || [])) walk(child, depth + 1);
      };
      seeds.forEach((seed, index) => walk(seed, index === 0 ? 0 : 1));
    }
    return { matches, row, box, field, composer, senders, inputs };
  };
"""

_READ_JS = (
    "async (arg) => {\n"
    + _COMMON_JS
    + """
  const wanted = norm(arg && arg.challenge);
  const replyNorm = norm(arg && arg.reply);
  const highlights = Array.from(document.querySelectorAll(".active")).filter((el) => !hidden(el));
  const located = locate(wanted);
  const base = {
    marker: "CONFIRMED_TEXT_READ",
    match_count: located.matches.length,
    highlight_count: highlights.length,
    input_count: 0,
    input_empty: null,
    sender_count: 0,
    reply_visible_count: 0,
    panel_fingerprint: "",
    row_id_raw: "",
    clicked: false,
    mutated: false
  };
  if (!located.row || !located.box) return base;
  base.input_count = located.inputs.length;
  base.sender_count = located.senders.length;
  base.reply_visible_count = replyCount(located.box, replyNorm);
  base.panel_fingerprint = [part(located.box), part(located.row), part(located.composer)].join("|");
  base.row_id_raw = usableId(located.row.getAttribute && located.row.getAttribute("id"));
  if (located.field) base.input_empty = String(located.field.value || "") === "";
  return base;
}
"""
)

_OBSERVE_JS = (
    "async (arg) => {\n"
    + _COMMON_JS
    + """
  const wanted = norm(arg && arg.challenge);
  const replyNorm = norm(arg && arg.reply);
  const located = locate(wanted);
  const flags = [];
  let visibleCaptcha = false;
  let visibleError = false;
  const scope = located.box || null;
  if (scope) {
    for (const node of Array.from(scope.querySelectorAll("*")).slice(0, 40)) {
      if (hidden(node)) continue;
      const tokens = safeTokens(node);
      const leaf = ownText(node);
      const captcha = tokens.some((token) => /captcha|geetest/i.test(token)) || leaf === "验证码";
      const error = tokens.some((token) => /^(?:error|is-error|alert)$/i.test(token)) || leaf === "发送失败" || leaf === "网络异常";
      if (captcha) visibleCaptcha = true;
      if (error) visibleError = true;
      if ((captcha || error) && flags.length < 3) flags.push(...tokens.slice(0, 2));
    }
  }
  const sender = located.senders.length === 1 ? located.senders[0] : null;
  let inputNonempty = null;
  let inputEquals = null;
  if (located.field && located.inputs && located.inputs.length === 1) {
    const value = String(located.field.value || "");
    inputNonempty = value !== "";
    inputEquals = value !== "" && norm(value) === replyNorm;
  }
  const hits = scope ? replyHits(scope, replyNorm) : { count: 0, truncated: false };
  return {
    marker: "INPUT_STATE_READONLY",
    match_count: located.matches.length,
    input_count: located.inputs ? located.inputs.length : 0,
    input_nonempty: inputNonempty,
    input_equals_reply: inputEquals,
    sender_count: located.senders.length,
    sender: sender ? {
      tag: String(sender.tagName || "").toLowerCase(),
      classes: safeTokens(sender),
      visible: !hidden(sender),
      disabled: sender.disabled === true,
      label_kind: labelKind(sender)
    } : null,
    visible_captcha: visibleCaptcha,
    visible_error: visibleError,
    flag_classes: flags.slice(0, 4),
    reply_exact_count: hits.count,
    reply_scan_truncated: hits.truncated,
    panel_fingerprint: located.row && located.box ? [part(located.box), part(located.row), part(located.composer)].join("|") : "",
    row_id_raw: located.row ? usableId(located.row.getAttribute && located.row.getAttribute("id")) : "",
    values_included: false
  };
}
"""
)

_HANDLES_JS = (
    "async (arg) => {\n"
    + _COMMON_JS
    + """
  const wanted = norm(arg && arg.challenge);
  const located = locate(wanted);
  const fail = (status) => ({ ok: false, status, field: null, sender: null });
  if (located.matches.length !== 1 || !located.row || !located.box || !located.field) return fail(located.matches.length === 0 ? "challenge_absent" : "challenge_not_unique");
  const fingerprint = [part(located.box), part(located.row), part(located.composer)].join("|");
  if (fingerprint !== String(arg && arg.fingerprint || "")) return fail("panel_changed");
  if (usableId(located.row.getAttribute && located.row.getAttribute("id")) !== String(arg && arg.row_id || "")) return fail("panel_changed");
  if (!located.inputs || located.inputs.length !== 1) return fail("input_not_verified");
  if (String(located.field.value || "") !== "") return fail("draft_present");
  if (located.senders.length !== 1) return fail("send_control_not_verified");
  if (replyCount(located.box, norm(arg && arg.reply)) !== 0) return fail("reply_already_visible");
  return { ok: true, status: "ready", field: located.field, sender: located.senders[0] };
}
"""
)

RECEIPT_WAIT_SECONDS = 5.0
RECEIPT_POLL_SECONDS = 0.4


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def publish_window(data_dir: Path) -> str:
    data_dir.mkdir(parents=True, exist_ok=True)
    window_id = secrets.token_hex(16)
    path = data_dir / _WINDOW
    path.write_text(
        json.dumps({"window_id": window_id, "kind": "process_local", "durable": False}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    return window_id


def clear_window(data_dir: Path) -> None:
    try:
        (data_dir / _WINDOW).unlink()
    except OSError:
        return


def read_window_id(data_dir: Path) -> str | None:
    path = data_dir / _WINDOW
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    window_id = payload.get("window_id") if isinstance(payload, dict) else None
    if not isinstance(window_id, str) or re.fullmatch(r"[0-9a-f]{32}", window_id) is None:
        return None
    return window_id


def reply_problem(reply: str) -> str | None:
    if not isinstance(reply, str) or reply == "" or reply != reply.strip() or "\n" in reply or len(reply) > _MAX_REPLY:
        return "reply_rejected"
    if any(term in reply for term in _FORBIDDEN):
        return "reply_rejected"
    return None


def write_authorization(
    data_dir: Path,
    *,
    challenge: str,
    reply: str,
    window_id: str,
    expires_at: float,
) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "scope": _SCOPE,
        "buyer_source": _BUYER,
        "challenge_sha256": digest(challenge),
        "reply_sha256": digest(reply),
        "reply_text": reply,
        "window_id": window_id,
        "expires_at": expires_at,
        "durable": False,
    }
    path = data_dir / _AUTH
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _stable_id_hash(value: str) -> str | None:
    if not value or re.fullmatch(r"\d{1,6}", value) or len(value) > 128:
        return None
    return digest(value)


def _report(status: str, **extra: Any) -> dict[str, Any]:
    report = {
        "handler": "confirmed-text",
        "op": "confirmed-text",
        "status": status,
        "error": extra.pop("error", None if status == "page_visible" else status),
        "clicked": False,
        "mutated": False,
        "match_count": 0,
        "receipt": "not_sent",
        "delivery_claimed": False,
        "delivered_to_buyer": False,
        "production_send_allowed": False,
        "production_mapping_validated": False,
        "storewide_auto_reply": False,
        "identity_from_highlight": False,
        "role_required": False,
        "currentuid_used": False,
        "model_called": False,
        "retry_allowed": False,
        "ledger_sealed": False,
        "values_included": False,
        "resend_allowed": False,
        "pairing": "operator_confirmed_panel",
        "durable": False,
        "input_attempted": False,
        "click_attempted": False,
        "input_method": None,
        "click_method": None,
        "buyer_receipt": "unproven",
        "platform_send_proven": False,
        "cause_unproven": True,
    }
    report.update(extra)
    return report


def _ledger_state(data_dir: Path) -> str | None:
    path = data_dir / _LEDGER
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "unreadable"
    if not isinstance(payload, dict) or payload.get("sealed") is not True:
        return "unreadable"
    return str(payload.get("state") or "sealed")


def _write_ledger(data_dir: Path, *, challenge: str, reply: str, state: str) -> None:
    payload = {
        "sealed": True,
        "state": state,
        "retry_allowed": False,
        "challenge_sha256": digest(challenge),
        "reply_sha256": digest(reply),
        "durable": False,
    }
    path = data_dir / _LEDGER
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _load_auth(
    data_dir: Path,
    challenge: str,
    window_id: str | None,
    now: float,
    *,
    allow_stale_window: bool = False,
) -> tuple[str | None, dict[str, Any] | None]:
    path = data_dir / _AUTH
    if not path.is_file():
        return "authorization_missing", None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "authorization_unreadable", None
    if not isinstance(payload, dict) or payload.get("scope") != _SCOPE:
        return "authorization_mismatch", None
    if payload.get("buyer_source") != _BUYER:
        return "buyer_source_unconfirmed", None
    if not allow_stale_window and not window_id:
        return "window_unbound", None
    if not allow_stale_window and payload.get("window_id") != window_id:
        return "window_mismatch", None
    expires_at = payload.get("expires_at")
    if not allow_stale_window and (not isinstance(expires_at, (int, float)) or float(expires_at) < now):
        return "authorization_expired", None
    if allow_stale_window and not isinstance(expires_at, (int, float)):
        return "authorization_unreadable", None
    reply = payload.get("reply_text")
    if not isinstance(reply, str) or digest(reply) != payload.get("reply_sha256") or digest(challenge) != payload.get("challenge_sha256"):
        return "authorization_mismatch", None
    problem = reply_problem(reply)
    if problem:
        return problem, None
    return None, payload


def _read_gate(raw: dict[str, Any]) -> str | None:
    count = int(raw.get("match_count") or 0)
    if count == 0:
        return "challenge_absent"
    if count != 1:
        return "challenge_not_unique"
    if int(raw.get("reply_visible_count") or 0) != 0:
        return "reply_already_visible"
    if int(raw.get("input_count") or 0) != 1 or raw.get("input_empty") is not True:
        if raw.get("input_empty") is False:
            return "draft_present"
        return "input_not_verified"
    if int(raw.get("sender_count") or 0) != 1:
        return "send_control_not_verified"
    if not isinstance(raw.get("panel_fingerprint"), str) or not raw.get("panel_fingerprint"):
        return "panel_unbound"
    return None


def _clean_state(raw: Any, *, before: int | None = None) -> dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    sender = source.get("sender") if isinstance(source.get("sender"), dict) else None
    after = source.get("reply_exact_count")
    after_count = int(after) if isinstance(after, int) else 0
    appeared = None if before is None else after_count > before
    return {
        "match_count": int(source.get("match_count") or 0),
        "input_nonempty": source.get("input_nonempty") if isinstance(source.get("input_nonempty"), bool) else None,
        "input_equals_reply": source.get("input_equals_reply") if isinstance(source.get("input_equals_reply"), bool) else None,
        "sender_count": int(source.get("sender_count") or 0),
        "sender": None
        if sender is None
        else {
            "tag": sender.get("tag") if isinstance(sender.get("tag"), str) else None,
            "classes": [token for token in sender.get("classes") or [] if isinstance(token, str)][:6],
            "visible": sender.get("visible") is True,
            "disabled": sender.get("disabled") is True,
            "label_kind": sender.get("label_kind") if sender.get("label_kind") in {"send", "send_with_shortcut", "empty", "other_redacted", "not_a_leaf_control"} else None,
        },
        "visible_captcha": source.get("visible_captcha") is True,
        "visible_error": source.get("visible_error") is True,
        "flag_classes": [token for token in source.get("flag_classes") or [] if isinstance(token, str)][:4],
        "reply_exact_count": after_count,
        "reply_before_count": before,
        "reply_appeared": appeared,
        "reply_age": "increased_after_action" if appeared is True else "unchanged_this_action" if appeared is False else "unproven_single_snapshot",
        "reply_scan_truncated": source.get("reply_scan_truncated") is True,
        "values_included": False,
    }


def _observe(page: Any, challenge: str, reply: str) -> dict[str, Any]:
    raw = page.evaluate(_OBSERVE_JS, {"challenge": challenge, "reply": reply})
    if not isinstance(raw, dict) or raw.get("marker") != "INPUT_STATE_READONLY":
        return _clean_state({})
    return _clean_state(raw)


def run_input_state(
    page: Any,
    data_dir: Path,
    challenge: str,
    *,
    window_id: str | None,
    now: float | None = None,
) -> dict[str, Any]:
    """Read booleans for the authorized reply. Does not type, click, or rewrite the ledger."""
    moment = time.time() if now is None else now
    before = _auth_bytes(data_dir)
    ledger_before = _ledger_bytes(data_dir)
    problem, auth = _load_auth(data_dir, challenge, window_id, moment, allow_stale_window=True)
    sealed = _ledger_state(data_dir)
    if problem or auth is None:
        report = _report(problem or "authorization_missing", handler="input-state-readonly", op="input-state-readonly")
        report["ledger_sealed"] = sealed is not None
        report["retry_allowed"] = False
        return report
    reply = str(auth["reply_text"])
    try:
        raw = page.evaluate(_OBSERVE_JS, {"challenge": challenge, "reply": reply})
    except Exception as exc:
        return _report("evaluate_failed", handler="input-state-readonly", op="input-state-readonly", error_type=type(exc).__name__, ledger_sealed=sealed is not None)
    if not isinstance(raw, dict) or raw.get("marker") != "INPUT_STATE_READONLY":
        return _report("evaluate_invalid", handler="input-state-readonly", op="input-state-readonly", ledger_sealed=sealed is not None)
    state = _clean_state(raw)
    status = "input_state_ready" if int(raw.get("match_count") or 0) == 1 else "challenge_absent" if int(raw.get("match_count") or 0) == 0 else "challenge_not_unique"
    report = _report(
        status,
        error=None if status == "input_state_ready" else status,
        handler="input-state-readonly",
        op="input-state-readonly",
        match_count=state["match_count"],
        ledger_sealed=sealed is not None,
        input_state=state,
        window_matches=auth.get("window_id") == window_id,
        authorization_expired=float(auth.get("expires_at") or 0) < moment,
        files_unchanged=_auth_bytes(data_dir) == before and _ledger_bytes(data_dir) == ledger_before,
    )
    return _leak_guard(report, challenge, reply)


def _auth_bytes(data_dir: Path) -> bytes | None:
    path = data_dir / _AUTH
    try:
        return path.read_bytes()
    except OSError:
        return None


def _ledger_bytes(data_dir: Path) -> bytes | None:
    path = data_dir / _LEDGER
    try:
        return path.read_bytes()
    except OSError:
        return None


def run_confirmed_text(
    page: Any,
    data_dir: Path,
    challenge: str,
    *,
    window_id: str | None,
    now: float | None = None,
    receipt_wait_seconds: float = RECEIPT_WAIT_SECONDS,
) -> dict[str, Any]:
    """Type one authorized sentence with Playwright, or refuse before touching the composer."""
    moment = time.time() if now is None else now
    sealed = _ledger_state(data_dir)
    if sealed is not None:
        return _report("ledger_closed", ledger_sealed=True, error="ledger_closed")
    problem, auth = _load_auth(data_dir, challenge, window_id, moment)
    if problem or auth is None:
        return _report(problem or "authorization_missing")
    reply = str(auth["reply_text"])
    try:
        raw = page.evaluate(_READ_JS, {"challenge": challenge, "reply": reply})
    except Exception as exc:
        return _report("evaluate_failed", error_type=type(exc).__name__)
    if not isinstance(raw, dict) or raw.get("marker") != "CONFIRMED_TEXT_READ":
        return _report("evaluate_invalid")
    gate = _read_gate(raw)
    row_hash = _stable_id_hash(str(raw.get("row_id_raw") or ""))
    context = {
        "match_count": int(raw.get("match_count") or 0),
        "highlight_count": int(raw.get("highlight_count") or 0),
        "row_id_sha256": row_hash,
        "row_id_is_platform_uid": False,
    }
    if gate:
        report = _report(gate, **context)
        return _leak_guard(report, challenge, reply)
    _write_ledger(data_dir, challenge=challenge, reply=reply, state="committed")
    action_arg = {
        "challenge": challenge,
        "reply": reply,
        "fingerprint": raw.get("panel_fingerprint"),
        "row_id": str(raw.get("row_id_raw") or ""),
    }
    try:
        recheck = page.evaluate(_OBSERVE_JS, action_arg)
    except Exception as exc:
        _write_ledger(data_dir, challenge=challenge, reply=reply, state="uncertain")
        return _report("evaluate_failed", ledger_sealed=True, error_type=type(exc).__name__, **context)
    if not isinstance(recheck, dict) or recheck.get("marker") != "INPUT_STATE_READONLY":
        _write_ledger(data_dir, challenge=challenge, reply=reply, state="uncertain")
        return _report("paused_uncertain", ledger_sealed=True, receipt="absent", **context)
    if recheck.get("panel_fingerprint") != raw.get("panel_fingerprint") or str(recheck.get("row_id_raw") or "") != str(raw.get("row_id_raw") or ""):
        _write_ledger(data_dir, challenge=challenge, reply=reply, state="revoked")
        return _leak_guard(_report("panel_changed", ledger_sealed=True, input_state=_clean_state(recheck, before=int(recheck.get("reply_exact_count") or 0)), **context), challenge, reply)
    pre_gate = _read_gate({**raw, "input_empty": recheck.get("input_nonempty") is False, "input_count": recheck.get("input_count"), "sender_count": recheck.get("sender_count"), "reply_visible_count": recheck.get("reply_exact_count"), "panel_fingerprint": recheck.get("panel_fingerprint")})
    if pre_gate:
        _write_ledger(data_dir, challenge=challenge, reply=reply, state="revoked")
        return _leak_guard(_report(pre_gate, ledger_sealed=True, input_state=_clean_state(recheck), **context), challenge, reply)
    before_count = int(recheck.get("reply_exact_count") or 0)
    bundle = None
    input_attempted = False
    click_attempted = False
    try:
        bundle = page.evaluate_handle(_HANDLES_JS, action_arg)
        picked = bundle.evaluate("(item) => ({ ok: item.ok === true, status: item.status || 'unavailable' })")
        if not isinstance(picked, dict) or picked.get("ok") is not True:
            _write_ledger(data_dir, challenge=challenge, reply=reply, state="revoked")
            return _leak_guard(
                _report(str(picked.get("status") if isinstance(picked, dict) else "panel_changed"), ledger_sealed=True, input_state=_clean_state(recheck, before=before_count), **context),
                challenge,
                reply,
            )
        field = bundle.get_property("field").as_element()
        sender = bundle.get_property("sender").as_element()
        if field is None or sender is None:
            raise RuntimeError("control_handle_missing")
        sender_meta = sender.evaluate(
            """(el) => ({
              tag: String(el.tagName || '').toLowerCase(),
              classes: String(el.getAttribute('class') || '').split(/\\s+/).filter((token) => token && token !== 'active' && /^[A-Za-z_][A-Za-z0-9_-]{0,48}$/.test(token)).slice(0, 6)
            })"""
        )
        if field.input_value() != "":
            _write_ledger(data_dir, challenge=challenge, reply=reply, state="revoked")
            return _leak_guard(_report("draft_present", ledger_sealed=True, input_state=_clean_state(recheck, before=before_count), **context), challenge, reply)
        field.fill(reply)
        input_attempted = True
        accepted = page.evaluate(_OBSERVE_JS, action_arg)
        if not isinstance(accepted, dict) or accepted.get("input_equals_reply") is not True:
            _write_ledger(data_dir, challenge=challenge, reply=reply, state="uncertain")
            return _leak_guard(
                _report(
                    "input_not_accepted",
                    ledger_sealed=True,
                    input_attempted=True,
                    mutated=True,
                    input_method="playwright_fill",
                    input_state=_clean_state(accepted if isinstance(accepted, dict) else {}, before=before_count),
                    **context,
                ),
                challenge,
                reply,
            )
        sender.click()
        click_attempted = True
        deadline = time.monotonic() + receipt_wait_seconds
        latest = accepted
        while True:
            latest = page.evaluate(_OBSERVE_JS, action_arg)
            if isinstance(latest, dict) and int(latest.get("reply_exact_count") or 0) > before_count:
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(RECEIPT_POLL_SECONDS)
    except Exception as exc:
        _write_ledger(data_dir, challenge=challenge, reply=reply, state="uncertain")
        return _report(
            "evaluate_failed" if not input_attempted and not click_attempted else "paused_uncertain",
            ledger_sealed=True,
            input_attempted=input_attempted,
            click_attempted=click_attempted,
            mutated=input_attempted,
            clicked=click_attempted,
            input_method="playwright_fill" if input_attempted else None,
            click_method="playwright_click" if click_attempted else None,
            error_type=type(exc).__name__,
            receipt="absent" if input_attempted or click_attempted else "not_sent",
            **context,
        )
    finally:
        if bundle is not None:
            try:
                bundle.dispose()
            except Exception:
                pass
    appeared = isinstance(latest, dict) and int(latest.get("reply_exact_count") or 0) > before_count
    state = _clean_state(latest if isinstance(latest, dict) else {}, before=before_count)
    if appeared and input_attempted and click_attempted:
        _write_ledger(data_dir, challenge=challenge, reply=reply, state="page_visible")
        return _leak_guard(
            _report(
                "page_visible",
                error=None,
                clicked=True,
                mutated=True,
                input_attempted=True,
                click_attempted=True,
                input_method="playwright_fill",
                click_method="playwright_click",
                clicked_tag=sender_meta.get("tag") if isinstance(sender_meta, dict) else None,
                clicked_classes=sender_meta.get("classes") if isinstance(sender_meta, dict) else [],
                receipt="page_visible",
                ledger_sealed=True,
                input_state=state,
                receipt_wait_seconds=receipt_wait_seconds,
                **context,
            ),
            challenge,
            reply,
        )
    _write_ledger(data_dir, challenge=challenge, reply=reply, state="uncertain")
    retained = bool(state.get("input_equals_reply")) and not appeared
    return _leak_guard(
        _report(
            "paused_uncertain",
            clicked=click_attempted,
            mutated=input_attempted,
            input_attempted=input_attempted,
            click_attempted=click_attempted,
            input_method="playwright_fill" if input_attempted else None,
            click_method="playwright_click" if click_attempted else None,
            clicked_tag=sender_meta.get("tag") if isinstance(sender_meta, dict) else None,
            clicked_classes=sender_meta.get("classes") if isinstance(sender_meta, dict) else [],
            receipt="absent",
            ledger_sealed=True,
            input_state=state,
            appearance="input_retained_receipt_absent" if retained else "receipt_absent",
            receipt_wait_seconds=receipt_wait_seconds,
            **context,
        ),
        challenge,
        reply,
    )


def _leak_guard(report: dict[str, Any], challenge: str, reply: str) -> dict[str, Any]:
    blob = json.dumps(report, ensure_ascii=False)
    if (challenge and challenge in blob) or (reply and reply in blob):
        return _report("phrase_leaked", ledger_sealed=report.get("ledger_sealed") is True)
    return report
