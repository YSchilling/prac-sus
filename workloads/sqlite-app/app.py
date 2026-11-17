#!/usr/bin/env python3
"""Single-process SQLite shop workload: prepare, search, report, update, import.

    app.py prepare --dir DATA --preset small --seed 42
    app.py search  --db DATA/base.db --seed 1 --duration 90 [--rate 500]
    app.py report  --db DATA/base.db --seed 2 --duration 90 [--rate 4]
    app.py clone   --src DATA/base.db --dst DATA/work.db
    app.py update  --db DATA/work.db --seed 3 --duration 90 [--rate 100] [--max-ops N]
    app.py import  --db DATA/work.db --csv DATA/receipts.csv --batch 100 --seed 4 --ops 50
    app.py verify  --db DATA/work.db

Python standard library only. One connection per command. Every line written
to stdout is a JSON object (it starts with "{"), so it never looks like the
"<UTC> <phase> start" marks run.sh prints for tools/lab_windows.py. Failures
print an "error" event, a traceback on stderr and exit non-zero.
"""
import argparse
import csv
import hashlib
import json
import math
import os
import platform
import random
import resource
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = Path(__file__).resolve().with_name("schema.sql")
INDEX_MARKER = "-- @indexes"

# Dataset sizes. tiny is for tests and smoke runs; small and medium are the lab
# presets. run.sh and prepare log the resulting file sizes.
PRESETS = {
    "tiny": {"products": 300, "orders": 3_000, "customers": 500, "receipts": 1_000},
    "small": {"products": 5_000, "orders": 50_000, "customers": 10_000, "receipts": 20_000},
    "medium": {"products": 50_000, "orders": 500_000, "customers": 100_000, "receipts": 100_000},
}

# Fixed connection settings, applied and read back on every connection.
# WAL with synchronous=FULL syncs the WAL on every commit: commits are durable
# and pay the real fsync cost. Nothing here trades durability for speed.
PRAGMAS = (
    ("journal_mode", "WAL", "wal"),
    ("synchronous", "FULL", 2),
    ("foreign_keys", "ON", 1),
    ("cache_size", "-65536", -65536),          # 64 MiB page cache per connection
    ("temp_store", "MEMORY", 2),
    ("mmap_size", "0", 0),                     # plain read() I/O, no memory mapping
    ("wal_autocheckpoint", "1000", 1000),      # pages; bounds WAL growth
    ("journal_size_limit", "67108864", 67108864),
    ("busy_timeout", "5000", 5000),
)

HISTORY_START = 1_735_689_600   # 2025-01-01T00:00:00Z, simulated clock
HISTORY_DAYS = 365
DAY = 86_400

CATEGORIES = (
    "audio", "baby", "bags", "bath", "bedding", "bikes", "books", "cameras",
    "camping", "cleaning", "coffee", "computers", "cookware", "crafts", "desk",
    "fitness", "flooring", "games", "garden", "grocery", "hardware", "health",
    "kitchen", "lighting", "luggage", "music", "office", "outdoor", "paint",
    "party", "pets", "phones", "plumbing", "shoes", "sports", "storage",
    "tea", "tools", "toys", "watches",
)
ADJECTIVES = (
    "Amber", "Bold", "Brisk", "Calm", "Clear", "Compact", "Cozy", "Crisp",
    "Deep", "Eager", "Fine", "Fresh", "Grand", "Handy", "Keen", "Light",
    "Lucky", "Mellow", "Neat", "Noble", "Plain", "Prime", "Quick", "Quiet",
    "Rapid", "Royal", "Sharp", "Silver", "Smart", "Solid", "Sturdy", "Swift",
)
NOUNS = (
    "Basket", "Blender", "Bottle", "Brush", "Cable", "Chair", "Clock", "Cup",
    "Drill", "Fan", "Funnel", "Glove", "Hammer", "Helmet", "Jacket", "Kettle",
    "Lamp", "Mat", "Mug", "Pan", "Pen", "Pillow", "Pump", "Rack", "Scale",
    "Shelf", "Speaker", "Stand", "Tent", "Towel", "Tray", "Vase",
)

SEARCH_MIX = (("by_id", 40), ("by_sku", 20), ("by_category", 25), ("name_prefix", 15))
REPORT_MIX = (("category_revenue", 30), ("top_products", 30), ("daily_revenue", 25), ("low_stock", 15))
UPDATE_MIX = (("order", 90), ("restock", 10))


class OutOfStock(Exception):
    """An order line asked for more units than are in stock. The order is rolled back."""


