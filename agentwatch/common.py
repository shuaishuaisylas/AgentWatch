from __future__ import annotations

import hashlib
import math
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
SID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,160}$")


def now():
    return datetime.now(timezone.utc).timestamp()


def timestamp(value, fallback=None):
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            n = float(value)
            if n > 1e12:
                n /= 1000
            return n if math.isfinite(n) and 0 < n < 1e11 else fallback
        if isinstance(value, str):
            d = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d.timestamp()
    except (ValueError, OverflowError, OSError):
        pass
    return fallback


def day_of(at):
    return datetime.fromtimestamp(at, TZ).date().isoformat()


def safe_id(value):
    return value if isinstance(value, str) and SID_RE.fullmatch(value) else None


def number(value):
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value < 1e16 and math.isfinite(value) else None


def short_text(value, limit=80):
    if not isinstance(value, str):
        return ""
    return " ".join(value.replace("\x00", "").split())[:limit]


def fingerprint(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def project_identity(cwd):
    """Only inspect Git path metadata; never run commands or inspect project code."""
    if not isinstance(cwd, str) or not cwd.startswith("/") or len(cwd) > 4096:
        return "unknown", "未识别项目", "", ""
    path = Path(cwd).expanduser()
    root = path
    for candidate in [path, *list(path.parents)[:6]]:
        if (candidate / ".git").exists():
            root = candidate
            break
    project = root
    git = root / ".git"
    try:
        if git.is_file() and git.stat().st_size < 8192:
            pointer = git.read_text().strip()
            if pointer.startswith("gitdir:"):
                target = (root / pointer[7:].strip()).resolve()
                if target.parent.name == "worktrees" and target.parent.parent.name == ".git":
                    project = target.parent.parent.parent
    except (OSError, UnicodeError):
        pass
    canonical = str(project)
    return fingerprint(canonical)[:16], project.name or "主目录", str(root), str(root) if root != project else ""


def period_bounds(period, selected):
    try:
        d = datetime.strptime(selected, "%Y-%m-%d").replace(tzinfo=TZ)
    except (TypeError, ValueError):
        d = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        start = d - timedelta(days=d.weekday())
        end = start + timedelta(days=7)
    elif period == "month":
        start = d.replace(day=1)
        end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    elif period == "year":
        start = d.replace(month=1, day=1)
        end = start.replace(year=start.year + 1)
    else:
        period = "day"
        start, end = d, d + timedelta(days=1)
    buckets = []
    cursor = start
    while cursor < end:
        if period == "day":
            nxt, label = cursor + timedelta(hours=1), cursor.strftime("%H:00")
        elif period == "year":
            nxt = cursor.replace(year=cursor.year + 1, month=1) if cursor.month == 12 else cursor.replace(month=cursor.month + 1)
            label = cursor.strftime("%m月")
        else:
            nxt, label = cursor + timedelta(days=1), cursor.strftime("%m/%d")
        buckets.append({"start": cursor.timestamp(), "end": nxt.timestamp(), "label": label, "tokens": 0})
        cursor = nxt
    return period, start, end, buckets


def private_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)

