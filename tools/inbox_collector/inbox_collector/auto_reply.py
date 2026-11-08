"""Optional local auto-reply. Production sending stays closed.

The verified generator contract is the public Ollama loopback on the machine
running this process: GET/POST http://127.0.0.1:11434, model qwen-kefu:latest.
A unit test injects a transport and does not contact the operator's Mac.
A successful model response is not customer-service quality verification and
is not a buyer send. Real mapping, login, official composer controls, and
account binding are still required before any production send, and no config
switch can mark those true.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from inbox_collector.store import Store

OLLAMA_ORIGIN = "http://127.0.0.1:11434"
VERIFIED_MODEL = "qwen-kefu:latest"
VERIFIED_FAMILY = "qwen2"
VERIFIED_SIZE = 1117321521
NUM_PREDICT = 24
GENERATE_TIMEOUT_SECONDS = 45
SIM_MARK = "【模拟】"

_MANUAL = ("退款", "退货", "改价", "补偿", "赔付", "赔偿", "仅退款")
_NO_MATERIAL = ("价格", "多少钱", "物流", "快递", "安装", "售后", "规格", "尺寸", "发货")
_ORDINARY = ("你好", "在吗", "咨询")
_PRODUCTION_BLOCKERS = (
    "mapping_not_validated",
    "login_not_verified",
    "official_composer_not_verified",
    "account_binding_not_verified",
    "conversation_binding_not_verified",
    "generator_quality_not_verified",
)

_READ_JS = """
() => {
  const root = document.documentElement;
  if (!root || root.getAttribute("data-simulated-inbox") !== "true") {
    return { ok: false, reason: "not_simulated_page" };
  }
  const body = document.body;
  const box = document.querySelector(".content-box");
  const composer = document.querySelector("#sim-composer");
  const button = document.querySelector("#sim-send");
  const messages = [];
  if (box) {
    for (const row of Array.from(box.querySelectorAll("li.onemsg"))) {
      const content = row.querySelector(".msg-content-box, .msg-content");
      const text = content ? String(content.textContent || "").replace(/\\s+/g, " ").trim() : "";
      messages.push({
        id: row.getAttribute("id") || "",
        direction: row.classList.contains("sim-outgoing")
          ? "merchant"
          : (row.getAttribute("data-sim-direction") || ""),
        body: text,
        hidden: row.hidden === true,
        read_mark: !!row.querySelector(".isread"),
        human_reply: row.getAttribute("data-sim-reply") || "",
        receipt: row.getAttribute("data-receipt") || ""
      });
    }
  }
  return {
    ok: true,
    simulated: true,
    page_state: body ? (body.getAttribute("data-page-state") || "") : "",
    open_conversation: body ? (body.getAttribute("data-open-conversation") || "") : "",
    box_conversation: box ? (box.getAttribute("data-sim-conversation") || "") : "",
    composer_conversation: composer ? (composer.getAttribute("data-sim-conversation") || "") : "",
    has_composer: !!composer,
    has_button: !!button && button.getAttribute("type") === "button",
    messages
  };
}
"""

_RECEIPT_JS = """
(expected) => {
  const nodes = Array.from(document.querySelectorAll(".sim-outgoing[data-receipt='sent']"));
  return nodes.some((node) => String(node.textContent || "").trim() === expected);
}
"""


class AutoReplyError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def production_blockers() -> list[str]:
    """These stay until a future verifier proves them. Config cannot clear them."""
    return list(_PRODUCTION_BLOCKERS)


def gate_report(raw: dict[str, Any] | None = None) -> dict[str, Any]:
    block = raw.get("auto_reply") if isinstance(raw, dict) and isinstance(raw.get("auto_reply"), dict) else {}
    enabled = block.get("enabled") is True
    scope = str(block.get("scope") or "disabled")
    if scope not in {"disabled", "local_simulation"}:
        scope = "refused"
    return {
        "enabled": enabled,
        "scope": scope,
        "production_send_allowed": False,
        "local_simulation_configured": enabled and scope == "local_simulation",
        "blockers": production_blockers(),
        "switch_bypasses_gates": False,
        "mac_ollama_contacted": False,
        "customer_service_quality_verified": False,
        "routine_is_sync_generator": False,
        "real_buyer_send": False,
    }


def require_loopback(url: str) -> str:
    parts = urlsplit(url)
    if (
        parts.scheme != "http"
        or parts.hostname != "127.0.0.1"
        or parts.port != 11434
        or parts.username
        or parts.password
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        raise AutoReplyError("ollama_origin_refused")
    return OLLAMA_ORIGIN


def buyer_policy(text: str) -> str:
    if any(term in text for term in _MANUAL):
        return "held_manual"
    if any(term in text for term in _NO_MATERIAL):
        return "held_no_material"
    if any(term in text for term in _ORDINARY):
        return "ordinary"
    return "held_unclassified"


def output_allowed(text: str) -> bool:
    return not any(term in text for term in _MANUAL + _NO_MATERIAL)


def present_simulated(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith(SIM_MARK):
        return cleaned
    return SIM_MARK + cleaned


def _normalize(text: str) -> str:
    return " ".join(text.split())


def dedupe_key(conversation: str, text: str) -> str:
    material = conversation + "\n" + _normalize(text)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class LoopbackGenerator:
    """Talks only to 127.0.0.1:11434. That is this process's loopback, not a Mac claim."""

    def __init__(
        self,
        transport: Callable[[str, str, dict[str, Any] | None, int], tuple[int, dict[str, Any]]],
        *,
        base_url: str = OLLAMA_ORIGIN,
        transport_label: str = "loopback_11434",
    ) -> None:
        self.base_url = require_loopback(base_url)
        self.transport = transport
        self.transport_label = transport_label
        self.mac_ollama_contacted = False
        self.customer_service_quality_verified = False
        self.paths: list[str] = []

    def _request(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        if path not in {"/api/tags", "/api/generate"}:
            raise AutoReplyError("ollama_path_refused")
        self.paths.append(path)
        status, payload = self.transport(method, path, body, GENERATE_TIMEOUT_SECONDS)
        if status != 200 or not isinstance(payload, dict):
            raise AutoReplyError("ollama_http_failed")
        return payload

    def assert_verified_model(self) -> None:
        payload = self._request("GET", "/api/tags", None)
        models = payload.get("models")
        if not isinstance(models, list):
            raise AutoReplyError("ollama_tags_invalid")
        match = next(
            (item for item in models if isinstance(item, dict) and item.get("name") == VERIFIED_MODEL),
            None,
        )
        if match is None:
            raise AutoReplyError("ollama_model_missing")
        if match.get("size") not in {None, VERIFIED_SIZE}:
            raise AutoReplyError("ollama_model_size_mismatch")
        details = match.get("details") if isinstance(match.get("details"), dict) else {}
        if details.get("family") not in {None, VERIFIED_FAMILY}:
            raise AutoReplyError("ollama_model_family_mismatch")

    def generate(self, buyer_text: str) -> str:
        self.assert_verified_model()
        prompt = (
            "你是本地模拟客服。只回答普通问候。"
            "不要承诺价格、物流、规格、安装、售后、退款、改价或补偿。"
            "回答不超过一句。买家消息：" + buyer_text
        )
        payload = self._request(
            "POST",
            "/api/generate",
            {
                "model": VERIFIED_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"num_predict": NUM_PREDICT},
            },
        )
        if payload.get("done") is not True or payload.get("done_reason") != "stop":
            raise AutoReplyError("ollama_not_stopped")
        text = payload.get("response")
        if not isinstance(text, str) or not text.strip() or len(text.strip()) > 80 or not output_allowed(text):
            raise AutoReplyError("ollama_output_rejected")
        return text.strip()


