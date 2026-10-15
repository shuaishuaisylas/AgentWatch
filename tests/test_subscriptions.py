from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from agentwatch.bridge import sanitize
from agentwatch.collector import RecordParser
from agentwatch.common import timestamp
from agentwatch.demo import DemoEnvironment
from agentwatch.store import Store
from agentwatch.subscriptions import (NoRedirect, Subscriptions, backfill_codex, claude_windows,
                                      codex_windows, fetch_workbuddy, record_claude, record_codex)

AT = timestamp("2026-10-15T10:00:00+08:00")
SECRET = "PRIVATE_QUOTA_CREDENTIAL_PROMPT_92741"


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "watch.sqlite")
        self.subs = Subscriptions(self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def codex(self, used=21.25, at=AT, bucket="codex", reset=AT+100000):
        record_codex(self.store, {"limit_id": bucket, "primary": {
            "used_percent": used, "window_minutes": 10080, "resets_at": reset}}, at)

    def platform(self, tool, at=AT, subs=None):
        return next(p for p in (subs or self.subs).snapshot(at)["platforms"] if p["tool"] == tool)

    def test_codex_window_duration_defines_label_and_decimal_remaining(self):
        self.codex()
        window = self.platform("codex")["limits"][0]
        self.assertEqual((window["label"], window["remaining"], window["window_minutes"]), ("每周额度", 78.75, 10080))
        self.assertEqual(window["status"], "available")
        windows = codex_windows({"primary": {"used_percent": 5, "window_minutes": 300},
                                "secondary": {"used_percent": 6, "window_minutes": 10080}})
        self.assertEqual([w["label"] for w in windows], ["每周额度", "5 小时额度"])

    def test_snapshots_are_latest_not_added_and_buckets_remain_separate(self):
        self.codex(10, AT)
        self.codex(25, AT+10)
        self.codex(90, AT-1)
        self.codex(60, AT+5, "codex-other")
        limits = self.platform("codex", AT+11)["limits"]
        self.assertEqual({w["bucket"]: w["remaining"] for w in limits}, {"codex": 75, "codex-other": 40})

    def test_invalid_or_missing_percent_does_not_become_zero(self):
        for invalid in [None, True, "20", -1, 101, float("nan"), float("inf")]:
            self.assertEqual(claude_windows({"seven_day": {"used_percentage": invalid}}), [])
        for used, remaining in [(0, 100), (100, 0)]:
            self.codex(used)
            self.assertEqual(self.platform("codex")["limits"][0]["remaining"], remaining)

    def test_expired_window_is_unknown_and_old_snapshot_is_explicit(self):
        self.codex(reset=AT+100)
        limit = self.platform("codex", AT+100)["limits"][0]
        self.assertEqual(limit["status"], "expired")
        self.assertIsNone(limit["remaining"])
        self.assertIsNone(limit["remaining_percent"])
        self.codex(at=AT+101, reset=AT+100000)
        old = self.platform("codex", AT+1002)
        self.assertEqual(old["status"], "stale")
        self.assertEqual(old["limits"][0]["remaining"], 78.75)
        self.assertEqual(old["updated_at"], AT+101)

    def test_claude_statusline_whitelists_only_actual_windows(self):
        raw = {"session_id": "s", "prompt": SECRET, "access_token": SECRET,
               "rate_limits": {"seven_day": {"used_percentage": 40.2, "resets_at": AT+10000, "secret": SECRET},
                               "extra_usage": {"secret": SECRET}}}
        clean = sanitize(raw, statusline=True, at=AT)
        self.assertNotIn(SECRET, json.dumps(clean))
        self.assertEqual(set(clean["rate_limits"]), {"seven_day"})
        parser = RecordParser(self.store)
        parser.hook(clean, {})
        windows = self.platform("claude")["limits"]
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]["remaining"], 59.8)
        self.assertNotIn(SECRET, "\n".join(self.store.db.iterdump()))

    def test_missing_claude_window_is_not_retained_or_reported_as_unused(self):
        record_claude(self.store, {"five_hour": {"used_percentage": 10, "resets_at": AT+1000}}, AT)
        record_claude(self.store, {"seven_day": {"used_percentage": 40, "resets_at": AT+10000}}, AT+1)
        self.assertEqual([w["key"] for w in self.platform("claude", AT+1)["limits"]], ["seven_day"])

    def test_quota_is_not_model_token_usage_or_project_filtered(self):
        parser = RecordParser(self.store)
        cursor = {}
        parser.codex({"type": "session_meta", "payload": {"id": "s", "source": "cli"}}, cursor)
        parser.codex({"type": "event_msg", "timestamp": AT, "payload": {"type": "token_count", "info": None,
                      "rate_limits": {"primary": {"used_percent": 33, "window_minutes": 10080}}}}, cursor)
        self.assertEqual(self.platform("codex")["limits"][0]["remaining"], 67)
        self.assertIsNone(self.store.stats({"role": "all", "date": "2026-10-15"}, AT)["totals"]["total"])

    def test_existing_file_backfill_is_bounded_and_does_not_replay_usage(self):
        path = self.root / "s.jsonl"
        valid = {"type": "event_msg", "timestamp": AT, "payload": {"type": "token_count",
                 "rate_limits": {"primary": {"used_percent": 12, "window_minutes": 10080}}}}
        path.write_text(json.dumps(["rate_limits"]) + "\n" + json.dumps(valid) + "\n")
        backfill_codex(self.store, {str(path): ("codex", None)})
        self.assertEqual(self.platform("codex")["limits"][0]["remaining"], 88)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM usage").fetchone()[0], 0)

    def test_workbuddy_fixed_endpoint_numeric_only_and_no_redirect(self):
        response = io.BytesIO(json.dumps({"total_capacity_size": 3609, "total_capacity_used": 285,
                                        "token": SECRET, "email": SECRET}).encode())
        opener = Mock()
        opener.open.return_value = response
        with patch("agentwatch.subscriptions.urllib.request.build_opener", return_value=opener):
            windows = fetch_workbuddy(SECRET)
        request = opener.open.call_args[0][0]
        self.assertEqual(request.full_url, "https://www.workbuddy.cn/openapi/v2/credit")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + SECRET)
        self.assertEqual(windows[0]["remaining"], 3324)
        self.assertNotIn(SECRET, json.dumps(windows))
        self.assertIsNone(NoRedirect().redirect_request(None, None, None, None, None, None))

    def test_workbuddy_does_not_reuse_another_credential_or_persist_balance(self):
        windows = [{"key": "credits", "label": "积分", "unit": "credits", "limit": 100, "used": 20,
                    "remaining": 80, "remaining_percent": 80, "window_minutes": None, "resets_at": None}]
        client = Subscriptions(self.store, workbuddy_token=SECRET, fetcher=lambda token: windows)
        client.refresh_workbuddy()
        self.assertEqual(self.platform("workbuddy", subs=client)["limits"][0]["remaining"], 80)
        next_account = Subscriptions(self.store, workbuddy_token="different-token")
        self.assertEqual(self.platform("workbuddy", subs=next_account)["limits"], [])
        self.assertEqual(self.platform("workbuddy")["status"], "auth_required")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM subscription_snapshots WHERE tool='workbuddy'").fetchone()[0], 0)
        self.assertNotIn(SECRET, json.dumps(client.snapshot()))
        self.assertNotIn(SECRET, "\n".join(self.store.db.iterdump()))

    def test_workbuddy_errors_are_generic_and_do_not_turn_into_zero_balance(self):
        for code in [401, 403, 500]:
            error = urllib.error.HTTPError(SECRET, code, SECRET, {}, None)
            client = Subscriptions(self.store, workbuddy_token=SECRET, fetcher=Mock(side_effect=error))
            client.refresh_workbuddy()
            platform = self.platform("workbuddy", subs=client)
            self.assertEqual(platform["status"], "auth_required" if code != 500 else "error")
            self.assertEqual(platform["limits"], [])
            self.assertNotIn(SECRET, json.dumps(platform))

    def test_workbuddy_missing_or_malformed_credit_fields_rejected(self):
        for data in [{}, {"total_capacity_size": True, "total_capacity_used": 2},
                     {"total_capacity_size": 10, "total_capacity_used": 1.2}, []]:
            opener = Mock()
            opener.open.return_value = io.BytesIO(json.dumps(data).encode())
            with patch("agentwatch.subscriptions.urllib.request.build_opener", return_value=opener):
                with self.assertRaises(ValueError):
                    fetch_workbuddy(SECRET)

    def test_demo_ignores_credential_and_never_contacts_credit_service(self):
        with DemoEnvironment() as demo:
            fetcher = Mock(side_effect=AssertionError("network"))
            client = Subscriptions(demo.store, demo=True, workbuddy_token=SECRET, fetcher=fetcher)
            client.start()
            client.refresh_workbuddy()
            client.stop()
            result = client.snapshot()
            self.assertTrue(result["demo"])
            self.assertTrue(all(p["limits"] for p in result["platforms"]))
            fetcher.assert_not_called()


if __name__ == "__main__":
    unittest.main()
