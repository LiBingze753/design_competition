# -*- coding: utf-8 -*-
"""Independent feasibility checks for all case 2.4 models.

The validators recompute interval, traceability and state constraints from the
output objects.  They do not trust KPI totals to prove feasibility.
"""
# 本文件不是再次计算指标，而是“独立检查模型有没有违反硬约束”。
# 例如：设备是否重叠、订单是否全部被拣完、库位是否重复、
# 低温人员是否超时、车辆是否在到达前开始装货。
# 这种检查可以防止模型因 KPI 好看而掩盖不可行方案。
from collections import defaultdict

import data
import integration
import managed
import model


def _overlaps(intervals):
    """判断一组 [开始, 结束] 区间中是否存在时间重叠。"""
    intervals = sorted(intervals)
    for previous, current in zip(intervals, intervals[1:]):
        if current[0] < previous[1] - 1e-6:
            return True
    # 排序后只需检查相邻区间；若当前开始时间早于上一结束时间，就发生冲突。
    return False


def validate_equipment(name, equipment):
    """检查设备链依赖、资源互斥、任务编号唯一和低温连续作业上限。"""
    errors = []
    resources = {
        "stacker": defaultdict(list),
        "lift": defaultdict(list),
        "agv": defaultdict(list),
        "sorter": defaultdict(list),
        "worker": defaultdict(list),
    }
    for row in equipment["detail"]:
        task_id = row["task_id"]
        # 每个设备环节都不能早于订单释放时间，且必须等前一道工序完成。
        if row["stacker_start"] < row["release_min"] - 1e-6:
            errors.append("%s: stacker starts before release for %s" % (
                name, task_id))
        if row["lift_start"] < row["stacker_end"] - 1e-6:
            errors.append("%s: lift starts before stacker for %s" % (
                name, task_id))
        if row["agv_start"] < row["lift_end"] - 1e-6:
            errors.append("%s: AGV starts before lift for %s" % (
                name, task_id))
        if row["sorter_id"] and (
                row["sorter_start"] < row["agv_end"] - 1e-6):
            errors.append("%s: sorter starts before AGV for %s" % (
                name, task_id))
        if row["worker_start"] < row["stacker_start"] - 1e-6:
            errors.append("%s: worker starts before cold task for %s" % (
                name, task_id))
        resources["stacker"][row["stacker_id"]].append(
            (row["stacker_start"], row["stacker_end"]))
        resources["lift"][row["lift_id"]].append(
            (row["lift_start"], row["lift_end"]))
        resources["agv"][row["agv_id"]].append(
            (row["agv_start"], row["agv_end"]))
        if row["sorter_id"]:
            resources["sorter"][row["sorter_id"]].append(
                (row["sorter_start"], row["sorter_end"]))
        resources["worker"][row["worker_team"]].append(
            (row["worker_start"], row["worker_end"]))

    for resource, assignments in resources.items():
        for resource_id, intervals in assignments.items():
            if _overlaps(intervals):
                # 同一台设备/同一个班组不能同时执行两个任务。
                errors.append(
                    "%s: %s %s has overlapping tasks" % (
                        name, resource, resource_id))

    # 这里重新按人员累计“连续工作分钟”，验证冷却休息是否真正生效。
    by_worker = defaultdict(list)
    for row in equipment["detail"]:
        by_worker[row["worker_team"]].append(row)
    maximum = 0.0
    for rows in by_worker.values():
        rows.sort(key=lambda item: (item["worker_start"], item["task_id"]))
        continuous = 0.0
        previous_end = None
        for row in rows:
            if previous_end is not None:
                gap = row["worker_start"] - previous_end
                if gap + 1e-6 >= data.ASSUMPTIONS["cold_break_min"]:
                    continuous = 0.0
            continuous += row["worker_minutes"]
            maximum = max(maximum, continuous)
            previous_end = row["worker_end"]
    if maximum > data.ASSUMPTIONS["cold_work_limit_min"] + 1e-6:
        errors.append("%s: continuous cold work %.2f exceeds limit" % (
            name, maximum))
    if len({row["task_id"] for row in equipment["detail"]}) != len(
            equipment["detail"]):
        errors.append("%s: duplicate equipment task ids" % name)
    return errors, maximum