# --- logging ----------------------------------------------------------------

def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def emit(event, **fields):
    print(json.dumps({"event": event, "ts": utc_now(), **fields}, separators=(",", ":")), flush=True)


def file_sizes(*paths):
    sizes = {}
    for p in paths:
        p = str(p)
        candidates = [p, p + "-wal", p + "-shm"] if p.endswith(".db") else [p]
        for c in candidates:
            sizes[os.path.basename(c)] = os.path.getsize(c) if os.path.exists(c) else None
    return sizes


def runtime_info():
    return {
        "python": platform.python_version(),
        "python_impl": platform.python_implementation(),
        "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "pid": os.getpid(),
    }


def max_rss_kib():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


# --- connection ---------------------------------------------------------------

def connect(path, *, create=False, query_only=False):
    """Open one connection with the fixed PRAGMAs, verify them and return it with their values."""
    path = str(path)
    if not create and not os.path.isfile(path):
        raise FileNotFoundError(f"database {path} does not exist; run prepare first")
    if create and os.path.exists(path):
        raise FileExistsError(f"refusing to overwrite existing database {path}")
    # isolation_level=None: no implicit transactions; every write uses explicit BEGIN/COMMIT.
    con = sqlite3.connect(path, isolation_level=None, timeout=5.0)
    settings = {}
    for name, value, expected in PRAGMAS:
        con.execute(f"PRAGMA {name}={value}").fetchall()
        actual = con.execute(f"PRAGMA {name}").fetchone()[0]
        if actual != expected:
            con.close()
            raise RuntimeError(f"PRAGMA {name}: expected {expected!r}, got {actual!r}")
        settings[name] = actual
    con.execute("PRAGMA query_only=" + ("ON" if query_only else "OFF"))
    settings["query_only"] = con.execute("PRAGMA query_only").fetchone()[0]
    settings["page_size"] = con.execute("PRAGMA page_size").fetchone()[0]
    return con, settings


def schema_parts():
    text = SCHEMA.read_text()
    if INDEX_MARKER not in text:
        raise RuntimeError(f"{SCHEMA} lacks the {INDEX_MARKER} marker")
    tables, rest = text.split(INDEX_MARKER, 1)
    return tables, rest.split("\n", 1)[1]  # drop the rest of the marker comment line


# --- deterministic data generation ----------------------------------------------

def skewed_index(rng, n):
    """0..n-1, low indexes more popular (density ~ 1/sqrt(x))."""
    return min(n - 1, int(n * rng.random() ** 2))


def generate_products(seed, n):
    rng = random.Random(f"{seed}:products")
    rows = []
    for pid in range(1, n + 1):
        name = f"{rng.choice(ADJECTIVES)} {rng.choice(NOUNS)} {pid:06d}"
        category = rng.choice(CATEGORIES)
        price = max(50, int(round(math.exp(rng.gauss(7.0, 0.9)))))  # median about 11 EUR
        rows.append([pid, f"SKU-{pid:07d}", name, category, price])
    return rows


def generate_orders(seed, n_orders, n_products, n_customers):
    """Yield (order_id, created_at, customer_id, [(product_id, quantity), ...]) in time order."""
    rng = random.Random(f"{seed}:orders")
    span = HISTORY_DAYS * DAY
    for oid in range(1, n_orders + 1):
        created_at = HISTORY_START + (oid - 1) * span // n_orders + rng.randrange(max(1, span // n_orders))
        k = min(n_products, rng.choice((1, 1, 2, 2, 3, 3, 4, 5)))
        picked = {}
        while len(picked) < k:
            pid = 1 + skewed_index(rng, n_products)
            if pid not in picked:
                picked[pid] = rng.choice((1, 1, 1, 2, 2, 3))
        yield oid, created_at, rng.randint(1, n_customers), list(picked.items())


def generate_receipts(seed, n, n_products):
    rng = random.Random(f"{seed}:receipts")
    return [[1 + skewed_index(rng, n_products), rng.randint(5, 60)] for _ in range(n)]


def write_csv(path, header, rows):
    with open(path, "x", newline="") as f:  # "x": never overwrite
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def read_csv(path, header):
    """Rows of a CSV after checking its exact header."""
    with open(path, newline="") as f:
        r = csv.reader(f)
        got = next(r, None)
        if got != list(header):
            raise ValueError(f"{path}: expected header {list(header)}, got {got}")
        return [row for row in r]


