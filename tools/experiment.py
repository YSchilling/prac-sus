#!/usr/bin/env python3
"""Compare model variants on the registry's lab data in one run.

    .venv/bin/python tools/experiment.py                       # all variants
    .venv/bin/python tools/experiment.py --variants baseline smooth3 --csv results.csv --steps steps.csv

Each variant is a feature function over one job's steps in time order plus the
inputs per zone; all of them are fitted with ordinary least squares, like
python/train.py. Data is the cache-miss data of snapshots/index.csv
(--select, default "tuning=default perf=yes"); locked jobs are never used.

Three numbers per variant, in order of importance:

1. New workload, realistic: leave one training workload out at a time, fit
   on the rest, and pool the held-out steps of every workload whose load the
   others still cover. This is the stand-in for the other team's workload,
   which will be new to the model but inside the range we train on. A change
   counts only if it improves this, in MAE and in energy error.
2. Test rounds: fit on all training data, score role=test (repeat rounds of
   the same workloads). A check that nothing got worse, not a decision.
3. Extrapolation: the held-out folds of --extrapolation (default cpu and
   memory). Without cpu-bound nothing covers the upper CPU range, without
   memory-bound nothing teaches the cache-miss weight; we always train with
   both, so these folds only show how a variant extrapolates.

Variants only ever look at a job's earlier steps, never later ones, so each
could run online as well (state between steps is allowed).
"""
import argparse
import csv
import math
import os
import sys
from types import SimpleNamespace

import numpy as np
from sklearn.linear_model import LinearRegression

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "python"))
sys.path.insert(0, HERE)
import labdata  # noqa: E402
import powermodel as pm  # noqa: E402
import score  # noqa: E402

ZONES = ["package", "dram"]
WORKLOAD = {"cpu-bound": "cpu", "memory-bound": "memory", "io-bound": "io", "mixed": "mixed", "sqlite-app": "sqlite"}


# ---------- data ----------

def load_jobs(select, step):
    """[{unit fields, steps: [{ts, ratio, miss_gbps, y: {zone: W}}]}] in time order."""
    units = [u for u in labdata.units(select) if u["role"] in ("train", "test")]
    if not units:
        sys.exit(f"--select {select!r} matches no train or test jobs")
    jobs = []
    with labdata.serve(u["snapshot"] for u in units) as urls:
        truth = {}
        for u in units:
            key = (u["snapshot"], u["node"])
            y = truth.setdefault(key, labdata.truth(*key))
            a = SimpleNamespace(prometheus=urls[u["snapshot"]], node=u["node"], step=step)
            grid = labdata.grid(u["snapshot"], u["start"], u["end"], step)
            cpu, _ = pm.fetch(a, grid.start - step, u["end"])
            misses = pm.fetch_misses(a, grid.start - step, u["end"])
            steps = []
            for ts in grid:
                x = pm.features(cpu.get(ts, {}), cpu.get(ts - step, {}), step)
                m = pm.miss_features(misses.get(ts, {}), misses.get(ts - step, {}), step)
                if x and m and all((ts, z) in y for z in ZONES):
                    steps.append({"ts": ts, "ratio": x["ratio"], "miss_gbps": m["miss_gbps"],
                                  "y": {z: y[(ts, z)] for z in ZONES}})
            jobs.append({**u, "workload": WORKLOAD.get(u["job"], u["job"]), "steps": steps})
            print(f"  {u['snapshot']}/{u['node']}/{u['job']}/r{u['round']} ({u['role']}): {len(steps)} steps", file=sys.stderr)
    return jobs


# ---------- variants ----------

def awake(r, scale):
    return 1 - math.exp(-r / scale)


def trailing_mean(values, k):
    """Mean of each value and up to k-1 values before it."""
    return [sum(values[max(0, i - k + 1):i + 1]) / len(values[max(0, i - k + 1):i + 1]) for i in range(len(values))]


def make_features(scale=pm.AWAKE_SCALE, smooth=1):
    """Feature function: per-step dicts with ratio, awake, miss_gbps, using
    the trailing mean over `smooth` steps of ratio and cache misses."""
    def fn(steps):
        r = trailing_mean([s["ratio"] for s in steps], smooth)
        m = trailing_mean([s["miss_gbps"] for s in steps], smooth)
        return [{"ratio": ri, "awake": awake(ri, scale), "miss_gbps": mi} for ri, mi in zip(r, m)]
    return fn


