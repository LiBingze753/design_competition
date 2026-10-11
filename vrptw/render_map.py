# -*- coding: utf-8 -*-
"""Render report-ready PNG figures with Pillow (no matplotlib needed).

Outputs:
  output/route_map.png     - optimised routes for both product families
  output/cost_compare.png  - baseline vs optimised cost composition
Run:  python render_map.py
"""
import os
import math

from PIL import Image, ImageDraw, ImageFont

from fleet import evaluate
from run import main as run_main, NAMES

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "output")

PALETTE = [(37, 99, 235), (220, 38, 38), (22, 163, 74), (217, 119, 6),
           (124, 58, 237), (8, 145, 178), (219, 39, 119), (101, 163, 13),
           (13, 148, 136), (192, 38, 211)]
VEH_SHORT = {"small": "小型4.2m", "medium": "中型7.6m"}
INK = (15, 23, 42)
MUTED = (100, 116, 139)
LINE = (226, 232, 240)

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
]


def font(size):
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


def projector(points, box):
    x0, y0, x1, y1 = box
    lons = [p[0] for p in points]
    lats = [p[1] for p in points]
    lon0, lon1 = min(lons), max(lons)
    lat0, lat1 = min(lats), max(lats)
    kx = math.cos(math.radians((lat0 + lat1) / 2))
    spanx = max((lon1 - lon0) * kx, 1e-6)
    spany = max(lat1 - lat0, 1e-6)
    pad = 28
    scale = min((x1 - x0 - 2 * pad) / spanx, (y1 - y0 - 2 * pad) / spany)
    ox = x0 + ((x1 - x0) - spanx * scale) / 2
    oy = y0 + ((y1 - y0) - spany * scale) / 2

    def f(lon, lat):
        return ox + (lon - lon0) * kx * scale, oy + (lat1 - lat) * scale
    return f


def draw_panel(d, prob, sol, box, title):
    x0, y0, x1, y1 = box
    d.rectangle(box, outline=LINE, width=1)
    d.text((x0 + 16, y0 + 12), title, font=font(20), fill=INK)
    pts = [(prob.lon[c], prob.lat[c]) for c in range(prob.n)] + [(104.26, 30.85)]
    proj = projector(pts, (x0, y0 + 44, x1, y1))
    routes = [(vt, r) for key, vt, r in sol if key == prob.key]
    for i, (vt, r) in enumerate(routes):
        col = PALETTE[i % len(PALETTE)]
        seq = [None] + list(r) + [None]
        xy = []
        for s in seq:
            if s is None:
                xy.append(proj(104.26, 30.85))
            else:
                xy.append(proj(prob.lon[s], prob.lat[s]))
        d.line(xy, fill=col, width=2)
        for s in r:
            px, py = proj(prob.lon[s], prob.lat[s])
            d.ellipse([px - 3.5, py - 3.5, px + 3.5, py + 3.5], fill=col)
    dx, dy = proj(104.26, 30.85)
    d.rectangle([dx - 6, dy - 6, dx + 6, dy + 6], fill=(17, 24, 39), outline=(255, 255, 255))
    # legend
    ly = y1 - 24 * len(routes) - 8
    for i, (vt, r) in enumerate(routes):
        col = PALETTE[i % len(PALETTE)]
        d.rectangle([x0 + 16, ly + i * 24, x0 + 28, ly + i * 24 + 12], fill=col)
        rc = prob.route_cost(r, vt)
        d.text((x0 + 36, ly + i * 24 - 3),
               "R%d  %s  %d点  %.0f元" % (i + 1, VEH_SHORT[vt], len(r),
                                         rc["total"] if rc else 0.0),
               font=font(14), fill=MUTED)


def route_map(problems, opt):
    W, H = 1240, 700
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    d.text((24, 16), "银犁冷链城配 · 智能调度路线图", font=font(26), fill=INK)
    half = (W - 48) // 2
    draw_panel(d, problems["jiangsuan"], opt,
               (24, 60, 24 + half, H - 20), "姜蒜（高温冷藏，可同车）")
    draw_panel(d, problems["miantiao"], opt,
               (W - 24 - half, 60, W - 24, H - 20), "鲜面条（0-4C，禁止与姜蒜混装）")
    img.save(os.path.join(OUT, "route_map.png"))


def cost_compare(problems, base, opt):
    comps = [("fixed", "固定成本", (30, 58, 138)),
             ("transport", "运输成本", (37, 99, 235)),
             ("refrig", "制冷成本", (6, 182, 212)),
             ("penalty", "时间窗惩罚", (245, 158, 11)),
             ("loss", "货损成本", (239, 68, 68))]
    brk = {}
    tot = {}
    for tag, sol in (("baseline", base), ("optimised", opt)):
        brk[tag] = {c: 0.0 for c, _, _ in comps}
        tot[tag] = 0.0
        for key, vt, r in sol:
            rc = problems[key].route_cost(r, vt)
            for c, _, _ in comps:
                brk[tag][c] += rc[c]
            tot[tag] += rc["total"]
    W, H = 1000, 460
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    d.text((28, 18), "成本结构对比（人工排程 vs 智能调度）", font=font(24), fill=INK)
    grand = max(tot.values())
    bar_x0, bar_max = 180, 640
    for k, (tag, label) in enumerate((("baseline", "人工排程"), ("optimised", "智能调度"))):
        y = 110 + k * 90
        d.text((28, y + 6), label, font=font(18), fill=INK)
        total_w = tot[tag] / grand * bar_max
        x = bar_x0
        for c, _, col in comps:
            w = brk[tag][c] / grand * bar_max
            d.rectangle([x, y, x + w, y + 40], fill=col)
            x += w
        d.text((bar_x0 + total_w + 12, y + 8), "%.0f 元" % tot[tag], font=font(18), fill=INK)
    # legend
    ly = 320
    for i, (c, nm, col) in enumerate(comps):
        cx = 28 + i * 190
        d.rectangle([cx, ly, cx + 16, ly + 16], fill=col)
        d.text((cx + 24, ly - 2), "%s %.0f" % (nm, brk["optimised"][c]),
               font=font(15), fill=MUTED)
    d.text((28, ly + 44),
           "总成本：人工 %.0f 元 -> 智能 %.0f 元，节省 %.0f 元（%.1f%%）"
           % (tot["baseline"], tot["optimised"], tot["baseline"] - tot["optimised"],
              100 * (tot["baseline"] - tot["optimised"]) / tot["baseline"]),
           font=font(17), fill=INK)
    img.save(os.path.join(OUT, "cost_compare.png"))


def main():
    problems, base, opt = run_main()
    route_map(problems, opt)
    cost_compare(problems, base, opt)
    print("figures ->", os.path.join(OUT, "route_map.png"), "|",
          os.path.join(OUT, "cost_compare.png"))


if __name__ == "__main__":
    main()
