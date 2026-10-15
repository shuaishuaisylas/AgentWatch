from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agentwatch.collector import Collector, RecordParser
from agentwatch.common import timestamp
from agentwatch.server import Handler
from agentwatch.store import Store


AT = timestamp("2026-10-15T10:00:00+08:00")


class CodexSessionAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.codex = self.root / "codex"
        self.store = Store(self.data / "watch.sqlite")
        self.addCleanup(self.store.close)
        self.parser = RecordParser(self.store)
        self.collector = Collector(self.store, self.codex, self.root / "claude", self.data,
                                   self.root / "desktop", workbuddy_home=self.root / "workbuddy")
        self.server = SimpleNamespace(demo=False, store=self.store, lifecycle_configs={})

    def hook(self, identity="synthetic"):
        return self.parser.lifecycle({
            "agentwatch": 1, "tool": "codex", "event": "UserPromptSubmit",
            "id": "event-" + identity, "session_id": identity, "at": AT,
            "surface": "desktop", "cwd": str(self.root / "synthetic-project"),
        }, {})

    def rollout(self, identity="synthetic", parse=True):
        path = self.codex / "sessions" / (identity + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = {"type": "session_meta", "timestamp": AT, "payload": {
            "id": identity, "source": "cli", "originator": "codex_cli_rs",
            "cwd": str(self.root / "synthetic-project"),
        }}
        path.write_text(json.dumps(doc) + "\n")
        if parse:
            self.collector.read_file(str(path), "codex", None)
        return path

    def card(self, identity="synthetic"):
        return next(card for card in self.store.snapshot({"role": "all"}, {}, AT + 1)["sessions"]
                    if card["id"] == "codex:" + identity)

    def post_jump(self, identity="synthetic", kind=None):
        handler = Handler.__new__(Handler)
        handler.server = self.server
        handler.path = "/api/jump"
        handler.trust = lambda: True
        handler.authorized = lambda **options: True
        responses = []
        handler.json_response = lambda status, value: responses.append((status, value))
        body = {"id": "codex:" + identity}
        if kind is not None:
            body["kind"] = kind
        raw = json.dumps(body).encode()
        handler.rfile = io.BytesIO(raw)
        handler.headers = {"Content-Length": str(len(raw))}
        handler.do_POST()
        self.assertEqual(len(responses), 1)
        return responses[0]

    def test_hook_only_session_stays_visible_without_promising_recovery(self):
        self.hook()
        card = self.card()
        self.assertEqual(card["state"], "active")
        self.assertEqual(card["role"], "unknown")
        self.assertEqual(card["coverage"], "status_only")
        self.assertFalse(card["jump"]["available"])
        self.assertEqual(card["jump"]["kind"], "none")
        self.assertIsNone(card["jump"]["precision"])
        self.assertNotIn("_rollout_path", card)

    def test_existing_file_must_be_parsed_and_linked_to_the_session(self):
        sid = self.hook()
        path = self.rollout(parse=False)
        self.assertFalse(self.card()["jump"]["available"])
        self.store.save_file(str(path), "codex", sid, "synthetic-inode", 0, {}, 0, path.stat().st_size)
        self.assertFalse(self.card()["jump"]["available"])
        self.collector.read_file(str(path), "codex", None)
        card = self.card()
        self.assertTrue(card["jump"]["available"])
        self.assertEqual(card["jump"]["precision"], "指定会话")
        self.assertNotEqual(card["coverage"], "status_only")
        self.assertNotIn("_rollout_path", card)

    def test_empty_non_jsonl_and_other_source_files_do_not_authorize_recovery(self):
        sid = self.hook()
        for filename, tool, text in [("empty.jsonl", "codex", ""),
                                     ("metadata.log", "codex", "synthetic"),
                                     ("another-source.jsonl", "lifecycle", "synthetic")]:
            with self.subTest(filename=filename):
                path = self.root / filename
                path.write_text(text)
                self.store.save_file(str(path), tool, sid, "synthetic-inode", 1, {}, 0, len(text))
                self.assertFalse(self.card()["jump"]["available"])

    def test_removing_a_previously_available_rollout_disables_the_next_card(self):
        self.hook()
        path = self.rollout()
        self.assertTrue(self.card()["jump"]["available"])
        path.unlink()
        self.assertFalse(self.card()["jump"]["available"])

    def test_jump_route_cannot_bypass_missing_rollout_with_explicit_kind(self):
        sid = self.hook()
        with patch("agentwatch.server.jump", side_effect=AssertionError("system navigation")) as navigate:
            for surface in ["desktop", "vscode", "unknown"]:
                self.store.update(sid, {"surface": surface})
                for kind in [None, "codex", "codex_vscode"]:
                    with self.subTest(surface=surface, kind=kind):
                        status, payload = self.post_jump(kind=kind)
                        self.assertEqual(status, 400)
                        self.assertIn("error", payload)
            navigate.assert_not_called()

    def test_jump_route_rechecks_rollout_deleted_after_snapshot(self):
        self.hook()
        path = self.rollout()
        self.assertTrue(self.card()["jump"]["available"])
        path.unlink()
        with patch("agentwatch.server.jump", side_effect=AssertionError("system navigation")) as navigate:
            for kind in [None, "codex", "codex_vscode"]:
                self.assertEqual(self.post_jump(kind=kind)[0], 400)
            navigate.assert_not_called()

    def test_hidden_sessions_and_descendants_cannot_be_opened_directly(self):
        for identity in ["parent", "child", "grandchild", "unrelated"]:
            self.hook(identity)
            self.rollout(identity)
            self.store.update("codex:" + identity, {"surface": "vscode"})
        self.store.update("codex:child", {"role": "child", "parent_id": "codex:parent"})
        self.store.update("codex:grandchild", {"role": "child", "parent_id": "codex:child"})
        with patch("agentwatch.server.jump", side_effect=AssertionError("system navigation")) as navigate:
            for flag in ["archived", "internal", "internal_memory"]:
                self.store.update("codex:parent", {"archived": 0, "internal": 0, "internal_memory": 0, flag: 1})
                cards = self.store.snapshot({"role": "all"}, {}, AT + 1)["sessions"]
                self.assertEqual([card["id"] for card in cards], ["codex:unrelated"])
                self.assertTrue(cards[0]["jump"]["available"])
                for identity in ["parent", "child", "grandchild"]:
                    for kind in [None, "codex", "codex_vscode"]:
                        with self.subTest(flag=flag, identity=identity, kind=kind):
                            self.assertEqual(self.post_jump(identity, kind)[0], 400)
            navigate.assert_not_called()

    def test_jump_route_supplies_private_rollout_for_default_and_desktop_backup(self):
        sid = self.hook()
        path = self.rollout()
        self.store.update(sid, {"surface": "vscode"})
        with patch("agentwatch.server.jump", return_value={"opened": True}) as navigate:
            for requested, expected in [(None, "codex_vscode"), ("codex", "codex")]:
                self.assertEqual(self.post_jump(kind=requested)[0], 200)
                session, kind = navigate.call_args.args
                self.assertEqual(kind, expected)
                self.assertEqual(session["_rollout_path"], str(path))


if __name__ == "__main__":
    unittest.main()
