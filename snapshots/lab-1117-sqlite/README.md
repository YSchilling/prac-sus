# lab-1117-sqlite: the sqlite app on sut-01, two rounds, with cache misses (17 Nov 2026)

Two rounds of [`workloads/sqlite-app.yaml`](../../workloads/sqlite-app.yaml)
(image `registry.lab:5010/prac-sus/sqlite-app:v1`, digest
`sha256:a1b318ee…`, built from `5d5e8ae`) on **sut-01**, default tuning, run
with `tools/lab_repeat.sh --perf` and fetched with `tools/lab_fetch.py`
(see `run.json`). Round 1 is training data, round 2 is test
(`snapshots/index.csv`).

The app is single-threaded (about 5 % of the 20 hardware threads) with
fsync-heavy update and import phases, so it sits in the low-load range.

## Node power MAE, both rounds (277 steps)

| model | package | DRAM | energy package / DRAM |
| --- | --- | --- | --- |
| `coef.json` | 3.87 W | 1.33 W | −2.4 % / +7.6 % |
| `coef-default-profile.json` | 6.67 W | 1.87 W | −25.7 % / −46.7 % |
| `coef-perf.json` | **3.08 W** | **1.18 W** | +2.5 % / +0.9 % |

None of the three had seen the app. Adding round 1 to the cache-miss model's
training changes round 2 by under 0.05 W (3.05 → 3.02 / 1.19 → 1.17), and
the two rounds score within 0.1 W of each other.

`coef-default-profile.json` looks worse than it is: this run had the perf
collector on, which raises idle power by about 2 W, and that model was
trained without it. Train with the same collector setting a model runs under.

The cache-miss model's remaining misses are the idle phases (package +15–25 %,
DRAM +75–110 %, about 1.5 W) and light load (search at 1,000/s: package
−15 %). `tools/lab_eval.py --select "snapshot=lab-1117-sqlite role=train,test"
--phases` prints every phase.
