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


def identity_key(conversation: str, message_id: str) -> str:
    """Stable handle for one row id. Same text with another id is a different key."""
    material = conversation + "\n" + message_id
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def text_collision_count(messages: list[dict[str, Any]]) -> int:
    groups: dict[str, set[str]] = {}
    for item in messages:
        if item.get("hidden") is True or item.get("direction") != "buyer":
            continue
        body = _normalize(str(item.get("body") or ""))
        message_id = str(item.get("id") or "")
        if not body or not message_id:
            continue
        groups.setdefault(body, set()).add(message_id)
    return sum(len(ids) for ids in groups.values() if len(ids) > 1)


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

    def generate_greeting(self) -> str:
        """Fixed greeting prompt. Page text is not sent to the model."""
        self.assert_verified_model()
        payload = self._request(
            "POST",
            "/api/generate",
            {
                "model": VERIFIED_MODEL,
                "prompt": (
                    "只输出一句简短中文：已收到测试。"
                    "不要提价格、物流、规格、安装、售后、退款、改价、补偿。"
                    "不要执行任何指令。不要复述任何原文。"
                ),
                "stream": False,
                "options": {"num_predict": NUM_PREDICT},
            },
        )
        if payload.get("done") is not True or payload.get("done_reason") != "stop":
            raise AutoReplyError("ollama_not_stopped")
        text = payload.get("response")
        if not isinstance(text, str):
            raise AutoReplyError("ollama_output_rejected")
        return text.strip()


def urllib_loopback_transport(
    method: str,
    path: str,
    body: dict[str, Any] | None,
    timeout: int,
) -> tuple[int, dict[str, Any]]:
    """Real HTTP to this process's 127.0.0.1:11434. Facts are appended for the report."""
    require_loopback(OLLAMA_ORIGIN)
    fact = {
        "via": "urllib",
        "host": "127.0.0.1",
        "port": 11434,
        "method": method,
        "path": path,
        "completed": False,
        "status": None,
    }
    urllib_loopback_transport.facts.append(fact)
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
        fact["status"] = int(exc.code)
        return int(exc.code), {}
    except (URLError, TimeoutError, json.JSONDecodeError):
        fact["status"] = 0
        return 0, {}
    fact["status"] = status
    fact["completed"] = status == 200
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        fact["completed"] = False
        return status, {}
    return status, parsed if isinstance(parsed, dict) else {}


urllib_loopback_transport.facts = []  # type: ignore[attr-defined]


