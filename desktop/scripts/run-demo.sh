#!/bin/zsh
set -euo pipefail
cd "${0:A:h}/../.."
desktop/scripts/build.sh
exec python3 desktop/scripts/launch.py --demo "$@"