def load_products_csv(path):
    header = ("id", "sku", "name", "category", "price_cents", "initial_stock")
    out = []
    for lineno, row in enumerate(read_csv(path, header), start=2):
        if len(row) != len(header):
            raise ValueError(f"{path}:{lineno}: expected {len(header)} fields")
        pid, price, stock = int(row[0]), int(row[4]), int(row[5])
        if pid <= 0 or price <= 0 or stock < 0 or not row[1] or not row[2] or not row[3]:
            raise ValueError(f"{path}:{lineno}: invalid product row {row}")
        out.append((pid, row[1], row[2], row[3], price, stock))
    return out


def load_receipts_csv(path):
    header = ("product_id", "quantity")
    out = []
    for lineno, row in enumerate(read_csv(path, header), start=2):
        if len(row) != 2:
            raise ValueError(f"{path}:{lineno}: expected 2 fields")
        pid, qty = int(row[0]), int(row[1])
        if pid <= 0 or qty <= 0:
            raise ValueError(f"{path}:{lineno}: invalid receipt row {row}")
        out.append((pid, qty))
    if not out:
        raise ValueError(f"{path}: no receipt rows")
    return out


# --- commands: prepare / clone / verify ------------------------------------------

def cmd_prepare(a):
    cfg = dict(PRESETS[a.preset])
    for key in ("products", "orders"):
        if getattr(a, key) is not None:
            cfg[key] = getattr(a, key)
    d = Path(a.dir)
    if d.exists() and (not d.is_dir() or any(d.iterdir())):
        raise FileExistsError(f"refusing to prepare into non-empty {d}; choose a new directory")
    d.mkdir(parents=True, exist_ok=True)
    products_csv, receipts_csv, db = d / "products.csv", d / "receipts.csv", d / "base.db"
    emit("prepare_start", dir=str(d), preset=a.preset, seed=a.seed, config=cfg, runtime=runtime_info())
    t0 = time.perf_counter()

    # Pass 1: simulate the sales history to know how many units each product sells.
    products = generate_products(a.seed, cfg["products"])
    sold = [0] * (cfg["products"] + 1)
    for _, _, _, lines in generate_orders(a.seed, cfg["orders"], cfg["products"], cfg["customers"]):
        for pid, qty in lines:
            sold[pid] += qty
    extra = random.Random(f"{a.seed}:stock")
    for row in products:
        row.append(sold[row[0]] + extra.randint(50, 2_000))  # initial_stock covers all sales
    write_csv(products_csv, ("id", "sku", "name", "category", "price_cents", "initial_stock"), products)
    write_csv(receipts_csv, ("product_id", "quantity"),
              generate_receipts(a.seed, cfg["receipts"], cfg["products"]))
    del products

    con, settings = connect(db, create=True)
    tables, indexes = schema_parts()
    con.executescript(tables)
    catalog = load_products_csv(products_csv)
    price = {p[0]: p[4] for p in catalog}
    initial_at = HISTORY_START - DAY
    con.execute("BEGIN")
    con.executemany("INSERT INTO products (id, sku, name, category, price_cents, stock) VALUES (?,?,?,?,?,?)", catalog)
    con.executemany("INSERT INTO stock_movements (product_id, created_at, delta, reason) VALUES (?,?,?,'initial')",
                    [(p[0], initial_at, p[5]) for p in catalog if p[5] > 0])
    con.execute("COMMIT")

    # Pass 2: the same orders again (same seed), now written in batches.
    batch_orders, batch_items, batch_moves = [], [], []

    def flush():
        con.execute("BEGIN")
        con.executemany("INSERT INTO orders (id, created_at, customer_id, item_count, total_cents) VALUES (?,?,?,?,?)", batch_orders)
        con.executemany("INSERT INTO order_items (order_id, line_no, product_id, quantity, unit_price_cents) VALUES (?,?,?,?,?)", batch_items)
        con.executemany("INSERT INTO stock_movements (product_id, created_at, delta, reason, order_id) VALUES (?,?,?,'sale',?)", batch_moves)
        con.execute("COMMIT")
        batch_orders.clear(); batch_items.clear(); batch_moves.clear()

    for oid, created_at, customer, lines in generate_orders(a.seed, cfg["orders"], cfg["products"], cfg["customers"]):
        total = 0
        for line_no, (pid, qty) in enumerate(lines, start=1):
            batch_items.append((oid, line_no, pid, qty, price[pid]))
            batch_moves.append((pid, created_at, -qty, oid))
            total += qty * price[pid]
        batch_orders.append((oid, created_at, customer, len(lines), total))
        if len(batch_orders) >= 10_000:
            flush()
    if batch_orders:
        flush()
    con.execute("BEGIN")
    con.executemany("UPDATE products SET stock = stock - ? WHERE id = ?",
                    [(sold[pid], pid) for pid in range(1, cfg["products"] + 1) if sold[pid]])
    con.execute("COMMIT")
    t_load = time.perf_counter()
    con.executescript(indexes)
    con.execute("ANALYZE")
    t_index = time.perf_counter()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    con.close()

    report = verify_db(db)
    manifest = {"preset": a.preset, "seed": a.seed, "config": cfg, "counts": report["counts"],
                "checksum": report["checksum"], "sqlite": sqlite3.sqlite_version,
                "python": platform.python_version(), "settings": settings}
    with open(d / "manifest.json", "x") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    emit("prepare_done", seconds_load=round(t_load - t0, 3), seconds_index=round(t_index - t_load, 3),
         seconds_total=round(time.perf_counter() - t0, 3), counts=report["counts"], checksum=report["checksum"],
         settings=settings, sizes=file_sizes(products_csv, receipts_csv, db, d / "manifest.json"))


