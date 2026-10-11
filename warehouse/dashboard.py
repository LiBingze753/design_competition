# -*- coding: utf-8 -*-
"""Generate a self-contained HTML dashboard for the case 2.4 warehouse pilot."""
# 本文件只负责“把模型结果变成图表和网页”，不再重新定义业务算法。
# 生成的 dashboard.html 可以离线打开，适合答辩现场演示。
import html
import os

import data
import model
import run as warehouse_run

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "output")

# 颜色只服务图表区分：橙色代表经验式，蓝色代表数据驱动。
COLORS = {
    "experience": "#d97706",
    "data_driven": "#2563eb",
    "fast": "#16a34a",
    "medium": "#2563eb",
    "slow": "#94a3b8",
}


def fmt(value, digits=1):
    """把数字格式化成固定小数位，避免图中的标签长度跳动。"""
    return ("%." + str(digits) + "f") % value


def zone_map(slots):
    """把 2,400 个货位画成 SVG 分区图，供看板快速检查分布。"""
    cell_w, cell_h = 18, 13
    width, height = 30 * cell_w + 30, 20 * cell_h + 30
    parts = ['<svg viewBox="0 0 %d %d" class="map">' % (width, height)]
    parts.append(
        '<text x="8" y="16" class="small">货位分区：绿色快取，蓝色常规，灰色慢取</text>')
    for slot in slots:
        # The two racks are stacked vertically to keep the figure readable.
        # 把两个货架上下堆叠显示，避免横向图形过宽。
        col = slot.bay - 1
        row = (slot.rack - 1) * 10 + (slot.level - 1)
        x = 8 + col * cell_w
        y = 22 + row * cell_h
        parts.append(
            '<rect x="%d" y="%d" width="%d" height="%d" rx="2" fill="%s" '
            'opacity="0.75"><title>%s %s %.1fs</title></rect>'
            % (x, y, cell_w - 3, cell_h - 3, COLORS[slot.zone],
               slot.slot_id, slot.zone, slot.travel_seconds))
    parts.append("</svg>")
    return "".join(parts)


def gantt(results, task_limit=58):
    """画前 58 个托盘任务的甘特图，展示设备链是否前后衔接。"""
    width, row_h = 760, 8
    height = task_limit * row_h + 34
    parts = ['<svg viewBox="0 0 %d %d" class="map">' % (width, height)]
    parts.append('<text x="8" y="15" class="small">设备链任务甘特图（前 58 个托盘任务）</text>')
    for result in results:
        color = COLORS[result["strategy"]]
        rows = result["equipment"]["detail"][:task_limit]
        for index, row in enumerate(rows):
            # 一根任务条从堆垛机开始，一直画到 AGV 或分拣线结束。
            start = row["stacker_start"]
            end = row["agv_end"]
            end = row["sorter_end"] if row["sorter_id"] else end
            x = 80 + (start - 360) * 0.55
            w = max(2.0, (end - start) * 0.55)
            y = 24 + index * row_h
            parts.append(
                '<rect x="%.1f" y="%d" width="%.1f" height="5" rx="2" fill="%s" '
                'opacity="0.82"><title>%s %s-%s</title></rect>'
                % (x, y, w, color, row["task_id"], fmt(start, 1), fmt(end, 1)))
    parts.append("</svg>")
    return "".join(parts)


def wait_chart(summary):
    """用水平柱状图对比经验式和数据驱动策略的平均月台等待。"""
    width, height = 760, 180
    parts = ['<svg viewBox="0 0 %d %d" class="map">' % (width, height)]
    strategies = ["experience", "data_driven"]
    max_wait = max(
        summary["strategies"][s]["docks"]["average_wait_min"] for s in strategies)
    max_wait = max(max_wait, 1.0)
    # 以较大值作为统一横轴，保证两根柱子的长度可以直接比较。
    for index, strategy in enumerate(strategies):
        value = summary["strategies"][strategy]["docks"]["average_wait_min"]
        y = 38 + index * 58
        w = 580 * value / max_wait
        parts.append('<text x="8" y="%d" class="small">%s</text>'
                     % (y + 17, "经验式" if strategy == "experience" else "数据驱动"))
        parts.append('<rect x="90" y="%d" width="580" height="24" rx="4" fill="#eef2f7"/>'
                     % y)
        parts.append('<rect x="90" y="%d" width="%.1f" height="24" rx="4" fill="%s"/>'
                     % (y, w, COLORS[strategy]))
        parts.append('<text x="%d" y="%d" class="small">%.1f 分钟</text>'
                     % (max(96 + w, 100), y + 17, value))
    parts.append("</svg>")
    return "".join(parts)


