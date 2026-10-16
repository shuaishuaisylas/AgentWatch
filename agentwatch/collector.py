from __future__ import annotations

import json
import fcntl
import os
import re
import sqlite3
import subprocess
import threading
import time
from contextlib import closing
from pathlib import Path

from .common import fingerprint, now, number, safe_id, short_text, timestamp
from .codex_names import read_codex_names
from .subscriptions import backfill_codex, record_claude, record_codex
from .lifecycle_bridge import EVENTS as LIFECYCLE_EVENTS
from .cursor_events import collect_cursor


def workbuddy_transition(raw, source_id):
    """Read only the SDK's typed state transitions, never diagnostic bodies."""
    try:
        stamp, marker, payload = raw.decode("utf-8").split(" ", 2)
        if marker != "state-machine:transition":
            return None
        doc = json.loads(payload)
        at = timestamp(stamp)
        if not isinstance(doc, dict) or at is None or not safe_id(source_id) or doc.get("conversationId") != source_id:
            return None
        signal, target = doc.get("input"), doc.get("to")
        signals = {"PROMPT_SENT", "PHASE_WORKING", "PHASE_PLANNING", "PERMISSION_REQUESTED",
                   "PERMISSION_RESOLVED", "TURN_COMPLETED", "TURN_CANCELLED", "TURN_FAILED"}
        if not isinstance(signal, str) or signal not in signals or target not in ["working", "planning", "pending", "idle", "error", "failed", "terminated", "archived"]:
            return None
        return {"session_id": source_id, "at": at, "signal": signal, "target": target}
    except (ValueError, UnicodeError):
        return None


def codex_source(value):
    if isinstance(value, str) and value.startswith("{"):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def internal_codex(source, thread_source=None):
    source = codex_source(source)
    subagent = source.get("subagent") if isinstance(source, dict) else None
    return thread_source == "guardian_review" or (isinstance(subagent, dict) and subagent.get("other") == "guardian")


def surface_for(originator="", executable="", tty=None):
    originator = originator if isinstance(originator, str) else ""
    executable = executable if isinstance(executable, str) else ""
    value = (originator + " " + executable).lower()
    if "vscode" in value or ".vscode/" in value or "agent-host/" in value:
        return "vscode"
    if "desktop" in value or "chatgpt.app" in value or "/application support/claude/" in value or "claude.app" in value:
        return "desktop"
    if tty or "cli" in originator.lower():
        return "terminal"
    return "unknown"