def cmd_clone(a):
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(a.dst + suffix):
            raise FileExistsError(f"refusing to overwrite {a.dst + suffix}")
    t0 = time.perf_counter()
    src, _ = connect(a.src, query_only=True)
    dst, settings = connect(a.dst, create=True)
    src.backup(dst)  # consistent page copy, including committed WAL content
    dst.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    src.close(); dst.close()
    emit("clone_done", src=a.src, dst=a.dst, seconds=round(time.perf_counter() - t0, 3),
         settings=settings, sizes=file_sizes(a.src, a.dst))


def logical_checksum(con):
    """SHA-256 over every row of every table in key order: equal content, equal checksum."""
    h = hashlib.sha256()
    for sql in ("SELECT id, sku, name, category, price_cents, stock FROM products ORDER BY id",
                "SELECT id, created_at, customer_id, item_count, total_cents FROM orders ORDER BY id",
                "SELECT order_id, line_no, product_id, quantity, unit_price_cents FROM order_items ORDER BY order_id, line_no",
                "SELECT id, product_id, created_at, delta, reason, order_id FROM stock_movements ORDER BY id"):
        for row in con.execute(sql):
            h.update(repr(row).encode())
            h.update(b"\n")
    return h.hexdigest()


def verify_db(path, checksum=True):
    """Check integrity, foreign keys and business invariants. Raises on any violation."""
    con, _ = connect(path, query_only=True)
    try:
        problems = []
        ic = con.execute("PRAGMA integrity_check").fetchall()
        if ic != [("ok",)]:
            problems.append(f"integrity_check: {ic[:5]}")
        fk = con.execute("PRAGMA foreign_key_check").fetchall()
        if fk:
            problems.append(f"foreign_key_check: {len(fk)} violations, first {fk[:5]}")
        checks = {
            "negative stock": "SELECT count(*) FROM products WHERE stock < 0",
            "stock != sum of movements": """
                SELECT count(*) FROM products p
                LEFT JOIN (SELECT product_id, sum(delta) AS s FROM stock_movements GROUP BY product_id) m
                       ON m.product_id = p.id
                WHERE p.stock != coalesce(m.s, 0)""",
            "order total/item_count != its lines": """
                SELECT count(*) FROM orders o
                LEFT JOIN (SELECT order_id, sum(quantity * unit_price_cents) AS t, count(*) AS n
                           FROM order_items GROUP BY order_id) i ON i.order_id = o.id
                WHERE o.total_cents != coalesce(i.t, -1) OR o.item_count != coalesce(i.n, -1)""",
            "order line without its sale movement": """
                SELECT count(*) FROM (
                    SELECT order_id, product_id, -quantity FROM order_items
                    EXCEPT
                    SELECT order_id, product_id, delta FROM stock_movements WHERE reason = 'sale')""",
            "sale movements != order lines": """
                SELECT (SELECT count(*) FROM stock_movements WHERE reason = 'sale')
                     - (SELECT count(*) FROM order_items)""",
        }
        for label, sql in checks.items():
            n = con.execute(sql).fetchone()[0]
            if n:
                problems.append(f"{label}: {n}")
        if problems:
            raise RuntimeError(f"{path} failed verification: " + "; ".join(problems))
        counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                  for t in ("products", "orders", "order_items", "stock_movements")}
        return {"counts": counts, "checksum": logical_checksum(con) if checksum else None}
    finally:
        con.close()


