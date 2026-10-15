"""Artificial review data, isolated from every installed Agent and its settings."""
from __future__ import annotations

import tempfile
import threading
from pathlib import Path

from .common import now
from .store import Store


class DemoConfiguration:
    def __init__(self):
        self.lock = threading.RLock()

    def status(self):
        return {"enabled": False, "installed_events": 0, "complete": False,
                "statusline_enabled": False, "backup": None, "error": None,
                "demo": True}


class DemoCollector:
    """An in-memory controller for sample timestamps; never starts a collector."""
    demo = True

    def __init__(self, store, root):
        self.store = store
        self.codex_home = root / "sources" / "codex"
        self.claude_home = root / "sources" / "claude"
        self.desktop_home = root / "sources" / "desktop"
        self.workbuddy_home = root / "sources" / "workbuddy"
        for path in [self.codex_home, self.claude_home, self.desktop_home,
                     self.workbuddy_home / "projects"]:
            path.mkdir(parents=True, mode=0o700)
        self.wake = threading.Event()
        self.last_discovery = 0
        self.status = {"running": False, "files": 0, "pending": 0, "read_bytes": 0,
                       "last_scan": None, "errors": 0, "demo": True}
        # Every label and value below is invented for interface review.
        self.samples = [
            ("codex", "design", "main", "active", "正在整理界面组件", "online", "desktop", "设计系统整理", 54000, 200000, 15),
            ("claude", "approval", "main", "waiting_approval", "等待你批准演示操作", "online", "terminal", "页面开发", 33000, 200000, 25),
            ("claude", "question", "child", "waiting_user", "等待你选择演示方案", "online", "terminal", "交互方案比较", None, None, None),
            ("codex", "error", "child", "error", "演示：构建检查失败", "online", "vscode", "构建检查", 72000, 200000, 900),
            ("workbuddy", "research", "unknown", "unknown", "最近阶段：整理演示资料", "unknown", "desktop", "市场资料整理", None, None, None),
            ("codex", "complete", "main", "idle", "演示回复已结束", "online", "desktop", "文档校对", 18000, 200000, 30),
        ]
        self._seed(root)

    def _seed(self, root):
        current = now()
        with self.store.lock, self.store.db:
            for index, sample in enumerate(self.samples):
                tool, source, role, state, stage, online, surface, alias, used, limit, age = sample
                sid = self.store.session(tool, "demo-" + source)
                self.store.metadata(sid, str(root / "projects" / ("示例产品" if index < 4 else "示例研究")),
                                    role=role, surface=surface, alias=alias, version="演示样例")
                if role == "child":
                    self.store.update(sid, {"parent_id": "codex:demo-design" if tool == "codex" else "claude:demo-approval"})
                self.store.state(sid, state, stage, current, "demo")
                self.store.update(sid, {"liveness": online, "started_at": current - 3600,
                                        "coverage": "observed"})
                # A month of deterministic, artificial events exercises all date views.
                for day in range(30):
                    at = current - day * 86400 - index * 60
                    input_count = 1200 + (day % 7) * 345 + index * 125
                    output_count = 80 + (day % 5) * 32 + index * 15
                    self.store.usage(sid, "demo-usage-" + str(index) + "-" + str(day), at,
                                     input_count, output_count, input_count // 3,
                                     input_count // 10 if tool == "claude" else None,
                                     output_count // 4, kind="demo")
        self.refresh()

    def refresh(self):
        """Keep the review scenario live while preserving its stale-context example."""
        current = now()
        with self.store.lock, self.store.db:
            for sample in self.samples:
                tool, source, role, state, stage, online, surface, alias, used, limit, age = sample
                sid = tool + ":demo-" + source
                self.store.update(sid, {"last_event_at": current, "state_at": current,
                                       "context_used": used, "context_limit": limit,
                                       "context_at": current - age if age is not None else None})
                self.store.touch(sid, current)
        self.status["last_scan"] = current

    def start(self):
        self.refresh()

    def stop(self):
        pass

    def scan(self, force=False):
        self.refresh()


class DemoEnvironment:
    """Own both the temporary database and all fake source roots."""
    def __enter__(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="agentwatch-demo-")
        self.root = Path(self.temporary.name)
        try:
            self.store = Store(self.root / "data" / "agentwatch.sqlite")
            self.collector = DemoCollector(self.store, self.root)
            self.config = DemoConfiguration()
        except BaseException:
            if hasattr(self, "store"):
                self.store.close()
            self.temporary.cleanup()
            raise
        return self

    def __exit__(self, *exception):
        try:
            self.store.close()
        finally:
            self.temporary.cleanup()
