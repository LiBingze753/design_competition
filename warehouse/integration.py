# -*- coding: utf-8 -*-
"""Warehouse-delivery integration model for case 2.4.4(3).

The module links OMS, Master WMS, TMS and a customer appointment front end.
It produces a full order-dock lifecycle with:

    customer request -> appointment -> TMS ETA -> actual arrival -> loading

The actual arrival is generated independently from the appointment.  The TMS
forecast is also imperfect, so the model can measure ETA error, appointment
compliance, rescheduling and dock waiting.
"""
# 本文件对应案例 2.4.4(3)：仓储与配送一体化。
# 它把客户、OMS、WMS、WCS 和 TMS 串成一条可检查的订单履约链：
# 客户请求 -> 预约确认 -> TMS 预测到达 -> 车辆实际到达 -> 装车离场。
# 重点不是“预测一定准确”，而是让四个时间分开记录，从而能计算：
# ETA 误差、预约偏差、月台等待、动态重排车辆数和迟到车辆数。

# 五个容易混淆的时间：
# requested_eta_min：客户自己希望到达的时间。
# eta_min：TMS 根据订单和运输状态预测的到达时间。
# appointment_min：企业与客户确认的预约到达时间。
# actual_arrival_min：仿真中车辆真正到达的时间，独立生成，不直接照抄预约。
# start_min：车辆实际开始装货的时间，必须在到达和备货完成之后。

from collections import defaultdict
from dataclasses import dataclass, field
import csv
import html
import json
import os

import data
import model


HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "output")
os.makedirs(OUT, exist_ok=True)
# 所有结果统一写入 warehouse/output，便于报告、校验脚本和演示页面共用。


@dataclass
class Interface:
    """一条系统接口约定：谁把什么数据、在什么事件下传给谁。"""
    interface_id: str
    source_system: str
    target_system: str
    direction: str
    payload: list
    trigger: str


@dataclass
class LinkedVehicle:
    """一辆提货车辆从下单到离场的完整协同记录。

    一行记录同时保留订单、备货、预约和月台信息，因此可以从结果表
    反查“为什么这辆车等待、为什么被重排、是否迟到”。
    """
    vehicle_id: str
    order_id: str
    order_day: int
    quantity: int
    release_min: int
    due_min: int
    ready_min: float
    # 下面四个字段分别代表客户意愿、企业预测、双方确认和仿真事实。
    requested_eta_min: float
    appointment_min: float
    eta_min: float
    actual_arrival_min: float
    # 预约偏差是“实际到达 - 预约时间”，正数表示晚于预约。
    appointment_deviation_min: float
    # 要求在此时刻前完成备货，给月台留出准备时间。
    prepare_by_min: float
    # initial_dock_id 是预约阶段初排，dock_id 是实际到达后重排的最终月台。
    dock_id: int = 0
    initial_dock_id: int = 0
    start_min: float = 0.0
    end_min: float = 0.0
    # wait_min 是车辆等待，cargo_wait_min 是因货未备齐产生的等待。
    wait_min: float = 0.0
    cargo_wait_min: float = 0.0
    late: bool = False
    appointment_compliant: bool = True
    rescheduled: bool = False
    state: str = "CREATED"
    # transitions 保存状态机历史，供“全链路追溯”页面或答辩演示使用。
    transitions: list = field(default_factory=list)