def strategy_table(summary):
    """生成 2.4.4(1) 核心策略对比表。"""
    rows = []
    labels = {
        "experience": "经验式分配",
        "data_driven": "数据驱动分配",
    }
    for strategy in ("experience", "data_driven"):
        item = summary["strategies"][strategy]
        rows.append(
            "<tr><td>%s</td><td>%.1f%%</td><td>%.2f 秒</td>"
            "<td>%.2f 分钟</td><td>%.1f%%</td><td>%.2f 分钟</td></tr>"
            % (labels[strategy],
               item["picking"]["fifo_compliance_pct"],
               item["picking"]["average_retrieval_seconds"],
               item["docks"]["average_wait_min"],
               item["equipment"]["stacker_utilisation_pct"],
               item["equipment"]["makespan_min"]))
    return ("<table><thead><tr><th>策略</th><th>FIFO 合规率</th>"
            "<th>平均拣选时间</th><th>平均月台等待</th>"
            "<th>堆垛机利用率</th><th>设备作业跨度</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table>")


def topic_table(summary):
    """生成 2.4.4(2) 和 2.4.4(3) 各自的主题结果表。"""
    managed = summary["managed_warehouse"]
    integration = summary["warehouse_delivery_integration"]
    random_metrics = managed["strategies"]["random"]
    optimized_metrics = managed["strategies"]["optimized"]
    manual = integration["modes"]["manual"]
    coordinated = integration["modes"]["integrated"]
    managed_html = (
        "<table><thead><tr><th>策略</th><th>翻箱率</th><th>平均拣选</th>"
        "<th>商家分散度</th></tr></thead><tbody>" 
        "<tr><td>随机</td><td>%.2f%%</td><td>%.2f 秒</td><td>%.2f</td></tr>"
        "<tr><td>优化</td><td>%.2f%%</td><td>%.2f 秒</td><td>%.2f</td></tr>"
        "</tbody></table>" % (
            random_metrics["flip_rate_pct"],
            random_metrics["average_retrieval_seconds"],
            random_metrics["average_merchant_rack_span"],
            optimized_metrics["flip_rate_pct"],
            optimized_metrics["average_retrieval_seconds"],
            optimized_metrics["average_merchant_rack_span"],
        )
    )
    integration_html = (
        "<table><thead><tr><th>模式</th><th>平均等待</th><th>最大等待</th>"
        "<th>ETA误差</th></tr></thead><tbody>" 
        "<tr><td>人工协调</td><td>%.2f 分钟</td><td>%.2f 分钟</td><td>-</td></tr>"
        "<tr><td>预约协同</td><td>%.2f 分钟</td><td>%.2f 分钟</td><td>%.2f 分钟</td></tr>"
        "</tbody></table>" % (
            manual["average_wait_min"], manual["max_wait_min"],
            coordinated["average_wait_min"], coordinated["max_wait_min"],
            coordinated["average_eta_abs_error_min"],
        )
    )
    return managed_html, integration_html


def build_html(summary, results):
    """把 KPI、图形和明细表组装成一个自包含 HTML 文件。"""
    sizing = summary["sizing"]
    exp = summary["strategies"]["experience"]
    opt = summary["strategies"]["data_driven"]
    managed_table, integration_table = topic_table(summary)
    # 每个 KPI 写成“标题、主值、对比说明”，方便口述答辩。
    kpis = [
        ("设计货位", "%d 个" % sizing["layout_slots"],
         "峰值需求 %d 个" % sizing["required_slots"]["required_slots"]),
        ("试点面积", "%d m²" % sizing["pilot_area_m2"], "占现有立体库一部分"),
        ("FIFO 合规", "%.1f%%" % opt["picking"]["fifo_compliance_pct"],
         "经验式 %.1f%%" % exp["picking"]["fifo_compliance_pct"]),
        ("平均拣选时间", "%.2f 秒" % opt["picking"]["average_retrieval_seconds"],
         "经验式 %.2f 秒" % exp["picking"]["average_retrieval_seconds"]),
        ("月台等待", "%.2f 分钟" % opt["docks"]["average_wait_min"],
         "经验式 %.2f 分钟" % exp["docks"]["average_wait_min"]),
        ("设备作业跨度", "%.1f 分钟" % opt["equipment"]["makespan_min"],
         "最后完工 %s；经验式 %s" % (
             data.hhmm(opt["equipment"]["work_end_min"]),
             data.hhmm(exp["equipment"]["work_end_min"]))),
        ("提升机利用率", "%.1f%%" % opt["equipment"]["lift_utilisation_pct"],
         "堆垛机 %.1f%%；AGV %.1f%%" % (
             opt["equipment"]["stacker_utilisation_pct"],
             opt["equipment"]["agv_utilisation_pct"])),
        ("低温连续作业", "%.1f 分钟" % opt["equipment"]["max_continuous_work_min"],
         "强制休息 %d 次" % opt["equipment"]["cold_breaks"]),
        ("代管仓优化翻箱率", "%.2f%%" % summary[
            "managed_warehouse"]["strategies"]["optimized"]["flip_rate_pct"],
         "随机分配 %.2f%%" % summary["managed_warehouse"]
         ["strategies"]["random"]["flip_rate_pct"]),
        ("仓配协同等待", "%.2f 分钟" % summary[
            "warehouse_delivery_integration"]["modes"]["integrated"]
            ["average_wait_min"],
         "人工协调 %.2f 分钟" % summary[
             "warehouse_delivery_integration"]["modes"]["manual"]
             ["average_wait_min"]),
    ]
    kpi_html = "".join(
        '<div class="kpi"><span>%s</span><b>%s</b><em>%s</em></div>'
        % (html.escape(str(a)), html.escape(str(b)), html.escape(str(c)))
        for a, b, c in kpis
    )
    checks = "；".join(
        ("%s：通过" % name) if passed else ("%s：失败" % name)
        for name, passed in [
            (item["name"], item["passed"])
            for item in summary["data_quality_checks"]
        ]
    )
    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>银犁冷链 2.4 智能仓储看板</title>