def process_inventory():
    """No command-line arguments, terminal text, environment, or credentials."""
    try:
        r = subprocess.run(["ps", "-axo", "pid=,ppid=,lstart=,tty=,comm="], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    result = {}
    for line in r.stdout.splitlines():
        fields = line.strip().split(None, 8)
        if len(fields) != 9:
            continue
        try:
            pid, ppid = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        executable = fields[8]
        base = Path(executable).name.lower()
        tool = "codex" if base == "codex" else None
        if base in ["claude", "claude-code"] or "/claude/versions/" in executable.lower():
            if "/contents/macos/claude" not in executable.lower():
                tool = "claude"
        if base == "workbuddy" and "/workbuddy.app/" in executable.lower():
            tool = "workbuddy"
        tty = "/dev/" + fields[7] if re.fullmatch(r"ttys\d+", fields[7]) else None
        result[pid] = {"pid": pid, "ppid": ppid, "birth": " ".join(fields[2:7]), "tty": tty,
                       "executable": executable, "tool": tool, "surface": surface_for(executable=executable, tty=tty)}
    return result


class RecordParser:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def question_tool(name):
        if not isinstance(name, str):
            return None
        for prefix in ["", "functions.", "functions__"]:
            for known in ["request_user_input", "request_user_input_async", "AskUserQuestion", "ExitPlanMode"]:
                if name == prefix + known:
                    return known
        return None

    def codex_attention(self, doc, sid):
        """Whitelist correlation identities only, independently of execution state."""
        payload = doc.get("payload") if isinstance(doc, dict) else None
        at = timestamp(doc.get("timestamp")) if isinstance(doc, dict) else None
        if not sid or not isinstance(payload, dict) or at is None:
            return
        if doc.get("type") == "event_msg":
            if payload.get("type") == "task_complete":
                self.store.complete(sid, at)
            return
        if doc.get("type") != "response_item":
            return
        kind = payload.get("type")
        if kind == "function_call":
            name, call = self.question_tool(payload.get("name")), safe_id(payload.get("call_id"))
            if not call:
                return
            if not name:
                if isinstance(payload.get("name"), str) and payload["name"].rsplit(".", 1)[-1].rsplit("__", 1)[-1] in ["request_user_input", "request_user_input_async"]:
                    self.store.open_interaction(sid, call, 0, "unknown", at, "unsupported_question_namespace", True, "record")
                return
            arguments = payload.get("arguments")
            try:
                arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
            except (ValueError, TypeError):
                arguments = None
            questions = arguments.get("questions") if isinstance(arguments, dict) else None
            def valid_question(question):
                return isinstance(question, dict) and (isinstance(question.get("question"), str) or
                    (name == "request_user_input_async" and isinstance(question.get("title"), str)))
            if not isinstance(questions, list) or not 0 < len(questions) <= 64 or not all(valid_question(question) for question in questions):
                self.store.open_interaction(sid, call, 0, "unknown", at, "invalid_question_shape", name.endswith("_async"), "record")
                return
            for index in range(len(questions)):
                self.store.open_interaction(sid, call, index, "unknown" if name.endswith("_async") else "waiting_user", at, name, name.endswith("_async"), "record")
            self.store.interaction_alias(sid, payload.get("id"), call)
        elif kind == "function_call_output":
            call = safe_id(payload.get("call_id"))
            if not call:
                return
            rows = self.store.db.execute("SELECT asynchronous,item_index,tool_name FROM interaction_requests WHERE session_id=? AND call_id=?", (sid, call)).fetchall()
            if not rows:
                return
            output = payload.get("output")
            try:
                output = json.loads(output) if isinstance(output, str) else output
            except (ValueError, TypeError):
                return
            if any(row["asynchronous"] for row in rows):
                if isinstance(output, dict) and output.get("accepted") is True:
                    for row in rows:
                        if row["tool_name"] == "request_user_input_async":
                            self.store.open_interaction(sid, call, row["item_index"], "waiting_user", at, row["tool_name"], True, "record")
                return  # accepted=true is delivery, not a human answer.
            answers = output.get("answers") if isinstance(output, dict) else None
            if isinstance(answers, dict) and len(answers) >= len(rows) and all(isinstance(value, dict) and isinstance(value.get("answers"), list) and value["answers"] and all(isinstance(answer, str) for answer in value["answers"]) for value in answers.values()):
                self.store.resolve_interaction(sid, call, at)
        elif kind == "message" and payload.get("role") == "user":
            content = payload.get("content")
            if not isinstance(content, list) or len(content) != 1:
                return
            for part in content:
                if not isinstance(part, dict) or part.get("type") not in ["text", "input_text"] or not isinstance(part.get("text"), str):
                    continue
                wrapper = re.fullmatch(r"\s*<send_user_message_question_reply>\s*(.*?)\s*</send_user_message_question_reply>\s*", part["text"], re.DOTALL)
                for wrapped in [wrapper.group(1)] if wrapper else []:
                    try:
                        items = json.loads(wrapped)
                    except (ValueError, TypeError):
                        continue
                    items = [items] if isinstance(items, dict) else items
                    if not isinstance(items, list):
                        continue
                    for item in items:
                        try:
                            identity = json.loads(item.get("questionItemId")) if isinstance(item, dict) else None
                        except (ValueError, TypeError):
                            continue
                        if not all(isinstance(item.get(key), str) for key in ["questionItemId", "question", "answer"]):
                            continue
                        if not isinstance(identity, list) or len(identity) != 3:
                            continue
                        name, call, index = identity
                        if name == "request_user_input_async":
                            self.store.resolve_interaction(sid, call, at, item_index=index, tool_name=name)

    def lifecycle_attention(self, sid, doc, at, superseded=False):
        if superseded:
            return
        event = doc.get("event")
        name = self.question_tool(doc.get("tool_name"))
        call = safe_id(doc.get("tool_use_id"))
        if event in ["PreToolUse", "PermissionRequest"] and name:
            if call:
                self.store.open_interaction(sid, call, 0, "unknown" if name.endswith("_async") else "waiting_user", at, name, name.endswith("_async"), "hook")
            else:
                self.store.open_interaction(sid, "unidentified-question", 0, "unknown", at, name, name.endswith("_async"), "hook")
        elif event == "PermissionRequest":
            # Hooks run before reviewer decisions; automatic review is not human work.
            self.store.open_interaction(sid, call or "unidentified-approval", 0, "unknown", at, "", False, "hook")
        elif event == "Notification" and doc.get("notification_type") in ["permission_prompt", "agent_needs_input", "elicitation_dialog"]:
            notification = doc["notification_type"]
            kind = "waiting_approval" if notification == "permission_prompt" and not name else "waiting_user"
            if not call:
                waiting = self.store.db.execute("SELECT DISTINCT call_id FROM interaction_requests WHERE session_id=? AND resolved_at IS NULL AND kind='unknown' AND asynchronous=0 AND tool_name=''", (sid,)).fetchall()
                if len(waiting) == 1:
                    call = waiting[0][0]
                elif self.store.db.execute("SELECT 1 FROM interaction_requests WHERE session_id=? AND resolved_at IS NULL AND kind='waiting_user' LIMIT 1", (sid,)).fetchone():
                    return  # An untyped repeated notice does not invent another question.
            self.store.open_interaction(sid, call or "visible-" + notification, 0, kind if call else "unknown", at, name or "", False, "hook")
        elif event in ["PostToolUse", "PostToolUseFailure"] and call:
            rows = self.store.db.execute("SELECT asynchronous FROM interaction_requests WHERE session_id=? AND call_id=? AND resolved_at IS NULL", (sid, call)).fetchall()
            if rows and not any(row["asynchronous"] for row in rows):
                self.store.resolve_interaction(sid, call, at)
        if event == "Stop" and doc.get("stop_reason") not in ["cancelled", "interrupted", "aborted", "error"]:
            self.store.complete(sid, at)

    def codex(self, doc, cursor, fallback_sid=None):
        if not isinstance(doc, dict):
            return cursor.get("sid")
        payload = doc.get("payload")
        if not isinstance(payload, dict):
            return cursor.get("sid")
        kind = doc.get("type")
        if kind == "session_meta":
            if "chrome" in str(payload.get("originator", "")).lower():
                cursor["excluded"] = True
                return None
            if cursor.get("excluded"):
                return None
            if fallback_sid and payload.get("id") != fallback_sid.partition(":")[2]:
                cursor["ignore_prefix"] = True
                return fallback_sid
            cursor["ignore_prefix"] = False
            sid = fallback_sid or self.store.session("codex", payload.get("id"))
            if sid:
                cursor["sid"] = sid
                source = codex_source(payload.get("source"))
                role = "child" if isinstance(source, dict) and "subagent" in source else "main" if isinstance(source, str) and source in ["cli", "vscode", "exec", "appServer"] else "unknown"
                parent = payload.get("forked_from_id") or payload.get("parent_thread_id")
                if isinstance(source, dict):
                    child = source.get("subagent", {})
                    if isinstance(child, dict):
                        spawn = child.get("thread_spawn", child)
                        if isinstance(spawn, dict):
                            parent = spawn.get("parent_thread_id") or parent
                values = {"version": payload.get("cli_version", ""), "role": role}
                if internal_codex(source, payload.get("thread_source")):
                    values["internal"] = 1
                if safe_id(parent):
                    values["parent_id"] = "codex:" + parent
                surface = surface_for(payload.get("originator", ""))
                if surface != "unknown":
                    values["surface"] = surface
                at = timestamp(payload.get("timestamp") or doc.get("timestamp"))
                if at:
                    values["started_at"] = at
                git = payload.get("git")
                if isinstance(git, dict):
                    values["branch"] = git.get("branch", "")
                self.store.metadata(sid, payload.get("cwd"), **values)
            return sid
        sid = cursor.get("sid") or fallback_sid
        if cursor.get("excluded"):
            return None
        if not sid:
            return None
        if cursor.get("ignore_prefix"):
            return sid
        at = timestamp(doc.get("timestamp"))
        if at is None:
            self.store.update(sid, {"coverage": "missing_time"})
            return sid
        if kind == "turn_context":
            self.store.metadata(sid, payload.get("cwd"))
        self.codex_attention(doc, sid)
        if kind != "event_msg":
            return sid  # response text, tool arguments and results are discarded here.
        event = payload.get("type")
        if event == "token_count":
            self.codex_usage(sid, payload.get("info"), at, cursor)
            record_codex(self.store, payload.get("rate_limits"), at)
        elif event in ["task_started", "task_complete", "turn_aborted"]:
            turn = safe_id(payload.get("turn_id"))
            if turn:
                if event == "task_started":
                    self.store.db.execute("INSERT OR IGNORE INTO turns(session_id,source_id,started_at,state) VALUES(?,?,?,'started')", (sid, turn, at))
                    self.store.state(sid, "unknown", "本轮已启动 · 执行状态待确认", at)
                else:
                    state = "interrupted" if event == "turn_aborted" else "completed"
                    self.store.db.execute("INSERT INTO turns(session_id,source_id,ended_at,state) VALUES(?,?,?,?) ON CONFLICT(session_id,source_id) DO UPDATE SET ended_at=excluded.ended_at,state=excluded.state", (sid, turn, at, state))
                    self.store.state(sid, "idle", "本轮已中断" if event == "turn_aborted" else "本轮已结束", at)
        elif event in ["exec_command_begin", "mcp_tool_call_begin"]:
            self.store.state(sid, "unknown", "最近阶段：调用工具", at)
        elif event in ["exec_command_end", "mcp_tool_call_end"]:
            self.store.state(sid, "unknown", "最近阶段：工具返回", at)
        elif event in ["exec_approval_request", "apply_patch_approval_request", "request_user_input", "request_user_input_async"]:
            question = event in ["request_user_input", "request_user_input_async"]
            self.store.state(sid, "active" if event == "request_user_input_async" else "waiting_user" if question else "waiting_approval", "问题已发出 · 继续处理" if event == "request_user_input_async" else "等待你回复" if question else "等待你批准", at)
            if not question:
                self.store.open_interaction(sid, safe_id(payload.get("call_id")) or "unidentified-approval", 0, "unknown", at, "", False, "record")
        elif event in ["context_compacted", "context_compaction_started"]:
            self.store.invalidate_context(sid, at)
            self.store.state(sid, "unknown", "上下文已压缩 · 等待下一次快照", at)
        elif event in ["thread/status/changed", "thread_status_changed"]:
            status = payload.get("status", {})
            if isinstance(status, dict):
                name, flags = status.get("type"), status.get("activeFlags", [])
                flags = flags if isinstance(flags, list) else []
                state = {"idle": "idle", "systemError": "error", "notLoaded": "unknown", "active": "active"}.get(name)
                if state:
                    if "waitingOnApproval" in flags:
                        state = "waiting_approval"
                    elif "waitingOnUserInput" in flags:
                        state = "waiting_user"
                    self.store.state(sid, state, {"active": "正在执行", "idle": "空闲", "error": "运行错误", "unknown": "未加载", "waiting_approval": "等待你批准", "waiting_user": "等待你回复"}[state], at, "event")
        return sid

    def codex_usage(self, sid, info, at, cursor):
        if not isinstance(info, dict):
            return
        cumulative, last = info.get("total_token_usage"), info.get("last_token_usage")
        if not isinstance(cumulative, dict) or not isinstance(last, dict):
            return
        names = ["input_tokens", "output_tokens", "cached_input_tokens", "reasoning_output_tokens"]
        total = {k: number(cumulative.get(k)) for k in names}
        recent = {k: number(last.get(k)) for k in names}
        if any(total[k] is None or recent[k] is None for k in names[:2]):
            return
        total = {k: v or 0 for k, v in total.items()}
        recent = {k: v or 0 for k, v in recent.items()}
        previous = cursor.get("last_totals")
        previous_at = cursor.get("last_usage_at", 0)
        if at < previous_at:
            return
        if previous is None:
            delta = recent
            if total["input_tokens"] != recent["input_tokens"] or total["output_tokens"] != recent["output_tokens"]:
                self.store.update(sid, {"coverage": "partial_baseline"})
        elif total == previous:
            delta = None
        elif any(total[k] < previous.get(k, 0) for k in names[:2]):
            delta = recent
            cursor["epoch"] = cursor.get("epoch", 0) + 1
            self.store.update(sid, {"coverage": "counter_reset"})
        else:
            delta = {k: max(0, total[k] - previous.get(k, 0)) for k in names}
        if delta is not None:
            key = "cx:" + fingerprint(sid + json.dumps(total, sort_keys=True) + str(cursor.get("epoch", 0)))
            cache = min(delta["cached_input_tokens"], delta["input_tokens"]) if number(cumulative.get("cached_input_tokens")) is not None else None
            reasoning = delta["reasoning_output_tokens"] if number(cumulative.get("reasoning_output_tokens")) is not None else None
            self.store.usage(sid, key, at, delta["input_tokens"], delta["output_tokens"], cache, None, reasoning, "cumulative_delta")
        cursor["last_totals"], cursor["last_usage_at"] = total, at
        limit = number(info.get("model_context_window"))
        if limit:
            used = number(last.get("total_tokens"))
            self.store.context(sid, used if used is not None else recent["input_tokens"] + recent["output_tokens"], limit, at)
        self.store.update(sid, {"reported_input": total["input_tokens"], "reported_output": total["output_tokens"], "reported_cache_read": total["cached_input_tokens"], "reported_total": total["input_tokens"] + total["output_tokens"]})
        self.store.touch(sid, at)

    def claude(self, doc, cursor, fallback_sid=None):
        if not isinstance(doc, dict):
            return cursor.get("sid")
        source_id = safe_id(doc.get("sessionId"))
        agent_id = safe_id(doc.get("agentId")) or cursor.get("agent_id")
        if source_id and (doc.get("isSidechain") is True or cursor.get("child_file")):
            if agent_id:
                source_id += ":agent:" + agent_id
        child_source = source_id and ":agent:" in source_id
        sid = (self.store.session("claude", source_id) if child_source else None) or fallback_sid or cursor.get("sid") or (self.store.session("claude", source_id) if source_id else None)
        if not sid:
            return None
        cursor["sid"] = sid
        values = {}
        if ":agent:" in sid:
            values["parent_id"] = sid.split(":agent:", 1)[0]
            values["role"] = "child"
        if isinstance(doc.get("isSidechain"), bool) and ":agent:" not in sid:
            values["role"] = "child" if doc["isSidechain"] else "main"
        if doc.get("version"):
            values["version"] = doc["version"]
        if doc.get("gitBranch"):
            values["branch"] = doc["gitBranch"]
        self.store.metadata(sid, doc.get("cwd"), **values)
        kind = doc.get("type")
        # Only explicit custom titles are accepted, never first prompts or generated summaries.
        if kind == "custom-title" and isinstance(doc.get("customTitle"), str):
            self.store.update(sid, {"alias": short_text(doc["customTitle"])})
        at = timestamp(doc.get("timestamp"))
        if at is None:
            return sid
        if kind in ["user", "assistant", "system"]:
            self.store.touch(sid, at)
        if kind == "user":
            self.store.state(sid, "unknown", "最近阶段：输入已提交", at)
        if kind == "system" and doc.get("subtype") == "compact_boundary":
            self.store.invalidate_context(sid, at)
        if kind != "assistant" or doc.get("isSynthetic"):
            return sid
        msg = doc.get("message")
        if not isinstance(msg, dict):
            return sid
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            return sid
        fresh, output = number(usage.get("input_tokens")), number(usage.get("output_tokens"))
        if fresh is None or output is None:
            return sid
        cr, cw = number(usage.get("cache_read_input_tokens")), number(usage.get("cache_creation_input_tokens"))
        if cr is None or cw is None:
            self.store.update(sid, {"coverage": "partial_usage"})
        identity = safe_id(msg.get("id")) or safe_id(doc.get("uuid"))
        if not identity:
            self.store.update(sid, {"coverage": "missing_request_id"})
            return sid
        self.store.usage(sid, "cl:" + fingerprint(sid + identity), at, fresh + (cr or 0) + (cw or 0), output, cr, cw, kind="request", merge=True)
        if cr is not None and cw is not None:
            self.store.context(sid, fresh + cr + cw, None, at, estimated=True)
        stop = msg.get("stop_reason")
        if stop == "end_turn":
            self.store.state(sid, "idle", "最近一轮已回复", at)
        elif stop == "tool_use":
            self.store.state(sid, "unknown", "最近阶段：请求调用工具", at)
        return sid

    def hook(self, doc, cursor):
        if not isinstance(doc, dict) or doc.get("agentwatch") != 1:
            return None
        source_id = safe_id(doc.get("session_id"))
        event_id = safe_id(doc.get("id"))
        at = timestamp(doc.get("at"))
        if not source_id or not event_id or not at:
            return None
        if self.store.db.execute("INSERT OR IGNORE INTO hook_seen VALUES(?)", (event_id,)).rowcount == 0:
            return "claude:" + source_id
        agent_id = safe_id(doc.get("agent_id"))
        original_id = source_id
        if agent_id:
            source_id += ":agent:" + agent_id
        sid = self.store.session("claude", source_id)
        role = "child" if agent_id else "main"
        surface = doc.get("surface") if doc.get("surface") in ["terminal", "vscode", "desktop"] else "unknown"
        self.store.metadata(sid, doc.get("cwd"), role=role, surface=surface, last_hook_at=at)
        if agent_id:
            self.store.update(sid, {"parent_id": "claude:" + original_id})
        pid = number(doc.get("pid"))
        if pid and pid > 1:
            self.store.update(sid, {"pid": pid, "process_birth": short_text(doc.get("process_birth")), "tty": doc.get("tty") if re.fullmatch(r"/dev/ttys\d+", doc.get("tty") or "") else None})
        event = doc.get("event")
        # Async commands can finish out of order. A completed tool/prompt must
        # not be made waiting again by a delayed command from the same action.
        tool_id, prompt_id = safe_id(doc.get("tool_use_id")), safe_id(doc.get("prompt_id"))
        for kind, identity, closed in [("tool", tool_id, event in ["PostToolUse", "PostToolUseFailure"]),
                                       ("prompt", prompt_id, event in ["Stop", "StopFailure"])]:
            if identity and closed:
                self.store.db.execute("INSERT OR IGNORE INTO hook_closed VALUES(?,?,?,?)", (sid, kind, identity, at))
        superseded = False
        for kind, identity in [("tool", tool_id), ("prompt", prompt_id)]:
            if identity and event in ["PreToolUse", "PermissionRequest", "UserPromptSubmit", "Notification"]:
                superseded |= self.store.db.execute("SELECT 1 FROM hook_closed WHERE session_id=? AND kind=? AND source_id=?", (sid, kind, identity)).fetchone() is not None
        states = {
            "SessionStart": ("idle", "会话已接入"), "UserPromptSubmit": ("active", "正在处理你的需求"),
            "PreToolUse": ("active", "调用工具"), "PostToolUse": ("active", "工具返回 · 继续处理"),
            "PermissionRequest": ("waiting_approval", "等待你批准"), "Stop": ("idle", "本轮回复已结束"),
            "StopFailure": ("error", "本轮出现错误"), "PostToolUseFailure": ("error", "工具调用出现错误"),
            "SessionEnd": ("ended", "本次运行已结束"), "PreCompact": ("active", "压缩上下文"),
            "PostCompact": ("active", "上下文压缩完成"),
        }
        if event == "Notification":
            states[event] = {"permission_prompt": ("waiting_approval", "等待你批准"), "agent_needs_input": ("waiting_user", "等待你回复"), "idle_prompt": ("idle", "本轮回复已结束")}.get(doc.get("notification_type"))
        # Claude also routes interactive tools through PermissionRequest.
        # Their named question/plan dialog is not a command approval.
        if (event in ["PreToolUse", "PermissionRequest"] or
                (event == "Notification" and doc.get("notification_type") == "permission_prompt")) and doc.get("tool_name") in ["AskUserQuestion", "ExitPlanMode"]:
            states[event] = ("waiting_user", "等待你回复" if doc["tool_name"] == "AskUserQuestion" else "等待你确认方案")
        current = self.store.db.execute("SELECT state,state_at FROM sessions WHERE id=?", (sid,)).fetchone()
        pending = self.store.db.execute("SELECT * FROM claude_pending_interactions WHERE session_id=?", (sid,)).fetchone()
        if (event == "Notification" and doc.get("notification_type") == "permission_prompt" and
                not doc.get("tool_name") and not tool_id and pending and
                current["state"] == "waiting_user" and current["state_at"] >= pending["at"]):
            # IDE hosts repeat question dialogs as untyped permission notices.
            # Keep the precise type and original age, including after a restart.
            states[event] = None
        self.lifecycle_attention(sid, doc, at, superseded)
        if states.get(event) and not superseded:
            if at >= current["state_at"]:
                if states[event][0] == "waiting_user" and doc.get("tool_name") in ["AskUserQuestion", "ExitPlanMode"]:
                    self.store.db.execute("""INSERT INTO claude_pending_interactions VALUES(?,?,?,?)
                        ON CONFLICT(session_id) DO UPDATE SET tool_name=excluded.tool_name,
                        tool_use_id=CASE WHEN excluded.tool_use_id IS NULL AND tool_name=excluded.tool_name
                            THEN tool_use_id ELSE excluded.tool_use_id END,at=excluded.at""",
                        (sid, doc["tool_name"], tool_id, at))
                elif states[event][0] != "waiting_user":
                    self.store.db.execute("DELETE FROM claude_pending_interactions WHERE session_id=?", (sid,))
            self.store.state(sid, *states[event], at, "hook", "hk:" + event_id)
        if event in ["PreCompact", "PostCompact"]:
            self.store.invalidate_context(sid, at)
        if event == "SessionEnd":
            self.store.update(sid, {"liveness": "offline"})
        if event == "Statusline":
            record_claude(self.store, doc.get("rate_limits"), at)
            used, limit = number(doc.get("context_used")), number(doc.get("context_limit"))
            if doc.get("context_valid") is False:
                self.store.invalidate_context(sid, at)
            elif used is not None and limit:
                self.store.context(sid, used, limit, at)
        if event in ["TaskCreated", "TaskCompleted"]:
            task = safe_id(doc.get("task_id"))
            if task:
                if event == "TaskCreated":
                    self.store.db.execute("INSERT OR IGNORE INTO tasks(session_id,source_id,created_at,state) VALUES(?,?,?,'unknown')", (sid, task, at))
                else:
                    self.store.db.execute("INSERT INTO tasks(session_id,source_id,completed_at,state) VALUES(?,?,?,'completed') ON CONFLICT(session_id,source_id) DO UPDATE SET completed_at=excluded.completed_at,state='completed'", (sid, task, at))
        self.store.touch(sid, at)
        return sid

    def lifecycle(self, doc, cursor):
        if not isinstance(doc, dict) or doc.get("agentwatch") != 1:
            return None
        if doc.get("tool") == "cursor":
            return collect_cursor(self.store, doc)
        tool, event = doc.get("tool"), doc.get("event")
        if tool not in LIFECYCLE_EVENTS or event not in LIFECYCLE_EVENTS[tool]:
            return None
        identity, event_id, at = safe_id(doc.get("session_id")), safe_id(doc.get("id")), timestamp(doc.get("at"))
        if not identity or not event_id or at is None:
            return None
        event_id = "lifecycle:" + tool + ":" + event_id
        if self.store.db.execute("SELECT 1 FROM hook_seen WHERE id=?", (event_id,)).fetchone():
            return None
        self.store.db.execute("INSERT INTO hook_seen VALUES(?)", (event_id,))
        parent = self.store.session(tool, identity)
        child = safe_id(doc.get("agent_id")) if event in ["SubagentStart", "SubagentStop"] else None
        if event in ["SubagentStart", "SubagentStop"] and not child:
            return parent
        sid = self.store.session(tool, child) if child else parent
        row = self.store.db.execute("SELECT state,state_source,state_at,last_hook_at,liveness FROM sessions WHERE id=?", (sid,)).fetchone()
        values = {"last_hook_at": max(at, row["last_hook_at"] or 0)}
        if child:
            values.update(role="child", parent_id=parent)
        if doc.get("surface") in ["desktop", "vscode", "terminal"]:
            values["surface"] = doc["surface"]
        if tool == "workbuddy":
            values["surface"] = "desktop"
        if at >= row["state_at"] and isinstance(doc.get("pid"), int) and not isinstance(doc["pid"], bool) and doc["pid"] > 1 and isinstance(doc.get("process_birth"), str):
            values.update(pid=doc["pid"], process_birth=doc["process_birth"], tty=doc.get("tty"), liveness="online")
        self.store.metadata(sid, doc.get("cwd"), **values)
        turn = safe_id(doc.get("turn_id")) if not child else None
        tool_id = safe_id(doc.get("tool_use_id"))
        if turn and event in ["Stop", "Interrupt", "StopFailure"]:
            self.store.db.execute("INSERT OR IGNORE INTO hook_closed VALUES(?,?,?,?)", (sid, "turn", turn, at))
        if tool_id and event in ["PostToolUse", "PostToolUseFailure"]:
            self.store.db.execute("INSERT OR IGNORE INTO hook_closed VALUES(?,?,?,?)", (sid, "tool", tool_id, at))
        superseded = False
        if event in ["UserPromptSubmit", "PreToolUse", "PermissionRequest", "PostToolUse", "PostToolUseFailure", "PreCompact", "PostCompact"]:
            superseded = bool(turn and self.store.db.execute("SELECT 1 FROM hook_closed WHERE session_id=? AND kind='turn' AND source_id=?", (sid, turn)).fetchone())
        if event in ["PreToolUse", "PermissionRequest"] and tool_id:
            superseded |= self.store.db.execute("SELECT 1 FROM hook_closed WHERE session_id=? AND kind='tool' AND source_id=?", (sid, tool_id)).fetchone() is not None
        self.lifecycle_attention(sid, doc, at, superseded)
        states = {"SessionStart": ("unknown", "已接入 · 等待状态事件"),
                  "UserPromptSubmit": ("active", "正在处理你的需求"), "PreToolUse": ("active", "调用工具"),
                  "PostToolUse": ("active", "工具返回 · 继续处理"), "PermissionRequest": ("waiting_approval", "等待你批准"),
                  "Stop": ("idle", "本轮回复已结束"), "Interrupt": ("idle", "本轮已中断"),
                  "SessionEnd": ("ended", "本次运行已结束"), "StopFailure": ("error", "本轮出现错误"),
                  "PostToolUseFailure": ("active", "工具失败 · Agent 可继续处理"),
                  "PreCompact": ("active", "压缩上下文"), "PostCompact": ("active", "上下文压缩完成"),
                  "SubagentStart": ("active", "子 Agent 正在工作"), "SubagentStop": ("idle", "子 Agent 本轮已结束")}
        name = self.question_tool(doc.get("tool_name")) or (doc.get("tool_name") or "").rsplit(".", 1)[-1]
        if event in ["PreToolUse", "PermissionRequest"] and (name in ["AskUserQuestion", "ExitPlanMode", "request_user_input", "request_user_input_async"] or name.endswith("__request_user_input")):
            states[event] = ("active", "问题已发出 · 继续处理") if name == "request_user_input_async" else ("waiting_user", "等待你确认方案" if name == "ExitPlanMode" else "等待你回复")
        if event == "SessionStart" and doc.get("source") == "compact":
            states[event] = ("active", "上下文压缩完成 · 继续处理")
        elif event == "SessionStart" and row["state_source"] in ["hook", "event"] and row["state"] in ["active", "waiting_user", "waiting_approval"] and at - row["state_at"] < 120:
            states[event] = None  # A late worker-start event does not undo a submitted prompt.
        native_conversation = tool == "workbuddy" and self.store.db.execute(
            "SELECT 1 FROM files WHERE tool='workbuddy_state' AND session_id=? LIMIT 1", (sid,)).fetchone()
        if native_conversation and event == "SessionEnd":
            states[event] = None  # The desktop conversation can outlive its CLI worker.
        if event == "Notification":
            states[event] = {"permission_prompt": ("waiting_approval", "等待你批准"),
                             "agent_needs_input": ("waiting_user", "等待你回复"),
                             "elicitation_dialog": ("waiting_user", "等待你填写信息"),
                             "idle_prompt": ("idle", "本轮回复已结束")}.get(doc.get("notification_type"))
        if states.get(event) and not superseded:
            if event != "SessionEnd" and at >= row["state_at"] and row["liveness"] == "offline" and "liveness" not in values:
                # Desktop clients can reuse a conversation after its worker exits.
                # Fresh lifecycle evidence clears old offline data without inventing a PID.
                self.store.update(sid, {"liveness": "unknown"})
            self.store.state(sid, *states[event], at, "hook", event_id)
        if event == "SessionEnd" and states.get(event) and at >= row["state_at"]:
            self.store.update(sid, {"liveness": "offline"})
        self.store.touch(sid, at)
        return sid

    def workbuddy_state(self, doc):
        sid = self.store.session("workbuddy", doc["session_id"])
        at, signal, target = doc["at"], doc["signal"], doc["target"]
        state = None
        if signal == "PERMISSION_REQUESTED" and target == "pending":
            state = ("waiting_approval", "等待你批准 · WorkBuddy 客户端")
        elif signal in ["PROMPT_SENT", "PHASE_WORKING", "PHASE_PLANNING", "PERMISSION_RESOLVED"] and target in ["working", "planning"]:
            state = ("active", "正在处理 · WorkBuddy 客户端")
        elif signal in ["TURN_COMPLETED", "TURN_CANCELLED"] and target == "idle":
            state = ("idle", "本轮回复已结束")
        elif signal == "TURN_FAILED" and target in ["error", "failed"]:
            state = ("error", "本轮出现错误")
        if state:
            row = self.store.db.execute("SELECT state_at,liveness FROM sessions WHERE id=?", (sid,)).fetchone()
            self.store.metadata(sid, surface="desktop")
            if at >= row["state_at"] and row["liveness"] == "offline":
                self.store.update(sid, {"liveness": "unknown"})
            self.store.state(sid, *state, at, "event", fingerprint("workbuddy-state:" + sid + str(at) + signal))
        if signal == "TURN_COMPLETED" and target == "idle":
            self.store.complete(sid, at)
        return sid

    def workbuddy(self, doc, cursor, fallback_sid=None):
        """Read numeric request usage and metadata only, never generated titles/content."""
        if not isinstance(doc, dict):
            return cursor.get("sid")
        kind = doc.get("type")
        stages = {"message": "最近阶段：消息活动", "reasoning": "最近阶段：模型处理",
                  "function_call": "最近阶段：请求调用工具", "function_call_result": "最近阶段：工具返回"}
        if kind not in ["session-meta", *stages]:
            return cursor.get("sid")
        source_id = safe_id(doc.get("sessionId"))
        sid = cursor.get("sid") or fallback_sid
        if source_id:
            candidate = "workbuddy:" + source_id
            if sid and candidate != sid:
                return sid  # Do not mix unrelated records in the same transcript.
            sid = self.store.session("workbuddy", source_id)
        if not sid or not sid.startswith("workbuddy:"):
            return None
        cursor["sid"] = sid
        at = timestamp(doc.get("timestamp"))
        if at is None:
            self.store.update(sid, {"coverage": "missing_time"})
            return sid
        row = self.store.db.execute("SELECT started_at FROM sessions WHERE id=?", (sid,)).fetchone()
        self.store.metadata(sid, doc.get("cwd"), surface="desktop", role="unknown",
                            started_at=min(at, row["started_at"]) if row["started_at"] else at)
        if kind == "session-meta":
            return sid
        # A message/tool completion is not a reliable whole-task completion event.
        self.store.state(sid, "unknown", stages[kind], at)
        provider = doc.get("providerData")
        if not isinstance(provider, dict):
            return sid
        usage = provider.get("usage")
        if not isinstance(usage, dict):
            return sid
        input_count, output_count = number(usage.get("inputTokens")), number(usage.get("outputTokens"))
        if input_count is None or output_count is None:
            self.store.update(sid, {"coverage": "partial_usage"})
            return sid
        # conversationRequestId and traceId span multiple model requests.
        identity = safe_id(provider.get("messageId")) or safe_id(doc.get("id"))
        if not identity:
            self.store.update(sid, {"coverage": "missing_request_id"})
            return sid
        raw = provider.get("rawUsage")
        raw = raw if isinstance(raw, dict) else {}

        def details(value, key):
            values = value if isinstance(value, list) else [value]
            return [v.get(key) for v in values if isinstance(v, dict)]

        def maximum(values, limit):
            values = [n for v in values if (n := number(v)) is not None]
            return min(max(values), limit) if values else None

        cache = maximum([raw.get("prompt_cache_hit_tokens"), raw.get("cache_read_input_tokens"), raw.get("cached_tokens"),
                         *details(raw.get("prompt_tokens_details"), "cached_tokens"),
                         *details(usage.get("inputTokensDetails"), "cached_tokens")], input_count)
        write = maximum([raw.get("cache_creation_input_tokens"), raw.get("prompt_cache_write_tokens")], input_count)
        reasoning = maximum([raw.get("completion_thinking_tokens"),
                             *details(raw.get("completion_tokens_details"), "reasoning_tokens"),
                             *details(usage.get("outputTokensDetails"), "reasoning_tokens")], output_count)
        # WorkBuddy normalized input/output already include cache/reasoning.
        self.store.usage(sid, "wb:" + fingerprint(sid + ":" + identity), at, input_count, output_count,
                         cache, write, reasoning, "request", merge=True)
        return sid


class Collector:
    def __init__(self, store, codex_home, claude_home, data_dir, desktop_home=None, workbuddy_home=None, cursor_home=None):
        self.store, self.parser = store, RecordParser(store)
        self.codex_home, self.claude_home, self.data_dir = Path(codex_home), Path(claude_home), Path(data_dir)
        self.desktop_home = Path(desktop_home) if desktop_home else Path.home() / "Library/Application Support/Claude"
        self.workbuddy_home = Path(workbuddy_home) if workbuddy_home else Path.home() / ".workbuddy"
        self.cursor_home = Path(cursor_home) if cursor_home else Path.home() / ".cursor"
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        self.discovered = {}
        self.codex_archive_states = {}
        self.codex_index_sessions = set()
        self.codex_index_available = False
        self.last_discovery = self.last_probe = 0
        self.quota_bootstrapped = False
        self.status = {"running": False, "files": 0, "pending": 0, "read_bytes": 0, "last_scan": None, "errors": 0}
        self.allowed_roots = [self.codex_home.resolve(), self.claude_home.resolve(), self.desktop_home.resolve(), self.data_dir.resolve()]

    def permitted_workbuddy(self, path):
        try:
            root = self.workbuddy_home.resolve()
            projects = (self.workbuddy_home / "projects").resolve()
            return projects.is_relative_to(root) and Path(path).resolve().is_relative_to(projects)
        except (ValueError, OSError, RuntimeError):
            return False

    def permitted_workbuddy_state(self, path):
        try:
            root = self.workbuddy_home.resolve()
            logs = (self.workbuddy_home / "logs").resolve()
            relative = Path(path).resolve().relative_to(logs)
            return (logs.is_relative_to(root) and len(relative.parts) == 4 and
                    re.fullmatch(r"\d{4}-\d{2}-\d{2}", relative.parts[0]) is not None and
                    relative.parts[1:3] == ("sdk", "conversations") and Path(path).suffix == ".log" and
                    bool(safe_id(Path(path).stem)))
        except (ValueError, OSError, RuntimeError):
            return False

    def workbuddy_state_files(self):
        logs = self.workbuddy_home / "logs"
        if logs.is_dir():
            for path in logs.glob("*/sdk/conversations/*.log"):
                if path.is_file() and self.permitted_workbuddy_state(path):
                    yield str(path.resolve()), "workbuddy:" + path.stem

    def permitted(self, path):
        try:
            resolved = Path(path).resolve()
            return any(resolved.is_relative_to(root) for root in self.allowed_roots)
        except (ValueError, OSError):
            return False

    def start(self):
        self.thread = threading.Thread(target=self.loop, name="agentwatch-collector", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=5)

    def loop(self):
        while not self.stop_event.is_set():
            try:
                self.scan()
            except Exception:
                self.status["errors"] += 1  # Never log source contents or exception values.
            self.wake.wait(3)
            self.wake.clear()

    def discover(self):
        files = {}
        excluded = set()
        self.codex_archive_states = {}
        self.codex_index_available = False
        index = self.codex_home / "state_5.sqlite"
        if index.exists():
            try:
                with closing(sqlite3.connect(index.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
                    db.row_factory = sqlite3.Row
                    cols = {r[1] for r in db.execute("PRAGMA table_info(threads)")}
                    wanted = [c for c in ["id", "rollout_path", "cwd", "source", "originator", "cli_version", "git_branch", "created_at", "created_at_ms", "archived", "thread_source", "title"] if c in cols]
                    rows = db.execute("SELECT " + ",".join(wanted) + " FROM threads").fetchall()
                    names = read_codex_names(self.codex_home, db)
                    self.codex_index_sessions = {safe_id(row["id"]) for row in rows if safe_id(row["id"])}
                    self.codex_index_available = True
                    with self.store.lock, self.store.db:
                        for raw in rows:
                            r = dict(raw)
                            if "chrome" in (r.get("originator") or "").lower():
                                if r.get("rollout_path"):
                                    excluded.add(str(Path(r["rollout_path"]).resolve()))
                                continue
                            source_id = safe_id(r.get("id"))
                            if not source_id:
                                continue
                            sid = self.store.session("codex", source_id)
                            src = codex_source(r.get("source"))
                            role = "child" if isinstance(src, dict) and "subagent" in src else "main" if isinstance(src, str) and src in ["vscode", "cli", "exec", "appServer"] else "unknown"
                            surface = surface_for(r.get("originator") or "")
                            values = {"surface": surface} if surface != "unknown" else {}
                            previous = self.store.db.execute("SELECT session_title_source FROM sessions WHERE id=?", (sid,)).fetchone()
                            if source_id in names.titles:
                                values.update(session_title=names.titles[source_id], session_title_source="session_index")
                            elif not names.titles_failed and "title" in r and previous["session_title_source"] != "session_index":
                                # Legacy clients only; a known sidebar name must
                                # never fall back to an older first-message title.
                                values.update(session_title=r["title"], session_title_source="thread_index")
                            project_covered, project_title = names.project_title(source_id, r.get("cwd"))
                            if project_covered:
                                values["project_display_name"] = project_title
                            if "source" in r or "thread_source" in r:
                                values["internal"] = int(internal_codex(src, r.get("thread_source")))
                            if r.get("archived") in (0, 1):
                                self.codex_archive_states[sid] = int(r["archived"])
                                values.update(archived=int(r["archived"]), archive_source="index")
                            if isinstance(src, dict) and isinstance(src.get("subagent"), dict):
                                spawn = src["subagent"].get("thread_spawn", {})
                                if isinstance(spawn, dict) and safe_id(spawn.get("parent_thread_id")):
                                    values["parent_id"] = "codex:" + spawn["parent_thread_id"]
                            self.store.metadata(sid, r.get("cwd"), **values, role=role,
                                                version=r.get("cli_version") or "", branch=r.get("git_branch") or "",
                                                started_at=timestamp(r.get("created_at_ms") or r.get("created_at")))
                            path = Path(r.get("rollout_path") or "")
                            if self.permitted(path) and path.suffix == ".jsonl" and path.is_file():
                                files[str(path.resolve())] = ("codex", sid)
                        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                        if "thread_spawn_edges" in tables:
                            for parent, child in db.execute("SELECT parent_thread_id,child_thread_id FROM thread_spawn_edges"):
                                if safe_id(parent) and safe_id(child):
                                    self.store.update("codex:" + child, {"parent_id": "codex:" + parent, "role": "child"})
            except (sqlite3.Error, OSError):
                self.status["errors"] += 1
        for root, tool in [(self.codex_home / "sessions", "codex"), (self.codex_home / "archived_sessions", "codex"), (self.claude_home / "projects", "claude"), (self.desktop_home / "claude-code-sessions", "claude")]:
            if root.is_dir():
                for path in root.rglob("*.jsonl"):
                    canonical = str(path.resolve())
                    if self.permitted(path) and canonical not in files and canonical not in excluded:
                        files[canonical] = (tool, None)
        projects = self.workbuddy_home / "projects"
        if projects.is_dir() and self.permitted_workbuddy(projects):
            for path in projects.rglob("*.jsonl"):
                if path.is_file() and self.permitted_workbuddy(path):
                    files.setdefault(str(path.resolve()), ("workbuddy", None))
        for path, sid in self.workbuddy_state_files():
            files[path] = ("workbuddy_state", sid)
        events = self.data_dir / "claude-events.jsonl"
        # Track the private transports before their first event, so first-time
        # setup does not wait for the next source-discovery interval.
        files[str(events.resolve())] = ("hook", None)
        lifecycle = self.data_dir / "lifecycle-events.jsonl"
        files[str(lifecycle.resolve())] = ("lifecycle", None)
        # Desktop Code can identify its exact transcript through a configured hook.
        registry = self.data_dir / "transcripts.jsonl"
        if registry.exists() and registry.stat().st_size < 8_000_000:
            try:
                for line in registry.read_text().splitlines():
                    try:
                        d = json.loads(line)
                    except ValueError:
                        self.status["errors"] += 1
                        continue
                    if not isinstance(d, dict):
                        continue
                    path = Path(d.get("path", ""))
                    if safe_id(d.get("session_id")) and path.suffix == ".jsonl" and path.is_file() and self.permitted(path):
                        files[str(path.resolve())] = ("claude", "claude:" + d["session_id"])
            except (ValueError, OSError, UnicodeError):
                self.status["errors"] += 1
        self.discovered = files
        self.sync_codex_archives()
        self.sync_codex_memory_visibility()
        if not self.quota_bootstrapped:
            with self.store.lock, self.store.db:
                backfill_codex(self.store, files)
            self.quota_bootstrapped = True
        self.status["files"] = len(files)
        self.last_discovery = now()

    def sync_codex_memory_visibility(self):
        # Codex may emit hooks for non-persisted work in its managed memory
        # directory. A registered user thread or parsed rollout remains visible.
        memory_root = (self.codex_home / "memories").resolve()
        with self.store.lock, self.store.db:
            for row in self.store.db.execute("SELECT id,source_id,cwd,role,internal_memory FROM sessions WHERE tool='codex'").fetchall():
                transient = False
                if row["role"] == "unknown" and row["source_id"] not in self.codex_index_sessions:
                    try:
                        managed_memory = bool(row["cwd"]) and Path(row["cwd"]).resolve().is_relative_to(memory_root)
                    except (OSError, ValueError, RuntimeError):
                        managed_memory = False
                    if managed_memory:
                        collected = self.store.db.execute("SELECT 1 FROM files WHERE tool='codex' AND session_id=? AND offset>0 LIMIT 1", (row["id"],)).fetchone()
                        transient = not collected
                # Absence is meaningful only after a successful index read.
                # Keep prior visibility through unavailable indexes/restarts.
                if transient and not self.codex_index_available:
                    continue
                if bool(row["internal_memory"]) != bool(transient):
                    self.store.update(row["id"], {"internal_memory": int(transient)})

    def sync_codex_archives(self):
        # A moved transcript may already be at EOF. Visibility must be refreshed
        # independently of whether new events or usage were appended.
        archived_root = (self.codex_home / "archived_sessions").resolve()
        active_root = (self.codex_home / "sessions").resolve()
        with self.store.lock, self.store.db:
            saved = {r["path"]: r["session_id"] for r in self.store.db.execute("SELECT path,session_id FROM files WHERE tool='codex'")}
            paths = {}
            for path, (tool, fallback_sid) in self.discovered.items():
                sid = fallback_sid or saved.get(path)
                if tool != "codex" or not sid or sid in self.codex_archive_states:
                    continue
                parent = Path(path).parent
                archived = parent == archived_root or archived_root in parent.parents
                active = parent == active_root or active_root in parent.parents
                if archived or active:
                    paths[sid] = paths.get(sid, False) or archived
            for sid, archived in paths.items():
                row = self.store.db.execute("SELECT archive_source FROM sessions WHERE id=?", (sid,)).fetchone()
                # An unavailable index must not undo its last confirmed archive.
                if row and (archived or row["archive_source"] != "index"):
                    self.store.update(sid, {"archived": int(archived), "archive_source": "path"})

    def read_codex_attention(self, path, st, fallback_sid, budget):
        """Versioned metadata replay, separate from usage/state cursors and baselines."""
        inode = str(st.st_dev) + ":" + str(st.st_ino)
        with self.store.lock:
            saved = self.store.db.execute("SELECT * FROM attention_files WHERE path=?", (path,)).fetchone()
            offset = saved["offset"] if saved else 0
            sid = (saved["session_id"] if saved else None) or fallback_sid
            ignored = bool(saved["ignore_prefix"]) if saved else False
            partial = bool(saved and saved["complete"] == -1)
        if saved and (saved["version"] != 1 or saved["inode"] != inode or st.st_size < offset):
            offset, ignored, partial = 0, False, False
        if saved and offset == st.st_size:
            return 0, False
        start, rows = offset, []
        with Path(path).open("rb") as stream:
            stream.seek(offset)
            while stream.tell() - start < budget:
                pos = stream.tell()
                raw = stream.readline(2_000_001)
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    if len(raw) > 2_000_000:
                        while raw and not raw.endswith(b"\n"):
                            raw = stream.readline(2_000_001)
                        if raw.endswith(b"\n"):
                            offset = stream.tell()
                            partial = True
                            continue
                    stream.seek(pos)
                    break
                offset = stream.tell()
                try:
                    rows.append(json.loads(raw))
                except (ValueError, UnicodeError):
                    partial = True
                    continue
        with self.store.lock, self.store.db:
            if sid:
                self.store.update(sid, {"attention_covered": 0})
            for doc in rows:
                payload = doc.get("payload") if isinstance(doc, dict) else None
                if not isinstance(payload, dict):
                    partial = True
                    continue
                if doc.get("type") == "session_meta":
                    identity = safe_id(payload.get("id"))
                    ignored = "chrome" in str(payload.get("originator", "")).lower() or bool(fallback_sid and identity != fallback_sid.partition(":")[2])
                    if not ignored and identity:
                        sid = fallback_sid or self.store.session("codex", identity)
                        self.store.update(sid, {"attention_covered": 0})
                if sid and not ignored:
                    if (doc.get("type") == "response_item" or (doc.get("type") == "event_msg" and payload.get("type") == "task_complete")) and timestamp(doc.get("timestamp")) is None:
                        partial = True
                    self.parser.codex_attention(doc, sid)
            complete = offset == st.st_size
            coverage = -1 if partial else int(complete)
            self.store.db.execute("""INSERT INTO attention_files VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(path) DO UPDATE SET inode=excluded.inode,offset=excluded.offset,version=excluded.version,
                session_id=excluded.session_id,complete=excluded.complete,ignore_prefix=excluded.ignore_prefix""",
                (path, inode, offset, 1, sid, coverage, int(ignored)))
            if sid and complete:
                self.store.update(sid, {"attention_covered": int(not partial)})
        return offset - start, not complete

    def read_file(self, path, tool, fallback_sid, budget=512_000):
        if tool == "workbuddy" and not self.permitted_workbuddy(path):
            return 0, False
        if tool == "workbuddy_state" and not self.permitted_workbuddy_state(path):
            return 0, False
        p = Path(path)
        try:
            st = p.stat()
        except OSError:
            return 0, False
        inode = str(st.st_dev) + ":" + str(st.st_ino)
        saved = self.store.file(path)
        metadata_amount, metadata_more = (0, False)
        if tool == "codex":
            metadata_amount, metadata_more = self.read_codex_attention(path, st, fallback_sid or (saved["session_id"] if saved else None), budget)
        offset, data = (saved["offset"], json.loads(saved["data"])) if saved else (0, {})
        if saved and (saved["inode"] != inode or st.st_size < offset):
            offset = 0  # Keep the baseline and usage keys; replay must not increase totals.
        if st.st_size == offset:
            return metadata_amount, metadata_more
        if fallback_sid and not data.get("sid"):
            data["sid"] = fallback_sid
        if tool == "claude" and p.parent.name == "subagents":
            data["child_file"] = True
            data["agent_id"] = safe_id(p.stem.removeprefix("agent-"))
        rows = []
        start = offset
        with p.open("rb") as f:
            f.seek(offset)
            while f.tell() - start < budget:
                pos = f.tell()
                raw = f.readline(2_000_001)
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    if len(raw) > 2_000_000:
                        while raw and not raw.endswith(b"\n"):
                            raw = f.readline(2_000_001)
                        if raw.endswith(b"\n"):
                            offset = f.tell()
                            data["oversized_skipped"] = True
                            continue
                    f.seek(pos)
                    break  # A partially written record will be read next time.
                offset = f.tell()
                if tool == "workbuddy_state":
                    doc = workbuddy_transition(raw, p.stem)
                    if doc:
                        rows.append(doc)
                    continue
                try:
                    doc = json.loads(raw)
                except (ValueError, UnicodeError):
                    data["malformed_skipped"] = True
                    continue
                rows.append(doc)
            amount = offset - start
        with self.store.lock, self.store.db:
            sid = data.get("sid")
            for doc in rows:
                if tool == "codex":
                    sid = self.parser.codex(doc, data, fallback_sid) or sid
                elif tool == "claude":
                    sid = self.parser.claude(doc, data, fallback_sid) or sid
                elif tool == "workbuddy":
                    sid = self.parser.workbuddy(doc, data, fallback_sid) or sid
                elif tool == "lifecycle":
                    self.parser.lifecycle(doc, data)
                elif tool == "workbuddy_state":
                    sid = self.parser.workbuddy_state(doc) or sid
                else:
                    self.parser.hook(doc, data)
            if sid and (data.get("oversized_skipped") or data.get("malformed_skipped")):
                self.store.update(sid, {"coverage": "partial_records"})
            self.store.save_file(path, tool, sid, inode, offset, data, st.st_mtime, st.st_size)
        if tool in ["hook", "lifecycle"] and offset == st.st_size:
            # Our own event spool is a transport, not a historical activity log.
            # Hold the same lock as the bridge and only drain a fully read file.
            with p.open("r+b") as spool:
                fcntl.flock(spool, fcntl.LOCK_EX)
                current = os.fstat(spool.fileno())
                if str(current.st_dev) + ":" + str(current.st_ino) == inode and current.st_size == offset:
                    spool.truncate(0)
                    with self.store.lock, self.store.db:
                        self.store.save_file(path, tool, None, inode, 0, {}, current.st_mtime, 0)
        return amount + metadata_amount, offset < st.st_size or metadata_more

    def probe(self):
        inventory = process_inventory()
        agents = {pid: p for pid, p in inventory.items() if p["tool"]}
        bindings = {}
        if agents:
            try:
                run = subprocess.run(["lsof", "-a", "-p", ",".join(str(pid) for pid in agents), "-Fpfan"], capture_output=True, text=True, timeout=5)
                pid, access = None, None
                for line in run.stdout.splitlines():
                    if line.startswith("p"):
                        pid = int(line[1:])
                    elif line.startswith("f"):
                        access = None
                    elif line.startswith("a"):
                        access = line[1:]
                    elif line.startswith("n") and access in ["u", "w"] and line.endswith(".jsonl"):
                        key = str(Path(line[1:]).resolve())
                        saved = self.store.file(key)
                        if saved and saved["session_id"] and pid in agents:
                            bindings[saved["session_id"]] = agents[pid]
            except (OSError, ValueError, subprocess.TimeoutExpired):
                self.status["errors"] += 1
        with self.store.lock, self.store.db:
            for row in self.store.all_sessions():
                if row["state"] == "ended" and row["state_source"] == "hook":
                    continue
                process = bindings.get(row["id"])
                hook_process = agents.get(row["pid"])
                if not process and row["last_hook_at"] and hook_process and hook_process["birth"] == row["process_birth"]:
                    process = hook_process
                if process and process["tool"] == row["tool"]:
                    updates = {"pid": process["pid"], "process_birth": process["birth"], "tty": process["tty"], "liveness": "online"}
                    if process["surface"] != "unknown":
                        updates["surface"] = process["surface"]
                    self.store.update(row["id"], updates)
                elif row["pid"] and (row["pid"] not in inventory or inventory[row["pid"]]["birth"] != row["process_birth"]):
                    self.store.update(row["id"], {"liveness": "offline", "state": "ended", "stage": "运行进程已退出", "state_source": "process"})
                elif row["liveness"] == "online":
                    self.store.update(row["id"], {"liveness": "unknown"})
        self.last_probe = now()

    def scan(self, force=False):
        self.status["running"] = True
        try:
            if force or now() - self.last_discovery > 20:
                self.discover()
            # A new desktop task must not wait twenty seconds for its first
            # sandbox approval to be observed. Only enumerate SDK state logs here.
            for path, sid in self.workbuddy_state_files():
                self.discovered[path] = ("workbuddy_state", sid)
            self.status["files"] = len(self.discovered)
            pending, spent = 0, 0
            # Read authoritative hooks before transcripts: Codex may flush its
            # turn-complete record a few milliseconds after emitting Stop.
            # Otherwise that newer record can suppress every hook in the batch.
            def modified(item):
                try:
                    return (item[1][0] in ["hook", "lifecycle", "workbuddy_state"], Path(item[0]).stat().st_mtime)
                except OSError:
                    return (False, 0)
            files = sorted(self.discovered.items(), key=modified, reverse=True)
            for path, (tool, sid) in files:
                if self.stop_event.is_set():
                    break
                if spent >= 16_000_000:
                    pending += 1
                    continue
                try:
                    amount, more = self.read_file(path, tool, sid)
                    spent += amount
                    pending += int(more)
                except (OSError, ValueError, sqlite3.Error):
                    self.status["errors"] += 1
            self.status.update(pending=pending, read_bytes=self.status["read_bytes"] + spent, last_scan=now())
            self.sync_codex_archives()
            self.sync_codex_memory_visibility()
            if force or now() - self.last_probe > 8:
                self.probe()
            # Only activity labels, never source records, expire at the local day boundary.
            from .common import period_bounds, day_of
            _, start, _, _ = period_bounds("day", day_of(now()))
            with self.store.lock, self.store.db:
                self.store.db.execute("DELETE FROM activity WHERE at<?", (start.timestamp(),))
        finally:
            self.status["running"] = False
