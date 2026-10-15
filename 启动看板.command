#!/bin/zsh
cd "${0:A:h}" || exit 1
for agentwatch_python in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if command -v "$agentwatch_python" >/dev/null 2>&1 && "$agentwatch_python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    exec "$agentwatch_python" -m agentwatch --open "$@"
  fi
done
print -u2 "AgentWatch 需要 Python 3.9 或以上版本。请先从 python.org 安装 Python，重新打开终端后再启动。"
if [[ -t 0 ]]; then
  read -k 1 "?按任意键关闭…"
fi
exit 1

