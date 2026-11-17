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

The flags from `CONTRACT.md`, plus `--listen` (online, default `:9270`),
`--out` (offline, default `predictions.csv`) and `--coef` (coefficient file,
default `coef.json`; `coef-default-profile.json` and `coef-perf.json` are the other two). Each can also be set as
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

## Default-profile model (2026-11-16)

`coef-default-profile.json` is fitted on our own lab runs with the default CPU
tuning on sut-01, and selected with `--coef python/coef-default-profile.json`
or `POWERMODEL_COEF`. `features()` adds `awake`, a curve that rises fast and
then flattens, because power jumps as soon as anything runs (deep sleep and
DRAM self-refresh end) and then levels off:

```text
r     = executed CPU seconds / (CPUs x step)            (= ratio)
awake = 1 - exp(-r / 0.1)                               (AWAKE_SCALE)
package = max(0, 11.028829 + 25.251207 * r + 34.283384 * awake)
dram    = max(0,  0.310309 +  8.952979 * awake)
```

A coefficient file only fits the `AWAKE_SCALE` it was trained with; retrain
after changing it.

| split | snapshots |
| --- | --- |
| train | [`lab-1115`](../snapshots/lab-1115/README.md) (cpu + memory, 4 rounds), [`lab-1116-io`](../snapshots/lab-1116-io/README.md) (io run 1) |
| test | [`lab-1116`](../snapshots/lab-1116/README.md) (cpu + memory), [`lab-1116-io2`](../snapshots/lab-1116-io2/) (io run 2), [`lab-1116-sut02`](../snapshots/lab-1116-sut02/README.md) (sut-02, all four workloads incl. mixed) |

Node power MAE on the test runs, `tools/score.py`:

| test run | `coef.json` package / DRAM | `coef-default-profile.json` package / DRAM |
| --- | --- | --- |
| lab-1116 | 11.03 / 3.67 | **5.22 / 3.18** |
| lab-1116-io2 | 4.42 / 1.55 | **2.20 / 0.91** |

The features were chosen by leaving one workload out (train on two of
cpu/memory/io-bound, test on the third; see `lab-1116-io`): scale 0.1 beat
0.05 and 0.2, a user/kernel split made every held-out workload worse, and
DRAM's ratio term only fit the training data. Held-out workloads stay hard:
about 9 W package / 5 W DRAM on memory-bound (memory traffic is invisible to
CPU time), and above 20 W when cpu-bound is missing from training (the other
workloads never exceed 41 % CPU, so the fit extrapolates).

On sut-02, which it never trained on
([`lab-1116-sut02`](../snapshots/lab-1116-sut02/README.md)), it does about as
well as on sut-01 (round 2: cpu 3.3 / 1.8 W, memory 6.8 / 4.7 W, io 2.3 /
1.4 W), and on the unseen mixed workload it scores 8.5–8.8 W package /
5.9–6.0 W DRAM (`coef.json`: 15.6–15.8 / 7.7 W). Its misses are the phases
with memory streaming. A refit that adds sut-02 gained at most 0.4 W, so
one model serves both nodes in the default profile.

Keep `coef.json` for the course snapshots: they look like a different CPU
configuration (sut-02 idles at 33 W and never sleeps, like the lab's
clean-room profile), and there the default-profile fit is worse.

## Cache-miss variant (2026-11-17)

`coef-perf.json` adds `miss_gbps`, last-level cache misses x 64 bytes per
second from node-exporter's perf collector, as a stand-in for memory traffic:

```text
package = max(0, 20.73 + 29.10 * r + 19.14 * awake + 1.59 * miss_gbps)
dram    = max(0,  3.37 +  2.34 * awake + 1.38 * miss_gbps)
```

Fitted on cpu/memory/io-bound of
[`snapshots/lab-1117-perf`](../snapshots/lab-1117-perf/README.md) and tested
on its mixed run: 4.46 W package / 1.10 W DRAM MAE, against 7.68 / 4.74 W
for a CPU-only fit on the same data. It needs `node_perf_cache_misses_total`
(`tools/lab_repeat.sh --perf` turns the collector on); without it offline mode
stops with a message and online mode answers 503. `train.py --perf` fits it,
but over a snapshot's whole span; this file was fitted without the mixed job,
which `train.py` can't exclude yet. The collector itself raises idle power by
about 2 W, so only use this file where it runs.

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
