#!/usr/bin/env python3
"""Score coefficient files on lab data from snapshots/index.csv. Stdlib only.

    tools/lab_eval.py --coef python/coef-default-profile.json python/coef-perf.json
    tools/lab_eval.py --coef python/coef-perf.json --select "perf=yes" --locked --phases

Serves the selected snapshots itself, predicts every step of every selected
job with powermodel.at() (what --mode offline runs) and scores it with
tools/score.py's metrics(), so the numbers match the hand-in path. Prints one
row per snapshot, node, job, round and zone, then a pooled summary per
coefficient file and zone: MAE and RMSE in watts, MAPE in %, and energy error
(total predicted minus total measured energy, in %).

The default selection is role=test. Jobs with role locked are left out unless
--locked is given: look at them at milestones only, or tuning against them
stops them being a fair test. A coefficient file that needs cache misses is
scored only where the snapshot has them (perf=yes); elsewhere it shows "-".
--phases adds a row per phase from the snapshot's windows.csv. --csv writes
every row to a file as well.
"""
import argparse
import csv
import os
import sys
from collections import defaultdict
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "python"))
sys.path.insert(0, HERE)
import labdata  # noqa: E402
import powermodel as pm  # noqa: E402
import score  # noqa: E402

ZONES = ["package", "dram"]


def phase_windows(snapshot, node):
    """{(job, round): [(phase, start, end)]} from windows.csv, if there is one."""
    path = os.path.join(labdata.SNAPSHOTS, snapshot, "windows.csv")
    out = defaultdict(list)
    if os.path.exists(path):
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                if r["node"] == node:
                    job, _, rnd = r["run"].partition("/r")
                    out[(job, rnd)].append((r["phase"], labdata.ts(r["start"]), labdata.ts(r["end"])))
    return out


