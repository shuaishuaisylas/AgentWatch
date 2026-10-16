from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agentwatch.cursor_bridge import EVENTS, MAX_INPUT_BYTES, sanitize


AT = 1_792_000_000.5
SECRET = "PRIVATE_CURSOR_PROMPT_OUTPUT_EMAIL_TOKEN_MUST_NOT_PERSIST"


class CursorBridgeTests(unittest.TestCase):
    def event(self, event="postToolUse", **extra):
        return {"conversation_id": "artificial-conversation", "generation_id": "artificial-turn",
                "hook_event_name": event, "cursor_version": "3.4.2", "workspace_roots": ["/example/project"], **extra}

    def test_supported_observation_events_keep_native_names_and_identity(self):
        for event in EVENTS:
            with self.subTest(event=event):
                row = sanitize(self.event(event), at=AT)
                self.assertEqual((row["tool"], row["event"], row["session_id"], row["turn_id"]),
                                 ("cursor", event, "artificial-conversation", "artificial-turn"))
                self.assertEqual((row["at"], row["cwd"], row["version"], row["surface"]),
                                 (AT, "/example/project", "3.4.2", "desktop"))
                self.assertNotIn("role", row)

    def test_permission_and_unneeded_body_events_are_not_supported(self):
        for event in ["preToolUse", "subagentStart", "subagentStop", "beforeShellExecution",
                      "beforeMCPExecution", "afterAgentResponse", "workspaceOpen", "unknown"]:
            with self.subTest(event=event):
                self.assertIsNone(sanitize(self.event(event), at=AT))

    def test_explicit_child_context_and_conflicting_lifecycle_identity_are_rejected(self):
        for key in ["agent_id", "subagent_id", "parent_conversation_id", "parent_tool_call_id"]:
            self.assertIsNone(sanitize(self.event(**{key: "artificial-child"}), at=AT))
        for event in ["sessionStart", "sessionEnd"]:
            self.assertIsNone(sanitize(self.event(event, session_id="different"), at=AT))
            self.assertIsNotNone(sanitize(self.event(event, session_id="artificial-conversation"), at=AT))

    def test_invalid_input_identity_and_times_do_not_enter_transport(self):
        for doc in [None, [], "text", {}, self.event(conversation_id="../bad"), self.event(conversation_id="")]:
            self.assertIsNone(sanitize(doc, at=AT))
        for stamp in [0, -1, True, "123", float("nan"), float("inf")]:
            self.assertIsNone(sanitize(self.event(), at=stamp))

    def test_body_email_transcript_and_unknown_fields_are_dropped(self):
        doc = self.event("afterAgentThought", text=SECRET, user_email=SECRET,
                         transcript_path="/private/" + SECRET, prompt=SECRET,
                         tool_input={"command": SECRET}, tool_output=SECRET,
                         model_params=[{"value": SECRET}], error_message=SECRET,
                         task=SECRET, summary=SECRET, token=SECRET, env={"TOKEN": SECRET})
        row = sanitize(doc, at=AT)
        self.assertNotIn(SECRET, json.dumps(row))
        self.assertEqual(set(row), {"agentwatch", "tool", "id", "at", "event", "session_id", "turn_id", "surface", "cwd", "version"})

    def test_paths_versions_and_tool_names_have_strict_metadata_validation(self):
        row = sanitize(self.event(tool_name="Shell", tool_use_id="call-123", cwd="/untrusted/subdirectory"), at=AT)
        self.assertEqual((row["tool_name"], row["tool_use_id"], row["cwd"]), ("Shell", "call-123", "/example/project"))
        self.assertEqual(sanitize(self.event(tool_name="MCP:server.tool"), at=AT)["tool_name"], "MCP:server.tool")
        for roots in [[], "bad", ["relative"], ["/bad\npath"], [123], ["/" + "x" * 4096], ["relative", "/do-not-guess"]]:
            self.assertNotIn("cwd", sanitize(self.event(workspace_roots=roots), at=AT))
        for version in [SECRET, "3.4.2\n" + SECRET, [], "1"]:
            self.assertNotIn("version", sanitize(self.event(cursor_version=version), at=AT))
        for name in [SECRET, "MCP:tool with spaces", "Shell\n" + SECRET, {}, None]:
            self.assertNotIn("tool_name", sanitize(self.event(tool_name=name), at=AT))

    def test_only_known_stop_and_end_reasons_are_kept(self):
        for reason in ["completed", "aborted", "error"]:
            self.assertEqual(sanitize(self.event("stop", status=reason), at=AT)["stop_reason"], reason)
        self.assertNotIn("stop_reason", sanitize(self.event("stop", status=SECRET), at=AT))
        self.assertNotIn("stop_reason", sanitize(self.event("stop"), at=AT))
        self.assertEqual(sanitize(self.event("sessionEnd", reason="window_close"), at=AT)["stop_reason"], "window_close")
        row = sanitize(self.event("postToolUseFailure", failure_type="permission_denied", is_interrupt=False, error_message=SECRET), at=AT)
        self.assertEqual((row["failure_type"], row["is_interrupt"]), ("permission_denied", False))
        self.assertNotIn(SECRET, json.dumps(row))

    def test_executable_is_silent_and_private_for_valid_invalid_and_large_input(self):
        with tempfile.TemporaryDirectory(prefix="cursor-bridge-") as directory:
            bridge = Path(__file__).resolve().parents[1] / "agentwatch/cursor_bridge.py"
            command = [sys.executable, str(bridge), "--data-dir", directory]
            inputs = [b"broken", b"[]", b"x" * (MAX_INPUT_BYTES + 1),
                      json.dumps(self.event("beforeSubmitPrompt", prompt=SECRET)).encode(),
                      json.dumps(self.event("preToolUse", tool_input={"command": SECRET})).encode()]
            for raw in inputs:
                result = subprocess.run(command, input=raw, capture_output=True, timeout=10)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))
            transport = Path(directory) / "lifecycle-events.jsonl"
            self.assertEqual(len(transport.read_text().splitlines()), 1)
            self.assertNotIn(SECRET, transport.read_text())
            self.assertEqual(os.stat(transport).st_mode & 0o777, 0o600)

    def test_transport_symlink_does_not_write_to_target(self):
        with tempfile.TemporaryDirectory(prefix="cursor-symlink-") as directory:
            root = Path(directory)
            target = root / "untouched.txt"
            target.write_text("unchanged")
            (root / "lifecycle-events.jsonl").symlink_to(target)
            bridge = Path(__file__).resolve().parents[1] / "agentwatch/cursor_bridge.py"
            result = subprocess.run([sys.executable, str(bridge), "--data-dir", directory],
                                    input=json.dumps(self.event()).encode(), capture_output=True, timeout=10)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))
            self.assertEqual(target.read_text(), "unchanged")


if __name__ == "__main__":
    unittest.main()
