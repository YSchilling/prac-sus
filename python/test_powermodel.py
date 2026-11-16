"""Run with: python3 -m unittest discover -s python -v."""
import json
import math
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import powermodel as pm


def samples(**values):
    return {json.dumps({"cpu": "0", "mode": mode}): values.get(mode, 0.0)
            for mode in pm.CPU_MODES | {"idle", "iowait", "steal"}}


class CPUModelTests(unittest.TestCase):
    def test_wait_and_steal_do_not_count_as_execution(self):
        prev = samples()
        now = samples(user=2, nice=1, system=1, irq=0.5, softirq=0.5,
                      idle=1, iowait=2, steal=2)
        self.assertEqual(pm.features(now, prev, 10),
                         {"user_ratio": 0.3, "kernel_ratio": 0.2, "ratio": 0.5,
                          "awake": 1 - math.exp(-0.5 / pm.AWAKE_SCALE)})
        self.assertEqual(pm.features(samples(iowait=5, steal=5), prev, 10)["ratio"], 0)

    def test_incomplete_reset_and_nonfinite_windows_are_rejected(self):
        prev = samples(user=5)
        valid = samples(user=6)
        incomplete = dict(valid)
        del incomplete[next(k for k in incomplete if json.loads(k)["mode"] == "user")]
        for now, before in [(valid, {}), (incomplete, prev),
                            (samples(user=1), prev), (samples(user=float("nan")), prev)]:
            with self.subTest(now=now):
                self.assertIsNone(pm.features(now, before, 10))

    def test_scrape_jitter_keeps_total_and_components_consistent(self):
        x = pm.features(samples(user=9, system=3), samples(), 10)
        self.assertEqual(x, {"user_ratio": 0.75, "kernel_ratio": 0.25, "ratio": 1.0,
                             "awake": 1 - math.exp(-1 / pm.AWAKE_SCALE)})

    def test_awake_steps_up_early_then_saturates(self):
        awake = lambda busy: pm.features(samples(user=busy * 10), samples(), 10)["awake"]
        self.assertEqual(awake(0), 0)
        self.assertAlmostEqual(awake(pm.AWAKE_SCALE), 1 - math.exp(-1))
        self.assertGreater(awake(0.2), 0.98)
        self.assertLess(awake(1) - awake(0.5), 1e-4)

    def test_every_zone_feature_is_computed(self):
        x = pm.features(samples(user=1), samples(), 10)
        for zone, names in pm.ZONE_FEATURES.items():
            with self.subTest(zone=zone):
                self.assertLessEqual(set(names), set(x))

    def test_vm_fetch_and_prediction_need_no_frequency(self):
        cpu = {0: {k: v for k, v in samples().items() if json.loads(k)["mode"] in pm.CPU_MODES},
               10: {k: v for k, v in samples(user=2, system=1).items()
                    if json.loads(k)["mode"] in pm.CPU_MODES}}
        pod = json.dumps({"namespace": "test", "pod": "app", "container": "app"})
        pods = {0: {pod: 0}, 10: {pod: 1}}
        a = SimpleNamespace(prometheus="http://unused", node="vm", step=10)
        with patch.object(pm, "query_range", side_effect=[cpu, pods]) as query:
            fetched = pm.fetch(a, 0, 10)
        self.assertEqual(query.call_count, 2)
        self.assertIn('mode=~"user|nice|system|irq|softirq"', query.call_args_list[0].args[1])
        coef = {"package": {"intercept": 10, "weights": {"user_ratio": 20, "kernel_ratio": 30}},
                "dram": {"intercept": 2, "weights": {"ratio": 5}}}
        rows = pm.at(coef, *fetched, 10, 10)
        self.assertEqual(rows["package"][0], 17)
        self.assertAlmostEqual(rows["package"][1], 5.1)
        self.assertAlmostEqual(rows["package"][2]["test/app"], 1.7)
        self.assertEqual(rows["dram"][0], 3.5)
        self.assertIsNone(pm.at(coef, *fetched, 0, 10))

    def test_coefficients_without_awake_still_predict(self):
        # coef.json from before the awake feature: weights name only old features.
        x = pm.features(samples(user=5), samples(), 10)
        old = {"dram": {"intercept": 4, "weights": {"ratio": 4}}}
        new = {"dram": {"intercept": 1, "weights": {"ratio": 0, "awake": 5}}}
        self.assertEqual(pm.predict(old, x, {}, 0)["dram"][0], 6)
        self.assertAlmostEqual(pm.predict(new, x, {}, 0)["dram"][0], 1 + 5 * x["awake"])

    def test_coef_file_comes_from_flag_or_env(self):
        seen = []
        with patch.object(pm, "offline", side_effect=lambda a: seen.append(a.coef)), \
                patch("sys.argv", ["powermodel.py", "--mode", "offline", "--node", "n", "--coef", "x.json"]):
            pm.main()
        with patch.object(pm, "offline", side_effect=lambda a: seen.append(a.coef)), \
                patch.dict("os.environ", {"POWERMODEL_COEF": "y.json"}), \
                patch("sys.argv", ["powermodel.py", "--mode", "offline", "--node", "n"]):
            pm.main()
        self.assertEqual(seen, ["x.json", "y.json"])


if __name__ == "__main__":
    unittest.main()