def cmd_verify(a):
    t0 = time.perf_counter()
    report = verify_db(a.db, checksum=a.checksum)
    emit("verify_ok", db=a.db, seconds=round(time.perf_counter() - t0, 3), **report, sizes=file_sizes(a.db))


# --- measured operations -----------------------------------------------------------

class Histogram:
    """Log-bucketed latency histogram: fixed memory, ~4.4 % bucket width."""
    PER_OCTAVE = 16

    def __init__(self):
        self.buckets = {}
        self.count = 0
        self.total = 0
        self.min = None
        self.max = 0

    def add(self, ns):
        ns = max(1, ns)
        b = int(math.log2(ns) * self.PER_OCTAVE)
        self.buckets[b] = self.buckets.get(b, 0) + 1
        self.count += 1
        self.total += ns
        self.min = ns if self.min is None else min(self.min, ns)
        self.max = max(self.max, ns)

    def quantile(self, q):
        """Upper edge of the bucket holding the q-quantile, capped at the observed max."""
        target = max(1, math.ceil(q * self.count))
        seen = 0
        for b in sorted(self.buckets):
            seen += self.buckets[b]
            if seen >= target:
                return min(self.max, 2 ** ((b + 1) / self.PER_OCTAVE))
        return self.max

    def summary_us(self):
        if not self.count:
            return {"count": 0}
        us = lambda ns: round(ns / 1000, 1)
        return {"count": self.count, "mean": us(self.total / self.count), "min": us(self.min),
                "p50": us(self.quantile(0.50)), "p90": us(self.quantile(0.90)),
                "p99": us(self.quantile(0.99)), "p999": us(self.quantile(0.999)), "max": us(self.max)}


def weighted_choice(rng, mix):
    total = sum(w for _, w in mix)
    x = rng.random() * total
    for name, w in mix:
        x -= w
        if x < 0:
            return name
    return mix[-1][0]


def prefix_upper(prefix):
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


