# -*- coding: utf-8 -*-
"""Generate a self-contained HTML dashboard (no external libraries).

Run:  python dashboard.py     ->  output/dashboard.html
Shows: KPI cards, route maps for both product families, cost-composition bars
and the full route table.
"""
import os
import math

from core import VEHICLES
from fleet import evaluate, plan_metrics
from run import main as run_main, NAMES, fmt_time

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "output")

PALETTE = ["#2563eb", "#dc2626", "#16a34a", "#d97706", "#7c3aed",
           "#0891b2", "#db2777", "#65a30d", "#0d9488", "#c026d3",
           "#ea580c", "#4f46e5"]

VEH_SHORT = {"small": "小型4.2m", "medium": "中型7.6m"}

COMP_COLORS = {"fixed": "#1e3a8a", "transport": "#2563eb", "refrig": "#06b6d4",
               "penalty": "#f59e0b", "loss": "#ef4444"}
COMP_NAMES = {"fixed": "固定成本", "transport": "运输成本", "refrig": "制冷成本",
              "penalty": "时间窗惩罚", "loss": "货损成本"}


def project(points, width=560, height=430, pad=32):
    lons = [p[0] for p in points]
    lats = [p[1] for p in points]
    lon0, lon1 = min(lons), max(lons)
    lat0, lat1 = min(lats), max(lats)
    kx = math.cos(math.radians((lat0 + lat1) / 2))
    spanx = max((lon1 - lon0) * kx, 1e-6)
    spany = max(lat1 - lat0, 1e-6)
    scale = min((width - 2 * pad) / spanx, (height - 2 * pad) / spany)
    ox = (width - spanx * scale) / 2
    oy = (height - spany * scale) / 2

    def f(lon, lat):
        return ox + (lon - lon0) * kx * scale, oy + (lat1 - lat) * scale
    return f


def route_svg(prob, sol, title):
    routes = [(vt, r) for key, vt, r in sol if key == prob.key]
    pts = [(prob.lon[c], prob.lat[c]) for c in range(prob.n)] + [(104.26, 30.85)]
    proj = project(pts)
    parts = ['<svg viewBox="0 0 560 430" class="map">']
    for i, (vt, r) in enumerate(routes):
        col = PALETTE[i % len(PALETTE)]
        coords = []
        for s in [0] + [c + 1 for c in r] + [0]:
            xy = proj(104.26, 30.85) if s == 0 else proj(prob.lon[s - 1], prob.lat[s - 1])
            coords.append("%.1f,%.1f" % xy)
        parts.append('<polyline points="%s" fill="none" stroke="%s" stroke-width="1.8" '
                     'opacity="0.75"/>' % (" ".join(coords), col))
        for c in r:
            x, y = proj(prob.lon[c], prob.lat[c])
            parts.append('<circle cx="%.1f" cy="%.1f" r="3.2" fill="%s"/>' % (x, y, col))
    dx, dy = proj(104.26, 30.85)
    parts.append('<rect x="%.1f" y="%.1f" width="11" height="11" fill="#111827" '
                 'stroke="#fff" stroke-width="1.5"/>' % (dx - 5.5, dy - 5.5))
    parts.append('<text x="%.1f" y="%.1f" class="maplabel">配送中心</text>' % (dx + 9, dy + 4))
    parts.append("</svg>")
    legend = "".join(
        '<span class="lg"><i style="background:%s"></i>R%d %s (%d点)</span>'
        % (PALETTE[i % len(PALETTE)], i + 1, VEH_SHORT[vt], len(r))
        for i, (vt, r) in enumerate(routes))
    return ('<div class="card"><h3>%s</h3>%s<div class="legend">%s</div></div>'
            % (title, "".join(parts), legend))


def cost_bars(problems, base, opt):
    comps = ["fixed", "transport", "refrig", "penalty", "loss"]
    tot = {"baseline": 0.0, "optimised": 0.0}
    brk = {"baseline": {c: 0.0 for c in comps},
           "optimised": {c: 0.0 for c in comps}}
    for tag, sol in (("baseline", base), ("optimised", opt)):
        for key, vt, r in sol:
            rc = problems[key].route_cost(r, vt)
            for c in comps:
                brk[tag][c] += rc[c]
            tot[tag] += rc["total"]
    grand = max(tot.values())
    rows = []
    for tag, label in (("baseline", "人工排程"), ("optimised", "智能调度")):
        segs = "".join(
            '<span class="seg" style="width:%.3f%%;background:%s"></span>'
            % (brk[tag][c] / grand * 100, COMP_COLORS[c]) for c in comps)
        rows.append('<div class="barrow"><span class="barlab">%s</span>'
                    '<span class="barcap" style="width:%.2f%%">%s</span>'
                    '<b>%.0f 元</b></div>' % (label, tot[tag] / grand * 100, segs, tot[tag]))
    legend = "".join(
        '<span class="lg"><i style="background:%s"></i>%s %.0f</span>'
        % (COMP_COLORS[c], COMP_NAMES[c], brk["optimised"][c]) for c in comps)
    return ('<div class="card"><h3>成本结构对比</h3>%s<div class="legend">%s</div></div>'
            % ("".join(rows), legend))


