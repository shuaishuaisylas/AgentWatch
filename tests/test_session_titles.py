"""Formal session titles from artificial client indexes, never prompt excerpts."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agentwatch.collector import Collector, RecordParser
from agentwatch.demo import DemoEnvironment
from agentwatch.store import Store


AT = 1792029600.0
CURRENT = AT + 100
PRIVATE = "ARTIFICIAL_PROMPT_OR_GENERATED_SUMMARY_NEVER_READ"


class SessionTitleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.codex = self.root / "codex"
        self.codex.mkdir()
        self.path = self.root / "data/watch.sqlite"
        self.store = Store(self.path)
        self.collector = Collector(self.store, self.codex, self.root / "claude", self.root / "data",
                                   self.root / "desktop", self.root / "workbuddy")

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def session(self, identity="artificial", role="main", parent=None, **extra):
        sid = self.store.session("codex", identity)
        self.store.update(sid, {"role": role, "parent_id": parent, "last_event_at": AT,
                                "state": "idle", "state_at": AT, "attention_covered": 1,
                                **extra})
        return sid

    def index(self, title="人工客户端会话名称", include_title=True, identity="artificial", **extra):
        index = self.codex / "state_5.sqlite"
        fields = {"id": identity, "cwd": str(self.root / "g-p-artificial-project"),
                  "source": "cli", "originator": "codex_cli_rs", "archived": 0,
                  "first_user_message": PRIVATE, "summary": PRIVATE}
        if include_title:
            fields["title"] = title
        fields.update(extra)
        with sqlite3.connect(index) as db:
            db.execute("DROP TABLE IF EXISTS threads")
            db.execute("CREATE TABLE threads (" + ",".join(name + " TEXT" for name in fields) + ")")
            db.execute("INSERT INTO threads (" + ",".join(fields) + ") VALUES (" +
                       ",".join("?" for _ in fields) + ")", tuple(fields.values()))
        return index

    def discover(self):
        with patch("agentwatch.collector.now", return_value=CURRENT):
            self.collector.discover()

    def snapshot(self):
        return self.store.snapshot({"role": "main"}, {}, current=CURRENT)

    def card(self, sid):
        return next(card for card in self.snapshot()["sessions"] if card["id"] == sid)

    def title(self, sid="codex:artificial"):
        return self.store.db.execute("SELECT session_title FROM sessions WHERE id=?", (sid,)).fetchone()[0]

    def test_new_sessions_have_a_nullable_title_in_api_cards(self):
        sid = self.session()
        self.assertIsNone(self.title(sid))
        self.assertIn("session_title", self.card(sid))
        self.assertIsNone(self.card(sid)["session_title"])

    def test_old_database_migration_preserves_alias_state_and_usage(self):
        sid = self.session(alias="人工手动别名")
        self.store.usage(sid, "artificial-usage", AT, 123, 45, None, None, None, "artificial")
        schema = self.store.db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='sessions'").fetchone()[0]
        legacy_schema = schema.replace(", session_title TEXT", "")
        self.assertNotEqual(legacy_schema, schema)
        columns = [row[1] for row in self.store.db.execute("PRAGMA table_info(sessions)") if row[1] != "session_title"]
        existing = tuple(self.store.db.execute("SELECT " + ",".join(columns) + " FROM sessions WHERE id=?", (sid,)).fetchone())
        self.store.db.commit()
        self.store.close()
        with sqlite3.connect(self.path) as db:
            db.execute("DROP VIEW visible_sessions")
            db.execute("DROP TABLE sessions")
            db.execute(legacy_schema)
            db.execute("INSERT INTO sessions (" + ",".join(columns) + ") VALUES (" +
                       ",".join("?" for _ in columns) + ")", existing)
        self.store = Store(self.path)
        row = self.store.db.execute("SELECT session_title,alias,state FROM sessions WHERE id=?", (sid,)).fetchone()
        self.assertEqual(tuple(row), (None, "人工手动别名", "idle"))
        self.assertEqual(self.store.db.execute("SELECT SUM(total) FROM usage").fetchone()[0], 168)
        self.assertIsNone(self.card(sid)["session_title"])

    def test_discovery_reads_existing_formal_client_title(self):
        self.index()
        self.discover()
        self.assertEqual(self.title(), "人工客户端会话名称")
        self.store.touch("codex:artificial", AT)
        card = self.card("codex:artificial")
        self.assertEqual(card["session_title"], "人工客户端会话名称")
        self.assertEqual(card["project_name"], "g-p-artificial-project")
        self.assertEqual(card["alias"], "")

    def test_client_rename_updates_title_without_overwriting_manual_alias(self):
        sid = self.session(alias="用户设置的人工别名")
        self.index(title="人工原标题")
        self.discover()
        self.index(title="人工重命名后的标题")
        self.discover()
        card = self.card(sid)
        self.assertEqual(card["session_title"], "人工重命名后的标题")
        self.assertEqual(card["alias"], "用户设置的人工别名")

    def test_explicit_empty_or_null_client_title_clears_to_null(self):
        sid = self.session()
        for value in ["", " \n\t ", None]:
            with self.subTest(value=value):
                self.store.update(sid, {"session_title": "人工旧标题"})
                self.index(title=value)
                self.discover()
                self.assertIsNone(self.title(sid))
                self.assertIsNone(self.card(sid)["session_title"])

    def test_index_without_title_column_preserves_known_title(self):
        sid = self.session(session_title="此前正式会话名")
        self.index(include_title=False)
        self.discover()
        self.assertTrue(self.collector.codex_index_available)
        self.assertEqual(self.title(sid), "此前正式会话名")

    def test_unreadable_or_missing_index_preserves_known_title(self):
        sid = self.session(session_title="此前正式会话名")
        index = self.index()
        index.write_bytes(b"artificial invalid SQLite file")
        self.discover()
        self.assertFalse(self.collector.codex_index_available)
        self.assertGreater(self.collector.status["errors"], 0)
        self.assertEqual(self.title(sid), "此前正式会话名")
        index.unlink()
        self.discover()
        self.assertEqual(self.title(sid), "此前正式会话名")

    def test_metadata_and_update_normalize_title_whitespace_nul_and_length(self):
        sid = self.session()
        self.store.metadata(sid, session_title="  人工\n标题\t\x00示例  ")
        self.assertEqual(self.title(sid), "人工 标题 示例")
        self.store.update(sid, {"session_title": "人" * 200})
        self.assertEqual(self.title(sid), "人" * 160)
        self.assertEqual(self.card(sid)["session_title"], "人" * 160)
        self.store.metadata(sid, session_title=None)
        self.assertIsNone(self.title(sid))

    def test_discovery_never_reads_first_prompt_or_generated_summary_columns(self):
        self.index(title="正式标题")
        real_connect = sqlite3.connect
        forbidden_reads = []
        def connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            def authorize(action, table, column, database, trigger):
                if action == sqlite3.SQLITE_READ and table == "threads" and column in ["first_user_message", "summary"]:
                    forbidden_reads.append(column)
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            connection.set_authorizer(authorize)
            return connection
        with patch("agentwatch.collector.sqlite3.connect", side_effect=connect):
            self.discover()
        self.assertEqual(forbidden_reads, [])
        self.assertEqual(self.title(), "正式标题")
        self.assertEqual(self.collector.status["errors"], 0)
        self.assertNotIn(PRIVATE, "\n".join(self.store.db.iterdump()))

    def test_missing_formal_title_does_not_use_first_prompt_or_summary(self):
        self.index(include_title=False)
        self.discover()
        self.assertIsNone(self.title())
        self.assertNotIn(PRIVATE, "\n".join(self.store.db.iterdump()))

    def test_transcript_content_and_generated_summary_do_not_set_title(self):
        sid = self.session()
        parser = RecordParser(self.store)
        cursor = {"sid": sid}
        for payload in [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": PRIVATE}]},
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": PRIVATE}]},
                {"type": "summary", "summary": PRIVATE, "title": PRIVATE}]:
            parser.codex({"type": "response_item", "timestamp": AT, "payload": payload}, cursor)
        self.assertIsNone(self.title(sid))
        self.assertNotIn(PRIVATE, "\n".join(self.store.db.iterdump()))

    def test_titles_do_not_change_archive_role_or_navigation_availability(self):
        sid = self.session(alias="人工别名", archived=1, archive_source="index", role="child", parent_id="codex:parent")
        before = dict(self.store.db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone())
        jump_before = self.store.jump_info(before)
        self.store.metadata(sid, session_title="人工正式名称")
        after = dict(self.store.db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone())
        self.assertEqual({key: value for key, value in before.items() if key != "session_title"},
                         {key: value for key, value in after.items() if key != "session_title"})
        self.assertEqual(self.store.jump_info(after), jump_before)
        self.assertEqual(self.snapshot()["sessions"], [])

    def test_root_snapshot_and_child_detail_return_distinct_formal_titles(self):
        parent = self.session("parent", session_title="人工主会话标题")
        child = self.session("child", role="child", parent=parent, session_title="人工子会话标题")
        grandchild = self.session("grandchild", role="child", parent=child)
        self.assertEqual(self.card(parent)["session_title"], "人工主会话标题")
        cards = {card["id"]: card for card in self.store.children(parent, current=CURRENT)["children"]}
        self.assertEqual(cards[child]["session_title"], "人工子会话标题")
        self.assertIsNone(cards[grandchild]["session_title"])

    def test_index_title_updates_parent_and_child_independently(self):
        parent = self.session("parent")
        child = self.session("child", role="child", parent=parent)
        self.index(identity="parent", title="人工主会话标题")
        index = self.codex / "state_5.sqlite"
        source = json.dumps({"subagent": {"thread_spawn": {"parent_thread_id": "parent"}}})
        with sqlite3.connect(index) as db:
            db.execute("INSERT INTO threads (id,source,title) VALUES (?,?,?)", ("child", source, "人工子会话标题"))
        self.discover()
        self.assertEqual(self.card(parent)["session_title"], "人工主会话标题")
        self.assertEqual(self.store.children(parent, current=CURRENT)["children"][0]["session_title"], "人工子会话标题")
        self.assertEqual(self.title(child), "人工子会话标题")

    def test_demo_has_a_title_only_main_and_preserves_alias_examples_and_counts(self):
        with DemoEnvironment() as demo:
            snapshot = demo.store.snapshot({"role": "main"}, {}, current=demo.collector.status["last_scan"])
            cards = {card["id"]: card for card in snapshot["sessions"]}
            titled = cards["codex:demo-complete"]
            self.assertEqual(titled["alias"], "")
            self.assertEqual(titled["session_title"], "演示会话 · 文档校对")
            self.assertEqual(titled["project_name"], "g-p-demo-title-project")
            self.assertEqual(cards["codex:demo-design"]["alias"], "设计系统整理")
            self.assertTrue(cards["codex:demo-design"]["session_title"].startswith("演示会话"))
            self.assertEqual(snapshot["counts"]["active"], 2)
            self.assertEqual(snapshot["attention_counts"]["pending"], 2)
            self.assertEqual(snapshot["inbox_counts"]["pending"], 3)


if __name__ == "__main__":
    unittest.main()
