from __future__ import annotations

import json
import os
import shlex
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path

from .common import fingerprint, private_dir
from .configuration import ConfigurationError, write_json
from .lifecycle_bridge import EVENTS


class LifecycleConfiguration:
    """Merge only AgentWatch's observational handlers into a user's hook file."""

    def __init__(self, tool, source_home, data_dir):
        if tool not in EVENTS:
            raise ValueError("Unsupported lifecycle source")
        self.tool, self.events = tool, EVENTS[tool]
        self.settings = Path(source_home) / ("hooks.json" if tool == "codex" else "settings.json")
        self.data_dir = Path(data_dir)
        self.manifest = self.data_dir / (tool + "-installation.json")
        self.lock = threading.RLock()
        bridge = str(Path(__file__).resolve().parent / "lifecycle_bridge.py")
        self.command = " ".join(shlex.quote(s) for s in [sys.executable, bridge, "--data-dir", str(self.data_dir), "--tool", tool])

    def read(self):
        if self.settings.is_symlink():
            raise ConfigurationError("状态配置是符号链接，已保持原文件不变。")
        raw = self.settings.read_bytes() if self.settings.exists() else b"{}"
        try:
            config = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ConfigurationError("状态配置不是有效 JSON，已保持原文件不变。")
        if not isinstance(config, dict) or not isinstance(config.get("hooks", {}), dict):
            raise ConfigurationError("状态配置结构无法安全合并，已保持原文件不变。")
        return raw, config

    def load_manifest(self):
        try:
            value = json.loads(self.manifest.read_text())
            return value if isinstance(value, dict) else {}
        except (ValueError, OSError):
            return {}

    def status(self):
        try:
            _, config = self.read()
            owned = {self.command, self.load_manifest().get("command")}
            count = 0
            for event in self.events:
                groups = config.get("hooks", {}).get(event, [])
                if isinstance(groups, list) and any(isinstance(g, dict) and isinstance(g.get("hooks"), list) and
                        any(isinstance(h, dict) and h.get("command") and h.get("command") in owned for h in g["hooks"])
                        for g in groups):
                    count += 1
            return {"enabled": count > 0, "installed_events": count, "complete": count == len(self.events),
                    "review_required": self.tool == "codex", "error": None}
        except (ConfigurationError, OSError):
            return {"enabled": False, "installed_events": 0, "complete": False, "review_required": self.tool == "codex",
                    "error": "状态配置暂不可安全合并"}

    def preview(self):
        raw, _ = self.read()
        label = "Codex" if self.tool == "codex" else "WorkBuddy"
        return {"hash": fingerprint(raw.decode("utf-8")), "tool": self.tool, "events": self.events,
                "command": self.command, "settings_label": label + " 用户配置 " + self.settings.name,
                "effect": "上报执行、等待和结束事件，保留现有配置；不会批准、拒绝或执行 Agent 任务。",
                "privacy": "仅保存会话身份、状态、时间与进程身份，丢弃提示词、回复、工具参数和结果。",
                "next_step": "在 Codex 的 /hooks 中审阅并信任新增项，再开启或继续会话。" if self.tool == "codex" else "新开 WorkBuddy 会话后检查实际状态上报；配置保存不等于已收到事件。"}

    def install(self, expected_hash):
        raw, config = self.read()
        if fingerprint(raw.decode("utf-8")) != expected_hash:
            raise ConfigurationError("状态配置在预览后发生变化，请重新预览。")
        manifest = self.load_manifest()
        owned = {self.command, manifest.get("command")}
        hooks = config.setdefault("hooks", {})
        for event in self.events:
            groups = hooks.setdefault(event, [])
            if not isinstance(groups, list) or any(not isinstance(g, dict) or not isinstance(g.get("hooks", []), list) for g in groups):
                raise ConfigurationError("已有 hook 分组无法安全合并，已保持原文件不变。")
            found = False
            for group in groups:
                for handler in group.get("hooks", []):
                    if isinstance(handler, dict) and handler.get("command") and handler.get("command") in owned:
                        handler.update(command=self.command, type="command", timeout=3, **{"async": False})
                        found = True
            if not found:
                groups.append({"hooks": [{"type": "command", "command": self.command, "timeout": 3, "async": False}]})
        private_dir(self.data_dir)
        if not manifest.get("backup"):
            backup_dir = self.data_dir / "config-backups"
            private_dir(backup_dir)
            backup = backup_dir / (self.tool + "-hooks-" + datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8] + ".json")
            backup.write_bytes(raw)
            os.chmod(backup, 0o600)
            manifest.update(backup=str(backup), existed=self.settings.exists())
        if self.read()[0] != raw:
            raise ConfigurationError("状态配置在保存前发生变化，请重新预览。")
        manifest["command"] = self.command
        write_json(self.manifest, manifest)
        write_json(self.settings, config, self.settings.stat().st_mode & 0o777 if self.settings.exists() else 0o600)
        return self.status()

    def uninstall(self):
        raw, config = self.read()
        manifest = self.load_manifest()
        owned = {self.command, manifest.get("command")}
        hooks = config.get("hooks", {})
        removed = 0
        for event, groups in list(hooks.items()):
            if not isinstance(groups, list):
                continue
            remaining_groups = []
            for group in groups:
                if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                    remaining_groups.append(group)
                    continue
                remaining = [h for h in group["hooks"] if not isinstance(h, dict) or not h.get("command") or h.get("command") not in owned]
                removed += len(group["hooks"]) - len(remaining)
                if remaining or not group["hooks"]:
                    remaining_groups.append(dict(group, hooks=remaining))
            if remaining_groups:
                hooks[event] = remaining_groups
            else:
                hooks.pop(event)
        if not hooks:
            config.pop("hooks", None)
        if self.read()[0] != raw:
            raise ConfigurationError("状态配置在撤销时发生变化，请重试。")
        if not config and manifest and not manifest.get("existed", True):
            self.settings.unlink(missing_ok=True)
        else:
            write_json(self.settings, config, self.settings.stat().st_mode & 0o777 if self.settings.exists() else 0o600)
        return {"removed": removed, "enabled": self.status()["enabled"]}
