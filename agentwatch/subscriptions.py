"""Account quota snapshots, separate from session token statistics.

Only whitelisted numeric limits are stored. No credentials or account identity
are read from Agent authentication files, saved, or returned to the browser.
"""
from __future__ import annotations

import json
import math
import threading
import urllib.error
import urllib.request
from pathlib import Path

from .common import now, safe_id, timestamp

STALE_SECONDS = 900
WORKBUDDY_CREDIT_URL = "https://www.workbuddy.cn/openapi/v2/credit"


def numeric(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value < 1e15 and math.isfinite(value):
        return value
    return None


def percentage_window(key, label, raw, minutes, percent_key="used_percentage", reset_key="resets_at"):
    if not isinstance(raw, dict):
        return None
    used = numeric(raw.get(percent_key))
    if used is None or used > 100:
        return None
    return {"key": key, "label": label, "unit": "percent", "limit": 100, "used": used,
            "remaining": round(100 - used, 4), "remaining_percent": round(100 - used, 4),
            "window_minutes": minutes, "resets_at": timestamp(raw.get(reset_key))}


def claude_windows(raw):
    if not isinstance(raw, dict):
        return []
    result = []
    for key, label, minutes in [("seven_day", "每周额度", 10080), ("five_hour", "5 小时额度", 300)]:
        window = percentage_window(key, label, raw.get(key), minutes)
        if window:
            result.append(window)
    return result


def codex_windows(raw):
    if not isinstance(raw, dict):
        return []
    result = []
    for key in ["primary", "secondary"]:
        item = raw.get(key)
        if not isinstance(item, dict):
            continue
        minutes = numeric(item.get("window_minutes"))
        if minutes is not None and (minutes <= 0 or minutes > 525600):
            minutes = None
        label = "每周额度" if minutes == 10080 else "5 小时额度" if minutes == 300 else (
            str(minutes) + " 分钟额度" if minutes else "额度窗口（时长未报告）")
        window = percentage_window(key, label, item, minutes, "used_percent")
        if window:
            result.append(window)
    return sorted(result, key=lambda w: w["window_minutes"] or 0, reverse=True)


def save_snapshot(store, tool, bucket, source, at, windows):
    if not windows or not at:
        return
    store.db.execute("""INSERT INTO subscription_snapshots VALUES(?,?,?,?,?)
        ON CONFLICT(tool,bucket) DO UPDATE SET source=excluded.source,at=excluded.at,data=excluded.data
        WHERE excluded.at>=subscription_snapshots.at""", (tool, bucket, source, at, json.dumps(windows, allow_nan=False)))


def record_codex(store, raw, at):
    windows = codex_windows(raw)
    bucket = safe_id(raw.get("limit_id")) if isinstance(raw, dict) else None
    save_snapshot(store, "codex", bucket or "codex", "codex_record", at, windows)


def record_claude(store, raw, at):
    save_snapshot(store, "claude", "claude", "claude_statusline", at, claude_windows(raw))


def backfill_codex(store, files):
    """Bootstrap newly added quotas without replaying old token usage cursors."""
    paths = [Path(path) for path, (tool, _) in files.items() if tool == "codex"]
    def modified(path):
        try:
            return path.stat().st_mtime
        except OSError:
            return 0
    paths.sort(key=modified, reverse=True)
    for path in paths[:12]:
        try:
            with path.open("rb") as stream:
                size = stream.seek(0, 2)
                stream.seek(max(0, size - 512000))
                if size > 512000:
                    stream.readline()  # Discard the partial first line.
                lines = stream.read(512000).splitlines()
            for line in lines:
                if b'"rate_limits"' not in line:
                    continue
                try:
                    doc = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(doc, dict):
                    continue
                payload = doc.get("payload")
                if doc.get("type") == "event_msg" and isinstance(payload, dict) and payload.get("type") == "token_count":
                    record_codex(store, payload.get("rate_limits"), timestamp(doc.get("timestamp")))
        except OSError:
            continue


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # A bearer token must never follow an external redirect.


def fetch_workbuddy(token):
    if not isinstance(token, str) or not token or len(token) > 16000 or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError("invalid credential")
    request = urllib.request.Request(WORKBUDDY_CREDIT_URL, headers={
        "Authorization": "Bearer " + token, "Accept": "application/json", "User-Agent": "AgentWatch"})
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=8) as response:
        body = response.read(65537)
    if len(body) > 65536:
        raise ValueError("oversized response")
    raw = json.loads(body)
    if not isinstance(raw, dict):
        raise ValueError("invalid response")
    total, used = numeric(raw.get("total_capacity_size")), numeric(raw.get("total_capacity_used"))
    if total is None or used is None or int(total) != total or int(used) != used:
        raise ValueError("invalid credit counters")
    remaining = max(0, total - used)
    return [{"key": "credits", "label": "有效资源包积分合计", "unit": "credits", "limit": total, "used": used,
             "remaining": remaining, "remaining_percent": round(remaining / total * 100, 4) if total else None,
             "window_minutes": None, "resets_at": None}]


