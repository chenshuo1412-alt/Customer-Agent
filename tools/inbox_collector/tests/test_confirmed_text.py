"""Offline checks for one operator-confirmed fixed reply. No merchant site."""

from __future__ import annotations

import hashlib
import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from inbox_collector.cli import main
from inbox_collector.confirmed_text import (
    RECEIPT_WAIT_SECONDS,
    _HANDLES_JS,
    _OBSERVE_JS,
    _READ_JS,
    digest,
    reply_problem,
    run_confirmed_text,
    run_input_state,
    write_authorization,
)
from inbox_collector.dom_diagnose import service_diagnose_request

_CHALLENGE = "客服配对1109-A7"
_REPLY = "测试收到，客服回复链路验证中。"
_NEW_CHALLENGE = "客服配对1109-B8"
_NEW_REPLY = "新链路测试B8收到。"
_OTHER_CHALLENGE = "历史封存挑战"
_OTHER_REPLY = "历史封存回复。"
_ROW_ID = "msg-token-aabbccdd"


def plant_legacy(data: Path, challenge: str = _OTHER_CHALLENGE, reply: str = _OTHER_REPLY) -> bytes:
    data.mkdir(parents=True, exist_ok=True)
    payload = {
        "sealed": True,
        "state": "uncertain",
        "retry_allowed": False,
        "challenge_sha256": digest(challenge),
        "reply_sha256": digest(reply),
        "durable": False,
    }
    path = data / "confirmed-text.ledger.json"
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path.read_bytes()


def _page(inner: str) -> str:
    return "<!DOCTYPE html><html><body style='margin:0'>" + inner + "</body></html>"


def _composer(button: str, value: str = "") -> str:
    text = value
    return (
        "<div class='reply-box'>"
        f"<textarea class='custom-scroll'>{text}</textarea>"
        f"{button}</div>"
    )


def _ready(button_extra: str = "") -> str:
    button = (
        "<button type='button' onclick=\""
        "var box=this.closest('.content-box');"
        "var t=box.querySelector('textarea');"
        "var li=document.createElement('li');"
        "li.className='onemsg';"
        "var d=document.createElement('div');"
        "d.className='msg-content';"
        "d.textContent=t.value;"
        "li.appendChild(d);"
        "box.insertBefore(li, box.firstChild);"
        "\">发送</button>"
    )
    if button_extra == "silent":
        button = "<button type='button'>发送</button>"
    return _page(
        "<ul><li class='chat-item'>张三</li></ul>"
        "<div class='content-box panel-a' style='width:640px;height:240px'>"
        "<div class='five-minute active' style='width:40px;height:16px'></div>"
        "<div class='all-chat-list active' style='width:40px;height:16px'></div>"
        f"<li class='onemsg' id='{_ROW_ID}' style='width:500px;height:40px'>"
        f"<div class='msg-content' style='width:160px;height:20px'>{_CHALLENGE}</div></li>"
        "<div class='system-tip' style='width:500px;height:18px;text-align:center'>系统样式</div>"
        + _composer(button)
        + "</div>"
    )


class _Silent:
    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, script: str, arg: object | None = None) -> dict:
        self.calls += 1
        raise AssertionError("page must not be read")


