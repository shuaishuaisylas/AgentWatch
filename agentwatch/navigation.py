from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote


class NavigationError(Exception):
    pass


def jump(session, kind, runner=subprocess.run):
    if sys.platform != "darwin":
        raise NavigationError("这个跳转入口目前支持 macOS。")
    if session["tool"] == "codex" and kind in ["codex", "codex_vscode"]:
        try:
            rollout = Path(session.get("_rollout_path") or "")
            available = rollout.is_absolute() and rollout.is_file() and rollout.stat().st_size > 0
        except (OSError, TypeError, ValueError):
            available = False
        if not available:
            raise NavigationError("没有可恢复的本地会话记录，暂时无法定位原会话。")
    source_id = session["source_id"]
    if kind == "codex" and session["tool"] == "codex":
        command = ["open", "codex://threads/" + quote(source_id, safe="")]
    elif kind == "codex_vscode" and session["tool"] == "codex":
        command = ["open", "vscode://openai.chatgpt/local/" + quote(source_id, safe="")]
    elif kind == "claude" and session["tool"] == "claude":
        command = ["open", "-a", "Claude"]
    elif kind == "workbuddy" and session["tool"] == "workbuddy":
        command = ["open", "-a", "WorkBuddy"]
    elif kind == "project" and session["tool"] == "claude" and session.get("cwd"):
        command = ["open", "-a", "Visual Studio Code", session["cwd"]]
    elif kind == "terminal" and session.get("tty") and session.get("liveness") == "online":
        tty = session["tty"]
        if not re.fullmatch(r"/dev/ttys\d+", tty):
            raise NavigationError("没有可靠的终端标签页标识。")
        script = '''tell application "Terminal"
repeat with w in windows
repeat with t in tabs of w
if tty of t is "''' + tty + '''" then
set selected of t to true
set frontmost of w to true
activate
return "focused"
end if
end repeat
end repeat
return "not-found"
end tell'''
        result = runner(["osascript", "-e", script], capture_output=True, text=True, timeout=8)
        if result.returncode != 0:
            raise NavigationError("系统未允许定位终端。可在系统设置 → 隐私与安全性 → 自动化中允许，再重试。")
        if result.stdout.strip() != "focused":
            raise NavigationError("原终端标签页已关闭或尚未匹配，未创建新的会话。")
        return {"opened": True, "kind": kind, "message": "已定位原终端标签页"}
    else:
        raise NavigationError("这个会话暂时没有可靠的跳转入口。")
    result = runner(command, capture_output=True, text=True, timeout=8)
    if result.returncode != 0:
        raise NavigationError("无法打开对应应用，请确认应用已经安装。")
    return {"opened": True, "kind": kind, "message": "已发送打开请求"}

