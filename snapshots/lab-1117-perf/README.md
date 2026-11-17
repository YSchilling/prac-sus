# lab-1117-perf: all four workloads on sut-01 with cache-miss counters (17 Nov 2026)

One round of cpu-bound, memory-bound, io-bound and mixed on **sut-01**
(booking 114, 12:18–13:34 UTC, default tuning), run with
`tools/lab_repeat.sh --perf`: node-exporter's perf collector was on, so the
snapshot also holds hardware counters per CPU (`node_perf_cache_misses_total`,
`node_perf_cache_references_total`, `node_perf_instructions_total`, …).
Same layout as the other lab runs (`tsdb/`, `truth.csv`/`span.txt`,
`windows.csv`, `logs/`).

**Split:** cpu-bound, memory-bound and io-bound train
`python/coef-perf.json`; mixed is the test and was never trained on.

## Cache misses track memory traffic

`miss_gbps` = cache misses × 64 B per second (`powermodel.miss_features`):

| phase | CPU | miss_gbps | stream GB/s (job log) | DRAM W | package W |
| --- | --- | --- | --- | --- | --- |
| memory level 0 (8 cpu workers) | 0.41 | 0.02 | 0 | 5.3 | 52.2 |
| memory level 25 | 0.41 | 3.71 | 27.8 | 10.9 | 60.8 |
| memory level 50 | 0.41 | 6.64 | 51.5 | 14.9 | 64.5 |
| memory level 75 | 0.41 | 7.89 | 62.3 | 16.4 | 64.8 |
| memory level 100 | 0.41 | 8.42 | 66.4 | 17.1 | 63.3 |
| cpu-bound level 100 | 1.00 | 0.08 | – | 5.7 | 66.7 |
| io-bound rand read qd32 | 0.06 | 0.01 | – | 5.6 | 32.3 |

It rises with measured bandwidth, but counts only about 1/7 of it
(probably because hardware-prefetched lines don't count as misses), and it
stays near zero under pure CPU or disk load. Over all 434 steps its
correlation with DRAM power is 0.93, against 0.66 for `awake`.

## The model with cache misses

```text
package = 20.73 + 29.10 r + 19.14 awake + 1.59 miss_gbps
dram    =  3.37            +  2.34 awake + 1.38 miss_gbps
```

Node power MAE, package / DRAM, W:

| | cpu | memory | io | **mixed (test)** |
| --- | --- | --- | --- | --- |
| `coef.json` | 6.63 / 1.36 | 13.44 / 5.20 | 5.04 / 1.51 | 14.55 / 6.06 |
| `coef-default-profile.json` (other runs) | 4.55 / 2.92 | 6.49 / 3.80 | 9.07 / 2.74 | 8.60 / 4.83 |
| CPU-only refit on this run | 4.08 / 2.93 | 5.91 / 3.77 | 3.13 / 1.23 | 7.68 / 4.74 |
| **`coef-perf.json`** | 3.27 / 0.54 | 4.14 / 0.97 | 3.71 / 1.58 | **4.46 / 1.11** |

cpu/memory/io are training data for the last two rows. On mixed,
`tools/score.py` gives the same 4.46 / 1.10 W, with total energy off by
+1.7 % / +2.7 % (CPU-only: −10.7 % / −33.6 %). In the memory-streaming
phases DRAM is now right (15.0 predicted vs. 15.2 measured; CPU-only 9.1).

## Caveats

1. **Memory traffic has to be in the training data.** Leaving memory-bound
   out (train on cpu + io, test on memory) gives 74 / 31 W: with almost no
   misses in training, the miss weight is unconstrained. Leaving cpu out
   instead improves on CPU-only (6.9 / 0.9 vs. 13.6 / 5.6 W).
2. **The collector changes idle power.** With perf on, idle sits at about
   19 W package / 2.1 W DRAM, against 16.7 / 1.4 W in lab-1116, probably
   because reading the counters on every scrape keeps cores out of deep
   sleep. So `coef-perf.json` is only valid where the perf collector runs,
   and its data can't simply be pooled with runs without it.
3. **Is it allowed?** A VM sees these counters only with a virtual PMU and
   `CAP_PERFMON` (checked on the cp-01 VM). Whether that counts as
   VM-visible is a question for the instructors, so this stays a separate
   variant beside the CPU-only model.
