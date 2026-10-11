# -*- coding: utf-8 -*-
"""Discrete-event planning model for the case 2.4 intelligent warehouse pilot.

The model deliberately keeps the case facts and planning assumptions separate:
  * slots, batches, orders and tasks are explicit dataclasses;
  * both an experience-based strategy and a data-driven strategy are simulated;
  * the equipment chain is stacker -> lift -> AGV -> sorter;
  * tasks are expanded for the complete three-day outbound horizon;
  * vehicle ETA, appointment and actual arrival are separate quantities;
  * cold-room work is interrupted by mandatory rest periods.

This is a planning-level queueing model.  It does not claim to resolve
simultaneous path conflicts or equipment failures like a vendor-grade digital
twin.
"""
# 本文件是 2.4.4(1) 的主模型。
# 它把“案例事实”和“计划假设”分开，明确模拟两条设备链：
# 堆垛机 -> 提升机 -> AGV -> 分拣线，并单独处理低温人员的强制休息。
# 经验式策略作为对照，数据驱动策略用于证明布局和调度优化效果。
# 这是规划级排队模型，不声称能解决真实设备避障、故障恢复等细节。
from collections import defaultdict
from dataclasses import dataclass, field
import math
import random

from data import (
    ASSUMPTIONS, DAILY_PATTERN, HORIZON_DAYS, INITIAL_INVENTORY_DAYS,
    LAYOUT, layout_slots,
)


# ------------------------------------------------------------------ objects
@dataclass
class Slot:
    """一个货位的三维坐标和到各作业点的基础时间。"""
    slot_id: str
    aisle: int
    rack: int
    bay: int
    level: int
    depth: int
    zone: str
    x_m: float
    y_m: float
    z_m: float
    travel_to_pd_m: float
    travel_to_dock_m: float
    stacker_seconds: float
    lift_seconds: float
    agv_seconds_pd: float
    agv_seconds_dock: float

    @property
    def travel_seconds(self):
        """兼容旧代码的拣选耗时字段，实际返回堆垛机作业时间。"""
        return self.stacker_seconds


@dataclass
class Batch:
    """一次入库形成的批次，同一批次可以占用多个货位。"""
    batch_id: str
    product: str
    inbound_min: int
    quantity: int
    planned_release_min: int
    remaining: int = 0
    slots: list = field(default_factory=list)

    def __post_init__(self):
        if self.remaining == 0:
            self.remaining = self.quantity


@dataclass
class Order:
    """一张出库订单，包含数量、释放时间、截止时间和关联车辆。"""
    order_id: str
    product: str
    quantity: int
    release_min: int
    due_min: int
    vehicle_id: str
    order_day: int


@dataclass
class PickLine:
    """订单和批次之间的一条拣选明细，是库存追溯的关键记录。"""
    order_id: str
    vehicle_id: str
    order_day: int
    product: str
    batch_id: str
    quantity: int
    slot_ids: list
    retrieval_seconds: float
    inbound_min: int
    release_min: int
    due_min: int
    fifo_violation: bool


@dataclass
class EquipmentTask:
    """一个托盘级设备任务，保存四个设备阶段的预计耗时。"""
    task_id: str
    task_type: str
    product: str
    batch_id: str
    order_id: str
    slot_id: str
    order_day: int
    release_min: int
    due_min: int
    stacker_seconds: float
    lift_seconds: float
    agv_seconds: float
    sorter_seconds: float
    worker_seconds: float
    travel_to_pd_m: float
    travel_to_dock_m: float
    path_distance_m: float
    requires_sorter: bool


# ------------------------------------------------------------------ layout
def slot_zone(bay, level):
    """根据货位离出入口的远近划分快取、常规和慢取分区。"""
    if bay <= 8 and level <= 5:
        return "fast"
    if bay <= 20 and level <= 8:
        return "medium"
    return "slow"