VARIANTS = {
    "baseline": {"about": "current coef-perf.json form: ratio, awake (scale 0.1), cache misses, one step",
                 "features": make_features()},
    "smooth2": {"about": "trailing mean of ratio and cache misses over 2 steps (20 s)",
                "features": make_features(smooth=2)},
    "smooth3": {"about": "trailing mean of ratio and cache misses over 3 steps (30 s)",
                "features": make_features(smooth=3)},
    "awake-fit": {"about": "awake scale chosen on the realistic held-out workloads of the training data",
                  "features": None, "smooth": 1},  # filled in by fit_awake_scale()
    "smooth2+awake-fit": {"about": "smooth2 and a fitted awake scale together", "features": None, "smooth": 2},
}
TUNED = {"awake-fit", "smooth2+awake-fit"}
ZONE_FEATURES = pm.PERF_ZONE_FEATURES


# ---------- fitting and scoring ----------

def matrix(jobs, features, zone):
    X, Y = [], []
    for j in jobs:
        for f, s in zip(features(j["steps"]), j["steps"]):
            X.append([f[n] for n in ZONE_FEATURES[zone]])
            Y.append(s["y"][zone])
    return np.array(X), np.array(Y)


def fit(jobs, features):
    return {z: LinearRegression().fit(*matrix(jobs, features, z)) for z in ZONES}


def predict(models, jobs, features, zone):
    """[(predicted, measured)] over the jobs' steps."""
    X, Y = matrix(jobs, features, zone)
    if not len(Y):
        return []
    return list(zip(np.maximum(models[zone].predict(X), 0.0), Y))


def lowo(jobs, features, extrapolation, tune=None):
    """{(held-out workload, zone): metrics} leaving one training workload out,
    plus ("realistic", zone): metrics pooled over the held-out steps of every
    workload not in extrapolation. tune, if given, picks the feature function
    from the remaining training jobs alone (nested), so a tuned variant never
    sees the workload it is scored on."""
    train = [j for j in jobs if j["role"] == "train"]
    out, pooled = {}, {z: [] for z in ZONES}
    for held in sorted({j["workload"] for j in train}):
        rest = [j for j in train if j["workload"] != held]
        if tune:
            features = tune(rest)
        models = fit(rest, features)
        for z in ZONES:
            pairs = predict(models, [j for j in train if j["workload"] == held], features, z)
            out[(held, z)] = score.metrics(pairs)
            if held not in extrapolation:
                pooled[z].extend(pairs)
    for z in ZONES:
        out[("realistic", z)] = score.metrics(pooled[z])
    return out


