# -*- coding: utf-8 -*-
"""Sensitivity analysis for the case 2.4 warehouse pilot.

The script changes one planning assumption at a time, records the effect on
slot sizing or operational KPIs, and writes both CSV and a report-ready PNG.
No case fact is changed.
"""
# 本文件做“单因素敏感性分析”：每次只改一个假设，观察结果是否变化。
# 案例事实保持不变，因此可以回答“如果实际在库时间更长，方案还够不够用”。
import csv
import os

from PIL import Image, ImageDraw, ImageFont

import data
import model
import run as warehouse_run

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "output")
os.makedirs(OUT, exist_ok=True)

# 字体和颜色只影响图表可读性，不影响数值结果。
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
]
INK = (15, 23, 42)
MUTED = (100, 116, 139)
LINE = (226, 232, 240)
COLORS = {
    "sizing": (217, 119, 6),
    "agv": (37, 99, 235),
    "dock": (22, 163, 74),
}


def font(size):
    """按优先级加载中文字体，找不到时回退到 PIL 默认字体。"""
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


def sizing_rows():
    """分析在库时间、峰值系数和安全缓冲变化对货位需求的影响。"""
    scenarios = [
        # 标签, 姜在库天数, 蒜在库天数, 峰值系数, 安全缓冲比例。
        ("基准", 7.0, 14.0, 1.15, 0.10),
        ("短在库", 5.0, 10.0, 1.15, 0.10),
        ("长在库", 9.0, 18.0, 1.15, 0.10),
        ("低峰", 7.0, 14.0, 1.00, 0.10),
        ("高峰", 7.0, 14.0, 1.30, 0.10),
    ]
    rows = []
    for label, ginger_days, garlic_days, peak, buffer in scenarios:
        result = data.required_slots_for(
            ginger_days, garlic_days, peak, buffer)
        rows.append({
            "experiment": "slot_sizing",
            "scenario": label,
            "parameter_value": "%g/%g/%.2f" % (ginger_days, garlic_days, peak),
            "required_slots": result["required_slots"],
            "layout_slots": data.layout_slots(),
            "pilot_area_m2": data.pilot_area_m2(),
            "makespan_min": "",
            "agv_util_pct": "",
            "dock_wait_min": "",
            "late_vehicles": "",
        })
    return rows


def equipment_rows(slots, batches, orders):
    """逐个改变 AGV 数量，观察设备作业跨度和 AGV 利用率。"""
    baseline = data.ASSUMPTIONS["agv_count"]
    rows = []
    for count in (4, 6, 8):
        data.ASSUMPTIONS["agv_count"] = count
        try:
            result = warehouse_run.strategy_result(
                "data_driven", slots, batches, orders)
        finally:
            # 无论本次运行是否报错，都恢复基准参数，避免污染后续实验。
            data.ASSUMPTIONS["agv_count"] = baseline
        rows.append({
            "experiment": "agv_count",
            "scenario": "%d 台 AGV" % count,
            "parameter_value": count,
            "required_slots": data.required_slots()["required_slots"],
            "layout_slots": data.layout_slots(),
            "pilot_area_m2": data.pilot_area_m2(),
            "makespan_min": result["equipment"]["makespan_min"],
            "agv_util_pct": result["equipment"]["agv_utilisation_pct"],
            "dock_wait_min": result["docks"]["average_wait_min"],
            "late_vehicles": result["docks"]["late_vehicles"],
        })
    return rows


def dock_rows(slots, batches, orders):
    """改变月台单件服务时间，识别装车能力是否是瓶颈。"""
    baseline = data.ASSUMPTIONS["dock_service_min_per_piece"]
    rows = []
    for service in (1.2, 1.8, 2.4):
        data.ASSUMPTIONS["dock_service_min_per_piece"] = service
        try:
            result = warehouse_run.strategy_result(
                "data_driven", slots, batches, orders)
        finally:
            data.ASSUMPTIONS["dock_service_min_per_piece"] = baseline
        detail = result["docks"]["detail"]
        horizon = (
            max(row["end_min"] for row in detail)
            - min(row["arrival_min"] for row in detail)
        )
        rows.append({
            "experiment": "dock_service",
            "scenario": "%.1f 分钟/件" % service,
            "parameter_value": service,
            "required_slots": data.required_slots()["required_slots"],
            "layout_slots": data.layout_slots(),
            "pilot_area_m2": data.pilot_area_m2(),
            "makespan_min": round(horizon, 2),
            "agv_util_pct": "",
            "dock_wait_min": result["docks"]["average_wait_min"],
            "late_vehicles": result["docks"]["late_vehicles"],
        })
    return rows


