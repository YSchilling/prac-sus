# lab-1117-sut02-perf: cpu, memory, io and the sqlite app on sut-02, two rounds, with cache misses (17 Nov 2026)

Two rounds of cpu-bound → memory-bound → io-bound → sqlite-app on **sut-02**
(booking 119, default tuning), run with `tools/lab_repeat.sh --perf` and
fetched with `tools/lab_fetch.py` (see `run.json`). Round 1 is training data,
round 2 test (`snapshots/index.csv`). A mixed job ran afterwards as a separate
block.

## Transfer: the sut-01 cache-miss model on sut-02

Before this run, `coef-perf.json` was fitted on sut-01 only. On all of this
run (894 steps, both rounds unseen) it scored **3.69 W package / 1.64 W DRAM**
MAE, against 7.94 / 3.54 W for the CPU-only `coef-default-profile.json`. Its
one systematic miss: DRAM about 17 % low (total energy −16.8 %), the known
machine difference (sut-02 draws about 1.3 W more DRAM under load).

## Refit on both machines

`coef-perf.json` is now fitted on sut-01 (lab-1117-perf: cpu, memory, io) and
round 1 of this run (cpu, memory, io, sqlite):

    python/train.py --select "tuning=default perf=yes role=train" --perf --holdout job

On the test data (this run's round 2 and lab-1117-sqlite round 2, 585 steps):

| | package | DRAM | energy package / DRAM |
| --- | --- | --- | --- |
| fitted on sut-01 only | 3.55 W | 1.52 W | −2.2 % / −14.0 % |
| **fitted on both** | **3.49 W** | **1.32 W** | −0.9 % / −4.9 % |

Most of the gain is sut-02 DRAM (cpu-bound 1.18 → 0.71 W); sut-01 sqlite is
unchanged in MAE (DRAM energy +0.7 % → +13.6 %).
