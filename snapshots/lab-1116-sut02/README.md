# lab-1116-sut02: all four workloads on sut-02, two rounds (16 Nov 2026)

[`cpu-bound`](../../workloads/cpu-bound.yaml), [`memory-bound`](../../workloads/memory-bound.yaml),
[`io-bound`](../../workloads/io-bound.yaml) and [`mixed`](../../workloads/mixed.yaml),
run twice in that order on **sut-02** (booking 109, 17:18–19:50 UTC, default
tuning, observability addon) by [`tools/lab_repeat.sh`](../../tools/lab_repeat.sh).
Same layout as [`lab-1115`](../lab-1115/README.md): `tsdb/` (in-cluster
Prometheus snapshot), `truth.csv`/`span.txt` (Kepler = RAPL, node `sut-02`),
`hw.env`, `windows.csv` (133 phases) and the job logs in `logs/`. Runs are
named `<job>/r<round>`.

**Nothing is trained on this run.** It tests the sut-01 model
(`python/coef-default-profile.json`) on a machine it has never seen, and
`mixed` stays out of training for good: it is the one workload no model has
seen.

## Results

Node power MAE over each job's window (apply to complete), package / DRAM, W:

| test | `coef.json` | `coef-default-profile.json` |
| --- | --- | --- |
| cpu-bound r1 / r2 | 7.35 / 1.27 · 7.06 / 1.20 | **3.46** / 1.80 · **3.31** / 1.80 |
| memory-bound r1 / r2 | 14.88 / 6.75 · 14.72 / 6.72 | **7.15 / 4.80** · **6.82 / 4.68** |
| io-bound r1 / r2 | 3.82 / 1.48 · 3.76 / 1.47 | **2.37 / 1.36** · **2.31 / 1.36** |
| mixed r1 / r2 | 15.59 / 7.67 · 15.84 / 7.73 | **8.54 / 5.94** · **8.80 / 5.97** |

1. **The sut-01 model carries over.** Its errors on sut-02 are about the
   same as on sut-01's own test runs. The machines differ by about 1 W of
   package power at idle (18.2 vs. 17.3 W) and 1.3 W of DRAM under load
   (7.0 vs. 5.7 W), which is why DRAM on pure CPU load is a little worse
   than with `coef.json`.
2. **Adding sut-02 to training barely helps.** A refit on sut-01's training
   runs plus round 1 here (cpu/memory/io) gained at most 0.4 W on round 2 and
   mixed and lost about 0.1 W on sut-01, so it was not adopted.
3. **Mixed is good without memory traffic and bad with it**, per phase,
   both rounds (measured / current model):

   | phase | CPU | package | DRAM |
   | --- | --- | --- | --- |
   | idle | 0.01 | 16.7 / 15.5 | 1.7 / 1.4 |
   | cpu + memory (4 + 4 stream) | 0.41 | 65.7 / 55.1 | 17.3 / 9.1 |
   | cpu + io (4 + fio qd8) | 0.25 | 46.8 / 48.9 | 6.8 / 8.5 |
   | memory + io (4 stream + fio qd8) | 0.25 | 58.1 / 49.0 | 17.4 / 8.6 |
   | all (4 + 4 + fio qd8) | 0.45 | 66.7 / 56.4 | 17.3 / 9.2 |
   | all high (8 + 8 + fio qd32) | 0.86 | 77.6 / 67.0 | 20.5 / 9.3 |

   Every phase with memory streaming puts DRAM at 17–20 W against about
   9 W predicted, and package about 10 W too low: memory traffic is
   invisible to CPU time (see `lab-1115`).
4. **The rounds repeat**: round 2 minus round 1 is within 0.4 W package and
   0.1 W DRAM for every job.
