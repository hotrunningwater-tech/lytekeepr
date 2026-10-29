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
        for n in ("autopilot_queue", "save_meeting_prep", "save_call_summary", "save_followup_draft", "save_message_review", "save_onboarding"):
            self.assertIn(n, names)

    def test_every_tool_named_in_instructions_exists(self):
        """The scheduled run and the tools themselves point Claude to other tools by name: each of those must exist."""
        import re
        from lk_calendar import autopilot, inbox_watch, onboarding
        names = {t["name"] for t in claude_connector.tool_list()}
        text = json.dumps([autopilot.INSTRUCTIONS, autopilot.PROMPT, inbox_watch.INSTRUCTIONS, onboarding.INSTRUCTIONS,
                           [t["description"] for t in claude_connector.TOOLS], claude_connector.RUN_ONLY_TOOLS])
        mentioned = set(re.findall(r"\b(?:get|save|list|search|send|create|update|set|add|open|discard|email|last|find|move|mark|link|write|clear|book|cancel|reply|log|edit|delete|doc|docs|tab|folder|accept|dismiss|who|calls|snooze|contacts|autopilot)_[a-z_]+\b", text))
        not_tools = {"add_note", "find_replace", "calls_needing_summary", "calls_without_contact"}     # fields and queue sections
        unknown = sorted(m for m in mentioned if m not in names and m not in not_tools and not m.endswith("_id") and not m.endswith("_ids"))
        self.assertEqual(unknown, [], "tools named in descriptions or instructions that don't exist")

    def test_run_only_tools_need_the_key(self):
        def call(method, path, body=None):
            if "run-key" in path:
                return 200, {"ok": False}
            raise AssertionError("must not get past the key check: " + path)
        for name in claude_connector.RUN_ONLY_TOOLS:
            out = claude_connector._run_tool(call, name, {"run_key": "nope", "contact_id": 1, "reviewed": [], "subject": "s", "body": "b"})
            self.assertFalse(out.get("saved"), name)

if __name__ == "__main__":
    unittest.main()