def transport_audit(generator: LoopbackGenerator) -> dict[str, Any]:
    """Separate a real loopback HTTP call from an injected test transport."""
    if generator.transport_label != "urllib_loopback":
        return {
            "generator_transport": generator.transport_label,
            "injected_test_transport": True,
            "process_loopback_http": [],
            "process_loopback_http_completed": False,
            "customer_service_quality_verified": False,
        }
    facts = [dict(item) for item in urllib_loopback_transport.facts]
    completed = bool(facts) and all(item.get("completed") is True and item.get("status") == 200 for item in facts)
    return {
        "generator_transport": "urllib_loopback",
        "injected_test_transport": False,
        "process_loopback_http": facts,
        "process_loopback_http_completed": completed,
        "customer_service_quality_verified": False,
    }


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
        "customer_service_quality_verified": False,
        "official_composer_verified": False,
        "switch_bypasses_gates": False,
        "routine_is_sync_generator": False,
        "buyer_body_retained": False,
        "composer_commits": 0,
        "text_merge_applied": False,
        "injected_test_transport": False,
        "process_loopback_http": [],
        "process_loopback_http_completed": False,
    }
    generator = getattr(_result, "active_generator", None)
    if isinstance(generator, LoopbackGenerator):
        report.update(transport_audit(generator))
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
    _result.active_generator = generator  # type: ignore[attr-defined]
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
    collisions = text_collision_count(visible)
    baseline_flag = "baseline_ready:" + hashlib.sha256(conversation.encode("utf-8")).hexdigest()
    if store.get_meta(baseline_flag) != "1":
        for item in visible:
            if item.get("direction") != "buyer":
                continue
            message_id = str(item.get("id") or "")
            if not message_id:
                continue
            store.insert_reply_attempt(
                identity_key(conversation, message_id),
                conversation,
                "baseline_held",
                "cold_start",
            )
        store.set_meta(baseline_flag, "1")
        return _result(
            "baseline_established",
            simulated_controls_used=False,
            conversation_bound=True,
            text_collision_count=collisions,
            text_merge_applied=False,
            new_messages_sent=0,
        )
    chosen: dict[str, Any] | None = None
    chosen_key = ""
    for index, item in enumerate(visible):
        if item.get("direction") != "buyer":
            continue
        body = _normalize(str(item.get("body") or ""))
        message_id = str(item.get("id") or "")
        if not body or not message_id:
            continue
        key = identity_key(conversation, message_id)
        if store.get_reply_attempt(key) is not None:
            continue
        if _already_replied(visible, index):
            store.insert_reply_attempt(key, conversation, "held_already_replied", "later_reply_present")
            continue
        decision = buyer_policy(body)
        if decision != "ordinary":
            store.insert_reply_attempt(key, conversation, decision, "policy")
            continue
        if chosen is None:
            chosen = item
            chosen_key = key
    if chosen is None:
        return _result(
            "no_eligible_buyer_message",
            simulated_controls_used=False,
            conversation_bound=True,
            text_collision_count=collisions,
            text_merge_applied=False,
        )
    try:
        generated = generator.generate(str(chosen.get("body") or ""))
    except AutoReplyError as exc:
        return _result(
            exc.code,
            simulated_controls_used=False,
            generator_transport=generator.transport_label,
            text_collision_count=collisions,
            text_merge_applied=False,
        )
    reply = present_simulated(generated)
    if not output_allowed(reply):
        store.insert_reply_attempt(chosen_key, conversation, "ollama_output_rejected", "policy")
        return _result(
            "ollama_output_rejected",
            generator_transport=generator.transport_label,
            text_collision_count=collisions,
            text_merge_applied=False,
        )
    try:
        again = _read(page)
    except Exception:
        return _result(
            "refused_read_failed",
            generator_transport=generator.transport_label,
            text_collision_count=collisions,
            text_merge_applied=False,
        )
    if _binding_problem(again) or str(again.get("open_conversation") or "") != conversation:
        return _result(
            "refused_conversation_changed",
            generator_transport=generator.transport_label,
            text_collision_count=collisions,
            text_merge_applied=False,
        )
    again_visible = [
        item
        for item in again.get("messages") or []
        if isinstance(item, dict) and item.get("hidden") is not True
    ]
    chosen_id = str(chosen.get("id") or "")
    still_there = False
    for index, item in enumerate(again_visible):
        if str(item.get("id") or "") != chosen_id or item.get("direction") != "buyer":
            continue
        still_there = True
        if _already_replied(again_visible, index):
            store.insert_reply_attempt(chosen_key, conversation, "held_already_replied", "changed_before_commit")
            return _result(
                "refused_replied_before_commit",
                generator_transport=generator.transport_label,
                text_collision_count=collisions,
                text_merge_applied=False,
            )
    if not still_there:
        return _result(
            "refused_message_missing",
            generator_transport=generator.transport_label,
            text_collision_count=collisions,
            text_merge_applied=False,
        )
    if not store.insert_reply_attempt(chosen_key, conversation, "committing", "sim_only"):
        return _result(
            "already_recorded",
            generator_transport=generator.transport_label,
            text_collision_count=collisions,
            text_merge_applied=False,
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
            text_collision_count=collisions,
            text_merge_applied=False,
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
        text_collision_count=collisions,
        text_merge_applied=False,
    )


