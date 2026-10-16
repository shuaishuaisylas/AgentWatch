#!/bin/zsh
set -euo pipefail
cd "${0:A:h}/.."
./scripts/build.sh --probe
exec .build/无痛胶囊.app/Contents/MacOS/AgentWatchCapsule --material-probe
