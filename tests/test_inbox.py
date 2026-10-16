"""Artificial completion-receipt tests; no external apps or personal records."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agentwatch.store import Store


AT = 1792029600.0
CURRENT = AT + 100


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "watch.sqlite"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def session(self, identity="root", role="main", parent=None, tool="codex", **extra):
        sid = self.store.session(tool, identity)
        self.store.update(sid, {"role": role, "parent_id": parent, "last_event_at": AT,
                                "state": "idle", "state_source": "hook", "state_at": AT,
                                "liveness": "offline", "attention_covered": 1, **extra})
        return sid

    def snapshot(self, **args):
        with patch("agentwatch.store.now", return_value=CURRENT):
            return self.store.snapshot({"role": "main", "tool": "all", "project": "all", **args}, {})

    def card(self, sid):
        return next(card for card in self.snapshot()["sessions"] if card["id"] == sid)

    def ack(self, sid, completed_at):
        return self.store.acknowledge_completion(sid, completed_at, current=CURRENT)

    def test_initial_completion_is_unread_and_snapshot_reads_do_not_acknowledge(self):
        sid = self.session()
        self.store.complete(sid, AT)
        for _ in range(3):
            snapshot = self.snapshot()
            self.assertEqual(snapshot["inbox_counts"]["pending"], 1)
            self.assertEqual(snapshot["inbox_counts"]["unread_completed"], 1)
            self.assertEqual(snapshot["inbox_counts"]["action_required"], 0)
            self.assertEqual([item["id"] for item in snapshot["recent_completed"]], [sid])
            self.assertEqual(self.card(sid)["inbox"], {"unread_completed": True, "completed_at": AT})

    def test_explicit_acknowledgement_removes_completion_but_keeps_history(self):
        sid = self.session()
        self.store.complete(sid, AT)
        self.ack(sid, AT)
        snapshot = self.snapshot()
        self.assertEqual(snapshot["inbox_counts"]["pending"], 0)
        self.assertEqual(snapshot["inbox_counts"]["unread_completed"], 0)
        self.assertEqual(snapshot["recent_completed"], [])
        self.assertFalse(self.card(sid)["inbox"]["unread_completed"])
        self.assertEqual(self.card(sid)["last_completed_at"], AT)

    def test_new_completion_becomes_unread_after_acknowledging_the_previous_version(self):
        sid = self.session()
        self.store.complete(sid, AT)
        self.ack(sid, AT)
        self.store.complete(sid, AT + 20)
        self.assertTrue(self.card(sid)["inbox"]["unread_completed"])
        self.assertEqual(self.card(sid)["inbox"]["completed_at"], AT + 20)
        self.assertEqual(self.snapshot()["inbox_counts"]["pending"], 1)

    def test_late_acknowledgement_of_old_version_cannot_hide_new_completion(self):
        sid = self.session()
        self.store.complete(sid, AT)
        self.store.complete(sid, AT + 20)
        self.ack(sid, AT)
        self.assertEqual(self.snapshot()["inbox_counts"]["unread_completed"], 1)
        self.ack(sid, AT + 20)
        self.assertEqual(self.snapshot()["inbox_counts"]["unread_completed"], 0)
        self.ack(sid, AT)
        self.assertEqual(self.snapshot()["inbox_counts"]["unread_completed"], 0)

    def test_acknowledgement_is_scoped_to_one_root(self):
        first = self.session("first")
        second = self.session("second")
        self.store.complete(first, AT)
        self.store.complete(second, AT)
        self.ack(first, AT)
        self.assertEqual(self.snapshot()["inbox_counts"]["unread_completed"], 1)
        self.assertTrue(self.card(second)["inbox"]["unread_completed"])

    def test_receipt_survives_restart_and_duplicate_completion_replay(self):
        sid = self.session()
        self.store.complete(sid, AT)
        self.ack(sid, AT)
        self.store.db.commit()
        self.store.close()
        self.store = Store(self.path)
        self.store.complete(sid, AT)
        self.assertEqual(self.snapshot()["inbox_counts"]["unread_completed"], 0)
        self.store.complete(sid, AT + 1)
        self.assertEqual(self.snapshot()["inbox_counts"]["unread_completed"], 1)

    def test_completion_acknowledgement_never_resolves_human_action(self):
        sid = self.session()
        self.store.complete(sid, AT)
        self.store.open_interaction(sid, "manual-question", 0, "waiting_user", AT + 1)
        self.ack(sid, AT)
        snapshot = self.snapshot()
        self.assertEqual(snapshot["inbox_counts"]["pending"], 1)
        self.assertEqual(snapshot["inbox_counts"]["action_required"], 1)
        self.assertEqual(snapshot["inbox_counts"]["unread_completed"], 0)
        self.assertTrue(self.card(sid)["attention"]["pending"])

    def test_human_and_completion_groups_are_counted_once_per_root(self):
        manual = self.session("manual")
        self.store.complete(manual, AT)
        for index in range(3):
            self.store.open_interaction(manual, "question-" + str(index), 0, "waiting_user", AT + 1)
        completed = self.session("completed")
        self.store.complete(completed, AT)
        snapshot = self.snapshot()
        self.assertEqual(snapshot["inbox_counts"]["pending"], 2)
        self.assertEqual(snapshot["inbox_counts"]["action_required"], 1)
        self.assertEqual(snapshot["inbox_counts"]["unread_completed"], 1)

    def test_child_human_action_rolls_up_but_child_completion_is_not_another_root(self):
        root = self.session()
        child = self.session("child", role="child", parent=root)
        self.store.complete(root, AT)
        self.store.complete(child, AT + 1)
        self.store.open_interaction(child, "child-question", 0, "waiting_user", AT + 2)
        self.ack(root, AT)
        snapshot = self.snapshot()
        self.assertEqual(snapshot["inbox_counts"]["pending"], 1)
        self.assertEqual(snapshot["inbox_counts"]["action_required"], 1)
        self.assertEqual(snapshot["inbox_counts"]["unread_completed"], 0)
        self.assertEqual(snapshot["recent_completed"], [])

    def test_busy_error_unknown_or_uncovered_roots_are_not_unread_completed(self):
        for index, extra in enumerate([
                {"state": "active", "liveness": "online"}, {"state": "error"},
                {"state": "unknown"}, {"state": "idle", "attention_covered": 0}]):
            sid = self.session("ineligible-" + str(index), **extra)
            self.store.complete(sid, AT)
        snapshot = self.snapshot()
        self.assertEqual(snapshot["inbox_counts"]["pending"], 0)
        self.assertEqual(snapshot["inbox_counts"]["unread_completed"], 0)
        self.assertEqual(snapshot["recent_completed"], [])
        self.assertGreaterEqual(snapshot["inbox_counts"]["unknown"], 1)

    def test_yesterdays_completion_within_24_hours_is_an_independent_candidate(self):
        sid = self.session(last_event_at=CURRENT - 30 * 3600, state="ended")
        self.store.complete(sid, CURRENT - 20 * 3600)
        snapshot = self.snapshot()
        self.assertEqual(snapshot["inbox_counts"]["unread_completed"], 1)
        self.assertEqual([item["id"] for item in snapshot["recent_completed"]], [sid])

    def test_expired_or_future_completion_is_not_an_unread_reminder(self):
        old = self.session("expired")
        future = self.session("future")
        self.store.complete(old, CURRENT - 86401)
        self.store.complete(future, CURRENT + 1)
        self.assertEqual(self.snapshot()["inbox_counts"]["pending"], 0)
        self.assertEqual(self.snapshot()["recent_completed"], [])

    def test_inbox_counts_are_complete_before_main_card_and_recent_list_limits(self):
        for index in range(170):
            sid = self.session("completion-" + str(index))
            self.store.complete(sid, AT + index / 10)
        snapshot = self.snapshot()
        self.assertTrue(snapshot["truncated"])
        self.assertEqual(len(snapshot["sessions"]), 160)
        self.assertEqual(len(snapshot["recent_completed"]), 5)
        self.assertEqual(snapshot["inbox_counts"]["unread_completed"], 170)
        self.assertEqual(snapshot["inbox_counts"]["pending"], 170)

    def test_read_or_busy_newer_rows_do_not_hide_five_older_unread_rows(self):
        done = []
        for index in range(10):
            sid = self.session("done-" + str(index))
            self.store.complete(sid, AT + index)
            done.append(sid)
        for index in range(7, 10):
            self.ack(done[index], AT + index)
        for index in range(5):
            busy = self.session("busy-" + str(index), state="active", liveness="online")
            self.store.complete(busy, AT + 30 + index)
        snapshot = self.snapshot()
        self.assertEqual([item["id"] for item in snapshot["recent_completed"]], list(reversed(done[:7]))[:5])
        self.assertEqual(snapshot["inbox_counts"]["unread_completed"], 7)

    def test_hidden_child_and_unknown_role_completions_do_not_create_root_reminders(self):
        root = self.session()
        for identity, extra in [
                ("hidden", {"archived": 1}), ("internal", {"internal": 1}),
                ("child", {"role": "child", "parent": root}), ("unknown", {"role": "unknown"})]:
            sid = self.session(identity, **extra)
            self.store.complete(sid, AT)
        self.assertEqual(self.snapshot()["inbox_counts"]["pending"], 0)
        self.assertEqual(self.snapshot()["recent_completed"], [])

    def test_tool_filter_scopes_unread_completion_without_touching_other_receipts(self):
        codex = self.session("codex-completion")
        claude = self.session("claude-completion", tool="claude")
        self.store.complete(codex, AT)
        self.store.complete(claude, AT)
        self.assertEqual(self.snapshot(tool="codex")["inbox_counts"]["unread_completed"], 1)
        self.ack(codex, AT)
        self.assertEqual(self.snapshot(tool="codex")["inbox_counts"]["unread_completed"], 0)
        self.assertEqual(self.snapshot(tool="claude")["inbox_counts"]["unread_completed"], 1)

    def test_future_receipt_cannot_preacknowledge_a_later_completion(self):
        sid = self.session()
        self.store.complete(sid, AT)
        try:
            self.ack(sid, AT + 1000)
        except (ValueError, TypeError):
            pass
        self.store.complete(sid, AT + 20)
        self.assertTrue(self.card(sid)["inbox"]["unread_completed"])

    def test_no_completion_means_unknown_completion_time_not_a_zero_timestamp(self):
        sid = self.session()
        inbox = self.card(sid)["inbox"]
        self.assertFalse(inbox["unread_completed"])
        self.assertIsNone(inbox["completed_at"])
        self.assertEqual(self.snapshot()["inbox_counts"]["pending"], 0)


if __name__ == "__main__":
    unittest.main()
