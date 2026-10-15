from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agentwatch.navigation import NavigationError, jump


class NavigationAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        platform = patch("agentwatch.navigation.sys.platform", "darwin")
        platform.start()
        self.addCleanup(platform.stop)
        self.commands = []

    def runner(self, command, **options):
        self.commands.append(command)
        return SimpleNamespace(returncode=0, stdout="focused")

    def test_missing_invalid_and_empty_rollouts_never_open_codex(self):
        empty = self.root / "empty.jsonl"
        empty.touch()
        directory = self.root / "directory"
        directory.mkdir()
        for kind in ["codex", "codex_vscode"]:
            with self.assertRaises(NavigationError):
                jump({"tool": "codex", "source_id": "synthetic-session"}, kind, self.runner)
        invalid_paths = [None, "", "relative.jsonl", "/invalid\0path", [], 1, str(empty),
                         str(directory), str(self.root / "missing.jsonl")]
        for path in invalid_paths:
            for kind in ["codex", "codex_vscode"]:
                with self.subTest(path=path, kind=kind):
                    session = {"tool": "codex", "source_id": "synthetic-session", "_rollout_path": path}
                    with self.assertRaises(NavigationError):
                        jump(session, kind, self.runner)
        self.assertEqual(self.commands, [])

    def test_nonempty_local_rollout_keeps_both_codex_routes(self):
        rollout = self.root / "synthetic.jsonl"
        rollout.write_text('{"type":"synthetic"}\n')
        session = {"tool": "codex", "source_id": "synthetic-session", "_rollout_path": str(rollout)}
        for kind in ["codex", "codex_vscode"]:
            self.assertTrue(jump(session, kind, self.runner)["opened"])
        self.assertEqual(self.commands, [
            ["open", "codex://threads/synthetic-session"],
            ["open", "vscode://openai.chatgpt/local/synthetic-session"],
        ])

    def test_removed_or_unreadable_rollout_is_rechecked_before_open(self):
        rollout = self.root / "removed.jsonl"
        rollout.write_text('{"type":"synthetic"}\n')
        session = {"tool": "codex", "source_id": "synthetic-session", "_rollout_path": str(rollout)}
        rollout.unlink()
        with self.assertRaises(NavigationError):
            jump(session, "codex", self.runner)
        with patch("agentwatch.navigation.Path.is_file", return_value=True), \
             patch("agentwatch.navigation.Path.stat", side_effect=PermissionError):
            with self.assertRaises(NavigationError):
                jump(session, "codex_vscode", self.runner)
        self.assertEqual(self.commands, [])

    def test_application_and_live_terminal_routes_do_not_require_rollout(self):
        for tool in ["claude", "workbuddy"]:
            session = {"tool": tool, "source_id": "synthetic-session"}
            self.assertTrue(jump(session, tool, self.runner)["opened"])
        terminal = {"tool": "codex", "source_id": "synthetic-session", "liveness": "online", "tty": "/dev/ttys123"}
        self.assertTrue(jump(terminal, "terminal", self.runner)["opened"])
        self.assertEqual(self.commands[:2], [["open", "-a", "Claude"], ["open", "-a", "WorkBuddy"]])
        self.assertEqual(self.commands[2][0], "osascript")
        self.assertNotIn("do script", self.commands[2][-1])


if __name__ == "__main__":
    unittest.main()
