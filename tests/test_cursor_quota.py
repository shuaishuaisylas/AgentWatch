from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from agentwatch.cursor_quota import SOURCE, cursor_platform, record_cursor_observation
from agentwatch.store import Store
from agentwatch.subscriptions import Subscriptions, record_codex


AT = datetime(2026, 10, 16, 4, 0, tzinfo=timezone.utc).timestamp()
SECRET = "PRIVATE_CURSOR_COOKIE_EMAIL_PAGE_BODY_MUST_NOT_PERSIST"


class CursorQuotaTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cursor-quota-")
        self.store = Store(Path(self.temporary.name) / "watch.sqlite")

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def window(self, key="cursor_models", used=23, period="monthly", **extra):
        return {"key": key, "used_percent": used, "period": period, **extra}

    def record(self, windows=None, captured=AT, current=None):
        return record_cursor_observation(self.store, {"captured_at": captured,
            "limits": [self.window()] if windows is None else windows}, current=captured if current is None else current)

    def platform(self, at=AT):
        return next(platform for platform in Subscriptions(self.store).snapshot(at)["platforms"] if platform["tool"] == "cursor")

    def stored(self):
        return [tuple(row) for row in self.store.db.execute("SELECT * FROM subscription_snapshots ORDER BY tool,bucket")]

    def test_missing_data_stays_unknown_and_capture_is_not_source_updated_time(self):
        missing = self.platform()
        self.assertEqual((missing["status"], missing["limits"], missing["updated_at"], missing["captured_at"]),
                         ("unavailable", [], None, None))
        result = self.record()
        self.assertEqual(result, {"recorded": True, "captured_at": AT})
        platform = self.platform(AT + 4)
        self.assertEqual(platform["captured_at"], AT)
        self.assertIsNone(platform["updated_at"])
        self.assertIsNone(platform["source_updated_at"])
        window = platform["limits"][0]
        self.assertEqual((window["observed_at"], window["source"], window["display_precision"]),
                         (AT, SOURCE, "integer_percent"))
        self.assertEqual(window["captured_at"], AT)
        self.assertIsNone(window["source_updated_at"])
        self.assertEqual(platform["captured_at"], self.platform(AT + 100)["captured_at"])

    def test_independent_pools_are_not_added_and_actual_zero_remains_known(self):
        self.record([self.window(used=0), self.window("other_models", used=67),
                     self.window("grok_bot", used=100, period="weekly")])
        platform = self.platform()
        values = {window["bucket"]: window for window in platform["limits"]}
        self.assertEqual(set(values), {"cursor_models", "other_models", "grok_bot"})
        self.assertEqual((values["cursor_models"]["used"], values["cursor_models"]["remaining"]), (0, 100))
        self.assertEqual(values["other_models"]["remaining"], 33)
        self.assertEqual(values["grok_bot"]["remaining"], 0)
        self.assertNotIn("remaining_percent", platform)
        self.assertEqual(values["cursor_models"]["label"], "Cursor Models · 月度")
        self.assertEqual(values["grok_bot"]["label"], "Grok Bot · 每周")
        self.assertIsNone(values["cursor_models"]["window_minutes"])
        self.assertEqual(values["grok_bot"]["window_minutes"], 10080)
        self.assertIsNone(values["grok_bot"]["resets_at"])

    def test_new_observation_replaces_missing_pools_instead_of_retaining_or_zeroing(self):
        self.record([self.window(), self.window("other_models", 44)])
        self.record([self.window(used=24)], captured=AT + 1)
        windows = self.platform(AT + 1)["limits"]
        self.assertEqual([(window["bucket"], window["used"]) for window in windows], [("cursor_models", 24)])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM subscription_snapshots WHERE tool='cursor'").fetchone()[0], 1)

    def test_same_pool_can_have_distinct_monthly_weekly_windows(self):
        self.record([self.window(used=19), self.window(used=31, period="weekly")])
        windows = self.platform()["limits"]
        self.assertEqual({(item["bucket"], item["key"]) for item in windows},
                         {("cursor_models", "monthly"), ("cursor_models", "weekly")})

    def test_total_is_exclusive_with_included_subpools_but_grok_is_independent(self):
        self.record([self.window("total", 12), self.window("grok_bot", 29, "weekly")])
        saved = self.stored()
        for pool in ["cursor_models", "other_models"]:
            with self.assertRaises(ValueError):
                self.record([self.window("total"), self.window(pool)], captured=AT + 1)
            self.assertEqual(self.stored(), saved)
        self.assertEqual({window["bucket"] for window in self.platform()["limits"]}, {"total", "grok_bot"})

    def test_date_only_reset_has_conservative_validity_and_never_exact_reset_time(self):
        self.record([self.window(reset_on="2026-10-17", reset_label="Oct 17")])
        boundary = datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc).timestamp()
        current = self.platform(boundary - 1)["limits"][0]
        self.assertEqual((current["reset_on"], current["reset_label"], current["valid_until"]),
                         ("2026-10-17", "Oct 17", boundary))
        self.assertEqual(current["valid_until_basis"], "reset_date_earliest_utc_plus_14")
        self.assertEqual(current["reset_precision"], "day")
        self.assertIsNone(current["resets_at"])
        expired = self.platform(boundary)["limits"][0]
        self.assertEqual(expired["status"], "expired")
        self.assertIsNone(expired["remaining"])
        self.assertIsNone(expired["remaining_percent"])
        self.assertEqual(expired["used"], 23)
        self.assertIsNone(self.platform(boundary + 86400)["limits"][0]["remaining"])

    def test_reset_label_only_does_not_invent_year_or_reset_timestamp(self):
        self.record([self.window(reset_label="Nov 16")])
        window = self.platform()["limits"][0]
        self.assertEqual(window["reset_label"], "Nov 16")
        self.assertIsNone(window["reset_on"])
        self.assertIsNone(window["valid_until"])
        self.assertIsNone(window["resets_at"])

    def test_full_year_labels_require_explicit_reset_date_in_every_supported_format(self):
        self.record()
        previous = self.stored()
        full_dates = ["2026-10-17", "2026/10/17", "2026年10月17日", "Oct 17 2026",
                      "October 17, 2026", "17 Oct 2026"]
        for label in full_dates:
            with self.subTest(label=label), self.assertRaises(ValueError):
                self.record([self.window(reset_label=label)], captured=AT + 1)
            self.assertEqual(self.stored(), previous)
        for index, label in enumerate(["Oct 17", "October 17", "17 Oct", "10月17日", "10/17"], 1):
            self.record([self.window(reset_label=label)], captured=AT + index)
            window = self.platform(AT + index)["limits"][0]
            self.assertEqual(window["reset_label"], label)
            self.assertIsNone(window["reset_on"])
            self.assertIsNone(window["valid_until"])

    def test_safe_date_labels_are_preserved_and_mismatches_rejected(self):
        labels = ["2026-11-16", "2026/11/16", "2026年11月16日", "11月16日", "Nov 16",
                  "November 16, 2026", "16 Nov 2026", "11/16"]
        for index, label in enumerate(labels):
            captured = AT + index
            self.record([self.window(reset_on="2026-11-16", reset_label=label)], captured=captured)
            self.assertEqual(self.platform(captured)["limits"][0]["reset_label"], label)
        previous = self.stored()
        for label in ["Nov 17", "2025-11-16", "Feb 30", "Nov 16\n", SECRET, "https://cursor.com", "user@example.com"]:
            with self.assertRaises(ValueError):
                self.record([self.window(reset_on="2026-11-16", reset_label=label)], captured=AT + 100)
            self.assertEqual(self.stored(), previous)
        with self.assertRaises(ValueError):
            self.record([self.window(reset_label="Nov 16, 9999")], captured=AT + 100)
        self.assertEqual(self.stored(), previous)
        with self.assertRaises(ValueError):
            self.record([self.window(reset_label="0000-11-16")], captured=AT + 100)
        self.assertEqual(self.stored(), previous)

    def test_stale_observation_keeps_last_values_and_original_capture_time(self):
        self.record()
        self.assertEqual(self.platform(AT + 900)["status"], "available")
        stale = self.platform(AT + 901)
        self.assertEqual((stale["status"], stale["limits"][0]["status"], stale["limits"][0]["remaining"]),
                         ("stale", "stale", 77))
        self.assertEqual(stale["captured_at"], AT)
        self.assertIsNone(stale["updated_at"])

    def test_old_conflicting_and_identical_imports_do_not_replace_newer_observation(self):
        self.record()
        before = self.stored()
        self.assertEqual(self.record(), {"recorded": False, "captured_at": AT})
        self.assertEqual(self.stored(), before)
        for captured in [AT - 1, AT]:
            with self.assertRaises(ValueError):
                self.record([self.window(used=99)], captured=captured, current=AT)
            self.assertEqual(self.stored(), before)

    def test_invalid_time_window_or_extra_fields_never_overwrite_previous_batch(self):
        self.record()
        previous = self.stored()
        invalid = [None, [], {}, {"captured_at": AT + 1, "limits": []},
                   {"captured_at": AT + 1, "limits": [self.window()] * 9},
                   {"captured_at": AT + 1, "limits": [self.window(), self.window()]},
                   {"captured_at": AT + 1, "limits": [self.window()], "url": SECRET},
                   {"captured_at": AT + 1, "limits": [self.window(email=SECRET)]},
                   {"captured_at": AT + 1, "limits": [self.window(label=SECRET)]},
                   {"captured_at": AT + 1, "limits": [self.window(source=SECRET)]},
                   {"captured_at": AT + 1, "limits": [{"key": "cursor_models", "period": "monthly"}]}]
        for captured in [True, "123", -1, 0, AT + 3, float("nan"), float("inf"), 10**400]:
            invalid.append({"captured_at": captured, "limits": [self.window()]})
        for value in [True, None, "12", -1, 101, float("nan"), float("inf"), 10**400]:
            invalid.append({"captured_at": AT + 1, "limits": [self.window(used=value)]})
        for item in [self.window(key="unknown"), self.window(period="yearly"), self.window(reset_on="2026-2-01"),
                     self.window(reset_on="2026-02-30"), self.window(reset_on="2036-01-01"), self.window(reset_on=SECRET)]:
            invalid.append({"captured_at": AT + 1, "limits": [self.window(), item]})
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                record_cursor_observation(self.store, raw, current=AT + 2)
            self.assertEqual(self.stored(), previous)
        self.assertNotIn(SECRET, "\n".join(self.store.db.iterdump()))
        self.assertNotIn(SECRET, json.dumps(self.platform(AT + 2)))

    def test_replacement_is_atomic_even_if_one_pool_insert_fails(self):
        self.record([self.window(used=18), self.window("other_models", 26)])
        before = self.stored()
        self.store.db.execute("""CREATE TRIGGER artificial_quota_failure BEFORE INSERT ON subscription_snapshots
            WHEN NEW.tool='cursor' AND NEW.bucket='grok_bot' BEGIN SELECT RAISE(ABORT,'artificial failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.record([self.window(used=90), self.window("grok_bot", 12, "weekly")], captured=AT + 1)
        self.assertEqual(self.stored(), before)

    def test_cursor_import_leaves_other_platforms_and_session_usage_unchanged(self):
        record_codex(self.store, {"primary": {"used_percent": 27, "window_minutes": 10080}}, AT)
        self.store.db.commit()
        self.record()
        snapshot = Subscriptions(self.store).snapshot(AT)
        codex = next(platform for platform in snapshot["platforms"] if platform["tool"] == "codex")
        self.assertEqual((codex["limits"][0]["remaining"], codex["updated_at"]), (73, AT))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM usage").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)

    def test_corrupt_stored_payload_is_not_returned_or_reinterpreted_as_zero(self):
        self.store.db.execute("INSERT INTO subscription_snapshots VALUES(?,?,?,?,?)", ("cursor", "cursor_models", SOURCE, AT,
            json.dumps([{"key": "monthly", "used": 12, "reset_label": SECRET}])))
        platform = self.platform()
        self.assertEqual((platform["status"], platform["limits"]), ("error", []))
        self.assertIsNone(platform["updated_at"])
        self.assertNotIn(SECRET, json.dumps(platform))
        self.assertEqual(cursor_platform([], current=AT)["status"], "unavailable")

    def test_artificial_demo_rows_keep_explicit_demo_source(self):
        self.record()
        rows = [dict(row) for row in self.store.db.execute("SELECT * FROM subscription_snapshots")]
        result = cursor_platform(rows, current=AT, demo=True)
        self.assertEqual((result["source"], result["limits"][0]["source"]), ("demo", "demo"))
        self.assertIn("人工演示", result["notice"])
        rows[0]["source"] = "demo"
        self.assertEqual(cursor_platform(rows, current=AT, demo=True)["status"], "available")
        self.assertEqual(cursor_platform(rows, current=AT, demo=False)["status"], "error")

    def test_mixed_capture_rows_never_mix_older_pools_into_latest_observation(self):
        self.record([self.window(used=19), self.window("other_models", 51)])
        self.store.db.execute("UPDATE subscription_snapshots SET at=? WHERE tool='cursor' AND bucket='cursor_models'", (AT + 1,))
        platform = self.platform(AT + 1)
        self.assertEqual(platform["captured_at"], AT + 1)
        self.assertEqual([(item["bucket"], item["remaining"]) for item in platform["limits"]], [("cursor_models", 81)])
        self.assertFalse(any(item["bucket"] == "other_models" for item in platform["limits"]))


if __name__ == "__main__":
    unittest.main()
