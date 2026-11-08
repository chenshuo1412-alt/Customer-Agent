"""DOM structure export. No merchant page and no second browser."""

from __future__ import annotations

import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from inbox_collector.cli import main
from inbox_collector.dom_diagnose import export_rendered_dom, wait_for_diagnose_result


class FakePage:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[str] = []
        self.url = "http://127.0.0.1/fixture"

    def set_default_timeout(self, timeout: int) -> None:
        self.calls.append(f"timeout:{timeout}")

    def evaluate(self, script: str):
        self.calls.append("evaluate")
        if "cookie" in script or "localStorage" in script or "click(" in script:
            raise AssertionError("diagnose script is not read-only")
        if self.error:
            raise self.error
        return self.result

    def goto(self, url: str) -> None:
        raise AssertionError("diagnose must not navigate")

    def click(self, selector: str) -> None:
        raise AssertionError("diagnose must not click")


class DomDiagnoseTest(unittest.TestCase):
    def test_fields_absent_is_not_platform_unsupported(self) -> None:
        page = FakePage(
            {
                "nodes": [
                    {
                        "tag": "div",
                        "attributes": ["class", "data-v-abc", "tabindex"],
                        "class_structure": ["panel", "buyer-13800000000"],
                        "conversation_selected": None,
                        "depth": 1,
                        "child_element_count": 0,
                    }
                ]
            }
        )
        report = export_rendered_dom(page, timeout_seconds=1)
        self.assertEqual(report["status"], "fields_absent")
        self.assertTrue(report["nodes"])
        self.assertFalse(report["platform_unsupported"])
        self.assertEqual(report["selector_candidates"], [])
        self.assertFalse(report["selectors_inferred"])
        self.assertIn("data-v-", report["ignored_unstable_attributes"])
        self.assertIn("tabindex", report["ignored_unstable_attributes"])
        self.assertNotIn("data-v-abc", json.dumps(report))
        self.assertNotIn("13800000000", json.dumps(report))
        self.assertNotIn("goto", page.calls)

    def test_conversation_not_selected_is_distinct_from_empty(self) -> None:
        page = FakePage(
            {
                "nodes": [
                    {
                        "tag": "section",
                        "attributes": ["data-conversation-selected", "class"],
                        "class_structure": ["shell"],
                        "conversation_selected": False,
                        "depth": 1,
                        "child_element_count": 1,
                    }
                ]
            }
        )
        report = export_rendered_dom(page)
        self.assertEqual(report["status"], "conversation_not_selected")
        self.assertNotEqual(report["nodes"], [])
        self.assertIsNotNone(report["nodes"])
        self.assertFalse(report["platform_unsupported"])

    def test_merchant_shell_is_not_fields_absent_and_does_not_invent_identity(self) -> None:
        page = FakePage(
            {
                "nodes": [
                    {
                        "tag": "div",
                        "attributes": [
                            "class",
                            "id",
                            "title",
                            "href",
                            "aria-label",
                            "msgtimeformate",
                            "threeminutesthreshold",
                            "data-clipboard-text",
                            "data-v-abc",
                            "tabindex",
                        ],
                        "class_structure": ["chat-list-box"],
                        "path": ["chat-list-box"],
                        "conversation_selected": None,
                        "depth": 2,
                        "child_element_count": 3,
                    },
                    {
                        "tag": "div",
                        "attributes": ["class", "id", "msgtimeformate"],
                        "class_structure": ["chat-item"],
                        "path": ["chat-list-box", "chat-item"],
                        "conversation_selected": None,
                        "depth": 3,
                        "child_element_count": 0,
                    },
                    {
                        "tag": "div",
                        "attributes": ["class", "id", "msgtimeformate"],
                        "class_structure": ["chat-item"],
                        "path": ["chat-list-box", "chat-item"],
                        "conversation_selected": None,
                        "depth": 3,
                        "child_element_count": 0,
                    },
                    {
                        "tag": "div",
                        "attributes": ["class"],
                        "class_structure": ["chatWindowHeader"],
                        "path": ["chatWindowHeader"],
                        "conversation_selected": None,
                        "depth": 2,
                        "child_element_count": 0,
                    },
                ]
            }
        )
        report = export_rendered_dom(page)
        rendered = json.dumps(report, ensure_ascii=False)
        self.assertEqual(report["status"], "identity_unproven")
        self.assertNotEqual(report["reason"], "semantic_attributes_missing")
        self.assertEqual(report["identity"]["stable_message_id"], "unproven")
        self.assertEqual(report["identity"]["message_time"], "unproven")
        self.assertEqual(report["identity"]["reply_status"], "unproven")
        self.assertEqual(report["identity"]["direction"], "unproven")
        self.assertEqual(report["identity"]["id_attribute_name"], "present_value_unread")
        self.assertEqual(report["identity"]["time_attribute_name"], "present_value_unread")
        self.assertEqual(report["identity"]["fixture_attribute_names"], "absent")
        self.assertIn("chat-list-box", report["observed_shell_classes"])
        self.assertFalse(report["fixture_names_required"])
        self.assertFalse(report["production_mapping_validated"])
        self.assertFalse(report["collection_enabled"])
        self.assertFalse(report["selectors_inferred"])
        self.assertEqual(report["selector_candidates"], [])
        self.assertGreaterEqual(report["repeated_structures"][0]["count"], 2)
        self.assertEqual(report["repeated_structures"][0]["row_identity"], "unproven")
        self.assertNotIn("data-message-id", rendered)
        self.assertNotIn("data-v-abc", rendered)
        self.assertNotIn("买家", rendered)

    def test_semantic_names_do_not_include_values(self) -> None:
        page = FakePage(
            {
                "nodes": [
                    {
                        "tag": "article",
                        "attributes": ["data-message-id", "href"],
                        "class_structure": ["card"],
                        "conversation_selected": True,
                        "depth": 2,
                        "child_element_count": 0,
                    }
                ]
            }
        )
        report = export_rendered_dom(page)
        self.assertEqual(report["status"], "structure_observed")
        rendered = json.dumps(report)
        self.assertNotIn("http", rendered)
        self.assertNotIn("token=", rendered)
        self.assertIn("data-message-id", report["nodes"][0]["attributes"])

    def test_evaluate_failure_is_an_error_with_no_node_list(self) -> None:
        report = export_rendered_dom(FakePage(error=RuntimeError("closed")))
        self.assertEqual(report["status"], "diagnostic_error")
        self.assertIsNone(report["nodes"])
        self.assertFalse(report["platform_unsupported"])

    def test_empty_node_list_is_not_treated_as_unsupported(self) -> None:
        report = export_rendered_dom(FakePage({"nodes": []}))
        self.assertEqual(report["status"], "diagnostic_error")
        self.assertEqual(report["reason"], "no_rendered_elements")
        self.assertIsNone(report["nodes"])
        self.assertFalse(report["platform_unsupported"])

    def test_raw_identifier_in_assessment_is_dropped(self) -> None:
        page = FakePage(
            {
                "nodes": [
                    {
                        "tag": "div",
                        "attributes": ["class"],
                        "class_structure": ["chat-list-box"],
                        "path": ["chat-list-box"],
                        "depth": 1,
                        "child_element_count": 2,
                    }
                ],
                "value_assessment": {
                    "groups": [
                        {
                            "tag": "div",
                            "class_structure": ["chat-item"],
                            "path": ["chat-list-box"],
                            "rendered_count": 2,
                            "visible_count": 2,
                            "hidden_count": 0,
                            "virtual_list": False,
                            "id": {
                                "present": 2,
                                "distinct": 2,
                                "duplicated": 0,
                                "uniqueness": "msg-882211",
                                "stability": "unproven_single_snapshot",
                            },
                            "time": {},
                            "labels": {},
                        }
                    ],
                    "shell_ids": {},
                },
            }
        )
        report = export_rendered_dom(page)
        self.assertNotIn("msg-882211", json.dumps(report))
        self.assertEqual(report["value_assessment"]["groups"][0]["id"]["uniqueness"], "insufficient")
        self.assertFalse(report["value_assessment"]["can_use_for_dedupe"])
        self.assertFalse(report["value_assessment"]["can_treat_time_as_message_time"])

    def test_frame_url_and_false_truncation_flag_are_not_trusted(self) -> None:
        page = FakePage(
            {
                "nodes": [
                    {
                        "tag": "div",
                        "attributes": ["class"],
                        "class_structure": ["chat-list-box"],
                        "path": ["chat-list-box"],
                        "depth": 2,
                        "child_element_count": 0,
                    }
                ],
                "truncated": False,
                "coverage": {
                    "top_page": "https://mms.pinduoduo.com/chat-merchant/index.html?user=secret",
                    "depth_limit": 64,
                    "max_depth_reached": 8,
                    "incomplete": False,
                    "reasons": ["visit_limit", "https://example.invalid/leak"],
                },
                "frames": {
                    "items": [
                        {
                            "category": "https://mms.pinduoduo.com/login/?token=raw",
                            "reason": "https://evil.example/path",
                            "entered": True,
                            "nesting": 1,
                            "src": "https://mms.pinduoduo.com/chat-merchant/index.html?buyer=1",
                        }
                    ]
                },
                "message_region": {
                    "reached": False,
                    "landmarks": [],
                    "conversation_list_landmarks": ["chat-list-box", "买家张三"],
                },
            }
        )
        report = export_rendered_dom(page)
        rendered = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("http", rendered)
        self.assertNotIn("token", rendered)
        self.assertNotIn("张三", rendered)
        self.assertNotIn("buyer", rendered)
        self.assertTrue(report["truncated"])
        self.assertTrue(report["coverage"]["incomplete"])
        self.assertEqual(report["coverage"]["reasons"], ["visit_limit"])
        self.assertEqual(report["coverage"]["top_page"], "off_scope")
        self.assertEqual(report["frames"]["items"][0]["category"], "off_scope")
        self.assertFalse(report["frames"]["items"][0]["entered"])
        self.assertFalse(report["message_region"]["reached"])
        self.assertEqual(report["message_region"]["conversation_list_landmarks"], ["chat-list-box"])
        self.assertFalse(report["message_region"]["conversation_list_counted_as_messages"])
        self.assertEqual(report["status"], "coverage_incomplete")
        self.assertFalse(report["platform_unsupported"])
        self.assertFalse(report["production_mapping_validated"])

    def test_command_does_not_launch_a_browser_when_nobody_is_listening(self) -> None:
        from unittest import mock

        with TemporaryDirectory() as directory:
            data = Path(directory)
            with mock.patch(
                "inbox_collector.browser_session.DedicatedBrowser.start",
                side_effect=AssertionError("browser launched"),
            ):
                code = main(["diagnose-dom", "--data-dir", str(data), "--timeout-seconds", "0.2"])
            self.assertEqual(code, 2)
            result = wait_for_diagnose_result(data, timeout_seconds=0.1)
            self.assertEqual(result["status"], "diagnostic_error")
            self.assertEqual(result["reason"], "listener_not_running")
            self.assertIsNone(result["nodes"])
            self.assertFalse(result["platform_unsupported"])