def urllib_loopback_transport(
    method: str,
    path: str,
    body: dict[str, Any] | None,
    timeout: int,
) -> tuple[int, dict[str, Any]]:
    """Optional real loopback call. Tests must not use this; it is not the Mac."""
    require_loopback(OLLAMA_ORIGIN)
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = Request(
        OLLAMA_ORIGIN + path,
        data=data,
        method=method,
        headers={} if data is None else {"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read()
            status = int(response.status)
    except HTTPError as exc:
        exc.close()
        return int(exc.code), {}
    except (URLError, TimeoutError, json.JSONDecodeError):
        return 0, {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return status, {}
    return status, parsed if isinstance(parsed, dict) else {}


def _local_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme in {"http", "https"} and parts.hostname in {"127.0.0.1", "localhost"}


def _result(status: str, **extra: Any) -> dict[str, Any]:
    report = {
        "status": status,
        "simulated": True,
        "real_buyer_send": False,
        "production_send_allowed": False,
        "production_blockers": production_blockers(),
        "mac_ollama_contacted": False,
        "customer_service_quality_verified": False,
        "official_composer_verified": False,
        "switch_bypasses_gates": False,
        "routine_is_sync_generator": False,
        "buyer_body_retained": False,
        "composer_commits": 0,
    }
    report.update(extra)
    return report


def _read(page: Any) -> dict[str, Any]:
    raw = page.evaluate(_READ_JS)
    if not isinstance(raw, dict):
        raise AutoReplyError("sim_read_invalid")
    return raw


def _binding_problem(snapshot: dict[str, Any]) -> str | None:
    if snapshot.get("ok") is not True:
        return str(snapshot.get("reason") or "not_simulated_page")
    state = str(snapshot.get("page_state") or "")
    if state in {"login", "captcha", "stale"}:
        return "page_" + state
    if state != "inbox":
        return "page_not_inbox"
    open_conversation = str(snapshot.get("open_conversation") or "")
    box = str(snapshot.get("box_conversation") or "")
    composer = str(snapshot.get("composer_conversation") or "")
    if not open_conversation or open_conversation != box or open_conversation != composer:
        return "cross_conversation"
    if snapshot.get("has_composer") is not True or snapshot.get("has_button") is not True:
        return "sim_controls_missing"
    return None


def _already_replied(messages: list[dict[str, Any]], index: int) -> bool:
    current = messages[index]
    if str(current.get("human_reply") or "") in {"human", "platform_bot"}:
        return True
    for later in messages[index + 1 :]:
        if later.get("hidden") is True:
            continue
        if later.get("direction") == "merchant" or later.get("receipt") == "sent":
            return True
        if str(later.get("human_reply") or "") in {"human", "platform_bot"}:
            return True
    return False


def _rate_limited(store: Store, now: float, min_interval: float, max_per_hour: int) -> bool:
    start = store.get_meta("auto_reply_window_start")
    count = int(store.get_meta("auto_reply_window_count") or "0")
    last = store.get_meta("auto_reply_last_at")
    if start is None or now - float(start) >= 3600:
        return False
    if count >= max_per_hour:
        return True
    if last is not None and min_interval > 0 and now - float(last) < min_interval:
        return True
    return False


def _note_commit(store: Store, now: float) -> None:
    start = store.get_meta("auto_reply_window_start")
    count = int(store.get_meta("auto_reply_window_count") or "0")
    if start is None or now - float(start) >= 3600:
        store.set_meta("auto_reply_window_start", str(now))
        count = 0
    store.set_meta("auto_reply_window_count", str(count + 1))
    store.set_meta("auto_reply_last_at", str(now))


def run_simulated_cycle(
    page: Any,
    store: Store,
    generator: LoopbackGenerator,
    *,
    min_interval: float = 60,
    max_per_hour: int = 3,
    now: float = 0,
    stopped: bool = False,
) -> dict[str, Any]:
    """One localhost simulated cycle. It never enables production sending."""
    if not _local_url(str(getattr(page, "url", ""))):
        return _result("refused_not_local", simulated_controls_used=False)
    if stopped or store.get_meta("auto_reply_stopped") == "1":
        return _result("refused_stop", simulated_controls_used=False)
    if _rate_limited(store, now, min_interval, max_per_hour):
        return _result("refused_rate_limit", simulated_controls_used=False)
    try:
        snapshot = _read(page)
    except Exception:
        return _result("refused_read_failed", simulated_controls_used=False)
    problem = _binding_problem(snapshot)
    if problem:
        return _result("refused_" + problem, simulated_controls_used=False)
    conversation = str(snapshot.get("open_conversation") or "")
    messages = [item for item in snapshot.get("messages") or [] if isinstance(item, dict)]
    visible = [item for item in messages if item.get("hidden") is not True]
    seen_text: set[str] = set()
    chosen: dict[str, Any] | None = None
    chosen_key = ""
    deduped = 0
    for index, item in enumerate(visible):
        if item.get("direction") != "buyer":
            continue
        body = _normalize(str(item.get("body") or ""))
        if not body or not str(item.get("id") or ""):
            continue
        key = dedupe_key(conversation, body)
        if store.get_reply_attempt(key) is not None:
            continue
        if key in seen_text:
            deduped += 1
            continue
        if _already_replied(visible, index):
            store.insert_reply_attempt(key, conversation, "held_already_replied", "later_reply_present")
            seen_text.add(key)
            continue
        decision = buyer_policy(body)
        if decision != "ordinary":
            store.insert_reply_attempt(key, conversation, decision, "policy")
            seen_text.add(key)
            continue
        if chosen is None:
            chosen = item
            chosen_key = key
            seen_text.add(key)
    if chosen is None:
        return _result(
            "no_eligible_buyer_message",
            simulated_controls_used=False,
            conversation_bound=True,
            deduped_same_text_count=deduped,
        )
    try:
        generated = generator.generate(str(chosen.get("body") or ""))
    except AutoReplyError as exc:
        return _result(
            exc.code,
            simulated_controls_used=False,
            generator_transport=generator.transport_label,
            deduped_same_text_count=deduped,
        )
    reply = present_simulated(generated)
    if not output_allowed(reply):
        store.insert_reply_attempt(chosen_key, conversation, "ollama_output_rejected", "policy")
        return _result(
            "ollama_output_rejected",
            generator_transport=generator.transport_label,
            deduped_same_text_count=deduped,
        )
    try:
        again = _read(page)
    except Exception:
        return _result(
            "refused_read_failed",
            generator_transport=generator.transport_label,
            deduped_same_text_count=deduped,
        )
    if _binding_problem(again) or str(again.get("open_conversation") or "") != conversation:
        return _result(
            "refused_conversation_changed",
            generator_transport=generator.transport_label,
            deduped_same_text_count=deduped,
        )
    if not store.insert_reply_attempt(chosen_key, conversation, "committing", "sim_only"):
        return _result(
            "already_recorded",
            generator_transport=generator.transport_label,
            deduped_same_text_count=deduped,
        )
    page.locator("#sim-composer").fill(reply)
    page.locator("#sim-send").click()
    try:
        matched = page.evaluate(_RECEIPT_JS, reply) is True
    except Exception:
        matched = False
    if not matched:
        store.update_reply_attempt(chosen_key, "paused_uncertain", "receipt_missing")
        store.set_meta("auto_reply_stopped", "1")
        return _result(
            "paused_uncertain",
            composer_commits=1,
            simulated_controls_used=True,
            generator_transport=generator.transport_label,
            model=VERIFIED_MODEL,
            num_predict=NUM_PREDICT,
            resend_allowed=False,
            deduped_same_text_count=deduped,
        )
    store.update_reply_attempt(chosen_key, "simulated_sent", "sim_receipt")
    _note_commit(store, now)
    return _result(
        "simulated_sent",
        composer_commits=1,
        simulated_controls_used=True,
        generator_transport=generator.transport_label,
        model=VERIFIED_MODEL,
        num_predict=NUM_PREDICT,
        resend_allowed=False,
        synthetic_reply=reply,
        deduped_same_text_count=deduped,
    )
