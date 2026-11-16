# lab-1116-io2: second I/O-bound run on sut-01 (16 Nov 2026)

A repeat of [`lab-1116-io`](../lab-1116-io/README.md) (booking 106,
16:37–16:54 UTC, same workload and setup). It is **held-out test data** for
`python/coef-default-profile.json`, which trains on lab-1115 and lab-1116-io.

Node power MAE (`tools/score.py`, 98 steps): `coef.json` 4.42 W package /
1.55 W DRAM, `coef-default-profile.json` **2.20 / 0.91** (package energy
+0.2 %, DRAM −7.3 %).

The two I/O runs agree closely: per-phase means differ by +0.12 W package on
average (at most 1.17 W) and +0.01 W DRAM (at most 0.41 W).
