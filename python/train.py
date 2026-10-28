#!/usr/bin/env python3
"""Fit powermodel.py's per-zone linear regression and write coef.json.

    .venv/bin/python python/train.py \
        --snapshot snapshots/stress-1008=http://127.0.0.1:19090 \
        --snapshot snapshots/teastore-1008=http://127.0.0.1:19091

Each snapshot must be served (tools/prom.sh, PORT=... for a second one).
Features come from powermodel.features(), the code the model runs, so
training and prediction cannot drift apart. Labels come from the snapshot's
truth.csv; the model itself never reads it.

Before fitting on everything, each snapshot is held out once and predicted
by a model trained on the others: the held-out snapshot is a new workload,
so that is the number to watch.
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime
from types import SimpleNamespace

import numpy as np
from sklearn.linear_model import LinearRegression

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import powermodel as pm  # noqa: E402

ZONES = ["package", "dram"]


def truth(path, node):
    out = {}
    for r in csv.DictReader(open(path, newline="")):
        if r["metric"] == "kepler_node_cpu_watts" and r["node"] == node:
            out[(int(r["ts"]), r["zone"])] = float(r["value"])
    return out


def rows(snapshot, prom, node, step):
    """[(features, {zone: watts})] for every step with signals and truth."""
    span = open(os.path.join(snapshot, "span.txt")).read().strip()
    start, end = (int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
                  for s in span.split(".."))
    cpu, _, freq = pm.fetch(SimpleNamespace(prometheus=prom, node=node, step=step), start, end)
    y = truth(os.path.join(snapshot, "truth.csv"), node)
    out = []
    for ts in range(start + step, end + 1, step):
        now, prev = cpu.get(ts, {}), cpu.get(ts - step, {})
        ghz = pm.mean_ghz(freq.get(ts) or freq.get(ts - step) or {})
        labels = {z: y[(ts, z)] for z in ZONES if (ts, z) in y}
        if now and prev and ghz is not None and len(labels) == len(ZONES):
            out.append((pm.features(pm.usage_ratio(now, prev, step), ghz), labels))
    return out


def matrix(data, zone):
    names = list(data[0][0])
    return names, np.array([[x[n] for n in names] for x, _ in data]), np.array([y[zone] for _, y in data])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot", action="append", required=True, metavar="DIR=PROMETHEUS_URL")
    ap.add_argument("--nodes", default="sut-01,sut-02")
    ap.add_argument("--step", type=int, default=10)
    ap.add_argument("--out", default=pm.COEF_FILE)
    a = ap.parse_args()

    data = {}  # (snapshot, node) -> rows
    for spec in a.snapshot:
        snapshot, _, prom = spec.partition("=")
        name = os.path.basename(snapshot.rstrip("/"))
        for node in a.nodes.split(","):
            data[(name, node)] = rows(snapshot, prom, node, a.step)
            print(f"{name}/{node}: {len(data[(name, node)])} steps", file=sys.stderr)

    snaps = sorted({s for s, _ in data})
    if len(snaps) > 1:
        print("\nheld-out MAE in watts (trained on the other snapshots):")
        for zone in ZONES:
            for held in snaps:
                train = [r for (s, _), rs in data.items() if s != held for r in rs]
                names, X, Y = matrix(train, zone)
                m = LinearRegression().fit(X, Y)
                for (s, node), rs in sorted(data.items()):
                    if s == held and rs:
                        _, Xt, Yt = matrix(rs, zone)
                        print(f"  {zone:<8} {s}/{node:<8} {np.mean(np.abs(m.predict(Xt) - Yt)):7.3f}")

    everything = [r for rs in data.values() for r in rs]
    coef = {}
    for zone in ZONES:
        names, X, Y = matrix(everything, zone)
        m = LinearRegression().fit(X, Y)
        coef[zone] = {"intercept": round(float(m.intercept_), 6),
                      "weights": {n: round(float(w), 6) for n, w in zip(names, m.coef_)}}
    with open(a.out, "w") as f:
        json.dump(coef, f, indent=2)
        f.write("\n")
    print(f"\nwrote {a.out} from {len(everything)} steps:\n{json.dumps(coef, indent=2)}")


if __name__ == "__main__":
    main()