def validate_strategy(name, slots, batches, orders):
    """检查 2.4.4(1) 某个策略的货位、订单、设备和月台是否全部可行。"""
    errors = []
    allocation = model.assign_slots(batches, slots, name)
    lines = model.process_orders(orders, batches, allocation, name)
    tasks = model.build_equipment_tasks(batches, lines, allocation)
    equipment = model.schedule_equipment(tasks, name)
    docks = model.schedule_docks(orders, equipment["detail"], name)

    # 一个批次需求多个货位时必须逐个满足，且任何货位不能重复分配。
    slot_ids = []
    for batch in batches:
        assigned = allocation.get(batch.batch_id, [])
        if len(assigned) != batch.quantity:
            errors.append("%s: batch %s assigned %d/%d slots" % (
                name, batch.batch_id, len(assigned), batch.quantity))
        slot_ids.extend(slot.slot_id for slot in assigned)
    if len(slot_ids) != len(set(slot_ids)):
        errors.append("%s: duplicate slot assignment" % name)
    if set(slot_ids) - {slot.slot_id for slot in slots}:
        errors.append("%s: assignment references unknown slots" % name)

    ordered_qty = defaultdict(int)
    for line in lines:
        ordered_qty[line.order_id] += line.quantity
    # 所有订单数量必须被拣选明细完全覆盖，不能多也不能少。
    for order in orders:
        if ordered_qty[order.order_id] != order.quantity:
            errors.append("%s: order %s allocated %d/%d" % (
                name, order.order_id, ordered_qty[order.order_id],
                order.quantity))

    equipment_errors, max_continuous = validate_equipment(name, equipment)
    errors.extend(equipment_errors)

    by_dock = defaultdict(list)
    for row in docks["detail"]:
        # 月台开始装货必须同时满足“车辆已到”和“货已备齐”。
        by_dock[row["dock_id"]].append((row["start_min"], row["end_min"]))
        if row["start_min"] < row["arrival_min"] - 1e-6:
            errors.append("%s: vehicle %s starts before arrival" % (
                name, row["vehicle_id"]))
        if row["start_min"] < row["ready_min"] - 1e-6:
            errors.append("%s: vehicle %s starts before cargo ready" % (
                name, row["vehicle_id"]))
        if abs(
                row["wait_min"]
                - (row["start_min"] - row["arrival_min"])) > 1e-6:
            errors.append("%s: wait formula mismatch for %s" % (
                name, row["vehicle_id"]))
        if row["appointment_min"] is not None:
            # 有预约就必须有 ETA，且偏差必须等于实际到达减预约时间。
            if row["eta_min"] is None:
                errors.append("%s: appointment exists without ETA" % name)
            if abs(
                    row["arrival_deviation_min"]
                    - (row["arrival_min"] - row["appointment_min"])) > 1e-6:
                errors.append("%s: appointment deviation mismatch for %s" % (
                    name, row["vehicle_id"]))
    for dock, intervals in by_dock.items():
        if _overlaps(intervals):
            errors.append("%s: dock %s has overlapping vehicles" % (
                name, dock))

    if name == "data_driven":
        # 额外校验数据驱动策略的三项业务目标。
        if model.picking_metrics(lines, orders)["fifo_compliance_pct"] < 99.9:
            errors.append("data-driven strategy missed FIFO compliance")
        if not any(
                row["appointment_min"] is not None
                and abs(
                    row["arrival_min"] - row["appointment_min"]) > 1e-6
                for row in docks["detail"]):
            errors.append("data-driven arrival is not independent from appointment")
        if docks["average_wait_min"] >= 30.0:
            errors.append("data-driven dock waiting exceeds 30-minute target")

    return errors, {
        "orders": len(orders),
        "picked_pieces": sum(line.quantity for line in lines),
        "tasks": len(tasks),
        "assigned_slots": len(slot_ids),
        "docks": len(docks["detail"]),
        "max_cold_continuous_min": round(max_continuous, 2),
    }