def fit_awake_scale(jobs, extrapolation, smooth=1):
    """The scale with the lowest realistic held-out MAE (package + DRAM)
    among the given jobs' training workloads."""
    best = None
    for scale in np.geomspace(0.01, 0.5, 30):
        res = lowo(jobs, make_features(scale=float(scale), smooth=smooth), extrapolation)
        err = res[("realistic", "package")][1] + res[("realistic", "dram")][1]
        if best is None or err < best[0]:
            best = (err, float(scale))
    return best[1]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--select", default="tuning=default perf=yes")
    ap.add_argument("--variants", nargs="+", default=list(VARIANTS), choices=list(VARIANTS))
    ap.add_argument("--step", type=int, default=10)
    ap.add_argument("--extrapolation", default="cpu,memory",
                    help="held-out workloads that only show extrapolation (default cpu,memory)")
    ap.add_argument("--csv", help="write every result row here")
    ap.add_argument("--steps", help="write test-step predictions of every variant here")
    a = ap.parse_args()

    print("loading data:", file=sys.stderr)
    jobs = load_jobs(a.select, a.step)
    extrap = set(a.extrapolation.split(",")) if a.extrapolation else set()
    for name in TUNED & set(a.variants):
        sm = VARIANTS[name]["smooth"]
        scale = fit_awake_scale(jobs, extrap, sm)
        VARIANTS[name]["features"] = make_features(scale=scale, smooth=sm)
        VARIANTS[name]["about"] = ((f"trailing mean over {sm} steps; " if sm > 1 else "") +
                                   f"awake scale fitted on the training workloads ({scale:.3f} on all of them); "
                                   "chosen again without each held-out workload; so it never sees what it is scored on")
        print(f"{name}: scale {scale:.3f}", file=sys.stderr)

    rows, step_rows = [], []
    train = [j for j in jobs if j["role"] == "train"]
    test = [j for j in jobs if j["role"] == "test"]
    for name in a.variants:
        feats = VARIANTS[name]["features"]
        sm = VARIANTS[name].get("smooth", 1)
        tune = (lambda rest, sm=sm: make_features(scale=fit_awake_scale(rest, extrap, sm), smooth=sm)) if name in TUNED else None
        for (held, z), m in lowo(jobs, feats, extrap, tune).items():
            kind = "realistic" if held == "realistic" else "extrapolation" if held in extrap else "new-workload"
            rows.append({"variant": name, "eval": kind, "workload": "all" if held == "realistic" else held, "node": "both", "zone": z,
                         **dict(zip(("n", "mae", "rmse", "mape", "energy"), m))})
        models = fit(train, feats)
        for z in ZONES:
            for (wl, node) in sorted({(j["workload"], j["node"]) for j in test}):
                sel = [j for j in test if j["workload"] == wl and j["node"] == node]
                rows.append({"variant": name, "eval": "test", "workload": wl, "node": node, "zone": z,
                             **dict(zip(("n", "mae", "rmse", "mape", "energy"), score.metrics(predict(models, sel, feats, z))))})
            rows.append({"variant": name, "eval": "test", "workload": "all", "node": "both", "zone": z,
                         **dict(zip(("n", "mae", "rmse", "mape", "energy"), score.metrics(predict(models, test, feats, z))))})
        if a.steps:
            for j in test:
                f = feats(j["steps"])
                for z in ZONES:
                    X = np.array([[fi[n] for n in ZONE_FEATURES[z]] for fi in f])
                    p = np.maximum(models[z].predict(X), 0.0)
                    t0 = j["steps"][0]["ts"]
                    for s, pi in zip(j["steps"], p):
                        step_rows.append({"variant": name, "run": f"{j['node']}-{j['workload']}-r{j['round']}", "node": j["node"],
                                          "workload": j["workload"], "zone": z, "minute": f"{(s['ts'] - t0) / 60:.3f}",
                                          "measured_w": f"{s['y'][z]:.3f}", "pred_w": f"{pi:.3f}"})

    # print
    names = a.variants
    by = {(r["variant"], r["eval"], r["workload"], r["node"], r["zone"]): r for r in rows}
    print("\nvariants:")
    for n in names:
        print(f"  {n:10} {VARIANTS[n]['about']}")
    cell = lambda r: f"{r['mae']:9.2f} ({r['energy']:+5.1f}%)"
    for z in ZONES:
        print(f"\n{z}: MAE W (energy error %)")
        print(f"  {'':34}" + "".join(f"{n:>18}" for n in names))
        print(f"  {'1. NEW WORKLOAD, realistic (pooled)':34}" + "".join(cell(by[(n, 'realistic', 'all', 'both', z)]) for n in names))
        for held in sorted({r["workload"] for r in rows if r["eval"] == "new-workload"}):
            print(f"  {'   leave out ' + held:34}" + "".join(cell(by[(n, 'new-workload', held, 'both', z)]) for n in names))
        print(f"  {'2. test rounds, all':34}" + "".join(cell(by[(n, 'test', 'all', 'both', z)]) for n in names))
        for (wl, node) in sorted({(r["workload"], r["node"]) for r in rows if r["eval"] == "test" and r["workload"] != "all"}):
            print(f"  {'   ' + node + ' ' + wl:34}" + "".join(cell(by[(n, 'test', wl, node, z)]) for n in names))
        for held in sorted({r["workload"] for r in rows if r["eval"] == "extrapolation"}):
            print(f"  {'3. extrapolation: leave out ' + held:34}" + "".join(cell(by[(n, 'extrapolation', held, 'both', z)]) for n in names))

    if a.csv:
        with open(a.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["variant", "about", "eval", "workload", "node", "zone", "n", "mae", "rmse", "mape", "energy"])
            w.writeheader()
            for r in rows:
                w.writerow({**r, "about": VARIANTS[r["variant"]]["about"],
                            **{k: f"{r[k]:.4f}" for k in ("mae", "rmse", "mape", "energy")}})
        print(f"\nwrote {len(rows)} rows to {a.csv}")
    if a.steps:
        with open(a.steps, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(step_rows[0]))
            w.writeheader()
            w.writerows(step_rows)
        print(f"wrote {len(step_rows)} step rows to {a.steps}")


if __name__ == "__main__":
    main()
