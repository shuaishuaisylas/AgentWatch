from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agentwatch.bridge import append_private
from agentwatch.collector import Collector, RecordParser
from agentwatch.common import timestamp
from agentwatch.cursor_bridge import sanitize
from agentwatch.navigation import NavigationError, jump
from agentwatch.store import Store
from agentwatch.subscriptions import Subscriptions


AT = timestamp("2026-10-16T10:00:00+08:00")
SECRET = "ARTIFICIAL_PRIVATE_TEXT_NOT_FOR_STORAGE"


class CursorEventTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "data/watch.sqlite")
        self.parser = RecordParser(self.store)
        self.clock = patch("agentwatch.cursor_events.now", return_value=AT + 1000)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def event(self, event, at=AT, generation="generation-1", **extra):
        row = sanitize({"hook_event_name": event, "conversation_id": "synthetic-conversation",
                        "generation_id": generation, "workspace_roots": [str(self.root / "example")],
                        "cursor_version": "3.24.12", "prompt": SECRET, "text": SECRET,
                        "tool_input": {"private": SECRET}, "tool_output": SECRET, **extra}, at=at)
        return self.parser.lifecycle(row, {})

    def row(self):
        return self.store.db.execute("SELECT * FROM sessions WHERE tool='cursor'").fetchone()

    def snapshot(self, current=AT + 20):
        return self.store.snapshot({"tool": "cursor", "role": "main"}, {}, current)

    def test_submission_is_unknown_then_observed_work_idle_and_session_close(self):
        self.event("sessionStart")
        self.event("beforeSubmitPrompt", AT + 1)
        self.assertEqual(self.row()["state"], "unknown")
        self.assertEqual(self.snapshot()["counts"]["active"], 0)
        for offset, event in enumerate(["afterAgentThought", "postToolUse", "preCompact"], 2):
            self.event(event, AT + offset)
            snap = self.snapshot()
            self.assertEqual(snap["counts"]["active"], 1)
            self.assertEqual(snap["sessions"][0]["liveness"], "unknown")
            self.assertEqual(snap["sessions"][0]["role"], "main")
        self.event("stop", AT + 6, status="completed")
        self.assertEqual((self.row()["state"], self.row()["last_completed_at"]), ("idle", AT + 6))
        self.event("sessionEnd", AT + 7, reason="user_close")
        self.assertEqual((self.row()["state"], self.row()["liveness"]), ("ended", "offline"))

    def test_usage_and_human_attention_remain_unknown_even_after_completion(self):
        self.event("postToolUse")
        self.event("stop", AT + 1, status="completed")
        snapshot = self.snapshot()
        card = snapshot["sessions"][0]
        self.assertEqual(card["coverage"], "status_only")
        self.assertEqual(card["attention"]["unknown"], 1)
        self.assertFalse(card["attention"]["pending"])
        self.assertFalse(card["inbox"]["unread_completed"])
        self.assertEqual(snapshot["inbox_counts"]["unread_completed"], 0)
        self.assertIsNone(card["usage"]["observed_total"])
        self.assertIsNone(card["usage"]["cumulative"])
        self.assertIsNone(card["context_percent"])
        platform = next(p for p in Subscriptions(self.store).snapshot()["platforms"] if p["tool"] == "cursor")
        self.assertEqual((platform["status"], platform["limits"], platform["updated_at"]), ("unavailable", [], None))
        self.assertNotIn(SECRET, "\n".join(self.store.db.iterdump()))

    def test_cancel_error_and_missing_status_do_not_invent_completion(self):
        for i, status in enumerate(["aborted", "error", "unknown"]):
            generation = "generation-" + str(i)
            self.event("beforeSubmitPrompt", AT + i * 10, generation)
            self.event("postToolUse", AT + i * 10 + 1, generation)
            self.event("stop", AT + i * 10 + 2, generation, status=status)
            self.assertEqual(self.row()["state"], {"aborted": "idle", "error": "error", "unknown": "unknown"}[status])
            self.assertIsNone(self.row()["last_completed_at"])

    def test_tool_failure_can_be_recovered_and_permission_denied_is_not_a_human_request(self):
        self.event("postToolUseFailure", failure_type="permission_denied")
        self.assertEqual(self.row()["state"], "active")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM interaction_requests").fetchone()[0], 0)
        self.event("postToolUse", AT + 1)
        self.event("stop", AT + 2, status="completed")
        self.assertEqual(self.row()["state"], "idle")

    def test_late_events_cannot_revive_closed_turn_or_stop_a_newer_generation(self):
        self.event("postToolUse")
        self.event("stop", AT + 1, status="completed")
        for event in ["postToolUse", "afterAgentThought", "beforeSubmitPrompt"]:
            self.event(event, AT + 2)
            self.assertEqual(self.row()["state"], "idle")
        self.event("beforeSubmitPrompt", AT + 3, "generation-2")
        self.event("afterAgentThought", AT + 4, "generation-2")
        self.event("beforeSubmitPrompt", AT + 4.5, "generation-2")
        self.assertEqual(self.row()["state"], "active")
        for event in ["stop", "sessionEnd", "postToolUse", "preCompact"]:
            self.event(event, AT + 5, "generation-1", status="completed", reason="completed")
            self.assertEqual(self.row()["state"], "active")
        self.assertEqual(self.row()["last_completed_at"], AT + 1)
        # A late creation event must not replace either the head or its state.
        self.event("sessionStart", AT + 6, "generation-1")
        self.assertEqual(self.row()["state"], "active")

    def test_duplicate_and_older_events_are_idempotent_and_stale_work_is_unknown(self):
        row = sanitize({"hook_event_name": "postToolUse", "conversation_id": "synthetic-conversation",
                        "generation_id": "generation-1"}, at=AT)
        self.parser.lifecycle(row, {})
        self.parser.lifecycle(row, {})
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM activity").fetchone()[0], 1)
        self.event("stop", AT - 1, status="completed")
        self.assertEqual(self.row()["state"], "active")
        self.assertIsNone(self.row()["last_completed_at"])
        self.assertEqual(self.snapshot(AT + 121)["counts"]["active"], 0)
        self.assertTrue(self.snapshot(AT + 121)["sessions"][0]["stale"])

    def test_session_close_without_stop_blocks_late_work_until_a_new_submission(self):
        self.event("beforeSubmitPrompt")
        self.event("postToolUse", AT + 1)
        self.event("sessionEnd", AT + 2, reason="user_close")
        self.event("afterAgentThought", AT + 3)
        self.assertEqual((self.row()["state"], self.row()["liveness"]), ("ended", "offline"))
        self.assertEqual(self.snapshot()["counts"]["active"], 0)
        self.assertIsNone(self.row()["last_completed_at"])
        self.event("beforeSubmitPrompt", AT + 4, "generation-2")
        self.event("postToolUse", AT + 5, "generation-2")
        self.assertEqual((self.row()["state"], self.row()["liveness"]), ("active", "unknown"))

    def test_missing_generation_and_future_or_child_transport_are_rejected(self):
        row = {"agentwatch": 1, "tool": "cursor", "event": "stop", "id": "synthetic-event",
               "session_id": "synthetic-conversation", "at": AT, "stop_reason": "completed"}
        self.assertIsNone(self.parser.lifecycle(row, {}))
        for extra in [{"at": AT + 2000}, {"parent_conversation_id": "parent"}, {"subagent_id": "child"}, {"parent_tool_call_id": "task"}]:
            self.assertIsNone(self.parser.lifecycle(dict(row, turn_id="generation-1", **extra), {}))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)

    def test_generation_survives_restart_and_transport_is_drained(self):
        collector = Collector(self.store, self.root / "codex", self.root / "claude", self.root / "data",
                              self.root / "desktop", self.root / "workbuddy", self.root / "cursor")
        spool = self.root / "data/lifecycle-events.jsonl"
        append_private(spool, sanitize({"hook_event_name": "beforeSubmitPrompt", "conversation_id": "synthetic-conversation",
                                       "generation_id": "generation-2"}, at=AT))
        append_private(spool, sanitize({"hook_event_name": "afterAgentThought", "conversation_id": "synthetic-conversation",
                                       "generation_id": "generation-2", "text": SECRET}, at=AT + 1))
        collector.read_file(str(spool), "lifecycle", None)
        self.assertEqual(spool.stat().st_size, 0)
        self.store.db.commit()
        self.store.close()
        self.store = Store(self.root / "data/watch.sqlite")
        self.parser = RecordParser(self.store)
        self.event("stop", AT + 2, "generation-1", status="completed")
        self.assertEqual(self.row()["state"], "active")
        self.assertNotIn(SECRET, "\n".join(self.store.db.iterdump()))

    def test_jump_is_application_only_and_cannot_use_another_platform_route(self):
        self.event("postToolUse")
        info = self.snapshot()["sessions"][0]["jump"]
        self.assertEqual(info, {"kind": "cursor", "label": "打开 Cursor", "precision": "应用", "available": True})
        commands = []
        def runner(command, **kwargs):
            commands.append(command)
            return SimpleNamespace(returncode=0)
        session = dict(self.row())
        with patch("agentwatch.navigation.sys.platform", "darwin"):
            self.assertTrue(jump(session, "cursor", runner)["opened"])
            with self.assertRaises(NavigationError):
                jump(session, "claude", runner)
        self.assertEqual(commands, [["open", "-a", "Cursor"]])


if __name__ == "__main__":
    unittest.main()
