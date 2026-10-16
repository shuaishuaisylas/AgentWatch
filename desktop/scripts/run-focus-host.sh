#!/bin/zsh
set -euo pipefail
cd "${0:A:h}/.."
mkdir -p .build/qa-host-cache .build/AgentWatchFocusHost.app/Contents/MacOS
xcrun swiftc -module-cache-path "$PWD/.build/qa-host-cache" QA/FocusHost.swift -o .build/AgentWatchFocusHost.app/Contents/MacOS/AgentWatchFocusHost
cat > .build/AgentWatchFocusHost.app/Contents/Info.plist <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>CFBundleIdentifier</key><string>local.agentwatch.focus-host</string><key>CFBundleName</key><string>AgentWatchFocusHost</string><key>CFBundleExecutable</key><string>AgentWatchFocusHost</string><key>CFBundlePackageType</key><string>APPL</string></dict></plist>
PLIST
exec .build/AgentWatchFocusHost.app/Contents/MacOS/AgentWatchFocusHost
