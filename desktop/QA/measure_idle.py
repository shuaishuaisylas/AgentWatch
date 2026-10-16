#!/usr/bin/env python3
"""Measure only this checkout's running capsule process; print no process inventory."""
import argparse
import json
from pathlib import Path
import subprocess
import time


def cpu_seconds(value):
    parts = value.split(':')
    result = 0.0
    for part in parts:
        result = result * 60 + float(part)
    return result


def sample(pid):
    row = subprocess.check_output(['ps', '-p', str(pid), '-o', 'time=,rss='], text=True).split()
    return cpu_seconds(row[0]), int(row[1]) / 1024


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds', type=int, default=60)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    target = str(root / 'desktop/.build/无痛胶囊.app/Contents/MacOS/AgentWatchCapsule')
    matches = []
    for row in subprocess.check_output(['ps', '-axo', 'pid=,comm='], text=True).splitlines():
        fields = row.strip().split(maxsplit=1)
        if len(fields) == 2 and fields[1] == target:
            matches.append(int(fields[0]))
    if len(matches) != 1:
        raise SystemExit('需要恰好一个本仓库构建的胶囊进程')
    pid = matches[0]
    first, initial_memory = sample(pid)
    start = time.monotonic()
    memory = [initial_memory]
    states = []
    for _ in range(args.seconds):
        time.sleep(1)
        last, rss = sample(pid)
        memory.append(rss)
        try:
            states.append(json.loads(Path('/tmp/agentwatch-material-probe.json').read_text()))
        except (OSError, ValueError):
            pass
    duration = time.monotonic() - start
    print(json.dumps({'duration_seconds': round(duration, 2), 'client_cpu_percent': round((last - first) * 100 / duration, 3),
                      'rss_mean_mib': round(sum(memory) / len(memory), 2), 'rss_peak_mib': round(max(memory), 2),
                      'samples': len(memory), 'animation_visible_all_samples': bool(states) and all(s.get('shine') and s.get('capsuleVisible') and not s.get('details') for s in states),
                      'fresh_all_samples': bool(states) and all(s.get('snapshotFresh') for s in states)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
