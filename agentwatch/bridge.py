#!/usr/bin/env python3
"""Silent Claude hook adapter. Only whitelisted metadata is written to disk.

The source payload is transient. Nothing is returned to the model and failures
always leave Claude's permission and execution decisions untouched.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agentwatch.common import now, number, private_dir, safe_id, short_text
from agentwatch.collector import process_inventory
from agentwatch.subscriptions import claude_windows


EVENTS = ["SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PermissionRequest", "PostToolUse",
          "PostToolUseFailure", "Stop", "StopFailure", "Notification", "PreCompact", "PostCompact", "TaskCreated", "TaskCompleted"]


def append_private(path, doc, unique=False):
    private_dir(path.parent)
    if path.is_symlink():
        return
    blob = (json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    fd = os.open(str(path), (os.O_RDWR if unique else os.O_WRONLY) | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if unique:
            if os.fstat(fd).st_size > 8_000_000 or blob in os.read(fd, 8_000_000).splitlines(keepends=True):
                return
        os.write(fd, blob)
    finally:
        os.close(fd)


def agent_ancestor():
    inventory = process_inventory()
    pid = os.getppid()
    for _ in range(10):
        p = inventory.get(pid)
        if not p:
            break
        if p["tool"] == "claude":
            return p
        pid = p["ppid"]
    return None


def project_path(value):
    return value if isinstance(value, str) and value.startswith("/") and len(value) < 4096 else None


def sanitize(doc, statusline=False, process=None, at=None):
    if not isinstance(doc, dict) or not safe_id(doc.get("session_id")):
        return None
    event = "Statusline" if statusline else doc.get("hook_event_name")
    if event not in EVENTS and event != "Statusline":
        return None
    row = {"agentwatch": 1, "id": str(uuid.uuid4()), "at": at or now(), "event": event,
           "session_id": doc["session_id"], "cwd": project_path(doc.get("cwd")), "surface": "unknown"}
    for key in ["tool_use_id", "prompt_id", "task_id", "agent_id"]:
        if safe_id(doc.get(key)):
            row[key] = doc[key]
    name = doc.get("tool_name")
    if isinstance(name, str) and len(name) < 160 and all(c.isalnum() or c in "_-:." for c in name):
        row["tool_name"] = name
    notification = doc.get("notification_type")
    if notification in ["permission_prompt", "idle_prompt", "agent_needs_input"]:
        row["notification_type"] = notification
    if process:
        row.update(pid=process["pid"], process_birth=process["birth"], tty=process["tty"], surface=process["surface"])
    if statusline:
        windows = claude_windows(doc.get("rate_limits"))
        if windows:
            row["rate_limits"] = {w["key"]: {"used_percentage": w["used"], "resets_at": w["resets_at"]} for w in windows}
        context = doc.get("context_window")
        if isinstance(context, dict):
            current = context.get("current_usage")
            valid = isinstance(current, dict) and number(current.get("input_tokens")) is not None
            row["context_valid"] = valid
            used, limit = number(context.get("total_input_tokens")), number(context.get("context_window_size"))
            if valid and used is not None:
                row["context_used"] = used
            if valid and limit:
                row["context_limit"] = limit
    return row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--agentwatch-hook", action="store_true")
    parser.add_argument("--agentwatch-statusline", action="store_true")
    args = parser.parse_args()
    raw = sys.stdin.buffer.read(2_000_001)
    data = Path(args.data_dir).expanduser().resolve()
    doc = None
    try:
        if len(raw) <= 2_000_000:
            doc = json.loads(raw)
            row = sanitize(doc, args.agentwatch_statusline, agent_ancestor())
            if row:
                append_private(data / "claude-events.jsonl", row)
                path = project_path(doc.get("transcript_path"))
                if path and path.endswith(".jsonl"):
                    append_private(data / "transcripts.jsonl", {"session_id": row["session_id"], "path": path}, unique=True)
    except Exception:
        pass
    if args.agentwatch_statusline:
        original = None
        try:
            manifest = json.loads((data / "claude-installation.json").read_text())
            original = manifest.get("old_statusline")
        except (OSError, ValueError):
            pass
        if isinstance(original, dict) and isinstance(original.get("command"), str):
            try:
                # This is the user's pre-existing command, never a hook-input value.
                proc = subprocess.run(original["command"], shell=True, executable="/bin/sh", input=raw,
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
                sys.stdout.buffer.write(proc.stdout)
            except Exception:
                pass
        else:
            percent = None
            context = doc.get("context_window", {}) if isinstance(doc, dict) else {}
            if isinstance(context, dict):
                percent = number(context.get("used_percentage"))
            print("Claude" + (" · 上下文 " + str(percent) + "%" if percent is not None else ""))
    return 0


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)