def build_slots():
    """按显式布局参数生成 2,400 个带坐标和作业时间的货位。"""
    # 用五层循环穷举所有货位；每个货位都保存坐标，供后面计算真实路径。
    slots = []
    for aisle in range(1, LAYOUT["aisles"] + 1):
        for rack in range(1, LAYOUT["racks_per_aisle"] + 1):
            for bay in range(1, LAYOUT["bays"] + 1):
                for level in range(1, LAYOUT["levels"] + 1):
                    for depth in range(1, LAYOUT["depths_per_position"] + 1):
                        x_m = (
                        # x 轴表示同一巷道内的列方向，深度越大离出入口越远。
                            (bay - 1) * LAYOUT["bay_width_m"]
                            + (depth - 1) * LAYOUT["position_depth_m"]
                        )
                        y_m = (
                        # y 轴区分不同巷道和货架面。
                            (aisle - 1)
                            * (LAYOUT["racks_per_aisle"] * LAYOUT["rack_gap_m"]
                               + LAYOUT["aisle_width_m"])
                            + (rack - 1) * LAYOUT["rack_gap_m"]
                        )
                        z_m = (level - 1) * LAYOUT["level_height_m"]
                        # z 轴表示高度，堆垛机和提升机的垂直耗时都由它产生。
                        horizontal = (
                            x_m + y_m + (depth - 1) * LAYOUT["position_depth_m"]
                        )
                        vertical = z_m
                        stacker_s = max(
                        # 水平与垂直同时运动，取较慢的一侧加上固定服务时间。
                            horizontal / ASSUMPTIONS["stacker_horizontal_mps"],
                            vertical / ASSUMPTIONS["stacker_vertical_mps"],
                        ) + ASSUMPTIONS["stacker_service_seconds"]
                        lift_s = (
                        # 提升机只计算垂直行程。
                            vertical / ASSUMPTIONS["lift_vertical_mps"]
                            + ASSUMPTIONS["lift_service_seconds"]
                        )
                        pd_distance = math.hypot(
                        # AGV 去 P&D 点和去月台是两条不同路径。
                            x_m - LAYOUT["p_and_d_x_m"],
                            y_m - LAYOUT["p_and_d_y_m"],
                        )
                        dock_distance = math.hypot(
                            LAYOUT["dock_x_m"] - x_m,
                            LAYOUT["dock_y_m"] - y_m,
                        )
                        agv_pd_s = (
                            ASSUMPTIONS["agv_transfer_seconds"]
                            + pd_distance / ASSUMPTIONS["agv_speed_mps"]
                        )
                        agv_dock_s = (
                            ASSUMPTIONS["agv_transfer_seconds"]
                            + dock_distance / ASSUMPTIONS["agv_speed_mps"]
                        )
                        slot_id = "A%02d-R%d-B%02d-L%02d-D%d" % (
                            aisle, rack, bay, level, depth)
                        slots.append(Slot(
                            slot_id=slot_id,
                            aisle=aisle,
                            rack=rack,
                            bay=bay,
                            level=level,
                            depth=depth,
                            zone=slot_zone(bay, level),
                            x_m=round(x_m, 4),
                            y_m=round(y_m, 4),
                            z_m=round(z_m, 4),
                            travel_to_pd_m=round(pd_distance, 3),
                            travel_to_dock_m=round(dock_distance, 3),
                            stacker_seconds=round(stacker_s, 3),
                            lift_seconds=round(lift_s, 3),
                            agv_seconds_pd=round(agv_pd_s, 3),
                            agv_seconds_dock=round(agv_dock_s, 3),
                        ))
    return slots


# ------------------------------------------------------------------ scenario
def _batch_start_min(day, index):
    """计算某一天第 index 个入库批次开始作业的分钟数。"""
    spacing = 18 if day == 0 else 55
    return day * 1440 + 6 * 60 + index * spacing


def generate_batches():
    """生成仿真开始前的初始库存，以及第 0 天新到货的入库批次。"""
    batches = []
    for day in range(-INITIAL_INVENTORY_DAYS, 1):
        day_label = "M%02d" % abs(day) if day < 0 else "P00"
        index = 0
        for product, pattern in DAILY_PATTERN.items():
            dwell_days = ASSUMPTIONS["average_%s_dwell_days" % product]
            for amount in pattern:
                inbound = _batch_start_min(day, index)
                batches.append(Batch(
                    batch_id="%s-%s-%02d" % (
                        product[:2].upper(), day_label, index),
                    product=product,
                    inbound_min=inbound,
                    quantity=amount,
                    planned_release_min=int(
                        inbound + dwell_days * 1440),
                ))
                index += 1
    return batches


