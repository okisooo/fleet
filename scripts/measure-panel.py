"""Bounded, secret-free panel benchmark. Never uses the installed cache.

Usage: python scripts/measure-panel.py PANEL_EXE SAMPLE_JSON OUTPUT_DIR [RUNS]
Requires psutil. All UI commands use Fleet's existing cache-file protocol.
Private resident (USS), total working set, and private commit are separate.
No heap collection, trimming, or changes to the system or installed Fleet.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import psutil

exe, fixture, out = map(Path, sys.argv[1:4])
out.mkdir(parents=True, exist_ok=True)
results = []


def sample(process, phase, seconds=8):
    cpu = process.cpu_times()
    start = time.perf_counter()
    points = []
    for _ in range(seconds):
        time.sleep(1)
        m = process.memory_full_info()
        points.append(dict(private_resident=m.uss, working_set=m.rss,
                           private_commit=m.private, handles=process.num_handles(),
                           threads=process.num_threads()))
    end = process.cpu_times()
    return dict(phase=phase, seconds=time.perf_counter()-start,
                cpu_ms=1000*((end.user-cpu.user)+(end.system-cpu.system)), points=points)


for run in range(int(sys.argv[4]) if len(sys.argv) > 4 else 3):
    cache = out / str(run)
    cache.mkdir(exist_ok=True)
    (cache/'panel.json').write_bytes(fixture.read_bytes())
    env = dict(os.environ, FLEET_PANEL_OFFSCREEN='1')
    env.pop('FLEET_PARENT_PID', None)
    child = subprocess.Popen([str(exe.resolve()), '--cache', str(cache.resolve())], env=env)
    process = psutil.Process(child.pid)
    try:
        time.sleep(3)
        phases = [sample(process, 'fresh-hidden')]
        (cache/'panel.show').write_text('{}')
        time.sleep(2)
        phases.append(sample(process, 'open'))
        (cache/'panel.show').write_text('{}')
        time.sleep(2)
        phases.append(sample(process, 'closed-after-open'))
        results.append(dict(run=run, pid=child.pid, phases=phases))
    finally:
        (cache/'panel.quit').touch()
        child.wait(timeout=10)
    (out/'samples.json').write_text(json.dumps(results, indent=2))
    print('completed run', run, flush=True)
