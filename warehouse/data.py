# -*- coding: utf-8 -*-
"""Data layer for the Yinli cold-storage intelligent warehouse model (case 2.4).

Case facts are separated from planning assumptions. Every assumed value is
marked in ``ASSUMPTIONS`` and repeated in the generated summary so that the
business team can replace it with site data without changing the model code.
"""
import math

# 本文件只负责“原始数据和参数”，不写算法。
# 企业后续拿到真实参数时，优先修改本文件，不需要重写模型逻辑。

# ------------------------------------------------------------------ case facts
# 案例原文可以直接查到的数据。报告引用这些数字时，应注明来自案例。
CASE_FACTS = {
    "cold_storage_area_m2": 260000,          # section 2.4.2
    "cold_storage_capacity_t": 600000,       # section 2.4.2
    "high_temperature_area_m2": 25000,       # table 2-1
    "high_temperature_capacity_t": 15000,    # table 2-1
    "stereoscopic_rack_area_m2": 30000,      # section 2.4.2
    "stereoscopic_slots": 30000,             # section 2.4.2
    "open_docks": 252,                       # section 2.4.2
    "closed_docks": 24,                      # section 2.4.2
    "forklifts": 78,
    "tractors": 20,
    "t_cars": 129,
    "three_phase_area_m2": 230000,
    "three_phase_investment_yuan": 1100000000,
    "historical_ginger_days": 510,
    "historical_ginger_pieces": 78000,
    "historical_ginger_daily_pieces": 150,
    "historical_ginger_cost_wan": 17.0,
    "historical_garlic_days": 150,
    "historical_garlic_pieces": 6300,
    "historical_garlic_daily_pieces": 40,
    "historical_garlic_cost_wan": 1.6,
}

# ------------------------------------------------------------------ assumptions
# 案例没有直接给出的数据。它们不是企业事实，而是模型假设。
ASSUMPTIONS = {
    # 货位规模：在库时间、峰值系数和安全缓冲。
    "average_ginger_dwell_days": 7.0,
    "average_garlic_dwell_days": 14.0,
    "peak_factor": 1.15,
    "buffer_ratio": 0.10,
    "pilot_slot_area_allowance_m2": 1.25,   # staging, aisles, charging, P&D
    "pilot_docks": 6,
    "stacker_count": 4,
    # 堆垛机：数量、水平/垂直速度和每次取放货固定耗时。
    "stacker_horizontal_mps": 2.0,
    "stacker_vertical_mps": 0.5,
    "stacker_service_seconds": 25.0,
    "agv_count": 6,
    # AGV：数量、速度和交接固定耗时。
    "agv_speed_mps": 1.0,
    "agv_transfer_seconds": 20.0,
    "lift_count": 2,
    # 提升机：负责不同楼层或高度之间的托盘接驳。
    "lift_vertical_mps": 0.8,
    "lift_service_seconds": 12.0,
    "lift_handoff_seconds": 8.0,
    "sorter_count": 2,
    # 自动分拣线：每条线处理一个托盘的作业时间。
    "sorter_seconds_per_piece": 3.0,
    "sorter_handoff_seconds": 5.0,
    "worker_teams": 4,
    # 低温人员：连续作业上限、强制休息时长和单个任务人工耗时。
    "cold_work_limit_min": 120.0,
    "cold_break_min": 20.0,
    "worker_task_seconds": 90.0,
    "dock_service_min_per_piece": 1.8,
    # 月台和预约：备货缓冲、到达偏差样本、容忍范围和等待目标。
    "appointment_prepare_buffer_min": 15.0,
    "appointment_deviation_pattern_min": [-12.0, -5.0, 0.0, 8.0, 18.0, 30.0],
    "appointment_compliance_tolerance_min": 15.0,
    "dock_wait_target_min": 30.0,
    "dock_eta_correction_min": 10.0,
    "picking_seconds_per_piece": 12.0,
}

# ------------------------------------------------------------------ layout
LAYOUT = {
    "aisles": 2,
    "racks_per_aisle": 2,
    "bays": 30,
    "levels": 10,
    "depths_per_position": 2,
    "bay_width_m": 1.2,
    "position_depth_m": 1.2,
    "aisle_width_m": 1.8,
    "level_height_m": 1.5,
    "rack_gap_m": 3.0,
    "p_and_d_x_m": 0.0,
    "p_and_d_y_m": 0.0,
    "dock_x_m": 90.0,
    "dock_y_m": 0.0,
}

