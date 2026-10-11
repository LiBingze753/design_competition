# -*- coding: utf-8 -*-
"""Acceptance tests for the Yinli cold-chain VRPTW model.

Run:
    python -m unittest -v test_model.py
"""
import unittest

import data
from core import Problem, VEHICLES
from fleet import baseline_fleet, evaluate, solve_fleet, plan_metrics
from validate import validate


def build_problems():
    return {
        "jiangsuan": Problem("jiangsuan", data.JIANGSUAN, "jiang"),
        "miantiao": Problem("miantiao", data.MIANTIAO, "noodle"),
    }


class DataQualityTests(unittest.TestCase):
    def test_customer_counts(self):
        self.assertEqual(len(data.JIANGSUAN), 30)
        self.assertEqual(len(data.MIANTIAO), 30)

    def test_total_demand(self):
        problems = build_problems()
        self.assertAlmostEqual(sum(problems["jiangsuan"].weight), 8831.0, places=6)
        self.assertAlmostEqual(sum(problems["miantiao"].weight), 2655.0, places=6)

    def test_time_window_order(self):
        problems = build_problems()
        for prob in problems.values():
            for i in range(prob.n):
                self.assertLessEqual(prob.acc_open[i], prob.pref_open[i])
                self.assertLessEqual(prob.pref_open[i], prob.pref_close[i])
                self.assertLessEqual(prob.pref_close[i], prob.acc_close[i])

    def test_coordinates_are_in_sichuan_service_area(self):
        problems = build_problems()
        for prob in problems.values():
            for lon, lat in zip(prob.lon, prob.lat):
                self.assertGreater(lon, 102.0)
                self.assertLess(lon, 106.0)
                self.assertGreater(lat, 29.0)
                self.assertLess(lat, 32.0)

    def test_vehicle_limits(self):
        self.assertEqual(VEHICLES["small"]["count"], 8)
        self.assertEqual(VEHICLES["medium"]["count"], 2)


class OptimisationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.problems = build_problems()
        cls.baseline = baseline_fleet(cls.problems)
        cls.optimised = solve_fleet(cls.problems)

    def test_optimised_solution_is_feasible(self):
        errors, fleet = validate(self.optimised, self.problems)
        self.assertEqual(errors, [])
        self.assertLessEqual(fleet[0], VEHICLES["small"]["count"])
        self.assertLessEqual(fleet[1], VEHICLES["medium"]["count"])

    def test_baseline_exceeds_medium_vehicle_cap(self):
        _errors, fleet = validate(self.baseline, self.problems)
        self.assertGreater(fleet[1], VEHICLES["medium"]["count"])

    def test_optimised_cost_is_lower(self):
        base_cost = evaluate(self.baseline)[0]
        opt_cost = evaluate(self.optimised)[0]
        self.assertLess(opt_cost, base_cost)

    def test_all_customers_are_served_once(self):
        for key, prob in self.problems.items():
            served = []
            for route_key, _vt, route in self.optimised:
                if route_key == key:
                    served.extend(route)
            self.assertEqual(sorted(served), list(range(prob.n)))

    def test_operational_kpis_are_in_range(self):
        metrics = plan_metrics(self.optimised)
        for key in ("load_util_weight_pct", "load_util_volume_pct",
                    "on_time_pref_pct", "on_time_acc_pct"):
            self.assertGreaterEqual(metrics[key], 0.0)
            self.assertLessEqual(metrics[key], 100.0 + 1e-6)
        # the acceptable window is a hard constraint, so it is always met
        self.assertAlmostEqual(metrics["on_time_acc_pct"], 100.0, places=6)

    def test_optimised_raises_load_utilisation(self):
        base = plan_metrics(self.baseline)["load_util_weight_pct"]
        opt = plan_metrics(self.optimised)["load_util_weight_pct"]
        self.assertGreater(opt, base)

    def test_kpis_reported_per_product_family(self):
        by_family = plan_metrics(self.optimised)["by_family"]
        self.assertEqual(set(by_family), {"jiang", "noodle"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
