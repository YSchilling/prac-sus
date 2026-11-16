# lab-1116-io: first I/O-bound run on sut-01 (16 Nov 2026)

One round of [`workloads/io-bound.yaml`](../../workloads/io-bound.yaml) on
**sut-01** (booking 106, 16:11–16:27 UTC, default tuning, observability addon)
via [`tools/lab_repeat.sh`](../../tools/lab_repeat.sh). Same layout as
[`lab-1116`](../lab-1116/README.md): `tsdb/`, `truth.csv`/`span.txt`
(Kepler = RAPL), `hw.env`, `windows.csv` from the job log, and `logs/` with
fio's full output (bandwidth, IOPS, latency per phase).

`/scratch` was the SUT's local disk (`/dev/mapper/vg_data-kubelet`, ext4), an
NVMe drive that reached about 3 GB/s sequential and up to 169k random-read
IOPS. RAPL does not include disk power, so this run shows only the CPU and
DRAM work that I/O causes. The scores below are from before it was used for
training: it was the first workload neither model had seen. Since then it is
**training data** for `python/coef-default-profile.json`;
[`lab-1116-io2`](../lab-1116-io2/) is the held-out repeat.

## Results

Node power MAE (`tools/score.py`, 98 steps):

| | package | DRAM | package energy | DRAM energy |
| --- | --- | --- | --- | --- |
| `coef.json` | 4.47 | 1.55 | +14.5 % | +33.4 % |
| `coef-default-profile.json` | **3.19** | **1.40** | +10.5 % | +32.8 % |

Per phase (window means; CPU fractions of 20 threads; disk = read + written
over all `node_disk_*` devices, which counts I/O through the device-mapper
volume twice, so fio's figure is about half):

| phase | user | kernel | iowait | disk MB/s | pkg | old | new | DRAM | old | new |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| idle | 0.008 | 0.005 | 0.004 | 3 | 15.0 | 23.5 | 15.4 | 1.0 | 4.1 | 1.0 |
| seq read 1M qd1 | 0.009 | 0.023 | 0.002 | 4050 | 19.3 | 24.9 | 24.0 | 2.1 | 4.2 | 4.2 |
| seq read 1M qd32 | 0.009 | 0.017 | 0.002 | 6256 | 24.8 | 24.5 | 21.9 | 5.0 | 4.2 | 3.4 |
| seq write 1M qd8 | 0.016 | 0.020 | 0.003 | 4973 | 20.0 | 25.0 | 25.4 | 2.4 | 4.2 | 4.7 |
| rand read 4k qd1 | 0.011 | 0.013 | 0.002 | 106 | 19.6 | 24.3 | 21.2 | 2.4 | 4.2 | 3.1 |
| rand read 4k qd32 | 0.026 | 0.036 | 0.002 | 1389 | 30.2 | 26.7 | 32.5 | 5.5 | 4.3 | 7.1 |
| rand write 4k qd8 | 0.019 | 0.042 | 0.002 | 722 | 27.5 | 26.8 | 32.4 | 4.5 | 4.3 | 7.1 |
| rand write 4k qd32 | 0.023 | 0.039 | 0.002 | 1038 | 28.6 | 26.8 | 32.5 | 4.8 | 4.3 | 7.1 |

All 14 phases are in `windows.csv`.

1. **I/O barely loads the CPU**: 2–6 % busy, now with more kernel than user
   time (kernel up to 0.042). This is the low-load region where `awake`
   overshoots, so random I/O at high queue depth is overestimated by up to
   4 W (package) and 2.6 W (DRAM).
2. **iowait stays near 0.** The NVMe drive answers fast enough that CPUs
   never sit waiting, so how iowait should count is not tested by this run.
3. **Sequential reads move power without CPU time.** At queue depth 32, 3 GB/s
   is written into memory by DMA: DRAM goes to 5.0 W and package to 24.8 W
   at 2.6 % busy, and both models are too low. Like memory-bound traffic,
   this is invisible to CPU ratios; disk bytes/s is a VM-visible candidate
   input for it (filter to one device to avoid the double count).
4. **There is now enough kernel time to fit a user/kernel split**, but only at
   small loads. Training on cpu-bound + memory-bound + io-bound should tell
   whether kernel time costs more per CPU-second than user time.

## Leave one workload out

Train on two of cpu/memory/io-bound (lab-1115 + lab-1116 + this run), test on
the third (lab-1116, or this run for io). MAE W:

| package features | test cpu | test mem | test io | mean |
| --- | --- | --- | --- | --- |
| user + kernel | 20.35 | 13.22 | 7.81 | 13.79 |
| ratio | 20.19 | 13.22 | 4.58 | 12.66 |
| ratio + awake (0.05) | 20.34 | 9.81 | 3.41 | 11.19 |
| **ratio + awake (0.1)** | 21.42 | **8.91** | **2.13** | **10.82** |
| ratio + awake (0.2) | 23.29 | 8.16 | 2.60 | 11.35 |
| user + kernel + awake (0.1) | 25.01 | 8.90 | 11.30 | 15.07 |

| DRAM features | test cpu | test mem | test io | mean |
| --- | --- | --- | --- | --- |
| ratio | 9.98 | 6.04 | 1.71 | 5.91 |
| ratio + awake (0.05) | 8.69 | 5.12 | 1.48 | 5.10 |
| **awake (0.05)** | **5.34** | 5.05 | 1.07 | **3.82** |
| awake (0.1) | 5.59 | 5.09 | 0.91 | 3.86 |

Without cpu-bound in training every model is off by 20 W or more: memory and
io never exceed 41 % CPU, so the fit extrapolates (predicts 125 W at full
load where 66 W is measured). Every training set needs the full CPU range.
