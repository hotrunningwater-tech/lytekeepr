"""The Claude connector's tool list: one malformed tool makes tools/list fail and Claude then sees no LyteKeepr tools at all."""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lk_calendar import claude_connector  # noqa: E402


class ToolListTest(unittest.TestCase):
    def test_tool_list_builds_and_is_valid(self):
        tools = claude_connector.tool_list()
        self.assertEqual(len(tools), len(claude_connector.TOOLS))
        names = [t["name"] for t in tools]
        self.assertEqual(len(names), len(set(names)), "duplicate tool names")
        for t in tools:
            self.assertTrue(t["name"] and t["title"] and t["description"], t["name"])
            schema = t["inputSchema"]
            self.assertEqual(schema.get("type"), "object", t["name"])
            for req in schema.get("required", []):
                self.assertIn(req, schema.get("properties", {}), f"{t['name']}: required {req} has no property")
            json.dumps(t)                                   # everything must be plain JSON

    def test_autopilot_tools_present(self):
        names = {t["name"] for t in claude_connector.tool_list()}
        for n in ("autopilot_queue", "save_meeting_brief", "save_call_summary", "save_followup_draft", "save_message_review", "save_onboarding"):
            self.assertIn(n, names)


if __name__ == "__main__":
    unittest.main()
