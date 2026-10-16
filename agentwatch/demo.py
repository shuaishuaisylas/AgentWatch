"""Artificial review data, isolated from every installed Agent and its settings."""
from __future__ import annotations

import tempfile
import threading
from datetime import datetime
from pathlib import Path

from .common import TZ, now
from .store import Store
from .subscriptions import seed_demo


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
        self.cursor_home = root / "sources" / "cursor"
        for path in [self.codex_home, self.claude_home, self.desktop_home,
                     self.workbuddy_home / "projects", self.cursor_home]:
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
            ("codex", "status-only", "unknown", "active", "演示：仅收到状态上报", "unknown", "desktop", "等待会话记录", None, None, None),
            ("codex", "implementation", "child", "active", "演示：正在实现界面", "unknown", "desktop", "组件实现", 21000, 200000, 15),
            ("codex", "audit", "child", "waiting_approval", "演示：较久前等待批准", "unknown", "terminal", "验收操作确认", None, None, None),
            ("codex", "copy", "child", "idle", "演示文案检查已结束", "offline", "vscode", "文案检查", 9000, 200000, 30),
            ("codex", "offline", "child", "active", "演示：离线前的执行阶段", "offline", "terminal", "离线任务样例", None, None, None),
            ("codex", "verification", "child", "waiting_user", "演示：等待你确认验收范围", "online", "desktop", "交互验收", None, None, None),
            ("cursor", "cursor-observed", "main", "active", "演示：Cursor 工具返回 · 继续处理", "unknown", "desktop", "Cursor 界面整理", None, None, None),
        ]
        self.parents = {"codex:demo-verification": "codex:demo-implementation"}
        self.state_ages = {"codex:demo-audit": 900}
        self.state_sources = {"codex:demo-implementation": "event", "codex:demo-offline": "event"}
        self._seed(root)

    def _seed(self, root):
        current = now()
        with self.store.lock, self.store.db:
            for index, sample in enumerate(self.samples):
                tool, source, role, state, stage, online, surface, alias, used, limit, age = sample
                sid = self.store.session(tool, "demo-" + source)
                project = "g-p-demo-title-project" if source == "complete" else "示例产品" if index < 4 or role == "child" else "示例研究"
                # Client project labels differ from filesystem folder names.
                # All names are artificial and independent of installed clients.
                self.store.metadata(sid, str(root / "projects" / project), role=role, surface=surface,
                                    alias="" if source == "complete" else alias,
                                    project_display_name=("演示长项目名称用于检查悬停展开与十字符限制" if source == "complete"
                                                          else "演示界面项目" if tool == "codex" else None),
                                    session_title="演示会话 · " + alias, version="演示样例")
                if role == "child":
                    parent = self.parents.get(sid, "codex:demo-design" if tool == "codex" else "claude:demo-approval")
                    self.store.update(sid, {"parent_id": parent})
                self.store.state(sid, state, stage, current - self.state_ages.get(sid, 0),
                                 self.state_sources.get(sid, "demo"))
                if source in {"approval", "question", "audit", "verification"}:
                    # Explicitly artificial human-action evidence, independent of
                    # execution state; multiple children still count as one root.
                    kind = "waiting_approval" if source in {"approval", "audit"} else "waiting_user"
                    self.store.open_interaction(sid, "demo-manual-" + source, 0, kind,
                                                current - self.state_ages.get(sid, 0),
                                                tool_name="artificial_manual_action", asynchronous=True)
                if source in {"complete", "copy"}:
                    self.store.complete(sid, current - 45)
                self.store.update(sid, {"liveness": online, "started_at": current - 3600,
                                        "coverage": "observed"})
                if tool == "cursor":
                    self.store.update(sid, {"coverage": "status_only", "attention_covered": 0,
                                            "last_hook_at": current, "state_source": "hook"})
                    continue
                if source == "status-only":
                    self.store.update(sid, {"last_hook_at": current, "state_source": "hook"})
                    continue
                if source in {"implementation", "audit", "copy", "offline", "verification"}:
                    # Relationship/status examples have no invented token report.
                    continue
                # A month of deterministic, artificial events exercises all date views.
                for day in range(30):
                    at = current - day * 86400 - index * 60
                    input_count = 1200 + (day % 7) * 345 + index * 125
                    output_count = 80 + (day % 5) * 32 + index * 15
                    self.store.usage(sid, "demo-usage-" + str(index) + "-" + str(day), at,
                                     input_count, output_count, input_count // 3,
                                     input_count // 10 if tool == "claude" else None,
                                     output_count // 4, kind="demo")
            # Older, sparse artificial activity gives the current natural year a
            # visible pattern without adding future usage or changing the recent month.
            calendar = datetime.fromtimestamp(current, TZ)
            for tool_index, sid in enumerate(["codex:demo-design", "claude:demo-approval", "workbuddy:demo-research"]):
                for month in range(1, calendar.month + 1):
                    for day in [2, 5, 11, 18, 23, 27]:
                        at = datetime(calendar.year, month, day, 12, tzinfo=TZ).timestamp()
                        if at >= current - 30 * 86400:
                            continue
                        input_count = 1600 + month * 240 + day * 95 + tool_index * 170
                        output_count = 120 + (day % 5) * 45 + tool_index * 20
                        self.store.usage(sid, f"demo-year-{tool_index}-{month}-{day}", at,
                                         input_count, output_count, input_count // 3,
                                         input_count // 10 if tool_index == 1 else None,
                                         output_count // 4, kind="demo")
        self.refresh()

    def refresh(self):
        """Keep sample activity live while preserving intentionally old evidence."""
        current = now()
        with self.store.lock, self.store.db:
            for sample in self.samples:
                tool, source, role, state, stage, online, surface, alias, used, limit, age = sample
                sid = tool + ":demo-" + source
                self.store.update(sid, {"last_event_at": current, "state_at": current - self.state_ages.get(sid, 0),
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
            seed_demo(self.store)
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
