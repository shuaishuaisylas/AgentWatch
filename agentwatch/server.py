from __future__ import annotations

import hmac
import json
import math
import mimetypes
import re
import secrets
import signal
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .common import now, short_text
from .configuration import ConfigurationError
from .navigation import NavigationError, jump
from .subscriptions import Subscriptions
from .lifecycle_configuration import LifecycleConfiguration
from .cursor_configuration import CursorConfiguration
from .cursor_quota import record_cursor_observation

STATIC = Path(__file__).resolve().parent.parent / "web"


def completion_version(value, session, current):
    """A client may acknowledge an observed older version, never a future one."""
    if not isinstance(value, str):
        raise ValueError("invalid completion version")
    version = float(value)
    last = session.get("last_completed_at")
    if (not math.isfinite(version) or not 0 < version <= current or session.get("role") != "main"
            or last is None or version > last):
        raise ValueError("invalid completion version")
    return version


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port, store, collector, config, subscriptions=None):
        super().__init__(("127.0.0.1", port), Handler)
        self.store, self.collector, self.config = store, collector, config
        self.demo = bool(getattr(collector, "demo", False))
        self.subscriptions = subscriptions or Subscriptions(store, demo=self.demo)
        self.lifecycle_configs = {tool: LifecycleConfiguration(tool, home, store.path.parent)
                                  for tool, home in [("codex", collector.codex_home), ("workbuddy", collector.workbuddy_home)]}
        self.lifecycle_configs["cursor"] = CursorConfiguration(collector.cursor_home, store.path.parent)
        self.browser_token = secrets.token_urlsafe(32)
        self.csrf_token = secrets.token_urlsafe(32)

    def trusted_host(self, value):
        return value in ["127.0.0.1:" + str(self.server_port), "localhost:" + str(self.server_port)]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def trust(self):
        host = self.headers.get("Host", "")
        if not self.server.trusted_host(host):
            self.json_response(403, {"error": "仅允许本机地址访问"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + host:
            self.json_response(403, {"error": "来源校验失败"})
            return False
        if self.headers.get("Sec-Fetch-Site") in ["cross-site"]:
            self.json_response(403, {"error": "不接受其他网站发起的请求"})
            return False
        return True

    def authorized(self, mutate=False):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            value = cookie.get("agentwatch")
            if not value or not hmac.compare_digest(value.value, self.server.browser_token):
                self.json_response(401, {"error": "请从看板首页重新打开"})
                return False
        except Exception:
            self.json_response(401, {"error": "请从看板首页重新打开"})
            return False
        if mutate and not hmac.compare_digest(self.headers.get("X-AgentWatch-CSRF", ""), self.server.csrf_token):
            self.json_response(403, {"error": "操作校验失败，请刷新看板"})
            return False
        return True

    def respond(self, status, body, content_type, bootstrap=False):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if bootstrap:
            self.send_header("Set-Cookie", "agentwatch=" + self.server.browser_token + "; HttpOnly; SameSite=Strict; Path=/")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def json_response(self, status, value):
        self.respond(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        if not self.trust():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ["/", "/index.html"]:
            html = (STATIC / "index.html").read_text().replace("__CSRF__", self.server.csrf_token)
            return self.respond(200, html.encode(), "text/html; charset=utf-8", bootstrap=True)
        if path in ["/app.js", "/style.css", "/favicon.svg"]:
            asset = STATIC / path[1:]
            return self.respond(200, asset.read_bytes(), mimetypes.guess_type(str(asset))[0] or "application/octet-stream")
        if not path.startswith("/api/"):
            return self.json_response(404, {"error": "入口不存在"})
        if not self.authorized():
            return
        args = {key: value[-1][:512] for key, value in parse_qs(parsed.query).items() if value}
        try:
            if path == "/api/snapshot":
                if self.server.demo:
                    self.server.collector.refresh()
                snapshot = self.server.store.snapshot(args, dict(self.server.collector.status))
                snapshot["demo"] = self.server.demo
                if self.server.demo:
                    for card in snapshot["sessions"] + snapshot.get("recent_completed", []):
                        card["jump"] = {"kind": "none", "label": "演示模式", "precision": None, "available": False}
                return self.json_response(200, snapshot)
            if path == "/api/stats":
                return self.json_response(200, self.server.store.stats(args))
            if path == "/api/heatmap":
                if self.server.demo:
                    self.server.collector.refresh()
                heatmap = self.server.store.heatmap(args)
                heatmap["demo"] = self.server.demo
                return self.json_response(200, heatmap)
            if path == "/api/children":
                if self.server.demo:
                    self.server.collector.refresh()
                children = self.server.store.children(args.get("id"))
                if children is None:
                    return self.json_response(404, {"error": "主会话不存在或当前不可见"})
                children["demo"] = self.server.demo
                if self.server.demo:
                    for card in children["children"]:
                        card["jump"] = {"kind": "none", "label": "演示模式", "precision": None, "available": False}
                return self.json_response(200, children)
            if path == "/api/subscriptions":
                return self.json_response(200, self.server.subscriptions.snapshot())
            if path == "/api/integrations":
                configuration = self.server.config.status()
                with self.server.store.lock:
                    hook_time = self.server.store.db.execute("SELECT MAX(last_hook_at) FROM sessions WHERE tool='claude'").fetchone()[0]
                    desktop = self.server.store.db.execute("SELECT COUNT(*) FROM sessions WHERE tool='claude' AND surface='desktop'").fetchone()[0]
                    hook_times = {r["tool"]: r["at"] for r in self.server.store.db.execute("SELECT tool,MAX(last_hook_at) AS at FROM sessions GROUP BY tool")}
                lifecycles = {tool: dict(config.status(), last_hook_at=hook_times.get(tool)) for tool, config in self.server.lifecycle_configs.items()}
                return self.json_response(200, {"demo": self.server.demo, "claude": configuration, "last_hook_at": hook_time, "desktop_code_sessions": desktop,
                    "lifecycles": lifecycles,
                    "sources": [{"tool": "codex", "name": "Codex", "present": self.server.collector.codex_home.exists(), "label": "本地会话索引与官方状态 hooks", "precision": "用量、上下文与生命周期状态；hooks 需客户端审阅信任及实际上报"},
                                {"tool": "claude", "name": "Claude Code", "present": self.server.collector.claude_home.exists(), "label": "终端、VS Code、本地桌面 Code", "precision": "记录提供用量；状态增强提供执行与等待事件"},
                                {"tool": "workbuddy", "name": "WorkBuddy", "present": any((self.server.collector.workbuddy_home / folder).is_dir() for folder in ["projects", "logs"]), "label": "本地会话、客户端状态日志与 hooks", "precision": "只读客户端状态事件可补充沙箱审批；hooks 提供工具与提问事件；积分需独立官方授权；上下文分母未知"},
                                {"tool": "cursor", "name": "Cursor", "present": self.server.collector.cursor_home.exists(), "label": "官方观察 hooks · 仅状态元数据", "precision": "提交仅表示执行待确认；思考、工具与结束事件提供状态；人工待处理、子 Agent、用量及额度暂未覆盖"}],
                    "deferred": ["Claude Cowork", "Claude Chat", "DeepSeek Harness"], "data_label": "临时演示数据（关闭后删除）" if self.server.demo else str(self.server.store.path.parent).replace(str(Path.home()), "~"),
                    "privacy": "全部为人工演示样例；未读取本机 Agent、对话或设置。" if self.server.demo else "本机运行，只保留会话元数据与统计，不保存对话正文。"})
            return self.json_response(404, {"error": "接口不存在"})
        except Exception:
            return self.json_response(500, {"error": "读取失败，请稍后刷新"})

    def do_POST(self):
        if not self.trust() or not self.authorized(mutate=True):
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= 16_384:
                return self.json_response(400, {"error": "请求大小无效"})
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                return self.json_response(400, {"error": "请求格式无效"})
            path = urlparse(self.path).path
            if self.server.demo and (path == "/api/jump" or any(path.startswith("/api/" + tool + "/") for tool in ["claude", "codex", "workbuddy", "cursor"])):
                return self.json_response(403, {"error": "演示模式使用人工样例，不能打开真实 Agent 或修改 Agent 接入设置。", "demo": True})
            if path == "/api/rescan":
                self.server.subscriptions.request_refresh()
                self.server.collector.last_discovery = 0
                self.server.collector.wake.set()
                return self.json_response(200, {"requested": True})
            if path == "/api/cursor/subscription":
                # Explicit local import of observed official-page numbers. This
                # never logs in, reads credentials, or contacts Cursor's API.
                return self.json_response(200, record_cursor_observation(self.server.store, body))
            if path == "/api/inbox/read":
                current = now()
                with self.server.store.lock, self.server.store.db:
                    row = self.server.store.db.execute("SELECT * FROM visible_sessions WHERE id=? AND role='main'", (body.get("id"),)).fetchone()
                    if not row:
                        return self.json_response(404, {"error": "主会话不存在或当前不可见"})
                    version = completion_version(body.get("completed_at"), dict(row), current)
                    marked = self.server.store.acknowledge_completion(row["id"], version, current=current)
                return self.json_response(200, {"read_completed": marked})
            if path == "/api/attention/dismiss":
                version = body.get("version")
                if not isinstance(version, str) or re.fullmatch(r"[0-9a-f]{64}", version) is None:
                    return self.json_response(400, {"error": "提醒版本无效，请刷新后重试"})
                with self.server.store.lock:
                    if not isinstance(body.get("id"), str) or not self.server.store.db.execute("SELECT 1 FROM visible_sessions WHERE id=? AND role='main'", (body.get("id"),)).fetchone():
                        return self.json_response(404, {"error": "主会话不存在或当前不可见"})
                    dismissed = self.server.store.dismiss_attention(body["id"], version, current=now())
                if not dismissed:
                    return self.json_response(409, {"error": "这批提醒已变化，请刷新并重新确认"})
                return self.json_response(200, {"dismissed": True})
            for tool, config in self.server.lifecycle_configs.items():
                if path in ["/api/" + tool + "/preview", "/api/" + tool + "/enable", "/api/" + tool + "/restore"]:
                    with config.lock:
                        if path.endswith("preview"):
                            result = config.preview()
                        elif path.endswith("enable"):
                            result = config.install(body.get("hash"))
                        else:
                            result = config.uninstall()
                    self.server.collector.last_discovery = 0
                    self.server.collector.wake.set()
                    return self.json_response(200, result)
            if path in ["/api/claude/preview", "/api/claude/enable", "/api/claude/restore"]:
                with self.server.config.lock:
                    if path.endswith("preview"):
                        result = self.server.config.preview(body.get("statusline") is True)
                    elif path.endswith("enable"):
                        result = self.server.config.install(body.get("hash"), body.get("statusline") is True)
                    else:
                        result = self.server.config.uninstall()
                self.server.collector.wake.set()
                return self.json_response(200, result)
            if path in ["/api/jump", "/api/alias"]:
                with self.server.store.lock:
                    row = self.server.store.db.execute("SELECT * FROM sessions WHERE id=?", (body.get("id"),)).fetchone()
                    if not row:
                        return self.json_response(404, {"error": "会话不存在或尚未接入"})
                    session = dict(row)
                    if path == "/api/alias":
                        with self.server.store.db:
                            self.server.store.update(session["id"], {"alias": short_text(body.get("alias"))})
                        return self.json_response(200, {"saved": True})
                allowed = self.server.store.jump_info(session)
                with self.server.store.lock:
                    visible = self.server.store.db.execute("SELECT 1 FROM visible_sessions WHERE id=?", (session["id"],)).fetchone()
                if not visible or not allowed["available"]:
                    return self.json_response(400, {"error": allowed.get("reason") or "此会话当前没有可靠的跳转入口。"})
                if session["tool"] == "codex":
                    session["_rollout_path"] = self.server.store.codex_rollout(session)
                kind = body.get("kind", allowed["kind"])
                # Only known, generated navigation kinds; no arbitrary path or command execution.
                if kind != allowed["kind"] and not (kind == "codex" and session["tool"] == "codex"):
                    return self.json_response(400, {"error": "此会话不支持所选跳转"})
                return self.json_response(200, jump(session, kind))
            return self.json_response(404, {"error": "接口不存在"})
        except ConfigurationError as e:
            return self.json_response(409, {"error": str(e)})
        except NavigationError as e:
            return self.json_response(400, {"error": str(e)})
        except (ValueError, TypeError):
            return self.json_response(400, {"error": "请求格式无效"})
        except Exception:
            return self.json_response(500, {"error": "操作未完成，请刷新后重试"})