def interface_contracts():
    """定义一体化方案需要的六个关键接口。

    接口是答辩中“系统如何打通”的最小证据：只有字段和触发事件明确，
    OMS、WMS、WCS、TMS 与客户页面之间才不是一张概念图。
    """
    return [
        Interface(
            "IF-01",
            "OMS",
            "Master WMS",
            "OMS->WMS",
            ["order_id", "merchant_id", "sku_id", "quantity", "due_time"],
            "订单审核通过",
        ),
        Interface(
            "IF-02",
            "Master WMS",
            "TMS",
            "WMS->TMS",
            ["order_id", "ready_time", "pick_status", "staging_dock", "weight"],
            "出库任务完成或状态变化",
        ),
        Interface(
            "IF-03",
            "Customer Portal",
            "TMS",
            "客户->TMS",
            ["order_id", "plate_no", "requested_eta", "quantity", "temperature_zone"],
            "客户提交预约",
        ),
        Interface(
            "IF-04",
            "TMS",
            "Master WMS",
            "TMS->WMS",
            ["order_id", "appointment_time", "prepare_by", "dock_id"],
            "预约确认或动态改约",
        ),
        Interface(
            "IF-05",
            "TMS",
            "Customer Portal",
            "TMS->客户",
            ["order_id", "appointment_time", "dock_id", "queue_no", "contact"],
            "月台分配或重排完成",
        ),
        Interface(
            "IF-06",
            "Master WCS",
            "Master WMS",
            "WCS->WMS",
            ["task_id", "device_id", "task_status", "event_time"],
            "设备任务状态回传",
        ),
    ]


def _warehouse_context():
    """先生成仓内基础结果，作为配送协同模型的输入。

    返回的 ready_by_order 表示每张订单最早具备提货条件的时刻。
    这个时间来自出库任务的最后一道分拣完成时间，而不是车辆到达时间。
    """
    slots = model.build_slots()
    batches = model.generate_batches()
    orders = model.generate_orders()
    allocation = model.assign_slots(batches, slots, "data_driven")
    lines = model.process_orders(orders, batches, allocation, "data_driven")
    tasks = model.build_equipment_tasks(batches, lines, allocation)
    equipment = model.schedule_equipment(tasks, "data_driven")
    ready_by_order = defaultdict(float)
    for row in equipment["detail"]:
        if row["task_type"] == "outbound":
            ready_by_order[row["order_id"]] = max(
                ready_by_order[row["order_id"]], row["sorter_end"])
    # 同一订单可能有多个托盘任务，必须取最后一个分拣完成时间。
    return orders, ready_by_order, equipment


def _deviation(index):
    """按固定样本循环生成实际到达偏差，保证实验可重复。

    这里使用“准时 + 早到 + 晚到”的混合样本，模拟真实运输中的
    不确定性。它不是企业真实数据，而是公开模型假设。
    """
    pattern = data.ASSUMPTIONS["appointment_deviation_pattern_min"]
    return pattern[index % len(pattern)]


def build_linked_vehicles(orders, ready_by_order, mode):
    """为某个对比模式，把每张订单转换成一辆可追溯的提货车辆记录。"""
    # 同一个订单只生成一辆提货车辆，便于比较 manual 和 integrated 两种模式。
    records = []
    for index, order in enumerate(orders):
        ready = ready_by_order.get(order.order_id, order.release_min)
        deviation = _deviation(index + (3 if mode == "integrated" else 0))
        requested_eta = order.release_min - 120.0 + (index % 6) * 5.0
        if mode == "manual":
            # 人工模式：预约基本沿用客户意愿，TMS 也把请求时间当作 ETA。
            appointment = requested_eta + 30.0
            eta = requested_eta
            actual_arrival = requested_eta + deviation
            prepare_by = requested_eta - 30.0
        elif mode == "integrated":
            # 协同模式：预约同时参考客户请求和 WMS 备货完成时间；
            # actual_arrival 仍按独立偏差生成，避免“预约等于实际到达”的假结果。
            # The appointment is recalculated against both the customer's
            # request and the WMS ready time.  Actual arrival remains separate.
            appointment = max(ready + 15.0, requested_eta + 45.0)
            eta = appointment + 0.5 * deviation
            actual_arrival = appointment + deviation
            prepare_by = (
                appointment
                - data.ASSUMPTIONS["appointment_prepare_buffer_min"]
            )
        else:
            raise ValueError("unknown integration mode: %s" % mode)
        records.append(LinkedVehicle(
            vehicle_id=order.vehicle_id,
            order_id=order.order_id,
            order_day=order.order_day,
            quantity=order.quantity,
            release_min=order.release_min,
            due_min=order.due_min,
            ready_min=ready,
            requested_eta_min=requested_eta,
            appointment_min=appointment,
            eta_min=eta,
            actual_arrival_min=actual_arrival,
            appointment_deviation_min=deviation,
            prepare_by_min=prepare_by,
        ))
    return records


