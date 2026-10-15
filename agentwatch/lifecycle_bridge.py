#!/usr/bin/env python3
"""Silent, observational Codex/WorkBuddy lifecycle hooks.

No decisions, context, prompts, tool inputs/results, or credentials are emitted.
This command always succeeds so monitoring cannot block the original Agent.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agentwatch.common import safe_id


CODEX_EVENTS = ["SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PermissionRequest",
                "PostToolUse", "PreCompact", "PostCompact", "SubagentStart", "SubagentStop", "Stop", "Interrupt"]
WORKBUDDY_EVENTS = ["SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PermissionRequest",
                    "PostToolUse", "PostToolUseFailure", "Stop", "StopFailure", "Notification",
                    "PreCompact", "PostCompact", "SubagentStart", "SubagentStop"]
EVENTS = {"codex": CODEX_EVENTS, "workbuddy": WORKBUDDY_EVENTS}


def sanitize(doc, tool, at=None, process=None):
    if not isinstance(doc, dict) or tool not in EVENTS or not safe_id(doc.get("session_id")):
        return None
    event = doc.get("hook_event_name")
    if event not in EVENTS[tool]:
        return None
    row = {"agentwatch": 1, "tool": tool, "id": str(uuid.uuid4()), "at": at or time.time(),
           "event": event, "session_id": doc["session_id"], "surface": "unknown"}
    cwd = doc.get("cwd")
    if isinstance(cwd, str) and cwd.startswith("/") and len(cwd) < 4096:
        row["cwd"] = cwd
    for key in ["turn_id", "tool_use_id", "agent_id"]:
        if safe_id(doc.get(key)):
            row[key] = doc[key]
    name = doc.get("tool_name")
    if isinstance(name, str) and len(name) < 160 and all(c.isalnum() or c in "_-:." for c in name):
        row["tool_name"] = name
    if doc.get("notification_type") in ["permission_prompt", "idle_prompt", "agent_needs_input", "elicitation_dialog"]:
        row["notification_type"] = doc["notification_type"]
    if doc.get("source") in ["startup", "resume", "clear", "compact", "fork"]:
        row["source"] = doc["source"]
    if process:
        row.update(pid=process["pid"], process_birth=process["birth"], tty=process["tty"], surface=process["surface"])
    return row


def agent_ancestor(tool):
    from agentwatch.collector import process_inventory
    inventory = process_inventory()
    pid = os.getppid()
    for _ in range(12):
        process = inventory.get(pid)
        if not process:
            break
        if process["tool"] == tool:
            return process
        pid = process["ppid"]
    return None


def main():
    at = time.time()
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--tool", choices=list(EVENTS), required=True)
    args = parser.parse_args()
    raw = sys.stdin.buffer.read(2_000_001)
    if len(raw) > 2_000_000:
        return
    doc = json.loads(raw)
    row = sanitize(doc, args.tool, at=at, process=agent_ancestor(args.tool))
    if row:
        from agentwatch.bridge import append_private
        append_private(Path(args.data_dir).expanduser().resolve() / "lifecycle-events.jsonl", row)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
