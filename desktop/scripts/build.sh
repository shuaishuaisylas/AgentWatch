#!/bin/zsh
set -euo pipefail
cd "${0:A:h}/.."

# Never replace the executable of a running capsule, including a QA instance.
if capsule_processes="$(pgrep -x AgentWatchCapsule 2>&1)"; then
  print -u2 -- "请先退出运行中的胶囊，再重新构建。"
  exit 1
else
  capsule_probe_status=$?
  if [[ "$capsule_probe_status" -ne 1 || -n "$capsule_processes" ]]; then
    print -u2 -- "无法确认胶囊是否已退出，已停止构建。"
    exit 1
  fi
fi

export CLANG_MODULE_CACHE_PATH="$PWD/.build/clang-cache"
swift build --build-system native --disable-sandbox --cache-path .build/cache --config-path .build/config --security-path .build/security --manifest-cache local -c release
APP="$PWD/.build/无痛胶囊.app"
mkdir -p "$APP/Contents/MacOS"
cp .build/release/AgentWatchCapsule "$APP/Contents/MacOS/AgentWatchCapsule"
cp Resources/Info.plist "$APP/Contents/Info.plist"
if [[ "${1:-}" == "--probe" ]]; then
  /usr/libexec/PlistBuddy -c 'Add :AWMaterialProbe bool true' "$APP/Contents/Info.plist"
fi
codesign --force --sign - "$APP" >/dev/null
print -r -- "$APP"
