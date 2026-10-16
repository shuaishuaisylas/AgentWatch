"""Artificial route tests: navigation and completed-version receipts are separate."""
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from agentwatch.navigation import NavigationError
from agentwatch.server import Handler
from agentwatch.store import Store


AT = 1792029600.0
CURRENT = AT + 100


class CompletionReceiptHttpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "watch.sqlite")
        self.sid = self.store.session("codex", "artificial-main")
        self.store.update(self.sid, {"role": "main", "state": "idle", "state_at": AT,
                                    "last_event_at": AT, "surface": "desktop", "attention_covered": 1})
        self.store.complete(self.sid, AT)
        rollout = self.root / "artificial.jsonl"
        rollout.write_text('{"artificial":true}\n')
        self.store.save_file(str(rollout), "codex", self.sid, "artificial-inode", 1, {}, AT, rollout.stat().st_size)
        self.store.db.commit()

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def dispatch(self, path="/api/inbox/read", body=None, demo=False, authorized=True):
        handler = Handler.__new__(Handler)
        handler.server = SimpleNamespace(store=self.store, demo=demo, lifecycle_configs={})
        handler.path = path
        payload = json.dumps(body if body is not None else {"id": self.sid, "completed_at": str(AT)}).encode()
        handler.headers = {"Content-Length": str(len(payload)), "Content-Type": "application/json"}
        handler.rfile = io.BytesIO(payload)
        responses = []
        handler.json_response = lambda status, value: responses.append((status, value))
        handler.trust = lambda: True
        mutations = []
        def authorize(mutate=False):
            mutations.append(mutate)
            if not authorized:
                handler.json_response(401, {"error": "artificial authentication failure"})
            return authorized
        handler.authorized = authorize
        with patch("agentwatch.server.now", return_value=CURRENT):
            handler.do_POST()
        self.assertEqual(mutations, [True])
        self.assertEqual(len(responses), 1)
        return responses[0]

    def snapshot(self):
        return self.store.snapshot({"role": "main"}, {}, current=CURRENT)

    def test_explicit_read_marks_only_the_completed_version(self):
        status, response = self.dispatch()
        self.assertEqual((status, response), (200, {"read_completed": True}))
        self.assertEqual(self.snapshot()["inbox_counts"]["pending"], 0)
        self.store.complete(self.sid, AT + 1)
        self.assertEqual(self.snapshot()["inbox_counts"]["unread_completed"], 1)

    def test_read_is_available_in_isolated_demo_without_allowing_navigation(self):
        self.assertEqual(self.dispatch(demo=True), (200, {"read_completed": True}))
        with patch("agentwatch.server.jump", side_effect=AssertionError("No real app")) as navigate:
            status, _ = self.dispatch("/api/jump", {"id": self.sid}, demo=True)
        self.assertEqual(status, 403)
        navigate.assert_not_called()

    def test_missing_authentication_cannot_write_a_receipt(self):
        self.assertEqual(self.dispatch(authorized=False)[0], 401)
        self.assertEqual(self.snapshot()["inbox_counts"]["unread_completed"], 1)

    def test_version_must_be_a_finite_positive_numeric_string(self):
        for value in [None, True, AT, "", "nan", "inf", "-inf", "0", "-1", "not-a-number"]:
            with self.subTest(value=value):
                self.assertEqual(self.dispatch(body={"id": self.sid, "completed_at": value})[0], 400)
        self.assertEqual(self.snapshot()["inbox_counts"]["unread_completed"], 1)

    def test_future_or_not_yet_observed_versions_cannot_be_preacknowledged(self):
        for value in [CURRENT + 1, AT + 1]:
            self.assertEqual(self.dispatch(body={"id": self.sid, "completed_at": str(value)})[0], 400)
        self.assertEqual(self.snapshot()["inbox_counts"]["unread_completed"], 1)

    def test_unknown_hidden_child_or_missing_completion_cannot_be_acknowledged(self):
        for values in [{"archived": 1}, {"internal": 1}, {"role": "child"}, {"role": "unknown"}]:
            with self.subTest(values=values):
                self.store.update(self.sid, {"archived": 0, "internal": 0, "role": "main", **values})
                self.assertEqual(self.dispatch()[0], 404)
        self.store.update(self.sid, {"role": "main", "last_completed_at": None})
        self.assertEqual(self.dispatch()[0], 400)

    def test_old_visible_card_cannot_mark_a_new_completion_read(self):
        self.store.complete(self.sid, AT + 10)
        self.assertEqual(self.dispatch(), (200, {"read_completed": True}))
        self.assertEqual(self.snapshot()["inbox_counts"]["unread_completed"], 1)
        self.assertEqual(self.snapshot()["recent_completed"][0]["inbox"]["completed_at"], AT + 10)

    def test_receipts_are_monotonic_and_never_resolve_a_human_request(self):
        self.store.complete(self.sid, AT + 10)
        self.dispatch(body={"id": self.sid, "completed_at": str(AT + 10)})
        self.dispatch()
        row = self.store.db.execute("SELECT read_completed_at FROM completion_receipts WHERE session_id=?", (self.sid,)).fetchone()
        self.assertEqual(row[0], AT + 10)
        self.store.open_interaction(self.sid, "artificial-question", 0, "waiting_user", AT)
        self.assertEqual(self.dispatch()[0], 200)
        self.assertEqual(self.snapshot()["attention_counts"]["pending"], 1)
        self.assertEqual(self.snapshot()["inbox_counts"]["action_required"], 1)

    def test_plain_jump_success_never_marks_a_completion_read(self):
        with patch("agentwatch.server.jump", return_value={"opened": True, "kind": "codex"}) as navigate:
            self.assertEqual(self.dispatch("/api/jump", {"id": self.sid})[0], 200)
        navigate.assert_called_once()
        self.assertEqual(self.snapshot()["inbox_counts"]["unread_completed"], 1)

    def test_legacy_extra_jump_fields_do_not_bypass_the_explicit_read_step(self):
        with patch("agentwatch.server.jump", return_value={"opened": True, "kind": "codex"}):
            status, response = self.dispatch("/api/jump", {"id": self.sid, "read_completed_at": str(AT)})
        self.assertEqual(status, 200)
        self.assertNotIn("read_completed", response)
        self.assertEqual(self.snapshot()["inbox_counts"]["unread_completed"], 1)

    def test_failed_or_ambiguous_navigation_never_creates_a_receipt(self):
        for failure in [NavigationError("artificial failure"), TimeoutError("artificial timeout")]:
            with patch("agentwatch.server.jump", side_effect=failure):
                self.assertIn(self.dispatch("/api/jump", {"id": self.sid})[0], [400, 500])
        self.assertEqual(self.snapshot()["inbox_counts"]["unread_completed"], 1)


if __name__ == "__main__":
    unittest.main()
