from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
from pathlib import Path
from datetime import datetime, timedelta

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
              role TEXT DEFAULT 'unknown', parent_id TEXT, alias TEXT DEFAULT '', session_title TEXT,
              project_display_name TEXT, session_title_source TEXT NOT NULL DEFAULT '',
              started_at REAL, last_event_at REAL, state TEXT DEFAULT 'unknown', stage TEXT DEFAULT '等待状态数据',
              state_at REAL DEFAULT 0, state_source TEXT DEFAULT 'record', liveness TEXT DEFAULT 'unknown',
              pid INTEGER, process_birth TEXT, tty TEXT, context_used INTEGER, context_limit INTEGER, context_at REAL,
              context_estimated INTEGER DEFAULT 0, context_invalid_at REAL DEFAULT 0,
              reported_input INTEGER, reported_output INTEGER, reported_cache_read INTEGER, reported_cache_write INTEGER,
              reported_total INTEGER, coverage TEXT DEFAULT 'observed', last_hook_at REAL,
              archived INTEGER NOT NULL DEFAULT 0, archive_source TEXT NOT NULL DEFAULT '',
              internal INTEGER NOT NULL DEFAULT 0,
              internal_memory INTEGER NOT NULL DEFAULT 0,
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
            CREATE TABLE IF NOT EXISTS cursor_generations (
              session_id TEXT PRIMARY KEY, generation_id TEXT NOT NULL, at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS claude_pending_interactions (
              session_id TEXT PRIMARY KEY, tool_name TEXT NOT NULL, tool_use_id TEXT, at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS interaction_requests (
              session_id TEXT NOT NULL, call_id TEXT NOT NULL, item_index INTEGER NOT NULL,
              tool_name TEXT NOT NULL, kind TEXT NOT NULL, asynchronous INTEGER NOT NULL DEFAULT 0,
              opened_at REAL NOT NULL, updated_at REAL NOT NULL, resolved_at REAL, source TEXT NOT NULL,
              PRIMARY KEY(session_id,call_id,item_index)
            );
            CREATE TABLE IF NOT EXISTS interaction_aliases (
              session_id TEXT NOT NULL, alias_id TEXT NOT NULL, call_id TEXT NOT NULL,
              PRIMARY KEY(session_id,alias_id)
            );
            CREATE TABLE IF NOT EXISTS attention_dismissals (
              session_id TEXT NOT NULL, call_id TEXT NOT NULL, item_index INTEGER NOT NULL,
              opened_at REAL NOT NULL, dismissed_at REAL NOT NULL,
              PRIMARY KEY(session_id,call_id,item_index)
            );
            CREATE TABLE IF NOT EXISTS attention_files (
              path TEXT PRIMARY KEY, inode TEXT NOT NULL, offset INTEGER NOT NULL DEFAULT 0,
              version INTEGER NOT NULL DEFAULT 1, session_id TEXT, complete INTEGER NOT NULL DEFAULT 0,
              ignore_prefix INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS completion_receipts (
              session_id TEXT PRIMARY KEY, read_completed_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS subscription_snapshots (
              tool TEXT NOT NULL, bucket TEXT NOT NULL, source TEXT NOT NULL, at REAL NOT NULL, data TEXT NOT NULL,
              PRIMARY KEY(tool,bucket)
            );
        """)
        # Existing installations keep their sessions, aliases, usage and cursors.
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(sessions)")}
        for name, declaration in [("archived", "INTEGER NOT NULL DEFAULT 0"),
                                  ("archive_source", "TEXT NOT NULL DEFAULT ''"),
                                  ("internal", "INTEGER NOT NULL DEFAULT 0"),
                                  ("internal_memory", "INTEGER NOT NULL DEFAULT 0"),
                                  ("last_completed_at", "REAL"),
                                  ("attention_covered", "INTEGER NOT NULL DEFAULT 0"),
                                  ("session_title", "TEXT"),
                                  ("project_display_name", "TEXT"),
                                  ("session_title_source", "TEXT NOT NULL DEFAULT ''")]:
            if name not in columns:
                self.db.execute("ALTER TABLE sessions ADD COLUMN " + name + " " + declaration)
        self.db.executescript("""
            CREATE INDEX IF NOT EXISTS sessions_parent ON sessions(parent_id);
            CREATE INDEX IF NOT EXISTS sessions_role_activity ON sessions(role,last_event_at);
            CREATE INDEX IF NOT EXISTS sessions_role_liveness ON sessions(role,liveness);
            CREATE INDEX IF NOT EXISTS files_session ON files(tool,session_id);
            CREATE INDEX IF NOT EXISTS interactions_open ON interaction_requests(session_id,resolved_at);
            CREATE INDEX IF NOT EXISTS attention_files_session ON attention_files(session_id,complete);
            CREATE INDEX IF NOT EXISTS sessions_completed ON sessions(role,last_completed_at);
            DROP VIEW IF EXISTS visible_sessions;
            CREATE VIEW visible_sessions AS
              WITH RECURSIVE hidden(id) AS (
                SELECT id FROM sessions WHERE archived=1 OR internal=1 OR internal_memory=1
                UNION
                SELECT s.id FROM sessions s JOIN hidden h ON s.parent_id=h.id WHERE s.role='child'
              )
              SELECT s.* FROM sessions s WHERE s.id NOT IN (SELECT id FROM hidden);
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
        self.db.execute("INSERT OR IGNORE INTO sessions(id,tool,source_id,seen_at,attention_covered) VALUES(?,?,?,?,1)", (sid, tool, source_id, now()))
        return sid

    def update(self, sid, values):
        allowed = {
            "surface", "project_id", "project_name", "cwd", "worktree", "branch", "version", "role", "parent_id",
            "alias", "started_at", "last_event_at", "state", "stage", "state_at", "state_source", "liveness", "pid",
            "process_birth", "tty", "context_used", "context_limit", "context_at", "context_estimated", "context_invalid_at",
            "reported_input", "reported_output", "reported_cache_read", "reported_cache_write", "reported_total",
            "coverage", "last_hook_at", "seen_at", "archived", "archive_source", "internal", "internal_memory",
            "last_completed_at", "attention_covered", "session_title", "project_display_name", "session_title_source",
        }
        values = {k: v for k, v in values.items() if k in allowed}
        for key in ("session_title", "project_display_name"):
            if key in values:
                # Existing client names only; never infer names from content.
                values[key] = short_text(values[key], limit=160) or None
        if "session_title_source" in values:
            values["session_title_source"] = values["session_title_source"] if values["session_title_source"] in ("session_index", "thread_index") else ""
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
        if source == "record" and row["state_source"] in ["hook", "event"]:
            return
        changed = row["state"] != state or row["stage"] != stage
        self.update(sid, {"state": state, "stage": stage, "state_at": at, "state_source": source})
        self.touch(sid, at)
        if changed:
            key = event_id or fingerprint(sid + state + stage + str(at))
            self.db.execute("INSERT OR IGNORE INTO activity VALUES(?,?,?,?,?)", (key, sid, at, state, stage))

    def complete(self, sid, at):
        """Explicit completed-turn evidence only; idle and process exit never call this."""
        self.db.execute("UPDATE sessions SET last_completed_at=MAX(COALESCE(last_completed_at,0),?) WHERE id=?", (at, sid))

    def acknowledge_completion(self, sid, completed_at, current=None):
        current = now() if current is None else current
        if (isinstance(completed_at, bool) or not isinstance(completed_at, (int, float)) or
                not math.isfinite(completed_at) or not 0 < completed_at <= current):
            return False
        row = self.db.execute("SELECT last_completed_at FROM visible_sessions WHERE id=? AND role='main'", (sid,)).fetchone()
        if not row or row["last_completed_at"] is None or completed_at > row["last_completed_at"]:
            return False
        # A click on an older version never acknowledges a later completion.
        self.db.execute("""INSERT INTO completion_receipts VALUES(?,?) ON CONFLICT(session_id)
            DO UPDATE SET read_completed_at=MAX(completion_receipts.read_completed_at,excluded.read_completed_at)""", (sid, completed_at))
        return True

    def completion_inbox(self, card, attention, current):
        completed = card.get("last_completed_at")
        eligible = (card.get("role") == "main" and completed is not None and
                    current - 86400 <= completed <= current and card["state"] in ["idle", "ended"] and
                    not attention["pending"] and not attention["unknown"])
        receipt = self.db.execute("SELECT read_completed_at FROM completion_receipts WHERE session_id=?", (card["id"],)).fetchone()
        unread = eligible and (not receipt or receipt["read_completed_at"] < completed)
        return {"unread_completed": bool(unread), "completed_at": completed}

    def open_interaction(self, sid, call_id, item_index, kind, at,
                         tool_name="request_user_input_async", asynchronous=True, source="event"):
        """Store correlation metadata only: no question, answer or tool argument."""
        if (not safe_id(call_id) or isinstance(item_index, bool) or not isinstance(item_index, int)
                or not 0 <= item_index < 64 or kind not in ["waiting_user", "waiting_approval", "unknown"]):
            return False
        self.db.execute("""INSERT INTO interaction_requests VALUES(?,?,?,?,?,?,?,?,NULL,?)
            ON CONFLICT(session_id,call_id,item_index) DO UPDATE SET
              kind=CASE WHEN interaction_requests.kind='unknown' THEN excluded.kind ELSE interaction_requests.kind END,
              tool_name=CASE WHEN excluded.tool_name!='' THEN excluded.tool_name ELSE interaction_requests.tool_name END,
              asynchronous=MAX(interaction_requests.asynchronous,excluded.asynchronous),
              opened_at=MIN(interaction_requests.opened_at,excluded.opened_at),
              updated_at=MAX(interaction_requests.updated_at,excluded.updated_at)""",
            (sid, call_id, item_index, tool_name, kind, int(asynchronous), at, at, source))
        self.touch(sid, at)
        return True

    def interaction_alias(self, sid, alias_id, call_id):
        if safe_id(alias_id) and safe_id(call_id):
            # A conflicting source identity must never be reassigned to another call.
            self.db.execute("INSERT OR IGNORE INTO interaction_aliases VALUES(?,?,?)", (sid, alias_id, call_id))

    def resolve_interaction(self, sid, call_id, at, item_index=None, tool_name=None):
        if not safe_id(call_id):
            return 0
        alias = self.db.execute("SELECT call_id FROM interaction_aliases WHERE session_id=? AND alias_id=?", (sid, call_id)).fetchone()
        if alias:
            if alias[0] != call_id and self.db.execute("SELECT 1 FROM interaction_requests WHERE session_id=? AND call_id=? LIMIT 1", (sid, call_id)).fetchone():
                return 0  # Conflicting correlation identities cannot safely close either call.
            call_id = alias[0]
        clauses, values = ["session_id=?", "call_id=?", "opened_at<=?"], [sid, call_id, at]
        if item_index is not None:
            if isinstance(item_index, bool) or not isinstance(item_index, int) or not 0 <= item_index < 64:
                return 0
            clauses.append("item_index=?"); values.append(item_index)
        if tool_name is not None:
            clauses.append("tool_name=?"); values.append(tool_name)
        result = self.db.execute("UPDATE interaction_requests SET resolved_at=COALESCE(resolved_at,?),updated_at=MAX(updated_at,?) WHERE " + " AND ".join(clauses), (at, at, *values))
        return result.rowcount

    def attention_summaries(self, root_ids, current):
        """One metadata pass plus explicit visible child edges; no truncated-card sums."""
        related = {root_id: [root_id] for root_id in root_ids}
        for offset in range(0, len(root_ids), 160):
            for row in self.descendant_rows(root_ids[offset:offset + 160], "s.id"):
                related[row["root_id"]].append(row["id"])
        requested = {sid for ids in related.values() for sid in ids}
        summaries = self.own_attention(requested, current)
        requests = {}
        for row in self.open_attention_requests(requested, current):
            requests.setdefault(row["session_id"], []).append(row)
        result = {}
        for root_id, ids in related.items():
            items = [summaries[sid] for sid in ids if sid in summaries]
            result[root_id] = self.merge_attention(items)
            pending = [row for sid in ids for row in requests.get(sid, [])]
            result[root_id]["dismiss_version"] = self.attention_version(root_id, pending)
        return result

    def open_attention_requests(self, session_ids, current):
        """Current human reminders; dismissal never rewrites actual resolution."""
        rows, session_ids = [], list(session_ids)
        for offset in range(0, len(session_ids), 160):
            batch = session_ids[offset:offset + 160]
            if not batch:
                continue
            rows.extend(self.db.execute("""SELECT i.session_id,i.call_id,i.item_index,
                i.opened_at,i.kind,i.tool_name FROM interaction_requests i
                LEFT JOIN attention_dismissals d ON d.session_id=i.session_id AND d.call_id=i.call_id
                    AND d.item_index=i.item_index AND d.opened_at=i.opened_at
                WHERE i.session_id IN (""" + ",".join("?" for _ in batch) + """)
                    AND i.resolved_at IS NULL AND i.opened_at<=?
                    AND i.kind IN ('waiting_user','waiting_approval') AND d.session_id IS NULL""",
                (*batch, current)).fetchall())
        return rows

    @staticmethod
    def attention_version(root_id, requests):
        if not requests:
            return None
        identities = sorted((r["session_id"], r["call_id"], r["item_index"], r["opened_at"],
                             r["kind"], r["tool_name"]) for r in requests)
        return fingerprint(json.dumps([root_id, identities], separators=(",", ":")))

    def dismiss_attention(self, sid, version, current=None):
        """Explicit user reconciliation of this exact batch, never an Agent answer."""
        current = now() if current is None else current
        if (not safe_id(sid) or not isinstance(version, str) or len(version) != 64
                or any(c not in "0123456789abcdef" for c in version)):
            return False
        with self.lock, self.db:
            if not self.db.execute("SELECT 1 FROM visible_sessions WHERE id=? AND role='main'", (sid,)).fetchone():
                return False
            ids = [sid] + [r["id"] for r in self.descendant_rows([sid], "s.id")]
            requests = self.open_attention_requests(ids, current)
            if self.attention_version(sid, requests) != version:
                return False
            self.db.executemany("""INSERT INTO attention_dismissals VALUES(?,?,?,?,?)
                ON CONFLICT(session_id,call_id,item_index) DO UPDATE SET
                    opened_at=excluded.opened_at,dismissed_at=excluded.dismissed_at""",
                [(r["session_id"], r["call_id"], r["item_index"], r["opened_at"], current) for r in requests])
        return True

    @staticmethod
    def merge_attention(items):
        values = {key: sum(item[key] for item in items) for key in ["waiting_user", "waiting_approval", "error", "unknown"]}
        values["pending"] = values["waiting_user"] + values["waiting_approval"] > 0
        times = [item["oldest_at"] for item in items if item["oldest_at"] is not None]
        values["oldest_at"] = min(times) if times else None
        return values

    def own_attention(self, session_ids, current):
        result = {}
        session_ids = list(session_ids)
        for offset in range(0, len(session_ids), 160):
            batch = session_ids[offset:offset + 160]
            if not batch:
                continue
            placeholders = ",".join("?" for _ in batch)
            rows = self.db.execute("SELECT id,state,state_at,coverage,attention_covered FROM sessions WHERE id IN (" + placeholders + ")", batch).fetchall()
            for row in rows:
                result[row["id"]] = {"pending": False, "waiting_user": 0, "waiting_approval": 0,
                    "error": int(row["state"] == "error"), "unknown": int(not row["attention_covered"] or row["coverage"] in ["partial_records", "missing_time"]), "oldest_at": None}
            for row in self.db.execute("""SELECT i.session_id,i.kind,COUNT(*) AS n,MIN(i.opened_at) AS oldest
                FROM interaction_requests i LEFT JOIN attention_dismissals d
                  ON d.session_id=i.session_id AND d.call_id=i.call_id AND d.item_index=i.item_index
                    AND d.opened_at=i.opened_at
                WHERE i.session_id IN (""" + placeholders + """) AND i.resolved_at IS NULL AND i.opened_at<=?
                  AND (d.session_id IS NULL OR i.kind='unknown') GROUP BY i.session_id,i.kind""", (*batch, current)):
                item = result[row["session_id"]]
                item[row["kind"]] += row["n"]
                item["oldest_at"] = min(item["oldest_at"] or row["oldest"], row["oldest"])
            for row in rows:
                item = result[row["id"]]
                reconciled = self.db.execute("""SELECT 1 FROM attention_dismissals d
                    JOIN interaction_requests i ON i.session_id=d.session_id AND i.call_id=d.call_id
                      AND i.item_index=d.item_index AND i.opened_at=d.opened_at
                    WHERE d.session_id=? AND d.dismissed_at>=? LIMIT 1""", (row["id"], row["state_at"] or 0)).fetchone()
                # Legacy waiting is not sufficient evidence that a person is needed.
                if not reconciled and row["state"] in ["waiting_user", "waiting_approval", "unknown"] and not any(item[key] for key in ["waiting_user", "waiting_approval", "unknown"]):
                    item["unknown"] = 1
                if self.db.execute("SELECT 1 FROM attention_files WHERE session_id=? AND complete!=1 LIMIT 1", (row["id"],)).fetchone():
                    item["unknown"] = max(1, item["unknown"])
                item["pending"] = item["waiting_user"] + item["waiting_approval"] > 0
        return result

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

    def heatmap(self, args, current=None):
        """Account for every role and retained history; an absent day is unknown."""
        current = now() if current is None else current
        year = datetime.fromtimestamp(current, TZ).year
        start = datetime(year, 1, 1, tzinfo=TZ)
        end = start.replace(year=year + 1)
        tool = args.get("tool") or "all"
        where, values = self.select_filter({"tool": tool, "role": "all"})
        fields = ["input", "output", "cache_read", "cache_write", "reasoning", "total"]
        with self.lock:
            totals = dict(self.db.execute("SELECT " + ",".join("SUM(u." + key + ") AS " + key for key in fields) +
                ",COUNT(*) AS records FROM usage u JOIN sessions s ON s.id=u.session_id WHERE " + where +
                " AND u.at>=? AND u.at<?", (*values, start.timestamp(), end.timestamp())).fetchone())
            daily = {r["day"]: r["tokens"] for r in self.db.execute(
                "SELECT u.day,SUM(u.total) AS tokens FROM usage u JOIN sessions s ON s.id=u.session_id WHERE " +
                where + " AND u.at>=? AND u.at<? GROUP BY u.day", (*values, start.timestamp(), end.timestamp()))}
            first = self.db.execute("SELECT MIN(at) FROM usage").fetchone()[0]
            partial = self.db.execute("SELECT COUNT(*) FROM sessions s WHERE " + where + " AND coverage!='observed'", values).fetchone()[0]
            tools = [row[0] for row in self.db.execute("SELECT DISTINCT tool FROM sessions ORDER BY tool")]
        has_usage = totals.pop("records") > 0
        days = []
        cursor = start
        while cursor < end:
            day = cursor.date().isoformat()
            days.append({"date": day, "tokens": daily.get(day), "has_usage": day in daily})
            cursor += timedelta(days=1)
        return {"year": year, "date": day_of(current), "timezone": "Asia/Shanghai", "tool": tool,
                "start": start.date().isoformat(), "end": end.date().isoformat(), "totals": totals,
                "has_usage": has_usage, "first_usage_day": day_of(first) if first is not None else None,
                "partial_sessions": partial, "tools": tools, "days": days}

    @staticmethod
    def card_state(row, current):
        card = dict(row)
        age = current - (card["state_at"] or 0)
        stale = card["state"] in ["active", "waiting_user", "waiting_approval"] and age > 120
        if stale and card["state"] == "active":
            card["state"] = "unknown"
            if "stage" in card:
                card["stage"] = "最近阶段：" + card["stage"]
        card.update(stale=stale, state_age=age)
        return card

    @staticmethod
    def state_counts(cards):
        return {"total": len(cards),
                "active": sum(c["state"] == "active" and not c["stale"] and
                              (c["liveness"] == "online" or (c["liveness"] != "offline" and c["state_source"] in ["hook", "event"])) for c in cards),
                "waiting": sum(c["state"] in ["waiting_user", "waiting_approval"] for c in cards),
                "error": sum(c["state"] == "error" for c in cards),
                "unknown": sum(c["state"] == "unknown" for c in cards)}

    def session_card(self, row, current):
        """The same safe metadata and conservative state for roots and descendants."""
        card = self.card_state(row, current)
        card["session_title"] = short_text(card.get("session_title"), limit=160) or None
        card["project_display_name"] = short_text(card.get("project_display_name"), limit=160) or None
        card["attention"] = self.own_attention([card["id"]], current)[card["id"]]
        card["inbox"] = self.completion_inbox(card, card["attention"], current)
        card["context_stale"] = card["context_at"] is not None and current - card["context_at"] > 300
        observed = self.db.execute("SELECT SUM(input) AS i,SUM(output) AS o,SUM(cache_read) AS cr,SUM(cache_write) AS cw,SUM(total) AS total FROM usage WHERE session_id=?", (card["id"],)).fetchone()
        today = self.db.execute("SELECT SUM(total) FROM usage WHERE session_id=? AND day=?", (card["id"], day_of(current))).fetchone()[0]
        card["usage"] = {"input": observed["i"], "output": observed["o"], "cache_read": observed["cr"], "cache_write": observed["cw"], "observed_total": observed["total"], "today": today,
                         "cumulative": card["reported_total"] if card["reported_total"] is not None else observed["total"]}
        limit, used = card["context_limit"], card["context_used"]
        card["context_percent"] = used / limit * 100 if used is not None and limit and limit > 0 else None
        card["jump"] = self.jump_info(card)
        card["worktree_name"] = Path(card["worktree"]).name if card["worktree"] else None
        directory = card["worktree"] or card["cwd"]
        if directory:
            directory_path = Path(directory)
            try:
                directory = str(Path("~") / directory_path.relative_to(Path.home()))
            except ValueError:
                pass
        card["working_directory"] = directory or None
        if card["tool"] == "codex" and card["last_hook_at"] and not self.db.execute(
                "SELECT 1 FROM files WHERE tool='codex' AND session_id=? AND offset>0 LIMIT 1", (card["id"],)).fetchone():
            card["coverage"] = "status_only"
        for key in ["cwd", "worktree", "pid", "process_birth", "tty", "reported_input", "reported_output", "reported_cache_read", "reported_cache_write", "reported_total", "archived", "archive_source", "internal", "internal_memory", "attention_covered", "session_title_source"]:
            card.pop(key, None)
        return card

    def descendant_rows(self, root_ids, columns="s.*"):
        if not root_ids:
            return []
        # Only walk indexed, explicit child edges below visible roots. Excluding a
        # hidden node before recursion also excludes its descendants, like the view.
        placeholders = ",".join("?" for _ in root_ids)
        return self.db.execute("""WITH RECURSIVE tree(root_id,id) AS (
            SELECT s.parent_id,s.id FROM sessions s WHERE s.parent_id IN (""" + placeholders + """)
              AND s.role='child' AND s.archived=0 AND s.internal=0 AND s.internal_memory=0
            UNION
            SELECT t.root_id,s.id FROM sessions s JOIN tree t ON s.parent_id=t.id
              WHERE s.role='child' AND s.archived=0 AND s.internal=0 AND s.internal_memory=0
            ) SELECT t.root_id,""" + columns + " FROM tree t JOIN sessions s ON s.id=t.id", root_ids).fetchall()

    def children_summaries(self, root_ids, current):
        grouped = {root_id: [] for root_id in root_ids}
        for offset in range(0, len(root_ids), 160):
            for row in self.descendant_rows(root_ids[offset:offset + 160], "s.state,s.state_at,s.liveness,s.state_source"):
                grouped[row["root_id"]].append(self.card_state(row, current))
        return {root_id: self.state_counts(cards) for root_id, cards in grouped.items()}

    def children(self, parent_id, current=None):
        current = now() if current is None else current
        with self.lock:
            parent = self.db.execute("SELECT 1 FROM visible_sessions WHERE id=? AND role='main'", (parent_id,)).fetchone()
            if not parent:
                return None
            descendants = self.descendant_rows([parent_id])
            by_parent = {}
            for row in descendants:
                by_parent.setdefault(row["parent_id"], []).append(row)
            for siblings in by_parent.values():
                siblings.sort(key=lambda row: (-(row["last_event_at"] or 0), row["id"]))
            # Iterative traversal keeps arbitrarily deep trees safe, with parents
            # before their children and a visited set for malformed cycles.
            stack = [(row, 1) for row in reversed(by_parent.get(parent_id, []))]
            visited, cards = {parent_id}, []
            while stack:
                row, depth = stack.pop()
                if row["id"] in visited:
                    continue
                visited.add(row["id"])
                card = self.session_card(row, current)
                card.pop("root_id", None)
                card["depth"] = depth
                cards.append(card)
                stack.extend((child, depth + 1) for child in reversed(by_parent.get(row["id"], [])))
        return {"now": current, "parent_id": parent_id, "children": cards,
                "counts": self.state_counts(cards), "truncated": False}

    def snapshot(self, args, scan_status, current=None):
        current = current or now()
        period, start, end, _ = period_bounds("day", day_of(current))
        where, values = self.select_filter(args)
        with self.lock:
            if args.get("role", "main") == "main":
                # A root remains the only entry point when a child works across
                # midnight. Seed from indexed activity, then follow explicit known
                # parent edges upward; hidden and unknown paths stop traversal.
                rows = self.db.execute("""WITH RECURSIVE recent_children(id,parent_id,role) AS (
                    SELECT id,parent_id,role FROM sessions WHERE role='child' AND (last_event_at>=?
                      OR EXISTS(SELECT 1 FROM interaction_requests i WHERE i.session_id=sessions.id
                        AND i.resolved_at IS NULL AND i.kind IN ('waiting_user','waiting_approval')
                        AND NOT EXISTS(SELECT 1 FROM attention_dismissals d WHERE d.session_id=i.session_id
                          AND d.call_id=i.call_id AND d.item_index=i.item_index AND d.opened_at=i.opened_at)))
                      AND archived=0 AND internal=0 AND internal_memory=0
                    UNION
                    SELECT id,parent_id,role FROM sessions WHERE role='child' AND liveness='online'
                      AND archived=0 AND internal=0 AND internal_memory=0
                    ), ancestors(id,parent_id,role) AS (
                    SELECT * FROM recent_children
                    UNION
                    SELECT p.id,p.parent_id,p.role FROM sessions p JOIN ancestors c ON p.id=c.parent_id
                      WHERE c.role='child' AND p.role IN ('child','main')
                        AND p.archived=0 AND p.internal=0 AND p.internal_memory=0
                    ) SELECT s.* FROM visible_sessions s WHERE """ + where + """
                      AND (s.last_event_at>=? OR s.liveness='online'
                        OR EXISTS(SELECT 1 FROM interaction_requests i WHERE i.session_id=s.id
                          AND i.resolved_at IS NULL AND i.kind IN ('waiting_user','waiting_approval')
                          AND NOT EXISTS(SELECT 1 FROM attention_dismissals d WHERE d.session_id=i.session_id
                            AND d.call_id=i.call_id AND d.item_index=i.item_index AND d.opened_at=i.opened_at))
                        OR (s.last_completed_at>=? AND s.last_completed_at<=? AND NOT EXISTS(
                            SELECT 1 FROM completion_receipts r WHERE r.session_id=s.id AND r.read_completed_at>=s.last_completed_at))
                        OR s.id IN (SELECT id FROM ancestors WHERE role='main'))
                    ORDER BY s.last_event_at DESC""", (start.timestamp(), *values, start.timestamp(), current - 86400, current)).fetchall()
            else:
                rows = self.db.execute("SELECT s.* FROM visible_sessions s WHERE " + where + " AND (s.last_event_at>=? OR s.liveness='online') ORDER BY s.last_event_at DESC", (*values, start.timestamp())).fetchall()
            priority = {"waiting_approval": 0, "waiting_user": 0, "error": 1, "active": 2, "unknown": 3, "idle": 4, "ended": 5}
            cards = [self.session_card(row, current) for row in rows]
            summaries = self.children_summaries([card["id"] for card in cards if card["role"] == "main"], current)
            attentions = self.attention_summaries([card["id"] for card in cards if card["role"] == "main"], current)
            children_counts = {key: sum(summary[key] for summary in summaries.values())
                               for key in ["total", "active", "waiting", "error", "unknown"]}
            for card in cards:
                if card["id"] in summaries:
                    card["children_summary"] = summaries[card["id"]]
                    card["attention"] = attentions[card["id"]]
                    card["inbox"] = self.completion_inbox(card, card["attention"], current)
            def card_priority(card):
                rank = priority.get(card["state"], 3)
                if card["attention"]["pending"]:
                    rank = 0
                summary = card.get("children_summary", {})
                for key, child_rank in [("waiting", 0), ("error", 1), ("active", 2), ("unknown", 3)]:
                    if summary.get(key):
                        rank = min(rank, child_rank)
                return rank, -(card["last_event_at"] or 0)
            cards.sort(key=card_priority)
            activity = [dict(r) for r in self.db.execute("SELECT a.id,a.at,a.kind,a.label,s.tool,s.source_id,s.project_name,s.id AS session_id FROM activity a JOIN visible_sessions s ON s.id=a.session_id WHERE " + where + " AND a.at>=? AND a.at<? ORDER BY a.at DESC LIMIT 24", (*values, start.timestamp(), end.timestamp()))]
            projects = [dict(r) for r in self.db.execute("SELECT s.project_id AS id,s.project_name AS name,COUNT(*) AS sessions FROM sessions s WHERE s.internal_memory=0 OR EXISTS (SELECT 1 FROM usage u WHERE u.session_id=s.id) GROUP BY s.project_id,s.project_name ORDER BY s.project_name")]
            unknown_roles = self.db.execute("SELECT COUNT(*) FROM visible_sessions WHERE role='unknown' AND last_event_at>=?", (start.timestamp(),)).fetchone()[0]
            recent_where, recent_values = self.select_filter(dict(args, role="main"))
            recent_rows = self.db.execute("SELECT s.* FROM visible_sessions s WHERE " + recent_where + " AND s.last_completed_at>=? AND s.last_completed_at<=? ORDER BY s.last_completed_at DESC,s.id", (*recent_values, current - 86400, current)).fetchall()
            recent_completed = []
            for row in recent_rows:
                card = self.session_card(row, current)
                card["attention"] = self.attention_summaries([card["id"]], current)[card["id"]]
                card["inbox"] = self.completion_inbox(card, card["attention"], current)
                if not card["inbox"]["unread_completed"]:
                    continue
                recent_completed.append(card)
                if len(recent_completed) == 5:
                    break
        counts = self.state_counts(cards)
        counts["observed"] = counts.pop("total")
        counts.update(online=sum(c["liveness"] == "online" for c in cards), unknown_roles=unknown_roles)
        attention_counts = {key: sum(bool(item[key]) for item in attentions.values()) for key in ["pending", "waiting_user", "waiting_approval", "error", "unknown"]}
        roots = [card for card in cards if card["role"] == "main"]
        inbox_counts = {"pending": sum(card["attention"]["pending"] or card["inbox"]["unread_completed"] for card in roots),
                        "action_required": attention_counts["pending"],
                        "unread_completed": sum(card["inbox"]["unread_completed"] for card in roots),
                        "unknown": attention_counts["unknown"]}
        return {"now": current, "date": day_of(current), "timezone": "Asia/Shanghai", "counts": counts,
                "children_counts": children_counts, "sessions": cards[:160],
                "attention_counts": attention_counts, "inbox_counts": inbox_counts, "recent_completed": recent_completed,
                "truncated": len(cards) > 160, "activity": activity, "projects": projects, "scan": scan_status,
                "today": self.stats(dict(args, period="day", date=day_of(current)), current)}

    def codex_rollout(self, card):
        """A lifecycle ID alone is not evidence of a restorable conversation."""
        with self.lock:
            rows = self.db.execute("SELECT path FROM files WHERE tool='codex' AND session_id=? AND offset>0", (card["id"],)).fetchall()
            for row in rows:
                path = Path(row["path"])
                try:
                    if path.is_absolute() and path.suffix == ".jsonl" and path.is_file() and path.stat().st_size > 0:
                        return str(path)
                except (OSError, ValueError):
                    continue
        return None

    def jump_info(self, card):
        if card["tool"] == "cursor":
            return {"kind": "cursor", "label": "打开 Cursor", "precision": "应用", "available": True}
        if card["tool"] == "workbuddy":
            return {"kind": "workbuddy", "label": "打开 WorkBuddy", "precision": "应用", "available": True}
        if card["tool"] == "codex":
            if any(card.get(key) for key in ["archived", "internal", "internal_memory"]) or not self.codex_rollout(card):
                return {"kind": "none", "label": "会话记录不可用", "precision": None, "available": False,
                        "reason": "未发现可恢复的本地会话记录；状态上报本身不能用于恢复对话。"}
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