class ConfirmedTextTest(unittest.TestCase):
    def test_script_does_not_use_private_channels_or_the_model(self) -> None:
        from inbox_collector import confirmed_text

        source = Path(confirmed_text.__file__).read_text(encoding="utf-8")
        script = _READ_JS + _OBSERVE_JS + _HANDLES_JS
        for word in ("currentuid", "localStorage", "cookie", "userinfo", "csinfosmap", "fetch(", "WebSocket"):
            self.assertNotIn(word, script)
        for word in ("ollama", "11434", "LoopbackGenerator", "new Event"):
            self.assertNotIn(word, source)
        self.assertNotIn(".click()", script)
        self.assertNotIn("eval(", script)
        self.assertGreaterEqual(RECEIPT_WAIT_SECONDS, 5)
        self.assertIsNone(reply_problem(_REPLY))

    def test_authorization_mismatches_do_not_read_the_page(self) -> None:
        page = _Silent()
        with TemporaryDirectory() as tmp:
            data = Path(tmp)
            window = "ab" * 16
            original = plant_legacy(data)
            write_authorization(data, challenge=_CHALLENGE, reply=_REPLY, window_id=window, expires_at=time.time() + 60)
            expired = run_confirmed_text(page, data, _CHALLENGE, window_id=window, now=time.time() + 600)
            self.assertEqual(expired["status"], "authorization_expired")
            mismatch = run_confirmed_text(page, data, "另一句挑战", window_id=window)
            self.assertEqual(mismatch["status"], "authorization_mismatch")
            other_window = run_confirmed_text(page, data, _CHALLENGE, window_id="cd" * 16)
            self.assertEqual(other_window["status"], "window_mismatch")
            unbound = run_confirmed_text(page, data, _CHALLENGE, window_id=None)
            self.assertEqual(unbound["status"], "window_unbound")
            payload = json.loads((data / "confirmed-text.auth.json").read_text(encoding="utf-8"))
            payload["buyer_source"] = "guessed"
            (data / "confirmed-text.auth.json").write_text(json.dumps(payload), encoding="utf-8")
            buyer = run_confirmed_text(page, data, _CHALLENGE, window_id=window)
            self.assertEqual(buyer["status"], "buyer_source_unconfirmed")
            self.assertEqual(page.calls, 0)
            self.assertEqual((data / "confirmed-text.ledger.json").read_bytes(), original)
            self.assertFalse((data / "confirmed-text.attempts").exists())

    def test_cli_refuses_without_writing_a_send(self) -> None:
        with TemporaryDirectory() as tmp:
            data = Path(tmp)
            code = main(
                [
                    "confirmed-text-send",
                    "--data-dir",
                    str(data),
                    "--challenge",
                    _CHALLENGE,
                    "--reply",
                    _REPLY,
                    "--buyer-source",
                    "own-account",
                    "--expires-seconds",
                    "600",
                    "--opt-in",
                    "storewide",
                    "--timeout-seconds",
                    "1",
                ]
            )
            self.assertEqual(code, 2)
            self.assertFalse((data / "confirmed-text.auth.json").exists())
            code = main(
                [
                    "confirmed-text-send",
                    "--data-dir",
                    str(data),
                    "--challenge",
                    _CHALLENGE,
                    "--reply",
                    _REPLY,
                    "--buyer-source",
                    "own-account",
                    "--expires-seconds",
                    "600",
                    "--opt-in",
                    "single-confirmed-text",
                    "--timeout-seconds",
                    "1",
                ]
            )
            self.assertEqual(code, 2)
            self.assertFalse((data / "confirmed-text.auth.json").exists())

    def test_visible_panel_gates(self) -> None:
        from playwright.sync_api import sync_playwright

        window = "ef" * 16
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page()
                self.assertNotIn("pinduoduo", page.url)

                def run(name: str, html: str, *, challenge: str = _CHALLENGE) -> dict:
                    data = root / name
                    data.mkdir()
                    plant_legacy(data)
                    page.set_content(html)
                    write_authorization(
                        data,
                        challenge=challenge,
                        reply=_REPLY,
                        window_id=window,
                        expires_at=time.time() + 120,
                    )
                    (data / "dom-diagnose.request").write_text(
                        json.dumps({"op": "confirmed-text", "probe_phrase": challenge}, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    service_diagnose_request(page, data, window_id=window)
                    report = json.loads((data / "dom-diagnose.result.json").read_text(encoding="utf-8"))
                    blob = json.dumps(report, ensure_ascii=False)
                    self.assertNotIn(_CHALLENGE, blob)
                    self.assertNotIn(_REPLY, blob)
                    self.assertNotIn(_ROW_ID, blob)
                    self.assertNotIn("张三", blob)
                    self.assertFalse(report["production_send_allowed"])
                    self.assertFalse(report["identity_from_highlight"])
                    self.assertFalse(report["role_required"])
                    self.assertFalse(report["currentuid_used"])
                    self.assertFalse(report["model_called"])
                    self.assertFalse(report["delivery_claimed"])
                    return report

                missing = run("absent", _page("<div class='content-box'><textarea></textarea><button type='button'>发送</button></div>"))
                self.assertEqual(missing["status"], "challenge_absent")
                self.assertFalse(missing["clicked"])
                self.assertFalse(missing["ledger_sealed"])

                listed = run(
                    "wrong-panel",
                    _page(
                        f"<ul><li class='chat-item active'>{_CHALLENGE}</li><li class='chat-item'>张三</li></ul>"
                        "<div class='content-box'><textarea></textarea><button type='button'>发送</button></div>"
                    ),
                )
                self.assertEqual(listed["status"], "challenge_absent")
                self.assertFalse(listed["clicked"])

                duplicate = run(
                    "duplicate-challenge",
                    _page(
                        "<div class='content-box'>"
                        f"<li class='onemsg'><div class='msg-content'>{_CHALLENGE}</div></li>"
                        f"<li class='onemsg'><div class='msg-content'>{_CHALLENGE}</div></li>"
                        "<textarea></textarea><button type='button'>发送</button></div>"
                    ),
                )
                self.assertEqual(duplicate["status"], "challenge_not_unique")
                self.assertFalse(duplicate["ledger_sealed"])

                draft_html = _ready().replace("<textarea class='custom-scroll'></textarea>", "<textarea class='custom-scroll'>已有草稿</textarea>")
                draft = run("draft", draft_html)
                self.assertEqual(draft["status"], "draft_present")
                self.assertFalse(draft["clicked"])
                self.assertFalse(draft["mutated"])
                self.assertFalse(draft["ledger_sealed"])
                self.assertEqual(page.locator("textarea").input_value(), "已有草稿")

                existing = run(
                    "existing-reply",
                    _ready().replace(
                        f"<div class='msg-content' style='width:160px;height:20px'>{_CHALLENGE}</div>",
                        f"<div class='msg-content'>{_REPLY}</div></li><li class='onemsg' id='{_ROW_ID}'><div class='msg-content'>{_CHALLENGE}</div>",
                    ),
                )
                self.assertEqual(existing["status"], "reply_already_visible")
                self.assertFalse(existing["clicked"])
                self.assertFalse(existing["ledger_sealed"])

                class _Switch:
                    def __init__(self) -> None:
                        self.calls = 0

                    def evaluate(self, script: str, arg: object | None = None) -> dict:
                        self.calls += 1
                        if self.calls == 2:
                            page.set_content(_ready().replace("panel-a", "panel-b"))
                        return page.evaluate(script, arg)

                switch_page = _Switch()
                switch_dir = root / "switch"
                switch_dir.mkdir()
                plant_legacy(switch_dir)
                page.set_content(_ready())
                write_authorization(switch_dir, challenge=_CHALLENGE, reply=_REPLY, window_id=window, expires_at=time.time() + 120)
                switched = run_confirmed_text(switch_page, switch_dir, _CHALLENGE, window_id=window)
                self.assertEqual(switched["status"], "panel_changed")
                self.assertFalse(switched["clicked"])
                self.assertFalse(switched["mutated"])
                self.assertTrue(switched["ledger_sealed"])
                self.assertEqual(page.locator("textarea").input_value(), "")
                again = run_confirmed_text(switch_page, switch_dir, _CHALLENGE, window_id=window)
                self.assertEqual(again["status"], "attempt_consumed")
                self.assertEqual(switch_page.calls, 2)

                class _Boom:
                    def __init__(self) -> None:
                        self.calls = 0

                    def evaluate(self, script: str, arg: object | None = None) -> dict:
                        self.calls += 1
                        if self.calls >= 2:
                            raise RuntimeError("act failed")
                        return page.evaluate(script, arg)

                boom = _Boom()
                boom_dir = root / "boom"
                boom_dir.mkdir()
                plant_legacy(boom_dir)
                page.set_content(_ready())
                write_authorization(boom_dir, challenge=_CHALLENGE, reply=_REPLY, window_id=window, expires_at=time.time() + 120)
                failed = run_confirmed_text(boom, boom_dir, _CHALLENGE, window_id=window)
                self.assertEqual(failed["status"], "evaluate_failed")
                self.assertTrue(failed["ledger_sealed"])
                self.assertFalse(failed["retry_allowed"])
                blocked = run_confirmed_text(boom, boom_dir, _CHALLENGE, window_id=window)
                self.assertEqual(blocked["status"], "attempt_consumed")
                self.assertEqual(boom.calls, 2)

                page.set_content(_ready("silent"))
                uncertain_dir = root / "uncertain"
                uncertain_dir.mkdir()
                plant_legacy(uncertain_dir)
                write_authorization(uncertain_dir, challenge=_CHALLENGE, reply=_REPLY, window_id=window, expires_at=time.time() + 120)
                uncertain = run_confirmed_text(page, uncertain_dir, _CHALLENGE, window_id=window, receipt_wait_seconds=0.4)
                self.assertEqual(uncertain["status"], "paused_uncertain")
                self.assertTrue(uncertain["clicked"])
                self.assertTrue(uncertain["ledger_sealed"])
                self.assertEqual(uncertain["receipt"], "absent")
                self.assertFalse(uncertain["delivered_to_buyer"])
                self.assertEqual(uncertain["input_method"], "playwright_fill")
                self.assertEqual(uncertain["click_method"], "playwright_click")
                self.assertEqual(uncertain["appearance"], "input_retained_receipt_absent")
                self.assertTrue(uncertain["input_state"]["input_equals_reply"])
                self.assertEqual(uncertain["input_state"]["reply_exact_count"], 0)
                self.assertFalse(uncertain["platform_send_proven"])
                (root / "uncertain" / "dom-diagnose.request").write_text(
                    json.dumps({"op": "confirmed-text", "probe_phrase": _CHALLENGE}, ensure_ascii=False),
                    encoding="utf-8",
                )
                service_diagnose_request(page, root / "uncertain", window_id=window)
                retry = json.loads((root / "uncertain" / "dom-diagnose.result.json").read_text(encoding="utf-8"))
                self.assertEqual(retry["status"], "attempt_consumed")
                self.assertFalse(retry["clicked"])

                ready = run("ready", _ready())
                self.assertEqual(ready["status"], "page_visible")
                self.assertEqual(ready["receipt"], "page_visible")
                self.assertEqual(ready["input_method"], "playwright_fill")
                self.assertEqual(ready["click_method"], "playwright_click")
                self.assertEqual(ready["buyer_receipt"], "unproven")
                self.assertTrue(ready["clicked"])
                self.assertEqual(ready["highlight_count"], 2)
                self.assertEqual(ready["row_id_sha256"], hashlib.sha256(_ROW_ID.encode()).hexdigest())
                self.assertFalse(ready["row_id_is_platform_uid"])
                self.assertEqual(page.locator("li.onemsg .msg-content").nth(0).inner_text(), _REPLY)
                (root / "ready" / "dom-diagnose.request").write_text(
                    json.dumps({"op": "confirmed-text", "probe_phrase": _CHALLENGE}, ensure_ascii=False),
                    encoding="utf-8",
                )
                service_diagnose_request(page, root / "ready", window_id=window)
                second = json.loads((root / "ready" / "dom-diagnose.result.json").read_text(encoding="utf-8"))
                self.assertEqual(second["status"], "attempt_consumed")
                self.assertEqual(page.locator("li.onemsg").count(), 2)
                browser.close()

        self.assertEqual(digest(_REPLY), hashlib.sha256(_REPLY.encode()).hexdigest())

    def test_rejected_operation_exits_nonzero(self) -> None:
        import threading

        from inbox_collector.cli import diagnose_exit_code

        self.assertEqual(diagnose_exit_code("probe", {"status": "op_rejected", "handler": "request-router"}), 2)
        self.assertEqual(diagnose_exit_code("probe", {"status": "field_rejected", "handler": "request-router"}), 2)
        page = _Silent()
        with TemporaryDirectory() as tmp:
            data = Path(tmp)
            stop = threading.Event()

            def serve() -> None:
                while not stop.is_set():
                    service_diagnose_request(page, data)
                    time.sleep(0.05)

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            try:
                code = main(
                    [
                        "diagnose-dom",
                        "--data-dir",
                        str(data),
                        "--timeout-seconds",
                        "2",
                        "--op",
                        "not-a-real-op",
                        "--probe-phrase",
                        "x",
                    ]
                )
            finally:
                stop.set()
                thread.join(1)
            self.assertEqual(code, 2)
            result = json.loads((data / "dom-diagnose.result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "op_rejected")
            self.assertEqual(page.calls, 0)

    def test_controlled_composer_matches_relayed_input_observation(self) -> None:
        """Local sample only. A Projects Manager relay said the merchant box still showed the reply and no bubble; the image was not viewed here."""
        from playwright.sync_api import sync_playwright

        html = (
            "<!DOCTYPE html><html><body>"
            "<div class='content-box panel-a' style='width:640px'>"
            f"<li class='onemsg' id='{_ROW_ID}'><div class='msg-content'>{_CHALLENGE}</div></li>"
            "<div class='reply-box'><textarea id='composer' class='custom-scroll'></textarea>"
            "<button id='send' type='button' class='send-btn'>发送</button></div>"
            "<div class='captcha'>验证码</div></div>"
            "<textarea id='other'>客户秘密</textarea>"
            "<script>window.__model='';window.__sent=0;"
            "const field=document.querySelector('#composer');"
            "field.addEventListener('input',(event)=>{if(!event.isTrusted)return;window.__model=field.value;});"
            "document.querySelector('#send').addEventListener('click',(event)=>{if(!event.isTrusted)return;"
            f"if(window.__model!=='{_REPLY}')return;window.__sent+=1;"
            "const li=document.createElement('li');li.className='onemsg';"
            "const span=document.createElement('span');span.className='bubble-text';span.textContent=window.__model;"
            "li.appendChild(span);document.querySelector('.content-box').appendChild(li);"
            "field.value='';window.__model='';});</script></body></html>"
        )
        window = "12" * 16
        with TemporaryDirectory() as tmp:
            data = Path(tmp)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page()
                page.set_content(html)
                page.evaluate(
                    """(reply) => {
                      const field = document.querySelector('#composer');
                      field.value = reply;
                      field.dispatchEvent(new Event('input', { bubbles: true }));
                      document.querySelector('#send').click();
                    }""",
                    _REPLY,
                )
                self.assertEqual(page.locator("#composer").input_value(), _REPLY)
                self.assertEqual(page.evaluate("() => window.__model"), "")
                self.assertEqual(page.evaluate("() => window.__sent"), 0)
                self.assertEqual(page.locator("span.bubble-text").count(), 0)
                page.evaluate("() => { document.querySelector('#composer').value = ''; }")
                plant_legacy(data)
                write_authorization(data, challenge=_CHALLENGE, reply=_REPLY, window_id=window, expires_at=time.time() + 120)
                sent = run_confirmed_text(page, data, _CHALLENGE, window_id=window, receipt_wait_seconds=2)
                self.assertEqual(sent["status"], "page_visible")
                self.assertEqual(sent["input_method"], "playwright_fill")
                self.assertEqual(sent["click_method"], "playwright_click")
                self.assertEqual(sent["clicked_tag"], "button")
                self.assertIn("send-btn", sent["clicked_classes"])
                self.assertFalse(sent["delivery_claimed"])
                self.assertEqual(sent["buyer_receipt"], "unproven")
                self.assertEqual(page.evaluate("() => window.__sent"), 1)
                self.assertEqual(page.locator("span.bubble-text").inner_text(), _REPLY)
                self.assertNotIn(_REPLY, json.dumps(sent, ensure_ascii=False))
                blocked = run_confirmed_text(page, data, _CHALLENGE, window_id=window)
                self.assertEqual(blocked["status"], "attempt_consumed")
                self.assertEqual(page.evaluate("() => window.__sent"), 1)

                held = html.replace(
                    "<textarea id='composer' class='custom-scroll'></textarea>",
                    f"<textarea id='composer' class='custom-scroll'>{_REPLY}</textarea>",
                )
                page.set_content(held)
                page.evaluate("() => { window.__clicks = 0; document.querySelector('#send').addEventListener('click', () => { window.__clicks += 1; }); }")
                state_dir = data / "state"
                state_dir.mkdir()
                write_authorization(state_dir, challenge=_CHALLENGE, reply=_REPLY, window_id=window, expires_at=time.time() - 5)
                ledger = state_dir / "confirmed-text.ledger.json"
                ledger.write_text(
                    json.dumps(
                        {
                            "sealed": True,
                            "state": "uncertain",
                            "retry_allowed": False,
                            "challenge_sha256": digest(_CHALLENGE),
                            "reply_sha256": digest(_REPLY),
                            "durable": False,
                        }
                    ),
                    encoding="utf-8",
                )
                auth_bytes = (state_dir / "confirmed-text.auth.json").read_bytes()
                ledger_bytes = ledger.read_bytes()
                (state_dir / "dom-diagnose.request").write_text(
                    json.dumps({"op": "input-state-readonly", "probe_phrase": _CHALLENGE}, ensure_ascii=False),
                    encoding="utf-8",
                )
                service_diagnose_request(page, state_dir, window_id="zz" * 16)
                report = json.loads((state_dir / "dom-diagnose.result.json").read_text(encoding="utf-8"))
                blob = json.dumps(report, ensure_ascii=False)
                self.assertEqual(report["status"], "input_state_ready")
                self.assertTrue(report["input_state"]["input_equals_reply"])
                self.assertTrue(report["input_state"]["input_nonempty"])
                self.assertEqual(report["input_state"]["reply_exact_count"], 0)
                self.assertEqual(report["input_state"]["reply_age"], "unproven_single_snapshot")
                self.assertTrue(report["input_state"]["visible_captcha"])
                self.assertTrue(report["ledger_sealed"])
                self.assertFalse(report["retry_allowed"])
                self.assertFalse(report["window_matches"])
                self.assertTrue(report["authorization_expired"])
                self.assertTrue(report["files_unchanged"])
                self.assertNotIn("客户秘密", blob)
                self.assertNotIn("验证码", blob)
                self.assertNotIn(_REPLY, blob)
                self.assertEqual((state_dir / "confirmed-text.auth.json").read_bytes(), auth_bytes)
                self.assertEqual(ledger.read_bytes(), ledger_bytes)
                self.assertEqual(page.evaluate("() => window.__clicks"), 0)
                write_authorization(state_dir, challenge=_CHALLENGE, reply=_REPLY, window_id=window, expires_at=time.time() + 60)
                refused = run_confirmed_text(page, state_dir, _CHALLENGE, window_id=window)
                self.assertEqual(refused["status"], "legacy_blocked")
                self.assertEqual(page.locator("#composer").input_value(), _REPLY)
                self.assertEqual(ledger.read_bytes(), ledger_bytes)
                browser.close()

    def test_new_pair_is_separate_from_the_sealed_legacy_file(self) -> None:
        from playwright.sync_api import sync_playwright

        from inbox_collector.cli import main

        class _El:
            def __init__(self, page: "_Gate") -> None:
                self.page = page

            def as_element(self) -> "_El":
                return self

            def input_value(self) -> str:
                return ""

            def fill(self, text: str) -> None:
                if self.page.fail_fill:
                    raise RuntimeError("fill failed")
                self.page.fills += 1
                self.page.filled = True

            def click(self) -> None:
                if self.page.fail_click:
                    raise RuntimeError("click failed")
                self.page.clicks += 1

            def evaluate(self, script: str) -> dict:
                return {"tag": "button", "classes": ["send-btn"]}

        class _Bundle:
            def __init__(self, page: "_Gate") -> None:
                self.page = page

            def evaluate(self, script: str) -> dict:
                return {"ok": True, "status": "ready"}

            def get_property(self, name: str) -> _El:
                return _El(self.page)

            def dispose(self) -> None:
                return None

        class _Gate:
            def __init__(self, *, fail_fill: bool = False, fail_click: bool = False, receipt: bool = True) -> None:
                self.fills = 0
                self.clicks = 0
                self.filled = False
                self.fail_fill = fail_fill
                self.fail_click = fail_click
                self.receipt = receipt

            def evaluate(self, script: str, arg: object | None = None) -> dict:
                if "CONFIRMED_TEXT_READ" in script:
                    return {
                        "marker": "CONFIRMED_TEXT_READ",
                        "match_count": 1,
                        "highlight_count": 0,
                        "input_count": 1,
                        "input_empty": True,
                        "sender_count": 1,
                        "reply_visible_count": 0,
                        "panel_fingerprint": "div.panel|li.row|div.composer",
                        "row_id_raw": "msg-new-b8token",
                    }
                if "INPUT_STATE_READONLY" in script:
                    visible = self.filled and self.receipt and self.clicks > 0
                    return {
                        "marker": "INPUT_STATE_READONLY",
                        "match_count": 1,
                        "input_count": 1,
                        "input_nonempty": self.filled,
                        "input_equals_reply": self.filled,
                        "sender_count": 1,
                        "sender": {"tag": "button", "classes": ["send-btn"], "visible": True, "disabled": False, "label_kind": "send"},
                        "visible_captcha": False,
                        "visible_error": False,
                        "flag_classes": [],
                        "reply_exact_count": 1 if visible else 0,
                        "reply_scan_truncated": False,
                        "panel_fingerprint": "div.panel|li.row|div.composer",
                        "row_id_raw": "msg-new-b8token",
                        "values_included": False,
                    }
                raise AssertionError("unexpected evaluate")

            def evaluate_handle(self, script: str, arg: object | None = None) -> _Bundle:
                return _Bundle(self)

        def _blocked(data: Path, page: _Gate, challenge: str, reply: str) -> dict:
            write_authorization(data, challenge=challenge, reply=reply, window_id="ab" * 16, expires_at=time.time() + 60)
            return run_confirmed_text(page, data, challenge, window_id="ab" * 16, receipt_wait_seconds=0.2)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = plant_legacy(root, _CHALLENGE, _REPLY)
            page = _Gate()
            same_challenge = _blocked(root, page, _CHALLENGE, _NEW_REPLY)
            same_reply = _blocked(root, page, _NEW_CHALLENGE, _REPLY)
            self.assertEqual(same_challenge["status"], "legacy_blocked")
            self.assertEqual(same_reply["status"], "legacy_blocked")
            self.assertEqual(page.fills, 0)
            self.assertEqual(page.clicks, 0)
            self.assertFalse(same_challenge["production_send_allowed"])
            self.assertTrue(same_challenge["legacy_unchanged"])
            self.assertEqual((root / "confirmed-text.ledger.json").read_bytes(), legacy)
            bare = root / "no-auth"
            plant_legacy(bare, _CHALLENGE, _REPLY)
            missing_auth = run_confirmed_text(_Gate(), bare, _NEW_CHALLENGE, window_id="ab" * 16)
            self.assertEqual(missing_auth["status"], "authorization_missing")
            self.assertFalse((bare / "confirmed-text.attempts").exists())
            absent = root / "absent"
            absent.mkdir()
            write_authorization(absent, challenge=_NEW_CHALLENGE, reply=_NEW_REPLY, window_id="ab" * 16, expires_at=time.time() + 60)
            self.assertEqual(run_confirmed_text(_Gate(), absent, _NEW_CHALLENGE, window_id="ab" * 16)["status"], "legacy_missing")
            self.assertEqual(_Gate().fills, 0)
            broken = root / "broken"
            broken.mkdir()
            (broken / "confirmed-text.ledger.json").write_bytes(b"{")
            self.assertEqual(run_confirmed_text(_Gate(), broken, _NEW_CHALLENGE, window_id="ab" * 16)["status"], "legacy_unreadable")
            conflict = root / "conflict"
            plant_legacy(conflict, _CHALLENGE, _REPLY)
            payload = json.loads((conflict / "confirmed-text.ledger.json").read_text(encoding="utf-8"))
            payload["retry_allowed"] = True
            (conflict / "confirmed-text.ledger.json").write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(run_confirmed_text(_Gate(), conflict, _NEW_CHALLENGE, window_id="ab" * 16)["status"], "legacy_conflict")

            fresh = root / "fresh"
            original = plant_legacy(fresh, _CHALLENGE, _REPLY)
            gate = _Gate()
            write_authorization(fresh, challenge=_NEW_CHALLENGE, reply=_NEW_REPLY, window_id="ab" * 16, expires_at=time.time() + 60)
            first_id = json.loads((fresh / "confirmed-text.auth.json").read_text(encoding="utf-8"))["authorization_id"]
            sent = run_confirmed_text(gate, fresh, _NEW_CHALLENGE, window_id="ab" * 16, receipt_wait_seconds=0.2)
            self.assertEqual(sent["status"], "page_visible")
            self.assertEqual(gate.fills, 1)
            self.assertEqual(gate.clicks, 1)
            self.assertFalse(sent["production_send_allowed"])
            self.assertFalse(sent["delivery_claimed"])
            self.assertTrue(sent["legacy_unchanged"])
            self.assertEqual((fresh / "confirmed-text.ledger.json").read_bytes(), original)
            self.assertEqual(os.stat(fresh / "confirmed-text.ledger.json").st_mode & 0o777, 0o600)
            write_authorization(fresh, challenge=_NEW_CHALLENGE, reply=_NEW_REPLY, window_id="cd" * 16, expires_at=time.time() + 60)
            second_id = json.loads((fresh / "confirmed-text.auth.json").read_text(encoding="utf-8"))["authorization_id"]
            self.assertNotEqual(first_id, second_id)
            again = run_confirmed_text(gate, fresh, _NEW_CHALLENGE, window_id="cd" * 16, receipt_wait_seconds=0.2)
            write_authorization(fresh, challenge=_NEW_CHALLENGE, reply="另一句不会发。", window_id="cd" * 16, expires_at=time.time() + 60)
            other_reply = run_confirmed_text(gate, fresh, _NEW_CHALLENGE, window_id="cd" * 16, receipt_wait_seconds=0.2)
            self.assertEqual(again["status"], "attempt_consumed")
            self.assertEqual(other_reply["status"], "attempt_consumed")
            self.assertEqual(gate.fills, 1)

            import threading

            raced = root / "race"
            plant_legacy(raced, _CHALLENGE, _REPLY)
            write_authorization(raced, challenge=_NEW_CHALLENGE, reply=_NEW_REPLY, window_id="ab" * 16, expires_at=time.time() + 60)
            shared = _Gate()
            results: list[str] = []

            def once() -> None:
                report = run_confirmed_text(shared, raced, _NEW_CHALLENGE, window_id="ab" * 16, receipt_wait_seconds=0.2)
                results.append(str(report["status"]))

            threads = [threading.Thread(target=once) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(shared.fills, 1)
            self.assertIn("page_visible", results)
            self.assertIn("attempt_consumed", results)

            failed = root / "fail-fill"
            plant_legacy(failed, _CHALLENGE, _REPLY)
            write_authorization(failed, challenge=_NEW_CHALLENGE, reply=_NEW_REPLY, window_id="ab" * 16, expires_at=time.time() + 60)
            broken_fill = _Gate(fail_fill=True)
            self.assertEqual(run_confirmed_text(broken_fill, failed, _NEW_CHALLENGE, window_id="ab" * 16)["status"], "evaluate_failed")
            self.assertEqual(run_confirmed_text(_Gate(), failed, _NEW_CHALLENGE, window_id="ab" * 16)["status"], "attempt_consumed")
            self.assertEqual(broken_fill.fills, 0)
            clicked = root / "fail-click"
            plant_legacy(clicked, _CHALLENGE, _REPLY)
            write_authorization(clicked, challenge=_NEW_CHALLENGE, reply=_NEW_REPLY, window_id="ab" * 16, expires_at=time.time() + 60)
            broken_click = _Gate(fail_click=True)
            self.assertEqual(run_confirmed_text(broken_click, clicked, _NEW_CHALLENGE, window_id="ab" * 16, receipt_wait_seconds=0.2)["status"], "paused_uncertain")
            self.assertEqual(run_confirmed_text(_Gate(), clicked, _NEW_CHALLENGE, window_id="ab" * 16)["status"], "attempt_consumed")
            self.assertEqual(broken_click.clicks, 0)
            quiet = root / "no-receipt"
            plant_legacy(quiet, _CHALLENGE, _REPLY)
            write_authorization(quiet, challenge=_NEW_CHALLENGE, reply=_NEW_REPLY, window_id="ab" * 16, expires_at=time.time() + 60)
            mute = _Gate(receipt=False)
            uncertain = run_confirmed_text(mute, quiet, _NEW_CHALLENGE, window_id="ab" * 16, receipt_wait_seconds=0.2)
            self.assertEqual(uncertain["status"], "paused_uncertain")
            self.assertEqual(run_confirmed_text(_Gate(), quiet, _NEW_CHALLENGE, window_id="ab" * 16)["status"], "attempt_consumed")
            readonly = root / "readonly"
            original_readonly = plant_legacy(readonly, _CHALLENGE, _REPLY)
            write_authorization(readonly, challenge=_CHALLENGE, reply=_REPLY, window_id="ab" * 16, expires_at=time.time() + 60)
            looked = run_input_state(_Gate(), readonly, _CHALLENGE, window_id="zz" * 16)
            self.assertEqual(looked["status"], "input_state_ready")
            self.assertFalse(looked["production_send_allowed"])
            self.assertTrue(looked["files_unchanged"])
            self.assertEqual((readonly / "confirmed-text.ledger.json").read_bytes(), original_readonly)
            self.assertFalse((readonly / "confirmed-text.attempts").exists())
            code = main(
                [
                    "confirmed-text-send",
                    "--data-dir",
                    str(readonly),
                    "--challenge",
                    _CHALLENGE,
                    "--reply",
                    _NEW_REPLY,
                    "--buyer-source",
                    "own-account",
                    "--expires-seconds",
                    "600",
                    "--opt-in",
                    "single-confirmed-text",
                    "--timeout-seconds",
                    "1",
                ]
            )
            self.assertEqual(code, 2)
            self.assertEqual((readonly / "confirmed-text.ledger.json").read_bytes(), original_readonly)
            stored = json.loads((readonly / "confirmed-text.auth.json").read_text(encoding="utf-8"))
            self.assertEqual(stored["challenge_sha256"], digest(_CHALLENGE))
            self.assertEqual(stored["reply_sha256"], digest(_REPLY))

        html = (
            "<!DOCTYPE html><html><body><div class='content-box panel-a' style='width:640px'>"
            f"<li class='onemsg' id='{_ROW_ID}'><div class='msg-content'>{_NEW_CHALLENGE}</div></li>"
            "<div class='reply-box'><textarea id='composer'></textarea>"
            "<button id='send' type='button' class='send-btn'>发送</button></div></div>"
            "<script>window.__model='';window.__sent=0;const field=document.querySelector('#composer');"
            "field.addEventListener('input',(event)=>{if(!event.isTrusted)return;window.__model=field.value;});"
            "document.querySelector('#send').addEventListener('click',(event)=>{if(!event.isTrusted)return;"
            f"if(window.__model!=='{_NEW_REPLY}')return;window.__sent+=1;"
            "const li=document.createElement('li');li.className='onemsg';const span=document.createElement('span');"
            "span.textContent=window.__model;li.appendChild(span);document.querySelector('.content-box').appendChild(li);});"
            "</script></body></html>"
        )
        with TemporaryDirectory() as tmp:
            data = Path(tmp)
            original = plant_legacy(data, _CHALLENGE, _REPLY)
            write_authorization(data, challenge=_NEW_CHALLENGE, reply=_NEW_REPLY, window_id="ab" * 16, expires_at=time.time() + 90)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page()
                page.set_content(html)
                self.assertNotIn("pinduoduo", page.url)
                sent = run_confirmed_text(page, data, _NEW_CHALLENGE, window_id="ab" * 16, receipt_wait_seconds=2)
                self.assertEqual(sent["status"], "page_visible")
                self.assertEqual(sent["input_method"], "playwright_fill")
                self.assertEqual(page.evaluate("() => window.__sent"), 1)
                self.assertEqual((data / "confirmed-text.ledger.json").read_bytes(), original)
                self.assertEqual(os.stat(data / "confirmed-text.ledger.json").st_mode & 0o777, 0o600)
                self.assertNotIn(_NEW_REPLY, json.dumps(sent, ensure_ascii=False))
                blocked = run_confirmed_text(page, data, _NEW_CHALLENGE, window_id="ab" * 16)
                self.assertEqual(blocked["status"], "attempt_consumed")
                self.assertEqual(page.evaluate("() => window.__sent"), 1)
                browser.close()


if __name__ == "__main__":
    unittest.main()
