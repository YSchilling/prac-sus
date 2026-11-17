#!/usr/bin/env python3
"""Linear-regression node power from Prometheus, split over pods. Stdlib only.

    python3 powermodel.py --mode offline --node sut-02 --range "$(cat <snapshot>/span.txt)"
    python3 powermodel.py --node sut-02       # online: /metrics on :9270

Every flag can also be set as POWERMODEL_<FLAG>, e.g. POWERMODEL_CPU_MODEL.
The model is features() + predict(), with coefficients from coef.json (train.py);
the rest reads counters and writes CONTRACT.md's shapes. --coef picks another
coefficient file, e.g. coef-default-profile.json for SUTs booked with the
lab's default CPU tuning.
"""
import argparse
import csv
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

# Your group's fixed tag: the groupname column and label in CONTRACT.md.
GROUP = "Group B"
# Per zone: watts = intercept + sum(weight x feature). Written by train.py.
# Chosen by leaving one lab workload out (cpu/memory/io-bound, see
# snapshots/lab-1116-io): package uses the total ratio, because a user/kernel
# split made every held-out workload worse (kernel time stays under 5 %);
# DRAM uses awake alone, because its ratio term only fit the training data.
# coef.json predates this and still uses the split; predict() reads whatever
# features a coefficient file names.
ZONE_FEATURES = {"package": ("ratio", "awake"), "dram": ("awake",)}
CPU_MODES = {"user", "nice", "system", "irq", "softirq"}
# CPU ratio at which "awake" is 63 % of the way from 0 to 1. Power steps up
# as soon as anything runs (cores leave deep sleep, DRAM leaves self-refresh)
# and then flattens: lab-1115 DRAM is 0.9 W idle and 5.6 W from about 20 %
# busy on, whatever the load. 0.1 beat 0.05 and 0.2 on held-out workloads
# (0.05 overshot the 2-6 % CPU of io-bound).
AWAKE_SCALE = 0.1
COEF_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "coef.json")
# Hardware-counter variant (train.py --perf): miss_gbps is last-level cache
# misses x 64-byte lines per second, in GB/s, a stand-in for memory traffic,
# which CPU time cannot see. It needs node-exporter's perf collector, which a
# VM only has with a virtual PMU and CAP_PERFMON. A coefficient file that names
# no PERF_FEATURES never fetches the counter.
PERF_FEATURES = {"miss_gbps"}
PERF_ZONE_FEATURES = {"package": ("ratio", "awake", "miss_gbps"), "dram": ("awake", "miss_gbps")}
CACHE_LINE_BYTES = 64


def query_range(prom, selector, start, end, step):
    """{ts: {labels_json: value}} for a raw selector over the window.

    No PromQL functions: the aggregation happens below, where a /proc reader
    would do it.
    """
    q = urllib.parse.urlencode({"query": selector, "start": start, "end": end, "step": step})
    with urllib.request.urlopen(f"{prom}/api/v1/query_range?{q}") as r:
        body = json.load(r)
    if body["status"] != "success":
        raise RuntimeError(f"{selector}: {body.get('error')}")
    out = {}
    for series in body["data"]["result"]:
        key = json.dumps(series["metric"], sort_keys=True)
        for ts, v in series["values"]:
            out.setdefault(int(ts), {})[key] = float(v)
    return out


def fetch(a, start, end):
    """Node and pod CPU counters on the step grid from start to end."""
    cpu = query_range(a.prometheus, f'node_cpu_seconds_total{{mode=~"user|nice|system|irq|softirq",node="{a.node}"}}',
                      start, end, a.step)
    pods = query_range(a.prometheus, f'container_cpu_usage_seconds_total{{pod!="",container!="",node="{a.node}"}}',
                       start, end, a.step)
    return cpu, pods


def fetch_misses(a, start, end):
    """Per-CPU last-level cache miss counters on the step grid."""
    return query_range(a.prometheus, f'node_perf_cache_misses_total{{node="{a.node}"}}', start, end, a.step)


def needs_perf(coef):
    return any(f in PERF_FEATURES for c in coef.values() for f in c["weights"])