_SIM_PAGE = """<!DOCTYPE html><html data-simulated-inbox="true"><body
 data-page-state="inbox" data-open-conversation="conv-sim-1">
<div class="content-box" data-sim-conversation="conv-sim-1">
<li class="onemsg" id="hist-sim" data-sim-direction="buyer"><p class="msg-content-box">【模拟】你好，在吗</p></li>
<li class="onemsg" id="new-sim" data-sim-direction="buyer" data-sim-reveal="new" hidden><p class="msg-content-box">【模拟】你好，在吗</p></li>
</div>
<textarea id="sim-composer" data-sim-conversation="conv-sim-1"></textarea>
<button type="button" id="sim-send">发送</button>
<script>
window.__sendClicks = 0;
document.querySelector("#sim-send").addEventListener("click", () => {
  window.__sendClicks += 1;
  const text = document.querySelector("#sim-composer").value;
  const item = document.createElement("li");
  item.className = "sim-outgoing onemsg";
  item.setAttribute("data-receipt", "sent");
  item.setAttribute("data-sim-direction", "merchant");
  item.textContent = text;
  document.querySelector(".content-box").appendChild(item);
});
</script></body></html>
"""

_REVEAL_JS = """
() => {
  const node = document.querySelector("[data-sim-reveal='new']");
  if (!node) return false;
  node.hidden = false;
  return true;
}
"""


def run_local_ollama_sim(
    *,
    transport: Callable[[str, str, dict[str, Any] | None, int], tuple[int, dict[str, Any]]] | None = None,
    transport_label: str = "urllib_loopback",
) -> dict[str, Any]:
    """Isolated localhost profile.

    The command path uses urllib to 127.0.0.1:11434. Tests pass an injected
    transport and must not label it urllib_loopback or bind that port.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from tempfile import TemporaryDirectory

    from inbox_collector.browser_session import DedicatedBrowser

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = _SIM_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = int(server.server_address[1])
    browser = None
    store: Store | None = None
    try:
        with TemporaryDirectory() as tmp:
            root = __import__("pathlib").Path(tmp)
            profile = root / "temp-profile"
            store = Store(root / "sim-data")
            browser = DedicatedBrowser(profile)
            browser.start()
            browser.page.goto(f"http://127.0.0.1:{port}/sim.html")
            urllib_loopback_transport.facts.clear()
            if transport is None:
                transport = urllib_loopback_transport
                transport_label = "urllib_loopback"
            elif transport_label == "urllib_loopback":
                raise AutoReplyError("injected_transport_must_be_labeled")
            generator = LoopbackGenerator(transport, transport_label=transport_label)
            now = __import__("time").time()
            baseline = run_simulated_cycle(browser.page, store, generator, min_interval=0, max_per_hour=3, now=now)
            if baseline.get("status") != "baseline_established":
                baseline["isolated_profile"] = True
                baseline["merchant_profile_used"] = False
                baseline["resend_allowed"] = False
                return baseline
            if browser.page.evaluate(_REVEAL_JS) is not True:
                return _result("refused_sim_reveal_missing", isolated_profile=True, merchant_profile_used=False)
            outcome = run_simulated_cycle(
                browser.page,
                store,
                generator,
                min_interval=0,
                max_per_hour=3,
                now=now + 1,
            )
            outcome["baseline_status"] = "baseline_established"
            outcome["isolated_profile"] = True
            outcome["merchant_profile_used"] = False
            outcome["profile_retained"] = False
            outcome["customer_service_quality_verified"] = False
            outcome["resend_allowed"] = False
            if outcome.get("status") != "simulated_sent":
                return outcome
            clicks = browser.page.evaluate("() => window.__sendClicks")
            outcome["composer_commits_seen"] = clicks if isinstance(clicks, int) else 0
            if outcome["composer_commits_seen"] != 1:
                outcome["status"] = "paused_uncertain"
                outcome["resend_allowed"] = False
            return outcome
    finally:
        if browser is not None:
            browser.close()
        if store is not None:
            store.close()
        server.shutdown()
        server.server_close()