class Shop:
    """The operations, bound to one connection. op_* return (kind, rows)."""

    def __init__(self, con):
        self.con = con
        self.n_products = con.execute("SELECT max(id) FROM products").fetchone()[0]
        if not self.n_products:
            raise RuntimeError("database has no products")
        self.categories = [r[0] for r in con.execute("SELECT DISTINCT category FROM products ORDER BY category")]
        self.max_customer = con.execute("SELECT coalesce(max(customer_id), 1) FROM orders").fetchone()[0]
        # Simulated clock: continues after the newest recorded event, so new rows
        # never depend on wall time.
        self.clock = 1 + max(HISTORY_START + HISTORY_DAYS * DAY,
                             con.execute("SELECT coalesce(max(created_at), 0) FROM orders").fetchone()[0],
                             con.execute("SELECT coalesce(max(created_at), 0) FROM stock_movements").fetchone()[0])

    def tick(self):
        self.clock += 1
        return self.clock

    def product_id(self, rng):
        return 1 + skewed_index(rng, self.n_products)

    # search: indexed point and range lookups
    def search(self, rng):
        kind = weighted_choice(rng, SEARCH_MIX)
        c = self.con
        if kind == "by_id":
            rows = c.execute("SELECT id, sku, name, category, price_cents, stock FROM products WHERE id = ?",
                             (self.product_id(rng),)).fetchall()
            if len(rows) != 1:
                raise RuntimeError("product lookup by id found no row")
        elif kind == "by_sku":
            rows = c.execute("SELECT id, sku, name, category, price_cents, stock FROM products WHERE sku = ?",
                             (f"SKU-{self.product_id(rng):07d}",)).fetchall()
            if len(rows) != 1:
                raise RuntimeError("product lookup by sku found no row")
        elif kind == "by_category":
            lo = rng.choice((100, 300, 600, 1000, 2000))
            rows = c.execute("""SELECT id, name, price_cents, stock FROM products
                                WHERE category = ? AND price_cents BETWEEN ? AND ?
                                ORDER BY price_cents, id LIMIT 20""",
                             (rng.choice(self.categories), lo, lo * 3)).fetchall()
        else:
            prefix = f"{rng.choice(ADJECTIVES)} {rng.choice(NOUNS)}"
            rows = c.execute("""SELECT id, name, price_cents, stock FROM products
                                WHERE name >= ? AND name < ? ORDER BY name LIMIT 20""",
                             (prefix, prefix_upper(prefix))).fetchall()
        return kind, len(rows)

    # report: aggregation, grouping and sorting over the sales history
    def report(self, rng):
        kind = weighted_choice(rng, REPORT_MIX)
        c = self.con
        if kind == "category_revenue":
            start = HISTORY_START + rng.randrange(HISTORY_DAYS - 30) * DAY
            rows = c.execute(REPORT_SQL["category_revenue"], (start, start + 30 * DAY)).fetchall()
        elif kind == "top_products":
            start = HISTORY_START + rng.randrange(HISTORY_DAYS - 14) * DAY
            rows = c.execute(REPORT_SQL["top_products"], (start, start + 14 * DAY)).fetchall()
        elif kind == "daily_revenue":
            start = HISTORY_START + rng.randrange(HISTORY_DAYS - 90) * DAY
            rows = c.execute(REPORT_SQL["daily_revenue"], (start, start + 90 * DAY)).fetchall()
        else:
            rows = c.execute(REPORT_SQL["low_stock"], (rng.choice((100, 200, 400)),)).fetchall()
        return kind, len(rows)

    # update: atomic order placement or restock
    def update(self, rng):
        if weighted_choice(rng, UPDATE_MIX) == "restock":
            self.restock([(self.product_id(rng), rng.randint(20, 200))])
            return "restock", 1
        picked = {}
        k = min(self.n_products, rng.choice((1, 1, 2, 2, 3, 3, 4)))
        while len(picked) < k:
            pid = self.product_id(rng)
            if pid not in picked:
                picked[pid] = rng.choice((1, 1, 1, 2, 2, 3))
        customer = rng.randint(1, self.max_customer)
        try:
            self.place_order(customer, list(picked.items()))
        except OutOfStock:
            return "order_rejected", 0
        return "order", len(picked)

    def place_order(self, customer_id, lines, created_at=None):
        """Insert one order with its lines, decrement stock and record sale movements, atomically.

        Raises OutOfStock (after rolling back) if any line exceeds stock; any
        other error also rolls the whole order back and propagates.
        """
        if not lines:
            raise ValueError("an order needs at least one line")
        created_at = self.tick() if created_at is None else created_at
        c = self.con
        c.execute("BEGIN IMMEDIATE")
        try:
            priced = []
            for pid, qty in lines:
                if not isinstance(qty, int) or qty <= 0:
                    raise ValueError(f"invalid quantity {qty!r}")
                row = c.execute("SELECT price_cents FROM products WHERE id = ?", (pid,)).fetchone()
                if row is None:
                    raise LookupError(f"unknown product {pid}")
                priced.append((pid, qty, row[0]))
            total = sum(q * p for _, q, p in priced)
            oid = c.execute("INSERT INTO orders (created_at, customer_id, item_count, total_cents) VALUES (?,?,?,?)",
                            (created_at, customer_id, len(priced), total)).lastrowid
            for line_no, (pid, qty, unit) in enumerate(priced, start=1):
                cur = c.execute("UPDATE products SET stock = stock - ? WHERE id = ? AND stock >= ?", (qty, pid, qty))
                if cur.rowcount != 1:
                    raise OutOfStock(pid)
                c.execute("INSERT INTO order_items (order_id, line_no, product_id, quantity, unit_price_cents) "
                          "VALUES (?,?,?,?,?)", (oid, line_no, pid, qty, unit))
                c.execute("INSERT INTO stock_movements (product_id, created_at, delta, reason, order_id) "
                          "VALUES (?,?,?,'sale',?)", (pid, created_at, -qty, oid))
            c.execute("COMMIT")
            return oid
        except BaseException:
            if c.in_transaction:
                c.execute("ROLLBACK")
            raise

    def restock(self, lines):
        """Add received units and their movements in one transaction; unknown products abort it."""
        created_at = self.tick()
        c = self.con
        c.execute("BEGIN IMMEDIATE")
        try:
            cur = c.executemany("UPDATE products SET stock = stock + ? WHERE id = ?", [(q, p) for p, q in lines])
            if cur.rowcount != len(lines):
                raise LookupError(f"restock touched {cur.rowcount} products, expected {len(lines)}")
            c.executemany("INSERT INTO stock_movements (product_id, created_at, delta, reason) VALUES (?,?,?,'restock')",
                          [(p, created_at, q) for p, q in lines])
            c.execute("COMMIT")
        except BaseException:
            if c.in_transaction:
                c.execute("ROLLBACK")
            raise


