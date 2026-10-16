from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import webbrowser
from contextlib import ExitStack
from pathlib import Path

from .common import private_dir
from .collector import Collector
from .configuration import ClaudeConfiguration
from .server import AppServer
from .store import Store
from .subscriptions import Subscriptions


def main():
    parser = argparse.ArgumentParser(description="AgentWatch 本机 Agent 看板")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--data-dir", default=str(Path.home() / ".local/share/agentwatch"))
    parser.add_argument("--codex-home", default=os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    parser.add_argument("--claude-home", default=os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
    parser.add_argument("--desktop-home", default=str(Path.home() / "Library/Application Support/Claude"))
    parser.add_argument("--workbuddy-home", default=os.environ.get("WORKBUDDY_HOME", str(Path.home() / ".workbuddy")))
    parser.add_argument("--cursor-home", default=str(Path.home() / ".cursor"), help="Cursor 用户 hooks 目录")
    parser.add_argument("--open", action="store_true", help="启动后打开浏览器")
    parser.add_argument("--demo", action="store_true", help="使用临时人工样例，不读取本机 Agent 或设置")
    parser.add_argument("--scan-once", action="store_true", help="只执行一轮元数据采集")
    args = parser.parse_args()
    if not 0 <= args.port < 65536:
        parser.error("端口无效")
    os.umask(0o077)
    with ExitStack() as cleanup:
        if args.demo:
            from .demo import DemoEnvironment
            demo = cleanup.enter_context(DemoEnvironment())
            store, collector, config = demo.store, demo.collector, demo.config
        else:
            data = Path(args.data_dir).expanduser().resolve()
            private_dir(data)
            store = Store(data / "agentwatch.sqlite")
            cleanup.callback(store.close)
            collector = Collector(store, Path(args.codex_home).expanduser().resolve(), Path(args.claude_home).expanduser().resolve(), data, Path(args.desktop_home).expanduser().resolve(), Path(args.workbuddy_home).expanduser().resolve(), Path(args.cursor_home).expanduser().resolve())
            config = ClaudeConfiguration(collector.claude_home, data)
        if args.scan_once:
            collector.scan(force=True)
            print("人工演示样例检查完成；未读取本机 Agent。" if args.demo else "元数据采集完成；尚有待回补记录：" + str(collector.status["pending"]))
            return 0
        subscriptions = Subscriptions(store, demo=args.demo,
                                      workbuddy_token="" if args.demo else os.environ.get("AGENTWATCH_WORKBUDDY_ACCESS_TOKEN", ""))
        cleanup.callback(subscriptions.stop)
        try:
            server = AppServer(args.port, store, collector, config, subscriptions=subscriptions)
        except OSError:
            print("无法监听这个本机端口。若看板已经运行，请打开 http://127.0.0.1:" + str(args.port) + "；也可以使用 --port 选择其他端口。", file=sys.stderr)
            return 1
        url = "http://127.0.0.1:" + str(server.server_port)
        print("AgentWatch 已启动：" + url, flush=True)
        print("演示模式：全部为人工样例，关闭后删除临时数据。" if args.demo else "统计保存在本机；按 Control+C 停止。", flush=True)
        collector.start()
        subscriptions.start()
        if args.open:
            threading.Timer(0.5, lambda: webbrowser.open(url)).start()
        def stop(signum, frame):
            threading.Thread(target=server.shutdown, daemon=True).start()
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        try:
            server.serve_forever(poll_interval=0.3)
        finally:
            collector.stop()
            server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)