def _assign_docks(records, use_forecast):
    """分两轮安排月台：预约阶段先初排，车辆实际到达后再动态调整。"""
    # 第一轮在预约阶段分配月台，给客户一个可见的预约结果。
    # 第二轮在车辆实际到达后重新排程，处理 ETA 误差和月台拥堵。
    dock_ready = [0.0] * data.ASSUMPTIONS["pilot_docks"]
    initial_intervals = defaultdict(list)
    order_for_initial = (
        (lambda row: (row.eta_min, row.due_min, row.vehicle_id))
        if use_forecast else
        (lambda row: (row.actual_arrival_min, row.due_min, row.vehicle_id))
    )
    # use_forecast=True 代表提前按 ETA 预约；False 代表只能按实际到达排。
    for row in sorted(records, key=order_for_initial):
        reference = (
            row.eta_min if use_forecast else row.actual_arrival_min)
        # 候选月台的开始时间取三者最大值：预约参考时间、货齐时间、月台空闲时间。
        candidates = [
            (max(reference, row.ready_min, dock_ready[dock]), dock)
            for dock in range(len(dock_ready))
        ]
        start, dock = min(candidates)
        service = (
            row.quantity * data.ASSUMPTIONS["dock_service_min_per_piece"])
        end = start + service
        dock_ready[dock] = end
        row.initial_dock_id = dock + 1
        initial_intervals[dock + 1].append((start, end))

    # 第二轮重新从零开始排时，确保后面的到达顺序和实际拥堵被重新考虑。
    dock_ready = [0.0] * data.ASSUMPTIONS["pilot_docks"]
    for row in sorted(records, key=lambda item: (
            item.start_min or item.actual_arrival_min,
            item.actual_arrival_min,
            item.vehicle_id)):
        candidates = []
        for dock in range(len(dock_ready)):
            start = max(
                row.actual_arrival_min, row.ready_min, dock_ready[dock])
            candidates.append((
                start,
                row.actual_arrival_min - row.ready_min,
                dock + 1,
            ))
        start, _ready_gap, dock_id = min(candidates)
        service = (
            row.quantity * data.ASSUMPTIONS["dock_service_min_per_piece"])
        end = start + service
        dock_ready[dock_id - 1] = end
        row.dock_id = dock_id
        row.start_min = start
        row.end_min = end
        row.wait_min = start - row.actual_arrival_min
        row.cargo_wait_min = max(
            0.0, row.ready_min - row.actual_arrival_min)
        row.late = start > row.due_min
        row.appointment_compliant = (
            abs(row.actual_arrival_min - row.appointment_min)
            <= data.ASSUMPTIONS["appointment_compliance_tolerance_min"]
        )
        row.rescheduled = dock_id != row.initial_dock_id
    return records, initial_intervals


def build_state_history(row):
    """生成一张订单从 OMS、WMS、WCS 到 TMS 的完整状态历史。"""
    # 状态事件按时间排序后写入，报告里可以展示每张订单经过的系统节点。
    # 这里不改变真实业务状态，只把模型已经算出的时间点翻译成可追溯状态。
    events = [
        (row.release_min - 360.0, "CREATED", "OMS"),
        (row.release_min, "RELEASED", "Master WMS"),
        (max(0.0, row.ready_min - 120.0), "ALLOCATED", "Master WMS"),
        (max(0.0, row.ready_min - 60.0), "PICKING", "Master WCS"),
        (row.ready_min, "STAGED", "Master WMS"),
        (row.appointment_min, "APPOINTMENT_CONFIRMED", "TMS"),
        (row.start_min, "LOADING", "TMS"),
        (row.end_min, "DEPARTED", "TMS"),
    ]
    deduplicated = []
    for event_min, state, system in sorted(events, key=lambda item: item[0]):
        if deduplicated and deduplicated[-1][1] == state:
            continue
        deduplicated.append((event_min, state, system))
    row.transitions = [
        {
            "order_id": row.order_id,
            "vehicle_id": row.vehicle_id,
            "state": state,
            "system": system,
            "event_min": event_min,
            "event_time": data.hhmm(event_min),
        }
        for event_min, state, system in deduplicated
    ]
    row.state = deduplicated[-1][1]
    return row.transitions


