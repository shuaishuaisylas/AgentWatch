#!/usr/bin/env python3
"""Observational Cursor hooks; only allowlisted metadata reaches the transport.

The permission hooks preToolUse/subagentStart are deliberately unsupported:
Cursor requires a permission response for them, which would make this observer
participate in the user's policy. Supported hooks emit no response or decisions.
The installed handlers explicitly use failClosed=false; Cursor documents that
no output does not block these non-permission hooks.
Official schema: https://cursor.com/docs/hooks
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agentwatch.common import safe_id


EVENTS = ["sessionStart", "sessionEnd", "beforeSubmitPrompt", "postToolUse",
          "postToolUseFailure", "stop", "preCompact", "afterAgentThought"]
TOOL_NAMES = {"Shell", "Read", "Write", "Grep", "Delete", "Task", "Glob",
              "Edit", "MultiEdit", "WebSearch", "WebFetch", "AskQuestion",
              "TodoWrite", "UpdateTodos"}
MAX_INPUT_BYTES = 2_000_000


def sanitize(doc, at=None):
    """Translate native Cursor hook input without copying arbitrary fields."""
    if not isinstance(doc, dict):
        return None
    event = doc.get("hook_event_name")
    identity = safe_id(doc.get("conversation_id"))
    if event not in EVENTS or not identity:
        return None
    # This first adapter covers composer roots only. Explicit child context is
    # rejected rather than silently reparented or presented as another root.
    if any(doc.get(key) is not None for key in ["subagent_id", "parent_conversation_id", "agent_id", "parent_tool_call_id"]):
        return None
    stamp = time.time() if at is None else at
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or stamp <= 0:
        return None
    # Lifecycle aliases must agree; never reassign a native identity.
    if event in ["sessionStart", "sessionEnd"] and doc.get("session_id") is not None and doc.get("session_id") != identity:
        return None
    row = {"agentwatch": 1, "tool": "cursor", "id": str(uuid.uuid4()),
           "at": stamp, "event": event, "session_id": identity, "surface": "desktop"}
    for source, target in [("generation_id", "turn_id"), ("tool_use_id", "tool_use_id")]:
        value = safe_id(doc.get(source))
        if value:
            row[target] = value
    roots = doc.get("workspace_roots")
    if isinstance(roots, list) and roots:
        root = roots[0]
        if isinstance(root, str) and root.startswith("/") and len(root) < 4096 and not any(ord(c) < 32 for c in root):
            row["cwd"] = root
    version = doc.get("cursor_version")
    if isinstance(version, str) and re.fullmatch(r"\d+(?:\.\d+){1,3}(?:[-+][A-Za-z0-9.-]+)?", version) and len(version) <= 80:
        row["version"] = version
    name = doc.get("tool_name")
    if isinstance(name, str) and (name in TOOL_NAMES or re.fullmatch(r"MCP:[A-Za-z0-9_.:-]{1,150}", name)):
        row["tool_name"] = name
    reason = doc.get("status") if event == "stop" else doc.get("reason") if event == "sessionEnd" else None
    allowed_reasons = ["completed", "aborted", "error"] if event == "stop" else ["completed", "aborted", "error", "window_close", "user_close"]
    if isinstance(reason, str) and reason in allowed_reasons:
        row["stop_reason"] = reason
    if event == "postToolUseFailure":
        if doc.get("failure_type") in ["timeout", "error", "permission_denied"]:
            row["failure_type"] = doc["failure_type"]
        if isinstance(doc.get("is_interrupt"), bool):
            row["is_interrupt"] = doc["is_interrupt"]
    return row


def main():
    at = time.time()
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    args = parser.parse_args()
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        return
    row = sanitize(json.loads(raw), at=at)
    if row:
        from agentwatch.bridge import append_private
        append_private(Path(args.data_dir).expanduser().resolve() / "lifecycle-events.jsonl", row)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    # Never emit continue, permission, followup_message, env or context fields.
    sys.exit(0)