REPORT_SQL = {
    "category_revenue": """
        SELECT p.category, count(DISTINCT o.id) AS orders, sum(oi.quantity) AS units,
               sum(oi.quantity * oi.unit_price_cents) AS revenue_cents
        FROM orders o
        JOIN order_items oi ON oi.order_id = o.id
        JOIN products p ON p.id = oi.product_id
        WHERE o.created_at >= ? AND o.created_at < ?
        GROUP BY p.category
        ORDER BY revenue_cents DESC, p.category""",
    "top_products": """
        SELECT oi.product_id, p.name, sum(oi.quantity) AS units,
               sum(oi.quantity * oi.unit_price_cents) AS revenue_cents
        FROM orders o
        JOIN order_items oi ON oi.order_id = o.id
        JOIN products p ON p.id = oi.product_id
        WHERE o.created_at >= ? AND o.created_at < ?
        GROUP BY oi.product_id
        ORDER BY units DESC, oi.product_id
        LIMIT 20""",
    "daily_revenue": """
        SELECT created_at / 86400 AS day, count(*) AS orders, sum(total_cents) AS revenue_cents
        FROM orders
        WHERE created_at >= ? AND created_at < ?
        GROUP BY day
        ORDER BY day""",
    "low_stock": """
        SELECT id, name, category, stock FROM products
        WHERE stock < ?
        ORDER BY stock, id
        LIMIT 100""",
}


def run_measured(a, step, label_fields):
    """Run step(rng) until --duration or --ops/--max-ops; pace with --rate; emit metrics."""
    rng = random.Random(f"{a.seed}:{a.command}")
    interval = 1.0 / a.rate if a.rate else 0.0
    # Small lags (sleep jitter, an fsync spike) are caught up; beyond the slack
    # the schedule restarts, so a stall never turns into a long burst.
    slack = max(interval, 0.05)
    limit = min(x for x in (a.ops, a.max_ops) if x is not None) if (a.ops or a.max_ops) else math.inf
    hist = Histogram()
    kinds = {}
    ops = rows = rejected = schedule_resets = 0
    stopped_by = None

    emit("measure_start", command=a.command, label=a.label, seed=a.seed, rate_target=a.rate,
         duration_target_s=a.duration, ops_target=a.ops, max_ops=a.max_ops, **label_fields)
    start_utc = utc_now()
    t0 = time.perf_counter()
    deadline = t0 + a.duration if a.duration else math.inf
    next_due = t0
    next_progress = t0 + a.progress_interval
    p_ops, p_t = 0, t0
    while True:
        now = time.perf_counter()
        if ops >= limit:
            stopped_by = "ops" if a.ops is not None and ops >= a.ops else "max_ops"
            break
        if now >= deadline:
            stopped_by = "duration"
            break
        if interval:
            if now < next_due:
                time.sleep(min(next_due, deadline) - now)
                continue
            if now - next_due > slack:  # far behind: restart the schedule, no long burst
                schedule_resets += 1
                next_due = now
            next_due += interval
        s = time.perf_counter_ns()
        kind, n = step(rng)
        dt = time.perf_counter_ns() - s
        hist.add(dt)
        k = kinds.get(kind)
        if k is None:
            k = kinds[kind] = {"ops": 0, "rows": 0, "hist": Histogram()}
        k["ops"] += 1
        k["rows"] += n
        k["hist"].add(dt)
        ops += 1
        rows += n
        if kind.endswith("_rejected"):
            rejected += 1
        if a.progress_interval and time.perf_counter() >= next_progress:
            t = time.perf_counter()
            emit("progress", command=a.command, label=a.label, ops=ops, elapsed_s=round(t - t0, 3),
                 interval_ops_per_s=round((ops - p_ops) / (t - p_t), 2))
            p_ops, p_t = ops, t
            next_progress = t + a.progress_interval
    elapsed = time.perf_counter() - t0
    result = {
        "command": a.command, "label": a.label, "seed": a.seed, "stopped_by": stopped_by,
        "rate_target": a.rate, "duration_target_s": a.duration, "ops_target": a.ops, "max_ops": a.max_ops,
        "measure_start_utc": start_utc, "measure_end_utc": utc_now(), "elapsed_s": round(elapsed, 3),
        "ops": ops, "rows": rows, "errors": 0, "rejected": rejected, "schedule_resets": schedule_resets,
        "ops_per_s": round(ops / elapsed, 3) if elapsed > 0 else None,
        "latency_us": hist.summary_us(),
        "by_kind": {name: {"ops": v["ops"], "rows": v["rows"], "latency_us": v["hist"].summary_us()}
                    for name, v in sorted(kinds.items())},
        "max_rss_kib": max_rss_kib(),
        **label_fields,
    }
    if stopped_by == "max_ops" and a.duration:
        result["warning"] = "stopped by --max-ops before --duration; the phase ended early"
    emit("result", **result)
    return result


