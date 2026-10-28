#!/usr/bin/env python3
"""Score a predictions.csv against a snapshot's truth.csv. Stdlib only.

    python3 tools/score.py predictions.csv --truth <snapshot>/truth.csv

A stand-in for the course scorer, which we do not have. It follows
CONTRACT.md: rows must sit on the truth grid, --step must be a multiple of
it, one row per node, zone and step. Files that break this are rejected.

What is compared:

  pred_node_w, zone package|dram   kepler_node_cpu_watts
  pred_node_w, zone host           scaph_host_power_microwatts, in watts
  pred_active_w                    sum of kepler_pod_cpu_watts at that step
                                   (approximation: Kepler hands pods the
                                   active share, so their sum is our best
                                   stand-in for active power)
  pred_pod_w                       kepler_pod_cpu_watts per namespace/pod;
                                   a pod missing on one side counts as 0 W

Per node and zone: MAE, RMSE, MAPE, and energy error, the relative error
of total energy over the steps both sides have.
"""
import argparse
import csv
import json
import math
import sys
from collections import Counter, defaultdict

COLUMNS = ["ts", "node", "zone", "pred_node_w", "pred_active_w", "groupname", "pred_pod_w"]


class Rejected(Exception):
    pass


def load_truth(path):
    node, active, pods = {}, defaultdict(float), defaultdict(dict)
    for r in csv.DictReader(open(path, newline="")):
        ts, v = int(r["ts"]), float(r["value"])
        if r["metric"] == "kepler_node_cpu_watts":
            node[(r["node"], r["zone"], ts)] = v
        elif r["metric"] == "scaph_host_power_microwatts":
            node[(r["node"], "host", ts)] = v / 1e6
        elif r["metric"] == "kepler_pod_cpu_watts":
            active[(r["node"], r["zone"], ts)] += v
            pod = f'{r["pod_namespace"]}/{r["pod_name"]}'
            pods[(r["node"], r["zone"], ts)][pod] = pods[(r["node"], r["zone"], ts)].get(pod, 0.0) + v
    if not node:
        raise Rejected(f"{path}: no node power in truth")
    stamps = sorted({k[2] for k in node})
    gaps = Counter(b - a for a, b in zip(stamps, stamps[1:]))
    grid = gaps.most_common(1)[0][0] if gaps else 10
    return node, active, pods, grid, stamps[0]


def load_predictions(path, grid, origin):
    rows, groups, steps = {}, set(), set()
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != COLUMNS:
            raise Rejected(f"header is {reader.fieldnames}, want {COLUMNS}")
        for n, r in enumerate(reader, start=2):
            try:
                ts = int(r["ts"])
                node_w, active_w = float(r["pred_node_w"]), float(r["pred_active_w"])
            except ValueError as e:
                raise Rejected(f"line {n}: {e}")
            if not all(map(math.isfinite, (node_w, active_w))):
                raise Rejected(f"line {n}: non-finite watts")
            if (ts - origin) % grid:
                raise Rejected(f"line {n}: ts {ts} is off the {grid} s truth grid")
            key = (r["node"], r["zone"], ts)
            if key in rows:
                raise Rejected(f"line {n}: second row for node {key[0]}, zone {key[1]}, ts {ts}")
            pods = None
            if r["pred_pod_w"]:
                try:
                    pods = {k: float(v) for k, v in json.loads(r["pred_pod_w"]).items()}
                except (ValueError, AttributeError) as e:
                    raise Rejected(f"line {n}: pred_pod_w is not a JSON object of watts: {e}")
            rows[key] = (node_w, active_w, pods)
            groups.add(r["groupname"])
    if not rows:
        raise Rejected("no rows")
    if groups == {""} or len(groups) > 1:
        raise Rejected(f"groupname must be one fixed, non-empty tag, got {sorted(groups)}")
    for node, zone in {k[:2] for k in rows}:
        stamps = sorted(k[2] for k in rows if k[:2] == (node, zone))
        steps.update(b - a for a, b in zip(stamps, stamps[1:]))
    step = Counter(steps).most_common(1)[0][0] if steps else grid
    if step % grid:
        raise Rejected(f"step {step} s is not a multiple of the {grid} s truth grid")
    return rows, groups.pop(), step


def metrics(pairs):
    """MAE, RMSE, MAPE (%, truth > 0 only), energy error (%) over (pred, truth)."""
    n = len(pairs)
    err = [p - t for p, t in pairs]
    mae = sum(map(abs, err)) / n
    rmse = math.sqrt(sum(e * e for e in err) / n)
    pct = [abs(p - t) / t for p, t in pairs if t > 0]
    mape = 100 * sum(pct) / len(pct) if pct else float("nan")
    total = sum(t for _, t in pairs)
    energy = 100 * (sum(p for p, _ in pairs) - total) / total if total else float("nan")
    return n, mae, rmse, mape, energy


def table(title, results):
    if not results:
        return
    print(f"\n{title}")
    print(f'  {"node":<10}{"zone":<9}{"n":>6}{"MAE W":>10}{"RMSE W":>10}{"MAPE %":>9}{"energy %":>10}')
    for (node, zone), m in sorted(results.items()):
        n, mae, rmse, mape, energy = m
        print(f"  {node:<10}{zone:<9}{n:>6}{mae:>10.3f}{rmse:>10.3f}{mape:>9.2f}{energy:>+10.2f}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("predictions")
    p.add_argument("--truth", required=True)
    a = p.parse_args()

    try:
        truth_node, truth_active, truth_pods, grid, origin = load_truth(a.truth)
        rows, group, step = load_predictions(a.predictions, grid, origin)
    except Rejected as e:
        sys.exit(f"rejected: {e}")

    node_pairs, active_pairs, pod_pairs = defaultdict(list), defaultdict(list), defaultdict(list)
    missing = Counter()
    for (node, zone, ts), (node_w, active_w, pods) in rows.items():
        key = (node, zone, ts)
        if key not in truth_node:
            missing[(node, zone)] += 1
            continue
        node_pairs[(node, zone)].append((node_w, truth_node[key]))
        if zone == "host":
            continue
        active_pairs[(node, zone)].append((active_w, truth_active.get(key, 0.0)))
        if pods is not None:
            want = truth_pods.get(key, {})
            for pod in pods.keys() | want.keys():
                pod_pairs[(node, zone)].append((pods.get(pod, 0.0), want.get(pod, 0.0)))

    print(f"group {group!r}, step {step} s on a {grid} s grid, {len(rows)} rows")
    for (node, zone), n in sorted(missing.items()):
        print(f"  warning: {n} rows for {node}/{zone} have no truth at their ts and are not scored")
    if not node_pairs:
        sys.exit("nothing to score: no prediction matches a truth series (check --node and zone names)")

    table("node power (pred_node_w)", {k: metrics(v) for k, v in node_pairs.items()})
    table("active power (pred_active_w vs. sum of pod watts, approximate)",
          {k: metrics(v) for k, v in active_pairs.items()})
    table("pod power (pred_pod_w, per pod and step)", {k: metrics(v) for k, v in pod_pairs.items()})


if __name__ == "__main__":
    main()
