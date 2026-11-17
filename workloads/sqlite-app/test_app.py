"""Tests for app.py. Run: python3 -m unittest discover -s workloads/sqlite-app -v

Temporary files go to tempfile's directory (set TMPDIR to keep them off /tmp).
"""
import contextlib
import importlib.util
import io
import json
import os
import random
import re
import sqlite3
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path

import app

HERE = Path(__file__).resolve().parent
LAB_WINDOWS = HERE.parent.parent / "tools" / "lab_windows.py"


def run_cli(*argv):
    """Run app.main and return its JSON events."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        app.main([str(x) for x in argv])
    return [json.loads(line) for line in out.getvalue().splitlines()]


def result_of(events):
    return next(e for e in events if e["event"] == "result")


class Dataset(unittest.TestCase):
    """One tiny dataset per test class, plus fresh copies for tests that write."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="sqlite-app-test-")
        cls.tmp = Path(cls._tmp.name)
        cls.data = cls.tmp / "data"
        run_cli("prepare", "--dir", cls.data, "--preset", "tiny", "--seed", 42)
        cls.base = cls.data / "base.db"

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def clone(self, name):
        dst = self.tmp / f"{self.id().rsplit('.', 1)[-1]}-{name}.db"
        run_cli("clone", "--src", self.base, "--dst", dst)
        return dst

    def shop(self, path):
        con, _ = app.connect(path)
        self.addCleanup(con.close)
        return app.Shop(con), con


class TestPrepare(Dataset):
    def test_same_seed_same_dataset(self):
        other = self.tmp / "again"
        run_cli("prepare", "--dir", other, "--preset", "tiny", "--seed", 42)
        for name in ("products.csv", "receipts.csv"):
            self.assertEqual((self.data / name).read_bytes(), (other / name).read_bytes(), name)
        m1 = json.loads((self.data / "manifest.json").read_text())
        m2 = json.loads((other / "manifest.json").read_text())
        self.assertEqual(m1["checksum"], m2["checksum"])
        self.assertEqual(m1["counts"], m2["counts"])

    def test_other_seed_other_dataset(self):
        other = self.tmp / "seed7"
        run_cli("prepare", "--dir", other, "--preset", "tiny", "--seed", 7)
        m1 = json.loads((self.data / "manifest.json").read_text())
        m2 = json.loads((other / "manifest.json").read_text())
        self.assertNotEqual(m1["checksum"], m2["checksum"])

    def test_counts_and_history(self):
        m = json.loads((self.data / "manifest.json").read_text())
        cfg = app.PRESETS["tiny"]
        self.assertEqual(m["counts"]["products"], cfg["products"])
        self.assertEqual(m["counts"]["orders"], cfg["orders"])
        self.assertGreater(m["counts"]["order_items"], cfg["orders"])
        # Every product has an initial movement plus one sale movement per line.
        self.assertEqual(m["counts"]["stock_movements"], cfg["products"] + m["counts"]["order_items"])
        con, settings = app.connect(self.base, query_only=True)
        self.addCleanup(con.close)
        self.assertEqual(settings["journal_mode"], "wal")
        self.assertEqual(settings["synchronous"], 2)
        self.assertEqual(settings["foreign_keys"], 1)
        lo, hi = con.execute("SELECT min(created_at), max(created_at) FROM orders").fetchone()
        self.assertGreaterEqual(lo, app.HISTORY_START)
        self.assertLess(hi, app.HISTORY_START + app.HISTORY_DAYS * app.DAY)

    def test_refuses_non_empty_dir(self):
        with self.assertRaises(FileExistsError):
            run_cli("prepare", "--dir", self.data, "--preset", "tiny")
        # The existing dataset is untouched.
        self.assertTrue((self.data / "base.db").exists())
        app.verify_db(self.base)

    def test_clone_refuses_overwrite(self):
        dst = self.clone("x")
        with self.assertRaises(FileExistsError):
            run_cli("clone", "--src", self.base, "--dst", dst)

    def test_measured_commands_need_existing_db(self):
        with self.assertRaises(FileNotFoundError):
            run_cli("search", "--db", self.tmp / "missing.db", "--ops", 1)
        self.assertFalse((self.tmp / "missing.db").exists())


