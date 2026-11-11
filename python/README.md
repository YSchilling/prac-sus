# examples/python

A linear-regression power model in one Python file, `powermodel.py`. It uses
only the standard library. The model is `features()` and `predict()`, with
per-zone coefficients in `coef.json`; `train.py` fits them. Everything around
it reads counters from Prometheus and writes the shapes in `CONTRACT.md`.

Needs Python 3.11+. Training needs the repo's `.venv` (numpy, scikit-learn;
`uv venv --python 3.13 .venv && uv pip install --python .venv numpy scikit-learn`). Below, `$SNAP` is the absolute path of a snapshot in `snapshots/`,
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

**2. Change the model.** The model is deliberately small:

- Package watts = intercept + user weight × user fraction + kernel weight × kernel fraction.
- DRAM watts = intercept + CPU weight × total executed CPU fraction.

User time includes `user` and `nice`; kernel time includes `system`, `irq`,
and `softirq`. Fractions use counter deltas divided by logical CPUs × elapsed
step seconds. Idle, I/O wait and steal do not count as execution. Frequency
and a fixed 50% SMT hinge are not used, so inference works in a VM without
hardware frequency or physical-core topology signals.

`features()` rejects incomplete, non-finite and reset CPU windows. Small
scrape-timing overshoots are capped proportionally so fractions sum to at most
one. `ZONE_FEATURES` selects inputs per zone and is shared with training.
`predict()` applies `coef.json`, clips negative watts to zero, and splits the
active portion over pods using their CPU time.

To use another signal, fetch it in `fetch()` with a raw selector, filtered by
node:

```python
query_range(a.prometheus, f'node_vmstat_pgfault{{node="{a.node}"}}', start, end, a.step)
```

Then take its delta between `ts - step` and `ts` in `at()`, the way
`features()` does. Selectors are metric names with label matchers, no
`rate()` or `sum()`: the arithmetic lives in Python, as it would when
reading `/proc`. Add it to `features()` and `ZONE_FEATURES`, then retrain.

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
docker build -t powermodel-python:dev python      # from the repo root
```

On a [power-kind](../../power-kind) cluster:

```sh
kind load docker-image powermodel-python:dev --name power-kind
kubectl --context kind-power-kind apply -k python/deploy/overlays/kind
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

## Verification

```sh
python3 -m unittest discover -s python -v
```

Training reports leave-one-snapshot-out MAE before fitting the final
coefficients on both snapshots and both nodes (`sut-01,sut-02` by default).
Hardware flags are metadata; they do not calibrate coefficients to a different
CPU. Successful VM inference verifies availability and output, not physical
power accuracy: the VM's synthetic power is not a validation target.

## Current trained baseline (2026-11-03)

`coef.json` was fitted with ordinary least squares on 778 ten-second samples:
267 stress samples and 122 TeaStore samples per node, for `sut-01` and `sut-02`.
The fitted equations (fractions are 0–1, output is watts) are:

```text
package = max(0, 22.842057 + 45.812934 * user_ratio + 73.699268 * kernel_ratio)
dram    = max(0,  4.053056 +  4.140415 * ratio)
ratio   = user_ratio + kernel_ratio
```

Leave-one-workload-out MAE in watts, training on both nodes of the other
snapshot (these are validation fits, not scores of the final combined fit):

| Held-out snapshot | Node | Package MAE | DRAM MAE |
|---|---|---:|---:|
| stress-1008 | sut-01 | 7.978 | 2.471 |
| stress-1008 | sut-02 | 6.238 | 2.236 |
| teastore-1008 | sut-01 | 3.804 | 1.593 |
| teastore-1008 | sut-02 | 11.853 | 3.210 |

These pooled-node results are not directly comparable to the earlier
feature screening on `sut-02` alone. The two nodes have different power
baselines despite matching board descriptions; this small shared model does
not account for that difference. It is a portable-input baseline, not a
calibration for arbitrary hardware. Separate hardware calibration and more
workloads would be needed to improve generalization.

Verification produced all 1,556 expected offline rows across both nodes and
snapshots, accepted by `tools/score.py`. A local exporter reading the live
`power-kind-worker` metrics returned HTTP 200 with package, DRAM and pod
predictions without frequency samples. No cluster deployment was changed.