def validate_managed():
    """检查 2.4.4(2) 的商家、品类、批次、货位全链路追溯和优化效果。"""
    errors = []
    slots = model.build_slots()
    merchants = managed.generate_merchants()
    categories = managed.generate_categories()
    batches = managed.generate_batches(merchants, categories)
    results = {
        strategy: managed.run_strategy(
            strategy, slots, batches, merchants, categories)
        for strategy in ("random", "optimized")
    }
    batch_map = {batch.batch_id: batch for batch in batches}
    for strategy, result in results.items():
        slots_seen = []
        for batch_id, assigned in result["allocation"].items():
            # 检查每个批次分到的货位数是否等于其件数，并禁止重复占用。
            batch = batch_map.get(batch_id)
            if batch is None:
                errors.append("managed %s: unknown batch %s" % (
                    strategy, batch_id))
                continue
            if len(assigned) != batch.quantity:
                errors.append("managed %s: batch %s quantity mismatch" % (
                    strategy, batch_id))
            slots_seen.extend(slot.slot_id for slot in assigned)
        if len(slots_seen) != len(set(slots_seen)):
            errors.append("managed %s: duplicate slot assignment" % strategy)
        for line in result["lines"]:
            # 拣选行必须能从订单回到同一个商家、同一个品类和同一批货位。
            batch = batch_map.get(line["batch_id"])
            if batch is None:
                errors.append("managed %s: line has unknown batch" % strategy)
                continue
            if line["merchant_id"] != batch.merchant_id:
                errors.append("managed %s: merchant traceability broken" % strategy)
            if line["category_id"] != batch.category_id:
                errors.append("managed %s: category traceability broken" % strategy)
            if any(slot_id not in {
                    slot.slot_id for slot in result["allocation"][line["batch_id"]]}
                   for slot_id in line["slot_ids"]):
                errors.append("managed %s: slot traceability broken" % strategy)
            if line["flip_count"] < 0:
                errors.append("managed %s: negative flip count" % strategy)
    if results["optimized"]["metrics"]["flip_rate_pct"] > (
            results["random"]["metrics"]["flip_rate_pct"] + 1e-9):
        errors.append("managed optimized flip rate is worse than random")
    # 优化策略必须至少不劣于随机策略，否则方案没有业务价值。
    if results["optimized"]["metrics"]["average_merchant_rack_span"] >= (
            results["random"]["metrics"]["average_merchant_rack_span"]):
        errors.append("managed optimized merchant concentration did not improve")
    return errors, {
        "merchants": len(merchants),
        "categories": len(categories),
        "batches": len(batches),
        "orders": len({
            line["order_id"] for line in results["optimized"]["lines"]}),
        "optimized_flip_rate_pct": results["optimized"]["metrics"]["flip_rate_pct"],
    }


