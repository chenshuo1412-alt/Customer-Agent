"""Offline checks for one operator-confirmed fixed reply. No merchant site."""

from __future__ import annotations

import hashlib
import json
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
    write_authorization,
)
from inbox_collector.dom_diagnose import service_diagnose_request

_CHALLENGE = "客服配对1109-A7"
_REPLY = "测试收到，客服回复链路验证中。"
_ROW_ID = "msg-token-aabbccdd"


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
            self.assertFalse((data / "confirmed-text.ledger.json").exists())

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
                page.set_content(_ready())
                write_authorization(switch_dir, challenge=_CHALLENGE, reply=_REPLY, window_id=window, expires_at=time.time() + 120)
                switched = run_confirmed_text(switch_page, switch_dir, _CHALLENGE, window_id=window)
                self.assertEqual(switched["status"], "panel_changed")
                self.assertFalse(switched["clicked"])
                self.assertFalse(switched["mutated"])
                self.assertTrue(switched["ledger_sealed"])
                self.assertEqual(page.locator("textarea").input_value(), "")
                again = run_confirmed_text(switch_page, switch_dir, _CHALLENGE, window_id=window)
                self.assertEqual(again["status"], "ledger_closed")
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
                page.set_content(_ready())
                write_authorization(boom_dir, challenge=_CHALLENGE, reply=_REPLY, window_id=window, expires_at=time.time() + 120)
                failed = run_confirmed_text(boom, boom_dir, _CHALLENGE, window_id=window)
                self.assertEqual(failed["status"], "evaluate_failed")
                self.assertTrue(failed["ledger_sealed"])
                self.assertFalse(failed["retry_allowed"])
                blocked = run_confirmed_text(boom, boom_dir, _CHALLENGE, window_id=window)
                self.assertEqual(blocked["status"], "ledger_closed")
                self.assertEqual(boom.calls, 2)

                page.set_content(_ready("silent"))
                uncertain_dir = root / "uncertain"
                uncertain_dir.mkdir()
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
                self.assertEqual(retry["status"], "ledger_closed")
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
                self.assertEqual(second["status"], "ledger_closed")
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
                self.assertEqual(blocked["status"], "ledger_closed")
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
                refused = run_confirmed_text(page, state_dir, _CHALLENGE, window_id=window)
                self.assertEqual(refused["status"], "ledger_closed")
                self.assertEqual(page.locator("#composer").input_value(), _REPLY)
                browser.close()


if __name__ == "__main__":
    unittest.main()