def predict_unit(u, url, coefs, step):
    """{coef: {(ts, zone): watts}} for one job, and the truth for it."""
    a = SimpleNamespace(prometheus=url, node=u["node"], step=step)
    steps = labdata.grid(u["snapshot"], u["start"], u["end"], step)
    cpu, pods = pm.fetch(a, steps.start - step, u["end"])
    misses = pm.fetch_misses(a, steps.start - step, u["end"]) if u["perf"] == "yes" else None
    out = {}
    for name, coef in coefs.items():
        if pm.needs_perf(coef) and misses is None:
            continue
        p = {}
        for ts in steps:
            rows = pm.at(coef, cpu, pods, ts, step, misses)
            if rows is not None:
                for zone, (node_w, _, _) in rows.items():
                    p[(ts, zone)] = node_w
        out[name] = p
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--coef", nargs="+", required=True, help="coefficient files to compare")
    ap.add_argument("--select", default="role=test", help='which jobs, e.g. "tuning=default node=sut-02" (default role=test)')
    ap.add_argument("--locked", action="store_true", help="also score jobs with role locked")
    ap.add_argument("--phases", action="store_true", help="add a row per phase (needs windows.csv)")
    ap.add_argument("--csv", help="also write every row to this file")
    ap.add_argument("--step", type=int, default=10)
    a = ap.parse_args()

    sel = labdata.parse_select(a.select)
    if a.locked and not any(k == "role" for k, _ in sel):
        sel.append(("role", {"train", "test", "locked", "course"}))
    elif a.locked:
        sel = [(k, v | {"locked"}) if k == "role" else (k, v) for k, v in sel]
    units = [u for u in labdata.units(sel) if a.locked or u["role"] != "locked"]
    if not units:
        sys.exit(f"nothing selected by {a.select!r}{' (locked jobs need --locked)' if not a.locked else ''}")
    coefs = {os.path.basename(p).removesuffix(".json"): pm.load_coef(p) for p in a.coef}

    results = []  # dicts: level, snapshot, node, job, round, phase, zone, coef, n, mae, rmse, mape, energy
    pooled = defaultdict(list)  # (coef, zone) -> pairs
    with labdata.serve(u["snapshot"] for u in units) as urls:
        truth_cache = {}
        for u in units:
            key = (u["snapshot"], u["node"])
            if key not in truth_cache:
                truth_cache[key] = labdata.truth(*key)
            y = truth_cache[key]
            preds = predict_unit(u, urls[u["snapshot"]], coefs, a.step)
            phases = phase_windows(*key).get((u["job"], u["round"]), []) if a.phases else []
            for name in coefs:
                for zone in ZONES:
                    base = dict(snapshot=u["snapshot"], node=u["node"], job=u["job"], round=u["round"], zone=zone, coef=name, role=u["role"])
                    if name not in preds:
                        results.append({**base, "level": "job", "phase": "", "n": 0})
                        continue
                    pairs = [(w, y[(ts, z)]) for (ts, z), w in sorted(preds[name].items()) if z == zone and (ts, z) in y]
                    if not pairs:
                        continue
                    pooled[(name, zone)].extend(pairs)
                    results.append({**base, "level": "job", "phase": "", **dict(zip(("n", "mae", "rmse", "mape", "energy"), score.metrics(pairs)))})
                    for phase, lo, hi in phases:
                        pp = [(w, y[(ts, z)]) for (ts, z), w in sorted(preds[name].items()) if z == zone and lo <= ts <= hi and (ts, z) in y]
                        if pp:
                            results.append({**base, "level": "phase", "phase": phase,
                                            **dict(zip(("n", "mae", "rmse", "mape", "energy"), score.metrics(pp)))})

    names = list(coefs)
    cell = lambda r: "-" if not r or not r.get("n") else f"{r['mae']:6.2f} {r['mape']:6.1f}% {r['energy']:+6.1f}%"
    head = f"{'snapshot':<20}{'node':<8}{'job':<14}{'rnd':<4}{'zone':<9}{'n':>5}  " + "  ".join(f"{n[:26]:>26}" for n in names)
    print(f"MAE W, MAPE %, energy error % per coefficient file (selection {a.select!r}{', with locked' if a.locked else ''})\n")
    print(head)
    index = {(r["level"], r["snapshot"], r["node"], r["job"], r["round"], r["phase"], r["zone"], r["coef"]): r for r in results}
    for u in units:
        for zone in ZONES:
            keys = [("job", u["snapshot"], u["node"], u["job"], u["round"], "", zone, n) for n in names]
            rs = [index.get(k) for k in keys]
            n = max((r.get("n", 0) for r in rs if r), default=0)
            if not n:
                continue
            lock = "*" if u["role"] == "locked" else ""
            print(f"{u['snapshot']:<20}{u['node']:<8}{(u['job'] + lock)[:13]:<14}{u['round']:<4}{zone:<9}{n:>5}  " + "  ".join(f"{cell(r):>26}" for r in rs))
            if a.phases:
                for phase in dict.fromkeys(r["phase"] for r in results if r["level"] == "phase" and r["snapshot"] == u["snapshot"]
                                           and r["node"] == u["node"] and r["job"] == u["job"] and r["round"] == u["round"]):
                    prs = [index.get(("phase", u["snapshot"], u["node"], u["job"], u["round"], phase, zone, n_)) for n_ in names]
                    pn = max((r.get("n", 0) for r in prs if r), default=0)
                    print(f"{'':<20}{'':<8}{'  ' + phase[:24]:<26}{zone:<9}{pn:>5}  " + "  ".join(f"{cell(r):>26}" for r in prs))
    print("\npooled over every selected step" + (" (* = locked job)" if a.locked else ""))
    for zone in ZONES:
        cells = []
        for name in names:
            p = pooled.get((name, zone))
            m = dict(zip(("n", "mae", "rmse", "mape", "energy"), score.metrics(p))) if p else None
            cells.append(f"{cell(m):>26}")
        n = max((len(pooled.get((nm, zone), [])) for nm in names), default=0)
        print(f"{'all':<20}{'':<8}{'':<14}{'':<4}{zone:<9}{n:>5}  " + "  ".join(cells))
    print("\nA coefficient file is pooled only over the steps it could score, so its n can differ.")

    if a.csv:
        fields = ["level", "snapshot", "node", "job", "round", "phase", "zone", "role", "coef", "n", "mae", "rmse", "mape", "energy"]
        with open(a.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(results)
        print(f"wrote {len(results)} rows to {a.csv}")


if __name__ == "__main__":
    main()
