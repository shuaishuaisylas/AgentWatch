from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from agentwatch.collector import Collector, RecordParser, workbuddy_transition
from agentwatch.common import timestamp
from agentwatch.configuration import ConfigurationError
from agentwatch.lifecycle_bridge import sanitize
from agentwatch.lifecycle_configuration import LifecycleConfiguration
from agentwatch.store import Store


AT = timestamp("2026-10-15T10:00:00+08:00")
SECRET = "PRIVATE_PROMPT_TOOL_RESPONSE_TOKEN_SHOULD_NEVER_PERSIST"


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "data/watch.sqlite")
        self.parser = RecordParser(self.store)
        self.collector = Collector(self.store, self.root / "codex", self.root / "claude", self.root / "data",
                                   self.root / "desktop", workbuddy_home=self.root / "workbuddy")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def event(self, event, tool="codex", at=AT, identity=None, **extra):
        doc = {"session_id": "session", "hook_event_name": event, "turn_id": "turn1", "cwd": str(self.root), **extra}
        row = sanitize(doc, tool, at=at)
        if identity:
            row["id"] = identity
        return self.parser.lifecycle(row, {})

    def state(self, sid="codex:session"):
        return self.store.db.execute("SELECT state FROM sessions WHERE id=?", (sid,)).fetchone()[0]

    def native(self, signal, target, at=AT, source_id="session"):
        path = self.root / "workbuddy/logs/2026-10-15/sdk/conversations" / (source_id + ".log")
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(at, timezone.utc).isoformat().replace("+00:00", "Z")
        with path.open("a") as output:
            output.write(stamp + " state-machine:transition " + json.dumps({"conversationId": source_id,
                         "input": signal, "from": "working", "to": target, "extra": SECRET}) + "\n")
        self.collector.read_file(str(path), "workbuddy_state", "workbuddy:" + source_id)
        return path

    def test_work_approval_reply_continue_idle_and_end_are_distinct(self):
        for tool in ["codex", "workbuddy"]:
            sid = self.event("SessionStart", tool, AT)
            self.assertEqual(self.state(sid), "unknown")
            for offset, event, expected, extra in [
                (1, "UserPromptSubmit", "active", {}),
                (2, "PreToolUse", "active", {"tool_use_id": "tool1", "tool_name": "Bash"}),
                (3, "PermissionRequest", "waiting_approval", {"tool_use_id": "tool1"}),
                (4, "PostToolUse", "active", {"tool_use_id": "tool1"}),
                (5, "PreToolUse", "waiting_user", {"tool_name": "request_user_input"}),
                (5.5, "PermissionRequest", "waiting_user", {"tool_name": "AskUserQuestion"}),
                (6, "PostToolUse", "active", {}),
                (7, "Stop", "idle", {}),
                (8, "SessionEnd", "ended", {})]:
                self.event(event, tool, AT + offset, **extra)
                self.assertEqual(self.state(sid), expected)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 2)

    def test_closed_turn_cannot_be_reactivated_by_delayed_tool_or_permission(self):
        self.event("UserPromptSubmit")
        self.event("Stop", at=AT + 2)
        self.event("PermissionRequest", at=AT + 3)
        self.event("PostToolUse", at=AT + 4)
        self.assertEqual(self.state(), "idle")
        self.event("UserPromptSubmit", at=AT + 5, turn_id="turn2")
        self.assertEqual(self.state(), "active")

    def test_duplicate_and_delayed_closed_tool_events_are_safe(self):
        self.event("PostToolUse", identity="one", tool_use_id="tool1")
        self.event("PermissionRequest", at=AT + 1, tool_use_id="tool1")
        self.assertEqual(self.state(), "active")
        self.event("Stop", at=AT + 2)
        self.event("PostToolUse", at=AT + 3, identity="one", turn_id="different-turn")
        self.assertEqual(self.state(), "idle")

    def test_subagents_keep_their_parent_and_do_not_end_the_parent(self):
        self.event("UserPromptSubmit")
        self.event("SubagentStart", at=AT + 1, agent_id="child")
        row = self.store.db.execute("SELECT role,parent_id,state FROM sessions WHERE id='codex:child'").fetchone()
        self.assertEqual(tuple(row), ("child", "codex:session", "active"))
        self.event("SubagentStop", at=AT + 2, agent_id="child")
        self.assertEqual(self.state("codex:child"), "idle")
        self.assertEqual(self.state(), "active")
        self.assertEqual(self.store.db.execute("SELECT role FROM sessions WHERE id='codex:session'").fetchone()[0], "unknown")

    def test_compaction_does_not_mark_a_running_thread_idle(self):
        self.event("UserPromptSubmit")
        self.event("SessionStart", at=AT + .5, source="startup")
        self.assertEqual(self.state(), "active")
        self.event("SessionStart", at=AT + 1, source="compact")
        self.assertEqual(self.state(), "active")
        self.event("Interrupt", at=AT + 2)
        self.assertEqual(self.state(), "idle")

    def test_native_workbuddy_sandbox_approval_resume_and_idle(self):
        sid = "workbuddy:session"
        self.native("PROMPT_SENT", "working")
        self.event("PreToolUse", tool="workbuddy", at=AT + 1, tool_name="Bash")
        self.native("PERMISSION_REQUESTED", "pending", AT + 2)
        self.assertEqual(self.state(sid), "waiting_approval")
        self.store.state(sid, "unknown", "记录不能覆盖明确审批", AT + 2.5)
        self.assertEqual(self.state(sid), "waiting_approval")
        self.native("PERMISSION_RESOLVED", "working", AT + 3)
        self.assertEqual(self.state(sid), "active")
        self.event("Stop", tool="workbuddy", at=AT + 4)
        self.native("TURN_COMPLETED", "idle", AT + 5)
        self.event("SessionEnd", tool="workbuddy", at=AT + 6)
        self.assertEqual(self.state(sid), "idle")
        self.assertEqual(self.store.db.execute("SELECT liveness FROM sessions WHERE id=?", (sid,)).fetchone()[0], "unknown")
        self.native("PROMPT_SENT", "working", AT + 10)
        self.event("Stop", tool="workbuddy", at=AT + 11, turn_id="second")
        self.event("SessionEnd", tool="workbuddy", at=AT + 12, turn_id="second")
        self.native("TURN_COMPLETED", "idle", AT + 11.5)
        self.assertEqual(self.state(sid), "idle")
        self.assertEqual(self.store.db.execute("SELECT state_source FROM sessions WHERE id=?", (sid,)).fetchone()[0], "event")

    def test_native_wait_does_not_overwrite_a_newer_question_hook(self):
        self.event("PermissionRequest", tool="workbuddy", at=AT + 2, tool_name="AskUserQuestion")
        self.native("PERMISSION_REQUESTED", "pending", AT + 1.9)
        self.assertEqual(self.state("workbuddy:session"), "waiting_user")

    def test_native_logs_only_accept_typed_transitions_and_preserve_privacy(self):
        path = self.native("PROMPT_SENT", "working")
        self.native("PHASE_IDLE", "working", AT + 1)
        self.assertEqual(self.state("workbuddy:session"), "active")
        count = self.store.db.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
        self.store.db.execute("DELETE FROM files WHERE path=?", (str(path),))
        self.collector.read_file(str(path), "workbuddy_state", "workbuddy:session")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM activity").fetchone()[0], count)
        self.assertNotIn(SECRET, "\n".join(self.store.db.iterdump()))
        self.assertIsNone(workbuddy_transition(b'2026-10-15T02:00:00Z other {"input":"PERMISSION_REQUESTED"}', "session"))
        self.assertIsNone(workbuddy_transition(b'2026-10-15T02:00:00Z state-machine:transition []', "session"))
        outside = self.root / "outside.log"
        outside.write_text(path.read_text())
        self.assertEqual(self.collector.read_file(str(outside), "workbuddy_state", None), (0, False))
        linked = path.parent / "linked.log"
        linked.symlink_to(outside)
        self.assertFalse(self.collector.permitted_workbuddy_state(linked))

    def test_native_log_for_a_new_task_is_discovered_on_each_scan(self):
        with patch("agentwatch.collector.now", return_value=AT):
            self.collector.discover()
        path = self.native("PERMISSION_REQUESTED", "pending", AT + 1)
        self.store.db.execute("DELETE FROM files WHERE path=?", (str(path),))
        with patch("agentwatch.collector.now", return_value=AT + 2), patch.object(self.collector, "probe"):
            self.collector.scan()
        self.assertEqual(self.collector.discovered[str(path.resolve())][0], "workbuddy_state")
        self.assertEqual(self.state("workbuddy:session"), "waiting_approval")

    def test_records_cannot_overwrite_precise_hooks_and_stale_active_is_unknown(self):
        sid = self.event("UserPromptSubmit")
        self.store.update(sid, {"liveness": "online"})
        self.parser.codex({"type": "event_msg", "timestamp": AT + 1, "payload": {"type": "task_complete", "turn_id": "turn1"}}, {"sid": sid})
        self.assertEqual(self.state(), "active")
        snap = self.store.snapshot({"role": "all"}, {}, AT + 2)
        self.assertEqual(snap["counts"]["active"], 1)
        stale = self.store.snapshot({"role": "all"}, {}, AT + 130)
        self.assertEqual(stale["sessions"][0]["state"], "unknown")
        self.assertTrue(stale["sessions"][0]["stale"])

    def test_recent_precise_event_confirms_work_without_a_process_binding(self):
        self.event("UserPromptSubmit", tool="workbuddy")
        snapshot = self.store.snapshot({"role": "all"}, {}, AT + 1)
        self.assertEqual(snapshot["counts"]["active"], 1)
        self.assertEqual(snapshot["counts"]["online"], 0)
        self.store.update("workbuddy:session", {"liveness": "offline"})
        self.assertEqual(self.store.snapshot({"role": "all"}, {}, AT + 1)["counts"]["active"], 0)
        self.event("UserPromptSubmit", tool="workbuddy", at=AT + 2, turn_id="new-run")
        resumed = self.store.snapshot({"role": "all"}, {}, AT + 3)
        self.assertEqual(resumed["counts"]["active"], 1)
        self.assertEqual(resumed["sessions"][0]["liveness"], "unknown")
        self.store.update("workbuddy:session", {"liveness": "offline"})
        self.event("PermissionRequest", tool="workbuddy", at=AT + 4, turn_id="new-run", tool_name="AskUserQuestion")
        waiting = self.store.snapshot({"role": "all"}, {}, AT + 5)
        self.assertEqual(waiting["sessions"][0]["state"], "waiting_user")
        self.assertEqual(waiting["sessions"][0]["liveness"], "unknown")

    def test_transcript_flushed_after_stop_does_not_suppress_lifecycle_events(self):
        from agentwatch.bridge import append_private
        folder = self.root / "codex/sessions"
        folder.mkdir(parents=True)
        transcript = folder / "demo.jsonl"
        transcript.write_text("\n".join(json.dumps(row) for row in [
            {"type": "session_meta", "payload": {"id": "session", "source": "cli"}},
            {"type": "event_msg", "timestamp": AT + 1.1, "payload": {"type": "task_started", "turn_id": "turn1"}},
            {"type": "event_msg", "timestamp": AT + 2.1, "payload": {"type": "task_complete", "turn_id": "turn1"}}
        ]) + "\n")
        spool = self.root / "data/lifecycle-events.jsonl"
        for event, at in [("UserPromptSubmit", AT + 1), ("Stop", AT + 2)]:
            append_private(spool, sanitize({"session_id": "session", "hook_event_name": event, "turn_id": "turn1"}, "codex", at=at))
        os.utime(spool, (AT + 2, AT + 2))
        os.utime(transcript, (AT + 2.1, AT + 2.1))
        with patch("agentwatch.collector.now", return_value=AT + 3), patch.object(self.collector, "probe"):
            self.collector.scan(force=True)
        row = self.store.db.execute("SELECT state,state_source FROM sessions WHERE id='codex:session'").fetchone()
        self.assertEqual(tuple(row), ("idle", "hook"))
        self.assertEqual([r[0] for r in self.store.db.execute("SELECT kind FROM activity ORDER BY at")], ["active", "idle"])

    def test_first_hook_is_read_without_waiting_for_rediscovery(self):
        from agentwatch.bridge import append_private
        with patch("agentwatch.collector.now", return_value=AT):
            self.collector.discover()
        append_private(self.root / "data/lifecycle-events.jsonl",
                       sanitize({"session_id": "session", "hook_event_name": "UserPromptSubmit"}, "codex", at=AT + 1))
        with patch("agentwatch.collector.now", return_value=AT + 2), patch.object(self.collector, "probe"):
            self.collector.scan()
        self.assertEqual(self.state(), "active")

    def test_hook_transport_drains_and_private_payload_is_never_written(self):
        from agentwatch.bridge import append_private
        doc = {"session_id": "session", "hook_event_name": "PreToolUse", "tool_name": "Bash",
               "prompt": SECRET, "tool_input": {"command": SECRET}, "tool_response": SECRET,
               "last_assistant_message": SECRET, "token": SECRET}
        row = sanitize(doc, "codex", at=AT)
        path = self.root / "data/lifecycle-events.jsonl"
        append_private(path, row)
        self.collector.read_file(str(path), "lifecycle", None)
        self.assertEqual(path.stat().st_size, 0)
        self.assertNotIn(SECRET, "\n".join(self.store.db.iterdump()))
        self.assertNotIn(SECRET, json.dumps(self.store.snapshot({"role": "all"}, {}, AT + 1)))
        self.assertIsNone(sanitize(doc, "untrusted-tool"))
        self.assertIsNone(sanitize(dict(doc, hook_event_name="UnknownEvent"), "codex"))

    def test_bridge_is_silent_for_bad_input_and_writes_only_valid_events(self):
        bridge = Path(__file__).resolve().parents[1] / "agentwatch/lifecycle_bridge.py"
        command = [sys.executable, str(bridge), "--data-dir", str(self.root / "data"), "--tool", "codex"]
        for raw in [b"broken", b"[]", json.dumps({"session_id": "x", "hook_event_name": "Stop", "last_assistant_message": SECRET}).encode()]:
            run = subprocess.run(command, input=raw, capture_output=True)
            self.assertEqual((run.returncode, run.stdout, run.stderr), (0, b"", b""))
        spool = self.root / "data/lifecycle-events.jsonl"
        self.assertEqual(len(spool.read_text().splitlines()), 1)
        self.assertNotIn(SECRET, spool.read_text())
        self.assertEqual(os.stat(spool).st_mode & 0o777, 0o600)

    def test_late_end_does_not_mark_a_newer_running_session_offline(self):
        self.event("UserPromptSubmit", at=AT + 10, turn_id="new")
        self.store.update("codex:session", {"liveness": "online"})
        self.event("SessionEnd", at=AT)
        row = self.store.db.execute("SELECT state,liveness,last_hook_at FROM sessions").fetchone()
        self.assertEqual(tuple(row), ("active", "online", AT + 10))


