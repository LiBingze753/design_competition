# -*- coding: utf-8 -*-
"""Managed warehouse model for case 2.4.4(2).

The case gives the business context but not a merchant-level operational data
set.  This module therefore generates an explicitly labelled synthetic sample
with many merchants, categories, batches and turnover classes.  It compares
random slotting with a turnover- and affinity-aware policy.

The key traceability chain is:

    merchant -> category -> inbound batch -> pallet/slot -> outbound order

The model is intended to prove the data relationship and slotting logic, not
to present the synthetic volumes as facts about Yinli.
"""
# 本文件对应 2.4.4(2)：多商家代管仓货位分配。
# 案例没有给出商家级明细，因此这里生成显式合成数据，只用于验证模型。
# 关键追溯链：商家 -> 品类 -> 入库批次 -> 托盘/货位 -> 出库订单。
# 模型比较随机分配和考虑周转率、商家/品类聚集度的优化分配。
from collections import defaultdict
from dataclasses import dataclass
import csv
import json
import math
import os
import random

import model


HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "output")
os.makedirs(OUT, exist_ok=True)


@dataclass
class Merchant:
    """入驻商家。不同商家即使品类相同，也必须保留独立库存归属。"""
    merchant_id: str
    merchant_name: str
    priority: str


@dataclass
class Category:
    """品类。turnover_class 表示 A 快周转、B 中周转、C 慢周转。"""
    category_id: str
    category_name: str
    turnover_class: str
    temperature_zone: str


@dataclass
class ManagedBatch:
    """代管仓入库批次，是商家、品类和库位之间的中间实体。"""
    batch_id: str
    merchant_id: str
    category_id: str
    inbound_min: int
    quantity: int
    expiry_day: int
    planned_outbound_min: int
    turnover_class: str


@dataclass
class ManagedOrder:
    """代管仓出库订单，必须能够回查商家、品类、批次和库位。"""
    order_id: str
    merchant_id: str
    category_id: str
    quantity: int
    release_min: int
    due_min: int
    order_day: int


def generate_merchants():
    """生成 12 个可复现的合成入驻商家。"""
    return [
        Merchant("M%02d" % index, "入驻商家%02d" % index, "A" if index <= 4 else "B")
        for index in range(1, 13)
    ]


def generate_categories():
    """生成 8 个合成品类，并给出周转等级和温区。"""
    return [
        Category("C01", "叶菜", "A", "0-4C"),
        Category("C02", "菌菇", "A", "0-4C"),
        Category("C03", "根茎", "B", "0-4C"),
        Category("C04", "鲜果", "A", "0-8C"),
        Category("C05", "冻品", "C", "-18C"),
        Category("C06", "干货", "C", "常温"),
        Category("C07", "调理品", "B", "-18C"),
        Category("C08", "蛋奶", "A", "0-4C"),
    ]


def generate_batches(merchants, categories):
    """生成可复现的合成库存。每个商家选择 3 个品类，每个品类 3 个批次。"""
    # 每个商家只选 3 个品类，模拟真实代管仓中“商家专区”的差异。
    batches = []
    for merchant_index, merchant in enumerate(merchants):
        selected = [
            categories[(merchant_index + offset) % len(categories)]
            for offset in range(3)
        ]
        for category in selected:
            for batch_index in range(3):
                inbound_day = -8 + batch_index * 3 + (merchant_index % 2)
                # 三批货的入库和计划出库时间错开，便于观察 FIFO 与周转差异。
                inbound_min = inbound_day * 1440 + 7 * 60 + batch_index * 45
                quantity = 10 + ((merchant_index + batch_index) % 5)
                batches.append(ManagedBatch(
                    batch_id="%s-%s-B%02d" % (
                        merchant.merchant_id, category.category_id, batch_index + 1),
                    merchant_id=merchant.merchant_id,
                    category_id=category.category_id,
                    inbound_min=inbound_min,
                    quantity=quantity,
                    expiry_day=inbound_day + (
                        8 if category.turnover_class == "A"
                        else 18 if category.turnover_class == "B"
                        else 45
                    ),
                    planned_outbound_min=(
                        inbound_day + (
                            2 if category.turnover_class == "A"
                            else 5 if category.turnover_class == "B"
                            else 12
                        )
                    ) * 1440 + 10 * 60,
                    turnover_class=category.turnover_class,
                ))
    return batches


def generate_orders(merchants, categories):
    """生成三天的合成商家订单，保持与库存品类的对应关系。"""
    # 每位商家每天对 3 个品类各下一单，订单量随序号变化。
    orders = []
    serial = 0
    for day in range(3):
        for merchant in merchants:
            selected = [
                categories[(int(merchant.merchant_id[1:]) - 1 + offset)
                           % len(categories)]
                for offset in range(3)
            ]
            for category in selected:
                serial += 1
                release = day * 1440 + 7 * 60 + (serial % 18) * 5
                quantity = 2 + ((serial + int(merchant.merchant_id[1:])) % 4)
                orders.append(ManagedOrder(
                    order_id="MO%03d" % serial,
                    merchant_id=merchant.merchant_id,
                    category_id=category.category_id,
                    quantity=quantity,
                    release_min=release,
                    due_min=release + 240,
                    order_day=day,
                ))
    return orders