class TestQueries(Dataset):
    def plan(self, sql, params):
        con, _ = app.connect(self.base, query_only=True)
        self.addCleanup(con.close)
        return " | ".join(r[3] for r in con.execute("EXPLAIN QUERY PLAN " + sql, params))

    def test_search_uses_indexes(self):
        self.assertIn("INTEGER PRIMARY KEY",
                      self.plan("SELECT * FROM products WHERE id = ?", (1,)))
        self.assertIn("sqlite_autoindex_products_1",
                      self.plan("SELECT * FROM products WHERE sku = ?", ("SKU-0000001",)))
        p = self.plan("SELECT id FROM products WHERE category = ? AND price_cents BETWEEN ? AND ? "
                      "ORDER BY price_cents, id LIMIT 20", ("tea", 100, 300))
        self.assertIn("products_category_price", p)
        self.assertIn("products_name", self.plan(
            "SELECT id FROM products WHERE name >= ? AND name < ? ORDER BY name LIMIT 20", ("Amber Cup", "Amber Cuq")))

    def test_reports_use_date_index(self):
        for kind in ("category_revenue", "top_products", "daily_revenue"):
            self.assertIn("orders_created_at", self.plan(app.REPORT_SQL[kind], (0, 1)), kind)

    def raw(self):
        con, _ = app.connect(self.base, query_only=True)
        self.addCleanup(con.close)
        cat = dict(con.execute("SELECT id, category FROM products"))
        orders = dict(con.execute("SELECT id, created_at FROM orders"))
        items = con.execute("SELECT order_id, product_id, quantity, unit_price_cents FROM order_items").fetchall()
        return con, cat, orders, items

    def test_category_revenue_totals(self):
        con, cat, orders, items = self.raw()
        start = app.HISTORY_START + 40 * app.DAY
        end = start + 30 * app.DAY
        revenue, units, order_ids = defaultdict(int), defaultdict(int), defaultdict(set)
        for oid, pid, qty, unit in items:
            if start <= orders[oid] < end:
                revenue[cat[pid]] += qty * unit
                units[cat[pid]] += qty
                order_ids[cat[pid]].add(oid)
        rows = con.execute(app.REPORT_SQL["category_revenue"], (start, end)).fetchall()
        self.assertGreater(len(rows), 10)
        self.assertEqual({r[0]: (r[1], r[2], r[3]) for r in rows},
                         {c: (len(order_ids[c]), units[c], revenue[c]) for c in revenue})
        self.assertEqual([r[3] for r in rows], sorted((r[3] for r in rows), reverse=True))
        self.assertTrue(all(isinstance(r[3], int) for r in rows))

    def test_top_products_and_daily_revenue(self):
        con, cat, orders, items = self.raw()
        start = app.HISTORY_START + 100 * app.DAY
        end = start + 14 * app.DAY
        units = defaultdict(int)
        for oid, pid, qty, _ in items:
            if start <= orders[oid] < end:
                units[pid] += qty
        expected = sorted(units.items(), key=lambda kv: (-kv[1], kv[0]))[:20]
        rows = con.execute(app.REPORT_SQL["top_products"], (start, end)).fetchall()
        self.assertEqual([(r[0], r[2]) for r in rows], expected)

        totals = dict(con.execute("SELECT id, total_cents FROM orders"))
        day_rev = defaultdict(int)
        for oid, t in orders.items():
            if start <= t < start + 90 * app.DAY:
                day_rev[t // app.DAY] += totals[oid]
        rows = con.execute(app.REPORT_SQL["daily_revenue"], (start, start + 90 * app.DAY)).fetchall()
        self.assertEqual({r[0]: r[2] for r in rows}, dict(day_rev))
        self.assertEqual(len(rows), 90)

    def test_search_returns_real_rows(self):
        con, _ = app.connect(self.base, query_only=True)
        self.addCleanup(con.close)
        shop = app.Shop(con)
        rng = random.Random(3)
        got = defaultdict(int)
        for _ in range(400):
            kind, n = shop.search(rng)
            got[kind] += n
        self.assertEqual(set(got), {"by_id", "by_sku", "by_category", "name_prefix"})
        self.assertTrue(all(n > 0 for n in got.values()), got)

    def test_read_phases_cannot_write(self):
        con, settings = app.connect(self.base, query_only=True)
        self.addCleanup(con.close)
        self.assertEqual(settings["query_only"], 1)
        with self.assertRaises(sqlite3.OperationalError):
            con.execute("UPDATE products SET stock = stock + 1")


class TestUpdates(Dataset):
    def snapshot(self, con):
        return {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                for t in ("orders", "order_items", "stock_movements")}

    def stock(self, con, pid):
        return con.execute("SELECT stock FROM products WHERE id = ?", (pid,)).fetchone()[0]

    def test_order_is_atomic_and_consistent(self):
        db = self.clone("ok")
        shop, con = self.shop(db)
        before = self.snapshot(con)
        s1, s2 = self.stock(con, 5), self.stock(con, 6)
        p1, p2 = (con.execute("SELECT price_cents FROM products WHERE id = ?", (i,)).fetchone()[0] for i in (5, 6))
        oid = shop.place_order(9, [(5, 2), (6, 1)])
        self.assertEqual(self.stock(con, 5), s1 - 2)
        self.assertEqual(self.stock(con, 6), s2 - 1)
        self.assertEqual(con.execute("SELECT total_cents, item_count FROM orders WHERE id = ?", (oid,)).fetchone(),
                         (2 * p1 + p2, 2))
        self.assertEqual(con.execute("SELECT product_id, delta, reason FROM stock_movements WHERE order_id = ? "
                                     "ORDER BY product_id", (oid,)).fetchall(),
                         [(5, -2, "sale"), (6, -1, "sale")])
        after = self.snapshot(con)
        self.assertEqual(after, {"orders": before["orders"] + 1, "order_items": before["order_items"] + 2,
                                 "stock_movements": before["stock_movements"] + 2})
        con.close()
        app.verify_db(db)

    def assert_unchanged(self, con, before, stocks):
        self.assertFalse(con.in_transaction)
        self.assertEqual(self.snapshot(con), before)
        for pid, s in stocks.items():
            self.assertEqual(self.stock(con, pid), s)

    def test_out_of_stock_rolls_back_whole_order(self):
        db = self.clone("oos")
        shop, con = self.shop(db)
        con.execute("UPDATE products SET stock = 1 WHERE id = 8")  # test setup, outside the app path
        before, stocks = self.snapshot(con), {7: self.stock(con, 7), 8: 1}
        with self.assertRaises(app.OutOfStock):
            shop.place_order(3, [(7, 1), (8, 2)])  # line 1 was already applied when line 2 fails
        self.assert_unchanged(con, before, stocks)

    def test_failure_after_partial_writes_rolls_back(self):
        db = self.clone("trigger")
        shop, con = self.shop(db)
        # Fail on the second sale movement, after the order row, both stock
        # updates and the first line were written in this transaction.
        con.execute("""CREATE TEMP TRIGGER fail_second AFTER INSERT ON stock_movements
                       WHEN NEW.product_id = 11 BEGIN SELECT RAISE(ABORT, 'injected'); END""")
        before, stocks = self.snapshot(con), {10: self.stock(con, 10), 11: self.stock(con, 11)}
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected"):
            shop.place_order(3, [(10, 1), (11, 1)])
        self.assert_unchanged(con, before, stocks)

    def test_unknown_product_and_bad_input_roll_back(self):
        db = self.clone("bad")
        shop, con = self.shop(db)
        before, stocks = self.snapshot(con), {12: self.stock(con, 12)}
        with self.assertRaises(LookupError):
            shop.place_order(3, [(12, 1), (10**9, 1)])
        with self.assertRaises(ValueError):
            shop.place_order(3, [(12, 0)])
        with self.assertRaises(sqlite3.IntegrityError):
            shop.place_order(0, [(12, 1)])  # customer_id CHECK
        with self.assertRaises(LookupError):
            shop.restock([(12, 5), (10**9, 5)])
        self.assert_unchanged(con, before, stocks)

    def test_constraints_backstop(self):
        db = self.clone("constraints")
        _, con = self.shop(db)
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute("UPDATE products SET stock = -1 WHERE id = 1")
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute("UPDATE products SET price_cents = 1.5 WHERE id = 1")  # STRICT: integer cents only
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute("INSERT INTO order_items VALUES (999999, 1, 1, 1, 100)")  # FK: no such order

    def test_verify_detects_drift(self):
        db = self.clone("drift")
        con, _ = app.connect(db)
        con.execute("UPDATE products SET stock = stock + 1 WHERE id = 1")
        con.close()
        with self.assertRaisesRegex(RuntimeError, "stock != sum of movements"):
            app.verify_db(db)

    def test_update_ops_are_deterministic(self):
        sums = []
        for name, seed in (("a", 7), ("b", 7), ("c", 8)):
            db = self.clone(name)
            res = result_of(run_cli("update", "--db", db, "--seed", seed, "--ops", 300))
            self.assertEqual(res["ops"], 300)
            self.assertEqual(res["errors"], 0)
            self.assertEqual(set(res["by_kind"]) - {"order_rejected"}, {"order", "restock"})
            sums.append(app.verify_db(db)["checksum"])
        self.assertEqual(sums[0], sums[1])
        self.assertNotEqual(sums[0], sums[2])

    def test_import_batches(self):
        db = self.clone("import")
        receipts = app.load_receipts_csv(self.data / "receipts.csv")
        con, _ = app.connect(db)
        stock_before = con.execute("SELECT sum(stock) FROM products").fetchone()[0]
        moves_before = con.execute("SELECT count(*) FROM stock_movements").fetchone()[0]
        con.close()
        res = result_of(run_cli("import", "--db", db, "--csv", self.data / "receipts.csv",
                                "--batch", 70, "--ops", 20))
        self.assertEqual((res["ops"], res["rows"], res["batch"]), (20, 1400, 70))
        rows = [receipts[i % len(receipts)] for i in range(1400)]  # wraps past the 1000-row CSV
        con, _ = app.connect(db)
        self.addCleanup(con.close)
        self.assertEqual(con.execute("SELECT sum(stock) FROM products").fetchone()[0],
                         stock_before + sum(q for _, q in rows))
        self.assertEqual(con.execute("SELECT count(*) FROM stock_movements").fetchone()[0], moves_before + 1400)
        app.verify_db(db)


class TestMetrics(Dataset):
    def test_result_event(self):
        events = run_cli("search", "--db", self.base, "--seed", 5, "--ops", 500, "--label", "search rate=max")
        self.assertEqual([e["event"] for e in events], ["config", "measure_start", "result", "sizes"])
        cfg = events[0]
        self.assertEqual(cfg["runtime"]["sqlite"], sqlite3.sqlite_version)
        self.assertEqual(cfg["settings"]["journal_mode"], "wal")
        self.assertGreater(cfg["sizes"]["base.db"], 0)
        res = result_of(events)
        self.assertEqual((res["ops"], res["errors"], res["stopped_by"], res["label"]), (500, 0, "ops", "search rate=max"))
        self.assertEqual(sum(k["ops"] for k in res["by_kind"].values()), 500)
        lat = res["latency_us"]
        self.assertEqual(lat["count"], 500)
        self.assertLessEqual(lat["min"], lat["p50"])
        self.assertLessEqual(lat["p50"], lat["p99"])
        self.assertLessEqual(lat["p99"], lat["max"])
        self.assertGreater(res["ops_per_s"], 0)
        self.assertTrue(res["measure_start_utc"].endswith("Z"))

    def test_same_seed_same_operation_mix(self):
        a = result_of(run_cli("report", "--db", self.base, "--seed", 9, "--ops", 40))
        b = result_of(run_cli("report", "--db", self.base, "--seed", 9, "--ops", 40))
        self.assertEqual({k: (v["ops"], v["rows"]) for k, v in a["by_kind"].items()},
                         {k: (v["ops"], v["rows"]) for k, v in b["by_kind"].items()})
        self.assertGreater(a["rows"], 0)

    def test_rate_cap_and_duration(self):
        res = result_of(run_cli("search", "--db", self.base, "--duration", 1.0, "--rate", 100))
        self.assertEqual(res["stopped_by"], "duration")
        self.assertGreaterEqual(res["elapsed_s"], 1.0)
        self.assertTrue(90 <= res["ops"] <= 101, res["ops"])
        self.assertLessEqual(res["ops_per_s"], 101)

    def test_max_ops_cap_ends_phase_early(self):
        db = self.clone("cap")
        res = result_of(run_cli("update", "--db", db, "--duration", 60, "--max-ops", 25))
        self.assertEqual((res["ops"], res["stopped_by"]), (25, "max_ops"))
        self.assertIn("warning", res)
        self.assertLess(res["elapsed_s"], 60)

    def test_progress_events(self):
        events = run_cli("search", "--db", self.base, "--duration", 2.2, "--rate", 200, "--progress-interval", 1)
        self.assertGreaterEqual(sum(e["event"] == "progress" for e in events), 2)

    def test_json_lines_do_not_look_like_phase_marks(self):
        if not LAB_WINDOWS.exists():
            self.skipTest("tools/lab_windows.py not available")
        spec = importlib.util.spec_from_file_location("lab_windows", LAB_WINDOWS)
        lw = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lw)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            app.main(["search", "--db", str(self.base), "--ops", "10"])
        log = self.tmp / "r1-sqlite-app.log"
        log.write_text("2026-01-01T00:00:00Z search rate=max start\n" + out.getvalue()
                       + "2026-01-01T00:01:00Z done\n")
        self.assertEqual([p[2] for p in lw.phases(str(log))], ["search-ratemax"])

    def test_error_event_and_nonzero_exit(self):
        db = self.clone("err")
        con, _ = app.connect(db)
        con.execute("DROP INDEX orders_created_at")
        con.execute("DROP TABLE order_items")  # break the schema: every order must fail
        con.close()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(sqlite3.OperationalError):
            app.main(["update", "--db", str(db), "--ops", "5"])
        events = [json.loads(l) for l in out.getvalue().splitlines()]
        self.assertEqual(events[-1]["event"], "error")
        self.assertFalse(any(e["event"] == "result" for e in events))