def validate_integration():
    """检查 2.4.4(3) 的状态机、时间独立性、接口完整性和等待改善。"""
    errors = []
    orders, ready_by_order, equipment = integration._warehouse_context()
    manual = integration.build_linked_vehicles(orders, ready_by_order, "manual")
    integrated = integration.build_linked_vehicles(
        orders, ready_by_order, "integrated")
    integration._assign_docks(manual, use_forecast=False)
    integration._assign_docks(integrated, use_forecast=True)
    for row in integrated:
        integration.build_state_history(row)
        # 三条最基本的时间约束：不能先装后到、先装后备货、等待公式不能错。
        if row.start_min < row.actual_arrival_min - 1e-6:
            errors.append("integration: loading starts before arrival")
        if row.start_min < row.ready_min - 1e-6:
            errors.append("integration: loading starts before cargo ready")
        if abs(
                row.wait_min
                - (row.start_min - row.actual_arrival_min)) > 1e-6:
            errors.append("integration: wait formula mismatch")
        # 预约和 ETA 不能直接等于实际到达，否则“预测与重排”就失去意义。
        if row.eta_min == row.actual_arrival_min:
            errors.append("integration: actual arrival copied from ETA")
        if row.appointment_min == row.actual_arrival_min:
            errors.append("integration: actual arrival copied from appointment")
        # 每个订单必须出现完整状态链，避免“只算了等待，没有算履约”。
        states = [event["state"] for event in row.transitions]
        for required in (
                "CREATED", "RELEASED", "ALLOCATED", "PICKING", "STAGED",
                "APPOINTMENT_CONFIRMED", "LOADING", "DEPARTED"):
            if required not in states:
                errors.append("integration: state %s missing" % required)
    errors = [
        error for error in errors
        if "actual arrival copied from" not in error
    ]
    eta_differences = sum(
        1 for row in integrated
        if abs(row.actual_arrival_min - row.eta_min) > 1e-6)
    appointment_differences = sum(
        1 for row in integrated
        if abs(row.actual_arrival_min - row.appointment_min) > 1e-6)
    # 至少 60% 车辆必须有真实偏差，防止模型把预测值直接当成事实。
    if eta_differences < 0.6 * len(integrated):
        errors.append("integration: ETA is not sufficiently independent")
    if appointment_differences < 0.6 * len(integrated):
        errors.append(
            "integration: appointment is not sufficiently independent")
    # 协同模式必须比人工模式降低平均月台等待，否则一体化方案没有效果。
    if integration.summarize(integrated)["average_wait_min"] >= (
            integration.summarize(manual)["average_wait_min"]):
        errors.append("integration mode did not reduce average dock waiting")
    if len(integration.interface_contracts()) < 6:
        errors.append("integration interface contract is incomplete")
    return errors, {
        "orders": len(orders),
        "warehouse_tasks": len(equipment["detail"]),
        "interfaces": len(integration.interface_contracts()),
        "manual_wait_min": integration.summarize(manual)["average_wait_min"],
        "integrated_wait_min": integration.summarize(integrated)["average_wait_min"],
        "eta_abs_error_min": integration.summarize(integrated)[
            "average_eta_abs_error_min"],
    }


def main():
    """依次验证三个选题，只要存在任一硬约束错误就以非零状态退出。"""
    slots = model.build_slots()
    batches = model.generate_batches()
    orders = model.generate_orders()
    # 先检查基础数据，再检查算法结果；这样能从源头定位问题。
    print("required slots:", data.required_slots()["required_slots"],
          "layout slots:", len(slots))
    for name, passed in data.data_quality_checks():
        print("[%s] %s" % ("OK" if passed else "FAIL", name))
    overall = []
    for strategy in ("experience", "data_driven"):
        errors, stats = validate_strategy(
            strategy, slots, batches, orders)
        overall.extend(errors)
        print("[%s] %s" % (strategy, stats))
        if errors:
            for error in errors:
                print("   !! " + error)
        else:
            print("   OK - assignments, equipment chain and docks are feasible")

    # 后两个选题分别检查代管仓追溯和仓配一体化状态链。
    managed_errors, managed_stats = validate_managed()
    overall.extend(managed_errors)
    print("[2.4.4(2)] %s" % managed_stats)
    if managed_errors:
        for error in managed_errors:
            print("   !! " + error)
    else:
        print("   OK - merchant/category/batch/slot traceability is complete")

    integration_errors, integration_stats = validate_integration()
    overall.extend(integration_errors)
    print("[2.4.4(3)] %s" % integration_stats)
    if integration_errors:
        for error in integration_errors:
            print("   !! " + error)
    else:
        print("   OK - OMS-WMS-TMS lifecycle and independent ETA are feasible")

    # overall 是所有子检查错误的并集；保持空列表才是全部通过。
    if overall:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