def _zone_for_turnover(turnover_class):
    """把 A/B/C 周转等级映射为快取、常规、慢取分区。"""
    return {"A": "fast", "B": "medium", "C": "slow"}[turnover_class]


def _zone_rank(zone):
    """把分区名称转为可比较的数字等级。"""
    return {"fast": 0, "medium": 1, "slow": 2}[zone]


def assign_managed_slots(batches, slots, strategy, seed=2026):
    """按策略给每个批次托盘分配货位，并保留完整归属关系。"""
    required = sum(batch.quantity for batch in batches)
    if required > len(slots):
        raise ValueError("not enough synthetic managed slots")
    rng = random.Random(seed)
    available = list(slots)
    allocation = defaultdict(list)

    if strategy == "random":
        # 对照组：随机占位，不区分商家、品类、周转率和深度。
        rng.shuffle(available)
        for batch in sorted(batches, key=lambda item: item.batch_id):
            for _ in range(batch.quantity):
                allocation[batch.batch_id].append(available.pop())
        return allocation

    if strategy != "optimized":
        raise ValueError("unknown managed slotting strategy: %s" % strategy)

    ordered = sorted(batches, key=lambda batch: (
    # 优化组：先安排计划出库早的批次，让快出库货物优先进入近端区域。
        batch.planned_outbound_min, batch.inbound_min, batch.batch_id))
    for batch in ordered:
        target_rank = _zone_rank(_zone_for_turnover(batch.turnover_class))
        # anchor 是最近一次同商家或同品类库位，用于衡量是否放得集中。
        same_merchant = [
            slot for other_id, values in allocation.items()
            if other_id.startswith(batch.merchant_id + "-")
            for slot in values
        ]
        same_category = [
            slot for other_id, values in allocation.items()
            if other_id.split("-")[1] == batch.category_id
            for slot in values
        ]
        merchant_anchor = same_merchant[-1] if same_merchant else None
        category_anchor = same_category[-1] if same_category else None
        for _ in range(batch.quantity):
            best_index = None
            best_score = None
            for index, slot in enumerate(available):
                zone_penalty = 35.0 * abs(_zone_rank(slot.zone) - target_rank)
                # 分区、深度、商家聚合和品类聚合共同决定货位得分。
                depth_penalty = 120.0 * max(0, slot.depth - 1)
                merchant_affinity = 0.0
                if merchant_anchor is not None:
                    merchant_distance = (
                        abs(slot.aisle - merchant_anchor.aisle)
                        + abs(slot.rack - merchant_anchor.rack)
                        + abs(slot.bay - merchant_anchor.bay)
                        + abs(slot.level - merchant_anchor.level)
                    )
                    merchant_affinity = (
                        -18.0 if merchant_distance <= 2 else 4.0)
                category_affinity = 0.0
                if category_anchor is not None:
                    category_distance = (
                        abs(slot.aisle - category_anchor.aisle)
                        + abs(slot.rack - category_anchor.rack)
                        + abs(slot.bay - category_anchor.bay)
                        + abs(slot.level - category_anchor.level)
                    )
                    category_affinity = (
                        -8.0 if category_distance <= 2 else 2.0)
                score = (
                # affinity 为负数相当于奖励，让相近库位得分更低。
                    0.10 * slot.stacker_seconds
                    + 0.25 * slot.travel_to_dock_m
                    + zone_penalty
                    + depth_penalty
                    + merchant_affinity
                    + category_affinity
                )
                if best_score is None or score < best_score:
                    best_score = score
                    best_index = index
            chosen = available.pop(best_index)
            allocation[batch.batch_id].append(chosen)
            same_merchant.append(chosen)
            same_category.append(chosen)
    return allocation


def _occupied_slot_map(allocation):
    """把货位坐标映射到占用它的批次和货位对象，供双深位翻箱检查。"""
    occupied = {}
    for batch_id, slots in allocation.items():
        for slot in slots:
            occupied[(slot.aisle, slot.rack, slot.bay, slot.level, slot.depth)] = (
                batch_id, slot)
    return occupied


def _front_slot_id(slot):
    """返回同一个双深位货位的前位编号。"""
    return "A%02d-R%d-B%02d-L%02d-D1" % (
        slot.aisle, slot.rack, slot.bay, slot.level)