class TestArguments(unittest.TestCase):
    def test_one_product_dataset_completes_prepare_and_updates(self):
        # Previously generation could request 2–5 distinct products from one.
        with tempfile.TemporaryDirectory(prefix="sqlite-app-test-") as d:
            data = Path(d) / "data"
            run_cli("prepare", "--dir", data, "--preset", "tiny",
                    "--products", 1, "--orders", 10)
            result = result_of(run_cli("update", "--db", data / "base.db", "--ops", 30))
            self.assertEqual(result["ops"], 30)
            self.assertEqual(app.verify_db(data / "base.db")["counts"]["products"], 1)

    def rejects(self, *argv):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            app.main(list(argv))
        self.assertEqual(cm.exception.code, 2)

    def test_validation(self):
        self.rejects("search", "--db", "x.db")                       # neither --duration nor --ops
        self.rejects("search", "--db", "x.db", "--ops", "0")
        self.rejects("search", "--db", "x.db", "--ops", "5", "--rate", "-1")
        self.rejects("search", "--db", "x.db", "--duration", "nan")
        self.rejects("report", "--db", "x.db", "--duration", "0")
        self.rejects("update", "--db", "x.db", "--ops", "5", "--max-ops", "0")
        self.rejects("import", "--db", "x.db", "--ops", "1")         # --csv required
        self.rejects("import", "--db", "x.db", "--csv", "r.csv", "--ops", "1", "--batch", "0")
        self.rejects("import", "--db", "x.db", "--csv", "r.csv", "--ops", "1", "--batch", "100001")
        self.rejects("prepare", "--dir", "d", "--preset", "huge")
        self.rejects("prepare", "--dir", "d", "--seed", "-1")
        self.rejects("bogus")

    def test_bad_receipts_csv(self):
        with tempfile.TemporaryDirectory(prefix="sqlite-app-test-") as d:
            p = os.path.join(d, "r.csv")
            for content in ("product_id,quantity\n1,0\n", "id,qty\n1,1\n", "product_id,quantity\n"):
                with open(p, "w") as f:
                    f.write(content)
                with self.assertRaises(ValueError):
                    app.load_receipts_csv(p)


class TestHistogram(unittest.TestCase):
    def test_quantiles_and_bounded_buckets(self):
        h = app.Histogram()
        for ns in range(1_000, 1_001_000, 10):  # 100k samples, 1 us .. 1 ms
            h.add(ns)
        self.assertLess(len(h.buckets), 200)
        s = h.summary_us()
        self.assertEqual(s["count"], 100_000)
        self.assertAlmostEqual(s["p50"], 500, delta=500 * 0.05)
        self.assertAlmostEqual(s["p99"], 990, delta=990 * 0.05)
        self.assertEqual(s["max"], 1001.0)  # exact, not bucketed: last sample is 1_000_990 ns


if __name__ == "__main__":
    unittest.main()