def measured_command(a):
    query_only = a.command in ("search", "report")
    con, settings = connect(a.db, query_only=query_only)
    try:
        sizes_before = file_sizes(a.db)
        extra = {}
        if a.command == "import":
            receipts = load_receipts_csv(a.csv)
            extra = {"batch": a.batch, "csv": a.csv, "csv_rows": len(receipts)}
            sizes_before.update(file_sizes(a.csv))
        emit("config", command=a.command, db=a.db, settings=settings, runtime=runtime_info(), sizes=sizes_before, **extra)
        shop = Shop(con)
        if a.command == "search":
            step = shop.search
        elif a.command == "report":
            step = shop.report
        elif a.command == "update":
            step = shop.update
        else:
            pos = [0]

            def step(_rng):
                lines = [receipts[(pos[0] + j) % len(receipts)] for j in range(a.batch)]
                pos[0] = (pos[0] + a.batch) % len(receipts)
                shop.restock(lines)
                return "import_batch", len(lines)
        result = run_measured(a, step, {"batch": a.batch} if a.command == "import" else {})
        emit("sizes", command=a.command, label=a.label, sizes=file_sizes(a.db))
        return result
    finally:
        con.close()


# --- CLI -----------------------------------------------------------------------

def positive_int(s):
    v = int(s)
    if v <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {s}")
    return v


def nonneg_int(s):
    v = int(s)
    if v < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {s}")
    return v


def positive_float(s):
    v = float(s)
    if not math.isfinite(v) or v <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive number, got {s}")
    return v


def parser():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="command", required=True)

    pr = sub.add_parser("prepare", help="generate CSVs and the seeded base.db into a new directory")
    pr.add_argument("--dir", required=True)
    pr.add_argument("--preset", choices=sorted(PRESETS), default="small")
    pr.add_argument("--seed", type=nonneg_int, default=42)
    pr.add_argument("--products", type=positive_int, help="override the preset")
    pr.add_argument("--orders", type=positive_int, help="override the preset")

    cl = sub.add_parser("clone", help="copy a database to a new file (refuses to overwrite)")
    cl.add_argument("--src", required=True)
    cl.add_argument("--dst", required=True)

    ve = sub.add_parser("verify", help="integrity, foreign key and stock/order invariants")
    ve.add_argument("--db", required=True)
    ve.add_argument("--checksum", action="store_true", help="also hash every row (slow on medium)")

    for name, help_ in (("search", "indexed product lookups"), ("report", "sales aggregation reports"),
                        ("update", "atomic orders and restocks"), ("import", "CSV stock receipts in batches")):
        m = sub.add_parser(name, help=help_)
        m.add_argument("--db", required=True)
        m.add_argument("--seed", type=nonneg_int, default=1)
        m.add_argument("--duration", type=positive_float, help="measured seconds")
        m.add_argument("--ops", type=positive_int, help="exact number of operations (smoke/tests)")
        m.add_argument("--max-ops", type=positive_int, help="safety cap; bounds database growth")
        m.add_argument("--rate", type=positive_float, help="operations/s cap; omit for unrestricted")
        m.add_argument("--label", default=name, help="free text copied into the metrics")
        m.add_argument("--progress-interval", type=nonneg_int, default=10, help="seconds; 0 disables")
        if name == "import":
            m.add_argument("--csv", required=True)
            m.add_argument("--batch", type=positive_int, default=100, help="receipt rows per transaction")
    return p


def main(argv=None):
    p = parser()
    a = p.parse_args(argv)
    if a.command in ("search", "report", "update", "import"):
        if a.duration is None and a.ops is None:
            p.error(f"{a.command}: give --duration and/or --ops")
        if a.command == "import" and a.batch > 100_000:
            p.error("import: --batch must be <= 100000")
        a.batch = getattr(a, "batch", None)
    try:
        if a.command == "prepare":
            cmd_prepare(a)
        elif a.command == "clone":
            cmd_clone(a)
        elif a.command == "verify":
            cmd_verify(a)
        else:
            measured_command(a)
    except Exception as e:
        emit("error", command=a.command, error=f"{type(e).__name__}: {e}")
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
