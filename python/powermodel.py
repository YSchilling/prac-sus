#!/usr/bin/env python3
"""Baseline node power from Prometheus, split over pods. Stdlib only.

    python3 powermodel.py --mode offline --node sut-02 --range "$(cat <snapshot>/span.txt)"
    python3 powermodel.py --node sut-02       # online: /metrics on :9270

Every flag can also be set as POWERMODEL_<FLAG>, e.g. POWERMODEL_CPU_MODEL.
The model is predict(); the rest reads counters and writes CONTRACT.md's shapes.
"""
import argparse
import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

# Your group's fixed tag: the groupname column and label in CONTRACT.md.
GROUP = "Group B"
IDLE_W, MAX_W = 50.0, 150.0
SPLIT = {"package": 0.85, "dram": 0.15}


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
    cpu = query_range(a.prometheus, f'node_cpu_seconds_total{{mode!="idle",node="{a.node}"}}',
                      start, end, a.step)
    pods = query_range(a.prometheus, f'container_cpu_usage_seconds_total{{pod!="",container!="",node="{a.node}"}}',
                       start, end, a.step)
    return cpu, pods


def counter_delta(now, prev):
    """Per-series counter deltas, summed. A negative delta is a counter reset
    and counts as zero; a series without a previous sample is skipped."""
    return sum(max(v - prev[k], 0.0) for k, v in now.items() if k in prev)


def usage_ratio(now, prev, step):
    """Non-idle fraction of all cores over the step, 0..1."""
    cores = len({json.loads(k)["cpu"] for k in now})
    if cores == 0:
        return 0.0
    return min(counter_delta(now, prev) / (cores * step), 1.0)


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


def predict(ratio, pods, busy_seconds):
    """Kepler's split: active = node x ratio; pod = active x pod_cpu / node_cpu."""
    node_w = IDLE_W + (MAX_W - IDLE_W) * ratio
    rows = {}
    for zone, frac in SPLIT.items():
        node, active = node_w * frac, node_w * frac * ratio
        share = {p: active * min(c / busy_seconds, 1.0) if busy_seconds > 0 else 0.0
                 for p, c in pods.items()}
        rows[zone] = (node, active, share)
    return rows


def at(cpu, pods, ts, step):
    """{zone: (node_w, active_w, {pod: w})} at ts, or None on a gap."""
    now, prev = cpu.get(ts, {}), cpu.get(ts - step, {})
    if not now or not prev:
        return None
    per_pod = pod_cpu(pods.get(ts, {}), pods.get(ts - step, {}))
    return predict(usage_ratio(now, prev, step), per_pod, counter_delta(now, prev))


def offline(a):
    if not a.range:
        sys.exit("--mode offline needs --range")
    start, end = (int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
                  for s in a.range.split(".."))
    cpu, pods = fetch(a, start, end)
    if not cpu:
        sys.exit(f"no node_cpu_seconds_total for node={a.node!r} in {a.range}: "
                 "wrong node name, or outside the snapshot's span?")

    with open(a.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "node", "zone", "pred_node_w", "pred_active_w", "groupname", "pred_pod_w"])
        n = 0
        # The first step only provides the previous sample (CONTRACT.md).
        for ts in range(start + a.step, end + 1, a.step):
            rows = at(cpu, pods, ts, a.step)
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
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/metrics":
                return self.send_error(404)
            # The last completed step and the one before: the two samples a
            # /proc reader would hold in memory, nothing older.
            ts = int(time.time()) // a.step * a.step
            try:
                rows = at(*fetch(a, ts - a.step, ts), ts, a.step)
            except (OSError, RuntimeError) as e:
                return self.send_error(503, str(e))
            if rows is None:
                # Zero watts would be a number pretending to be an estimate.
                return self.send_error(503, f"no node_cpu_seconds_total for node={a.node!r} at {ts}")
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
