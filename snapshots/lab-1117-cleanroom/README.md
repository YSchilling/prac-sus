# lab-1117-cleanroom: cpu, memory and io on sut-01 with the clean-room profile (17 Nov 2026)

One round of [`cpu-bound`](../../workloads/cpu-bound.yaml),
[`memory-bound`](../../workloads/memory-bound.yaml) and
[`io-bound`](../../workloads/io-bound.yaml) on **sut-01** booked with
`lab-book --tuning clean-room` (booking 112, 10:04–10:58 UTC): every core
fixed at 2.20 GHz, no turbo, no deep sleep. Same layout as the other lab
runs (`tsdb/`, `truth.csv`/`span.txt`, `windows.csv`, `logs/`); `hw.env`
adds `POWERMODEL_TUNING="clean-room"`. Nothing is trained on it.

It was run to test whether the course snapshots were recorded in clean-room.

## Power by CPU load

Package / DRAM watts, mean per band of CPU busy fraction:

| | 0–2 % | 5–10 % | 10–20 % | 20–30 % | 50–70 % | 90–100 % |
| --- | --- | --- | --- | --- | --- | --- |
| **sut-01 clean-room (this run)** | 32.2 / 5.8 | 33.5 / 5.8 | 35.8 / 7.2 | 42.4 / 7.7 | 49.9 / 5.6 | 58.0 / 5.7 |
| sut-01 default (lab-1116) | 16.7 / 1.4 | 25.9 / 4.3 | 27.6 / 5.6 | 41.5 / 6.5 | 58.5 / 5.6 | 66.5 / 5.7 |
| course stress-1008, sut-02 | 33.2 / 6.9 | 33.2 / 6.9 | 38.3 / 8.2 | 46.0 / 8.8 | 51.7 / 6.9 | 62.6 / 6.8 |
| course teastore-1008, sut-02 | 32.9 / 6.9 | 36.4 / 7.3 | 39.7 / 7.6 | 41.6 / 7.9 | – | – |
| course stress/teastore, sut-01 | – | 21.0–21.1 / 2.4–2.5 | 21.7–22.7 / 2.5–2.7 | – | – | – |

1. **The course snapshots' sut-02 matches clean-room**: idle about 33 W
   package with DRAM that never sleeps (5.8–6.9 W), and no turbo at full
   load (58–63 W instead of 67 W). The rest of the gap (about 1 W DRAM,
   a few W at full load) fits the difference between the two machines.
2. **The course snapshots' sut-01 does not**: 21 W at 5–20 % CPU is far
   below clean-room's 33–36 W and close to the default profile. So the two
   nodes in the course data were most likely tuned differently.
3. **Clean-room has no sleep step.** Power is nearly flat up to about 10 %
   CPU and then rises roughly in a line (package ≈ 33.3 + 33.0·r over all
   three workloads), so `awake` has little to model here.

## The models on clean-room

Node power MAE, package / DRAM, W:

| workload | `coef.json` (course data) | `coef-default-profile.json` | mean measured |
| --- | --- | --- | --- |
| cpu-bound | **6.39 / 1.24** | 11.55 / 3.59 | 45.2 / 5.6 |
| memory-bound | 11.28 / 5.26 | **8.67 / 4.91** | 45.2 / 10.3 |
| io-bound | **8.09 / 1.56** | 13.71 / 3.55 | 32.5 / 5.7 |

The default-profile model is 10–14 W too low here: it expects deep sleep
that clean-room never allows. The old model, fitted partly on clean-room
data, does better except on memory-bound, where neither sees the memory
traffic. **The tuning profile matters more than anything else we have
measured**, so a model has to be told which profile it runs under.
