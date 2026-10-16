"""Artificial sidebar name catalogs; no installed client sources or credentials."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agentwatch.collector import Collector
from agentwatch.codex_names import read_codex_names
from agentwatch.store import Store


AT = 1792029600.0
PRIVATE = "ARTIFICIAL_PRIVATE_BODY_AND_SETTINGS_NOT_PERSISTED"


class CodexNamesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "codex"
        self.home.mkdir()
        self.path = self.root / "data/watch.sqlite"
        self.store = Store(self.path)
        self.sid = self.store.session("codex", "artificial")
        self.store.update(self.sid, {"role": "main", "last_event_at": AT, "state": "idle", "state_at": AT,
                                     "alias": "人工手动别名", "attention_covered": 1})
        self.index = self.home / "state_5.sqlite"
        self.cwd = str(self.root / "projects/g-p-artificial-project")
        with sqlite3.connect(self.index) as db:
            db.execute("CREATE TABLE threads (id TEXT, title TEXT, cwd TEXT, source TEXT, first_user_message TEXT)")
            db.execute("INSERT INTO threads VALUES (?,?,?,?,?)", ("artificial", "[https://artificial.invalid/first-message]", self.cwd, "cli", PRIVATE))
        self.collector = self.make_collector()

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def make_collector(self):
        return Collector(self.store, self.home, self.root / "claude", self.root / "data",
                         self.root / "desktop", self.root / "workbuddy")

    def discover(self):
        with patch("agentwatch.collector.now", return_value=AT + 100):
            self.collector.discover()

    def row(self):
        return dict(self.store.db.execute("SELECT * FROM sessions WHERE id=?", (self.sid,)).fetchone())

    def sidebar(self, name="人工侧栏聊天标题", at=AT, identity="artificial", append=False):
        with (self.home / "session_index.jsonl").open("a" if append else "w") as stream:
            stream.write(json.dumps({"id": identity, "thread_name": name, "updated_at": at}) + "\n")

    def projects(self, name="人工正式项目名", roots=None, updated=AT, assignment=None, identity="legacy-project", migration=None):
        document = {"local-projects": {identity: {"id": identity, "name": name, "rootPaths": roots if roots is not None else [self.cwd], "updatedAt": updated}},
                    "electron-persisted-atom-state": {"private-body": PRIVATE}}
        if assignment:
            document["thread-project-assignments"] = {"artificial": {"projectId": assignment, "projectKind": "local"}}
        if migration:
            document["app-server-project-id-by-legacy-project-id-by-host"] = {"local:" + str(self.home): {identity: migration}}
        (self.home / ".codex-global-state.json").write_text(json.dumps(document))

    def native_project(self, name="人工原生项目名", at=AT + 1, identity="native-project", root=None):
        with sqlite3.connect(self.index) as db:
            db.execute("CREATE TABLE IF NOT EXISTS projects (id TEXT, name TEXT, metadata TEXT, updated_at_ms REAL)")
            db.execute("CREATE TABLE IF NOT EXISTS project_roots (project_id TEXT, path TEXT)")
            db.execute("DELETE FROM projects WHERE id=?", (identity,))
            db.execute("DELETE FROM project_roots WHERE project_id=?", (identity,))
            db.execute("INSERT INTO projects VALUES (?,?,?,?)", (identity, name, PRIVATE, at * 1000))
            db.execute("INSERT INTO project_roots VALUES (?,?)", (identity, root or self.cwd))

    def test_sidebar_name_wins_over_legacy_first_message_title_without_changing_alias(self):
        self.sidebar()
        self.discover()
        row = self.row()
        self.assertEqual(row["session_title"], "人工侧栏聊天标题")
        self.assertEqual(row["session_title_source"], "session_index")
        self.assertEqual(row["alias"], "人工手动别名")

    def test_latest_sidebar_timestamp_wins_over_file_order(self):
        self.sidebar("人工最新标题", AT + 10)
        self.sidebar("人工旧标题", AT, append=True)
        self.discover()
        self.assertEqual(self.row()["session_title"], "人工最新标题")
        self.sidebar("人工重命名", AT + 20, append=True)
        self.discover()
        self.assertEqual(self.row()["session_title"], "人工重命名")

    def test_rfc3339_variable_fractional_precision_is_valid_on_python_39(self):
        for fraction in ["1", "1234", "12345", "123456789"]:
            with self.subTest(fraction=fraction):
                self.sidebar("人工合法时间标题", "2026-10-15T10:00:00." + fraction + "Z")
                self.discover()
                self.assertEqual(self.row()["session_title"], "人工合法时间标题")
                self.assertEqual(self.row()["session_title_source"], "session_index")

    def test_unrelated_bad_records_and_partial_tail_do_not_discard_valid_names(self):
        self.sidebar("人工已验证标题")
        path = self.home / "session_index.jsonl"
        with path.open("a") as stream:
            stream.write(json.dumps({"id": "other", "thread_name": "人工坏时间", "updated_at": "invalid"}) + "\n")
            stream.write("not-json\n")
            stream.write('{"id":"unfinished')
        self.discover()
        self.assertEqual(self.row()["session_title"], "人工已验证标题")

    def test_known_invalid_record_identity_preserves_that_threads_existing_name(self):
        self.sidebar("人工此前确认名")
        self.discover()
        self.sidebar("人工不应采用的旧名")
        self.sidebar("人工坏时间记录", "invalid", append=True)
        self.discover()
        self.assertEqual(self.row()["session_title"], "人工此前确认名")

    def test_explicit_empty_sidebar_title_never_falls_back_to_first_message(self):
        for title in ["", " \n\t ", None]:
            with self.subTest(title=title):
                self.sidebar(title)
                self.discover()
                self.assertIsNone(self.row()["session_title"])
                self.assertEqual(self.row()["session_title_source"], "session_index")
                (self.home / "session_index.jsonl").unlink()
                self.discover()
                self.assertIsNone(self.row()["session_title"])

    def test_missing_corrupt_or_uncovered_sidebar_index_preserves_known_name_after_restart(self):
        self.sidebar()
        self.discover()
        self.store.db.commit()
        self.store.close()
        self.store = Store(self.path)
        self.collector = self.make_collector()
        path = self.home / "session_index.jsonl"
        for content in [None, "not-json\n", json.dumps({"id": "other", "thread_name": "其它人工标题", "updated_at": AT}) + "\n"]:
            with self.subTest(content=content):
                path.unlink(missing_ok=True)
                if content is not None:
                    path.write_text(content)
                self.discover()
                self.assertEqual(self.row()["session_title"], "人工侧栏聊天标题")
                self.assertEqual(self.row()["session_title_source"], "session_index")

    def test_legacy_index_remains_compatible_when_sidebar_file_is_absent(self):
        self.discover()
        self.assertEqual(self.row()["session_title_source"], "thread_index")
        with sqlite3.connect(self.index) as db:
            db.execute("UPDATE threads SET title='人工兼容重命名'")
        self.discover()
        self.assertEqual(self.row()["session_title"], "人工兼容重命名")

    def test_metadata_limits_and_invalid_records_preserve_known_sidebar_name(self):
        self.sidebar()
        self.discover()
        path = self.home / "session_index.jsonl"
        for content in [json.dumps({"id": "bad id", "thread_name": "人工坏记录", "updated_at": AT}),
                        json.dumps({"id": "artificial", "thread_name": "人工缺时间"}), "[null]"]:
            path.write_text(content + "\n")
            self.discover()
            self.assertEqual(self.row()["session_title"], "人工侧栏聊天标题")
        self.sidebar("人工可能截断标题")
        with patch("agentwatch.codex_names.MAX_METADATA_BYTES", 20):
            self.discover()
        self.assertEqual(self.row()["session_title"], "人工侧栏聊天标题")

    def test_global_project_exact_root_mapping_and_renaming_preserve_directory_identity(self):
        self.projects()
        self.discover()
        before = self.row()
        self.assertEqual(before["project_display_name"], "人工正式项目名")
        self.assertEqual(before["project_name"], "g-p-artificial-project")
        self.projects(name="人工项目新名称", updated=AT + 1)
        self.discover()
        after = self.row()
        self.assertEqual(after["project_display_name"], "人工项目新名称")
        for key in ["project_name", "project_id", "cwd", "alias", "role"]:
            self.assertEqual(after[key], before[key])

    def test_same_basename_prefix_or_parent_path_never_guesses_a_project(self):
        for root in [str(self.root / "other/g-p-artificial-project"), self.cwd + "-other", str(Path(self.cwd).parent)]:
            with self.subTest(root=root):
                self.projects(roots=[root])
                self.discover()
                self.assertIsNone(self.row()["project_display_name"])
        self.projects(roots=[self.cwd + "/./"])
        self.discover()
        self.assertEqual(self.row()["project_display_name"], "人工正式项目名")

    def test_explicit_project_identity_supports_a_different_worktree_root(self):
        self.projects(roots=[str(self.root / "other-worktree")], assignment="legacy-project")
        self.discover()
        self.assertEqual(self.row()["project_display_name"], "人工正式项目名")

    def test_unknown_explicit_assignment_does_not_guess_from_a_shared_root(self):
        self.store.update(self.sid, {"project_display_name": "人工已知项目"})
        self.projects(assignment="uncovered-project")
        self.discover()
        self.assertEqual(self.row()["project_display_name"], "人工已知项目")

    def test_malformed_explicit_assignment_never_falls_back_to_cwd_matching(self):
        self.store.update(self.sid, {"project_display_name": "人工此前确认名称"})
        self.projects()
        path = self.home / ".codex-global-state.json"
        original = json.loads(path.read_text())
        for assignment in [None, "legacy-project", {}, {"projectId": 123},
                           {"projectId": "legacy-project", "projectKind": "unsupported-future-kind"}]:
            with self.subTest(assignment=assignment):
                document = dict(original, **{"thread-project-assignments": {"artificial": assignment}})
                path.write_text(json.dumps(document))
                self.discover()
                self.assertEqual(self.row()["project_display_name"], "人工此前确认名称")

    def test_newer_native_and_global_names_use_migration_identity_and_timestamp(self):
        self.projects(updated=AT, migration="native-project", assignment="legacy-project")
        self.native_project(at=AT + 1)
        self.discover()
        self.assertEqual(self.row()["project_display_name"], "人工原生项目名")
        self.projects(name="人工全局较新项目名", updated=AT + 2, migration="native-project", assignment="legacy-project")
        self.discover()
        self.assertEqual(self.row()["project_display_name"], "人工全局较新项目名")

    def test_native_project_catalog_works_without_global_state(self):
        self.native_project()
        self.discover()
        self.assertEqual(self.row()["project_display_name"], "人工原生项目名")

    def test_equal_time_conflict_or_distinct_projects_on_one_root_preserve_known_name(self):
        self.store.update(self.sid, {"project_display_name": "人工此前确认名称"})
        self.projects(updated=AT, migration="native-project")
        self.native_project(at=AT)
        self.discover()
        self.assertEqual(self.row()["project_display_name"], "人工此前确认名称")
        self.projects(updated=AT)
        self.discover()
        self.assertEqual(self.row()["project_display_name"], "人工此前确认名称")

    def test_unavailable_corrupt_or_unsupported_project_metadata_preserves_known_name(self):
        self.store.update(self.sid, {"project_display_name": "人工此前确认名称"})
        self.discover()
        self.assertEqual(self.row()["project_display_name"], "人工此前确认名称")
        state = self.home / ".codex-global-state.json"
        for content in ["not-json", "{}", json.dumps({"local-projects": {"legacy": {"id": "legacy", "name": 123}}})]:
            state.write_text(content)
            self.discover()
            self.assertEqual(self.row()["project_display_name"], "人工此前确认名称")

    def test_corrupt_global_catalog_does_not_downgrade_to_stale_native_name(self):
        self.store.update(self.sid, {"project_display_name": "人工较新正式名"})
        self.native_project(name="人工旧原生名")
        (self.home / ".codex-global-state.json").write_text("invalid")
        self.discover()
        self.assertEqual(self.row()["project_display_name"], "人工较新正式名")

    def test_invalid_project_roots_are_unknown_rather_than_confirmed_absent(self):
        self.store.update(self.sid, {"project_display_name": "人工已知项目"})
        for root in [None, 123, "relative/path", "/invalid\x00path"]:
            with self.subTest(root=root):
                self.projects(roots=[root])
                self.discover()
                self.assertEqual(self.row()["project_display_name"], "人工已知项目")

    def test_explicit_empty_name_and_complete_empty_catalog_clear_project_display_only(self):
        self.projects(name="")
        self.discover()
        self.assertIsNone(self.row()["project_display_name"])
        self.store.update(self.sid, {"project_display_name": "人工此前名称"})
        (self.home / ".codex-global-state.json").write_text(json.dumps({"local-projects": {}}))
        self.discover()
        self.assertIsNone(self.row()["project_display_name"])
        self.assertEqual(self.row()["project_name"], "g-p-artificial-project")

    def test_private_source_columns_and_settings_never_enter_store(self):
        self.sidebar()
        self.projects()
        self.native_project()
        real_connect = sqlite3.connect
        forbidden = []
        def connect(*args, **kwargs):
            db = real_connect(*args, **kwargs)
            def authorize(action, table, column, database, trigger):
                if action == sqlite3.SQLITE_READ and (table, column) in [("threads", "first_user_message"), ("projects", "metadata")]:
                    forbidden.append((table, column))
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            db.set_authorizer(authorize)
            return db
        with patch("agentwatch.collector.sqlite3.connect", side_effect=connect):
            self.discover()
        self.assertEqual(forbidden, [])
        self.assertEqual(self.collector.status["errors"], 0)
        self.assertNotIn(PRIVATE, "\n".join(self.store.db.iterdump()))

    def test_source_metadata_is_byte_for_byte_unchanged_by_discovery(self):
        self.sidebar()
        self.projects()
        paths = [self.index, self.home / "session_index.jsonl", self.home / ".codex-global-state.json"]
        before = {path: path.read_bytes() for path in paths}
        self.discover()
        self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def test_project_name_normalization_and_internal_source_not_exposed(self):
        self.projects(name="  人工\n\t项目\x00名  ")
        self.sidebar()
        self.discover()
        self.assertEqual(self.row()["project_display_name"], "人工 项目名")
        self.store.update(self.sid, {"project_display_name": "长" * 200})
        card = self.store.snapshot({"role": "main"}, {}, current=AT + 100)["sessions"][0]
        self.assertEqual(card["project_display_name"], "长" * 160)
        self.assertNotIn("session_title_source", card)

    def test_legacy_database_migrates_project_name_and_title_source_without_losing_data(self):
        self.store.usage(self.sid, "artificial-use", AT, 3, 4, None, None, None, "artificial")
        schema = self.store.db.execute("SELECT sql FROM sqlite_master WHERE name='sessions'").fetchone()[0]
        legacy = schema.replace("project_display_name TEXT, ", "").replace("session_title_source TEXT NOT NULL DEFAULT '',", "")
        self.assertNotEqual(schema, legacy)
        columns = [row[1] for row in self.store.db.execute("PRAGMA table_info(sessions)") if row[1] not in ["project_display_name", "session_title_source"]]
        values = tuple(self.store.db.execute("SELECT " + ",".join(columns) + " FROM sessions").fetchone())
        self.store.db.commit()
        self.store.close()
        with sqlite3.connect(self.path) as db:
            db.execute("DROP VIEW visible_sessions")
            db.execute("DROP TABLE sessions")
            db.execute(legacy)
            db.execute("INSERT INTO sessions (" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")", values)
        self.store = Store(self.path)
        self.assertIsNone(self.row()["project_display_name"])
        self.assertEqual(self.row()["session_title_source"], "")
        self.assertEqual(self.row()["alias"], "人工手动别名")
        self.assertEqual(self.store.db.execute("SELECT SUM(total) FROM usage").fetchone()[0], 7)


if __name__ == "__main__":
    unittest.main()