class LifecycleConfigurationTests(unittest.TestCase):
    def test_install_preserves_settings_and_uninstall_keeps_later_changes(self):
        for tool in ["codex", "workbuddy"]:
            with self.subTest(tool=tool), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = LifecycleConfiguration(tool, root / tool, root / "data")
                config.settings.parent.mkdir()
                existing = {"some_setting": "keep", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo keep"}]}]}}
                config.settings.write_text(json.dumps(existing))
                preview = config.preview()
                result = config.install(preview["hash"])
                self.assertTrue(result["complete"])
                installed = json.loads(config.settings.read_text())
                self.assertEqual(installed["some_setting"], "keep")
                self.assertEqual(len(installed["hooks"]["Stop"]), 2)
                self.assertFalse(installed["hooks"]["Stop"][1]["hooks"][0]["async"])
                config.install(config.preview()["hash"])
                self.assertEqual(len(json.loads(config.settings.read_text())["hooks"]["Stop"]), 2)
                installed["later"] = 42
                config.settings.write_text(json.dumps(installed))
                config.uninstall()
                expected = dict(existing, later=42)
                self.assertEqual(json.loads(config.settings.read_text()), expected)

    def test_new_hook_file_can_be_removed_without_touching_other_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = LifecycleConfiguration("codex", root / "codex", root / "data")
            config.install(config.preview()["hash"])
            self.assertTrue(config.settings.exists())
            self.assertTrue(config.status()["review_required"])
            config.uninstall()
            self.assertFalse(config.settings.exists())

    def test_malformed_symlink_and_changed_settings_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = LifecycleConfiguration("workbuddy", root, root / "data")
            config.settings.write_text("{}")
            old_hash = config.preview()["hash"]
            config.settings.write_text('{"later":true}')
            with self.assertRaises(ConfigurationError): config.install(old_hash)
            config.settings.write_text('{"hooks":{"Stop":"invalid"}}')
            original = config.settings.read_bytes()
            with self.assertRaises(ConfigurationError): config.install(config.preview()["hash"])
            self.assertEqual(config.settings.read_bytes(), original)
            config.settings.unlink()
            target = root / "other.json"
            target.write_text("{}")
            config.settings.symlink_to(target)
            with self.assertRaises(ConfigurationError): config.preview()
            self.assertEqual(target.read_text(), "{}")


if __name__ == "__main__":
    unittest.main()
