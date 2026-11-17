"""Run with: python3 -m unittest discover -s python -v."""
import os
import unittest

import labdata

REGISTRY = [
    {"snapshot": "lab-1117-perf", "node": "sut-01", "job": "*", "tuning": "default", "perf": "yes", "role": "train", "note": ""},
    {"snapshot": "lab-1117-perf", "node": "sut-01", "job": "mixed", "tuning": "default", "perf": "yes", "role": "locked", "note": ""},
    {"snapshot": "stress-1008", "node": "sut-02", "job": "*", "tuning": "clean-room", "perf": "no", "role": "course", "note": ""},
]


class LabdataTests(unittest.TestCase):
    def test_selection_syntax(self):
        self.assertEqual(labdata.parse_select("role=train,test perf=yes"),
                         [("role", {"train", "test"}), ("perf", {"yes"})])
        with self.assertRaises(ValueError):
            labdata.parse_select("colour=red")
        with self.assertRaises(ValueError):
            labdata.parse_select("role")

    def test_a_job_row_overrides_the_star_row(self):
        units = labdata.units("snapshot=lab-1117-perf", REGISTRY)
        roles = {u["job"]: u["role"] for u in units}
        self.assertEqual(roles, {"cpu-bound": "train", "memory-bound": "train", "io-bound": "train", "mixed": "locked"})
        self.assertNotIn("mixed", {u["job"] for u in labdata.units("role=train", REGISTRY)})

    def test_a_round_row_overrides_the_job_row(self):
        reg = REGISTRY + [{"snapshot": "lab-1116-sut02", "node": "sut-02", "job": "*", "tuning": "default", "perf": "no", "role": "train", "note": ""},
                          {"snapshot": "lab-1116-sut02", "node": "sut-02", "job": "cpu-bound/r2", "tuning": "default", "perf": "no", "role": "test", "note": ""}]
        roles = {(u["job"], u["round"]): u["role"] for u in labdata.units("snapshot=lab-1116-sut02", reg)}
        self.assertEqual(roles[("cpu-bound", "2")], "test")
        self.assertEqual(roles[("cpu-bound", "1")], "train")
        self.assertEqual(roles[("memory-bound", "2")], "train")

    def test_snapshot_without_runs_log_is_one_job_over_its_span(self):
        (u,) = labdata.units("snapshot=stress-1008", REGISTRY)
        self.assertEqual((u["job"], u["round"]), ("*", "1"))
        self.assertEqual((u["start"], u["end"]), labdata.span("stress-1008"))

    def test_grid_starts_on_the_truth_grid(self):
        origin = labdata.span("stress-1008")[0]
        steps = labdata.grid("stress-1008", origin + 3, origin + 40, 10)
        self.assertEqual(list(steps), [origin + 10, origin + 20, origin + 30, origin + 40])

    def test_the_registry_in_the_repo_is_valid(self):
        rows = labdata.load_registry()
        self.assertTrue(rows)
        for r in rows:
            self.assertTrue(os.path.isfile(os.path.join(labdata.SNAPSHOTS, r["snapshot"], "truth.csv")), r["snapshot"])
            self.assertIn(r["perf"], {"yes", "no"})


if __name__ == "__main__":
    unittest.main()