def _flip_count(slot, selected_ids, occupied):
    # 双深位后位被取货时，如果前位有托盘且不同时出库，就要先翻出前位。
    """计算取后位托盘时是否需要先移动前位托盘，返回 0 或 1。"""
    if slot.depth != 2:
        return 0
    front_id = _front_slot_id(slot)
    front = None
    for (aisle, rack, bay, level, depth), (batch_id, front_slot) in occupied.items():
        if front_slot.slot_id == front_id:
            front = (batch_id, front_slot)
            break
    if front is None:
        return 0
    if front_id in selected_ids:
        return 0
    return 1


def process_managed_orders(orders, batches, allocation):
    # 逐订单扣减库存，所有拣选行都保留商家、品类、批次和库位。
    """生成代管仓拣选任务，并保留商家、品类、批次、货位全链路追溯。"""
    working = {batch.batch_id: batch.quantity for batch in batches}
    batch_map = {batch.batch_id: batch for batch in batches}
    occupied = _occupied_slot_map(allocation)
    lines = []
    for order in orders:
        candidates = [
            batch for batch in batches
            if batch.merchant_id == order.merchant_id
            and batch.category_id == order.category_id
            and working[batch.batch_id] > 0
            and batch.inbound_min <= order.release_min
        ]
        if not candidates:
            raise RuntimeError("no managed inventory for %s" % order.order_id)
        candidates.sort(key=lambda batch: (
        # 先按最早入库批次取货，严格保持 FIFO。
            batch.inbound_min, batch.planned_outbound_min, batch.batch_id))
        need = order.quantity
        for batch in candidates:
            if need <= 0:
                break
            take = min(need, working[batch.batch_id])
            if take <= 0:
                continue
            slots = sorted(
                allocation[batch.batch_id],
                key=lambda slot: (
                    slot.depth,
                    slot.stacker_seconds + slot.agv_seconds_dock * 0.2,
                ),
            )[:take]
            selected_ids = {slot.slot_id for slot in slots}
            flip_count = sum(
                _flip_count(slot, selected_ids, occupied) for slot in slots)
            lines.append({
                "order_id": order.order_id,
                "merchant_id": order.merchant_id,
                "category_id": order.category_id,
                "batch_id": batch.batch_id,
                "quantity": take,
                "slot_ids": [slot.slot_id for slot in slots],
                "slot_instructions": [
                    "巷道%d/货架%d/%02d列/%02d层/%d深" % (
                        slot.aisle, slot.rack, slot.bay, slot.level, slot.depth)
                    for slot in slots
                ],
                "retrieval_seconds": round(
                    sum(
                        slot.stacker_seconds
                        + slot.lift_seconds
                        + slot.agv_seconds_dock
                        for slot in slots
                    ) / len(slots),
                    2,
                ),
                "path_distance_m": round(
                    sum(slot.travel_to_dock_m for slot in slots) / len(slots),
                    2,
                ),
                "flip_count": flip_count,
                "fifo_violation": int(any(
                    other.inbound_min < batch.inbound_min - 1e-9
                    and working[other.batch_id] > 0
                    for other in candidates
                )),
                "release_min": order.release_min,
                "due_min": order.due_min,
            })
            working[batch.batch_id] -= take
            need -= take
        if need > 0:
            raise RuntimeError(
                "managed order %s remains short by %d" % (order.order_id, need))
    return lines


def managed_metrics(lines, allocation, batches):
    """汇总代管仓 KPI：翻箱率、FIFO、拣选时间、路径和商家分散度。"""
    total_pieces = sum(line["quantity"] for line in lines)
    total_flips = sum(line["flip_count"] for line in lines)
    fifo_violations = sum(
        1 for line in lines if line["fifo_violation"])
    by_merchant = defaultdict(list)
    for batch_id, slots in allocation.items():
        merchant_id = batch_id.split("-")[0]
        by_merchant[merchant_id].extend(slots)
    spans = []
    for slots in by_merchant.values():
        spans.append(len({(slot.aisle, slot.rack, slot.bay) for slot in slots}))
    return {
        "picked_pieces": total_pieces,
        "orders": len({line["order_id"] for line in lines}),
        "average_retrieval_seconds": round(
            sum(line["retrieval_seconds"] * line["quantity"] for line in lines)
            / total_pieces,
            2,
        ),
        "average_path_distance_m": round(
            sum(line["path_distance_m"] * line["quantity"] for line in lines)
            / total_pieces,
            2,
        ),
        "flip_count": total_flips,
        "flip_rate_pct": round(100.0 * total_flips / total_pieces, 2),
        "fifo_compliance_pct": round(
            100.0 * (len(lines) - fifo_violations) / len(lines), 1),
        "average_merchant_rack_span": round(
            sum(spans) / len(spans), 2) if spans else 0.0,
    }


