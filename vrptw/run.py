# -*- coding: utf-8 -*-
"""Run the full pipeline for both product families and write outputs."""
import csv
import json
import os

import data
from core import Problem, VEHICLES
from fleet import solve_fleet, baseline_fleet, evaluate, plan_metrics

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "output")
os.makedirs(OUT, exist_ok=True)

NAMES = {"jiang": "姜蒜", "noodle": "鲜面条"}


def fmt_time(m):
    return "%02d:%02d" % (int(m) // 60, int(m) % 60)


def make_problems():
    return {
        "jiangsuan": Problem("jiangsuan", data.JIANGSUAN, "jiang"),
        "miantiao": Problem("miantiao", data.MIANTIAO, "noodle"),
    }


def rows_for(sol, prob_map, tag):
    rows = []
    for key, vt, route in sol:
        p = prob_map[key]
        rc = p.route_cost(route, vt)
        rows.append({
            "plan": tag,
            "product": NAMES[p.kind],
            "vehicle": VEHICLES[vt]["name"],
            "stops": len(route),
            "customers": " ".join(str(p.ids[c]) for c in route),
            "load_kg": round(rc["load_kg"], 1),
            "util_w_pct": round(100.0 * rc["load_kg"] / VEHICLES[vt]["cap_w"], 1),
            "util_v_pct": round(100.0 * rc["load_m3"] / VEHICLES[vt]["cap_v"], 1),
            "distance_km": round(rc["distance_km"], 2),
            "depart": fmt_time(rc["depart"]),
            "last_service_end": fmt_time(rc["starts"][-1] + p.service[route[-1]])
            if rc["starts"] else "-",
            "fixed": round(rc["fixed"], 1),
            "transport": round(rc["transport"], 1),
            "refrig": round(rc["refrig"], 1),
            "penalty": round(rc["penalty"], 1),
            "loss": round(rc["loss"], 1),
            "total": round(rc["total"], 1),
        })
    return rows


def product_stats(sol, prob_map, kind):
    cost = dist = stops = veh = 0
    for key, vt, route in sol:
        p = prob_map[key]
        if p.kind != kind:
            continue
        rc = p.route_cost(route, vt)
        cost += rc["total"]
        dist += rc["distance_km"]
        stops += len(route)
        veh += 1
    return cost, dist, veh, stops


def main():
    problems = make_problems()
    base = baseline_fleet(problems)
    opt = solve_fleet(problems)

    print("=" * 70)
    print("银犁冷链城配 VRPTW - 人工排程 vs 智能调度 (车队: 8 小 + 2 中)")
    print("=" * 70)
    for kind in ("jiang", "noodle"):
        demand = sum(sum(p.weight) for p in problems.values() if p.kind == kind)
        bc, bd, bv, bs = product_stats(base, problems, kind)
        oc, od, ov, os_ = product_stats(opt, problems, kind)
        print("[%s]  需求合计 %.0f kg (%d 点)" % (NAMES[kind], demand, bs))
        print("   baseline : %d 车, %.1f km, %.0f 元" % (bv, bd, bc))
        print("   optimised: %d 车, %.1f km, %.0f 元" % (ov, od, oc))
        print("   降本 %.0f 元 (%.1f%%), 里程 -%.1f km"
              % (bc - oc, 100 * (bc - oc) / bc, bd - od))
    eb, eo = evaluate(base), evaluate(opt)
    mb, mo = plan_metrics(base), plan_metrics(opt)
    print("-" * 70)
    print("baseline  : 成本 %.0f 元, 小型 %d 台, 中型 %d 台" % (eb[0], eb[1], eb[2]))
    print("optimised : 成本 %.0f 元, 小型 %d 台, 中型 %d 台" % (eo[0], eo[1], eo[2]))
    print("合计降本 %.0f 元 (%.1f%%)" % (eb[0] - eo[0], 100 * (eb[0] - eo[0]) / eb[0]))
    print("平均装载率  : 人工 %.1f%% -> 智能 %.1f%% (体积口径 %.1f%%)"
          % (mb["load_util_weight_pct"], mo["load_util_weight_pct"],
             mo["load_util_volume_pct"]))
    print("准时送达率  : 人工 %.1f%% -> 智能 %.1f%% (硬时间窗达成 %.1f%%)"
          % (mb["on_time_pref_pct"], mo["on_time_pref_pct"], mo["on_time_acc_pct"]))
    for kind in ("jiang", "noodle"):
        fb, fo = mb["by_family"][kind], mo["by_family"][kind]
        print("  [%s] 装载率 %.1f%% -> %.1f%%, 准时率 %.1f%% -> %.1f%%"
              % (NAMES[kind], fb["load_util_pct"], fo["load_util_pct"],
                 fb["on_time_pref_pct"], fo["on_time_pref_pct"]))

    all_rows = rows_for(base, problems, "baseline") + rows_for(opt, problems, "optimised")
    with open(os.path.join(OUT, "routes.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(all_rows[0].keys()))
        w.writeheader()
        w.writerows(all_rows)

    summary = {
        "baseline": {"cost": round(eb[0], 1), "small": eb[1], "medium": eb[2]},
        "optimised": {"cost": round(eo[0], 1), "small": eo[1], "medium": eo[2]},
        "saving": round(eb[0] - eo[0], 1),
        "saving_pct": round(100 * (eb[0] - eo[0]) / eb[0], 1),
        "kpi": {"baseline": mb, "optimised": mo},
    }
    with open(os.path.join(OUT, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return problems, base, opt


if __name__ == "__main__":
    main()
