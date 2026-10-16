#!/usr/bin/env python3
"""Isolated, artificial loopback fixture for native transport acceptance.

This is not AgentWatch and never reads its databases, sources, or credentials.
Two origins are started with independent in-memory credentials. Examples:

  python3 desktop/QA/transport_fixture.py --scenario normal
  python3 desktop/QA/transport_fixture.py --scenario restart

The first stdout line is JSON containing both assigned origins. A test runner can
read /__fixture__/counts to inspect request counts without seeing credentials.
POST /__fixture__/restart rotates that origin's artificial credentials. The
post-timeout scenario deliberately takes 7.5 seconds after receiving an action;
the action count must stay at one when the native client's 6-second timeout fires.
Stop with Control-C. There are no source-directory, real-mode, or remote options.
"""

from __future__ import annotations

import argparse
from collections import Counter
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from urllib.parse import urlsplit


SCENARIOS = (
    "normal", "restart", "always401", "post401", "post-timeout",
    "redirect", "missing-cookie", "missing-csrf", "slow-snapshot",
)


class FixtureServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, scenario):
        super().__init__(("127.0.0.1", port), FixtureHandler)
        self.scenario = scenario
        self.generation = 1
        self.counts = Counter()
        self.lock = threading.Lock()
        self.restarted_once = False
        self.other_port = self.server_port

    @property
    def origin(self):
        return "http://127.0.0.1:" + str(self.server_port)

    def authentication(self):
        with self.lock:
            suffix = str(self.server_port) + "-" + str(self.generation)
            return "fixture-cookie-" + suffix, "fixture-csrf-" + suffix

    def restart(self):
        with self.lock:
            self.generation += 1


