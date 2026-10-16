"""Explicit observations of Cursor's signed-in spending page, never credentials.

This imports a small, complete observation rather than a URL or account session.
The capture time is not the service's update time. Date-only reset labels impose
a conservative validity boundary and are never converted to an exact reset time.
"""
from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, time, timedelta, timezone

from .common import now


SOURCE = "cursor_dashboard_ui"
STALE_SECONDS = 900
POOL_LABELS = {"cursor_models": "Cursor Models", "other_models": "Other Models",
               "grok_bot": "Grok Bot", "total": "Included 总额度"}
PERIOD_LABELS = {"monthly": "月度", "weekly": "每周"}
MONTHS = {name.lower(): index for index, names in enumerate([
    ("Jan", "January"), ("Feb", "February"), ("Mar", "March"), ("Apr", "April"),
    ("May", "May"), ("Jun", "June"), ("Jul", "July"), ("Aug", "August"),
    ("Sep", "September"), ("Oct", "October"), ("Nov", "November"), ("Dec", "December")], 1) for name in names}
NOTICE = ("来自 Cursor 官网的页面快照，不会自动刷新；百分比按页面显示精度，各额度池独立，不可相加。"
          "未取得的额度保持未知，页面读取时间不是官方更新时间。"
          "重置日期仅到日，进入可能重置范围后保守显示未知，等待重新读取。")


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and -1e15 < value < 1e15 and math.isfinite(value)


def _clock(value):
    value = now() if value is None else value
    if not _number(value) or not 0 < value <= 253402300799:
        raise ValueError("观察时间无效")
    return value


