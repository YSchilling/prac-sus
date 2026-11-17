# sqlite-app: a small real application workload

A shop inventory CLI on SQLite: products, orders with line items, and stock
movements. Python 3.13 standard library only (`sqlite3`), one process and one
connection per measured phase, synthetic data only. It complements the
synthetic stress Jobs with a real query, transaction and fsync mix. It reports
what it did (operations, latency, errors). It measures no power and records
no truth.

| File | Purpose |
|---|---|
| `app.py` | CLI: `prepare`, `search`, `report`, `update`, `import`, `clone`, `verify` |
| `schema.sql` | Tables (STRICT, integer cents, CHECK and FK constraints); indexes after the `-- @indexes` marker |
| `run.sh` | Phase driver: UTC marks for `tools/lab_windows.py`, `full` or `smoke` profile |
| `Dockerfile` | `python:3.13-slim` pinned by digest, app files only, runs as UID 65534 |
| `test_app.py` | `python3 -m unittest discover -s workloads/sqlite-app -v` |
| `../sqlite-app.yaml` | Kubernetes Job `sqlite-app`, compatible with `tools/lab_repeat.sh` |

## Data and operations

`prepare --dir NEW --preset P --seed S` writes into a new or empty directory
and refuses anything else:

- `products.csv` (catalog with initial stock) and `receipts.csv` (supplier
  stock receipts for `import`), both generated from the seed;
- `base.db`, loaded from `products.csv` plus a year of sales history
  (2025, simulated clock), with indexes built and `ANALYZE` run after the bulk
  load, then verified and checkpointed;
- `manifest.json`: preset, seed, row counts and a logical checksum over every
  row.

Initial stock is the units sold in the history plus a random margin, so stock
never goes negative. Every product's stock equals the sum of its movements.

| Preset | Products | Orders | Order lines | `base.db` | CSVs | Prepare on free-dev |
|---|---:|---:|---:|---:|---:|---:|
| `tiny` (tests, smoke) | 300 | 3,000 | 7,889 | 0.7 MB | 21 KB | 0.2 s |
| `small` | 5,000 | 50,000 | 130,870 | 12.0 MB | 0.4 MB | 3.3 s |
| `medium` (Job default) | 50,000 | 500,000 | 1,312,947 | 125.1 MB | 3.5 MB | 39 s |

Sizes are with seed 42 on SQLite 3.46.1. `prepare` and every phase log the
actual file sizes. Both lab presets are far smaller than the SUT's RAM. The
medium database is larger than SQLite's own 64 MiB cache but fits in the OS
page cache.

Measured commands (each opens one connection, then measures):

- **search** (read-only, `query_only=ON`): product by id (primary key), by SKU
  (unique index), by category and price range (`products_category_price`), by
  name prefix (`products_name`). Products are picked with a popularity skew.
- **report** (read-only): category revenue over 30 days, top 20 products over
  14 days, daily revenue over 90 days (joins over `orders_created_at`,
  grouping, sorting), and a low-stock list (table scan).
- **update**: 90 % orders, 10 % restocks. An order is one `BEGIN IMMEDIATE`
  transaction: insert the order, then for each line decrement stock only
  if enough is left, insert the line, and insert a sale movement. If a line
  lacks stock, the whole order rolls back and counts as `order_rejected` (a
  business outcome, reported separately). Any other error rolls back and
  aborts the run.
- **import**: `receipts.csv` applied in transactions of `--batch` rows (stock
  increase plus a restock movement per row), cycling through the file. One
  operation is one batch.

All SQL uses parameters. Timestamps of new rows come from a simulated clock
that continues after the newest stored event, not from wall time.

`verify` runs `integrity_check`, `foreign_key_check` and the invariants: no
negative stock, stock = sum of movements, order total and item count = its
lines, one sale movement per line. Any violation fails the command.

## Measurement settings

Applied and read back on every connection, and logged in each `config` event:

| PRAGMA | Value | Why |
|---|---|---|
| `journal_mode` | WAL | one writer, readers do not block |
| `synchronous` | FULL | every commit syncs the WAL; durability is not traded for speed |
| `cache_size` | -65536 (64 MiB) | fixed per connection |
| `mmap_size` | 0 | plain reads through the page cache |
| `temp_store` | MEMORY | sorts and groupings stay off disk |
| `wal_autocheckpoint` / `journal_size_limit` | 1000 pages / 64 MiB | bounds the WAL file |
| `foreign_keys` | ON | FK violations fail |