class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ArtificialTransportFixture/1"
    sys_version = ""

    def log_message(self, *_args):
        pass  # Never log request headers, cookies, query strings, or bodies.

    def respond(self, status, body, content_type="application/json", headers=None):
        encoded = body.encode() if isinstance(body, str) else json.dumps(body, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def trusted(self):
        expected_host = "127.0.0.1:" + str(self.server.server_port)
        if self.headers.get("Host") != expected_host:
            self.respond(403, {"error": "artificial host mismatch"})
            return False
        if self.headers.get("Origin") not in (None, self.server.origin):
            self.respond(403, {"error": "artificial origin mismatch"})
            return False
        return True

    def record(self, method, path):
        with self.server.lock:
            self.server.counts[method + " " + path] += 1

    def authorized(self, mutate=False):
        expected_cookie, expected_csrf = self.server.authentication()
        try:
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
            actual = cookies.get("agentwatch")
            matches = actual and actual.value == expected_cookie
        except Exception:
            matches = False
        if not matches:
            self.respond(401, {"error": "artificial authentication expired"})
            return False
        if mutate and self.headers.get("X-AgentWatch-CSRF") != expected_csrf:
            self.respond(403, {"error": "artificial csrf mismatch"})
            return False
        return True

    def do_GET(self):
        if not self.trusted():
            return
        path = urlsplit(self.path).path
        if path == "/__fixture__/counts":
            with self.server.lock:
                counts = dict(self.server.counts)
            return self.respond(200, {"fixture": True, "counts": counts})
        self.record("GET", path)
        if path == "/":
            cookie, csrf = self.server.authentication()
            headers = {} if self.server.scenario == "missing-cookie" else {
                "Set-Cookie": "agentwatch=" + cookie + "; HttpOnly; SameSite=Strict; Path=/"
            }
            meta = "" if self.server.scenario == "missing-csrf" else '<meta name="csrf" content="' + csrf + '">'
            return self.respond(200, "<!doctype html><head>" + meta + "</head><body>Artificial fixture</body>",
                                "text/html; charset=utf-8", headers)
        if path == "/__fixture__/redirect-target":
            return self.respond(200, {"fixture": True, "unexpected_redirect_followed": True})
        if not path.startswith("/api/") or not self.authorized():
            if not path.startswith("/api/"):
                self.respond(404, {"error": "artificial route unavailable"})
            return
        if self.server.scenario == "always401":
            return self.respond(401, {"error": "artificial persistent unauthorized"})
        if self.server.scenario == "restart":
            with self.server.lock:
                first = not self.server.restarted_once
                if first:
                    self.server.restarted_once = True
                    self.server.generation += 1
            if first:
                return self.respond(401, {"error": "artificial restart"})
        if self.server.scenario == "redirect":
            target = "http://127.0.0.1:" + str(self.server.other_port) + "/__fixture__/redirect-target"
            return self.respond(307, {"fixture": True}, headers={"Location": target})
        now = time.time()
        if path == "/api/snapshot":
            if self.server.scenario == "slow-snapshot":
                time.sleep(7.5)
            return self.respond(200, {
                "demo": True, "now": now, "date": "2026-10-15", "timezone": "Asia/Shanghai",
                "counts": {"observed": 0, "online": 0, "active": 0, "waiting": 0,
                           "error": 0, "unknown": 0, "unknown_roles": 0},
                "children_counts": {"total": 0, "active": 0, "waiting": 0, "error": 0, "unknown": 0},
                "sessions": [], "truncated": False, "activity": [], "projects": [],
                "scan": {"running": False, "files": 0, "pending": 0, "errors": 0}, "today": {},
            })
        if path == "/api/subscriptions":
            return self.respond(200, {"demo": True, "now": now, "platforms": [{
                "tool": "codex", "name": "人工样例", "status": "available", "updated_at": now,
                "notice": "人工传输验收样例，不是实际额度", "refreshing": False,
                "limits": [{"key": "week", "bucket": "subscription", "label": "人工周窗口",
                            "source": "fixture", "observed_at": now, "unit": "percent", "limit": 100,
                            "used": 38, "remaining": 62, "remaining_percent": 62, "window_minutes": 10080,
                            "resets_at": now + 3600, "status": "available"}],
            }]})
        if path == "/api/children":
            return self.respond(200, {"demo": True, "now": now, "parent_id": "artificial",
                                      "children": [], "counts": {"total": 0, "active": 0, "waiting": 0,
                                                                  "error": 0, "unknown": 0}, "truncated": False})
        if path == "/api/integrations":
            return self.respond(200, {"demo": True, "sources": [], "deferred": [],
                                      "data_label": "人工传输样例", "privacy": "不读取任何本机来源"})
        self.respond(404, {"error": "artificial route unavailable"})

    def do_POST(self):
        if not self.trusted():
            return
        path = urlsplit(self.path).path
        self.record("POST", path)
        if path == "/__fixture__/restart":
            self.server.restart()
            return self.respond(200, {"fixture": True, "restarted": True})
        if path not in ("/api/jump", "/api/rescan"):
            return self.respond(404, {"error": "artificial route unavailable"})
        if not self.authorized(mutate=True):
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16384 or self.headers.get_content_type() != "application/json":
                raise ValueError()
            if not isinstance(json.loads(self.rfile.read(length)), dict):
                raise ValueError()
        except (ValueError, json.JSONDecodeError):
            return self.respond(400, {"error": "artificial invalid body"})
        if self.server.scenario == "post401":
            return self.respond(401, {"error": "artificial action unauthorized"})
        if self.server.scenario == "post-timeout":
            time.sleep(7.5)
        # Artificial jump records the request only; it never opens an application.
        result = {"requested": True} if path == "/api/rescan" else {
            "opened": False, "kind": "none", "message": "Artificial fixture never opens applications"
        }
        self.respond(200, result)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", choices=SCENARIOS, default="normal")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--second-port", type=int, default=0)
    args = parser.parse_args()
    servers = []
    started_servers = []
    try:
        for port in (args.port, args.second_port):
            servers.append(FixtureServer(port, args.scenario))
        servers[0].other_port = servers[1].server_port
        servers[1].other_port = servers[0].server_port
        for server in servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            started_servers.append(server)
        print(json.dumps({"fixture": True, "scenario": args.scenario,
                          "origins": [server.origin for server in servers]}), flush=True)
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        for server in servers:
            if server in started_servers:
                server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
