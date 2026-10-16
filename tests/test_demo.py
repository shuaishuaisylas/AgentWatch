from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agentwatch import __main__ as entrypoint
from agentwatch.common import TZ, day_of, now
from agentwatch.demo import DemoEnvironment
from agentwatch.server import Handler
from agentwatch.subscriptions import Subscriptions
from agentwatch.lifecycle_configuration import LifecycleConfiguration


class DemoTests(unittest.TestCase):
    def setUp(self):
        self.environment = DemoEnvironment()
        self.demo = self.environment.__enter__()
        self.server = SimpleNamespace(demo=True, store=self.demo.store,
                                      collector=self.demo.collector, config=self.demo.config,
                                      lifecycle_configs={tool: LifecycleConfiguration(tool, home, self.demo.store.path.parent)
                                                         for tool, home in [("codex", self.demo.collector.codex_home), ("workbuddy", self.demo.collector.workbuddy_home)]},
                                      subscriptions=Subscriptions(self.demo.store, demo=True))

    def tearDown(self):
        self.environment.__exit__(None, None, None)

    def dispatch(self, path, body=None):
        """Exercise real route handling without binding a network port."""
        handler = Handler.__new__(Handler)
        handler.server, handler.path = self.server, path
        handler.trust = lambda: True
        handler.authorized = lambda **options: True
        responses = []
        handler.json_response = lambda status, value: responses.append((status, value))
        raw = json.dumps(body).encode() if body is not None else b""
        handler.rfile = io.BytesIO(raw)
        handler.headers = {"Content-Length": str(len(raw))}
        handler.do_GET() if body is None else handler.do_POST()
        self.assertEqual(len(responses), 1)
        return responses[0]

    def test_all_source_roots_and_database_are_temporary_and_no_real_collector_runs(self):
        collector = self.demo.collector
        for path in [collector.codex_home, collector.claude_home, collector.desktop_home,
                     collector.workbuddy_home, collector.cursor_home, self.demo.store.path]:
            self.assertTrue(path.is_relative_to(self.demo.root))
            self.assertTrue(path.exists())
        with patch("agentwatch.collector.process_inventory", side_effect=AssertionError("real processes")), \
             patch("agentwatch.collector.Collector.discover", side_effect=AssertionError("real records")), \
             patch("agentwatch.configuration.ClaudeConfiguration.read", side_effect=AssertionError("real settings")):
            collector.start()
            collector.scan(force=True)
            collector.stop()
            status, integrations = self.dispatch("/api/integrations")
        self.assertEqual(status, 200)
        self.assertTrue(integrations["demo"])
        self.assertTrue(all(source["present"] for source in integrations["sources"]))
        self.assertEqual(collector.status["files"], 0)
        self.assertEqual(collector.status["read_bytes"], 0)
        self.assertIn("人工", integrations["privacy"])

    def test_sample_states_roles_context_and_history_have_explicit_demo_identity(self):
        status, snapshot = self.dispatch("/api/snapshot?role=all")
        self.assertEqual(status, 200)
        self.assertTrue(snapshot["demo"])
        cards = snapshot["sessions"]
        self.assertEqual({card["tool"] for card in cards}, {"codex", "claude", "workbuddy", "cursor"})
        self.assertEqual({card["role"] for card in cards}, {"main", "child", "unknown"})
        self.assertEqual({card["state"] for card in cards},
                         {"active", "waiting_user", "waiting_approval", "error", "idle", "unknown"})
        self.assertTrue(any(card["context_percent"] is None for card in cards))
        self.assertTrue(any(card["context_percent"] is not None and not card["context_stale"] for card in cards))
        self.assertTrue(any(card["context_stale"] for card in cards))
        self.assertTrue(all(not card["jump"]["available"] for card in cards))
        status_only = next(card for card in cards if card["source_id"] == "demo-status-only")
        self.assertEqual(status_only["coverage"], "status_only")
        self.assertIsNone(status_only["usage"]["observed_total"])
        self.assertTrue(status_only["working_directory"])
        status, stats = self.dispatch("/api/stats?role=all&period=month&date=" + day_of(now()))
        self.assertEqual(status, 200)
        self.assertGreater(stats["totals"]["total"], 0)
        # The first day of a month legitimately has one populated month bucket.
        history_days = self.demo.store.db.execute("SELECT COUNT(DISTINCT day) FROM usage").fetchone()[0]
        self.assertGreaterEqual(history_days, 30)

    def test_refresh_keeps_active_sample_live_without_making_old_context_fresh(self):
        future = now() + 600
        with patch("agentwatch.demo.now", return_value=future), patch("agentwatch.store.now", return_value=future):
            self.demo.collector.refresh()
            snapshot = self.demo.store.snapshot({"role": "all"}, {}, future)
        active = next(card for card in snapshot["sessions"] if card["id"] == "codex:demo-design")
        old = next(card for card in snapshot["sessions"] if card["id"] == "codex:demo-error")
        self.assertEqual(active["state"], "active")
        self.assertFalse(active["stale"])
        self.assertTrue(old["context_stale"])

    def test_child_samples_include_nested_waiting_stale_and_offline_evidence(self):
        status, snapshot = self.dispatch("/api/snapshot?role=all")
        self.assertEqual(status, 200)
        cards = {card["id"]: card for card in snapshot["sessions"]}
        self.assertEqual(cards["codex:demo-verification"]["parent_id"], "codex:demo-implementation")
        self.assertEqual(cards["codex:demo-implementation"]["parent_id"], "codex:demo-design")
        self.assertEqual(cards["codex:demo-implementation"]["state"], "active")
        self.assertEqual(cards["codex:demo-implementation"]["liveness"], "unknown")
        self.assertEqual(cards["codex:demo-implementation"]["state_source"], "event")
        self.assertEqual(cards["codex:demo-audit"]["state"], "waiting_approval")
        self.assertTrue(cards["codex:demo-audit"]["stale"])
        self.assertEqual(cards["codex:demo-copy"]["state"], "idle")
        self.assertEqual(cards["codex:demo-copy"]["liveness"], "offline")
        self.assertEqual(cards["codex:demo-offline"]["state"], "active")
        self.assertEqual(cards["codex:demo-offline"]["liveness"], "offline")
        self.assertEqual(snapshot["counts"]["active"], 4)
        self.assertTrue(all(card["parent_id"] is None for card in cards.values() if card["role"] == "unknown"))
        self.assertFalse(any(card["parent_id"] == "codex:demo-complete" for card in cards.values()))
        for sid in ["codex:demo-implementation", "codex:demo-audit", "codex:demo-copy",
                    "codex:demo-offline", "codex:demo-verification"]:
            self.assertIsNone(cards[sid]["usage"]["observed_total"])
        future = now() + 600
        with patch("agentwatch.demo.now", return_value=future), patch("agentwatch.store.now", return_value=future):
            self.demo.collector.refresh()
            snapshot = self.demo.store.snapshot({"role": "all"}, {}, future)
        audit = next(card for card in snapshot["sessions"] if card["id"] == "codex:demo-audit")
        self.assertTrue(audit["stale"])
        self.assertEqual(audit["state_age"], 900)

    def test_year_samples_span_elapsed_months_without_future_or_extra_today_usage(self):
        current = datetime(2026, 10, 15, 16, tzinfo=TZ).timestamp()
        with patch("agentwatch.demo.now", return_value=current), patch("agentwatch.store.now", return_value=current):
            with DemoEnvironment() as demo:
                rows = demo.store.db.execute("SELECT at,day,kind FROM usage").fetchall()
                self.assertEqual({row["day"][:7] for row in rows}, {f"2026-{month:02}" for month in range(1, 11)})
                self.assertTrue(all(row["at"] <= current and row["kind"] == "demo" for row in rows))
                today = demo.store.stats({"role": "all", "period": "day", "date": "2026-10-15"}, current)
                # The six original usage samples retain their original current-day totals.
                self.assertEqual(today["totals"]["total"], 9780)
                year = demo.store.stats({"role": "all", "period": "year", "date": "2026-10-15"}, current)
                self.assertGreater(year["totals"]["total"], today["totals"]["total"])
                self.assertEqual(set(year["by_tool"]), {"codex", "claude", "workbuddy"})

    def test_real_navigation_and_claude_configuration_routes_are_forbidden(self):
        with patch("agentwatch.server.jump", side_effect=AssertionError("real navigation")), \
             patch("agentwatch.configuration.ClaudeConfiguration.install", side_effect=AssertionError("real settings")):
            for path in ["/api/jump", "/api/claude/preview", "/api/claude/enable", "/api/claude/restore",
                         "/api/codex/preview", "/api/codex/enable", "/api/codex/restore",
                         "/api/workbuddy/preview", "/api/workbuddy/enable", "/api/workbuddy/restore",
                         "/api/cursor/preview", "/api/cursor/enable", "/api/cursor/restore", "/api/cursor/subscription"]:
                status, payload = self.dispatch(path, {"id": "codex:demo-design", "kind": "codex"})
                self.assertEqual(status, 403)
                self.assertTrue(payload["demo"])
                self.assertIn("演示", payload["error"])

    def test_alias_and_refresh_only_change_temporary_samples(self):
        status, payload = self.dispatch("/api/alias", {"id": "codex:demo-design", "alias": "合作者测试名称"})
        self.assertEqual((status, payload["saved"]), (200, True))
        self.assertEqual(self.dispatch("/api/rescan", {})[0], 200)
        status, snapshot = self.dispatch("/api/snapshot?role=all")
        card = next(card for card in snapshot["sessions"] if card["id"] == "codex:demo-design")
        self.assertEqual(card["alias"], "合作者测试名称")

    def test_subscription_endpoint_is_account_scoped_and_demo_only(self):
        status, payload = self.dispatch("/api/subscriptions?tool=workbuddy&role=main&project=unrelated")
        self.assertEqual(status, 200)
        self.assertTrue(payload["demo"])
        self.assertEqual({p["tool"] for p in payload["platforms"]}, {"codex", "claude", "workbuddy", "cursor"})
        self.assertTrue(all(p["limits"] for p in payload["platforms"] if p["tool"] != "cursor"))
        cursor = next(p for p in payload["platforms"] if p["tool"] == "cursor")
        self.assertEqual((cursor["status"], cursor["limits"]), ("unavailable", []))