def run_strategy(strategy, slots, batches, merchants, categories):
    """按指定策略运行代管仓模型，并返回分配、拣选和指标。"""
    allocation = assign_managed_slots(batches, slots, strategy)
    orders = generate_orders(merchants, categories)
    lines = process_managed_orders(orders, batches, allocation)
    metrics = managed_metrics(lines, allocation, batches)
    return {
        "strategy": strategy,
        "allocation": allocation,
        "lines": lines,
        "metrics": metrics,
    }


def _write_csv(path, fieldnames, rows):
    """用 UTF-8-SIG 写 CSV，保证 Excel 正常显示中文。"""
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_outputs(results, merchants, categories, batches):
    """输出代管仓货位分配、拣选明细和汇总 JSON。"""
    allocation_rows = []
    for result in results:
        batch_map = {batch.batch_id: batch for batch in batches}
        for batch_id, slots in result["allocation"].items():
            batch = batch_map[batch_id]
            for sequence, slot in enumerate(slots, 1):
                allocation_rows.append({
                    "strategy": result["strategy"],
                    "merchant_id": batch.merchant_id,
                    "category_id": batch.category_id,
                    "batch_id": batch.batch_id,
                    "pallet_sequence": sequence,
                    "slot_id": slot.slot_id,
                    "zone": slot.zone,
                    "depth": slot.depth,
                    "inbound_min": batch.inbound_min,
                    "planned_outbound_min": batch.planned_outbound_min,
                    "turnover_class": batch.turnover_class,
                    "path_distance_m": round(slot.travel_to_dock_m, 2),
                })
    _write_csv(
        os.path.join(OUT, "managed_allocation.csv"),
        ["strategy", "merchant_id", "category_id", "batch_id", "pallet_sequence",
         "slot_id", "zone", "depth", "inbound_min", "planned_outbound_min",
         "turnover_class", "path_distance_m"],
        allocation_rows,
    )

    picking_rows = []
    for result in results:
        for line in result["lines"]:
            picking_rows.append({
                "strategy": result["strategy"],
                "order_id": line["order_id"],
                "merchant_id": line["merchant_id"],
                "category_id": line["category_id"],
                "batch_id": line["batch_id"],
                "quantity": line["quantity"],
                "slot_ids": " ".join(line["slot_ids"]),
                "slot_instructions": " | ".join(line["slot_instructions"]),
                "retrieval_seconds": line["retrieval_seconds"],
                "path_distance_m": line["path_distance_m"],
                "flip_count": line["flip_count"],
                "fifo_violation": line["fifo_violation"],
                "release_min": line["release_min"],
                "due_min": line["due_min"],
            })
    _write_csv(
        os.path.join(OUT, "managed_picking.csv"),
        ["strategy", "order_id", "merchant_id", "category_id", "batch_id",
         "quantity", "slot_ids", "slot_instructions", "retrieval_seconds",
         "path_distance_m", "flip_count", "fifo_violation", "release_min",
         "due_min"],
        picking_rows,
    )

    summary = {
        "synthetic_data_notice": (
            "本模块数据为显式合成数据，仅用于验证多商家、多品类、多批次、"
            "多周转率条件下的代管仓模型，不代表银犁企业实际业务量。"
        ),
        "data_scale": {
            "merchants": len(merchants),
            "categories": len(categories),
            "batches": len(batches),
            "batch_pallets": sum(batch.quantity for batch in batches),
            "orders": len(generate_orders(merchants, categories)),
        },
        "strategies": {
            result["strategy"]: result["metrics"] for result in results
        },
        "traceability_chain": [
            "merchant_id", "category_id", "batch_id", "slot_id", "order_id"
        ],
        "outputs": {
            "allocation_csv": "managed_allocation.csv",
            "picking_csv": "managed_picking.csv",
        },
    }
    with open(
            os.path.join(OUT, "managed_summary.json"),
            "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
    return summary


def main():
    """运行随机和优化两种代管仓方案，并返回可直接嵌套的总汇总。"""
    slots = model.build_slots()
    merchants = generate_merchants()
    categories = generate_categories()
    batches = generate_batches(merchants, categories)
    results = [
        run_strategy("random", slots, batches, merchants, categories),
        run_strategy("optimized", slots, batches, merchants, categories),
    ]
    summary = write_outputs(results, merchants, categories, batches)
    print("=" * 76)
    print("银犁冷链 2.4.4(2) 代管仓库多商家货位分配模型")
    print("=" * 76)
    for strategy in ("random", "optimized"):
        metrics = summary["strategies"][strategy]
        print("[%s] 翻箱率 %.2f%%  平均拣选 %.2f 秒  货架跨度 %.2f" % (
            strategy,
            metrics["flip_rate_pct"],
            metrics["average_retrieval_seconds"],
            metrics["average_merchant_rack_span"],
        ))
    print("说明:", summary["synthetic_data_notice"])
    return summary


if __name__ == "__main__":
    main()
