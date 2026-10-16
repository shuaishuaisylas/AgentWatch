#!/bin/zsh
set -euo pipefail
cd "${0:A:h}/.."

# Compile into one temporary module so the runner can inject an artificial
# URLProtocol into APIClient without exposing that initializer in the product.
controllerCheckWork="$(mktemp -d /private/tmp/agentwatch-controller-read.XXXXXX)"
trap 'rm -rf "$controllerCheckWork"' EXIT
mkdir -p "$controllerCheckWork/Sources" "$controllerCheckWork/module-cache"
python3 - "$PWD" "$controllerCheckWork/Sources" <<'PY'
import re
import sys
from pathlib import Path

desktop = Path(sys.argv[1])
destination = Path(sys.argv[2])
for source in sorted((desktop / 'Sources/CapsuleCore').glob('*.swift')):
    (destination / source.name).write_text(source.read_text())
for source in sorted((desktop / 'Sources/AgentWatchCapsule').glob('*.swift')):
    text = source.read_text()
    if source.name == 'main.swift':
        marker = '\nMainActor.assumeIsolated {'
        if text.count(marker) != 1 or 'app.run()' not in text[text.rfind(marker):]:
            raise SystemExit('App startup boundary changed; update the controller harness before compiling')
        text = text[:text.rfind(marker)]
    text = re.sub(r'^import CapsuleCore\s*$', '', text, flags=re.MULTILINE)
    name = 'AppDelegate.swift' if source.name == 'main.swift' else source.name
    (destination / name).write_text(text)
(destination / 'ControllerReadCheck.swift').write_text((desktop / 'QA/ControllerReadCheck.swift').read_text())
PY
xcrun swiftc -parse-as-library -module-cache-path "$controllerCheckWork/module-cache" \
  "$controllerCheckWork"/Sources/*.swift -framework AppKit -o "$controllerCheckWork/ControllerReadCheck"
"$controllerCheckWork/ControllerReadCheck"
