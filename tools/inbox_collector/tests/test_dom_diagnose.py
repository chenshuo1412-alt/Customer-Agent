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
