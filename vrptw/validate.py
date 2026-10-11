# -*- coding: utf-8 -*-
"""Independent feasibility validator for the produced plan.

Checks, for whatever solution is passed in:
  1. every customer of each product is served exactly once
  2. per-route weight and volume capacity
  3. hard acceptable time windows
  4. fleet size (<=8 small, <=2 medium)
Run:  python validate.py
"""
import data
from core import Problem, VEHICLES
from fleet import solve_fleet, baseline_fleet, evaluate, plan_metrics


def build():
    return {
        "jiangsuan": Problem("jiangsuan", data.JIANGSUAN, "jiang"),
        "miantiao": Problem("miantiao", data.MIANTIAO, "noodle"),
    }


def validate(sol, problems):
    errors = []
    small = medium = 0
    served = {k: [] for k in problems}

    for key, vt, route in sol:
        p = problems[key]
        rc = p.route_cost(route, vt)
        if rc is None:
            errors.append("route %s/%s infeasible (time window)" % (key, route))
            continue
        served[key].extend(route)
        if vt == "small":
            small += 1
        else:
            medium += 1
        if rc["load_kg"] > VEHICLES[vt]["cap_w"] + 1e-6:
            errors.append("overweight on %s route %s" % (vt, route))
        if rc["load_m3"] > VEHICLES[vt]["cap_v"] + 1e-6:
            errors.append("over-volume on %s route %s" % (vt, route))
        # route_cost() already returns None when any hard window is violated

    for key, p in problems.items():
        got = sorted(served[key])
        want = list(range(p.n))
        if got != want:
            errors.append("%s serves %d/%d customers; dupes=%s missing=%s"
                          % (key, len(got), p.n,
                             [x for x in set(got) if got.count(x) > 1],
                             sorted(set(want) - set(got))))
    if small > VEHICLES["small"]["count"]:
        errors.append("uses %d small vehicles (cap %d)" % (small, VEHICLES["small"]["count"]))
    if medium > VEHICLES["medium"]["count"]:
        errors.append("uses %d medium vehicles (cap %d)" % (medium, VEHICLES["medium"]["count"]))
    return errors, (small, medium)


def main():
    problems = build()
    for label, sol in (("baseline", baseline_fleet(problems)),
                       ("optimised", solve_fleet(problems))):
        errs, (s, m) = validate(sol, problems)
        cost = evaluate(sol)[0]
        print("[%s] cost=%.0f  small=%d medium=%d  routes=%d"
              % (label, cost, s, m, len(sol)))
        kpi = plan_metrics(sol)
        print("   KPI: 平均装载率 %.1f%% (体积 %.1f%%), 准时送达率 %.1f%% (硬时间窗达成 %.1f%%)"
              % (kpi["load_util_weight_pct"], kpi["load_util_volume_pct"],
                 kpi["on_time_pref_pct"], kpi["on_time_acc_pct"]))
        if errs:
            for e in errs:
                print("   !! " + e)
        else:
            print("   OK - all customers served once, capacities & windows satisfied, fleet respected")


if __name__ == "__main__":
    main()
