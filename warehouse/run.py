# -*- coding: utf-8 -*-
"""Run all three case 2.4 warehouse models.

Outputs:
  output/slotting.csv                  batch-to-slot allocation
  output/picking.csv                   order-to-batch retrieval decisions
  output/equipment.csv                 stacker-lift-AGV-sorter-worker schedule
  output/docks.csv                     vehicle ETA, appointment and dock schedule
  output/managed_*.csv/json             case 2.4.4(2) managed warehouse model
  output/integration_*.csv/json         case 2.4.4(3) integration model
  output/appointment_portal.html        lightweight appointment prototype
  output/summary.json                   all sizing and comparison KPIs
"""
# 本文件是 2.4 三个选题的统一入口。
# 它的职责是组织运行顺序、把中间结果写成 CSV/JSON，并打印关键指标；
# 具体算法分别在 model.py、managed.py 和 integration.py 中实现。
#
# 输出文件对应关系：
# slotting.csv：批次分配到哪些货位。
# picking.csv：每张订单从哪些批次和货位取货。
# equipment.csv：堆垛机、提升机、AGV、分拣线和人员的作业时间。
# docks.csv：车辆到达、预约、月台、等待和迟到结果。
# managed_*.csv/json：2.4.4(2) 多商家代管仓结果。
# integration_*.csv/json：2.4.4(3) 仓配一体化结果。
# summary.json：供报告和检查脚本使用的汇总指标。
import csv
import json
import os

import integration
import managed
from data import (
    ASSUMPTIONS, CASE_FACTS, required_slots, layout_slots, pilot_area_m2,
    data_quality_checks, hhmm,
)
import model

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "output")
os.makedirs(OUT, exist_ok=True)


def strategy_result(name, slots, batches, orders):
    """按指定策略跑一遍 2.4.4(1) 的完整仓内链路。

    name 可以是 experience（经验式）或 data_driven（数据驱动）。
    函数按“分配货位 -> 生成拣选 -> 计算设备任务 -> 设备调度 -> 月台调度”
    的顺序执行，返回的报告都来自同一组中间结果，便于公平比较。
    """
    allocation = model.assign_slots(batches, slots, name)
    picking_lines = model.process_orders(orders, batches, allocation, name)
    picking = model.picking_metrics(picking_lines, orders)
    tasks = model.build_equipment_tasks(batches, picking_lines, allocation)
    equipment = model.schedule_equipment(tasks, name)
    docks = model.schedule_docks(orders, equipment["detail"], name)
    return {
        "strategy": name,
        "allocation": allocation,
        "picking_lines": picking_lines,
        "picking": picking,
        "tasks": tasks,
        "equipment": equipment,
        "docks": docks,
    }


