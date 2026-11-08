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
  const replyCount = (box, replyNorm) => Array.from(box.querySelectorAll("li.onemsg")).filter((row) => {
    if (hidden(row)) return false;
    const named = row.querySelector(".msg-content-box, .msg-content");
    return norm(named ? named.textContent : "") === replyNorm;
  }).length;
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

_ACT_JS = (
    "async (arg) => {\n"
    + _COMMON_JS
    + """
  const wanted = norm(arg && arg.challenge);
  const reply = String(arg && arg.reply || "");
  const replyNorm = norm(reply);
  const located = locate(wanted);
  const fail = (status) => ({ marker: "CONFIRMED_TEXT_ACT", status, clicked: false, mutated: false, match_count: located.matches.length });
  if (located.matches.length === 0) return fail("challenge_absent");
  if (located.matches.length !== 1 || !located.row || !located.box) return fail("challenge_not_unique");
  const fingerprint = [part(located.box), part(located.row), part(located.composer)].join("|");
  if (fingerprint !== String(arg && arg.fingerprint || "")) return fail("panel_changed");
  const rawId = usableId(located.row.getAttribute && located.row.getAttribute("id"));
  if (rawId !== String(arg && arg.row_id || "")) return fail("panel_changed");
  if (!located.field || located.inputs.length !== 1) return fail("input_not_verified");
  if (String(located.field.value || "") !== "") return fail("draft_present");
  if (located.senders.length !== 1) return fail("send_control_not_verified");
  if (replyCount(located.box, replyNorm) !== 0) return fail("reply_already_visible");
  const before = replyCount(located.box, replyNorm);
  located.field.value = reply;
  located.field.dispatchEvent(new Event("input", { bubbles: true }));
  located.senders[0].click();
  const seen = () => replyCount(located.box, replyNorm) > before;
  for (let attempt = 0; attempt < 4; attempt += 1) {
    if (seen()) return { marker: "CONFIRMED_TEXT_ACT", status: "page_visible", clicked: true, mutated: true, match_count: 1, receipt: "page_visible" };
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  return { marker: "CONFIRMED_TEXT_ACT", status: "paused_uncertain", clicked: true, mutated: true, match_count: 1, receipt: "absent" };
}
"""
)


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
    if not window_id:
        return "window_unbound", None
    if payload.get("window_id") != window_id:
        return "window_mismatch", None
    expires_at = payload.get("expires_at")
    if not isinstance(expires_at, (int, float)) or float(expires_at) < now:
        return "authorization_expired", None
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


def run_confirmed_text(
    page: Any,
    data_dir: Path,
    challenge: str,
    *,
    window_id: str | None,
    now: float | None = None,
) -> dict[str, Any]:
    """Type one authorized sentence, or refuse before touching the composer."""
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
    try:
        acted = page.evaluate(
            _ACT_JS,
            {
                "challenge": challenge,
                "reply": reply,
                "fingerprint": raw.get("panel_fingerprint"),
                "row_id": str(raw.get("row_id_raw") or ""),
            },
        )
    except Exception as exc:
        _write_ledger(data_dir, challenge=challenge, reply=reply, state="uncertain")
        return _report(
            "evaluate_failed",
            ledger_sealed=True,
            error_type=type(exc).__name__,
            **context,
        )
    if not isinstance(acted, dict) or acted.get("marker") != "CONFIRMED_TEXT_ACT":
        _write_ledger(data_dir, challenge=challenge, reply=reply, state="uncertain")
        return _report("paused_uncertain", ledger_sealed=True, receipt="absent", **context)
    status = str(acted.get("status") or "paused_uncertain")
    clicked = acted.get("clicked") is True
    mutated = acted.get("mutated") is True
    if status == "page_visible" and clicked and mutated:
        _write_ledger(data_dir, challenge=challenge, reply=reply, state="page_visible")
        report = _report(
            "page_visible",
            error=None,
            clicked=True,
            mutated=True,
            receipt="page_visible",
            ledger_sealed=True,
            **context,
        )
        return _leak_guard(report, challenge, reply)
    final = "paused_uncertain" if clicked or mutated else status
    _write_ledger(data_dir, challenge=challenge, reply=reply, state="uncertain" if clicked or mutated else "revoked")
    report = _report(
        final,
        clicked=clicked,
        mutated=mutated,
        receipt="absent" if clicked else "not_sent",
        ledger_sealed=True,
        **context,
    )
    return _leak_guard(report, challenge, reply)


def _leak_guard(report: dict[str, Any], challenge: str, reply: str) -> dict[str, Any]:
    blob = json.dumps(report, ensure_ascii=False)
    if (challenge and challenge in blob) or (reply and reply in blob):
        return _report("phrase_leaked", ledger_sealed=report.get("ledger_sealed") is True)
    return report
