#!/usr/bin/env python3
"""Export a lab run from the lab Prometheus as CSV.

    ssh -f -N -L 9090:mon:9090 lab
    tools/lab_export.py --node sut-02 --range 2026-11-11T17:59:00Z..2026-11-11T18:38:00Z \
        --out snapshots/lab-1111/trace-sut-02.csv \
        [--windows snapshots/lab-1111/windows.csv --phases snapshots/lab-1111/phases.csv]

The trace has one row per --step seconds: RAPL power (ground truth) and
VM-visible candidate inputs, already turned into rates and ratios. With
--windows, each window row (run,node,phase,start,end) for this node is
averaged over the trace and appended to --phases.

The lab Prometheus only lives on the gateway and keeps a limited history,
so export soon after a run. Stdlib only.
"""
import argparse
import csv
import json
import math
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# column -> PromQL; {i} is the instance selector, {w} the rate window.
# The first two are ground truth; the rest are inputs a VM could also see.
SERIES = {
    "pkg_w": 'sum(rate(node_rapl_package_joules_total{{{i}}}[{w}]))',
    "dram_w": 'sum(rate(node_rapl_dram_joules_total{{{i}}}[{w}]))',
    "busy": 'sum(rate(node_cpu_seconds_total{{{i},mode!~"idle|iowait|steal"}}[{w}]))'
            ' / count(node_cpu_seconds_total{{{i},mode="idle"}})',
    "user": 'sum(rate(node_cpu_seconds_total{{{i},mode=~"user|nice"}}[{w}]))'
            ' / count(node_cpu_seconds_total{{{i},mode="idle"}})',
    "kernel": 'sum(rate(node_cpu_seconds_total{{{i},mode=~"system|irq|softirq"}}[{w}]))'
              ' / count(node_cpu_seconds_total{{{i},mode="idle"}})',
    "iowait": 'sum(rate(node_cpu_seconds_total{{{i},mode="iowait"}}[{w}]))'
              ' / count(node_cpu_seconds_total{{{i},mode="idle"}})',
    "mem_used_gb": '(node_memory_MemTotal_bytes{{{i}}} - node_memory_MemAvailable_bytes{{{i}}}) / 1e9',
    "mem_active_anon_gb": 'node_memory_Active_anon_bytes{{{i}}} / 1e9',
    "ctx_switches_ps": 'sum(rate(scaph_context_switches_total{{{i}}}[{w}]))',
    "disk_read_mbps": 'sum(rate(node_disk_read_bytes_total{{{i}}}[{w}])) / 1e6',
    "disk_write_mbps": 'sum(rate(node_disk_written_bytes_total{{{i}}}[{w}])) / 1e6',
    "net_rx_mbps": 'sum(rate(node_network_receive_bytes_total{{{i},device!="lo"}}[{w}])) / 1e6',
    "net_tx_mbps": 'sum(rate(node_network_transmit_bytes_total{{{i},device!="lo"}}[{w}])) / 1e6',
    "freq_ghz": 'avg(node_cpu_scaling_frequency_hertz{{{i}}}) / 1e9',
}


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def query_range(prom, expr, start, end, step):
    """{ts: value} for a single-series expression; empty if nothing matched."""
    url = f"{prom}/api/v1/query_range?" + urllib.parse.urlencode(
        {"query": expr, "start": start, "end": end, "step": step})
    with urllib.request.urlopen(url, timeout=60) as r:
        result = json.load(r)["data"]["result"]
    if len(result) > 1:
        raise SystemExit(f"expected one series, got {len(result)}: {expr}")
    return {int(float(t)): float(v) for t, v in result[0]["values"]} if result else {}


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--prometheus", default="http://127.0.0.1:9090")
    p.add_argument("--node", required=True, help="instance label, e.g. sut-02")
    p.add_argument("--range", required=True, help="RFC3339..RFC3339")
    p.add_argument("--step", type=int, default=5)
    p.add_argument("--out", required=True, help="trace CSV to write")
    p.add_argument("--windows", help="CSV of run,node,phase,start,end to average")
    p.add_argument("--phases", help="phase averages CSV to append to (needs --windows)")
    a = p.parse_args()

    start, end = (ts(s) for s in a.range.split(".."))
    sel = f'instance="{a.node}"'
    window = f"{3 * a.step}s"
    cols = {c: query_range(a.prometheus, e.format(i=sel, w=window), start, end, a.step)
            for c, e in SERIES.items()}
    grid = sorted(cols["pkg_w"])
    if not grid:
        raise SystemExit(f"no RAPL data for {a.node} in {a.range}")

    with open(a.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "node", *SERIES])
        for t in grid:
            w.writerow([iso(t), a.node, *(fmt(cols[c].get(t)) for c in SERIES)])
    print(f"wrote {len(grid)} rows to {a.out}")

    if a.windows:
        if not a.phases:
            raise SystemExit("--windows needs --phases")
        new = not os.path.exists(a.phases)
        with open(a.windows) as f, open(a.phases, "a", newline="") as out:
            w = csv.writer(out)
            if new:
                w.writerow(["run", "node", "phase", "start", "end", "samples", *SERIES])
            n = 0
            for row in csv.DictReader(f):
                if row["node"] != a.node:
                    continue
                lo, hi = ts(row["start"]), ts(row["end"])
                pts = [t for t in grid if lo <= t <= hi]
                avg = [mean(cols[c].get(t) for t in pts) for c in SERIES]
                w.writerow([row["run"], a.node, row["phase"], row["start"], row["end"],
                            len(pts), *(fmt(v) for v in avg)])
                n += 1
        print(f"appended {n} phases to {a.phases}")


def mean(values):
    vals = [v for v in values if v is not None and math.isfinite(v)]
    return sum(vals) / len(vals) if vals else None


def fmt(v):
    return "" if v is None or not math.isfinite(v) else f"{v:.4f}"


if __name__ == "__main__":
    main()
