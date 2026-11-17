#!/usr/bin/env python3
"""Turn a finished tools/lab_repeat.sh run into a snapshot directory. Stdlib only.

    tools/lab_fetch.py lab-1118-perf                       # newest run on the gateway
    tools/lab_fetch.py lab-1118-perf --run lab-runs/20261118-0900/ --role test

Downloads the run from the lab gateway (ssh host "lab", as in our ~/.ssh/config)
and builds snapshots/<name>/ like the other lab runs: tsdb/ (the in-cluster
Prometheus snapshot, without the files Prometheus rewrites when serving),
logs/, truth.csv and span.txt (tools/make_truth.py), windows.csv
(tools/lab_windows.py), hw.env, and run.json with what the run was:

- node: the SUT behind the cp node in runs.log (cp-01 runs sut-01);
- tuning: detected from the data, since Slurm forgets it after the booking.
  Clean-room pins every core at one frequency, so a spread of under 50 MHz
  over the whole run reads as clean-room, anything else as default;
- perf: whether lab_repeat.sh ran with --perf.

Then it prints the snapshots/index.csv rows to add, or appends them with
--role (train, test or course; a mixed job is always added as locked).
"""
import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "python"))
import labdata  # noqa: E402

HW = ('POWERMODEL_CPU_MODEL="Intel(R) Xeon(R) Silver 4114 CPU @ 2.20GHz"\n'
      'POWERMODEL_MACHINE_TYPE="X11SPi-TF"\n')
RUNTIME_FILES = ("lock", "wal", "chunks_head", "queries.active")


def sh(*cmd, capture=False):
    r = subprocess.run(cmd, check=True, text=True, capture_output=capture)
    return r.stdout.strip() if capture else None


def query_range(url, expr, start, end, step):
    q = urllib.parse.urlencode({"query": expr, "start": start, "end": end, "step": step})
    with urllib.request.urlopen(f"{url}/api/v1/query_range?{q}", timeout=60) as r:
        return [float(v) for s in json.load(r)["data"]["result"] for _, v in s["values"]]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", help="snapshot directory to create under snapshots/, e.g. lab-1118-perf")
    ap.add_argument("--run", help="run directory on the gateway (default: the newest under lab-runs/)")
    ap.add_argument("--host", default="lab")
    ap.add_argument("--role", choices=["train", "test", "course"], help="append index.csv rows with this role")
    ap.add_argument("--force", action="store_true", help="replace an existing snapshots/<name>")
    a = ap.parse_args()

    dest = os.path.join(labdata.SNAPSHOTS, a.name)
    if os.path.exists(dest) and not a.force:
        sys.exit(f"{dest} exists (use --force to replace it)")
    run = a.run or sh("ssh", a.host, "ls -td lab-runs/*/ | head -1", capture=True)
    run = run.rstrip("/") + "/"
    print(f"==> fetching {a.host}:{run}", flush=True)

    with tempfile.TemporaryDirectory() as tmp:
        sh("scp", "-q", f"{a.host}:{run}*", tmp)
        runs_log = open(os.path.join(tmp, "runs.log")).read()
        if " done" not in runs_log:
            sys.exit(f"{run}runs.log has no 'done' line: the run is still going or was cut off")
        if not os.path.exists(os.path.join(tmp, "prometheus.tar.gz")):
            sys.exit(f"{run} has no prometheus.tar.gz: the snapshot step failed (see runs.log)")
        if os.path.exists(dest):
            shutil.rmtree(dest)
        os.makedirs(os.path.join(dest, "logs"))
        sh("tar", "xzf", os.path.join(tmp, "prometheus.tar.gz"), "-C", tmp)
        snap = [d for d in glob.glob(os.path.join(tmp, "*")) if os.path.isdir(d)]
        if len(snap) != 1:
            sys.exit(f"expected one snapshot directory in prometheus.tar.gz, found {len(snap)}")
        shutil.copytree(snap[0], os.path.join(dest, "tsdb"),
                        ignore=lambda d, names: [n for n in names if d == snap[0] and n in RUNTIME_FILES])
        for f in glob.glob(os.path.join(tmp, "*.log")):
            shutil.copy(f, os.path.join(dest, "logs"))

    first = runs_log.splitlines()[0].split()
    cp = next((p.split("=", 1)[1] for p in first if p.startswith("node=")), "")
    node = "sut-" + cp.split("-")[-1] if cp.startswith("cp-") else sys.exit(f"no node=cp-NN in {first}")
    perf = "yes" if "perf=1" in first else "no"
    starts = [l.split()[0] for l in runs_log.splitlines() if l.split()[3:4] == ["apply"]]
    end = next((l.split()[0] for l in runs_log.splitlines() if "captured" in l), runs_log.splitlines()[-1].split()[0])
    rng = f"{starts[0]}..{end}"

    with labdata.serve([a.name]) as urls:
        url = urls[a.name]
        sh(sys.executable, os.path.join(HERE, "make_truth.py"), "--prometheus", url, "--node", node, "--range", rng, dest)
        lo, hi = labdata.ts(starts[0]), labdata.ts(end)
        freq = query_range(url, f'node_cpu_scaling_frequency_hertz{{node="{node}"}}', lo, hi, 60)
    if not freq:
        tuning, how = "unknown", "no node_cpu_scaling_frequency_hertz in the snapshot"
    elif max(freq) - min(freq) < 50e6:
        tuning, how = "clean-room", f"every core at {min(freq) / 1e9:.2f} GHz for the whole run"
    else:
        tuning, how = "default", f"core frequency ranged {min(freq) / 1e9:.2f}-{max(freq) / 1e9:.2f} GHz"

    logs = sorted(glob.glob(os.path.join(dest, "logs", "r*-*.log")))
    with open(os.path.join(dest, "windows.csv"), "w") as f:
        subprocess.run([sys.executable, os.path.join(HERE, "lab_windows.py"), "--node", node, *logs], check=True, stdout=f)
    with open(os.path.join(dest, "hw.env"), "w") as f:
        f.write(HW + f'POWERMODEL_TUNING="{tuning}"\n')
    jobs = labdata.job_windows(a.name)
    info = {
        "snapshot": a.name, "fetched_from": f"{a.host}:{run}", "node": node, "cp": cp,
        "tuning": tuning, "tuning_detected_from": how, "perf": perf, "range": rng,
        "jobs": [{"job": j, "round": r, "start": datetime.fromtimestamp(s, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "end": datetime.fromtimestamp(e, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")} for j, r, s, e in jobs],
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with open(os.path.join(dest, "run.json"), "w") as f:
        json.dump(info, f, indent=2)
        f.write("\n")

    names = sorted({j for j, *_ in jobs})
    rows = [[a.name, node, "*", tuning, perf, a.role or "ROLE", ""]]
    if "mixed" in names:
        rows.append([a.name, node, "mixed", tuning, perf, "locked", "new workload"])
    print(f"\n==> {dest}: {node}, tuning {tuning} ({how}), perf {perf}, "
          f"{len(jobs)} jobs ({', '.join(names)})")
    if a.role:
        with open(labdata.REGISTRY, "a") as f:
            for r in rows:
                f.write(",".join(r) + "\n")
        print(f"appended to {labdata.REGISTRY}:")
    else:
        print(f"add to {labdata.REGISTRY} (or rerun with --role):")
    for r in rows:
        print("  " + ",".join(r))


if __name__ == "__main__":
    main()