def generate_orders():
    """生成连续三天的出库订单，每天总量为案例日均 190 件。"""
    orders = []
    for day in range(HORIZON_DAYS):
        index = 0
        for product, pattern in DAILY_PATTERN.items():
            for amount in pattern:
                release = day * 1440 + 6 * 60 + index * 12
                orders.append(Order(
                    order_id="O%02d-%02d" % (day, index),
                    product=product,
                    quantity=amount,
                    release_min=release,
                    due_min=release + 180,
                    vehicle_id="V%02d-%02d" % (day, index),
                    order_day=day,
                ))
                index += 1
    return orders


def _zone_rank(zone):
    """把分区名称转成可排序、可相减的数字等级。"""
    return {"fast": 0, "medium": 1, "slow": 2}[zone]


def _required_zone(batch):
    """按批次预计在库天数决定它应该优先进入哪个分区。"""
    dwell_days = (batch.planned_release_min - batch.inbound_min) / 1440.0
    if dwell_days <= 8.0:
        return "fast"
    if dwell_days <= 12.0:
        return "medium"
    return "slow"


def _batch_retrieval_seconds(batch, allocation):
    """计算一个批次所有已分货位的平均堆垛机拣选时间。"""
    slots = allocation.get(batch.batch_id, [])
    if not slots:
        return 9999.0
    return sum(slot.stacker_seconds for slot in slots) / len(slots)


def assign_slots(batches, slots, strategy, seed=42):
    """为每个批次件数分配一个货位。

    experience：随机占位，不优化距离或 FIFO。
    data_driven：按计划出库时间排序，优先选择分区匹配、距离较近的货位，并
    尽量把同一批货放在一起。
    """
    if sum(batch.quantity for batch in batches) > len(slots):
        raise ValueError("not enough slots for the pilot inventory")
    rng = random.Random(seed)
    available = list(slots)
    allocation = defaultdict(list)

    if strategy == "experience":
        # 经验策略按任务顺序轮流分设备，不比较各设备当前空闲时间。
        # 对照组：纯随机货位，不看距离、分区和批次邻近性。
        rng.shuffle(available)
        for batch in batches:
            for _ in range(batch.quantity):
                allocation[batch.batch_id].append(available.pop())
        return allocation

    if strategy != "data_driven":
        raise ValueError("unknown allocation strategy: %s" % strategy)

    ordered = sorted(batches, key=lambda batch: (
    # 优化组：先处理更早计划出库的批次，让快出库货物优先占好库位。
        batch.planned_release_min, batch.inbound_min, batch.batch_id))
    for batch in ordered:
        required_zone = _required_zone(batch)
        existing = allocation[batch.batch_id]
        for _ in range(batch.quantity):
            best = None
            best_score = None
            for index, slot in enumerate(available):
                zone_gap = abs(
                    _zone_rank(slot.zone) - _zone_rank(required_zone))
                score = (
                # 评分越低越好：行程短、分区匹配、深度浅。
                    slot.stacker_seconds
                    + 35.0 * zone_gap
                    + 8.0 * max(0, slot.depth - 1)
                )
                if existing:
                    # 同一批次尽量放在相邻列，减少后续找货和空驶。
                    nearest_bay = min(
                        abs(slot.bay - placed.bay) for placed in existing)
                    score += 0.0 if nearest_bay <= 1 else 12.0
                if best_score is None or score < best_score:
                    best_score = score
                    best = index
            allocation[batch.batch_id].append(available.pop(best))
    return allocation


