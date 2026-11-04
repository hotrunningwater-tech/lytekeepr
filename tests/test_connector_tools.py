"""The Claude connector's tool list: one malformed tool makes tools/list fail and Claude then sees no LyteKeepr tools at all."""

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lk_calendar import claude_connector  # noqa: E402


ROOT = Path(__file__).resolve().parent.parent


class ToolListTest(unittest.TestCase):
    def test_tool_list_builds_and_is_valid(self):
        self.assertEqual(len(claude_connector.tool_list(work=True)), len(claude_connector.TOOLS))
        off = claude_connector.tool_list()
        self.assertFalse({t["name"] for t in off} & claude_connector.WORK_TOOLS, "Claude sees Work tools where Work pipelines are off")
        cf = next(t for t in off if t["name"] == "create_funnel")
        self.assertFalse({"kind", "item_name", "stages"} & set(cf["inputSchema"]["properties"]), "Claude sees Work options where Work pipelines are off")
        tools = claude_connector.tool_list(work=True)
        names = [t["name"] for t in tools]
        self.assertEqual(len(names), len(set(names)), "duplicate tool names")
        for t in tools:
            self.assertTrue(t["name"] and t["title"] and t["description"], t["name"])
            schema = t["inputSchema"]
            self.assertEqual(schema.get("type"), "object", t["name"])
            for req in schema.get("required", []):
                self.assertIn(req, schema.get("properties", {}), f"{t['name']}: required {req} has no property")
            json.dumps(t)                                   # everything must be plain JSON

    def test_tool_list_is_the_agreed_one(self):
        """No tool comes or goes unnoticed, and nothing old is left behind: the code's tools must match lk_calendar/claude_tools.txt."""
        listed = [line.split("|")[0].strip() for line in (ROOT / "lk_calendar" / "claude_tools.txt").read_text().splitlines()
                  if line.strip() and not line.startswith("#")]
        names = [t["name"] for t in claude_connector.TOOLS]
        self.assertEqual(len(listed), len(set(listed)), "a tool is listed twice in claude_tools.txt")
        self.assertEqual(sorted(set(names) - set(listed)), [], "new tools not in claude_tools.txt: add them there, and remove what they replace")
        self.assertEqual(sorted(set(listed) - set(names)), [], "tools in claude_tools.txt that the code no longer has: take them off the list")

    def test_autopilot_tools_present(self):
        names = {t["name"] for t in claude_connector.tool_list()}
        for n in ("autopilot_queue", "save_meeting_prep", "save_call_summary", "save_followup_draft", "save_message_review", "save_onboarding"):
            self.assertIn(n, names)

    def test_every_tool_named_in_instructions_exists(self):
        """The scheduled run and the tools themselves point Claude to other tools by name: each of those must exist."""
        import re
        from lk_calendar import autopilot, inbox_watch, onboarding
        names = {t["name"] for t in claude_connector.tool_list(work=True)}
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

    def test_every_change_is_shown_first(self):
        """No tool that adds, changes or deletes does anything without the code from showing exactly that change (the Katie/Vicky rule)."""
        def call(method, path, body=None):
            if method != "GET" and not (path.endswith("/follow-ups/clear") and (body or {}).get("preview")):
                raise AssertionError(f"changed something before the user saw it: {method} {path}")
            return 200, {"funnels": [{"funnel_id": 1, "name": "Pilot"}], "brands": [{"key": "nexy", "name": "Nexy"}], "overdue": [], "suggestions": [],
                         "count": 0, "follow_ups": [], "ok": False,
                         "drafts": [{"draft_id": 1, "kind": "manual", "to_email": "a@b.c", "subject": "s", "body": "b"}]}
        args = {"contact_id": 1, "item_id": 1, "task_id": 1, "opportunity_id": 1, "doc_id": 1, "folder_id": 1, "record_id": 1, "goal_id": 1, "call_id": 1,
                "company_id": 1, "suggestion_id": 1, "stage_id": 1, "lead_ids": [1], "draft": "one:1", "name": "New name", "brand": "nexy", "title": "t",
                "date": "2026-11-02", "text": "hello", "stage": "Contacted", "email": "x@y.z", "user_said": "save it", "tab": "Main", "status": "completed"}
        for name in sorted(claude_connector.SHOWN_FIRST):
            out = claude_connector._run_tool(call, name, dict(args))
            self.assertIn("show_the_user", out, name)
            self.assertTrue(out["confirm_code"], name)
            wrong = claude_connector._run_tool(call, name, {**args, "confirm_code": "nope"})
            self.assertIn("show_the_user", wrong, name)                    # a code for something else does nothing
            other = claude_connector._run_tool(call, name, {**args, "title": "something else", "text": "else", "date": "2026-12-01"})
            if {"title", "text", "date"} & set(claude_connector.TOOLS[[t["name"] for t in claude_connector.TOOLS].index(name)]["inputSchema"]["properties"]):
                self.assertNotEqual(other["confirm_code"], out["confirm_code"], name)

    def test_change_runs_with_its_code(self):
        writes = []
        def call(method, path, body=None):
            if method != "GET":
                writes.append((method, path, body))
                return 201, {"id": 7, "title": "Send Anna the demo"}
            return 200, {"display": "Anna Lee", "email": "anna@newco.com", "brand": "nexy", "brands": [{"key": "nexy", "name": "Nexy"}]}
        args = {"title": "Send Anna the demo", "date": "2026-11-02", "contact_id": 3}
        shown = claude_connector._run_tool(call, "create_task", args)
        self.assertIn("Anna Lee <anna@newco.com>", shown["show_the_user"])
        self.assertEqual(writes, [])
        claude_connector._run_tool(call, "create_task", {**args, "confirm_code": shown["confirm_code"]})
        self.assertEqual(len(writes), 1)

    def test_personal_task_has_no_brand_or_contact(self):
        writes = []
        def call(method, path, body=None):
            if method != "GET":
                writes.append(body)
            return 200, {"brands": [{"key": "nexy", "name": "Nexy"}]}
        args = {"title": "Call mom", "date": "2026-11-02", "personal": True}
        shown = claude_connector._run_tool(call, "create_task", args)
        self.assertIn("Personal (only you see it): yes", shown["show_the_user"])
        claude_connector._run_tool(call, "create_task", {**args, "confirm_code": shown["confirm_code"]})
        self.assertEqual(writes[0]["project"], "personal")
        self.assertNotIn("lead_ref", writes[0])
        refused = claude_connector._run_tool(call, "create_task", {**args, "contact_id": 3})
        self.assertNotIn("show_the_user", refused)

    def test_reads_never_ask(self):
        reads = {t["name"] for t in claude_connector.TOOLS if t["read"]}
        self.assertFalse(reads & claude_connector.SHOWN_FIRST)
        self.assertEqual(claude_connector.SHOWN_FIRST | reads | claude_connector.SENDS | set(claude_connector.RUN_ONLY_TOOLS),
                         {t["name"] for t in claude_connector.TOOLS})

    def test_scheduled_run_saves_prep_with_its_key(self):
        writes = []
        def call(method, path, body=None):
            if "run-key" in path:
                return 200, {"ok": "good" in path}
            if method != "GET":
                writes.append(path)
            return 200, {"ok": True}
        out = claude_connector._run_tool(call, "save_meeting_prep", {"item_id": 1, "text": "prep", "run_key": "bad"})
        self.assertIn("show_the_user", out)
        claude_connector._run_tool(call, "save_meeting_prep", {"item_id": 1, "text": "prep", "run_key": "good"})
        self.assertEqual(writes, ["/api/calendar/events/1/prep"])


if __name__ == "__main__":
    unittest.main()