def route_table(prob_map, base, opt):
    rows = []
    for tag, sol in (("人工排程", base), ("智能调度", opt)):
        for key, vt, r in sol:
            p = prob_map[key]
            rc = p.route_cost(r, vt)
            rows.append(
                "<tr><td>%s</td><td>%s</td><td>%s</td><td>%d</td>"
                "<td>%s</td><td>%.0f</td><td>%.1f</td><td>%s</td><td>%.0f</td></tr>"
                % (tag, NAMES[p.kind], VEHICLES[vt]["name"], len(r),
                   " ".join(str(p.ids[c]) for c in r),
                   rc["load_kg"], rc["distance_km"], fmt_time(rc["depart"]), rc["total"]))
    return ("<table><thead><tr><th>方案</th><th>品类</th><th>车型</th><th>点数</th>"
            "<th>客户顺序</th><th>载重kg</th><th>里程km</th><th>发车</th>"
            "<th>成本元</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>")


TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>银犁冷链城配智能调度看板</title>
<style>
:root{--ink:#0f172a;--muted:#64748b;--line:#e2e8f0;--bg:#f8fafc}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
background:var(--bg);color:var(--ink);padding:24px}
h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--muted);font-size:13px;margin-bottom:18px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:16px}
.kpi{background:#fff;border:1px solid var(--line);border-radius:8px;padding:14px 16px}
.kpi span{color:var(--muted);font-size:12px}
.kpi b{display:block;font-size:24px;margin:6px 0 2px}
.kpi em{color:var(--muted);font-style:normal;font-size:12px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.card{background:#fff;border:1px solid var(--line);border-radius:8px;padding:16px}
.card h3{margin:0 0 12px;font-size:15px}
.map{width:100%%;height:auto;background:#fbfdff;border:1px solid var(--line);border-radius:6px}
.maplabel{font-size:10px;fill:#111827}
.legend{display:flex;flex-wrap:wrap;gap:10px;margin-top:10px;font-size:12px;color:var(--muted)}
.lg i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:middle}
.barrow{display:flex;align-items:center;gap:10px;margin:10px 0}
.barlab{width:64px;font-size:12px;color:var(--muted)}
.barcap{height:22px;border-radius:4px;overflow:hidden;background:#eef2f7;display:flex}
.seg{display:block;height:100%%}
.barrow b{min-width:72px;text-align:right;font-size:13px}
table{width:100%%;border-collapse:collapse;margin-top:16px;background:#fff;font-size:12px}
th,td{border:1px solid var(--line);padding:6px 8px;text-align:left}
th{background:#f1f5f9}
@media(max-width:860px){.grid{grid-template-columns:1fr}}
</style></head><body>
<h1>银犁冷链城配 · 双品类差异化智能调度看板</h1>
<div class="sub">案例 2.3 数据建模 · VRPTW + 车队约束 · 人工排程 vs 智能调度（数据为案例抽样 30+30 客户点）</div>
<div class="kpis">%s</div>
<div class="grid">
%s
%s
</div>
%s
<div class="card" style="margin-top:16px"><h3>线路明细</h3>%s</div>
</body></html>"""


def build_html(problems, base, opt):
    eb, eo = evaluate(base), evaluate(opt)
    mb, mo = plan_metrics(base), plan_metrics(opt)
    saving = eb[0] - eo[0]
    pct = saving / eb[0] * 100
    kpi = [("配送总成本", "%.0f 元" % eo[0], "人工排程 %.0f 元" % eb[0]),
           ("成本节省", "%.0f 元" % saving, "下降 %.1f%%" % pct),
           ("使用车队", "%d 小 + %d 中" % (eo[1], eo[2]), "可用上限 8 小 + 2 中"),
           ("路线数", "%d 条" % len(opt), "人工排程 %d 条" % len(base)),
           ("平均装载率", "%.1f%%" % mo["load_util_weight_pct"],
            "人工排程 %.1f%% · 体积口径 %.1f%%"
            % (mb["load_util_weight_pct"], mo["load_util_volume_pct"])),
           ("准时送达率", "%.1f%%" % mo["on_time_pref_pct"],
            "人工排程 %.1f%% · 硬时间窗达成 %.1f%%"
            % (mb["on_time_pref_pct"], mo["on_time_acc_pct"]))]
    kpi_html = "".join('<div class="kpi"><span>%s</span><b>%s</b><em>%s</em></div>' % k
                       for k in kpi)
    return TEMPLATE % (
        kpi_html,
        route_svg(problems["jiangsuan"], opt, "路线图 · 姜蒜（高温冷藏，可同车）"),
        route_svg(problems["miantiao"], opt, "路线图 · 鲜面条（0-4C，禁止与姜蒜混装）"),
        cost_bars(problems, base, opt),
        route_table(problems, base, opt),
    )


def main():
    problems, base, opt = run_main()
    html = build_html(problems, base, opt)
    path = os.path.join(OUT, "dashboard.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print("dashboard ->", path)


if __name__ == "__main__":
    main()