# ------------------------------------------------------------------ picking
def process_orders(orders, batches, allocation, strategy):
    """按订单扣减库存，记录实际取了哪些批次、货位以及是否违反 FIFO。"""
    working = {batch.batch_id: batch.remaining for batch in batches}
    # working 模拟实时库存，每拣一次就扣减，避免订单重复占用同一批货。
    lines = []
    for order in orders:
        candidates = [
        # 车辆开始装车必须同时满足：已到达、货已备齐、月台已空闲。
        # 产品、库存、入库时间三个条件都满足，才是本订单的候选批次。
            batch for batch in batches
            if batch.product == order.product
            and working[batch.batch_id] > 0
            and batch.inbound_min <= order.release_min
        ]
        if not candidates:
            raise RuntimeError("no inventory for %s" % order.order_id)
        min_inbound = min(batch.inbound_min for batch in candidates)
        # 最早入库批次是 FIFO 判断基准；库存不足时才允许继续取后续批次。
        if strategy == "experience":
            # 人工模式没有预约，车辆按固定规则到达，等待由月台拥堵决定。
            candidates.sort(key=lambda batch: (
                _batch_retrieval_seconds(batch, allocation),
                batch.inbound_min,
            ))
        else:
            candidates.sort(key=lambda batch: (
                batch.inbound_min,
                _batch_retrieval_seconds(batch, allocation),
            ))
        need = order.quantity
        violation = False
        for batch in candidates:
            if need <= 0:
                break
            take = min(need, working[batch.batch_id])
            if take <= 0:
                continue
            if batch.inbound_min > min_inbound + 1e-9:
                violation = True
            slot_list = sorted(
            # 从该批次已有货位中选择综合行程最短的托盘。
                allocation[batch.batch_id],
                key=lambda slot: (
                    slot.stacker_seconds * 0.55
                    + slot.lift_seconds * 0.20
                    + slot.agv_seconds_dock * 0.25
                ),
            )[:take]
            retrieval = (
                sum(slot.stacker_seconds for slot in slot_list)
                / len(slot_list)
                if slot_list else 0.0
            )
            lines.append(PickLine(
                order_id=order.order_id,
                vehicle_id=order.vehicle_id,
                order_day=order.order_day,
                product=order.product,
                batch_id=batch.batch_id,
                quantity=take,
                slot_ids=[slot.slot_id for slot in slot_list],
                retrieval_seconds=retrieval,
                inbound_min=batch.inbound_min,
                release_min=order.release_min,
                due_min=order.due_min,
                fifo_violation=violation,
            ))
            working[batch.batch_id] -= take
            need -= take
        if need > 0:
            raise RuntimeError(
                "order %s remains short by %d" % (order.order_id, need))
    return lines


def picking_metrics(lines, orders):
    """汇总订单拣选 KPI：件数、平均时间、FIFO 合规率和平均库龄。"""
    total_pieces = sum(line.quantity for line in lines)
    fifo_orders = {}
    for line in lines:
        fifo_orders[line.order_id] = (
            fifo_orders.get(line.order_id, False) or line.fifo_violation)
    violations = sum(1 for value in fifo_orders.values() if value)
    weighted_age = 0.0
    for line in lines:
        weighted_age += line.quantity * max(
            0.0, (line.release_min - line.inbound_min) / 1440.0)
    return {
        "picked_pieces": total_pieces,
        "orders": len(orders),
        "average_retrieval_seconds": round(
            sum(line.retrieval_seconds * line.quantity for line in lines)
            / total_pieces, 2),
        "fifo_violation_orders": violations,
        "fifo_compliance_pct": round(
            100.0 * (len(orders) - violations) / len(orders), 1),
        "average_inventory_age_days": round(weighted_age / total_pieces, 2),
    }


# ------------------------------------------------------------------ equipment
def _slot_index(allocation):
    """建立“批次编号 + 货位编号 -> 货位对象”的查询索引。"""
    index = {}
    for batch_id, slots in allocation.items():
        for slot in slots:
            index[(batch_id, slot.slot_id)] = slot
    return index


