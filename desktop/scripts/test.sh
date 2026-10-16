#!/bin/zsh
set -euo pipefail
cd "${0:A:h}/.."
export CLANG_MODULE_CACHE_PATH="$PWD/.build/clang-cache"
# CLT ships Swift Testing outside swiftc's default framework search path.
frameworkRoot=""
developerRoot="$(xcode-select -p)"
compilerPath="$(xcrun --find swiftc)"
compilerRoot="${compilerPath:h:h}"
platformRoot="$(xcrun --show-sdk-platform-path 2>/dev/null || true)"
for candidate in "$platformRoot/Developer/Library/Frameworks" "$developerRoot/Library/Developer/Frameworks" "$developerRoot/Platforms/MacOSX.platform/Developer/Library/Frameworks" "$compilerRoot/Library/Developer/Frameworks"; do
  if [[ -d "$candidate/Testing.framework/Modules" ]]; then
    frameworkRoot="$candidate"
    break
  fi
done
if [[ -z "$frameworkRoot" ]]; then
  print -u2 '找不到 Swift Testing；请安装带 Swift Testing 的 Command Line Tools / Xcode。'
  exit 1
fi
swift test --build-system native --disable-sandbox --cache-path .build/cache --config-path .build/config --security-path .build/security --manifest-cache local --disable-xctest --enable-swift-testing -Xswiftc -F -Xswiftc "$frameworkRoot" -Xlinker -F -Xlinker "$frameworkRoot" -Xlinker -rpath -Xlinker "$frameworkRoot" "$@"
