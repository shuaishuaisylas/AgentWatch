#!/bin/zsh
set -euo pipefail
cd "${0:A:h}/../.."
mkdir -p desktop/.build/qa-transport-cache
machineArchitecture="$(uname -m)"
xcrun swiftc -target "$machineArchitecture-apple-macosx13.0" -module-cache-path "$PWD/desktop/.build/qa-transport-cache" -parse-as-library desktop/Sources/CapsuleCore/APIClient.swift desktop/QA/TransportCheck.swift -o desktop/.build/AgentWatchTransportCheck
exec desktop/.build/AgentWatchTransportCheck "$PWD/desktop/QA/transport_fixture.py"