def miss_features(now, prev, step):
    """{"miss_gbps"} from per-CPU miss counters, or None if a CPU is missing
    or a counter is not finite. A reset counts as zero, like counter_delta."""
    if not now or now.keys() != prev.keys():
        return None
    if not all(math.isfinite(v) for v in (*now.values(), *prev.values())):
        return None
    return {"miss_gbps": counter_delta(now, prev) * CACHE_LINE_BYTES / step / 1e9}


def counter_delta(now, prev):
    """Per-series counter deltas, summed. A negative delta is a counter reset
    and counts as zero; a series without a previous sample is skipped."""
    return sum(max(v - prev[k], 0.0) for k, v in now.items() if k in prev)


def pod_cpu(now, prev):
    """CPU seconds per pod over the step, summed over its containers."""
    out = {}
    for k, v in now.items():
        if k not in prev:
            continue
        lab = json.loads(k)
        pod = f"{lab.get('namespace', '')}/{lab.get('pod', '')}"
        out[pod] = out.get(pod, 0.0) + max(v - prev[k], 0.0)
    return out


def features(now, prev, step):
    """Executed CPU fractions, shared by training and inference.

    Only user/nice and system/IRQ time count as execution. No frequency or
    physical-core topology is needed. Reject incomplete/reset CPU windows.
    """
    now = {k: v for k, v in now.items() if json.loads(k)["mode"] in CPU_MODES}
    prev = {k: v for k, v in prev.items() if json.loads(k)["mode"] in CPU_MODES}
    if not now or now.keys() != prev.keys():
        return None
    modes = {}
    user = kernel = 0.0
    for k, value in now.items():
        lab = json.loads(k)
        modes.setdefault(lab["cpu"], set()).add(lab["mode"])
        if not math.isfinite(value) or not math.isfinite(prev[k]) or value < prev[k]:
            return None
        if lab["mode"] in {"user", "nice"}:
            user += value - prev[k]
        else:
            kernel += value - prev[k]
    if any(m != CPU_MODES for m in modes.values()):
        return None
    # Scrape jitter can make a delta slightly exceed the nominal window.
    capacity = max(len(modes) * step, user + kernel)
    ratio = (user + kernel) / capacity
    return {"user_ratio": user / capacity, "kernel_ratio": kernel / capacity,
            "ratio": ratio, "awake": 1 - math.exp(-ratio / AWAKE_SCALE)}


def load_coef(path=COEF_FILE):
    with open(path) as f:
        return json.load(f)


def predict(coef, x, pods, busy_seconds):
    """Linear model per zone; Kepler's split: active = node x ratio,
    pod = active x pod_cpu / node_cpu."""
    rows = {}
    for zone, c in coef.items():
        node = max(c["intercept"] + sum(w * x[f] for f, w in c["weights"].items()), 0.0)
        active = node * x["ratio"]
        share = {p: active * min(s / busy_seconds, 1.0) if busy_seconds > 0 else 0.0
                 for p, s in pods.items()}
        rows[zone] = (node, active, share)
    return rows


def at(coef, cpu, pods, ts, step, misses=None):
    """{zone: (node_w, active_w, {pod: w})} at ts, or None on a gap.
    misses (fetch_misses) is needed only by a coefficient file that uses it."""
    now, prev = cpu.get(ts, {}), cpu.get(ts - step, {})
    x = features(now, prev, step)
    if x is None:
        return None
    if needs_perf(coef):
        m = miss_features((misses or {}).get(ts, {}), (misses or {}).get(ts - step, {}), step)
        if m is None:
            return None
        x.update(m)
    per_pod = pod_cpu(pods.get(ts, {}), pods.get(ts - step, {}))
    return predict(coef, x, per_pod, counter_delta(now, prev))


