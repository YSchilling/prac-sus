# lab-1117-sut02-mixed: the mixed workload on sut-02, with cache misses (17 Nov 2026)

One run of [`workloads/mixed.yaml`](../../workloads/mixed.yaml) on **sut-02**
(booking 119, default tuning, `tools/lab_repeat.sh --perf`), directly after
[`lab-1117-sut02-perf`](../lab-1117-sut02-perf/README.md). The mixed job is
locked in `snapshots/index.csv`, like the sut-01 mixed runs: nothing trains
on it.

Node power MAE (124 steps): `coef-perf.json` **6.29 W package / 2.31 W DRAM**
(energy −0.6 % / −5.6 %), the CPU-only model 11.33 / 6.51 W. On sut-01's
mixed run the cache-miss model scores 4.41 / 1.36 W.

The gap to sut-01 is almost all phase edges. Inside the phase windows
(`windows.csv`, 98 steps) package MAE is 3.03 W on sut-02 and 3.04 W on
sut-01; DRAM is 1.45 against 0.91 W (sut-02's higher DRAM draw). The 26 steps
at phase starts and stops average 18.6 W package error on sut-02 (9.8 W on
sut-01): power and counters fall into slightly different 10 s windows there.
Smoothing over steps, now allowed, is the obvious fix to try.