def write_csv(rows):
    """把全部敏感性实验写成统一 CSV，供报告或 Excel 复核。"""
    path = os.path.join(OUT, "sensitivity.csv")
    fields = [
        "experiment", "scenario", "parameter_value", "required_slots",
        "layout_slots", "pilot_area_m2", "makespan_min", "agv_util_pct",
        "dock_wait_min", "late_vehicles",
    ]
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return path


def draw_panel(draw, x0, y0, width, height, title, rows, value_key, color):
    """在指定区域内绘制一个柱状图面板。"""
    draw.text((x0, y0), title, font=font(16), fill=INK)
    plot_y = y0 + 34
    plot_h = height - 52
    values = [float(row[value_key]) for row in rows]
    maximum = max(values) if values else 1.0
    maximum = max(maximum, 1.0)
    # 柱高按当前面板最大值归一化，让不同单位的图可以共用一个画布。
    bar_gap = 14
    bar_w = (width - bar_gap * (len(rows) + 1)) / len(rows)
    for index, row in enumerate(rows):
        value = float(row[value_key])
        bar_h = plot_h * value / maximum
        x = x0 + bar_gap + index * (bar_w + bar_gap)
        y = plot_y + plot_h - bar_h
        draw.rectangle([x, y, x + bar_w, plot_y + plot_h], fill=color)
        draw.text((x, y - 20), "%.1f" % value, font=font(11), fill=INK)
        label = row["scenario"]
        draw.text((x, plot_y + plot_h + 8), label, font=font(10), fill=MUTED)
    draw.rectangle(
        [x0, plot_y, x0 + width, plot_y + plot_h], outline=LINE, width=1)


def render(rows):
    """将三组敏感性结果绘制成 sensitivity.png。"""
    width, height = 1040, 680
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    draw.text((24, 18), "2.4 仓储方案敏感性分析", font=font(24), fill=INK)
    draw.text(
        (24, 52),
        "单因素变化；货位需求来自表 2-7 日均量，设备与月台指标来自数据驱动策略",
        font=font(13), fill=MUTED)

    # 按 experiment 字段拆分三类实验，分别画到三个面板。
    sizing = [row for row in rows if row["experiment"] == "slot_sizing"]
    agv = [row for row in rows if row["experiment"] == "agv_count"]
    docks = [row for row in rows if row["experiment"] == "dock_service"]

    draw_panel(draw, 24, 92, 480, 250, "峰值货位需求", sizing,
               "required_slots", COLORS["sizing"])
    draw_panel(draw, 536, 92, 480, 250, "AGV 数量与设备作业跨度", agv,
               "makespan_min", COLORS["agv"])
    draw_panel(draw, 24, 370, 480, 250, "月台服务时间与作业跨度", docks,
               "makespan_min", COLORS["dock"])

    draw.text((536, 370), "结论与使用边界", font=font(18), fill=INK)
    notes = [
        "1. 基准情景需要 2,037 个货位，2,400 货位有余量。",
        "2. 生姜 9 天、大蒜 18 天时，需求超过 2,400 货位，",
        "   说明当前布局对长在库时间不稳健。",
        "3. 峰值系数 1.30 时仍在 2,400 货位以内。",
        "4. AGV 数量变化应结合设备投资，不应只追求作业跨度最小。",
        "5. 月台服务时间用于识别人员和装车能力瓶颈。",
        "6. 所有变化均是单因素实验，不能直接叠加成最坏情景。",
    ]
    for index, note in enumerate(notes):
        draw.text((536, 410 + index * 27), note, font=font(13), fill=MUTED)
    path = os.path.join(OUT, "sensitivity.png")
    img.save(path)
    return path


def main():
    """运行货位、设备和月台三类单因素实验，并输出 CSV 与 PNG。"""
    slots = model.build_slots()
    batches = model.generate_batches()
    orders = model.generate_orders()
    rows = sizing_rows()
    rows.extend(equipment_rows(slots, batches, orders))
    rows.extend(dock_rows(slots, batches, orders))
    csv_path = write_csv(rows)
    png_path = render(rows)
    print("sensitivity ->", csv_path)
    print("sensitivity ->", png_path)
    for row in rows:
        print("%-12s %-14s slots=%s makespan=%s" % (
            row["experiment"], row["scenario"], row["required_slots"],
            row["makespan_min"]))
    return rows


if __name__ == "__main__":
    main()