def offline(a):
    if not a.range:
        sys.exit("--mode offline needs --range")
    start, end = (int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
                  for s in a.range.split(".."))
    coef = load_coef(a.coef)
    cpu, pods = fetch(a, start, end)
    if not cpu:
        sys.exit(f"no node_cpu_seconds_total for node={a.node!r} in {a.range}: "
                 "wrong node name, or outside the snapshot's span?")
    misses = fetch_misses(a, start, end) if needs_perf(coef) else None
    if needs_perf(coef) and not misses:
        sys.exit(f"{a.coef} needs node_perf_cache_misses_total, which has no samples for node={a.node!r}: "
                 "turn on node-exporter's perf collector, or use a coefficient file without miss_gbps")

    with open(a.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "node", "zone", "pred_node_w", "pred_active_w", "groupname", "pred_pod_w"])
        n = 0
        # The first step only provides the previous sample (CONTRACT.md).
        for ts in range(start + a.step, end + 1, a.step):
            rows = at(coef, cpu, pods, ts, a.step, misses)
            if rows is None:
                continue  # a gap: no row, and the scorer counts the miss
            for zone, (node, active, share) in rows.items():
                w.writerow([ts, a.node, zone, f"{node:g}", f"{active:g}", GROUP, json.dumps(share)])
            n += 1
    print(f"wrote {a.out} ({n} steps)", file=sys.stderr)


def metrics(a, rows):
    lab = lambda **kv: ",".join(f"{k}={json.dumps(v)}" for k, v in kv.items())
    out = ["# TYPE powermodel_node_watts gauge"]
    for zone, (node, _, _) in rows.items():
        out.append(f"powermodel_node_watts{{{lab(node_name=a.node, zone=zone, groupname=GROUP)}}} {node:.4f}")
    out.append("# TYPE powermodel_pod_watts gauge")
    for zone, (_, _, share) in rows.items():
        for pod, watts in share.items():
            ns, _, name = pod.partition("/")
            out.append(f"powermodel_pod_watts{{{lab(node_name=a.node, zone=zone, groupname=GROUP, pod_namespace=ns, pod_name=name)}}} {watts:.4f}")
    return "\n".join(out) + "\n"


def online(a):
    coef = load_coef(a.coef)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/metrics":
                return self.send_error(404)
            # The last completed step and the one before: the two samples a
            # /proc reader would hold in memory, nothing older.
            ts = int(time.time()) // a.step * a.step
            try:
                misses = fetch_misses(a, ts - a.step, ts) if needs_perf(coef) else None
                rows = at(coef, *fetch(a, ts - a.step, ts), ts, a.step, misses)
            except (OSError, RuntimeError) as e:
                return self.send_error(503, str(e))
            if rows is None:
                # Zero watts would be a number pretending to be an estimate.
                return self.send_error(503, f"missing, incomplete or reset CPU or perf samples for node={a.node!r} at {ts}")
            body = metrics(a, rows).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    host, _, port = a.listen.rpartition(":")
    print(f"serving /metrics on {a.listen} as groupname={GROUP!r}", file=sys.stderr)
    HTTPServer((host, int(port)), Handler).serve_forever()


def env(flag, default=None):
    """CONTRACT.md: every flag can also be set as POWERMODEL_<FLAG>."""
    return os.environ.get("POWERMODEL_" + flag.upper().replace("-", "_"), default)


def main():
    ap = argparse.ArgumentParser()
    # CONTRACT.md flags.
    ap.add_argument("--mode", choices=["online", "offline"], default=env("mode", "online"))
    ap.add_argument("--prometheus", default=env("prometheus", "http://127.0.0.1:19090"))
    ap.add_argument("--node", default=env("node"))
    ap.add_argument("--step", type=int, default=env("step", "10"), help="whole seconds")
    ap.add_argument("--cpu-model", default=env("cpu-model", ""))
    ap.add_argument("--machine-type", default=env("machine-type", ""))
    ap.add_argument("--range", default=env("range"), help="RFC3339..RFC3339, offline only")
    # This example's extras.
    ap.add_argument("--out", default=env("out", "predictions.csv"))
    ap.add_argument("--coef", default=env("coef", COEF_FILE), help="coefficient file from train.py")
    ap.add_argument("--listen", default=env("listen", ":9270"))
    a = ap.parse_args()
    if not a.node:
        ap.error("--node is required")
    if a.step <= 0:
        ap.error("--step must be positive")
    print(f"hardware: cpu_model={a.cpu_model!r} machine_type={a.machine_type!r}", file=sys.stderr)
    if a.mode == "offline":
        offline(a)
    else:
        online(a)


if __name__ == "__main__":
    main()