Every stdout line from `app.py` is one JSON object (it starts with `{`):

- `config`: runtime (Python, SQLite, platform), settings and file sizes before
  the phase;
- `measure_start`, then `progress` every 10 s (ops and interval rate);
- `result`: `ops`, `rows`, `errors` (always 0, since any error aborts with an
  `error` event and a non-zero exit), `rejected`, `elapsed_s`, `ops_per_s`,
  latency in µs (mean, min, p50, p90, p99, p99.9, max) overall and per
  operation kind, `measure_start_utc`/`measure_end_utc`, `stopped_by`,
  `schedule_resets` and `max_rss_kib`;
- `sizes`: database, WAL and SHM sizes after the phase.

Latency percentiles come from a log-bucketed histogram (about 4.4 % bucket
width, fixed memory); min, max and mean are exact. Only the `run.sh` marks
start with a UTC timestamp, so `tools/lab_windows.py` sees nothing else.

`--rate R` caps operations per second. The runner paces against a fixed
schedule and catches up small lags (sleep jitter, an fsync spike). If it falls
more than max(one interval, 50 ms) behind, it restarts the schedule instead of
bursting, and counts a `schedule_resets`. Without `--rate` it runs
unrestricted. Compare `ops_per_s` with the target: a cap above what the SUT
can do is just another unrestricted phase.

## Phases (`run.sh`)

`run.sh <new-data-dir>` refuses a non-empty directory, logs the storage
backing (`findmnt`, `df`) and the configuration, and runs `prepare` (not
marked). Then:

| Mark | Full profile | What runs |
|---|---|---|
| `idle` | 90 s | nothing |
| `warmup` | 60 s | search, then report on `base.db`; warms accessed pages, not a measured phase |
| `search rate=1000/5000/max` | 90 s each | search on `base.db` |
| `report rate=1/3/max` | 90 s each | report on `base.db` |
| `restore` | ~20 s | delete the previous work copy, `clone` `base.db` to `work.db` (backup API), settle |
| `update rate=50/200/max` | 90 s each | update on `work.db` (cap 300,000 ops) |
| `verify` | ~10-20 s | `verify` the work copy, then delete it |
| `import batch=1/100` | 60 s each | import on `work.db` (cap 3,000,000 rows) |
| `idle` | 90 s | nothing |
| `done` | | |

Each write phase is preceded by `restore` and followed by `verify`. Their
windows contain I/O and CPU work and are not idle. The full profile takes
about 25 minutes including the medium prepare. The Job deadline is 35 minutes
and the `lab_repeat.sh` wait is 40.

Override any of this with environment variables: `PROFILE` (`full`, `smoke`),
`PRESET`, `SEED`, `IDLE_S`, `WARMUP_S`, `PHASE_S`, `IMPORT_S`, `SETTLE_S`,
`SEARCH_RATES`, `REPORT_RATES`, `UPDATE_RATES`, `IMPORT_BATCHES`,
`UPDATE_MAX_OPS`, `IMPORT_MAX_ROWS`, `REQUIRE_DISK_FS=1` (fail unless the
data directory is on ext4/XFS). `PROFILE=smoke` uses `tiny`, 2 s phases and
fewer rates (about 30 s in total).

Unrestricted throughput on free-dev (8 vCPU, ext4 on `/dev/sda1`, medium,
5-10 s probes, not a lab result): search ≈ 37,600 ops/s, report ≈ 10 ops/s,
update ≈ 800 ops/s (fsync-bound), import ≈ 1,500 batches/s at batch 1 and
≈ 120 at batch 100. The capped rates sit below these values. Check that they
are also below the SUT's on the first lab run (`schedule_resets` = 0 and
`ops_per_s` ≈ target). Otherwise lower them via the environment.

## Determinism and repeating runs

Deterministic: the CSVs, `base.db` content (`manifest.json` checksum; equal
under Python 3.13.18 and 3.14.4 for the same seed), and the sequence of
operations and their parameters for a given `--seed`. With `--ops N` the
whole outcome is deterministic: `update --ops 300 --seed 7` on two copies
gives identical databases.

Not deterministic: timings, and therefore how many operations a
`--duration` phase completes. The same sequence is cut at a different point.

The database drifts as writes accumulate. Updates and imports add rows and
change stock, and SQLite pages split as indexes fill (medium grows about
2 KB per update op at first). Reads would then see a different database.
To keep runs comparable:

