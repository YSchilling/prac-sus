#!/usr/bin/env python3
"""Fit powermodel.py's per-zone linear regression and write coef.json.

    .venv/bin/python python/train.py --select "tuning=default perf=yes role=train" --perf \
        --holdout job --out python/coef-perf.json

--select picks training data from snapshots/index.csv (python/labdata.py has
the syntax) and serves the snapshots itself. Rows with role locked are
never trained on. The older form still works, with snapshots served by hand
(tools/prom.sh, PORT=... for a second one):

    .venv/bin/python python/train.py \
        --snapshot snapshots/stress-1008=http://127.0.0.1:19090 \
        --snapshot snapshots/teastore-1008=http://127.0.0.1:19091

Features come from powermodel.features(), the code the model runs, so
training and prediction cannot drift apart. Labels come from the snapshot's
truth.csv; the model itself never reads it.

Before fitting on everything, each group (a snapshot, or with --holdout job a
workload) is held out once and predicted by a model trained on the others:
the held-out group is new to the model, so that is the number to watch.
"""
import argparse
import csv
import json
import os
import sys
from types import SimpleNamespace

import numpy as np
from sklearn.linear_model import LinearRegression

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import labdata  # noqa: E402
import powermodel as pm  # noqa: E402

ZONES = ["package", "dram"]


def truth(path, node):
    out = {}
    for r in csv.DictReader(open(path, newline="")):
        if r["metric"] == "kepler_node_cpu_watts" and r["node"] == node:
            out[(int(r["ts"]), r["zone"])] = float(r["value"])
    return out


def rows(snapshot, prom, node, step, perf=False, windows=None):
    """[(features, {zone: watts})] for every step with signals and truth.
    perf adds miss_gbps and drops steps without cache-miss samples; windows,
    [(start, end)], keeps only steps inside one of them."""
    start, end = (labdata.ts(s) for s in open(os.path.join(snapshot, "span.txt")).read().strip().split(".."))
    a = SimpleNamespace(prometheus=prom, node=node, step=step)
    cpu, _ = pm.fetch(a, start, end)
    misses = pm.fetch_misses(a, start, end) if perf else {}
    y = truth(os.path.join(snapshot, "truth.csv"), node)
    out = []
    for ts in range(start + step, end + 1, step):
        if windows is not None and not any(lo <= ts <= hi for lo, hi in windows):
            continue
        now, prev = cpu.get(ts, {}), cpu.get(ts - step, {})
        x = pm.features(now, prev, step)
        if x is not None and perf:
            m = pm.miss_features(misses.get(ts, {}), misses.get(ts - step, {}), step)
            x = None if m is None else {**x, **m}
        labels = {z: y[(ts, z)] for z in ZONES if (ts, z) in y}
        if x is not None and len(labels) == len(ZONES):
            out.append((x, labels))
    return out


def matrix(data, zone, zone_features=pm.ZONE_FEATURES):
    names = zone_features[zone]
    return names, np.array([[x[n] for n in names] for x, _ in data]), np.array([y[zone] for _, y in data])


def from_registry(select, holdout, step, perf):
    """{(group, node): rows} for the selected units, serving their snapshots."""
    units = labdata.units(select)
    if not units:
        sys.exit(f"--select {select!r} matches nothing in {labdata.REGISTRY}")
    locked = [u for u in units if u["role"] == "locked"]
    if locked:
        sys.exit(f"--select includes {len(locked)} locked job(s), e.g. {locked[0]['snapshot']}/{locked[0]['job']}: "
                 "locked data is never trained on")
    if perf and any(u["perf"] != "yes" for u in units):
        sys.exit("--perf needs perf=yes on every selected snapshot (add perf=yes to --select)")
    data = {}
    with labdata.serve(u["snapshot"] for u in units) as urls:
        for snap, node in dict.fromkeys((u["snapshot"], u["node"]) for u in units):
            mine = [u for u in units if u["snapshot"] == snap and u["node"] == node]
            for group in dict.fromkeys(u["job"] if holdout == "job" else snap for u in mine):
                windows = [(u["start"], u["end"]) for u in mine if (u["job"] if holdout == "job" else snap) == group]
                rs = rows(os.path.join(labdata.SNAPSHOTS, snap), urls[snap], node, step, perf, windows)
                data.setdefault((group, node), []).extend(rs)
                print(f"{snap}/{node}/{group}: {len(rs)} steps", file=sys.stderr)
    return data


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--select", help="training data from snapshots/index.csv, e.g. \"tuning=default role=train\"")
    src.add_argument("--snapshot", action="append", metavar="DIR=PROMETHEUS_URL")
    ap.add_argument("--holdout", choices=["snapshot", "job"], default="snapshot",
                    help="what the held-out check leaves out in turn (--select only; default snapshot)")
    ap.add_argument("--nodes", default="sut-01,sut-02", help="--snapshot only")
    ap.add_argument("--step", type=int, default=10)
    ap.add_argument("--out", default=pm.COEF_FILE)
    ap.add_argument("--perf", action="store_true",
                    help="add the cache-miss feature (needs node_perf_cache_misses_total in every snapshot)")
    a = ap.parse_args()
    zf = pm.PERF_ZONE_FEATURES if a.perf else pm.ZONE_FEATURES

    if a.select:
        data = from_registry(a.select, a.holdout, a.step, a.perf)
    else:
        data = {}  # (snapshot, node) -> rows
        for spec in a.snapshot:
            snapshot, _, prom = spec.partition("=")
            name = os.path.basename(snapshot.rstrip("/"))
            for node in a.nodes.split(","):
                data[(name, node)] = rows(snapshot, prom, node, a.step, a.perf)
                print(f"{name}/{node}: {len(data[(name, node)])} steps", file=sys.stderr)

    groups = sorted({g for g, _ in data})
    if len(groups) > 1:
        print(f"\nheld-out MAE in watts (trained on the other {'workloads' if a.holdout == 'job' and a.select else 'snapshots'}):")
        for zone in ZONES:
            for held in groups:
                train = [r for (g, _), rs in data.items() if g != held for r in rs]
                names, X, Y = matrix(train, zone, zf)
                m = LinearRegression().fit(X, Y)
                for (g, node), rs in sorted(data.items()):
                    if g == held and rs:
                        _, Xt, Yt = matrix(rs, zone, zf)
                        print(f"  {zone:<8} {g}/{node:<8} {np.mean(np.abs(np.maximum(m.predict(Xt), 0.0) - Yt)):7.3f}")

    everything = [r for rs in data.values() for r in rs]
    coef = {}
    for zone in ZONES:
        names, X, Y = matrix(everything, zone, zf)
        m = LinearRegression().fit(X, Y)
        coef[zone] = {"intercept": round(float(m.intercept_), 6),
                      "weights": {n: round(float(w), 6) for n, w in zip(names, m.coef_)}}
    with open(a.out, "w") as f:
        json.dump(coef, f, indent=2)
        f.write("\n")
    print(f"\nwrote {a.out} from {len(everything)} steps:\n{json.dumps(coef, indent=2)}")


if __name__ == "__main__":
    main()