<style>
:root{--ink:#0f172a;--muted:#64748b;--line:#e2e8f0;--bg:#f8fafc}
*{box-sizing:border-box}body{margin:0;padding:22px;background:var(--bg);color:var(--ink);
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}
h1{font-size:22px;margin:0 0 4px}.sub{font-size:13px;color:var(--muted);margin-bottom:16px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.kpi{background:#fff;border:1px solid var(--line);border-radius:8px;padding:13px 15px}
.kpi span{font-size:12px;color:var(--muted)}.kpi b{display:block;font-size:23px;margin:5px 0 2px}
.kpi em{font-style:normal;font-size:12px;color:var(--muted)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:16px}
.card{background:#fff;border:1px solid var(--line);border-radius:8px;padding:15px}
.card h3{font-size:15px;margin:0 0 10px}.map{width:100%%;height:auto;background:#fbfdff;border:1px solid var(--line);border-radius:6px}
.small{font-size:11px;fill:#475569}.note{font-size:12px;color:var(--muted);line-height:1.7}
table{width:100%%;border-collapse:collapse;margin-top:12px;font-size:12px;background:#fff}
th,td{border:1px solid var(--line);padding:7px 8px;text-align:left}th{background:#f1f5f9}
@media(max-width:860px){.grid{grid-template-columns:1fr}}
</style></head><body>
<h1>银犁冷链 · 2.4 姜蒜智能仓储试点看板</h1>
<div class="sub">2.4.4(1) 设备协同与仓内布局 · 2.4.4(2) 代管仓货位分配 · 2.4.4(3) 仓配预约协同</div>
<div class="kpis">%s</div>
<div class="grid">
<div class="card"><h3>试点货位布局</h3>%s</div>
<div class="card"><h3>设备任务时序</h3>%s</div>
</div>
<div class="grid">
<div class="card"><h3>月台平均等待对比</h3>%s</div>
<div class="card"><h3>数据质量</h3><div class="note">%s<br><br>原文数据来自表 2-7；设备速度、货位布局、AGV数量和月台服务时间在 README 中列为显式假设。</div></div>
</div>
<div class="card" style="margin-top:16px"><h3>策略对比</h3>%s</div>
<div class="grid"><div class="card"><h3>2.4.4(2) 代管仓货位分配</h3>%s</div><div class="card"><h3>2.4.4(3) 仓配一体化协同</h3>%s</div></div>
</body></html>""" % (
        kpi_html,
        zone_map(model.build_slots()),
        gantt(results),
        wait_chart(summary),
        html.escape(checks),
        strategy_table(summary),
        managed_table,
        integration_table,
    )


def main():
    """先运行全部模型，再生成离线可打开的 dashboard.html。"""
    summary = warehouse_run.main()
    slots = model.build_slots()
    batches = model.generate_batches()
    orders = model.generate_orders()
    results = [
        warehouse_run.strategy_result("experience", slots, batches, orders),
        warehouse_run.strategy_result("data_driven", slots, batches, orders),
    ]
    path = os.path.join(OUT, "dashboard.html")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(build_html(summary, results))
    print("dashboard ->", path)


if __name__ == "__main__":
    main()
