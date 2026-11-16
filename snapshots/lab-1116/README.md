# lab-1116: held-out test run on sut-01 (16 Nov 2026)

One round of [`workloads/cpu-bound.yaml`](../../workloads/cpu-bound.yaml) and
[`workloads/memory-bound.yaml`](../../workloads/memory-bound.yaml) on **sut-01**
(booking 106, 15:29–16:07 UTC, default tuning, observability addon), run with
[`tools/lab_repeat.sh`](../../tools/lab_repeat.sh). Same layout as
[`lab-1115`](../lab-1115/README.md): `tsdb/` (in-cluster Prometheus snapshot),
`truth.csv`/`span.txt` from [`tools/make_truth.py`](../../tools/make_truth.py),
`hw.env`, and the job logs in `logs/`.

**Nothing is trained on this run.** It is a test set for
`python/coef-default-profile.json`; the tables below score the first version
(fitted on lab-1115 only, `AWAKE_SCALE` 0.05). For the current version see
[`python/README.md`](../../python/README.md#default-profile-model-2026-11-16).

```bash
PORT=19096 tools/prom.sh $PWD/snapshots/lab-1116/tsdb &
python3 python/powermodel.py --mode offline --prometheus http://127.0.0.1:19096 --node sut-01 \
    --range "$(cat snapshots/lab-1116/span.txt)" --coef python/coef-default-profile.json
python3 tools/score.py predictions.csv --truth snapshots/lab-1116/truth.csv
```

## Results

Node power, MAE in watts (`tools/score.py`, 226 steps):

| | package | DRAM | package energy | DRAM energy |
| --- | --- | --- | --- | --- |
| `coef.json` (course snapshots) | 11.03 | 3.67 | −12.1 % | −16.9 % |
| `coef-default-profile.json` (lab-1115, awake) | **6.09** | **3.00** | −0.7 % | −1.0 % |

| workload | zone | old | new | mean measured |
| --- | --- | --- | --- | --- |
| cpu-bound | package | 7.31 | **4.60** | 46.1 |
| cpu-bound | DRAM | **1.55** | 2.36 | 4.5 |
| memory-bound | package | 14.62 | **7.98** | 43.0 |
| memory-bound | DRAM | 5.63 | **3.86** | 8.9 |

This run repeats lab-1115's workloads, so it tests stability more than
generalisation. Training on one workload of lab-1115 and testing on the
other workload here is much harsher:

| features | package cpu→mem | package mem→cpu | DRAM cpu→mem | DRAM mem→cpu |
| --- | --- | --- | --- | --- |
| user + kernel (old) | 13.72 | 20.43 | – | – |
| ratio (old DRAM) | 13.31 | 20.43 | 6.06 | 10.02 |
| ratio + awake (new) | **9.82** | 22.12 | **5.16** | **8.71** |

`awake` helps except when only memory-bound is seen: its CPU ratio sits
near 0.41 almost all the time, so any fit has to extrapolate. More and more
varied workloads matter more than the feature choice.
