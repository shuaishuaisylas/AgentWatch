from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentwatch.collector import Collector
from agentwatch.common import timestamp
from agentwatch.store import Store


AT = timestamp("2026-10-15T10:00:00+08:00")


class VisibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.codex = self.root / "codex"
        self.codex.mkdir()
        self.store = Store(self.root / "data/watch.sqlite")
        self.collector = Collector(self.store, self.codex, self.root / "claude", self.root / "data",
                                   self.root / "desktop", workbuddy_home=self.root / "workbuddy")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def transcript(self, identity, source="cli", directory="sessions", parent=None):
        path = self.codex / directory / (identity + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = {"id": identity, "source": source, "originator": "codex_cli_rs"}
        if parent:
            meta["parent_thread_id"] = parent
        usage = {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15}
        docs = [{"type": "session_meta", "timestamp": AT, "payload": meta},
                {"type": "event_msg", "timestamp": AT, "payload": {"type": "token_count", "info": {
                    "total_token_usage": usage, "last_token_usage": usage}}},
                {"type": "event_msg", "timestamp": AT, "payload": {"type": "task_started", "turn_id": "turn1"}}]
        path.write_text("".join(json.dumps(d) + "\n" for d in docs))
        return path

    def index(self, entries, archive_column=True):
        with sqlite3.connect(self.codex / "state_5.sqlite") as db:
            db.execute("CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, rollout_path TEXT, source TEXT, thread_source TEXT" +
                       (", archived INTEGER" if archive_column else "") + ")")
            for identity, path, source, thread_source, archived in entries:
                values = (identity, str(path), source if isinstance(source, str) else json.dumps(source), thread_source)
                if archive_column:
                    values += (archived,)
                db.execute("INSERT OR REPLACE INTO threads VALUES(" + ",".join("?" for _ in values) + ")", values)

    def scan(self):
        with patch.object(self.collector, "probe"), patch("agentwatch.collector.now", return_value=AT + 1):
            self.collector.scan(force=True)

    def snapshot(self, **args):
        return self.store.snapshot(dict(role="all", **args), {}, AT + 1)

    def test_archive_updates_at_eof_hides_online_and_preserves_history(self):
        path = self.transcript("one")
        self.index([("one", path, "cli", "user", 0)])
        self.scan()
        self.store.update("codex:one", {"liveness": "online", "role": "unknown", "alias": "Keep this alias"})
        before = self.store.stats({"role": "all", "date": "2026-10-15"}, AT)
        self.assertEqual(self.snapshot()["counts"]["online"], 1)
        self.index([("one", path, "cli", "user", 1)])
        self.scan()
        snap = self.snapshot()
        self.assertEqual(snap["sessions"], [])
        self.assertEqual(snap["activity"], [])
        self.assertTrue(all(value == 0 for value in snap["counts"].values()))
        self.assertEqual(self.store.stats({"role": "all", "date": "2026-10-15"}, AT), before)
        self.assertEqual(snap["today"], before)
        # Historical project filtering remains available even after archiving.
        self.assertEqual(len(snap["projects"]), 1)
        self.index([("one", path, "cli", "user", 0)])
        self.scan()
        card = self.snapshot()["sessions"][0]
        self.assertEqual(card["alias"], "Keep this alias")
        self.assertEqual(card["usage"]["observed_total"], 15)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM usage").fetchone()[0], 1)

    def test_archived_parent_hides_descendants_even_without_parent_transcript(self):
        missing = self.codex / "sessions/missing.jsonl"
        child_source = {"subagent": {"thread_spawn": {"parent_thread_id": "parent"}}}
        child = self.transcript("child", child_source)
        grandchild = self.transcript("grandchild", {"subagent": {}}, parent="child")
        self.index([("parent", missing, "cli", "user", 1),
                    ("child", child, child_source, "subagent", 0)])
        self.scan()
        self.assertEqual(self.snapshot()["sessions"], [])
        self.assertEqual(self.snapshot()["activity"], [])
        self.index([("parent", missing, "cli", "user", 0)])
        self.scan()
        self.assertEqual({c["id"] for c in self.snapshot()["sessions"]}, {"codex:child", "codex:grandchild"})

    def test_spawn_edges_hide_previously_collected_child_after_parent_archive(self):
        parent = self.transcript("parent")
        child = self.transcript("child", {"subagent": {}})
        self.index([("parent", parent, "cli", "user", 0), ("child", child, "cli", "subagent", 0)])
        with sqlite3.connect(self.codex / "state_5.sqlite") as db:
            db.execute("CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT)")
            db.execute("INSERT INTO thread_spawn_edges VALUES ('parent','child')")
        self.scan()
        self.assertEqual(len(self.snapshot()["sessions"]), 2)
        self.index([("parent", parent, "cli", "user", 1)])
        self.scan()
        self.assertEqual(self.snapshot()["sessions"], [])

    def test_internal_guardian_is_hidden_without_hiding_regular_or_unknown_children(self):
        guardian = self.transcript("review", {"subagent": {"other": "guardian"}})
        normal_source = {"subagent": {"thread_spawn": {"parent_thread_id": "parent"}}}
        self.transcript("worker", normal_source)
        self.transcript("future-child", {"subagent": {"other": "future_kind"}})
        self.scan()  # No index: the transcript metadata is sufficient.
        self.assertEqual({c["id"] for c in self.snapshot()["sessions"]}, {"codex:worker", "codex:future-child"})
        self.assertTrue(all(a["session_id"] != "codex:review" for a in self.snapshot()["activity"]))
        # A pre-existing row at EOF is also corrected by index metadata.
        self.store.update("codex:review", {"internal": 0, "liveness": "online"})
        self.index([("review", guardian, "unknown", "guardian_review", 0)])
        self.scan()
        self.assertNotIn("codex:review", {c["id"] for c in self.snapshot()["sessions"]})

    def test_only_metadata_guardian_never_becomes_a_visible_online_session(self):
        path = self.transcript("empty-review", {"subagent": {"other": "guardian"}})
        path.write_text(path.read_text().splitlines()[0] + "\n")
        self.scan()
        self.store.update("codex:empty-review", {"liveness": "online"})
        self.assertEqual(self.snapshot()["sessions"], [])

    def test_directory_fallback_tracks_moves_and_deduplicates_usage(self):
        path = self.transcript("moved")
        self.scan()
        self.assertEqual(len(self.snapshot()["sessions"]), 1)
        archived = self.codex / "archived_sessions/moved.jsonl"
        archived.parent.mkdir()
        path.rename(archived)
        self.scan()
        self.assertEqual(self.snapshot()["sessions"], [])
        archived.rename(path)
        self.scan()
        self.assertEqual(len(self.snapshot()["sessions"]), 1)
        self.assertEqual(self.store.db.execute("SELECT SUM(total) FROM usage").fetchone()[0], 15)

    def test_legacy_index_without_archive_column_uses_directory_fallback(self):
        path = self.transcript("legacy", directory="archived_sessions")
        self.index([("legacy", path, "cli", "user", None)], archive_column=False)
        self.scan()
        self.assertEqual(self.snapshot()["sessions"], [])
        self.assertEqual(self.store.db.execute("SELECT archived FROM sessions").fetchone()[0], 1)

    def test_explicit_index_state_wins_over_a_stale_directory_copy(self):
        path = self.transcript("restored", directory="archived_sessions")
        self.index([("restored", path, "cli", "user", 0)])
        self.scan()
        self.assertEqual(len(self.snapshot()["sessions"]), 1)
        self.index([("restored", path, "cli", "user", 1)])
        self.scan()
        self.assertEqual(self.snapshot()["sessions"], [])

    def test_missing_index_does_not_clear_its_last_confirmed_archive(self):
        path = self.transcript("offline-index")
        self.index([("offline-index", path, "cli", "user", 1)])
        self.scan()
        (self.codex / "state_5.sqlite").unlink()
        self.scan()
        self.assertEqual(self.snapshot()["sessions"], [])

    def test_ancestor_cycles_terminate_and_never_hide_unrelated_sessions(self):
        for identity, parent in [("a", "b"), ("b", "a"), ("unrelated", None)]:
            sid = self.store.session("codex", identity)
            self.store.update(sid, {"parent_id": "codex:" + parent if parent else None, "role": "child" if parent else "main", "liveness": "online"})
        self.store.update("codex:a", {"archived": 1})
        self.assertEqual([c["id"] for c in self.snapshot()["sessions"]], ["codex:unrelated"])

    def test_independent_user_fork_is_not_hidden_with_its_original_thread(self):
        parent = self.store.session("codex", "original")
        fork = self.store.session("codex", "user-fork")
        self.store.update(parent, {"archived": 1, "role": "main", "liveness": "online"})
        self.store.update(fork, {"parent_id": parent, "role": "main", "liveness": "online"})
        self.assertEqual([c["id"] for c in self.snapshot()["sessions"]], [fork])

    def hook_only(self, identity, cwd):
        sid = self.store.session("codex", identity)
        self.store.metadata(sid, str(cwd), surface="desktop")
        self.store.state(sid, "active", "Synthetic tool returned", AT, "hook")
        self.store.update(sid, {"last_hook_at": AT, "liveness": "online"})
        return sid

    def test_unregistered_memory_status_and_descendants_are_hidden_without_erasing_history(self):
        self.index([])
        sid = self.hook_only("memory-status", self.codex / "memories")
        child = self.hook_only("memory-child", self.root / "helper")
        self.store.update(child, {"role": "child", "parent_id": sid})
        self.store.update(sid, {"alias": "Synthetic retained name"})
        self.store.usage(sid, "synthetic-usage", AT, 12, 3)
        history = self.store.stats({"role": "all", "date": "2026-10-15"}, AT)
        self.scan()
        snap = self.snapshot()
        self.assertEqual(snap["sessions"], [])
        self.assertEqual(snap["activity"], [])
        self.assertTrue(all(value == 0 for value in snap["counts"].values()))
        self.assertEqual(snap["today"], history)
        row = self.store.db.execute("SELECT alias,internal_memory FROM sessions WHERE id=?", (sid,)).fetchone()
        self.assertEqual(tuple(row), ("Synthetic retained name", 1))
        self.assertTrue(any(p["name"] == "memories" for p in snap["projects"]))

    def test_only_managed_memory_status_is_hidden_and_not_listed_as_a_new_project(self):
        self.index([])
        memory = self.hook_only("managed", self.codex / "memories" / "scratch")
        ordinary = self.hook_only("ordinary", self.root / "projects" / "memories")
        neighbor = self.hook_only("neighbor", self.codex / "memories-copy")
        self.scan()
        snap = self.snapshot()
        self.assertEqual({c["id"] for c in snap["sessions"]}, {ordinary, neighbor})
        self.assertNotIn("scratch", {p["name"] for p in snap["projects"]})
        self.assertEqual(self.store.db.execute("SELECT internal_memory FROM sessions WHERE id=?", (memory,)).fetchone()[0], 1)

    def test_registered_or_known_user_session_in_memory_directory_stays_visible(self):
        indexed = self.hook_only("registered-memory", self.codex / "memories")
        known = self.hook_only("known-user", self.codex / "memories")
        self.store.update(known, {"role": "main"})
        self.index([("registered-memory", "", "unknown", "user", 0)])
        self.scan()
        self.assertEqual({c["id"] for c in self.snapshot()["sessions"]}, {indexed, known})
        # Losing the index temporarily must not hide a previously recognized thread.
        (self.codex / "state_5.sqlite").unlink()
        self.scan()
        self.assertEqual({c["id"] for c in self.snapshot()["sessions"]}, {indexed, known})

    def test_rollout_arrival_restores_visibility_and_later_deletion_only_disables_jump(self):
        self.index([])
        sid = self.hook_only("late-record", self.codex / "memories")
        self.scan()
        self.assertEqual(self.snapshot()["sessions"], [])
        path = self.transcript("late-record", source="unknown")
        self.scan()
        card = self.snapshot()["sessions"][0]
        self.assertEqual(card["id"], sid)
        self.assertTrue(card["jump"]["available"])
        self.assertNotEqual(card["coverage"], "status_only")
        path.unlink()
        self.scan()
        card = self.snapshot()["sessions"][0]
        self.assertEqual(card["id"], sid)
        self.assertFalse(card["jump"]["available"])

    def test_unavailable_index_after_restart_does_not_newly_hide_a_registered_user(self):
        sid = self.hook_only("registered-memory", self.codex / "memories")
        self.index([("registered-memory", "", "unknown", "user", 0)])
        self.scan()
        (self.codex / "state_5.sqlite").unlink()
        self.collector = Collector(self.store, self.codex, self.root / "claude", self.root / "data",
                                   self.root / "desktop", workbuddy_home=self.root / "workbuddy")
        self.scan()
        self.assertEqual([c["id"] for c in self.snapshot()["sessions"]], [sid])

    def test_existing_database_migrates_without_losing_aliases_or_history(self):
        sid = self.store.session("codex", "legacy")
        self.store.update(sid, {"alias": "Existing name"})
        self.store.usage(sid, "legacy-usage", AT, 12, 3)
        self.store.db.commit()
        original = self.store.db.execute("SELECT sql FROM sqlite_master WHERE name='sessions'").fetchone()[0]
        columns = [r[1] for r in self.store.db.execute("PRAGMA table_info(sessions)")
                   if r[1] not in {"archived", "archive_source", "internal", "internal_memory"}]
        self.store.db.execute("DROP VIEW visible_sessions")
        self.store.db.execute("ALTER TABLE sessions RENAME TO old_sessions")
        legacy_schema = original.replace("archived INTEGER NOT NULL DEFAULT 0, archive_source TEXT NOT NULL DEFAULT '',", "").replace("internal INTEGER NOT NULL DEFAULT 0,", "").replace("internal_memory INTEGER NOT NULL DEFAULT 0,", "")
        self.store.db.execute(legacy_schema)
        names = ",".join(columns)
        self.store.db.execute("INSERT INTO sessions (" + names + ") SELECT " + names + " FROM old_sessions")
        self.store.db.execute("DROP TABLE old_sessions")
        self.store.db.commit()
        self.store.close()
        self.store = Store(self.root / "data/watch.sqlite")
        self.assertEqual(self.snapshot()["sessions"][0]["alias"], "Existing name")
        self.assertEqual(self.store.stats({"role": "all", "date": "2026-10-15"}, AT)["totals"]["total"], 15)
        self.assertEqual(self.store.db.execute("SELECT archived,internal,internal_memory FROM sessions").fetchone()[:], (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
