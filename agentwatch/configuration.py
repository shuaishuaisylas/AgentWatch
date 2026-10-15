from __future__ import annotations

import copy
import json
import os
import shlex
import sys
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path

from .bridge import EVENTS
from .common import fingerprint, private_dir


class ConfigurationError(Exception):
    pass


def write_json(path, value, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".agentwatch-", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class ClaudeConfiguration:
    def __init__(self, claude_home, data_dir):
        self.settings = Path(claude_home) / "settings.json"
        self.data_dir = Path(data_dir)
        self.manifest = self.data_dir / "claude-installation.json"
        self.lock = threading.RLock()
        bridge = str(Path(__file__).resolve().parent / "bridge.py")
        command = " ".join(shlex.quote(s) for s in [sys.executable, bridge, "--data-dir", str(self.data_dir)])
        self.hook_command = command + " --agentwatch-hook"
        self.status_command = command + " --agentwatch-statusline"

    def read(self):
        if self.settings.is_symlink():
            raise ConfigurationError("settings.json 是符号链接，无法安全自动合并。请使用普通配置文件后重试。")
        raw = self.settings.read_bytes() if self.settings.exists() else b"{}"
        try:
            config = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ConfigurationError("Claude 设置不是有效 JSON，已保持原文件不变。")
        if not isinstance(config, dict) or not isinstance(config.get("hooks", {}), dict):
            raise ConfigurationError("Claude hooks 的结构无法安全合并，已保持原文件不变。")
        return raw, config

    def load_manifest(self):
        try:
            d = json.loads(self.manifest.read_text())
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def status(self):
        manifest = self.load_manifest()
        try:
            _, config = self.read()
            owned = manifest.get("hook_command", self.hook_command)
            count = 0
            for event in EVENTS:
                groups = config.get("hooks", {}).get(event, [])
                if isinstance(groups, list) and any(isinstance(g, dict) and any(isinstance(h, dict) and h.get("command") == owned for h in g.get("hooks", [])) for g in groups):
                    count += 1
            statusline = config.get("statusLine", {})
            return {"enabled": count > 0, "installed_events": count, "complete": count == len(EVENTS),
                    "statusline_enabled": isinstance(statusline, dict) and statusline.get("command") == manifest.get("status_command", self.status_command),
                    "backup": Path(manifest.get("backup", "")).name if manifest.get("backup") else None,
                    "error": None}
        except (ConfigurationError, OSError):
            return {"enabled": False, "installed_events": 0, "statusline_enabled": False, "error": "设置暂不可安全合并"}

    def preview(self, include_statusline=False):
        raw, config = self.read()
        status = self.status()
        if include_statusline and config.get("statusLine") is not None and not isinstance(config["statusLine"], dict):
            raise ConfigurationError("现有状态栏结构无法安全保留。可先只启用状态上报。")
        return {"hash": fingerprint(raw.decode("utf-8")), "events": EVENTS, "add_events": len(EVENTS) - status["installed_events"],
                "include_statusline": bool(include_statusline), "has_statusline": bool(config.get("statusLine")),
                "command": self.hook_command, "settings_label": "Claude 用户设置 settings.json",
                "effect": "后台上报状态，保留现有 hooks；不会批准、拒绝或执行 Agent 的任务。新启动的本机会话可开始上报。" + ("状态栏同时上报上下文和来源提供的订阅额度。" if include_statusline else ""),
                "privacy": "只写会话标识、状态、时间和数字统计，丢弃提示词、回复与工具参数。"}

    def install(self, expected_hash, include_statusline=False):
        raw, config = self.read()
        if fingerprint(raw.decode("utf-8")) != expected_hash:
            raise ConfigurationError("设置在预览后发生变化，请重新预览。")
        manifest = self.load_manifest()
        old_command = manifest.get("hook_command")
        hooks = config.setdefault("hooks", {})
        for event in EVENTS:
            groups = hooks.setdefault(event, [])
            if not isinstance(groups, list):
                raise ConfigurationError("有一个 hook 事件无法安全合并，已保持原设置。")
            for group in groups:
                if not isinstance(group, dict) or not isinstance(group.get("hooks", []), list):
                    raise ConfigurationError("现有 hook 分组无法安全合并，已保持原设置。")
            owned_commands = {c for c in [self.hook_command, old_command] if c}
            if old_command and old_command != self.hook_command:
                for group in groups:
                    for hook in group.get("hooks", []):
                        if isinstance(hook, dict) and hook.get("command") == old_command:
                            hook["command"] = self.hook_command
            if any(h.get("command") in owned_commands for g in groups for h in g.get("hooks", []) if isinstance(h, dict)):
                continue
            groups.append({"hooks": [{"type": "command", "command": self.hook_command, "async": True, "timeout": 3}]})
        if include_statusline:
            old_statusline = config.get("statusLine")
            if old_statusline is not None and not isinstance(old_statusline, dict):
                raise ConfigurationError("现有状态栏无法安全合并，已保持原设置。")
            if not isinstance(old_statusline, dict) or old_statusline.get("command") != manifest.get("status_command", self.status_command):
                manifest["old_statusline"] = copy.deepcopy(old_statusline)
            wrapped = copy.deepcopy(old_statusline or {})
            wrapped.update(type="command", command=self.status_command)
            config["statusLine"] = wrapped
        private_dir(self.data_dir)
        if not manifest.get("backup"):
            backup_dir = self.data_dir / "config-backups"
            private_dir(backup_dir)
            backup = backup_dir / ("claude-settings-" + datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8] + ".json")
            backup.write_bytes(raw)
            os.chmod(backup, 0o600)
            manifest.update(backup=str(backup), existed=self.settings.exists())
        manifest.update(hook_command=self.hook_command, status_command=self.status_command)
        # The wrapper must know the original command before Claude can invoke it.
        latest, _ = self.read()
        if fingerprint(latest.decode("utf-8")) != expected_hash:
            raise ConfigurationError("设置在保存前发生变化，请重新预览。")
        write_json(self.manifest, manifest)
        mode = (self.settings.stat().st_mode & 0o777) if self.settings.exists() else 0o600
        write_json(self.settings, config, mode)
        return self.status()

    def uninstall(self):
        raw, config = self.read()
        manifest = self.load_manifest()
        owned = {c for c in [self.hook_command, manifest.get("hook_command")] if c}
        hooks = config.get("hooks", {})
        removed = 0
        for event in list(hooks):
            groups = hooks[event]
            if not isinstance(groups, list):
                continue
            new_groups = []
            for group in groups:
                if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                    new_groups.append(group)
                    continue
                remaining = [h for h in group["hooks"] if not isinstance(h, dict) or h.get("command") not in owned]
                removed += len(group["hooks"]) - len(remaining)
                if remaining or len(remaining) == len(group["hooks"]):
                    replacement = dict(group, hooks=remaining)
                    new_groups.append(replacement)
            if new_groups:
                hooks[event] = new_groups
            else:
                hooks.pop(event)
        if not hooks:
            config.pop("hooks", None)
        statusline = config.get("statusLine", {})
        if isinstance(statusline, dict) and statusline.get("command") in {c for c in [self.status_command, manifest.get("status_command")] if c}:
            original = manifest.get("old_statusline")
            if original is None:
                config.pop("statusLine", None)
            else:
                config["statusLine"] = original
        latest, _ = self.read()
        if latest != raw:
            raise ConfigurationError("设置在撤销时发生变化，请重新查看后重试。")
        if not config and manifest and not manifest.get("existed", True):
            self.settings.unlink(missing_ok=True)
        else:
            mode = (self.settings.stat().st_mode & 0o777) if self.settings.exists() else 0o600
            write_json(self.settings, config, mode)
        # Keep the private backup and original command for any in-flight wrapper.
        return {"removed": removed, "enabled": self.status()["enabled"], "message": "已移除 AgentWatch 的设置，保留其他设置及后续修改。"}
