# lab-1115: four repeats of cpu-bound and memory-bound on sut-01 (15 Nov 2026)

[`workloads/cpu-bound.yaml`](../../workloads/cpu-bound.yaml) and
[`workloads/memory-bound.yaml`](../../workloads/memory-bound.yaml), run
alternately for 4 rounds on **sut-01** by [`tools/lab_repeat.sh`](../../tools/lab_repeat.sh)
(booking 93, 08:55–11:27 UTC, default tuning, observability addon, nothing else
on the SUT). Follows up on [`lab-1111`](../lab-1111/README.md), which has the
first single runs and the reasoning behind the workloads.

| Path | Content |
| --- | --- |
| `tsdb/` | Prometheus snapshot of the **in-cluster** Prometheus (node-exporter with PSI, vmstat, schedstat; cAdvisor; Kepler). Serve with `PORT=19095 tools/prom.sh $PWD/snapshots/lab-1115/tsdb` |
| `trace-sut-01.csv` | 5 s trace from the lab Prometheus: RAPL `pkg_w`, `dram_w` plus VM-visible inputs (same columns as `lab-1111`) |
| `windows.csv`, `phases.csv` | 164 phase windows (from the job logs, via [`tools/lab_windows.py`](../../tools/lab_windows.py)) and the trace averaged over each |
| `stream-bandwidth.csv` | memory bandwidth the `stream` workers actually achieved per memory level, read from the job logs |
| `logs/` | `runs.log` (Job start/end times) and each Job's log with UTC step marks |

Runs are named `<job>/r<round>` in the CSVs. The snapshot has no
`truth.csv`/`span.txt` yet, so `tools/score.py` cannot use it directly.
Its node labels are `node="sut-01"` (in-cluster) vs. `instance="sut-01"`
(lab Prometheus). Kepler's `kepler_node_cpu_watts` in the snapshot reads RAPL
directly, so it is ground truth and must not be a model input.

## Results

Mean over the 4 rounds ± standard deviation between rounds, watts. Model: the
current `python/coef.json` (062f142).

| phase | busy | pkg | dram | model pkg | model dram |
| --- | --- | --- | --- | --- | --- |
| idle | 0.01 | 15.0 ± 0.02 | 0.9 ± 0.01 | 23.5 | 4.1 |
| cpu 25 % | 0.26 | 44.5 ± 0.12 | 5.6 ± 0.02 | 34.9 | 5.1 |
| cpu 50 % | 0.51 | 59.3 ± 0.10 | 5.6 ± 0.03 | 46.1 | 6.1 |
| cpu 75 % | 0.76 | 63.1 ± 0.24 | 5.7 ± 0.01 | 57.5 | 7.2 |
| cpu 100 % | 1.00 | 67.0 ± 0.10 | 5.7 ± 0.01 | 68.7 | 8.2 |
| mem 0 % (8 threads in cache) | 0.41 | 55.0 ± 0.47 | 5.6 ± 0.03 | 41.6 | 5.7 |
| mem 25 % (2 streaming, 28.4 GB/s) | 0.41 | 61.4 ± 0.35 | 11.4 ± 0.06 | 41.7 | 5.7 |
| mem 50 % (4 streaming, 51.8 GB/s) | 0.41 | 64.9 ± 0.43 | 15.3 ± 0.03 | 41.7 | 5.7 |
| mem 75 % (6 streaming, 62.8 GB/s) | 0.41 | 65.1 ± 0.07 | 16.6 ± 0.21 | 41.7 | 5.7 |
| mem 100 % (8 streaming, 67.1 GB/s) | 0.41 | 63.3 ± 0.31 | 17.3 ± 0.02 | 41.7 | 5.8 |
| 16 GiB held, not touched | 0.01 | 15.0 ± 0.13 | 0.9 ± 0.05 | 23.6 | 4.1 |

Model error averaged over all 164 phases: **package 8.4 W, DRAM 2.7 W**
(cpu-bound 6.3 / 1.2, memory-bound 12.0 / 5.1).

1. **The runs repeat closely.** The median spread between rounds is
   0.25 W for package and 0.03 W for DRAM, with a worst case of 0.5 W in
   the steady phases. The 15 s CPU ramp steps vary more (up to 3.9 W)
   because their windows are short. So one run per workload is enough for
   training, and the model errors above are real, not noise.
2. **The nodes differ more than the runs.** Compared with sut-02 in
   `lab-1111`, sut-01 idles about 2 W lower (15.0 vs. 16.8 W) and its
   DRAM runs lower under load (5.6 vs. 7.0 W with CPU work, 17.3 vs. 19.3 W
   with full memory traffic). Per-node calibration (FR3/FR4) is worth it.
3. **DRAM ≈ 5.6 W + 0.2 W per GB/s** of measured bandwidth. It is close to
   linear and saturates at about 67 GB/s. Package power also rises with
   traffic, from 55 to 65 W at the same CPU busy.
4. **No VM-visible node counter tracks memory bandwidth.** Across the memory
   levels (busy fixed at 0.41, DRAM 5.6 → 17.3 W):
   - page faults stay flat at about 670/s;
   - PSI memory pressure stays at 0;
   - `pgpgin`/`pgpgout`, interrupts, context switches and scheduler
     timeslices change by 5 % or less.

   Context switches and timeslices do rise during the memrate ramp, but
   only because memrate throttles by sleeping, so that is a side effect of
   the tool. With node-level OS counters, the memory-driven part of DRAM
   power (up to about 12 W here) cannot be predicted. Options:
   - accept the error for memory-heavy loads;
   - find a per-container signal;
   - ask whether cache-miss counters (vPMU) count as VM-visible. This is an
     open question from our pitch.
5. **The other lab-1111 findings hold on sut-01.** Package power is concave
   in CPU busy, idle sits far below the model's intercept, DRAM is about
   1 W when the package sleeps, and holding memory costs no power.

## Reproduce

Setup (VPN, SSH config, booking) is as in [`lab-1111`](../lab-1111/README.md#reproduce). Then:

```bash
ssh lab lab-book --addon observability 1 now 4
ssh lab 'mkdir -p prac-sus' && scp -r workloads tools/lab_repeat.sh lab:prac-sus/
ssh cp-01 'nohup setsid prac-sus/lab_repeat.sh 4 prac-sus/workloads/cpu-bound.yaml \
    prac-sus/workloads/memory-bound.yaml >/dev/null 2>&1 </dev/null &'
# about 2.5 h later, results are in lab:~/lab-runs/<UTC start>/
scp 'lab:lab-runs/<UTC start>/*' <dir>/
tools/lab_windows.py --node sut-01 <dir>/r*-cpu-bound.log <dir>/r*-memory-bound.log > windows.csv
ssh -f -N -L 9090:mon:9090 lab
tools/lab_export.py --node sut-01 --range <start>..<end> --out trace-sut-01.csv \
    --windows windows.csv --phases phases.csv
```

Use `cp-01` for a sut-01 booking and `cp-02` for sut-02 (`lab-status` shows
which). The driver runs on the cp node and writes to the ZFS home, so it
survives network drops and the reset at the end of the slot. It also takes
the `tsdb/` snapshot itself.
