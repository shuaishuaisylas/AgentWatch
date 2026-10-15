from __future__ import annotations

import hmac
import json
import mimetypes
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

STATIC = Path(__file__).resolve().parent.parent / "web"


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port, store, collector, config):
        super().__init__(("127.0.0.1", port), Handler)
        self.store, self.collector, self.config = store, collector, config
        self.demo = bool(getattr(collector, "demo", False))
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
                    for card in snapshot["sessions"]:
                        card["jump"] = {"kind": "none", "label": "演示模式", "precision": None, "available": False}
                return self.json_response(200, snapshot)
            if path == "/api/stats":
                return self.json_response(200, self.server.store.stats(args))
            if path == "/api/integrations":
                configuration = self.server.config.status()
                with self.server.store.lock:
                    hook_time = self.server.store.db.execute("SELECT MAX(last_hook_at) FROM sessions WHERE tool='claude'").fetchone()[0]
                    desktop = self.server.store.db.execute("SELECT COUNT(*) FROM sessions WHERE tool='claude' AND surface='desktop'").fetchone()[0]
                return self.json_response(200, {"demo": self.server.demo, "claude": configuration, "last_hook_at": hook_time, "desktop_code_sessions": desktop,
                    "sources": [{"tool": "codex", "name": "Codex", "present": self.server.collector.codex_home.exists(), "label": "本地会话记录与索引", "precision": "用量与上下文快照；准确等待状态视来源而定"},
                                {"tool": "claude", "name": "Claude Code", "present": self.server.collector.claude_home.exists(), "label": "终端、VS Code、本地桌面 Code", "precision": "记录提供用量；状态增强提供执行与等待事件"},
                                {"tool": "workbuddy", "name": "WorkBuddy", "present": (self.server.collector.workbuddy_home / "projects").is_dir(), "label": "本地桌面会话记录（只读）", "precision": "会话、工作目录、最近活动与 token；精确等待状态、上下文占用和积分余额尚未接入"}],
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
            if self.server.demo and (path == "/api/jump" or path.startswith("/api/claude/")):
                return self.json_response(403, {"error": "演示模式使用人工样例，不能打开真实 Agent 或修改 Claude 设置。", "demo": True})
            if path == "/api/rescan":
                self.server.collector.last_discovery = 0
                self.server.collector.wake.set()
                return self.json_response(200, {"requested": True})
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

