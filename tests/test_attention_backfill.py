"""Versioned attention replay uses artificial files and never touches usage cursors."""
import json
from pathlib import Path
import tempfile
import unittest

from agentwatch.collector import Collector, RecordParser
from agentwatch.lifecycle_bridge import sanitize
from agentwatch.store import Store


AT = 1792029600.0
PRIVATE = "ARTIFICIAL_CONTENT_NEVER_PERSISTED"


class AttentionBackfillTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = self.root / "codex/sessions/artificial.jsonl"
        self.path.parent.mkdir(parents=True)
        self.store = Store(self.root / "data/watch.sqlite")
        self.sid = self.store.session("codex", "artificial")
        self.store.update(self.sid, {"role": "main", "state": "active", "state_source": "hook",
                                    "state_at": AT + 500, "last_event_at": AT + 500, "liveness": "online"})
        self.collector = self.make_collector()

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def make_collector(self):
        return Collector(self.store, self.root / "codex", self.root / "claude", self.root / "data",
                         self.root / "desktop", self.root / "workbuddy")

    def document(self, payload, offset=0, kind="response_item"):
        return {"type": kind, "timestamp": AT + offset, "payload": payload}

    def request(self, call="call-artificial", item="item-artificial"):
        return self.document({"type": "function_call", "name": "functions.request_user_input_async", "call_id": call,
            "id": item, "arguments": json.dumps({"questions": [{"question": PRIVATE}]})}, 1)

    def reply(self, item="item-artificial"):
        wrapper = [{"questionItemId": json.dumps(["request_user_input_async", item, 0]), "question": PRIVATE, "answer": PRIVATE}]
        return self.document({"type": "message", "role": "user", "content": [{"type": "input_text", "text":
            "<send_user_message_question_reply>" + json.dumps(wrapper) + "</send_user_message_question_reply>"}]}, 3)

    def write_old_file(self, documents):
        expanded = []
        for doc in documents:
            expanded.append(doc)
            if doc.get("payload", {}).get("type") == "function_call":
                expanded.append(self.document({"type": "function_call_output", "call_id": doc["payload"]["call_id"],
                    "output": json.dumps({"accepted": True})}, 2))
        documents = expanded
        self.path.write_text("".join(json.dumps(doc) + "\n" for doc in documents))
        stat = self.path.stat()
        inode = str(stat.st_dev) + ":" + str(stat.st_ino)
        baseline = {"sid": self.sid, "last_totals": {"input_tokens": 123, "output_tokens": 45}, "last_usage_at": AT}
        self.store.save_file(str(self.path), "codex", self.sid, inode, stat.st_size, baseline, stat.st_mtime, stat.st_size)
        self.store.db.commit()
        return self.store.file(str(self.path))

    def replay(self, budget=512000):
        return self.collector.read_file(str(self.path), "codex", self.sid, budget=budget)

    def attention(self):
        return self.store.own_attention([self.sid], AT + 1000)[self.sid]

    def test_preupgrade_unanswered_request_before_old_offset_is_backfilled(self):
        old = self.write_old_file([self.request()])
        amount, more = self.replay()
        self.assertGreater(amount, 0)
        self.assertFalse(more)
        self.assertTrue(self.attention()["pending"])
        self.assertEqual(self.attention()["unknown"], 0)
        self.assertEqual(self.store.file(str(self.path)), old)

    def test_current_async_title_schema_backfills_accepted_delivery(self):
        document = self.request()
        document["payload"]["arguments"] = json.dumps({"questions": [{"title": PRIVATE, "options": []}]})
        self.write_old_file([document])
        self.replay()
        self.assertTrue(self.attention()["pending"])
        self.assertEqual(self.attention()["waiting_user"], 1)
        self.assertNotIn(PRIVATE, "\n".join(self.store.db.iterdump()))

    def test_historical_reply_replays_in_order_and_does_not_reopen(self):
        self.write_old_file([self.request(), self.reply()])
        self.replay()
        self.assertFalse(self.attention()["pending"])
        self.assertEqual(self.replay(), (0, False))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM interaction_requests").fetchone()[0], 1)

    def test_metadata_replay_preserves_usage_baseline_and_current_hook_state(self):
        old = self.write_old_file([self.request(), self.document({"type": "task_complete", "turn_id": "artificial-turn"}, 4, "event_msg")])
        self.store.usage(self.sid, "known-usage", AT, 123, 45, None, None, None, "test")
        self.replay()
        row = self.store.db.execute("SELECT state,state_source,state_at,last_completed_at FROM sessions WHERE id=?", (self.sid,)).fetchone()
        self.assertEqual(tuple(row), ("active", "hook", AT + 500, AT + 4))
        self.assertEqual(self.store.file(str(self.path)), old)
        self.assertEqual(self.store.db.execute("SELECT SUM(total) FROM usage").fetchone()[0], 168)

    def test_incomplete_backfill_has_unknown_coverage_until_eof(self):
        self.write_old_file([self.request(), self.reply()])
        _, more = self.replay(budget=1)
        self.assertTrue(more)
        self.assertGreater(self.attention()["unknown"], 0)
        while more:
            _, more = self.replay(budget=1)
        self.assertFalse(self.attention()["pending"])
        self.assertEqual(self.attention()["unknown"], 0)

    def test_restart_resumes_independent_metadata_cursor(self):
        self.write_old_file([self.request(), self.reply()])
        self.replay(budget=1)
        self.store.db.commit(); self.store.close()
        self.store = Store(self.root / "data/watch.sqlite")
        self.collector = self.make_collector()
        while self.replay(budget=1)[1]:
            pass
        self.assertFalse(self.attention()["pending"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM interaction_requests").fetchone()[0], 1)

    def test_appended_native_reply_closes_an_existing_backfilled_call(self):
        self.write_old_file([self.request()])
        self.replay()
        with self.path.open("a") as output:
            output.write(json.dumps(self.reply()) + "\n")
        self.replay()
        self.assertFalse(self.attention()["pending"])

    def test_malformed_or_oversized_metadata_keeps_coverage_unknown_at_eof(self):
        for malformed in [b"not-json\n", b"x" * 2_000_002 + b"\n"]:
            with self.subTest(size=len(malformed)):
                self.write_old_file([])
                self.path.write_bytes(malformed)
                self.replay()
                self.assertGreater(self.attention()["unknown"], 0)
                self.assertFalse(self.replay()[1])
                self.assertGreater(self.attention()["unknown"], 0)

    def test_unfinished_line_is_not_treated_as_complete_coverage(self):
        self.write_old_file([])
        self.path.write_text(json.dumps(self.request()))
        self.assertTrue(self.replay()[1])
        self.assertGreater(self.attention()["unknown"], 0)
        with self.path.open("a") as output:
            output.write("\n")
        self.replay()
        self.assertFalse(self.attention()["pending"])
        with self.path.open("a") as output:
            output.write(json.dumps(self.document({"type": "function_call_output", "call_id": "call-artificial", "output": json.dumps({"accepted": True})}, 2)) + "\n")
        self.replay()
        self.assertTrue(self.attention()["pending"])

    def test_missing_source_timestamp_preserves_unknown_coverage(self):
        document = self.request()
        document.pop("timestamp")
        self.write_old_file([document])
        self.replay()
        self.assertFalse(self.attention()["pending"])
        self.assertGreater(self.attention()["unknown"], 0)

    def test_replay_after_rotation_cannot_reopen_an_already_answered_id(self):
        self.write_old_file([self.request(), self.reply()])
        self.replay()
        self.path.unlink()
        self.path.write_text(json.dumps(self.request()) + "\n" + json.dumps(self.document({"type": "function_call_output", "call_id": "call-artificial", "output": json.dumps({"accepted": True})}, 2)) + "\n")
        self.replay()
        self.assertFalse(self.attention()["pending"])

    def test_fork_prefix_is_not_adopted_as_child_interaction(self):
        parent = self.document({"id": "parent-artificial", "source": "cli"}, 0, "session_meta")
        own = self.document({"id": "artificial", "source": "cli"}, 2, "session_meta")
        self.write_old_file([parent, self.request(), own])
        self.replay()
        self.assertFalse(self.attention()["pending"])

    def test_cancelled_stop_and_native_cancel_do_not_record_completion(self):
        parser = RecordParser(self.store)
        parser.lifecycle(sanitize({"session_id": "artificial", "hook_event_name": "Stop", "stop_reason": "cancelled"}, "codex", at=AT + 1), {})
        self.assertIsNone(self.store.db.execute("SELECT last_completed_at FROM sessions WHERE id=?", (self.sid,)).fetchone()[0])
        parser.workbuddy_state({"session_id": "native-artificial", "at": AT + 2, "signal": "TURN_CANCELLED", "target": "idle"})
        self.assertIsNone(self.store.db.execute("SELECT last_completed_at FROM sessions WHERE id='workbuddy:native-artificial'").fetchone()[0])
        parser.workbuddy_state({"session_id": "native-artificial", "at": AT + 3, "signal": "TURN_COMPLETED", "target": "idle"})
        self.assertEqual(self.store.db.execute("SELECT last_completed_at FROM sessions WHERE id='workbuddy:native-artificial'").fetchone()[0], AT + 3)

    def test_replay_never_persists_raw_arguments_or_reply_text(self):
        self.write_old_file([self.request(), self.reply()])
        self.replay()
        self.assertNotIn(PRIVATE, "\n".join(self.store.db.iterdump()))
        self.assertNotIn("send_user_message_question_reply", "\n".join(self.store.db.iterdump()))


if __name__ == "__main__":
    unittest.main()
