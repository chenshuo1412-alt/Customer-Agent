"""Headless synthetic checks for the geometry-readonly request protocol."""

from __future__ import annotations

import json
import os
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from inbox_collector.dom_diagnose import service_diagnose_request
from inbox_collector.geometry_readonly import _GEOMETRY_JS

_PHRASE = "客服测试2006"
_PIXEL = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"


def _page(inner: str) -> str:
    return (
        "<!DOCTYPE html><html><body style='margin:0'>"
        "<div class='content-box' style='position:relative;width:640px;height:240px'>"
        f"{inner}</div></body></html>"
    )


def _ready_rows() -> str:
    return (
        "<ul><li class='chat-item active' id='conv-88442211' currentuid='uid-conv-10000001' "
        "style='width:200px;height:24px'>列表</li></ul>"
        "<li class='onemsg buyer-item' id='msg-88442211' currentuid='uid-conv-10000001' "
        "style='position:relative;width:640px;height:48px'>"
        f"<img src='{_PIXEL}' style='position:absolute;left:4px;top:4px;width:20px;height:20px'>"
        "<div class='msg-content-box' style='position:absolute;left:360px;width:120px;height:24px'>"
        f"{_PHRASE}</div><span>商家</span></li>"
        "<li class='onemsg' id='next-88442211' currentuid='uid-other-10000009' "
        "style='position:relative;width:640px;height:40px'>"
        "<div class='merchantMessage'><div class='msg-content-box' style='position:absolute;left:8px;width:80px;height:20px'>"
        "下一条结构</div></div><span>买家</span></li>"
    )


class _SilentPage:
    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, script: str, *args: object) -> dict:
        self.calls += 1
        raise AssertionError("geometry request must not fall back to export")

    def set_default_timeout(self, timeout: int) -> None:
        return None


