#!/usr/bin/env python3
"""Own only the demo server and capsule processes created by this invocation."""
import argparse
import http.cookiejar
import json
import re
import selectors
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / 'desktop/.build/无痛胶囊.app/Contents/MacOS/AgentWatchCapsule'


def verify_origin(value):
    parsed = urllib.parse.urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError('本机端口无效') from exc
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not port or
            parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('地址须为 http://127.0.0.1:端口')
    return 'http://127.0.0.1:' + str(port)


class RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('已拒绝服务重定向')


def check_service(origin, demo):
    # This cookie jar exists only for this preflight and is never shared with the app.
    opener = urllib.request.build_opener(RejectRedirect(), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    with opener.open(origin + '/', timeout=6) as response:
        html = response.read(1024 * 1024).decode('utf-8')
        if '<meta name="csrf"' not in html or 'AgentWatch' not in html:
            raise ValueError('目标端口不是预期的 AgentWatch 服务')
    with opener.open(origin + '/api/snapshot?tool=all&project=all&role=main', timeout=6) as response:
        data = json.load(response)
    if data.get('demo') is not demo:
        raise ValueError('服务模式不匹配，已停止连接')


def stop_owned(process):
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def main():
    parser = argparse.ArgumentParser(description='无痛胶囊启动器')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--demo', action='store_true', help='启动自己拥有的临时人工演示服务')
    mode.add_argument('--real', action='store_true', help='连接已经运行的本机服务，不代为启动或关闭它')
    parser.add_argument('--url', default='http://127.0.0.1:8766')
    parser.add_argument('--port', type=int, default=0, help='演示服务端口；默认自动选临时空闲端口')
    args = parser.parse_args()
    server = capsule = None
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        if not APP.is_file():
            raise ValueError('请先运行 desktop/scripts/build.sh')
        if args.demo:
            server = subprocess.Popen([sys.executable, '-u', '-m', 'agentwatch', '--demo', '--port', str(args.port)],
                                      cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            selector = selectors.DefaultSelector()
            selector.register(server.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + 10
            origin = None
            while time.monotonic() < deadline and server.poll() is None:
                if not selector.select(timeout=0.2):
                    continue
                line = server.stdout.readline()
                match = re.fullmatch(r'AgentWatch 已启动：(http://127\.0\.0\.1:\d+)\s*', line)
                if match:
                    origin = verify_origin(match.group(1))
                    break
            selector.close()
            if origin is None:
                raise ValueError('人工演示服务未启动；指定端口可能被占用，没有复用占用者')
        else:
            origin = verify_origin(args.url)
        check_service(origin, demo=args.demo)
        print(('人工演示' if args.demo else '本机服务') + '：' + origin, flush=True)
        print('菜单栏“胶囊”可暂隐、恢复或退出；退出后保留原有真实服务。', flush=True)
        command = [str(APP), '--url', origin]
        if args.demo:
            command.append('--expect-demo')
        capsule = subprocess.Popen(command, cwd=ROOT)
        return capsule.wait()
    except KeyboardInterrupt:
        return 0
    except (ValueError, OSError, urllib.error.URLError) as exc:
        print('无法启动胶囊：' + str(exc) + '。真实模式请先运行“启动看板.command”。', file=sys.stderr)
        return 1
    finally:
        stop_owned(capsule)
        stop_owned(server)


if __name__ == '__main__':
    sys.exit(main())
