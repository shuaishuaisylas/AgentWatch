"""Artificial local-reminder dismissals; never answers or approves an Agent task."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agentwatch.store import Store


AT = 1792029600.0
CURRENT = AT + 100


class AttentionDismissalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "watch.sqlite"
        self.store = Store(self.path)
        self.sid = self.session()

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def session(self, identity="root", role="main", parent=None, **extra):
        sid = self.store.session("codex", identity)
        self.store.update(sid, {"role": role, "parent_id": parent, "last_event_at": AT,
                                "state": "idle", "state_source": "hook", "state_at": AT,
                                "liveness": "offline", "attention_covered": 1, **extra})
        return sid

    def request(self, call="question-one", sid=None, index=0, kind="waiting_user", at=AT):
        sid = sid or self.sid
        self.store.open_interaction(sid, call, index, kind, at,
                                    tool_name="Bash" if kind == "waiting_approval" else "request_user_input_async",
                                    asynchronous=kind != "waiting_approval", source="artificial")

    def snapshot(self):
        return self.store.snapshot({"role": "main", "tool": "all", "project": "all"}, {}, current=CURRENT)

    def card(self, sid=None):
        return next(card for card in self.snapshot()["sessions"] if card["id"] == (sid or self.sid))

    def version(self, sid=None):
        return self.card(sid)["attention"]["dismiss_version"]

    def dismiss(self, sid=None, version=None):
        return self.store.dismiss_attention(sid or self.sid, version or self.version(sid), current=CURRENT)

    def test_version_is_null_without_known_human_requests(self):
        self.assertIsNone(self.version())
        self.request(kind="unknown")
        self.assertIsNone(self.version())
        self.assertFalse(self.card()["attention"]["pending"])
        self.assertGreater(self.card()["attention"]["unknown"], 0)

    def test_version_is_stable_for_the_same_outstanding_request_set(self):
        self.request()
        version = self.version()
        self.assertRegex(version, r"^[0-9a-f]{64}$")
        self.assertEqual(self.version(), version)
        self.request()
        self.assertEqual(self.version(), version)
        self.store.update(self.sid, {"alias": "人工别名", "state_at": AT + 5})
        self.assertEqual(self.version(), version)

    def test_explicit_dismissal_removes_only_local_human_reminder(self):
        self.request()
        self.request(call="approval", kind="waiting_approval")
        self.assertEqual(self.snapshot()["attention_counts"]["pending"], 1)
        self.assertTrue(self.dismiss())
        attention = self.card()["attention"]
        self.assertFalse(attention["pending"])
        self.assertEqual(attention["waiting_user"], 0)
        self.assertEqual(attention["waiting_approval"], 0)
        self.assertIsNone(attention["dismiss_version"])
        self.assertEqual(self.snapshot()["attention_counts"]["pending"], 0)
        self.assertEqual(self.snapshot()["inbox_counts"]["action_required"], 0)
        unresolved = self.store.db.execute("SELECT COUNT(*) FROM interaction_requests WHERE resolved_at IS NULL").fetchone()[0]
        self.assertEqual(unresolved, 2)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM attention_dismissals").fetchone()[0], 2)

    def test_dismissal_is_scoped_to_one_root(self):
        other = self.session("other")
        self.request()
        self.request(sid=other, call="other-question")
        self.assertTrue(self.dismiss())
        self.assertFalse(self.card()["attention"]["pending"])
        self.assertTrue(self.card(other)["attention"]["pending"])
        self.assertEqual(self.snapshot()["attention_counts"]["pending"], 1)

    def test_root_dismissal_covers_explicit_visible_children_and_grandchildren(self):
        child = self.session("child", role="child", parent=self.sid)
        grandchild = self.session("grandchild", role="child", parent=child)
        self.request()
        self.request(sid=child, call="child-question", index=0)
        self.request(sid=child, call="child-question", index=1)
        self.request(sid=grandchild, call="grandchild-approval", kind="waiting_approval")
        self.assertEqual(self.card()["attention"]["waiting_user"], 3)
        self.assertEqual(self.card()["attention"]["waiting_approval"], 1)
        self.assertEqual(self.snapshot()["attention_counts"]["pending"], 1)
        self.assertTrue(self.dismiss())
        self.assertFalse(self.card()["attention"]["pending"])
        children = self.store.children(self.sid, current=CURRENT)["children"]
        self.assertTrue(all(not card["attention"]["pending"] for card in children))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM attention_dismissals").fetchone()[0], 4)

    def test_hidden_and_unknown_relationship_requests_do_not_change_root_version(self):
        self.request()
        version = self.version()
        for identity, values in [
                ("archived", {"archived": 1}), ("internal", {"internal": 1}),
                ("memory", {"internal_memory": 1}), ("unknown", {"role": "unknown"})]:
            child = self.session(identity, role=values.pop("role", "child"), parent=self.sid, **values)
            self.request(sid=child, call=identity + "-question")
            if identity == "unknown":
                grandchild = self.session("under-unknown", role="child", parent=child)
                self.request(sid=grandchild, call="unrelated-question")
        self.assertEqual(self.version(), version)
        self.assertTrue(self.dismiss(version=version))
        self.assertFalse(self.card()["attention"]["pending"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM attention_dismissals").fetchone()[0], 1)

    def test_new_request_changes_version_and_rejects_a_stale_confirmation(self):
        self.request()
        old = self.version()
        self.request(call="new-question", at=AT + 1)
        self.assertNotEqual(self.version(), old)
        self.assertFalse(self.dismiss(version=old))
        self.assertEqual(self.card()["attention"]["waiting_user"], 2)
        self.assertTrue(self.dismiss())

    def test_a_new_question_index_also_invalidates_the_version(self):
        self.request(index=0)
        old = self.version()
        self.request(index=1)
        self.assertNotEqual(self.version(), old)
        self.assertFalse(self.dismiss(version=old))
        self.assertEqual(self.card()["attention"]["waiting_user"], 2)

    def test_a_resolved_request_invalidates_an_old_multi_request_version(self):
        self.request()
        self.request(call="question-two")
        old = self.version()
        self.store.resolve_interaction(self.sid, "question-one", AT + 1)
        self.assertNotEqual(self.version(), old)
        self.assertFalse(self.dismiss(version=old))
        self.assertTrue(self.dismiss())
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM attention_dismissals").fetchone()[0], 1)

    def test_a_fully_resolved_request_cannot_be_dismissed(self):
        self.request()
        old = self.version()
        self.store.resolve_interaction(self.sid, "question-one", AT + 1)
        self.assertIsNone(self.version())
        self.assertFalse(self.dismiss(version=old))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM attention_dismissals").fetchone()[0], 0)

    def test_duplicate_dismissal_is_not_a_new_success(self):
        self.request()
        version = self.version()
        self.assertTrue(self.dismiss(version=version))
        self.assertFalse(self.dismiss(version=version))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM attention_dismissals").fetchone()[0], 1)

    def test_new_call_reappears_after_old_request_was_dismissed(self):
        self.request()
        old = self.version()
        self.assertTrue(self.dismiss(version=old))
        self.request(call="new-question", at=AT + 1)
        self.assertTrue(self.card()["attention"]["pending"])
        self.assertEqual(self.card()["attention"]["waiting_user"], 1)
        self.assertNotEqual(self.version(), old)
        self.assertFalse(self.dismiss(version=old))

    def test_restart_and_duplicate_replay_do_not_revive_dismissed_requests(self):
        self.request()
        self.assertTrue(self.dismiss())
        self.store.db.commit()
        self.store.close()
        self.store = Store(self.path)
        self.request()
        self.assertFalse(self.card()["attention"]["pending"])
        self.assertIsNone(self.version())
        self.request(call="new-question", at=AT + 1)
        self.assertEqual(self.card()["attention"]["waiting_user"], 1)

    def test_unknown_requests_do_not_invalidate_a_known_request_version(self):
        self.request()
        version = self.version()
        self.request(call="unknown-delivery", kind="unknown", at=AT + 1)
        self.assertEqual(self.version(), version)
        self.assertTrue(self.dismiss(version=version))
        self.assertFalse(self.card()["attention"]["pending"])
        self.assertGreater(self.card()["attention"]["unknown"], 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM attention_dismissals").fetchone()[0], 1)

    def test_later_confirmed_delivery_of_unknown_request_remains_a_new_reminder(self):
        self.request()
        self.request(call="unknown-delivery", kind="unknown", at=AT + 1)
        self.assertTrue(self.dismiss())
        self.request(call="unknown-delivery", kind="waiting_user", at=AT + 2)
        self.assertEqual(self.card()["attention"]["waiting_user"], 1)
        self.assertTrue(self.card()["attention"]["pending"])
        self.assertRegex(self.version(), r"^[0-9a-f]{64}$")

    def test_child_visibility_change_invalidates_a_stale_root_confirmation(self):
        child = self.session("child", role="child", parent=self.sid)
        self.request()
        self.request(sid=child, call="child-question")
        before_hide = self.version()
        self.store.update(child, {"archived": 1})
        self.assertNotEqual(self.version(), before_hide)
        self.assertFalse(self.dismiss(version=before_hide))
        self.assertTrue(self.dismiss())
        self.store.update(child, {"archived": 0})
        self.assertTrue(self.card()["attention"]["pending"])
        self.assertEqual(self.card()["attention"]["waiting_user"], 1)

    def test_root_confirmation_includes_more_than_160_descendants(self):
        for index in range(170):
            child = self.session("child-" + str(index), role="child", parent=self.sid)
            self.request(sid=child, call="child-question-" + str(index))
        self.assertEqual(self.card()["attention"]["waiting_user"], 170)
        self.assertEqual(self.snapshot()["attention_counts"]["pending"], 1)
        self.assertTrue(self.dismiss())
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM attention_dismissals").fetchone()[0], 170)
        self.assertEqual(self.snapshot()["attention_counts"]["pending"], 0)

    def test_global_dismissal_counts_are_complete_before_the_card_limit(self):
        for index in range(170):
            root = self.session("root-" + str(index))
            self.request(sid=root, call="root-question-" + str(index))
        snapshot = self.snapshot()
        self.assertTrue(snapshot["truncated"])
        self.assertEqual(snapshot["attention_counts"]["pending"], 170)
        chosen = snapshot["sessions"][0]
        self.assertTrue(self.store.dismiss_attention(chosen["id"], chosen["attention"]["dismiss_version"], current=CURRENT))
        self.assertEqual(self.snapshot()["attention_counts"]["pending"], 169)
        self.assertEqual(self.snapshot()["inbox_counts"]["action_required"], 169)

    def test_dismissal_does_not_invent_unknown_from_a_legacy_waiting_state(self):
        self.store.update(self.sid, {"state": "waiting_user"})
        self.request()
        self.assertTrue(self.dismiss())
        attention = self.card()["attention"]
        self.assertFalse(attention["pending"])
        self.assertEqual(attention["unknown"], 0)
        self.assertEqual(self.snapshot()["inbox_counts"]["unknown"], 0)

    def test_real_coverage_gaps_stay_unknown_after_dismissal(self):
        self.store.update(self.sid, {"state": "waiting_user", "attention_covered": 0})
        self.request()
        self.assertTrue(self.dismiss())
        self.assertFalse(self.card()["attention"]["pending"])
        self.assertGreater(self.card()["attention"]["unknown"], 0)
        self.store.update(self.sid, {"attention_covered": 1, "coverage": "partial_records"})
        self.assertGreater(self.card()["attention"]["unknown"], 0)

    def test_invalid_versions_never_write_a_dismissal(self):
        self.request()
        for value in [None, True, 1, "", "a" * 63, "a" * 65, "z" * 64, "0" * 64]:
            with self.subTest(value=value):
                self.assertFalse(self.store.dismiss_attention(self.sid, value, current=CURRENT))
        self.assertTrue(self.card()["attention"]["pending"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM attention_dismissals").fetchone()[0], 0)

    def test_missing_hidden_child_and_unknown_roles_cannot_dismiss(self):
        self.request()
        version = self.version()
        self.assertFalse(self.store.dismiss_attention("codex:missing", version, current=CURRENT))
        for extra in [{"archived": 1}, {"internal": 1}, {"internal_memory": 1},
                      {"role": "child"}, {"role": "unknown"}]:
            with self.subTest(extra=extra):
                self.store.update(self.sid, {"archived": 0, "internal": 0, "internal_memory": 0, "role": "main", **extra})
                self.assertFalse(self.store.dismiss_attention(self.sid, version, current=CURRENT))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM attention_dismissals").fetchone()[0], 0)

    def test_dismissal_preserves_original_requests_state_usage_and_completion(self):
        self.request()
        self.store.complete(self.sid, AT)
        self.store.usage(self.sid, "artificial-usage", AT, 123, 45, None, None, None, "artificial")
        self.store.acknowledge_completion(self.sid, AT, current=CURRENT)
        tables = ["interaction_requests", "sessions", "usage", "completion_receipts"]
        before = {table: [tuple(row) for row in self.store.db.execute("SELECT * FROM " + table)] for table in tables}
        self.assertTrue(self.dismiss())
        after = {table: [tuple(row) for row in self.store.db.execute("SELECT * FROM " + table)] for table in tables}
        self.assertEqual(after, before)

    def test_dismissal_default_current_uses_store_clock(self):
        self.request()
        with patch("agentwatch.store.now", return_value=CURRENT):
            self.assertTrue(self.store.dismiss_attention(self.sid, self.version()))


if __name__ == "__main__":
    unittest.main()
