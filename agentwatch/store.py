from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from datetime import datetime

from .common import TZ, day_of, fingerprint, now, period_bounds, project_identity, safe_id, short_text


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sessions (
              id TEXT PRIMARY KEY, tool TEXT NOT NULL, source_id TEXT NOT NULL,
              surface TEXT DEFAULT 'unknown', project_id TEXT DEFAULT 'unknown', project_name TEXT DEFAULT '未识别项目',
              cwd TEXT DEFAULT '', worktree TEXT DEFAULT '', branch TEXT DEFAULT '', version TEXT DEFAULT '',
              role TEXT DEFAULT 'unknown', parent_id TEXT, alias TEXT DEFAULT '',
              started_at REAL, last_event_at REAL, state TEXT DEFAULT 'unknown', stage TEXT DEFAULT '等待状态数据',
              state_at REAL DEFAULT 0, state_source TEXT DEFAULT 'record', liveness TEXT DEFAULT 'unknown',
              pid INTEGER, process_birth TEXT, tty TEXT, context_used INTEGER, context_limit INTEGER, context_at REAL,
              context_estimated INTEGER DEFAULT 0, context_invalid_at REAL DEFAULT 0,
              reported_input INTEGER, reported_output INTEGER, reported_cache_read INTEGER, reported_cache_write INTEGER,
              reported_total INTEGER, coverage TEXT DEFAULT 'observed', last_hook_at REAL,
              seen_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS usage (
              id TEXT PRIMARY KEY, session_id TEXT NOT NULL, at REAL NOT NULL, day TEXT NOT NULL,
              input INTEGER NOT NULL, output INTEGER NOT NULL, cache_read INTEGER, cache_write INTEGER,
              reasoning INTEGER, total INTEGER NOT NULL, kind TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS usage_at ON usage(at);
            CREATE INDEX IF NOT EXISTS usage_session ON usage(session_id);
            CREATE TABLE IF NOT EXISTS activity (
              id TEXT PRIMARY KEY, session_id TEXT NOT NULL, at REAL NOT NULL, kind TEXT NOT NULL, label TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS days (
              session_id TEXT NOT NULL, day TEXT NOT NULL, PRIMARY KEY(session_id,day)
            );
            CREATE TABLE IF NOT EXISTS turns (
              session_id TEXT NOT NULL, source_id TEXT NOT NULL, started_at REAL, ended_at REAL,
              state TEXT DEFAULT 'started', PRIMARY KEY(session_id,source_id)
            );
            CREATE TABLE IF NOT EXISTS tasks (
              session_id TEXT NOT NULL, source_id TEXT NOT NULL, created_at REAL, completed_at REAL,
              state TEXT DEFAULT 'unknown', PRIMARY KEY(session_id,source_id)
            );
            CREATE TABLE IF NOT EXISTS files (
              path TEXT PRIMARY KEY, tool TEXT NOT NULL, session_id TEXT, inode TEXT NOT NULL,
              offset INTEGER DEFAULT 0, data TEXT DEFAULT '{}', mtime REAL DEFAULT 0, size INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS hook_seen (id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS hook_closed (
              session_id TEXT, kind TEXT, source_id TEXT, at REAL, PRIMARY KEY(session_id,kind,source_id)
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS subscription_snapshots (
              tool TEXT NOT NULL, bucket TEXT NOT NULL, source TEXT NOT NULL, at REAL NOT NULL, data TEXT NOT NULL,
              PRIMARY KEY(tool,bucket)
            );
        """)
        os.chmod(self.path, 0o600)
        self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()

    def session(self, tool, source_id):
        source_id = safe_id(source_id)
        if not source_id:
            return None
        sid = tool + ":" + source_id
        self.db.execute("INSERT OR IGNORE INTO sessions(id,tool,source_id,seen_at) VALUES(?,?,?,?)", (sid, tool, source_id, now()))
        return sid

    def update(self, sid, values):
        allowed = {
            "surface", "project_id", "project_name", "cwd", "worktree", "branch", "version", "role", "parent_id",
            "alias", "started_at", "last_event_at", "state", "stage", "state_at", "state_source", "liveness", "pid",
            "process_birth", "tty", "context_used", "context_limit", "context_at", "context_estimated", "context_invalid_at",
            "reported_input", "reported_output", "reported_cache_read", "reported_cache_write", "reported_total",
            "coverage", "last_hook_at", "seen_at",
        }
        values = {k: v for k, v in values.items() if k in allowed}
        if values:
            self.db.execute("UPDATE sessions SET " + ",".join(k + "=?" for k in values) + " WHERE id=?", (*values.values(), sid))

    def metadata(self, sid, cwd=None, **values):
        row = self.db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if cwd and row and not row["cwd"]:
            project_id, name, root, worktree = project_identity(cwd)
            values.update(project_id=project_id, project_name=name, cwd=root, worktree=worktree)
        for key in ["branch", "version"]:
            if key in values:
                values[key] = short_text(values[key])
        self.update(sid, values)

    def touch(self, sid, at):
        self.db.execute("UPDATE sessions SET last_event_at=MAX(COALESCE(last_event_at,0),?), seen_at=? WHERE id=?", (at, now(), sid))
        self.db.execute("INSERT OR IGNORE INTO days VALUES(?,?)", (sid, day_of(at)))

    def state(self, sid, state, stage, at, source="record", event_id=None):
        row = self.db.execute("SELECT state,stage,state_at,state_source FROM sessions WHERE id=?", (sid,)).fetchone()
        if not row or at < row["state_at"]:
            return
        if source == "record" and row["state_source"] == "hook":
            return
        changed = row["state"] != state or row["stage"] != stage
        self.update(sid, {"state": state, "stage": stage, "state_at": at, "state_source": source})
        self.touch(sid, at)
        if changed:
            key = event_id or fingerprint(sid + state + stage + str(at))
            self.db.execute("INSERT OR IGNORE INTO activity VALUES(?,?,?,?,?)", (key, sid, at, state, stage))

    def context(self, sid, used, limit, at, estimated=False):
        row = self.db.execute("SELECT context_at,context_invalid_at FROM sessions WHERE id=?", (sid,)).fetchone()
        if row and at >= max(row["context_at"] or 0, row["context_invalid_at"] or 0):
            self.update(sid, {"context_used": used, "context_limit": limit, "context_at": at, "context_estimated": int(estimated)})

    def invalidate_context(self, sid, at):
        row = self.db.execute("SELECT context_invalid_at FROM sessions WHERE id=?", (sid,)).fetchone()
        if row and at >= row["context_invalid_at"]:
            self.update(sid, {"context_used": None, "context_at": None, "context_invalid_at": at})

    def usage(self, sid, key, at, input_count, output_count, cache_read=None, cache_write=None, reasoning=None, kind="request", merge=False):
        values = (key, sid, at, day_of(at), input_count, output_count, cache_read, cache_write, reasoning, input_count + output_count, kind)
        if merge:
            self.db.execute("""INSERT INTO usage VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                input=MAX(usage.input,excluded.input), output=MAX(usage.output,excluded.output),
                cache_read=NULLIF(MAX(COALESCE(usage.cache_read,-1),COALESCE(excluded.cache_read,-1)),-1),
                cache_write=NULLIF(MAX(COALESCE(usage.cache_write,-1),COALESCE(excluded.cache_write,-1)),-1),
                reasoning=NULLIF(MAX(COALESCE(usage.reasoning,-1),COALESCE(excluded.reasoning,-1)),-1),
                total=MAX(usage.input,excluded.input)+MAX(usage.output,excluded.output)""", values)
        else:
            self.db.execute("INSERT OR IGNORE INTO usage VALUES(?,?,?,?,?,?,?,?,?,?,?)", values)
        self.touch(sid, at)

    def all_sessions(self):
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM sessions")]

    def file(self, path):
        with self.lock:
            row = self.db.execute("SELECT * FROM files WHERE path=?", (str(path),)).fetchone()
            return dict(row) if row else None

    def save_file(self, path, tool, sid, inode, offset, data, mtime, size):
        self.db.execute("""INSERT INTO files VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
            session_id=excluded.session_id,inode=excluded.inode,offset=excluded.offset,data=excluded.data,
            mtime=excluded.mtime,size=excluded.size""", (str(path), tool, sid, inode, offset, json.dumps(data), mtime, size))

    def select_filter(self, args, prefix="s."):
        clauses, values = [], []
        for name, col in [("tool", "tool"), ("project", "project_id"), ("surface", "surface")]:
            v = args.get(name, "all")
            if v and v != "all":
                clauses.append(prefix + col + "=?")
                values.append(v)
        role = args.get("role", "main")
        if role in ["main", "child", "unknown"]:
            clauses.append(prefix + "role=?")
            values.append(role)
        return " AND ".join(clauses) or "1=1", values

    def stats(self, args, current=None):
        current = current or now()
        period, start, end, buckets = period_bounds(args.get("period", "day"), args.get("date", day_of(current)))
        where, values = self.select_filter(args)
        with self.lock:
            rows = self.db.execute("SELECT u.* FROM usage u JOIN sessions s ON s.id=u.session_id WHERE " + where + " AND u.at>=? AND u.at<? ORDER BY u.at", (*values, start.timestamp(), end.timestamp())).fetchall()
            days = self.db.execute("SELECT DISTINCT d.session_id FROM days d JOIN sessions s ON s.id=d.session_id WHERE " + where + " AND d.day>=? AND d.day<?", (*values, start.date().isoformat(), end.date().isoformat())).fetchall()
            turns = self.db.execute("SELECT COUNT(*) FROM turns t JOIN sessions s ON s.id=t.session_id WHERE " + where + " AND t.started_at>=? AND t.started_at<?", (*values, start.timestamp(), end.timestamp())).fetchone()[0]
            tasks = self.db.execute("SELECT COUNT(*) FROM tasks t JOIN sessions s ON s.id=t.session_id WHERE " + where + " AND t.created_at>=? AND t.created_at<?", (*values, start.timestamp(), end.timestamp())).fetchone()[0]
            task_coverage = self.db.execute("SELECT COUNT(*) FROM tasks t JOIN sessions s ON s.id=t.session_id WHERE " + where, values).fetchone()[0]
            first = self.db.execute("SELECT MIN(at) FROM usage").fetchone()[0]
            partial = self.db.execute("SELECT COUNT(*) FROM sessions s WHERE " + where + " AND coverage!='observed'", values).fetchone()[0]
            totals = {k: sum(r[k] for r in rows if r[k] is not None) if any(r[k] is not None for r in rows) else None for k in ["input", "output", "cache_read", "cache_write", "reasoning", "total"]}
            by_tool = {}
            session_tools = {r["id"]: r["tool"] for r in self.db.execute("SELECT id,tool FROM sessions")}
            for r in rows:
                tool = session_tools[r["session_id"]]
                by_tool[tool] = by_tool.get(tool, 0) + r["total"]
                for bucket in buckets:
                    if bucket["start"] <= r["at"] < bucket["end"]:
                        bucket["tokens"] += r["total"]
                        break
        return {"period": period, "start": start.date().isoformat(), "end": end.date().isoformat(), "timezone": "Asia/Shanghai",
                "totals": totals, "sessions": len(days), "turns": turns, "tasks": tasks if task_coverage else None,
                "task_scope": "已覆盖的 Claude 原生任务" if task_coverage else "未取得可靠任务 ID", "by_tool": by_tool,
                "buckets": buckets, "first_usage_day": day_of(first) if first else None, "partial_sessions": partial,
                "has_usage": bool(rows)}

    def snapshot(self, args, scan_status, current=None):
        current = current or now()
        period, start, end, _ = period_bounds("day", day_of(current))
        where, values = self.select_filter(args)
        with self.lock:
            rows = self.db.execute("SELECT s.* FROM sessions s WHERE " + where + " AND (s.last_event_at>=? OR s.liveness='online') ORDER BY s.last_event_at DESC", (*values, start.timestamp())).fetchall()
            cards = []
            priority = {"waiting_approval": 0, "waiting_user": 0, "error": 1, "active": 2, "unknown": 3, "idle": 4, "ended": 5}
            for r in rows:
                card = dict(r)
                age = current - (card["state_at"] or 0)
                stale = card["state"] in ["active", "waiting_user", "waiting_approval"] and age > 120
                state = "unknown" if stale and card["state"] == "active" else card["state"]
                if state == "unknown" and stale and card["state"] == "active":
                    card["stage"] = "最近阶段：" + card["stage"]
                card.update(state=state, stale=stale, state_age=age, context_stale=card["context_at"] is not None and current - card["context_at"] > 300)
                observed = self.db.execute("SELECT SUM(input) AS i,SUM(output) AS o,SUM(cache_read) AS cr,SUM(cache_write) AS cw,SUM(total) AS total FROM usage WHERE session_id=?", (card["id"],)).fetchone()
                today = self.db.execute("SELECT SUM(total) FROM usage WHERE session_id=? AND day=?", (card["id"], day_of(current))).fetchone()[0]
                card["usage"] = {"input": observed["i"], "output": observed["o"], "cache_read": observed["cr"], "cache_write": observed["cw"], "observed_total": observed["total"], "today": today,
                                 "cumulative": card["reported_total"] if card["reported_total"] is not None else observed["total"]}
                limit, used = card["context_limit"], card["context_used"]
                card["context_percent"] = used / limit * 100 if used is not None and limit and limit > 0 else None
                card["jump"] = self.jump_info(card)
                card["worktree_name"] = Path(card["worktree"]).name if card["worktree"] else None
                for key in ["cwd", "worktree", "pid", "process_birth", "tty", "reported_input", "reported_output", "reported_cache_read", "reported_cache_write", "reported_total"]:
                    card.pop(key, None)
                cards.append(card)
            cards.sort(key=lambda c: (priority.get(c["state"], 3), -(c["last_event_at"] or 0)))
            activity = [dict(r) for r in self.db.execute("SELECT a.id,a.at,a.kind,a.label,s.tool,s.source_id,s.project_name,s.id AS session_id FROM activity a JOIN sessions s ON s.id=a.session_id WHERE " + where + " AND a.at>=? AND a.at<? ORDER BY a.at DESC LIMIT 24", (*values, start.timestamp(), end.timestamp()))]
            projects = [dict(r) for r in self.db.execute("SELECT project_id AS id,project_name AS name,COUNT(*) AS sessions FROM sessions GROUP BY project_id,project_name ORDER BY project_name")]
            unknown_roles = self.db.execute("SELECT COUNT(*) FROM sessions WHERE role='unknown' AND last_event_at>=?", (start.timestamp(),)).fetchone()[0]
        counts = {"observed": len(cards), "online": sum(c["liveness"] == "online" for c in cards),
                  "active": sum(c["state"] == "active" and not c["stale"] and c["liveness"] == "online" for c in cards),
                  "waiting": sum(c["state"] in ["waiting_user", "waiting_approval"] for c in cards),
                  "error": sum(c["state"] == "error" for c in cards),
                  "unknown": sum(c["state"] == "unknown" for c in cards), "unknown_roles": unknown_roles}
        return {"now": current, "date": day_of(current), "timezone": "Asia/Shanghai", "counts": counts, "sessions": cards[:160],
                "truncated": len(cards) > 160, "activity": activity, "projects": projects, "scan": scan_status,
                "today": self.stats(dict(args, period="day", date=day_of(current)), current)}

    @staticmethod
    def jump_info(card):
        if card["tool"] == "workbuddy":
            return {"kind": "workbuddy", "label": "打开 WorkBuddy", "precision": "应用", "available": True}
        if card["tool"] == "codex":
            if card["surface"] == "vscode":
                return {"kind": "codex_vscode", "label": "在 VS Code 打开", "precision": "会话路由", "available": True}
            return {"kind": "codex", "label": "在 Codex 打开", "precision": "指定会话", "available": True}
        if card["surface"] == "terminal" and card["tty"] and card["liveness"] == "online":
            return {"kind": "terminal", "label": "跳回终端", "precision": "原标签页", "available": True}
        if card["surface"] == "vscode" and card["cwd"]:
            return {"kind": "project", "label": "打开 VS Code 项目", "precision": "项目", "available": True}
        if card["surface"] == "desktop":
            return {"kind": "claude", "label": "打开 Claude", "precision": "应用", "available": True}
        return {"kind": "none", "label": "入口待识别", "precision": None, "available": False}
