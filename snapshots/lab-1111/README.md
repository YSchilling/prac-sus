# lab-1111: first runs on the ISE bare-metal lab (11 Nov 2026)

Our own CPU- and memory-bound workloads, measured with RAPL on the real SUTs
(Xeon Silver 4114, 10 cores / 20 threads, 48 GB). Unlike `stress-1008` and
`teastore-1008` this is **not a Prometheus snapshot**: it is CSV exported from
the lab Prometheus with [`tools/lab_export.py`](../../tools/lab_export.py),
so it cannot be served with `tools/prom.sh` or scored with `tools/score.py`.

| File | Content |
| --- | --- |
| `trace-sut-01.csv`, `trace-sut-02.csv` | 5 s grid: RAPL `pkg_w`, `dram_w` (ground truth) plus VM-visible inputs (CPU ratios by mode, memory, context switches, disk, network, frequency) |
| `windows.csv` | start/end of every phase, trimmed to the steady part |
| `phases.csv` | every input averaged over each window (50 rows) |

All times are UTC. `busy`/`user`/`kernel`/`iowait` are fractions of the 20
hardware threads. `freq_ghz` averages `scaling_cur_freq` over all cores, idle
ones included, so it reads lower than the clock of the busy cores.

## Runs

| run | node | manifest | time (UTC) |
| --- | --- | --- | --- |
| `cpu-staircase` | sut-01 | `workloads/stress-ng-job.yaml` at commit 304da40: 5/10/15/20 threads at 50 % and 100 % duty, 75 s each | 16:19:50–16:32:50 |
| `cpu-bound` | sut-02 | [`workloads/cpu-bound.yaml`](../../workloads/cpu-bound.yaml): 25/50/75/100 % levels, then 10→100→10 % ramp | ≈18:01–18:18:20 |
| `memory-bound` | sut-02 | [`workloads/memory-bound.yaml`](../../workloads/memory-bound.yaml): 8 busy threads with 0–100 % of them streaming memory, a 16 GiB idle footprint, then a memrate ramp | ≈18:18:20–18:37:20 |

Default tuning profile, observability addon, nothing else running on the SUT.
The sut-02 job logs were lost when the booking ended, so its phase boundaries
come from the power and CPU traces (they match the manifests' timings).
Ramp `…GBps-cap` labels are the bandwidth *caps* given to memrate, not
measured bandwidth.

## What we learned

Model column: the current `python/coef.json` (062f142) applied to the phase
averages. Watts.

| phase | busy | pkg | model | dram | model |
| --- | --- | --- | --- | --- | --- |
| idle (sut-02) | 0.02 | 16.8 | 23.7 | 1.7 | 4.1 |
| cpu 25 % | 0.27 | 46.0 | 35.2 | 6.9 | 5.2 |
| cpu 50 % | 0.51 | 61.0 | 46.3 | 6.9 | 6.2 |
| cpu 100 % | 0.99 | 67.6 | 68.3 | 7.0 | 8.2 |
| mem 0 % (8 threads in cache) | 0.41 | 57.2 | 41.7 | 7.3 | 5.8 |
| mem 75 % (6 of 8 streaming) | 0.41 | 67.1 | 41.8 | 20.3 | 5.8 |
| 16 GiB held, not touched | 0.02 | 17.1 | 23.8 | 1.8 | 4.1 |

1. **Package power is concave in CPU busy.** The first quarter of the threads
   adds about 29 W, the last half about 7 W. The linear model overshoots idle by
   about 7 W and undershoots the middle by up to 15 W.
2. **Idle is about 15–17 W**, not the 23 W intercept learned from the course
   snapshots.
3. **Packing vs. spreading matters a little.** At busy 0.51 on sut-01, 10
   threads at full load draw 59 W and 20 threads at half duty draw 55 W.
4. **DRAM follows memory traffic, not CPU.** At a fixed busy of 0.41, DRAM goes
   from 7 W to 20 W as threads switch from in-cache work to streaming.
   Bandwidth saturates around 6 streaming threads. Under pure CPU load, DRAM is
   a flat 7 W (sut-02) or 5.6 W (sut-01) whenever anything runs, and about
   1–2 W when the package sleeps.
5. **Memory traffic also raises package power** (57 → 67 W at the same
   busy), probably from the memory controller and caches.
6. **Allocated memory does not cost power.** Holding 16 GiB untouched adds
   about 0.1 W, so `mem_used` is not a useful DRAM feature.
7. **sut-01 and sut-02 differ slightly**: about 1.5 W at idle and DRAM at 5.6 W
   vs. 7 W under load.

**Open problem:** we need a VM-visible signal that tracks memory bandwidth.
CPU busy does not (flat in the memory levels). Context switches rose during
the memrate ramp but that is memrate's throttling, not bandwidth. The lab
Prometheus has no `node_pressure_*` or `node_vmstat_*` (page faults) for the
SUTs. The in-cluster Prometheus of the observability addon has the full
node-exporter, but it is wiped when the booking ends, so next time capture it
with `tools/capture.sh` **before** the slot runs out.

## Reproduce

Needs TUM VPN (eduVPN, MWN split tunnel) and our `~/.ssh/config` entries for
`lab` and `cp-01`/`cp-02` (see `SustainabilityLab.pdf`, slides 4–6).

```bash
ssh lab lab-book --addon observability 1 now 1   # interactive booking
ssh lab lab-status                                # which SUT; sut-01 pairs with cp-01, sut-02 with cp-02
ssh cp-02 'kubectl apply -f -' < workloads/cpu-bound.yaml      # wait for "done", then:
ssh cp-02 'kubectl apply -f -' < workloads/memory-bound.yaml

ssh -f -N -L 9090:mon:9090 lab                    # lab Prometheus on localhost:9090
python3 tools/lab_export.py --node sut-02 \
    --range 2026-11-11T17:59:00Z..2026-11-11T18:38:00Z \
    --out snapshots/lab-1111/trace-sut-02.csv \
    --windows snapshots/lab-1111/windows.csv --phases snapshots/lab-1111/phases.csv
```

Booking with an image fails for now: the base image from the slides
(`ghcr.io/information-system-engineering/sustainability-lab:latest`) is not
publicly pullable, so book without an image and apply jobs via `ssh cp-0x`.
Run workloads one after another, never in parallel, and leave a few minutes of
margin before the slot ends.