DAILY_PATTERN = {
    "ginger": [20, 20, 20, 20, 20, 25, 25],
    "garlic": [10, 10, 10, 10],
}

# Initial inventory days before the simulation horizon.
INITIAL_INVENTORY_DAYS = 10
HORIZON_DAYS = 3
WORK_START_MIN = 6 * 60
WORK_END_MIN = 18 * 60


def hhmm(minutes):
    """把“从当天 0 点起的分钟数”格式化为 HH:MM。"""
    minutes = int(round(minutes))
    return "%02d:%02d" % ((minutes // 60) % 24, minutes % 60)


def required_slots_for(ginger_days, garlic_days, peak_factor, buffer_ratio):
    """在不修改全局参数的前提下，计算一组敏感性情景所需货位数。"""
    # 计算加权平均在库天数：日销量越大的品类对平均库存影响越大。
    ginger_daily = CASE_FACTS["historical_ginger_daily_pieces"]
    garlic_daily = CASE_FACTS["historical_garlic_daily_pieces"]
    total_daily = ginger_daily + garlic_daily
    weighted_dwell = (
        ginger_daily * ginger_days + garlic_daily * garlic_days
    ) / total_daily
    base = total_daily * weighted_dwell
    # 基础库存之后，再叠加需求峰值和运营缓冲。
    peak = base * peak_factor
    required = peak * (1.0 + buffer_ratio)
    return {
        "daily_pieces": total_daily,
        "weighted_dwell_days": weighted_dwell,
        "base_slots": base,
        "peak_slots": peak,
        "required_slots": int(math.ceil(required)),
    }


def required_slots():
    """按表 2-7 日均量和显式假设计算基准峰值货位需求。"""
    return required_slots_for(
        ASSUMPTIONS["average_ginger_dwell_days"],
        ASSUMPTIONS["average_garlic_dwell_days"],
        ASSUMPTIONS["peak_factor"],
        ASSUMPTIONS["buffer_ratio"],
    )


def layout_slots():
    """返回试点货架布局能提供的总货位数。"""
    # 总货位数 = 巷道数 × 货架数 × 列数 × 层数 × 深度数。
    return (
        LAYOUT["aisles"]
        * LAYOUT["racks_per_aisle"]
        * LAYOUT["bays"]
        * LAYOUT["levels"]
        * LAYOUT["depths_per_position"]
    )


def pilot_area_m2():
    """由货位数量和单位面积余量估算试点占地。"""
    slots = layout_slots()
    return int(math.ceil(slots * ASSUMPTIONS["pilot_slot_area_allowance_m2"]))


def data_quality_checks():
    """返回案例事实与计划假设之间的显式一致性检查。"""
    # 每个检查都返回 (名称, 真假)，命令行和 summary.json 共用。
    checks = []
    checks.append((
        "daily demand matches Table 2-7",
        CASE_FACTS["historical_ginger_daily_pieces"]
        + CASE_FACTS["historical_garlic_daily_pieces"] == 190,
    ))
    checks.append((
        "pilot layout can cover required peak slots",
        layout_slots() >= required_slots()["required_slots"],
    ))
    checks.append((
        "pilot uses less than 15% of the existing stereoscopic slots",
        layout_slots() <= 0.15 * CASE_FACTS["stereoscopic_slots"],
    ))
    checks.append((
        "all dwell and equipment assumptions are positive",
        all(
            (isinstance(value, (int, float)) and not isinstance(value, bool)
             and value > 0)
            or (isinstance(value, (list, tuple)) and len(value) > 0)
            for key, value in ASSUMPTIONS.items()
            if key != "buffer_ratio"
        ),
    ))
    checks.append((
        "cold-work limit exceeds one task and break is positive",
        ASSUMPTIONS["cold_work_limit_min"] * 60.0
        > ASSUMPTIONS["worker_task_seconds"]
        and ASSUMPTIONS["cold_break_min"] > 0,
    ))
    checks.append((
        "appointment deviation samples are finite",
        all(math.isfinite(value)
            for value in ASSUMPTIONS["appointment_deviation_pattern_min"]),
    ))
    return checks