class Subscriptions:
    def __init__(self, store, demo=False, workbuddy_token="", fetcher=fetch_workbuddy):
        self.store, self.demo = store, demo
        self._token, self._fetcher = workbuddy_token, fetcher
        self._wake, self._stop = threading.Event(), threading.Event()
        self._thread = None
        self._last_attempt = 0
        self._error = None
        self._refreshing = False
        self._workbuddy_snapshot = None

    def start(self):
        if not self.demo and self._token:
            self._thread = threading.Thread(target=self._loop, name="agentwatch-subscriptions", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=10)

    def request_refresh(self):
        self._wake.set()

    def _loop(self):
        while not self._stop.is_set():
            self._wake.clear()
            if now() - self._last_attempt >= 30:
                self.refresh_workbuddy()
            self._wake.wait(300)

    def refresh_workbuddy(self):
        if self.demo or not self._token:
            return
        self._last_attempt, self._refreshing = now(), True
        try:
            windows = self._fetcher(self._token)
            self._workbuddy_snapshot = {"tool": "workbuddy", "bucket": "workbuddy", "source": "workbuddy_openapi",
                                       "at": now(), "data": json.dumps(windows, allow_nan=False)}
            self._error = None
        except urllib.error.HTTPError as error:
            self._error = "auth_required" if error.code in [401, 403] else "error"
        except Exception:
            self._error = "error"  # No URL, exception text, response body or token in logs/API.
        finally:
            self._refreshing = False

    def snapshot(self, current=None):
        current = current or now()
        notices = {
            "codex": "来自本地会话最近上报的账号额度；不是会话 token 累计。账号切换后请以原客户端为准。",
            "claude": "需在接入设置启用终端状态栏上报，并由新 Claude Code 会话完成一次回复；只显示来源实际报告的窗口。",
            "workbuddy": "需官方 user.credit.readable 授权凭证；积分合并订阅、充值及赠送资源包，不代表周订阅额度。"}
        with self.store.lock:
            rows = [dict(r) for r in self.store.db.execute("SELECT * FROM subscription_snapshots ORDER BY tool,bucket")]
        if not self.demo:
            rows = [r for r in rows if r["tool"] != "workbuddy"]
            if self._workbuddy_snapshot:
                rows.append(dict(self._workbuddy_snapshot))
        platforms = []
        for tool, name in [("codex", "Codex"), ("claude", "Claude Code"), ("workbuddy", "WorkBuddy")]:
            limits, updated = [], None
            for row in rows:
                if row["tool"] != tool:
                    continue
                # WorkBuddy cache is account-bound to this process's explicit credential.
                if tool == "workbuddy" and not self.demo and not self._token:
                    continue
                updated = max(updated or 0, row["at"])
                for window in json.loads(row["data"]):
                    window.update(bucket=row["bucket"], source=row["source"], observed_at=row["at"])
                    expired = window["resets_at"] is not None and current >= window["resets_at"]
                    stale = current - row["at"] > STALE_SECONDS or row["at"] > current + 60
                    window["status"] = "expired" if expired else "stale" if stale or (tool == "workbuddy" and self._error) else "available"
                    if expired:
                        window["remaining"] = window["remaining_percent"] = None
                    limits.append(window)
            status = "available" if any(w["status"] == "available" for w in limits) else "stale" if limits else "unavailable"
            if tool == "workbuddy" and not self.demo:
                if not self._token or self._error == "auth_required":
                    status = "auth_required"
                elif self._error:
                    status = "error"
            platforms.append({"tool": tool, "name": name, "status": status, "updated_at": updated,
                              "notice": notices[tool], "limits": limits,
                              "refreshing": self._refreshing if tool == "workbuddy" else False})
        return {"demo": self.demo, "now": current, "platforms": platforms}


def seed_demo(store):
    at = now()
    with store.lock, store.db:
        record_codex(store, {"limit_id": "codex", "primary": {"used_percent": 24.5, "window_minutes": 300, "resets_at": at + 7200},
                             "secondary": {"used_percent": 68, "window_minutes": 10080, "resets_at": at + 259200}}, at)
        record_claude(store, {"seven_day": {"used_percentage": 43.2, "resets_at": at + 172800},
                              "five_hour": {"used_percentage": 91, "resets_at": at + 3600}}, at)
        save_snapshot(store, "workbuddy", "workbuddy", "demo", at,
                      [{"key": "credits", "label": "有效资源包积分合计", "unit": "credits", "limit": 3600, "used": 285,
                        "remaining": 3315, "remaining_percent": 92.0833, "window_minutes": None, "resets_at": None}])
