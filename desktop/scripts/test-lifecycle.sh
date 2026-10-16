#!/bin/zsh
set -euo pipefail
cd "${0:A:h}/.."

# Explicit execution is required because this check displays artificial windows.
mode="${1:---build-only}"
if [[ "$mode" != "--build-only" && "$mode" != "--run" ]]; then
  print -u2 -- "Usage: $0 [--build-only | --run [previously-built-runner]]"
  exit 2
fi
if (( $# > 2 )) || [[ "$mode" == "--build-only" && $# -gt 1 ]]; then
  print -u2 -- "Unexpected lifecycle test arguments"
  exit 2
fi

if [[ "$mode" == "--run" && $# -eq 2 ]]; then
  runner="${2:A}"
  if [[ "${runner:t}" != "LifecycleCheck" || ! -x "$runner" || "${runner:h:t}" != agentwatch-lifecycle.* ]]; then
    print -u2 -- "Expected a runner produced by this script"
    exit 2
  fi
else
  work="$(mktemp -d /private/tmp/agentwatch-lifecycle.XXXXXX)"
  mkdir -p "$work/Sources" "$work/module-cache"
  python3 - "$PWD" "$work/Sources" <<'PY'
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
            raise SystemExit('App startup boundary changed; update the integration harness before compiling')
        text = text[:text.rfind(marker)]
    text = re.sub(r'^import CapsuleCore\s*$', '', text, flags=re.MULTILINE)
    name = 'AppDelegate.swift' if source.name == 'main.swift' else source.name
    (destination / name).write_text(text)
(destination / 'LifecycleCheck.swift').write_text((desktop / 'QA/LifecycleCheck.swift').read_text())
PY
  runner="$work/LifecycleCheck"
  xcrun swiftc -parse-as-library -module-cache-path "$work/module-cache" \
    "$work"/Sources/*.swift -framework AppKit -o "$runner"
  print -r -- "Lifecycle runner built: $runner"
fi

if [[ "$mode" == "--run" ]]; then
  "$runner" --url http://127.0.0.1:1 --material-probe --expect-demo
else
  print -r -- "Not executed. After closing the existing capsule, run:"
  print -r -- "$0 --run ${(q)runner}"
fi
