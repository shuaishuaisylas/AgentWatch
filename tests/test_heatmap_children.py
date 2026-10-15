from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agentwatch.common import timestamp
from agentwatch.server import Handler
from agentwatch.store import Store


AT = timestamp("2026-10-15T10:00:00+08:00")


class HeatmapAndChildrenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "synthetic.sqlite")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def session(self, name, tool="codex", role="main", parent=None, at=AT, **values):
        sid = self.store.session(tool, name)
        self.store.update(sid, dict(role=role, parent_id=parent, last_event_at=at, state_at=at,
                                    state="idle", **values))
        return sid

    def dispatch(self, path, demo=False):
        handler = Handler.__new__(Handler)
        handler.path = path
        handler.server = SimpleNamespace(store=self.store, demo=demo,
                                         collector=SimpleNamespace(refresh=lambda: None))
        handler.trust = lambda: True
        handler.authorized = lambda **options: True
        responses = []
        handler.json_response = lambda status, value: responses.append((status, value))
        with patch("agentwatch.store.now", return_value=AT):
            handler.do_GET()
        self.assertEqual(len(responses), 1)
        return responses[0]

    def test_current_beijing_year_boundaries_and_no_record_days_remain_unknown(self):
        sid = self.session("boundaries")
        for identity, at, amount in [
                ("old", "2025-12-31T15:59:59Z", 1),
                ("first", "2025-12-31T16:00:00Z", 10),
                ("last", "2026-12-31T15:59:59Z", 20),
                ("next", "2026-12-31T16:00:00Z", 100)]:
            self.store.usage(sid, identity, timestamp(at), amount, 0)
        heatmap = self.store.heatmap({"period": "day", "date": "1999-01-01"}, AT)
        self.assertEqual((heatmap["year"], heatmap["start"], heatmap["end"], len(heatmap["days"])),
                         (2026, "2026-01-01", "2027-01-01", 365))
        self.assertEqual(heatmap["date"], "2026-10-15")
        self.assertEqual(heatmap["totals"]["total"], 30)
        self.assertEqual(heatmap["days"][0], {"date": "2026-01-01", "tokens": 10, "has_usage": True})
        self.assertEqual(heatmap["days"][-1], {"date": "2026-12-31", "tokens": 20, "has_usage": True})
        self.assertEqual(heatmap["days"][1], {"date": "2026-01-02", "tokens": None, "has_usage": False})
        self.assertEqual(heatmap["first_usage_day"], "2025-12-31")

    def test_leap_year_and_true_zero_are_not_missing(self):
        sid = self.session("zero")
        self.store.usage(sid, "zero-record", timestamp("2024-02-29T12:00:00+08:00"), 0, 0, cache_read=0)
        heatmap = self.store.heatmap({}, timestamp("2023-12-31T16:00:00Z"))
        self.assertEqual((heatmap["year"], len(heatmap["days"])), (2024, 366))
        leap_day = next(day for day in heatmap["days"] if day["date"] == "2024-02-29")
        self.assertEqual(leap_day, {"date": "2024-02-29", "tokens": 0, "has_usage": True})
        self.assertTrue(heatmap["has_usage"])
        self.assertEqual(heatmap["totals"]["total"], 0)
        self.assertEqual(heatmap["totals"]["cache_read"], 0)
        self.assertIsNone(heatmap["totals"]["cache_write"])

    def test_all_roles_archives_and_dynamic_tools_share_stats_token_semantics(self):
        for name, tool, role, archived in [("root", "codex", "main", 0),
                                           ("child", "claude", "child", 0),
                                           ("unknown", "future", "unknown", 1)]:
            sid = self.session(name, tool, role, archived=archived, coverage="partial_usage")
            self.store.usage(sid, name, AT, 10, 5, cache_read=7, reasoning=2)
        self.session("discovered-without-usage", "other")
        heatmap = self.store.heatmap({"tool": "all", "role": "main", "project": "missing", "surface": "none"}, AT)
        self.assertEqual(heatmap["totals"], self.store.stats({"role": "all", "period": "year", "date": "2026-10-15"}, AT)["totals"])
        self.assertEqual(heatmap["totals"]["total"], 45)
        self.assertEqual(heatmap["partial_sessions"], 3)
        self.assertEqual(heatmap["tools"], ["claude", "codex", "future", "other"])
        filtered = self.store.heatmap({"tool": "future"}, AT)
        self.assertEqual(filtered["totals"]["total"], 15)
        self.assertEqual(filtered["partial_sessions"], 1)
        self.assertEqual(self.store.heatmap({"tool": "missing"}, AT)["totals"], dict.fromkeys(heatmap["totals"]))

    def test_empty_year_has_no_fabricated_usage(self):
        heatmap = self.store.heatmap({}, AT)
        self.assertFalse(heatmap["has_usage"])
        self.assertIsNone(heatmap["first_usage_day"])
        self.assertTrue(all(value is None for value in heatmap["totals"].values()))
        self.assertTrue(all(day["tokens"] is None and not day["has_usage"] for day in heatmap["days"]))

    def test_children_are_independent_of_160_card_limit_age_and_filter(self):
        root = self.session("root", project_id="root-project")
        for index in range(175):
            self.session("child" + str(index), role="child", parent=root, at=AT - 86400,
                         project_id="different-project")
        child = self.session("branch", tool="future", role="child", parent=root, at=AT - 86400)
        grandchild = self.session("leaf", role="child", parent=child, at=AT - 86400)
        snap = self.store.snapshot({"role": "main", "tool": "codex", "project": "root-project"}, {}, AT)
        self.assertEqual(len(snap["sessions"]), 1)
        self.assertEqual(snap["sessions"][0]["children_summary"]["total"], 177)
        tree = self.store.children(root, AT)
        self.assertFalse(tree["truncated"])
        self.assertEqual(tree["counts"]["total"], 177)
        children = {card["id"]: card for card in tree["children"]}
        self.assertEqual(children[child]["depth"], 1)
        self.assertEqual(children[grandchild]["depth"], 2)
        ids = [card["id"] for card in tree["children"]]
        self.assertLess(ids.index(child), ids.index(grandchild))
        self.assertTrue(all(card["role"] == "child" for card in tree["children"]))
        self.assertIsNone(children[child]["usage"]["observed_total"])
        self.assertIsNone(children[child]["context_percent"])

    def test_hidden_unknown_and_unrelated_relationships_are_not_adopted(self):
        root = self.session("root")
        keep = self.session("keep", role="child", parent=root)
        for flag in ["archived", "internal", "internal_memory"]:
            hidden = self.session(flag, role="child", parent=root, **{flag: 1})
            self.session(flag + "-descendant", role="child", parent=hidden)
        unknown = self.session("unknown", role="unknown", parent=root)
        self.session("below-unknown", role="child", parent=unknown)
        self.session("unrelated", role="child", parent="codex:missing")
        self.assertEqual([card["id"] for card in self.store.children(root, AT)["children"]], [keep])
        for identity in [unknown, keep, "codex:missing"]:
            self.assertIsNone(self.store.children(identity, AT))
        self.store.update(root, {"archived": 1})
        self.assertIsNone(self.store.children(root, AT))

    def test_cycles_terminate_without_duplicating_or_adopting_unknown_roles(self):
        root = self.session("root")
        child = self.session("child", role="child", parent=root)
        self.store.update(root, {"parent_id": child})  # Malformed back edge into the main node.
        a = self.session("cycle-a", role="child", parent="codex:cycle-b")
        self.session("cycle-b", role="child", parent=a)
        tree = self.store.children(root, AT)
        self.assertEqual([(card["id"], card["depth"]) for card in tree["children"]], [(child, 1)])
        self.assertEqual(self.store.children_summaries([root], AT)[root]["total"], 1)

    def test_children_counts_use_exact_snapshot_stale_and_liveness_rules(self):
        root = self.session("root")
        cases = [("fresh-hook", "active", "unknown", "hook", 0),
                 ("fresh-event", "active", "unknown", "event", 0),
                 ("offline", "active", "offline", "hook", 0),
                 ("online-record", "active", "online", "record", 0),
                 ("unconfirmed", "active", "unknown", "record", 0),
                 ("stale-active", "active", "online", "hook", 121),
                 ("stale-wait", "waiting_user", "unknown", "hook", 600),
                 ("permission", "waiting_approval", "unknown", "hook", 0),
                 ("error", "error", "unknown", "event", 0)]
        for name, state, live, source, age in cases:
            sid = self.session(name, role="child", parent=root)
            self.store.update(sid, dict(state=state, liveness=live, state_source=source, state_at=AT-age))
        tree = self.store.children(root, AT)
        self.assertEqual(tree["counts"], {"total": 9, "active": 3, "waiting": 2, "error": 1, "unknown": 1})
        card = self.store.snapshot({}, {}, AT)["sessions"][0]
        self.assertEqual(card["children_summary"], tree["counts"])
        self.assertTrue(next(card for card in tree["children"] if card["source_id"] == "stale-wait")["stale"])

    def test_children_card_has_snapshot_fields_and_no_private_process_metadata(self):
        root = self.session("root")
        child = self.session("child", tool="claude", role="child", parent=root,
                             cwd="/example/synthetic", pid=123, tty="/dev/synthetic", context_used=7)
        self.store.usage(child, "request", AT, 10, 5)
        card = self.store.children(root, AT)["children"][0]
        snap_card = next(card for card in self.store.snapshot({"role": "all"}, {}, AT)["sessions"] if card["id"] == child)
        self.assertEqual({key: value for key, value in card.items() if key != "depth"}, snap_card)
        self.assertFalse({"pid", "tty", "cwd", "worktree", "root_id", "archived"}.intersection(card))

    def test_main_snapshot_keeps_old_roots_with_recent_or_online_known_descendants(self):
        root = self.session("old-root", at=AT - 86400 * 2, project_id="selected")
        branch = self.session("old-branch", role="child", parent=root, at=AT - 86400 * 2)
        self.session("recent-leaf", role="child", parent=branch, project_id="different")
        online = self.session("online-root", at=AT - 86400 * 2, project_id="selected")
        self.session("online-child", role="child", parent=online, at=AT - 86400 * 2, liveness="online")
        unknown_root = self.session("unknown-path-root", at=AT - 86400 * 2, project_id="selected")
        unknown = self.session("unknown-node", role="unknown", parent=unknown_root, at=AT - 86400 * 2)
        self.session("recent-below-unknown", role="child", parent=unknown)
        for flag in ["archived", "internal", "internal_memory"]:
            blocked_root = self.session(flag + "-root", at=AT - 86400 * 2, project_id="selected")
            blocked = self.session(flag + "-branch", role="child", parent=blocked_root,
                                   at=AT - 86400 * 2, **{flag: 1})
            self.session(flag + "-recent-leaf", role="child", parent=blocked)
        snapshot = self.store.snapshot({"project": "selected"}, {}, AT)
        self.assertEqual({card["id"] for card in snapshot["sessions"]}, {root, online})
        self.assertEqual(snapshot["counts"]["observed"], 2)
        self.assertEqual(snapshot["children_counts"]["total"], 3)
        # The compatibility all-role query retains its original today/online range.
        legacy = self.store.snapshot({"role": "all", "project": "selected"}, {}, AT)
        self.assertEqual(legacy["sessions"], [])

    def test_descendant_wait_and_error_prioritize_roots_before_card_truncation(self):
        for index in range(170):
            self.session("idle-root-" + str(index))
        waiting_root = self.session("older-wait-root", at=AT - 86400 * 2)
        waiter = self.session("waiter", role="child", parent=waiting_root)
        self.store.update(waiter, {"state": "waiting_user", "state_source": "hook"})
        error_root = self.session("older-error-root", at=AT - 86400 * 2)
        error = self.session("error", role="child", parent=error_root)
        self.store.update(error, {"state": "error", "state_source": "event"})
        snapshot = self.store.snapshot({}, {}, AT)
        self.assertEqual([card["id"] for card in snapshot["sessions"][:2]], [waiting_root, error_root])
        self.assertTrue(snapshot["truncated"])
        self.assertEqual(snapshot["counts"]["observed"], 172)
        self.assertEqual(snapshot["counts"]["waiting"], 0)
        self.assertEqual(snapshot["counts"]["error"], 0)
        self.assertEqual(snapshot["children_counts"], {"total": 2, "active": 0, "waiting": 1, "error": 1, "unknown": 0})

    def test_child_counts_include_all_filtered_roots_before_truncation_and_chunking(self):
        for index in range(200):
            root = self.session("root-" + str(index), project_id="selected")
            child = self.session("child-" + str(index), role="child", parent=root, project_id="different")
            self.store.update(child, {"state": "active", "state_source": "hook"})
        excluded = self.session("excluded-root", project_id="excluded")
        excluded_child = self.session("excluded-child", role="child", parent=excluded)
        self.store.update(excluded_child, {"state": "waiting_approval"})
        snapshot = self.store.snapshot({"role": "main", "project": "selected"}, {}, AT)
        self.assertEqual(len(snapshot["sessions"]), 160)
        self.assertEqual(snapshot["counts"]["observed"], 200)
        self.assertEqual(snapshot["counts"]["active"], 0)
        self.assertEqual(snapshot["children_counts"], {"total": 200, "active": 200, "waiting": 0, "error": 0, "unknown": 0})
        self.assertTrue(all(card["children_summary"]["active"] == 1 for card in snapshot["sessions"]))

    def test_read_routes_preserve_demo_identity_jump_restriction_and_missing_root_404(self):
        root = self.session("root")
        self.session("child", tool="workbuddy", role="child", parent=root)
        for demo in [False, True]:
            status, heatmap = self.dispatch("/api/heatmap?tool=all&role=main", demo)
            self.assertEqual((status, heatmap["demo"], heatmap["year"]), (200, demo, 2026))
            status, tree = self.dispatch("/api/children?id=" + root + "&tool=missing&project=missing", demo)
            self.assertEqual((status, tree["demo"], tree["counts"]["total"]), (200, demo, 1))
            self.assertEqual(tree["children"][0]["jump"]["available"], not demo)
            self.assertEqual(self.dispatch("/api/children?id=codex:missing", demo)[0], 404)
            self.assertEqual(self.dispatch("/api/children", demo)[0], 404)


if __name__ == "__main__":
    unittest.main()