def summarize(records):
    """把车辆明细汇总成答辩需要的 KPI。

    这里的口径必须和报告文字一致：
    - ETA 误差比较 TMS 预测与实际到达；
    - 预约误差比较客户预约与实际到达；
    - 月台利用率 = 总装货服务时间 /（仿真跨度 × 月台数）。
    """
    eta_errors = [
        abs(row.actual_arrival_min - row.eta_min) for row in records]
    appointment_errors = [
        abs(row.actual_arrival_min - row.appointment_min) for row in records]
    total_service = sum(
        row.quantity * data.ASSUMPTIONS["dock_service_min_per_piece"]
        for row in records)
    first_arrival = min(row.actual_arrival_min for row in records)
    last_end = max(row.end_min for row in records)
    horizon = last_end - first_arrival
    # horizon 是实际仿真跨度，不是固定 24 小时，避免高估或低估利用率。
    return {
        "vehicles": len(records),
        "orders": len({row.order_id for row in records}),
        "average_wait_min": round(
            sum(row.wait_min for row in records) / len(records), 2),
        "max_wait_min": round(
            max(row.wait_min for row in records), 2),
        "average_cargo_wait_min": round(
            sum(row.cargo_wait_min for row in records) / len(records), 2),
        "average_eta_abs_error_min": round(
            sum(eta_errors) / len(eta_errors), 2),
        "average_appointment_abs_error_min": round(
            sum(appointment_errors) / len(appointment_errors), 2),
        "appointment_compliance_pct": round(
            100.0 * sum(1 for row in records if row.appointment_compliant)
            / len(records), 1),
        "rescheduled_vehicles": sum(1 for row in records if row.rescheduled),
        "late_vehicles": sum(1 for row in records if row.late),
        "dock_utilisation_pct": round(
            100.0 * total_service
            / (horizon * data.ASSUMPTIONS["pilot_docks"]), 1)
        if horizon else 0.0,
    }


def _write_csv(path, fieldnames, rows):
    """以 UTF-8-SIG 写 CSV，确保 Excel 能直接打开中文列名。"""
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_vehicle_csv(manual, integrated):
    """输出两种模式的车辆明细，供对比等待、重排和迟到情况。"""
    rows = []
    for mode, records in (("manual", manual), ("integrated", integrated)):
        for row in records:
            rows.append({
                "mode": mode,
                "vehicle_id": row.vehicle_id,
                "order_id": row.order_id,
                "order_day": row.order_day,
                "quantity": row.quantity,
                "release_min": round(row.release_min, 2),
                "due_min": round(row.due_min, 2),
                "ready_min": round(row.ready_min, 2),
                "requested_eta_min": round(row.requested_eta_min, 2),
                "appointment_min": round(row.appointment_min, 2),
                "eta_min": round(row.eta_min, 2),
                "actual_arrival_min": round(row.actual_arrival_min, 2),
                "arrival_deviation_min": round(
                    row.appointment_deviation_min, 2),
                "prepare_by_min": round(row.prepare_by_min, 2),
                "initial_dock_id": row.initial_dock_id,
                "dock_id": row.dock_id,
                "start_min": round(row.start_min, 2),
                "end_min": round(row.end_min, 2),
                "wait_min": round(row.wait_min, 2),
                "cargo_wait_min": round(row.cargo_wait_min, 2),
                "appointment_compliant": int(row.appointment_compliant),
                "rescheduled": int(row.rescheduled),
                "late": int(row.late),
                "state": row.state,
            })
    _write_csv(
        os.path.join(OUT, "integration_vehicles.csv"),
        ["mode", "vehicle_id", "order_id", "order_day", "quantity",
         "release_min", "due_min", "ready_min", "requested_eta_min",
         "appointment_min", "eta_min", "actual_arrival_min",
         "arrival_deviation_min", "prepare_by_min", "initial_dock_id",
         "dock_id", "start_min", "end_min", "wait_min", "cargo_wait_min",
         "appointment_compliant", "rescheduled", "late", "state"],
        rows,
    )