- every Job run prepares a new dataset from the same seed (the emptyDir starts
  empty), so every run starts from the same content;
- search and report only read `base.db`, before any write phase;
- every write phase runs on a fresh copy of `base.db`, so all update and
  import phases start from identical content regardless of rate or order;
- `--max-ops` bounds growth per phase, and each work copy is deleted after it
  is verified. Disk use peaks at `base.db` plus one work copy plus its WAL
  (about 1 GB at most for medium under the caps).

The cache is warmed by prior activity; full residency is not guaranteed. Nothing drops the OS page cache (that needs
privileges the Job does not have). `prepare`, `clone` and the warm-up leave
data in memory. Physical layout on disk is not controlled either.

## Run locally

```sh
python3 -m unittest discover -s workloads/sqlite-app -v
d=$(mktemp -d ~/tmp/sqlite-app.XXXXXX)
PROFILE=smoke workloads/sqlite-app/run.sh "$d/run" > "$d/r1-sqlite-app.log"
tools/lab_windows.py --node local "$d/r1-sqlite-app.log"
```

Single commands, for example:

```sh
python3 workloads/sqlite-app/app.py prepare --dir "$d/data" --preset small --seed 42
python3 workloads/sqlite-app/app.py report --db "$d/data/base.db" --duration 10 --rate 2
```

Container (no network, read-only root, a task-owned name):

```sh
docker build -t sqlite-app:dev workloads/sqlite-app
docker run --rm --network none --read-only --user "$(id -u):$(id -g)" \
  -e PROFILE=smoke -e TMPDIR=/data -v "$d:/data" sqlite-app:dev
```

## Run in the lab

The image is not in any registry. The Job uses `image: sqlite-app:dev` with
`imagePullPolicy: Never`, so the image must already be in the containerd
image store (`k8s.io` namespace) of the SUT node that runs it. Otherwise the
pod fails with `ErrImageNeverPull`. One way, for each SUT you book:

```sh
docker build -t sqlite-app:dev workloads/sqlite-app        # x86_64, like the SUTs
docker save sqlite-app:dev -o sqlite-app.tar
scp sqlite-app.tar <sut-node>:
ssh <sut-node> 'sudo ctr -n k8s.io images import sqlite-app.tar && sudo ctr -n k8s.io images ls | grep sqlite-app'
```

This needs SSH and root (or containerd socket) access on the SUT node.
Nobody has verified that a lab booking grants either. If it does not, ask
the lab staff how to load a local image, and do not push it to a public
registry. Then, on the cp node:

```sh
kubectl apply -f prac-sus/workloads/sqlite-app.yaml        # or via lab_repeat.sh
kubectl logs -f job/sqlite-app
```

Run it alone, not next to the other Jobs. The Job asks for 1 CPU and 1 GiB
(limit 2 GiB; no CPU limit, which would throttle) and a 4 GiB disk-backed
emptyDir on the node's kubelet storage, and requires ext4/XFS. The emptyDir is
released when the Job is deleted. `lab_repeat.sh` deletes and re-applies it,
so every round starts from a fresh dataset.

To analyse a run, select `"event":"result"` lines from the Job log (for
example `grep '^{' r1-sqlite-app.log | jq 'select(.event=="result")'`) and
match `label` to the phase mark. `lab_windows.py` normalizes that mark
(removing `=` and replacing spaces with `-`), so normalize labels the same way
when joining. Exclude `warmup`, `restore` and `verify` windows from measured
application phases. Remove your local scratch directory when finished.

## Local verification

The lead reviewed and integrated this workload without committing or deploying.
On free-dev, 31 application tests and all 12 existing model tests passed. The
pinned image built successfully; its no-network, read-only-root smoke profile
completed with 10 operation results, four successful integrity checks and 19
phase windows parsed by `tools/lab_windows.py`. The application tests include
custom one-product datasets, which must not hang generating multi-item orders.

The Job YAML was parsed and its placement/storage settings checked locally.
It has not been validated against a Kubernetes API server or executed in the
lab. The full-length profile has not been run end to end; its duration remains
an estimate. Local smoke runs verify functionality, not physical power accuracy.

## Limits

- Never run in the lab yet. The rates are calibrated on free-dev only.
- One writer and one connection per phase. There is no concurrency and no
  network service, so this is not a model of a multi-client server.
- The warm page cache is not controlled, and nothing measures cold-cache
  behaviour.
- RAPL measures package and DRAM, not the disk. Fsync-bound phases show
  mostly waiting.