class DemoLifecycleTests(unittest.TestCase):
    def test_context_exit_removes_database_and_source_roots(self):
        with DemoEnvironment() as demo:
            root = demo.root
            self.assertTrue(demo.store.path.exists())
        self.assertFalse(root.exists())

    def test_cli_demo_ignores_real_paths_and_runs_requested_port_and_browser_mode(self):
        observed = {}
        class FakeServer:
            def __init__(self, port, store, collector, config, **options):
                self.server_port = port
                observed.update(port=port, root=collector.codex_home.parents[1], store=store,
                                collector=collector, config=config)
            def serve_forever(self, **options):
                observed["tools"] = {row["tool"] for row in observed["store"].all_sessions()}
            def server_close(self):
                observed["closed"] = True

        with tempfile.TemporaryDirectory() as directory:
            forbidden = Path(directory) / "must-not-create"
            argv = ["agentwatch", "--demo", "--port", "8767", "--open", "--data-dir", str(forbidden),
                    "--codex-home", str(forbidden), "--claude-home", str(forbidden),
                    "--desktop-home", str(forbidden), "--workbuddy-home", str(forbidden), "--cursor-home", str(forbidden)]
            with patch("sys.argv", argv), patch.object(entrypoint, "AppServer", FakeServer), \
                 patch.object(entrypoint, "Collector", side_effect=AssertionError("production collector")), \
                 patch.object(entrypoint, "ClaudeConfiguration", side_effect=AssertionError("production config")), \
                 patch.object(entrypoint, "private_dir", side_effect=AssertionError("production data")), \
                 patch.object(entrypoint.signal, "signal"), patch.object(entrypoint.threading, "Timer") as timer, \
                 redirect_stdout(io.StringIO()) as output:
                self.assertEqual(entrypoint.main(), 0)
            self.assertFalse(forbidden.exists())
        self.assertEqual(observed["port"], 8767)
        self.assertEqual(observed["tools"], {"codex", "claude", "workbuddy", "cursor"})
        self.assertTrue(observed["closed"])
        self.assertFalse(observed["root"].exists())
        timer.assert_called_once()
        timer.return_value.start.assert_called_once()
        self.assertIn("演示模式", output.getvalue())


if __name__ == "__main__":
    unittest.main()