def build_equipment_tasks(batches, lines, allocation):
    """把入库批次和出库拣选展开成托盘级设备任务。"""
    slot_map = _slot_index(allocation)
    tasks = []
    inbound = [
        batch for batch in batches
        if int(batch.inbound_min // 1440) == 0
    ]
    # 第 0 天到货的托盘需要真实走一遍入库设备链。
    for batch in inbound:
        assigned = allocation.get(batch.batch_id, [])
        for index in range(batch.quantity):
            slot = assigned[index]
            release = int(batch.inbound_min + (index // 4) * 2)
            tasks.append(EquipmentTask(
                task_id="IN-%s-%03d" % (batch.batch_id, index),
                task_type="inbound",
                product=batch.product,
                batch_id=batch.batch_id,
                order_id="",
                slot_id=slot.slot_id,
                order_day=0,
                release_min=release,
                due_min=10 * 60 + 600,
                stacker_seconds=slot.stacker_seconds,
                lift_seconds=slot.lift_seconds,
                agv_seconds=slot.agv_seconds_pd,
                sorter_seconds=0.0,
                worker_seconds=ASSUMPTIONS["worker_task_seconds"],
                travel_to_pd_m=slot.travel_to_pd_m,
                travel_to_dock_m=slot.travel_to_dock_m,
                path_distance_m=slot.travel_to_pd_m,
                requires_sorter=False,
            ))
    for line in lines:
        for index, slot_id in enumerate(line.slot_ids):
            slot = slot_map[(line.batch_id, slot_id)]
            release = int(
                line.release_min + (index // 4) * 2)
            tasks.append(EquipmentTask(
                task_id="OUT-%s-%s-%03d" % (
                    line.order_id, line.batch_id, index),
                task_type="outbound",
                product=line.product,
                batch_id=line.batch_id,
                order_id=line.order_id,
                slot_id=slot.slot_id,
                order_day=line.order_day,
                release_min=release,
                due_min=line.due_min,
                stacker_seconds=slot.stacker_seconds,
                lift_seconds=slot.lift_seconds,
                agv_seconds=slot.agv_seconds_dock,
                sorter_seconds=(
                    ASSUMPTIONS["sorter_seconds_per_piece"]
                    + max(0.0, slot.travel_to_dock_m - 80.0) / 80.0
                ),
                worker_seconds=ASSUMPTIONS["worker_task_seconds"],
                travel_to_pd_m=slot.travel_to_pd_m,
                travel_to_dock_m=slot.travel_to_dock_m,
                path_distance_m=slot.travel_to_dock_m,
                requires_sorter=True,
            ))
    return tasks


def _schedule_stage(
        tasks, machine_count, duration_getter, release_getter, strategy):
    """给一个设备阶段排程：任务按释放时间排队，同一设备不能重叠使用。"""
    # machine_ready[i] 表示设备 i 最早空闲时间，从根上防止同一设备任务重叠。
    machine_ready = [0.0] * machine_count
    schedule = {}
    ordered = sorted(tasks, key=lambda task: (
        release_getter(task), task.due_min, task.task_id))
    if strategy == "experience":
        for index, task in enumerate(ordered):
            machine = index % machine_count
            release = float(release_getter(task))
            duration = float(duration_getter(task)) / 60.0
            start = max(release, machine_ready[machine])
            end = start + duration
            machine_ready[machine] = end
            schedule[task.task_id] = (machine, start, end)
    elif strategy == "data_driven":
        # 数据驱动策略为每个任务比较所有设备，选择最早完成的设备。
        for task in ordered:
            release = float(release_getter(task))
            duration = float(duration_getter(task)) / 60.0
            best = None
            for machine in range(machine_count):
                start = max(release, machine_ready[machine])
                end = start + duration
                key = (end, machine)
                if best is None or key < best[0]:
                    best = (key, machine, start, end)
            _key, machine, start, end = best
            machine_ready[machine] = end
            schedule[task.task_id] = (machine, start, end)
    else:
        raise ValueError("unknown scheduling strategy: %s" % strategy)
    return schedule, machine_ready


def _schedule_workers(detail):
    """给低温作业分配人工班组，并在连续作业超限时插入强制休息。"""
    teams = ASSUMPTIONS["worker_teams"]
    # 每个班组分别记录最早空闲、连续作业累计和上次结束时间。
    limit = ASSUMPTIONS["cold_work_limit_min"]
    break_min = ASSUMPTIONS["cold_break_min"]
    ready = [0.0] * teams
    continuous = [0.0] * teams
    last_end = [0.0] * teams
    breaks = defaultdict(list)

    ordered = sorted(detail, key=lambda row: (
        row["stacker_start"], row["task_id"]))
    for row in ordered:
        duration = row["worker_seconds"] / 60.0
        best = None
        for team in range(teams):
            start = max(row["stacker_start"], ready[team])
            team_continuous = continuous[team]
            if start - last_end[team] + 1e-9 >= break_min:
                # 自然间隔已经足够长，连续作业计时归零。
                team_continuous = 0.0
            if team_continuous + duration > limit + 1e-9:
                # 下一任务会超过低温上限，先等待一个强制休息。
                start = max(start, last_end[team] + break_min)
                team_continuous = 0.0
            key = (start + duration, start, team)
            if best is None or key < best[0]:
                best = (key, team, start, team_continuous)
        _key, team, start, team_continuous = best
        if team_continuous == 0.0 and continuous[team] > 0.0:
            break_start = last_end[team]
            break_end = start
            if break_end > break_start + 1e-9:
                breaks[team].append((break_start, break_end))
        end = start + duration
        continuous[team] = team_continuous + duration
        ready[team] = end
        last_end[team] = end
        row["worker_team"] = team + 1
        row["worker_start"] = start
        row["worker_end"] = end
        row["worker_minutes"] = duration
        row["cold_break_count"] = len(breaks[team])
    return breaks


def schedule_equipment(tasks, strategy):
    """依次调度堆垛机、提升机、AGV、分拣线和低温人工资源。"""
    stackers, _stacker_ready = _schedule_stage(
    # 第一段：堆垛机从任务释放时间开始取货。
        tasks,
        ASSUMPTIONS["stacker_count"],
        lambda task: task.stacker_seconds,
        lambda task: task.release_min,
        strategy,
    )
    lift_release = {
    # 第二段：提升机必须等堆垛机结束并完成交接后才能开始。
        task.task_id: (
            stackers[task.task_id][2]
            + ASSUMPTIONS["lift_handoff_seconds"] / 60.0
        )
        for task in tasks
    }
    lifts, _lift_ready = _schedule_stage(
        tasks,
        ASSUMPTIONS["lift_count"],
        lambda task: task.lift_seconds,
        lambda task: lift_release[task.task_id],
        strategy,
    )
    agv_release = {
    # 第三段：AGV 必须等提升机结束并完成交接后才能开始。
        task.task_id: (
            lifts[task.task_id][2]
            + ASSUMPTIONS["lift_handoff_seconds"] / 60.0
        )
        for task in tasks
    }
    agvs, _agv_ready = _schedule_stage(
        tasks,
        ASSUMPTIONS["agv_count"],
        lambda task: task.agv_seconds,
        lambda task: agv_release[task.task_id],
        strategy,
    )
    sortable = [task for task in tasks if task.requires_sorter]
    # 第四段：只有出库任务进入自动分拣线，入库任务不占分拣资源。
    sorter_release = {
        task.task_id: (
            agvs[task.task_id][2]
            + ASSUMPTIONS["sorter_handoff_seconds"] / 60.0
        )
        for task in sortable
    }
    sorters, _sorter_ready = _schedule_stage(
        sortable,
        ASSUMPTIONS["sorter_count"],
        lambda task: task.sorter_seconds,
        lambda task: sorter_release[task.task_id],
        strategy,
    )

    detail = []
    # 把四个阶段的时间合并成一个扁平字典，便于输出 CSV 和独立校验。
    for task in tasks:
        stacker_id, stacker_start, stacker_end = stackers[task.task_id]
        lift_id, lift_start, lift_end = lifts[task.task_id]
        agv_id, agv_start, agv_end = agvs[task.task_id]
        if task.requires_sorter:
            sorter_id, sorter_start, sorter_end = sorters[task.task_id]
            sorter_id += 1
        else:
            sorter_id = 0
            sorter_start = agv_end
            sorter_end = agv_end
        detail.append({
            "task_id": task.task_id,
            "task_type": task.task_type,
            "order_id": task.order_id,
            "batch_id": task.batch_id,
            "product": task.product,
            "slot_id": task.slot_id,
            "order_day": task.order_day,
            "release_min": task.release_min,
            "due_min": task.due_min,
            "path_distance_m": round(task.path_distance_m, 2),
            "travel_to_pd_m": round(task.travel_to_pd_m, 2),
            "travel_to_dock_m": round(task.travel_to_dock_m, 2),
            "stacker_id": stacker_id + 1,
            "stacker_start": stacker_start,
            "stacker_end": stacker_end,
            "lift_id": lift_id + 1,
            "lift_start": lift_start,
            "lift_end": lift_end,
            "agv_id": agv_id + 1,
            "agv_start": agv_start,
            "agv_end": agv_end,
            "sorter_id": sorter_id,
            "sorter_start": sorter_start,
            "sorter_end": sorter_end,
            "worker_seconds": task.worker_seconds,
        })

    breaks = _schedule_workers(detail)
    # 设备排完之后再为人工班组排班，因为人工受低温连续作业限制。
    work_start = min(
        (row["stacker_start"] for row in detail), default=0.0)
    equipment_end = max(
        (row["sorter_end"] for row in detail), default=work_start)
    work_end = max(
        equipment_end,
        max((row["worker_end"] for row in detail), default=equipment_end),
    )
    makespan = work_end - work_start
    # 利用率 = 有效作业时间 / (时间跨度 × 设备数量)。
    busy_stacker = sum(task.stacker_seconds for task in tasks) / 60.0
    busy_lift = sum(task.lift_seconds for task in tasks) / 60.0
    busy_agv = sum(task.agv_seconds for task in tasks) / 60.0
    busy_sorter = sum(task.sorter_seconds for task in sortable) / 60.0
    denominator = makespan if makespan else 1.0
    return {
        "detail": detail,
        "breaks": {
            "team_%d" % (team + 1): [
                {"start_min": start, "end_min": end}
                for start, end in intervals
            ]
            for team, intervals in breaks.items()
        },
        "work_start_min": round(work_start, 2),
        "work_end_min": round(work_end, 2),
        "equipment_end_min": round(equipment_end, 2),
        "makespan_min": round(makespan, 2),
        "stacker_utilisation_pct": round(
            100.0 * busy_stacker
            / (denominator * ASSUMPTIONS["stacker_count"]), 1),
        "lift_utilisation_pct": round(
            100.0 * busy_lift
            / (denominator * ASSUMPTIONS["lift_count"]), 1),
        "agv_utilisation_pct": round(
            100.0 * busy_agv
            / (denominator * ASSUMPTIONS["agv_count"]), 1),
        "sorter_utilisation_pct": round(
            100.0 * busy_sorter
            / (denominator * ASSUMPTIONS["sorter_count"]), 1),
        "cold_breaks": sum(len(intervals) for intervals in breaks.values()),
        "max_continuous_work_min": _max_continuous_work(detail),
        "average_wait_min": round(
            sum(row["stacker_start"] - row["release_min"] for row in detail)
            / len(detail), 2) if detail else 0.0,
    }


def _max_continuous_work(detail):
    """返回考虑自然间隔和强制休息后，单个班组最长的连续作业时间。"""
    by_team = defaultdict(list)
    for row in detail:
        by_team[row["worker_team"]].append(row)
    maximum = 0.0
    limit = ASSUMPTIONS["cold_work_limit_min"]
    break_min = ASSUMPTIONS["cold_break_min"]
    for rows in by_team.values():
        rows.sort(key=lambda row: (row["worker_start"], row["task_id"]))
        continuous = 0.0
        previous_end = None
        for row in rows:
            if previous_end is not None:
                gap = row["worker_start"] - previous_end
                if gap + 1e-9 >= break_min:
                    continuous = 0.0
            continuous += row["worker_minutes"]
            maximum = max(maximum, continuous)
            previous_end = row["worker_end"]
    return round(maximum, 2)


# ------------------------------------------------------------------ docks
def _deviation(index):
    """按固定模式循环生成预约与到达偏差，保证每次运行结果可重复。"""
    pattern = ASSUMPTIONS["appointment_deviation_pattern_min"]
    return pattern[index % len(pattern)]


def schedule_docks(orders, equipment_detail, strategy):
    """为车辆安排月台，并分别保留预约、TMS 预测和实际到达时间。

    appointment_min 是承诺给客户的预约时段，eta_min 是 TMS 预测，arrival_min
    是仿真实际到达，不能直接照抄预约时间。开始装车还必须同时满足车辆已到、
    货已备齐、月台空闲三个条件。
    """
    ready_by_order = defaultdict(float)
    for row in equipment_detail:
        if row["task_type"] != "outbound":
            continue
        ready_by_order[row["order_id"]] = max(
            ready_by_order[row["order_id"]], row["sorter_end"])
    vehicles = []
    # 每辆车保留五个不同时间：客户请求、TMS预测、预约、实际到达、开始装车。
    for index, order in enumerate(orders):
        ready = ready_by_order.get(order.order_id, order.release_min)
        requested_eta = order.release_min - 180.0 + index * 4.0
        if strategy == "experience":
            appointment = None
            eta = None
            arrival = requested_eta
            deviation = None
            prepare_by = None
        elif strategy == "data_driven":
            # 预约取“货物备齐后缓冲时间”和“截止前 60 分钟”中较晚者。
            appointment = max(
                ready + ASSUMPTIONS["appointment_prepare_buffer_min"],
                order.due_min - 60.0,
            )
            deviation = _deviation(index)
            # The TMS forecast is deliberately imperfect and is generated
            # independently from the promised appointment.
            eta = appointment + 0.5 * deviation
            # ETA 是预测，实际到达再叠加完整偏差，两者不能写成同一个值。
            arrival = appointment + deviation
            prepare_by = (
                appointment
                - ASSUMPTIONS["appointment_prepare_buffer_min"]
            )
        else:
            raise ValueError("unknown dock strategy: %s" % strategy)
        vehicles.append({
            "vehicle_id": order.vehicle_id,
            "order_id": order.order_id,
            "quantity": order.quantity,
            "order_day": order.order_day,
            "requested_eta_min": requested_eta,
            "release_min": order.release_min,
            "due_min": order.due_min,
            "ready_min": ready,
            "eta_min": eta,
            "appointment_min": appointment,
            "arrival_deviation_min": deviation,
            "arrival_min": arrival,
            "prepare_by_min": prepare_by,
        })

    if strategy == "experience":
        ordered = sorted(
            vehicles, key=lambda item: (item["arrival_min"], item["vehicle_id"]))
    else:
        ordered = sorted(
            vehicles, key=lambda item: (
                item["eta_min"], item["due_min"], item["vehicle_id"]))
    dock_ready = [0.0] * ASSUMPTIONS["pilot_docks"]
    detail = []
    for vehicle in ordered:
        candidates = [
            (
                max(vehicle["arrival_min"], vehicle["ready_min"],
                    dock_ready[dock]),
                dock,
            )
            for dock in range(len(dock_ready))
        ]
        start, dock = min(candidates)
        service = (
            vehicle["quantity"]
            * ASSUMPTIONS["dock_service_min_per_piece"])
        end = start + service
        dock_ready[dock] = end
        detail.append({
            **vehicle,
            "dock_id": dock + 1,
            "start_min": start,
            "end_min": end,
            "wait_min": start - vehicle["arrival_min"],
            "cargo_wait_min": max(
                0.0, vehicle["ready_min"] - vehicle["arrival_min"]),
            "late": start > vehicle["due_min"],
            "appointment_compliant": (
                None
                if vehicle["appointment_min"] is None
                else abs(vehicle["arrival_deviation_min"])
                <= ASSUMPTIONS["appointment_compliance_tolerance_min"]
            ),
        })

    total_service = sum(
        vehicle["quantity"] * ASSUMPTIONS["dock_service_min_per_piece"]
        for vehicle in vehicles)
    horizon = (
        max(vehicle["end_min"] for vehicle in detail)
        - min(vehicle["arrival_min"] for vehicle in detail)
    )
    eta_errors = [
        abs(row["arrival_min"] - row["eta_min"])
        for row in detail if row["eta_min"] is not None
    ]
    appointment_compliant = [
        row["appointment_compliant"] for row in detail
        if row["appointment_compliant"] is not None
    ]
    return {
        "detail": detail,
        "average_wait_min": round(
            sum(row["wait_min"] for row in detail) / len(detail), 2),
        "max_wait_min": round(
            max(row["wait_min"] for row in detail), 2),
        "average_cargo_wait_min": round(
            sum(row["cargo_wait_min"] for row in detail) / len(detail), 2),
        "average_eta_abs_error_min": round(
            sum(eta_errors) / len(eta_errors), 2) if eta_errors else 0.0,
        "appointment_compliance_pct": round(
            100.0 * sum(1 for value in appointment_compliant if value)
            / len(appointment_compliant), 1)
        if appointment_compliant else 0.0,
        "late_vehicles": sum(1 for row in detail if row["late"]),
        "dock_utilisation_pct": round(
            100.0 * total_service
            / (horizon * ASSUMPTIONS["pilot_docks"]), 1)
        if horizon else 0.0,
    }