def write_transition_csv(records):
    """输出订单状态历史，支持从订单反查各系统节点时间。"""
    rows = []
    for row in records:
        rows.extend(build_state_history(row))
    _write_csv(
        os.path.join(OUT, "integration_state_history.csv"),
        ["order_id", "vehicle_id", "state", "system", "event_min",
         "event_time"],
        rows,
    )


def write_interface_json():
    """把接口约定写成 JSON，方便答辩时展示系统边界和数据流。"""
    contracts = interface_contracts()
    payload = {
        "architecture": "OMS -> Master WMS -> Master WCS; Master WMS <-> TMS; Customer Portal <-> TMS",
        "interfaces": [
            {
                "interface_id": item.interface_id,
                "source_system": item.source_system,
                "target_system": item.target_system,
                "direction": item.direction,
                "payload": item.payload,
                "trigger": item.trigger,
            }
            for item in contracts
        ],
    }
    with open(
            os.path.join(OUT, "integration_interfaces.json"),
            "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return payload


def appointment_portal_html():
    """返回一个无需后端即可演示的客户预约页面 HTML。

    页面只是交互原型，使用前端规则模拟月台号分配；
    真实落地时需要由 TMS/WMS 接口返回预约和月台结果。
    """
    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>银犁提货预约轻量原型</title>
<style>
:root{--ink:#0f172a;--muted:#64748b;--line:#dbe3ee;--blue:#1d4ed8;--green:#15803d}
*{box-sizing:border-box}body{margin:0;background:#f4f7fb;color:var(--ink);
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}
main{max-width:760px;margin:0 auto;padding:24px 18px 48px}
header{padding:22px 0 15px;border-bottom:1px solid var(--line)}
h1{font-size:24px;margin:0 0 6px}p{line-height:1.65;color:var(--muted)}
.panel{background:#fff;border:1px solid var(--line);border-radius:8px;padding:18px;margin-top:18px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
label{display:block;font-size:13px;color:var(--muted);margin-bottom:6px}
input,select{width:100%;height:40px;border:1px solid var(--line);border-radius:6px;
padding:0 10px;background:#fff;color:var(--ink);font-size:14px}
button{height:42px;border:0;border-radius:6px;background:var(--blue);color:#fff;
font-size:14px;padding:0 20px;cursor:pointer;margin-top:16px}
.result{display:none;margin-top:16px;border-left:3px solid var(--green);padding:10px 12px;background:#f0fdf4}
span.status{font-size:12px;color:var(--green);border:1px solid #bbf7d0;background:#f0fdf4;
border-radius:999px;padding:3px 8px}
@media(max-width:620px){.grid{grid-template-columns:1fr}}
</style></head><body><main>
<header><span class="status">比赛演示 · 未接入真实微信小程序</span>
<h1>提货预约与月台确认</h1>
<p>客户提交订单、车牌、预计到达时间和提货量，系统回传预约时段与月台号。</p></header>
<section class="panel"><div class="grid">
<div><label>订单号</label><input id="order" value="O00-00"></div>
<div><label>车牌号</label><input id="plate" placeholder="川A12345"></div>
<div><label>预计到达</label><input id="eta" type="datetime-local"></div>
<div><label>提货件数</label><input id="quantity" type="number" min="1" value="20"></div>
<div><label>温区</label><select id="zone"><option>0-4C</option><option>-18C</option><option>常温</option></select></div>
<div><label>联系电话</label><input id="phone" placeholder="13800000000"></div>
</div><button id="submit">提交预约</button>
<div class="result" id="result"></div></section>
</main><script>
document.getElementById('submit').addEventListener('click',function(){
 var order=document.getElementById('order').value.trim();
 var plate=document.getElementById('plate').value.trim();
 var eta=document.getElementById('eta').value;
 var qty=Number(document.getElementById('quantity').value||0);
 if(!order||!plate||!eta||qty<=0){alert('请完整填写订单、车牌、到达时间和提货件数');return;}
 var dock=1+(order.charCodeAt(order.length-1)%6);
 document.getElementById('result').style.display='block';
 document.getElementById('result').innerHTML='<b>预约已进入演示队列</b><br>订单：'+order+
 '<br>车牌：'+plate+'<br>预约月台：D'+dock+'<br>系统将在实际作业中根据备货完成时间和月台拥堵情况动态重排。';
});
</script></body></html>"""


def main():
    """运行 2.4.4(3) 的完整协同仿真并输出全部交付物。

    运行顺序是固定的：
    仓内备货 -> 生成车辆时间 -> 预约初排 -> 到达后重排 -> 状态追溯 -> KPI 汇总。
    """
    orders, ready_by_order, equipment = _warehouse_context()
    manual = build_linked_vehicles(orders, ready_by_order, "manual")
    integrated = build_linked_vehicles(orders, ready_by_order, "integrated")
    _assign_docks(manual, use_forecast=False)
    _assign_docks(integrated, use_forecast=True)
    for row in manual:
        build_state_history(row)
    for row in integrated:
        build_state_history(row)

    # 先落盘明细，再落盘汇总。汇总文件只引用明细结果，不反向修改模型。
    write_vehicle_csv(manual, integrated)
    write_transition_csv(integrated)
    interfaces = write_interface_json()
    with open(
            os.path.join(OUT, "appointment_portal.html"),
            "w", encoding="utf-8") as handle:
        handle.write(appointment_portal_html())

    summary = {
        "data_scope": {
            "orders": len(orders),
            "simulation_days": data.HORIZON_DAYS,
            "warehouse_tasks": len(equipment["detail"]),
        },
        "modes": {
            "manual": summarize(manual),
            "integrated": summarize(integrated),
        },
        "interfaces": interfaces,
        "prototype_notice": "预约页面为比赛用静态 HTML 原型，未接入真实微信或企业系统。",
        "outputs": {
            "vehicles_csv": "integration_vehicles.csv",
            "state_history_csv": "integration_state_history.csv",
            "interface_json": "integration_interfaces.json",
            "portal_html": "appointment_portal.html",
        },
    }
    with open(
            os.path.join(OUT, "integration_summary.json"),
            "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)

    manual_metrics = summary["modes"]["manual"]
    integrated_metrics = summary["modes"]["integrated"]
    print("=" * 76)
    print("银犁冷链 2.4.4(3) 仓储-配送一体化协同模型")
    print("=" * 76)
    print("订单数 %d  仓内设备任务 %d" % (
        len(orders), len(equipment["detail"])))
    print("人工模式: 平均等待 %.2f 分钟, 最大等待 %.2f 分钟, 迟到 %d 台" % (
        manual_metrics["average_wait_min"],
        manual_metrics["max_wait_min"],
        manual_metrics["late_vehicles"],
    ))
    print("协同模式: 平均等待 %.2f 分钟, 最大等待 %.2f 分钟, ETA平均误差 %.2f 分钟" % (
        integrated_metrics["average_wait_min"],
        integrated_metrics["max_wait_min"],
        integrated_metrics["average_eta_abs_error_min"],
    ))
    print("预约偏差合规 %.1f%%  动态重排 %d 台  迟到 %d 台" % (
        integrated_metrics["appointment_compliance_pct"],
        integrated_metrics["rescheduled_vehicles"],
        integrated_metrics["late_vehicles"],
    ))
    print("说明:", summary["prototype_notice"])
    return summary


if __name__ == "__main__":
    main()
