#!/usr/bin/env python3
"""Turn workload Job logs into phase windows for tools/lab_export.py.

    tools/lab_windows.py --node sut-01 r1-cpu-bound.log r1-memory-bound.log ... > windows.csv

Reads the "<UTC> <phase> start" marks the workloads/*.yaml Jobs print. A log
named r<N>-<job>.log (as tools/lab_repeat.sh writes them) becomes run
"<job>/r<N>". Each phase runs until the next mark; the first --lead seconds
are dropped so the rate window behind a sample does not reach back into
the previous phase. Short phases keep their last third (a 15 s ramp step
keeps 5 s, so its samples still overlap the previous step a little).
Repeated names such as "idle" are numbered in order: idle, idle-2, idle-3.
"""
import argparse
import csv
import os
import re
import sys
from datetime import datetime, timedelta

MARK = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) (.+?)(?: start)?$")


def phases(path):
    marks = []
    with open(path) as f:
        for line in f:
            m = MARK.match(line.strip())
            if m:
                marks.append((datetime.strptime(m[1], "%Y-%m-%dT%H:%M:%SZ"), m[2]))
    seen = {}
    for (t, name), (end, _) in zip(marks, marks[1:]):
        # "level=25 stream=2 cpu=6" -> "level25-stream2-cpu6"; "ramp=125MB/s/worker" -> "ramp125MBps"
        name = name.replace("MB/s/worker", "MBps").replace("=", "").replace(" ", "-")
        seen[name] = seen.get(name, 0) + 1
        yield t, end, name if seen[name] == 1 else f"{name}-{seen[name]}"


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--node", required=True)
    p.add_argument("--lead", type=int, default=20, help="seconds dropped at each phase start")
    p.add_argument("logs", nargs="+")
    a = p.parse_args()

    w = csv.writer(sys.stdout)
    w.writerow(["run", "node", "phase", "start", "end"])
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    for path in a.logs:
        base = os.path.basename(path).removesuffix(".log")
        m = re.fullmatch(r"r(\d+)-(.+)", base)
        run = f"{m[2]}/r{m[1]}" if m else base
        for start, end, name in phases(path):
            lead = min(a.lead, (end - start).total_seconds() * 2 / 3)
            w.writerow([run, a.node, name,
                        (start + timedelta(seconds=lead)).strftime(fmt), end.strftime(fmt)])


if __name__ == "__main__":
    main()
