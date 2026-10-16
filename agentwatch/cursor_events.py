"""Cursor observation events; never interpret tool arguments or client approvals."""
from __future__ import annotations

from .common import now, safe_id, short_text, timestamp
from .cursor_bridge import EVENTS


def collect_cursor(store, doc):
    if not isinstance(doc, dict) or doc.get("agentwatch") != 1 or doc.get("tool") != "cursor":
        return None
    event = doc.get("event")
    source = safe_id(doc.get("session_id"))
    identity = safe_id(doc.get("id"))
    at = timestamp(doc.get("at"))
    generation = safe_id(doc.get("turn_id"))
    if (event not in EVENTS or not source or not identity or at is None or at <= 0 or
            at > now() + 60 or (event not in ["sessionStart", "sessionEnd"] and not generation)):
        return None
    # The first release accepts composer conversations only, not unknown agents.
    if any(doc.get(key) is not None for key in ["parent_conversation_id", "subagent_id", "agent_id", "parent_tool_call_id"]):
        return None
    event_id = "cursor:" + identity
    if store.db.execute("SELECT 1 FROM hook_seen WHERE id=?", (event_id,)).fetchone():
        return None
    store.db.execute("INSERT INTO hook_seen VALUES(?)", (event_id,))
    sid = store.session("cursor", source)
    row = store.db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    metadata = {"role": "main", "surface": "desktop", "coverage": "status_only",
                "attention_covered": 0, "last_hook_at": max(at, row["last_hook_at"] or 0)}
    version = short_text(doc.get("version"), limit=80)
    if version:
        metadata["version"] = version
    cwd = doc.get("cwd")
    cwd = cwd if isinstance(cwd, str) and cwd.startswith("/") and len(cwd) < 4096 else None
    store.metadata(sid, cwd, **metadata)
    store.touch(sid, at)
    head = store.db.execute("SELECT * FROM cursor_generations WHERE session_id=?", (sid,)).fetchone()
    closed = bool(generation and store.db.execute(
        "SELECT 1 FROM hook_closed WHERE session_id=? AND kind='turn' AND source_id=?", (sid, generation)).fetchone())

    # A new submission provides the generation boundary, not proof that a backend
    # accepted the request: another Cursor hook may still reject submission.
    if event == "beforeSubmitPrompt":
        if closed or at < row["state_at"] or (head and at < head["at"]):
            return sid
        if head and head["generation_id"] == generation:
            return sid  # A repeated submission hook cannot undo observed work.
        if head and head["generation_id"] != generation:
            store.db.execute("INSERT OR IGNORE INTO hook_closed VALUES(?,?,?,?)",
                             (sid, "turn", head["generation_id"], at))
        store.db.execute("INSERT INTO cursor_generations VALUES(?,?,?) ON CONFLICT(session_id) "
                         "DO UPDATE SET generation_id=excluded.generation_id,at=excluded.at", (sid, generation, at))
        state = ("unknown", "请求已提交 · 执行待确认")
    elif event == "sessionStart":
        # Delayed session creation cannot undo a later generation's state.
        if row["state_at"] or head:
            return sid
        state = ("unknown", "已接入 Cursor · 等待执行事件")
    else:
        if head and (generation != head["generation_id"] or at < head["at"]):
            return sid
        if closed and event != "sessionEnd":
            return sid
        if generation and not head:
            # Enabling hooks halfway through a conversation is supported.
            store.db.execute("INSERT INTO cursor_generations VALUES(?,?,?)", (sid, generation, at))
        if at < row["state_at"]:
            return sid
        if event in ["postToolUse", "postToolUseFailure", "afterAgentThought", "preCompact"]:
            labels = {"postToolUse": "工具返回 · 继续处理", "postToolUseFailure": "工具失败 · Agent 可继续处理",
                      "afterAgentThought": "思考事件 · 正在处理", "preCompact": "压缩上下文"}
            state = ("active", labels[event])
        elif event == "stop":
            reason = doc.get("stop_reason")
            reason = reason if isinstance(reason, str) else None
            state = {"completed": ("idle", "Cursor 本轮已结束"),
                     "aborted": ("idle", "Cursor 本轮已中断"),
                     "error": ("error", "Cursor 本轮出现错误")}.get(reason)
            if state is None:
                state = ("unknown", "Cursor 本轮状态待确认")
            if generation and reason in ["completed", "aborted", "error"]:
                store.db.execute("INSERT OR IGNORE INTO hook_closed VALUES(?,?,?,?)", (sid, "turn", generation, at))
            if reason == "completed":
                store.complete(sid, at)
        elif event == "sessionEnd":
            # Session closure is not proof that a response completed successfully.
            if generation:
                store.db.execute("INSERT OR IGNORE INTO hook_closed VALUES(?,?,?,?)", (sid, "turn", generation, at))
            state = ("error", "Cursor 会话出现错误") if doc.get("stop_reason") == "error" else ("ended", "Cursor 会话已关闭")
        else:
            return sid
    store.state(sid, *state, at, "hook", event_id)
    # No shared app process is bound to this particular conversation.
    store.update(sid, {"liveness": "offline" if event == "sessionEnd" else "unknown"})
    return sid
