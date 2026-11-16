#!/usr/bin/env python3
"""Write truth.csv and span.txt for one of our own snapshots. Stdlib only.

    PORT=19095 tools/prom.sh $PWD/snapshots/lab-1115/tsdb &
    tools/make_truth.py --prometheus http://127.0.0.1:19095 --node sut-01 \
        --range 2026-11-15T08:55:10Z..2026-11-15T11:26:50Z snapshots/lab-1115

Same format as the course snapshots' truth.csv, so train.py and score.py
read it unchanged: kepler_node_cpu_watts per zone and kepler_pod_cpu_watts
per pod, sampled on a --step grid that starts at the range start. Kepler on
the SUT reads RAPL directly, so these are the measured watts. span.txt gets
the range, which train.py and powermodel.py --range use as the window.
"""
import argparse
import csv
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone

COLUMNS = ["ts", "metric", "node", "zone", "pod_id", "pod_name", "pod_namespace", "value"]


def ts(s):
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp())


def series(prom, selector, start, end, step):
    """[(labels, [(ts, value)])] for a raw selector."""
    q = urllib.parse.urlencode({"query": selector, "start": start, "end": end, "step": step})
    with urllib.request.urlopen(f"{prom}/api/v1/query_range?{q}", timeout=120) as r:
        body = json.load(r)
    if body["status"] != "success":
        raise SystemExit(f"{selector}: {body.get('error')}")
    return [(s["metric"], [(int(float(t)), float(v)) for t, v in s["values"]])
            for s in body["data"]["result"]]


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--prometheus", required=True)
    p.add_argument("--node", action="append", required=True, help="node_name label; repeatable")
    p.add_argument("--range", required=True, help="RFC3339..RFC3339")
    p.add_argument("--step", type=int, default=10)
    p.add_argument("snapshot", help="snapshot directory to write truth.csv and span.txt into")
    a = p.parse_args()

    start, end = (ts(s) for s in a.range.split(".."))
    rows = []
    for node in a.node:
        for labels, values in series(a.prometheus, f'kepler_node_cpu_watts{{node_name="{node}"}}',
                                     start, end, a.step):
            rows += [(t, "kepler_node_cpu_watts", node, labels["zone"], "", "", "", v)
                     for t, v in values]
        for labels, values in series(a.prometheus, f'kepler_pod_cpu_watts{{node_name="{node}"}}',
                                     start, end, a.step):
            rows += [(t, "kepler_pod_cpu_watts", node, labels["zone"], labels.get("pod_id", ""),
                      labels.get("pod_name", ""), labels.get("pod_namespace", ""), v)
                     for t, v in values]
    if not any(r[1] == "kepler_node_cpu_watts" for r in rows):
        raise SystemExit(f"no kepler_node_cpu_watts for {a.node} in {a.range}")

    rows.sort(key=lambda r: (r[0], r[1], r[2], r[3], r[6], r[5]))
    with open(os.path.join(a.snapshot, "truth.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        w.writerows(rows)
    with open(os.path.join(a.snapshot, "span.txt"), "w") as f:
        f.write(a.range + "\n")
    steps = len({r[0] for r in rows})
    print(f"wrote {len(rows)} rows over {steps} steps to {a.snapshot}/truth.csv and span.txt")


if __name__ == "__main__":
    main()