def _date_label(value, reset_on, captured_at):
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 40 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("重置日期显示无效")
    value = value.strip()
    year = month = day = None
    match = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", value)
    if match:
        year, month, day = map(int, match.groups())
    if month is None:
        match = re.fullmatch(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日?", value)
        if match:
            year = int(match[1]) if match[1] else None
            month, day = int(match[2]), int(match[3])
    if month is None:
        match = re.fullmatch(r"([A-Za-z]+) (\d{1,2})(?:,? (\d{4}))?", value)
        if match and match[1].lower() in MONTHS:
            month, day = MONTHS[match[1].lower()], int(match[2])
            year = int(match[3]) if match[3] else None
    if month is None:
        match = re.fullmatch(r"(\d{1,2}) ([A-Za-z]+)(?: (\d{4}))?", value)
        if match and match[2].lower() in MONTHS:
            month, day = MONTHS[match[2].lower()], int(match[1])
            year = int(match[3]) if match[3] else None
    if month is None:
        match = re.fullmatch(r"(\d{1,2})/(\d{1,2})", value)
        if match:
            month, day = map(int, match.groups())
    try:
        parsed = date(year if year is not None else 2000, month, day)
    except (ValueError, TypeError):
        raise ValueError("重置日期显示无效")
    if year is not None and reset_on is None:
        raise ValueError("完整重置日期显示必须同时提供重置日期")
    if reset_on and (parsed.month != reset_on.month or parsed.day != reset_on.day or (year and parsed.year != reset_on.year)):
        raise ValueError("重置日期与显示不一致")
    if year and abs((parsed - datetime.fromtimestamp(captured_at, timezone.utc).date()).days) > 366:
        raise ValueError("重置日期显示超出观察范围")
    return value


def _window(item, captured_at):
    if not isinstance(item, dict) or set(item) - {"key", "used_percent", "period", "reset_on", "reset_label"}:
        raise ValueError("额度窗口字段无效")
    pool, period, used = item.get("key"), item.get("period"), item.get("used_percent")
    if not isinstance(pool, str) or pool not in POOL_LABELS or not isinstance(period, str) or period not in PERIOD_LABELS:
        raise ValueError("额度池或周期无效")
    if not _number(used) or not 0 <= used <= 100:
        raise ValueError("额度百分比无效")
    reset_raw, reset = item.get("reset_on"), None
    if reset_raw is not None:
        if not isinstance(reset_raw, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", reset_raw):
            raise ValueError("重置日期无效")
        try:
            reset = date.fromisoformat(reset_raw)
            captured_date = datetime.fromtimestamp(captured_at, timezone.utc).date()
        except (ValueError, OverflowError, OSError):
            raise ValueError("重置日期无效")
        if abs((reset - captured_date).days) > 366:
            raise ValueError("重置日期超出观察范围")
    label = _date_label(item.get("reset_label"), reset, captured_at)
    # UTC+14 is the world's earliest civil start of this date. This is a
    # conservative stop-display boundary, explicitly not a known reset instant.
    valid_until = datetime.combine(reset, time.min, timezone(timedelta(hours=14))).timestamp() if reset else None
    remaining = round(100 - used, 4)
    return pool, {"key": period, "label": POOL_LABELS[pool] + " · " + PERIOD_LABELS[period],
                  "unit": "percent", "limit": 100, "used": used, "remaining": remaining,
                  "remaining_percent": remaining, "window_minutes": 10080 if period == "weekly" else None,
                  "resets_at": None, "reset_on": reset_raw, "reset_label": label,
                  "valid_until": valid_until, "valid_until_basis": "reset_date_earliest_utc_plus_14" if reset else None,
                  "reset_precision": "day" if reset else None,
                  "display_precision": "integer_percent", "source_updated_at": None}


def record_cursor_observation(store, raw, current=None):
    """Validate completely, then replace every Cursor pool in one transaction.

    No source text, URLs, account identity, cookies or authentication information
    are accepted. Same-time identical imports are idempotent; older or conflicting
    observations are rejected without deleting any currently stored pools.
    """
    current = _clock(current)
    if not isinstance(raw, dict) or set(raw) != {"captured_at", "limits"}:
        raise ValueError("观察字段无效")
    captured = raw["captured_at"]
    if not _number(captured) or not 0 < captured <= current:
        raise ValueError("观察时间无效")
    limits = raw["limits"]
    if not isinstance(limits, list) or not 1 <= len(limits) <= 8:
        raise ValueError("额度窗口数量无效")
    grouped, identities = {}, set()
    for item in limits:
        pool, window = _window(item, captured)
        identity = (pool, window["key"])
        if identity in identities:
            raise ValueError("额度窗口重复")
        identities.add(identity)
        grouped.setdefault(pool, []).append(window)
    if "total" in grouped and any(pool in grouped for pool in ["cursor_models", "other_models"]):
        raise ValueError("总额度与分池不能混用")
    encoded = {pool: json.dumps(sorted(windows, key=lambda item: item["key"]), ensure_ascii=False,
                                sort_keys=True, allow_nan=False) for pool, windows in grouped.items()}
    with store.lock, store.db:
        previous = [dict(row) for row in store.db.execute("SELECT * FROM subscription_snapshots WHERE tool='cursor'")]
        latest = max((row["at"] for row in previous), default=None)
        if latest is not None and captured < latest:
            raise ValueError("观察早于已保存快照")
        if latest == captured:
            existing = {row["bucket"]: row["data"] for row in previous if row["source"] == SOURCE and row["at"] == captured}
            if len(existing) == len(previous) and existing == encoded:
                return {"recorded": False, "captured_at": captured}
            raise ValueError("同一观察时间存在不同快照")
        store.db.execute("DELETE FROM subscription_snapshots WHERE tool='cursor'")
        for pool, data in encoded.items():
            store.db.execute("INSERT INTO subscription_snapshots VALUES(?,?,?,?,?)", ("cursor", pool, SOURCE, captured, data))
    return {"recorded": True, "captured_at": captured}


def cursor_platform(rows, current=None, demo=False):
    """Build the existing subscription shape without inventing a service update."""
    current = _clock(current)
    result = {"tool": "cursor", "name": "Cursor", "status": "unavailable", "updated_at": None,
              "source_updated_at": None, "captured_at": None, "source": "demo" if demo else SOURCE,
              "display_precision": "integer_percent", "notice": "人工演示的 Cursor 额度快照；不代表真实账号。" if demo else NOTICE,
              "limits": [], "refreshing": False}
    candidates = [row for row in rows if row.get("tool") == "cursor"]
    if not candidates:
        return result
    try:
        if any(not _number(row.get("at")) or not 0 < row["at"] <= current for row in candidates):
            raise ValueError("invalid stored time")
        captured = max(row["at"] for row in candidates)
        recent = [row for row in candidates if row["at"] == captured]
        reconstructed = []
        for row in recent:
            if row["source"] not in ([SOURCE, "demo"] if demo else [SOURCE]) or row["bucket"] not in POOL_LABELS:
                raise ValueError("invalid stored source")
            windows = json.loads(row["data"])
            if not isinstance(windows, list) or not windows:
                raise ValueError("invalid stored windows")
            for saved in windows:
                if not isinstance(saved, dict):
                    raise ValueError("invalid stored window")
                reconstructed.append({"key": row["bucket"], "period": saved.get("key"),
                    "used_percent": saved.get("used"), "reset_on": saved.get("reset_on"), "reset_label": saved.get("reset_label")})
        if not 1 <= len(reconstructed) <= 8:
            raise ValueError("invalid stored size")
        pools = {item["key"] for item in reconstructed}
        if "total" in pools and pools.intersection({"cursor_models", "other_models"}):
            raise ValueError("incompatible stored pools")
        identities, limits = set(), []
        for raw in reconstructed:
            pool, window = _window(raw, captured)
            identity = (pool, window["key"])
            if identity in identities:
                raise ValueError("duplicate stored identity")
            identities.add(identity)
            expired = window["valid_until"] is not None and current >= window["valid_until"]
            window.update(bucket=pool, source="demo" if demo else SOURCE, observed_at=captured, captured_at=captured,
                          status="expired" if expired else "stale" if current - captured > STALE_SECONDS else "available")
            if expired:
                window["remaining"] = window["remaining_percent"] = None
            limits.append(window)
        result.update(captured_at=captured, limits=sorted(limits, key=lambda item: (item["bucket"], item["key"])),
                      status="available" if any(item["status"] == "available" for item in limits) else "stale")
    except (ValueError, TypeError, KeyError, OverflowError):
        result.update(status="error", notice="Cursor 页面观察快照暂不可读取；额度保持未知。")
    return result
