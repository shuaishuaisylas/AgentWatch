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
from .cursor_bridge import EVENTS


class CursorConfiguration:
    """Merge observational user hooks using Cursor's direct-command arrays."""

    def __init__(self, cursor_home, data_dir):
        self.tool, self.events = "cursor", list(EVENTS)
        self.settings = Path(cursor_home) / "hooks.json"
        self.data_dir = Path(data_dir)
        self.manifest = self.data_dir / "cursor-installation.json"
        self.lock = threading.RLock()
        bridge = str(Path(__file__).resolve().parent / "cursor_bridge.py")
        self.command = " ".join(shlex.quote(s) for s in [sys.executable, bridge, "--data-dir", str(self.data_dir)]) + " >/dev/null 2>&1 || :"

    def read(self):
        if self.settings.is_symlink():
            raise ConfigurationError("Cursor hooks.json 是符号链接，已保持原文件不变。")
        raw = self.settings.read_bytes() if self.settings.exists() else b"{}"
        try:
            config = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ConfigurationError("Cursor hooks.json 不是有效 JSON，已保持原文件不变。")
        if not isinstance(config, dict) or not isinstance(config.get("hooks", {}), dict):
            raise ConfigurationError("Cursor hooks 结构无法安全合并，已保持原文件不变。")
        if config and (type(config.get("version")) is not int or config.get("version") != 1):
            raise ConfigurationError("Cursor hooks 配置版本未知，请使用官方 version: 1 格式后重试。")
        for event in self.events:
            handlers = config.get("hooks", {}).get(event, [])
            if not isinstance(handlers, list) or any(not isinstance(item, dict) for item in handlers):
                raise ConfigurationError("Cursor hook 事件不是直接配置数组，已保持原文件不变。")
            for item in handlers:
                kind = item.get("type", "command")
                key = "prompt" if kind == "prompt" else "command"
                if kind not in ["command", "prompt"] or not isinstance(item.get(key), str) or not item[key].strip():
                    raise ConfigurationError("Cursor hook 定义无法安全合并，已保持原文件不变。")
        return raw, config

    def load_manifest(self):
        if self.manifest.is_symlink():
            raise ConfigurationError("Cursor 接入记录是符号链接，已保持原文件不变。")
        try:
            value = json.loads(self.manifest.read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def owned_commands(self, manifest):
        previous = manifest.get("command") if manifest.get("active", True) or manifest.get("pending") else None
        pending_previous = manifest.get("pending_previous_command") if manifest.get("pending") else None
        return {value for value in [self.command, previous, pending_previous]
                if isinstance(value, str) and value}

    def status(self):
        with self.lock:
            try:
                _, config = self.read()
                owned = self.owned_commands(self.load_manifest())
                enabled = any(isinstance(item.get("command"), str) and item["command"] in owned for event in self.events
                              for item in config.get("hooks", {}).get(event, []))
                count = sum(any(isinstance(item.get("command"), str) and item["command"] in owned and item.get("type", "command") == "command"
                                and item.get("failClosed") is False and item.get("matcher", "") in ["", "*"]
                                for item in config.get("hooks", {}).get(event, [])) for event in self.events)
                return {"enabled": enabled, "installed_events": count, "complete": count == len(self.events),
                        "review_required": False, "error": None}
            except (ConfigurationError, OSError):
                return {"enabled": False, "installed_events": 0, "complete": False,
                        "review_required": False, "error": "Cursor 状态配置暂不可安全合并"}

    def preview(self):
        with self.lock:
            raw, _ = self.read()
            try:
                settings_label = str(Path("~") / self.settings.relative_to(Path.home()))
            except ValueError:
                settings_label = str(self.settings)
            return {"hash": fingerprint(raw.decode("utf-8")), "tool": self.tool,
                    "events": self.events, "command": self.command,
                    "configuration": {"version": 1, "hooks": {
                        event: [{"command": self.command, "timeout": 3, "failClosed": False}]
                        for event in self.events}},
                    "settings_label": "Cursor 用户配置 " + settings_label,
                    "effect": "只观察本机会话生命周期，保留已有 hooks；不批准、拒绝或执行 Agent 任务。",
                    "privacy": "仅记录会话、轮次、工具身份、状态、时间、工作目录和版本；不保存提示词、回复、工具输入输出、邮箱或会话正文。",
                    "next_step": "在 Cursor 的 Hooks 页面检查实际事件；配置保存不等于已收到上报。仅接入本机用户 hooks，不覆盖云端、子 Agent 或完整人工审批。"}

    def install(self, expected_hash):
        with self.lock:
            raw, config = self.read()
            if fingerprint(raw.decode("utf-8")) != expected_hash:
                raise ConfigurationError("Cursor 配置在预览后发生变化，请重新预览。")
            manifest = self.load_manifest()
            owned = self.owned_commands(manifest)
            previous_command = next((command for command in owned if command != self.command), None)
            already_owned = any(isinstance(item, dict) and isinstance(item.get("command"), str) and item["command"] in owned
                                for handlers in config.get("hooks", {}).values() if isinstance(handlers, list)
                                for item in handlers)
            existed = self.settings.exists()
            added_version = "version" not in config
            config.setdefault("version", 1)
            hooks = config.setdefault("hooks", {})
            for event in self.events:
                handlers = hooks.setdefault(event, [])
                # Replace only exact owned commands, leaving all third-party fields
                # and handler order intact. One observer per event is sufficient.
                remaining, inserted = [], False
                for item in handlers:
                    if isinstance(item.get("command"), str) and item["command"] in owned:
                        if not inserted:
                            remaining.append({"command": self.command, "timeout": 3, "failClosed": False})
                            inserted = True
                    else:
                        remaining.append(item)
                if not inserted:
                    remaining.append({"command": self.command, "timeout": 3, "failClosed": False})
                hooks[event] = remaining
            private_dir(self.data_dir)
            if self.read()[0] != raw:
                raise ConfigurationError("Cursor 配置在保存前发生变化，请重新预览。")
            if not manifest.get("backup") or manifest.get("active") is False or not already_owned:
                backup_dir = self.data_dir / "config-backups"
                private_dir(backup_dir)
                backup = backup_dir / ("cursor-hooks-" + datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8] + ".json")
                backup.write_bytes(raw)
                os.chmod(backup, 0o600)
                previous = manifest.get("backup")
                history = manifest.get("previous_backups", [])
                history = [item for item in history if isinstance(item, str)] if isinstance(history, list) else []
                if isinstance(previous, str) and previous not in history:
                    history.append(previous)
                manifest.update(backup=str(backup), existed=existed, added_version=added_version,
                                previous_backups=history)
            # Save the recovery metadata as preparation, never as enabled state.
            # A manifest/backup write can be slow, so it must precede the last
            # read of user settings. Cursor does not offer a compare-and-swap
            # settings API; this check narrows rather than eliminates the final
            # external-editor race before the atomic file replacement.
            manifest.update(command=self.command, active=False, pending=True)
            if previous_command:
                manifest["pending_previous_command"] = previous_command
            write_json(self.manifest, manifest)
            if self.read()[0] != raw:
                raise ConfigurationError("Cursor 配置在保存前发生变化，请重新预览。")
            write_json(self.settings, config, self.settings.stat().st_mode & 0o777 if self.settings.exists() else 0o600)
            manifest.update(active=True, pending=False)
            manifest.pop("pending_previous_command", None)
            write_json(self.manifest, manifest)
            return self.status()

    def uninstall(self):
        with self.lock:
            raw, config = self.read()
            manifest = self.load_manifest()
            owned = self.owned_commands(manifest)
            hooks = config.get("hooks", {})
            removed = 0
            for event, handlers in list(hooks.items()):
                if not isinstance(handlers, list):
                    continue
                remaining = [item for item in handlers if not isinstance(item, dict) or
                             not isinstance(item.get("command"), str) or item["command"] not in owned]
                removed += len(handlers) - len(remaining)
                if remaining or len(remaining) == len(handlers):
                    hooks[event] = remaining
                else:
                    hooks.pop(event)
            if not removed:
                if manifest and manifest.get("active", True):
                    manifest["active"] = False
                    manifest["pending"] = False
                    manifest.pop("pending_previous_command", None)
                    manifest.pop("existed", None)
                    manifest.pop("added_version", None)
                    write_json(self.manifest, manifest)
                return {"removed": 0, "enabled": self.status()["enabled"],
                        "message": "未发现 AgentWatch 的 Cursor hooks，配置保持不变。"}
            if not hooks:
                config.pop("hooks", None)
            if manifest.get("added_version") and config == {"version": 1}:
                config.pop("version")
            if self.read()[0] != raw:
                raise ConfigurationError("Cursor 配置在撤销时发生变化，请重试。")
            if not config and manifest and not manifest.get("existed", True):
                self.settings.unlink(missing_ok=True)
            else:
                write_json(self.settings, config, self.settings.stat().st_mode & 0o777 if self.settings.exists() else 0o600)
            if manifest:
                manifest["active"] = False
                manifest["pending"] = False
                manifest.pop("pending_previous_command", None)
                manifest.pop("existed", None)
                manifest.pop("added_version", None)
                write_json(self.manifest, manifest)
            return {"removed": removed, "enabled": self.status()["enabled"],
                    "message": "已移除 AgentWatch 的 Cursor hooks，保留其他设置及后续修改。"}
