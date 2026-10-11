# -*- coding: utf-8 -*-
"""Render report-ready PNG figures for the case 2.4 warehouse model."""
# 本文件把模型结果绘制成报告可直接插入的 PNG 图。
# 它不参与业务计算，只负责选颜色、画布局和绘制 KPI 对比。
import os

from PIL import Image, ImageDraw, ImageFont

import model
import run as warehouse_run

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "output")

# 中文字体按优先级自动寻找，避免不同电脑字体路径不同导致英文乱码。
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
]
INK = (15, 23, 42)
MUTED = (100, 116, 139)
LINE = (226, 232, 240)
PALETTE = {"ginger": (217, 119, 6), "garlic": (22, 163, 74)}
STRATEGY_COLOR = {"experience": (217, 119, 6), "data_driven": (37, 99, 235)}


def font(size):
    """返回可用中文字体；如果系统没有候选字体就退回 PIL 默认字体。"""
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


def draw_layout(img, draw, results):
    """在画布上并排绘制经验式与数据驱动策略的货位占用分布。"""
    draw.text((24, 16), "姜蒜试点货位占用对比", font=font(24), fill=INK)
    draw.text((24, 48), "左：经验式随机分配    右：数据驱动分区分配",
              font=font(14), fill=MUTED)
    panel_w, panel_h = 440, 330
    for panel, result in enumerate(results):
        x0 = 24 + panel * 500
        y0 = 88
        draw.rectangle([x0, y0, x0 + panel_w, y0 + panel_h],
                       outline=LINE, width=1)
        batch_map = {b.batch_id: b for b in model.generate_batches()}
        for batch_id, slots in result["allocation"].items():
            product = batch_map[batch_id].product
            for slot in slots:
                # 横坐标表示货位 bay，纵坐标表示货架和层级。
                row = (slot.rack - 1) * 10 + (slot.level - 1)
                px = x0 + 8 + (slot.bay - 1) * 13
                py = y0 + 12 + row * 7
                draw.rectangle(
                    [px, py, px + 8, py + 4],
                    fill=PALETTE[product],
                )
        draw.text((x0 + 10, y0 + panel_h - 45), "橙色：生姜", font=font(13), fill=MUTED)
        draw.text((x0 + 110, y0 + panel_h - 45), "绿色：大蒜", font=font(13), fill=MUTED)
        occupied = sum(len(v) for v in result["allocation"].values())
        draw.text((x0 + 10, y0 + panel_h - 25),
                  "批次数：%d    占位件数：%d" % (
                      len(result["allocation"]), occupied),
                  font=font(13), fill=MUTED)
    return img


def draw_kpis(img, draw, summary):
    """绘制四个核心 KPI 的双策略条形对比。"""
    exp = summary["strategies"]["experience"]
    opt = summary["strategies"]["data_driven"]
    draw.text((24, 450), "运营 KPI 对比", font=font(22), fill=INK)
    items = [
        # label, 经验式数值, 数据驱动数值, 用于做长度归一化的上限。
        ("FIFO合规率(%)", exp["picking"]["fifo_compliance_pct"],
         opt["picking"]["fifo_compliance_pct"], 100.0),
        ("平均拣选时间(秒)", exp["picking"]["average_retrieval_seconds"],
         opt["picking"]["average_retrieval_seconds"],
         max(exp["picking"]["average_retrieval_seconds"],
             opt["picking"]["average_retrieval_seconds"])),
        ("平均月台等待(分)", exp["docks"]["average_wait_min"],
         opt["docks"]["average_wait_min"],
         max(exp["docks"]["average_wait_min"], opt["docks"]["average_wait_min"])),
        ("设备作业跨度(分)", exp["equipment"]["makespan_min"],
         opt["equipment"]["makespan_min"],
         max(exp["equipment"]["makespan_min"], opt["equipment"]["makespan_min"])),
    ]
    x0, y0 = 36, 500
    for index, (label, first, second, scale) in enumerate(items):
        x = x0 + index * 235
        draw.text((x, y0), label, font=font(13), fill=MUTED)
        for bar, value, color, name in (
            (0, first, STRATEGY_COLOR["experience"], "经验式"),
            (1, second, STRATEGY_COLOR["data_driven"], "数据驱动"),
        ):
            width = 180 * value / scale if scale else 0
            y = y0 + 24 + bar * 30
            draw.rectangle([x, y, x + 180, y + 18], fill=(241, 245, 249))
            draw.rectangle([x, y, x + width, y + 18], fill=color)
            draw.text((x + 188, y + 1), "%.1f" % value, font=font(12), fill=INK)
        draw.text((x, y0 + 88), "橙：经验式  蓝：数据驱动",
                  font=font(11), fill=MUTED)
    return img


def main():
    """运行模型并输出 warehouse_layout.png。"""
    slots = model.build_slots()
    batches = model.generate_batches()
    orders = model.generate_orders()
    results = [
        warehouse_run.strategy_result("experience", slots, batches, orders),
        warehouse_run.strategy_result("data_driven", slots, batches, orders),
    ]
    summary = warehouse_run.main()
    img = Image.new("RGB", (1000, 640), "white")
    draw = ImageDraw.Draw(img)
    draw_layout(img, draw, results)
    draw_kpis(img, draw, summary)
    path = os.path.join(OUT, "warehouse_layout.png")
    img.save(path)
    print("figure ->", path)


if __name__ == "__main__":
    main()
