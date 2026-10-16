"""Artificial attention routes with real host/cookie/CSRF checks and no sockets."""
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from agentwatch.server import Handler
from agentwatch.store import Store


AT = 1792029600.0
CURRENT = AT + 100
HOST = "127.0.0.1:18766"
COOKIE = "artificial-browser-token"
CSRF = "artificial-csrf-token"


class AttentionDismissalHttpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "watch.sqlite")
        self.sid = self.store.session("codex", "artificial-main")
        self.store.update(self.sid, {"role": "main", "state": "waiting_user", "state_at": AT,
                                    "last_event_at": AT, "attention_covered": 1})
        self.store.open_interaction(self.sid, "artificial-question", 0, "waiting_user", AT)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def snapshot(self):
        return self.store.snapshot({"role": "main"}, {}, current=CURRENT)

    def version(self):
        return next(card for card in self.snapshot()["sessions"] if card["id"] == self.sid)["attention"]["dismiss_version"]

    def dispatch(self, body=None, headers=None, demo=False, path="/api/attention/dismiss"):
        handler = Handler.__new__(Handler)
        handler.server = SimpleNamespace(store=self.store, demo=demo, lifecycle_configs={},
                                         browser_token=COOKIE, csrf_token=CSRF,
                                         trusted_host=lambda value: value == HOST)
        handler.path = path
        payload = json.dumps(body if body is not None else {"id": self.sid, "version": self.version()}).encode()
        handler.headers = {"Host": HOST, "Cookie": "agentwatch=" + COOKIE,
                           "X-AgentWatch-CSRF": CSRF, "Content-Length": str(len(payload)),
                           "Content-Type": "application/json"}
        for name, value in (headers or {}).items():
            if value is None:
                handler.headers.pop(name, None)
            else:
                handler.headers[name] = value
        handler.rfile = io.BytesIO(payload)
        responses = []
        handler.json_response = lambda status, response: responses.append((status, response))
        with patch("agentwatch.server.now", return_value=CURRENT):
            handler.do_POST()
        self.assertEqual(len(responses), 1)
        return responses[0]

    def test_explicit_confirmation_dismisses_local_reminder_only(self):
        self.assertEqual(self.dispatch(), (200, {"dismissed": True}))
        self.assertEqual(self.snapshot()["attention_counts"]["pending"], 0)
        row = self.store.db.execute("SELECT resolved_at FROM interaction_requests").fetchone()
        self.assertIsNone(row[0])

    def test_isolated_demo_can_dismiss_artificial_reminder(self):
        with patch("agentwatch.server.jump", side_effect=AssertionError("No app navigation")) as navigate:
            self.assertEqual(self.dispatch(demo=True), (200, {"dismissed": True}))
        navigate.assert_not_called()

    def test_missing_or_wrong_cookie_cannot_dismiss(self):
        for value in [None, "agentwatch=wrong-token", "other=" + COOKIE]:
            with self.subTest(cookie=value):
                self.assertEqual(self.dispatch(headers={"Cookie": value})[0], 401)
        self.assertEqual(self.snapshot()["attention_counts"]["pending"], 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM attention_dismissals").fetchone()[0], 0)

    def test_missing_or_wrong_csrf_cannot_dismiss(self):
        for value in [None, "wrong-token"]:
            with self.subTest(csrf=value):
                self.assertEqual(self.dispatch(headers={"X-AgentWatch-CSRF": value})[0], 403)
        self.assertEqual(self.snapshot()["attention_counts"]["pending"], 1)

    def test_untrusted_host_origin_or_cross_site_request_cannot_dismiss(self):
        for headers in [{"Host": "evil.example"}, {"Origin": "https://evil.example"},
                        {"Sec-Fetch-Site": "cross-site"}]:
            with self.subTest(headers=headers):
                self.assertEqual(self.dispatch(headers=headers)[0], 403)
        self.assertEqual(self.snapshot()["attention_counts"]["pending"], 1)

    def test_matching_same_origin_is_allowed(self):
        self.assertEqual(self.dispatch(headers={"Origin": "http://" + HOST}), (200, {"dismissed": True}))

    def test_version_must_be_a_64_digit_hex_string(self):
        for value in [None, True, 123, "", "a" * 63, "a" * 65, "z" * 64, " " + "a" * 64]:
            with self.subTest(value=value):
                self.assertEqual(self.dispatch(body={"id": self.sid, "version": value})[0], 400)
        self.assertEqual(self.dispatch(body={"id": self.sid})[0], 400)
        self.assertEqual(self.snapshot()["attention_counts"]["pending"], 1)

    def test_a_valid_but_unknown_version_is_a_conflict(self):
        self.assertEqual(self.dispatch(body={"id": self.sid, "version": "0" * 64})[0], 409)
        self.assertEqual(self.snapshot()["attention_counts"]["pending"], 1)

    def test_version_change_after_a_new_request_returns_conflict(self):
        old = self.version()
        self.store.open_interaction(self.sid, "new-artificial-question", 0, "waiting_user", AT + 1)
        self.assertEqual(self.dispatch(body={"id": self.sid, "version": old})[0], 409)
        self.assertEqual(self.snapshot()["sessions"][0]["attention"]["waiting_user"], 2)

    def test_already_dismissed_or_resolved_request_returns_conflict(self):
        version = self.version()
        self.assertEqual(self.dispatch(body={"id": self.sid, "version": version})[0], 200)
        self.assertEqual(self.dispatch(body={"id": self.sid, "version": version})[0], 409)
        self.store.open_interaction(self.sid, "new-artificial-question", 0, "waiting_user", AT + 1)
        version = self.version()
        self.store.resolve_interaction(self.sid, "new-artificial-question", AT + 2)
        self.assertEqual(self.dispatch(body={"id": self.sid, "version": version})[0], 409)

    def test_missing_hidden_child_or_unknown_root_returns_not_found(self):
        version = self.version()
        for identity in [None, "codex:missing"]:
            with self.subTest(identity=identity):
                self.assertEqual(self.dispatch(body={"id": identity, "version": version})[0], 404)
        for extra in [{"archived": 1}, {"internal": 1}, {"internal_memory": 1},
                      {"role": "child"}, {"role": "unknown"}]:
            with self.subTest(extra=extra):
                self.store.update(self.sid, {"archived": 0, "internal": 0, "internal_memory": 0, "role": "main", **extra})
                self.assertEqual(self.dispatch(body={"id": self.sid, "version": version})[0], 404)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM attention_dismissals").fetchone()[0], 0)

    def test_unknown_only_root_has_no_dismissable_human_request(self):
        self.store.resolve_interaction(self.sid, "artificial-question", AT + 1)
        self.store.open_interaction(self.sid, "unknown-delivery", 0, "unknown", AT + 2)
        self.assertEqual(self.dispatch(body={"id": self.sid, "version": "0" * 64})[0], 409)
        self.assertGreater(self.snapshot()["sessions"][0]["attention"]["unknown"], 0)

    def test_malformed_request_body_does_not_dismiss(self):
        for body in [[], "artificial", 123]:
            with self.subTest(body=body):
                self.assertEqual(self.dispatch(body=body)[0], 400)
        self.assertEqual(self.snapshot()["attention_counts"]["pending"], 1)


if __name__ == "__main__":
    unittest.main()
