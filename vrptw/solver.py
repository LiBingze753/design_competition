# -*- coding: utf-8 -*-
"""Construction + improvement heuristics.

baseline_solution : manual-style sequential append (dispatch-clerk behaviour)
clarke_wright     : parallel savings construction with time windows
local_search      : 2-opt / relocate / swap / merge until no improvement
solve             : combined pipeline (the optimised plan)
"""
from core import Problem


# ---------------------------------------------------------------- baseline
def baseline_solution(p):
    """Emulate a clerk: process customers by acceptable-window opening time and
    append to the current route while it fits, otherwise open a new route."""
    order = sorted(range(p.n), key=lambda c: (p.acc_open[c], p.acc_close[c]))
    routes = []
    for c in order:
        placed = False
        for r in routes:
            cand = r + [c]
            if p.pick_vehicle(cand) is not None and p.route_cost(cand) is not None:
                r.append(c)
                placed = True
                break
        if not placed:
            if p.route_cost([c]) is None:
                raise RuntimeError("customer %d infeasible alone" % c)
            routes.append([c])
    return routes


# ---------------------------------------------------------------- savings
def clarke_wright(p):
    routes = [[c] for c in range(p.n)]
    # savings only meaningful for customers we can pair
    sav = []
    for i in range(p.n):
        for j in range(i + 1, p.n):
            s = p.dm[0][i + 1] + p.dm[0][j + 1] - p.dm[i + 1][j + 1]
            sav.append((s, i, j))
    sav.sort(reverse=True)

    def find(c):
        for idx, r in enumerate(routes):
            if c in r:
                return idx
        return -1

    for s, i, j in sav:
        ri, rj = find(i), find(j)
        if ri == rj or ri < 0 or rj < 0:
            continue
        a, b = routes[ri], routes[rj]
        # i must be an endpoint of a, j an endpoint of b
        variants = []
        if a[-1] == i and b[0] == j:
            variants.append(a + b)
        if a[0] == i and b[-1] == j:
            variants.append(b + a)
        if a[-1] == i and b[-1] == j:
            variants.append(a + b[::-1])
        if a[0] == i and b[0] == j:
            variants.append(a[::-1] + b)
        merged = None
        for cand in variants:
            if p.pick_vehicle(cand) is not None and p.route_cost(cand) is not None:
                merged = cand
                break
        if merged is None:
            continue
        keep, drop = (ri, rj) if ri < rj else (rj, ri)
        routes[keep] = merged
        routes.pop(drop)
    return routes


# ---------------------------------------------------------------- local search
def _cost(p, r):
    rc = p.route_cost(r)
    return rc["total"] if rc else float("inf")


def _try(p, routes):
    """One pass of improving moves. Returns (routes, improved)."""
    improved = False
    costs = [_cost(p, r) for r in routes]

    # --- intra-route 2-opt
    for k, r in enumerate(routes):
        base = costs[k]
        n = len(r)
        improved_here = True
        while improved_here and n >= 3:
            improved_here = False
            for a in range(n - 1):
                for b in range(a + 1, n):
                    cand = r[:a] + r[a:b + 1][::-1] + r[b + 1:]
                    if cand == r:
                        continue
                    cc = p.route_cost(cand)
                    if cc and cc["total"] < base - 1e-9:
                        r[:] = cand
                        base = cc["total"]
                        costs[k] = base
                        improved_here = True
                        improved = True
            n = len(r)

    # --- relocate: move one customer from one route to another
    for a in range(len(routes)):
        for b in range(len(routes)):
            if a == b:
                continue
            ra, rb = routes[a], routes[b]
            for pos in range(len(ra)):
                if len(ra) == 1:
                    continue
                c = ra[pos]
                new_ra = ra[:pos] + ra[pos + 1:]
                if p.route_cost(new_ra) is None:
                    continue
                for ins in range(len(rb) + 1):
                    new_rb = rb[:ins] + [c] + rb[ins:]
                    na, nb = p.route_cost(new_ra), p.route_cost(new_rb)
                    if na is None or nb is None:
                        continue
                    if na["total"] + nb["total"] < costs[a] + costs[b] - 1e-9:
                        routes[a], routes[b] = new_ra, new_rb
                        costs[a], costs[b] = na["total"], nb["total"]
                        improved = True
                        break
                else:
                    continue
                break
            else:
                continue
            break

    # --- swap two customers between routes
    for a in range(len(routes)):
        for b in range(a + 1, len(routes)):
            ra, rb = routes[a], routes[b]
            for i in range(len(ra)):
                for j in range(len(rb)):
                    na_ = ra[:i] + [rb[j]] + ra[i + 1:]
                    nb_ = rb[:j] + [ra[i]] + rb[j + 1:]
                    ca, cb = p.route_cost(na_), p.route_cost(nb_)
                    if ca is None or cb is None:
                        continue
                    if ca["total"] + cb["total"] < costs[a] + costs[b] - 1e-9:
                        routes[a], routes[b] = na_, nb_
                        costs[a], costs[b] = ca["total"], cb["total"]
                        improved = True
                        return routes, True

    # --- merge two routes if feasible
    for a in range(len(routes)):
        for b in range(a + 1, len(routes)):
            for cand in (routes[a] + routes[b], routes[a] + routes[b][::-1],
                         routes[a][::-1] + routes[b]):
                cc = p.route_cost(cand)
                if cc and cc["total"] < costs[a] + costs[b] - 1e-9:
                    routes[a] = cand
                    routes.pop(b)
                    return routes, True
    return routes, improved


def local_search(p, routes, max_iter=400):
    for _ in range(max_iter):
        routes, improved = _try(p, routes)
        if not improved:
            break
    # drop empty
    routes = [r for r in routes if r]
    return routes


def solve(p):
    init = clarke_wright(p)
    base_cost = p.solution_cost(init)
    routes = local_search(p, init)
    return {
        "initial_cost": base_cost,
        "routes": routes,
        "cost": p.solution_cost(routes),
    }
