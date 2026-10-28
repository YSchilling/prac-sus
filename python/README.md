# examples/python

A linear-regression power model in one Python file, `powermodel.py`. It uses
only the standard library. The model is `features()` and `predict()`, with
per-zone coefficients in `coef.json`; `train.py` fits them. Everything around
it reads counters from Prometheus and writes the shapes in `CONTRACT.md`.

Needs Python 3.11+. Training needs the repo's `.venv` (numpy, scikit-learn;
`uv venv --python 3.13 .venv && uv pip install --python .venv numpy scikit-learn`). The snapshots are described in
`snapshots/README.md`; below, `$SNAP` is the absolute path of an unpacked one
and `sut-02` its node.

## The loop

**1. Look at the data.** From the repo root, serve the snapshot and leave it
running:

```sh
tools/prom.sh $SNAP        # web UI on http://127.0.0.1:19090
```

The data is in the past, so use the Graph tab with a time range inside the
snapshot (`span.txt` for a stripped one). The raw training snapshot also has
the measured power, `kepler_node_cpu_watts`, so you can see which signals
move with it.

**2. Change the model.** Per zone, watts = intercept + weights x
`features()`: the CPU usage ratio, its part above one half (hyperthreads add
less power once every physical core is busy), and the mean scaling frequency
in GHz. `predict()` applies `coef.json` and splits the result over pods. To
use another signal, fetch it in `fetch()` with a raw selector, filtered by
node:

```python
query_range(a.prometheus, f'node_vmstat_pgfault{{node="{a.node}"}}', start, end, a.step)
```

Then take its delta between `ts - step` and `ts` in `at()`, the way
`usage_ratio()` does. Selectors are metric names with label matchers, no
`rate()` or `sum()`: the arithmetic lives in Python, as it would when
reading `/proc`. Add it to `features()` and retrain.

**3. Train.** Serve each training snapshot on its own port and fit, from the
repo root:

```sh
tools/prom.sh $PWD/snapshots/stress-1008 &
PORT=19091 tools/prom.sh $PWD/snapshots/teastore-1008 &
.venv/bin/python python/train.py \
    --snapshot snapshots/stress-1008=http://127.0.0.1:19090 \
    --snapshot snapshots/teastore-1008=http://127.0.0.1:19091
```

It prints the error on each snapshot when held out, the number that predicts
how the model does on an unseen workload, then fits on everything and writes
`coef.json`.

**4. Score.** With the snapshot served, from `python/`:

```sh
set -a; . $SNAP/hw.env; set +a     # POWERMODEL_CPU_MODEL, POWERMODEL_MACHINE_TYPE
python3 powermodel.py --mode offline --node sut-02 --range "$(cat $SNAP/span.txt)"
python3 ../tools/score.py predictions.csv --truth "$SNAP/truth.csv"
```

The score shows MAE, RMSE, MAPE and energy error per zone. Scoring a snapshot
the model was trained on flatters it; trust the held-out numbers from step 3.

Prometheus writes a `lock` and new WAL segments into a snapshot while serving
it; delete those untracked files afterwards rather than committing them.

**5. Predict the held-out snapshot.** It has no truth, so run only the
`python3` line and hand in `predictions.csv`. Set `GROUP` at the top of the
file to your group's tag first.

**6. Deploy.** Without `--mode offline` the script serves
`powermodel_node_watts` and `powermodel_pod_watts` on `:9270`.

```sh
docker build -t powermodel-python:dev examples/python      # from the repo root
```

On a [power-kind](../../power-kind) cluster:

```sh
kind load docker-image powermodel-python:dev --name power-kind
kubectl --context kind-power-kind apply -k examples/python/deploy/overlays/kind
curl -sG http://localhost:30090/api/v1/query --data-urlencode 'query=powermodel_node_watts'
```

kind proves that the deployment works, nothing more: its power is synthetic.
On bare metal, set the two hardware values in
`deploy/overlays/bare-metal/kustomization.yaml`, label the nodes to model
`role=sut`, and apply that overlay instead.

## Flags

The flags from `CONTRACT.md`, plus `--listen` (online, default `:9270`) and
`--out` (offline, default `predictions.csv`). Each can also be set as
`POWERMODEL_<FLAG>`.