def simulation_time(minutes):
    """把“从仿真起点算起的分钟数”转成 D+日 时:分，方便跨天阅读。"""
    day = int(minutes // 1440)
    return "D+%d %s" % (day, hhmm(minutes))


def write_csv(path, fieldnames, rows):
    """统一用 UTF-8-SIG 写 CSV，避免 Excel 打开中文时乱码。"""
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_slotting(results, batches):
    """导出每种策略的货位分配明细，用于检查容量、唯一性和分区效果。"""
    rows = []
    for result in results:
        for batch in batches:
            for slot in result["allocation"].get(batch.batch_id, []):
                rows.append({
                    "strategy": result["strategy"],
                    "batch_id": batch.batch_id,
                    "product": batch.product,
                    "slot_id": slot.slot_id,
                    "zone": slot.zone,
                    "aisle": slot.aisle,
                    "rack": slot.rack,
                    "bay": slot.bay,
                    "level": slot.level,
                    "depth": slot.depth,
                    "x_m": slot.x_m,
                    "y_m": slot.y_m,
                    "z_m": slot.z_m,
                    "travel_to_pd_m": slot.travel_to_pd_m,
                    "travel_to_dock_m": slot.travel_to_dock_m,
                    "stacker_seconds": round(slot.stacker_seconds, 2),
                    "lift_seconds": round(slot.lift_seconds, 2),
                    "agv_seconds_dock": round(slot.agv_seconds_dock, 2),
                })
    write_csv(
        os.path.join(OUT, "slotting.csv"),
        ["strategy", "batch_id", "product", "slot_id", "zone", "aisle",
         "rack", "bay", "level", "depth", "x_m", "y_m", "z_m",
         "travel_to_pd_m", "travel_to_dock_m", "stacker_seconds",
         "lift_seconds", "agv_seconds_dock"],
        rows,
    )


def write_picking(results):
    """导出订单拣选明细，记录订单、批次、货位和 FIFO 是否违规。"""
    rows = []
    for result in results:
        for line in result["picking_lines"]:
            rows.append({
                "strategy": result["strategy"],
                "order_id": line.order_id,
                "vehicle_id": line.vehicle_id,
                "order_day": line.order_day,
                "product": line.product,
                "batch_id": line.batch_id,
                "quantity": line.quantity,
                "slot_ids": " ".join(line.slot_ids),
                "retrieval_seconds": round(line.retrieval_seconds, 2),
                "release_min": line.release_min,
                "due_min": line.due_min,
                "fifo_violation": int(line.fifo_violation),
            })
    write_csv(
        os.path.join(OUT, "picking.csv"),
        ["strategy", "order_id", "vehicle_id", "order_day", "product",
         "batch_id", "quantity", "slot_ids", "retrieval_seconds",
         "release_min", "due_min", "fifo_violation"],
        rows,
    )


def write_equipment(results):
    """导出设备链明细；每一行是一个托盘的完整作业链。"""
    rows = []
    for result in results:
        for row in result["equipment"]["detail"]:
            rows.append({
                "strategy": result["strategy"],
                "task_id": row["task_id"],
                "task_type": row["task_type"],
                "product": row["product"],
                "batch_id": row["batch_id"],
                "order_id": row["order_id"],
                "slot_id": row["slot_id"],
                "order_day": row["order_day"],
                "release_min": round(row["release_min"], 2),
                "due_min": round(row["due_min"], 2),
                "path_distance_m": row["path_distance_m"],
                "travel_to_pd_m": row["travel_to_pd_m"],
                "travel_to_dock_m": row["travel_to_dock_m"],
                "stacker_id": row["stacker_id"],
                "stacker_start": round(row["stacker_start"], 2),
                "stacker_end": round(row["stacker_end"], 2),
                "lift_id": row["lift_id"],
                "lift_start": round(row["lift_start"], 2),
                "lift_end": round(row["lift_end"], 2),
                "agv_id": row["agv_id"],
                "agv_start": round(row["agv_start"], 2),
                "agv_end": round(row["agv_end"], 2),
                "sorter_id": row["sorter_id"],
                "sorter_start": round(row["sorter_start"], 2),
                "sorter_end": round(row["sorter_end"], 2),
                "worker_team": row["worker_team"],
                "worker_start": round(row["worker_start"], 2),
                "worker_end": round(row["worker_end"], 2),
                "worker_minutes": round(row["worker_minutes"], 2),
                "cold_break_count": row["cold_break_count"],
            })
    write_csv(
        os.path.join(OUT, "equipment.csv"),
        ["strategy", "task_id", "task_type", "product", "batch_id",
         "order_id", "slot_id", "order_day", "release_min", "due_min",
         "path_distance_m", "travel_to_pd_m", "travel_to_dock_m",
         "stacker_id", "stacker_start", "stacker_end", "lift_id",
         "lift_start", "lift_end", "agv_id", "agv_start", "agv_end",
         "sorter_id", "sorter_start", "sorter_end", "worker_team",
         "worker_start", "worker_end", "worker_minutes", "cold_break_count"],
        rows,
    )


def write_docks(results):
    """导出车辆与月台明细，同时保留预测、预约、到达和实际开始时间。"""
    rows = []
    for result in results:
        for row in result["docks"]["detail"]:
            rows.append({
                "strategy": result["strategy"],
                "vehicle_id": row["vehicle_id"],
                "order_id": row["order_id"],
                "order_day": row["order_day"],
                "quantity": row["quantity"],
                "release_min": round(row["release_min"], 2),
                "due_min": round(row["due_min"], 2),
                "ready_min": round(row["ready_min"], 2),
                "requested_eta_min": (
                    "" if row["eta_min"] is None
                    else round(row["requested_eta_min"], 2)
                ),
                "eta_min": (
                    "" if row["eta_min"] is None
                    else round(row["eta_min"], 2)
                ),
                "appointment_min": (
                    "" if row["appointment_min"] is None
                    else round(row["appointment_min"], 2)
                ),
                "actual_arrival_min": round(row["arrival_min"], 2),
                "arrival_deviation_min": (
                    "" if row["arrival_deviation_min"] is None
                    else round(row["arrival_deviation_min"], 2)
                ),
                "prepare_by_min": (
                    "" if row["prepare_by_min"] is None
                    else round(row["prepare_by_min"], 2)
                ),
                "dock_id": row["dock_id"],
                "start_min": round(row["start_min"], 2),
                "end_min": round(row["end_min"], 2),
                "wait_min": round(row["wait_min"], 2),
                "cargo_wait_min": round(row["cargo_wait_min"], 2),
                "appointment_compliant": (
                    "" if row["appointment_compliant"] is None
                    else int(row["appointment_compliant"])
                ),
                "late": int(row["late"]),
            })
    write_csv(
        os.path.join(OUT, "docks.csv"),
        ["strategy", "vehicle_id", "order_id", "order_day", "quantity",
         "release_min", "due_min", "ready_min", "requested_eta_min",
         "eta_min", "appointment_min", "actual_arrival_min",
         "arrival_deviation_min", "prepare_by_min", "dock_id", "start_min",
         "end_min", "wait_min", "cargo_wait_min",
         "appointment_compliant", "late"],
        rows,
    )


def main():
    """运行 2.4.4(1)、2.4.4(2)、2.4.4(3) 并生成统一汇总。"""
    slots = model.build_slots()
    batches = model.generate_batches()
    orders = model.generate_orders()
    results = [
        strategy_result("experience", slots, batches, orders),
        strategy_result("data_driven", slots, batches, orders),
    ]

    # 先完成 2.4.4(1) 的两种策略对比，相关 CSV 是后续选题的输入基础。
    write_slotting(results, batches)
    write_picking(results)
    write_equipment(results)
    write_docks(results)

    # 两个子模型各自运行并返回汇总；主汇总文件再把它们嵌套进去。
    managed_summary = managed.main()
    integration_summary = integration.main()

    summary = {
        "case_facts": CASE_FACTS,
        "assumptions": ASSUMPTIONS,
        "sizing": {
            "required_slots": required_slots(),
            "layout_slots": layout_slots(),
            "pilot_area_m2": pilot_area_m2(),
            "existing_stereoscopic_slots": CASE_FACTS["stereoscopic_slots"],
        },
        "data_quality_checks": [
            {"name": name, "passed": bool(passed)}
            for name, passed in data_quality_checks()
        ],
        "strategies": {
            result["strategy"]: {
                "picking": result["picking"],
                "equipment": {
                    key: value for key, value in result["equipment"].items()
                    if key not in ("detail", "breaks")
                },
                "docks": {
                    key: value for key, value in result["docks"].items()
                    if key != "detail"
                },
            }
            for result in results
        },
        "managed_warehouse": managed_summary,
        "warehouse_delivery_integration": integration_summary,
        "ai_assistance_notice": (
            "代码、图表和文档由 AI 辅助生成；案例事实与计划假设已分栏标注。"
        ),
    }
    with open(
            os.path.join(OUT, "summary.json"),
            "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)

    print("=" * 76)
    print("银犁冷链 2.4 智能仓储三个选题统一运行")
    print("=" * 76)
    print("历史需求: %d 件/天 (生姜 %d + 大蒜 %d)" % (
        required_slots()["daily_pieces"],
        CASE_FACTS["historical_ginger_daily_pieces"],
        CASE_FACTS["historical_garlic_daily_pieces"],
    ))
    print("货位需求: %d 个 -> 设计 %d 个，试点面积约 %d m2" % (
        required_slots()["required_slots"], layout_slots(), pilot_area_m2()))
    for result in results:
        picking = result["picking"]
        equipment = result["equipment"]
        docks = result["docks"]
        print("-" * 76)
        print("[%s]" % result["strategy"])
        print("  FIFO合规 %5.1f%%  平均拣选 %5.2f 秒  库存平均库龄 %.2f 天" % (
            picking["fifo_compliance_pct"],
            picking["average_retrieval_seconds"],
            picking["average_inventory_age_days"]))
        print("  堆垛机 %5.1f%%  提升机 %5.1f%%  AGV %5.1f%%  分拣线 %5.1f%%" % (
            equipment["stacker_utilisation_pct"],
            equipment["lift_utilisation_pct"],
            equipment["agv_utilisation_pct"],
            equipment["sorter_utilisation_pct"]))
        print("  设备任务完工 %s  强制休息 %d 次  单次连续作业上限 %.1f 分钟" % (
            simulation_time(equipment["equipment_end_min"]),
            equipment["cold_breaks"],
            equipment["max_continuous_work_min"],
        ))
        print("  月台平均等待 %6.2f 分钟  最大等待 %6.2f 分钟  迟到 %d 台" % (
            docks["average_wait_min"],
            docks["max_wait_min"],
            docks["late_vehicles"]))
    print("-" * 76)
    print("2.4.4(2) 翻箱率 随机 %.2f%% -> 优化 %.2f%%" % (
        managed_summary["strategies"]["random"]["flip_rate_pct"],
        managed_summary["strategies"]["optimized"]["flip_rate_pct"],
    ))
    print("2.4.4(3) 月台等待 人工 %.2f -> 协同 %.2f 分钟，ETA 平均误差 %.2f 分钟" % (
        integration_summary["modes"]["manual"]["average_wait_min"],
        integration_summary["modes"]["integrated"]["average_wait_min"],
        integration_summary["modes"]["integrated"]["average_eta_abs_error_min"],
    ))
    print("输出目录:", OUT)
    return summary


if __name__ == "__main__":
    main()
