#!/bin/zsh
set -euo pipefail
if (( $# != 1 )); then
  print -u2 'Usage: test-demo-api.sh http://127.0.0.1:EXPLICIT_DEMO_PORT'
  exit 2
fi
cd "${0:A:h}/.."
DEMO_CHECK_TMP="$(mktemp -d /private/tmp/agentwatch-demo-check.XXXXXX)"
trap 'rm -rf "$DEMO_CHECK_TMP"' EXIT
DEMO_CHECK_ARCH="$(uname -m)"
export CLANG_MODULE_CACHE_PATH="$DEMO_CHECK_TMP/clang-cache"
xcrun swiftc -target "$DEMO_CHECK_ARCH-apple-macosx13.0" \
  -module-cache-path "$DEMO_CHECK_TMP/modules" -parse-as-library \
  Sources/CapsuleCore/*.swift QA/DemoCheck.swift -o "$DEMO_CHECK_TMP/DemoCheck"
"$DEMO_CHECK_TMP/DemoCheck" "$1"