class GeometryProtocolTest(unittest.TestCase):
    def test_script_does_not_touch_private_state(self) -> None:
        for word in ("localStorage", "sessionStorage", "cookie", "userinfo", "csinfosmap", "indexedDB"):
            self.assertNotIn(word, _GEOMETRY_JS)

    def test_unknown_op_and_fields_do_not_run_a_query(self) -> None:
        page = _SilentPage()
        with TemporaryDirectory() as tmp:
            data = Path(tmp)
            (data / "dom-diagnose.request").write_text('{"op":"geometry-write","probe_phrase":"x"}\n', encoding="utf-8")
            service_diagnose_request(page, data)
            rejected = json.loads((data / "dom-diagnose.result.json").read_text(encoding="utf-8"))
            self.assertEqual(page.calls, 0)
            self.assertEqual(rejected["status"], "op_rejected")
            self.assertEqual(rejected["handler"], "request-router")
            self.assertNotIn("nodes", rejected)
            (data / "dom-diagnose.request").write_text(
                '{"op":"geometry-readonly","probe_phrase":"客服测试2006","script":"return 1"}\n',
                encoding="utf-8",
            )
            service_diagnose_request(page, data)
            fields = json.loads((data / "dom-diagnose.result.json").read_text(encoding="utf-8"))
        self.assertEqual(page.calls, 0)
        self.assertEqual(fields["status"], "field_rejected")
        self.assertEqual(fields["rejected_fields"], ["script"])

    def test_eight_requests_dispatch_and_cover_geometry_errors(self) -> None:
        from playwright.sync_api import sync_playwright

        cases = {
            "ready": _page(_ready_rows()),
            "no-role": _page(
                "<ul><li class='chat-item active' currentuid='uid-conv-10000001' style='width:120px;height:20px'></li></ul>"
                "<li class='onemsg' currentuid='uid-conv-10000001' style='width:320px;height:30px'>"
                "<div class='msg-content-box buyer-item' style='width:90px;height:20px'>无角色短句</div></li>"
            ),
            "zero": _page(
                "<ul><li class='chat-item active' currentuid='uid-conv-10000001' style='width:120px;height:20px'></li></ul>"
                "<li class='onemsg' currentuid='uid-conv-10000001' style='width:0;height:0'>"
                "<div class='msg-content-box' style='width:0;height:0'>零矩形短句</div><span>客服</span></li>"
            ),
            "duplicate": _page(
                "<li class='onemsg' style='width:200px;height:20px'><div class='msg-content-box'>重复短句</div></li>"
                "<li class='onemsg' style='width:200px;height:20px'><div class='msg-content-box'>重复短句</div></li>"
            ),
            "mismatch": _page(
                "<ul><li class='chat-item active' id='7' currentuid='uid-sel-10000002' style='width:100px;height:20px'></li></ul>"
                "<li class='onemsg' id='msg-10000008' currentuid='uid-msg-10000001' style='width:300px;height:28px'>"
                "<div class='msg-content-box' style='width:100px;height:20px'>标识不一致</div><span>商家</span></li>"
            ),
        }
        phrases = {
            "ready": _PHRASE,
            "no-role": "无角色短句",
            "zero": "零矩形短句",
            "duplicate": "重复短句",
            "mismatch": "标识不一致",
        }
        with TemporaryDirectory() as tmp:
            data = Path(tmp)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page()
                self.assertNotIn("pinduoduo", page.url)
                widths: list[float] = []
                for index in range(8):
                    page.set_content(cases["ready"])
                    (data / "dom-diagnose.request").write_text(
                        json.dumps({"op": "geometry-readonly", "probe_phrase": phrases["ready"]}, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    service_diagnose_request(page, data)
                    report = json.loads((data / "dom-diagnose.result.json").read_text(encoding="utf-8"))
                    self.assertEqual(report["handler"], "geometry-readonly", index)
                    self.assertEqual(report["samples"], 8, index)
                    self.assertNotIn("nodes", report)
                    self.assertNotIn(_PHRASE, json.dumps(report, ensure_ascii=False))
                    self.assertNotIn("uid-conv-10000001", json.dumps(report, ensure_ascii=False))
                    widths.append(float(report["matched"]["bubble"]["width"]))
                ready = report
                self.assertEqual(ready["status"], "geometry_ready")
                self.assertEqual(ready["matched"]["role_label"], "商家")
                self.assertEqual(ready["matched"]["role_source"], "visible_allowlist_label")
                self.assertEqual(ready["matched"]["bubble_horizontal"], "right")
                self.assertEqual(ready["matched"]["avatar_side"], "left_of_bubble")
                self.assertEqual(ready["following"]["role_label"], "买家")
                self.assertEqual(ready["following"]["bubble_horizontal"], "left")
                self.assertEqual(ready["conversation_link"], "hash_match")
                self.assertEqual(ready["matched"]["id"]["format"], "token")
                self.assertIsNotNone(ready["matched"]["id"]["sha256"])
                self.assertFalse(ready["production_send_allowed"])
                self.assertFalse((data / "geometry-readonly-detail.json").exists())
                mean = round(sum(widths) / len(widths), 1)
                self.assertEqual(mean, widths[-1])

                def run(name: str) -> dict:
                    page.set_content(cases[name])
                    (data / "dom-diagnose.request").write_text(
                        json.dumps({"op": "geometry-readonly", "probe_phrase": phrases[name]}, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    service_diagnose_request(page, data)
                    return json.loads((data / "dom-diagnose.result.json").read_text(encoding="utf-8"))

                missing_role = run("no-role")
                self.assertEqual(missing_role["status"], "role_unproven")
                self.assertEqual(missing_role["matched"]["role_label"], "unproven")
                self.assertEqual(missing_role["matched"]["role_source"], "no_allowlist_label")
                self.assertIsNotNone(missing_role["matched"]["bubble"])
                zero = run("zero")
                self.assertEqual(zero["status"], "zero_rectangle")
                self.assertEqual(zero["error"], "zero_rectangle")
                self.assertEqual(zero["samples"], 8)
                duplicate = run("duplicate")
                self.assertEqual(duplicate["status"], "phrase_not_unique")
                self.assertEqual(duplicate["match_count"], 2)
                self.assertIsNone(duplicate["matched"]["bubble"])
                mismatch = run("mismatch")
                self.assertEqual(mismatch["status"], "uid_mismatch")
                self.assertEqual(mismatch["conversation_link"], "uid_mismatch")
                self.assertEqual(mismatch["selected"]["id"]["format"], "short_index_not_uid")
                self.assertIsNone(mismatch["selected"]["id"]["sha256"])
                self.assertEqual(mismatch["matched"]["id"]["format"], "token")
                (data / "dom-diagnose.request").write_text(
                    json.dumps(
                        {
                            "op": "geometry-readonly",
                            "probe_phrase": phrases["mismatch"],
                            "keep_local_detail": True,
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                page.set_content(cases["mismatch"])
                service_diagnose_request(page, data)
                detail = data / "geometry-readonly-detail.json"
                self.assertTrue(detail.is_file())
                self.assertEqual(stat.S_IMODE(detail.stat().st_mode), 0o600)
                stored = detail.read_text(encoding="utf-8")
                self.assertIn("uid-msg-10000001", stored)
                public = (data / "dom-diagnose.result.json").read_text(encoding="utf-8")
                self.assertNotIn("uid-msg-10000001", public)
                browser.close()


if __name__ == "__main__":
    unittest.main()
