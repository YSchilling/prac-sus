"""Which lab data exists, which part is training or test, and serving it. Stdlib only.

snapshots/index.csv has one row per snapshot, node and job:

    snapshot,node,job,tuning,perf,role,note
    lab-1117-perf,sut-01,*,default,yes,train,
    lab-1117-perf,sut-01,mixed,default,yes,locked,new workload

job "*" covers every job of that snapshot and node without a row of its
own, so the second row keeps mixed out of training. A job written as
"<job>/r<N>" (sqlite-app/r2) overrides both for that round alone. A job is a workload
name as tools/lab_repeat.sh logs it (cpu-bound, memory-bound, io-bound,
mixed); a snapshot without logs/runs.log (the course snapshots) is one job
"*" over its span.txt. role is train, test, locked (looked at only on
purpose, see tools/lab_eval.py --locked) or course.

A selection is "key=value key=v1,v2": every key must match, any of its
values will do, e.g. "tuning=default perf=yes role=train".
"""
import contextlib
import csv
import os
import socket
import subprocess
import time
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPSHOTS = os.path.join(ROOT, "snapshots")
REGISTRY = os.path.join(SNAPSHOTS, "index.csv")
FIELDS = ["snapshot", "node", "job", "tuning", "perf", "role", "note"]
ROLES = {"train", "test", "locked", "course"}


def ts(s):
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp())


def load_registry(path=REGISTRY):
    with open(path, newline="") as f:
        rows = [dict(r) for r in csv.DictReader(f)]
    for r in rows:
        if r["role"] not in ROLES:
            raise ValueError(f"{path}: role {r['role']!r} for {r['snapshot']}/{r['node']}/{r['job']} is not one of {sorted(ROLES)}")
    return rows


def parse_select(expr):
    """[(key, {values})] from "key=v1,v2 key=v"."""
    out = []
    for part in (expr or "").split():
        key, sep, values = part.partition("=")
        if not sep or key not in FIELDS + ["round"]:
            raise ValueError(f"bad selection {part!r}: use key=value with key one of {FIELDS + ['round']}")
        out.append((key, set(values.split(","))))
    return out


def span(snapshot):
    with open(os.path.join(SNAPSHOTS, snapshot, "span.txt")) as f:
        a, b = f.read().strip().split("..")
    return ts(a), ts(b)


def job_windows(snapshot):
    """[(job, round, start, end)] from logs/runs.log (apply to complete), or
    [("*", 1, span)] for a snapshot without one."""
    path = os.path.join(SNAPSHOTS, snapshot, "logs", "runs.log")
    if not os.path.exists(path):
        return [("*", 1, *span(snapshot))]
    started, out = {}, []
    with open(path) as f:
        for line in f:
            p = line.split()
            if len(p) < 4 or not p[1].startswith("round=") or not p[2].startswith("job="):
                continue
            key = (p[2][4:], int(p[1][6:]))
            if p[3] == "apply":
                started[key] = ts(p[0])
            elif p[3] == "complete" and key in started:
                out.append((*key, started.pop(key), ts(p[0])))
    return out


def units(select=None, registry=None):
    """Every (snapshot, node, job, round) the registry describes, each with
    its row's fields and its time window, filtered by a selection."""
    registry = registry if registry is not None else load_registry()
    sel = parse_select(select) if isinstance(select, str) or select is None else select
    by_key = {(r["snapshot"], r["node"], r["job"]): r for r in registry}
    out = []
    for snap, node in dict.fromkeys((r["snapshot"], r["node"]) for r in registry):
        for job, rnd, start, end in job_windows(snap):
            row = (by_key.get((snap, node, f"{job}/r{rnd}")) or by_key.get((snap, node, job))
                   or by_key.get((snap, node, "*")))
            if row is None:
                continue
            u = {**row, "job": job, "round": str(rnd), "start": start, "end": end}
            if all(u[k] in vals for k, vals in sel):
                out.append(u)
    return out


def truth(snapshot, node):
    """{(ts, zone): watts} from a snapshot's truth.csv."""
    out = {}
    with open(os.path.join(SNAPSHOTS, snapshot, "truth.csv"), newline="") as f:
        for r in csv.DictReader(f):
            if r["metric"] == "kepler_node_cpu_watts" and r["node"] == node:
                out[(int(r["ts"]), r["zone"])] = float(r["value"])
    return out


def grid(snapshot, start, end, step):
    """The truth grid's steps in [start, end]: span start plus multiples of step."""
    origin = span(snapshot)[0]
    first = origin + max(0, -(-(start - origin) // step)) * step
    return range(first, end + 1, step)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def serve(snapshots, timeout=120):
    """{snapshot: prometheus_url} with tools/prom.sh serving each snapshot on a
    free port; every server is stopped on exit."""
    procs, urls = [], {}
    try:
        for snap in dict.fromkeys(snapshots):
            port = _free_port()
            log = open(os.path.join(SNAPSHOTS, snap, ".prom.log"), "w")
            procs.append(subprocess.Popen([os.path.join(ROOT, "tools", "prom.sh"), os.path.join(SNAPSHOTS, snap)],
                                          env={**os.environ, "PORT": str(port)}, stdout=log, stderr=subprocess.STDOUT,
                                          start_new_session=True))
            urls[snap] = f"http://127.0.0.1:{port}"
        deadline = time.time() + timeout
        for snap, url in urls.items():
            while True:
                try:
                    with urllib.request.urlopen(url + "/-/ready", timeout=2):
                        break
                except OSError:
                    if time.time() > deadline:
                        raise RuntimeError(f"Prometheus for {snap} not ready after {timeout} s, see snapshots/{snap}/.prom.log")
                    time.sleep(1)
        yield urls
    finally:
        for p in procs:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(p.pid, 15)
        for p in procs:
            with contextlib.suppress(subprocess.TimeoutExpired):
                p.wait(timeout=20)
