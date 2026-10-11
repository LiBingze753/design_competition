# -*- coding: utf-8 -*-
"""Fleet-aware joint optimisation across both product families.

A solution is a list of routes; every route carries (product_key, vehicle_type,
ordered customer list). Vehicle types must satisfy the real fleet:
    <= 8 small (4.2m) and <= 2 medium (7.6m) in total.
Moves keep each route single-product (garlic/ginger and noodles never mix).
"""
from core import VEHICLES
from solver import clarke_wright, baseline_solution

SMALL_MAX = VEHICLES["small"]["count"]   # 8
MEDIUM_MAX = VEHICLES["medium"]["count"] # 2
BIG = 1e6

_cache = {}


def rc(prob, route, vt):
    key = (prob.key, vt, tuple(route))
    if key not in _cache:
        _cache[key] = prob.route_cost(route, vt)
    return _cache[key]


def best_type(prob, route):
    """Return (vt, cost) of the cheapest feasible vehicle, or None."""
    best = None
    for vt in ("small", "medium"):
        if prob.capacity_ok(route, VEHICLES[vt]):
            c = rc(prob, route, vt)
            if c and (best is None or c["total"] < best[1]):
                best = (vt, c["total"])
    return best


def evaluate(sol):
    """Return (raw_cost, small_n, medium_n) or None if any route infeasible."""
    total = 0.0
    small = medium = 0
    for key, vt, route in sol:
        prob = PROBS[key]
        c = rc(prob, route, vt)
        if c is None:
            return None
        total += c["total"]
        if vt == "small":
            small += 1
        else:
            medium += 1
    return total, small, medium


def objective(sol):
    e = evaluate(sol)
    if e is None:
        return float("inf")
    total, small, medium = e
    pen = BIG * max(0, small - SMALL_MAX) + BIG * max(0, medium - MEDIUM_MAX)
    return total + pen


def plan_metrics(sol):
    """Operational KPIs required by the multi-product coordination direction.

    装载率: mean payload / vehicle capacity across routes (weight & volume).
    准时送达率: share of stops served inside the *preferred* window; the
    *acceptable* window is the hard constraint, so its rate should be 100%.
    """
    w_util = []
    v_util = []
    fam = {}
    stops = on_pref = on_acc = 0
    for key, vt, route in sol:
        prob = PROBS[key]
        c = rc(prob, route, vt)
        if c is None:
            return None
        veh = VEHICLES[vt]
        w_util.append(c["load_kg"] / veh["cap_w"])
        v_util.append(c["load_m3"] / veh["cap_v"])
        f = fam.setdefault(prob.kind, {"routes": 0, "stops": 0, "w": 0.0,
                                       "pref": 0, "acc": 0})
        f["routes"] += 1
        f["w"] += c["load_kg"] / veh["cap_w"]
        for cust, start in zip(route, c["starts"]):
            stops += 1
            f["stops"] += 1
            if prob.pref_open[cust] - 1e-6 <= start <= prob.pref_close[cust] + 1e-6:
                on_pref += 1
                f["pref"] += 1
            if prob.acc_open[cust] - 1e-6 <= start <= prob.acc_close[cust] + 1e-6:
                on_acc += 1
                f["acc"] += 1

    def pct(x, n):
        return round(100.0 * x / n, 1) if n else 0.0

    by_family = {kind: {"routes": f["routes"],
                        "load_util_pct": pct(f["w"], f["routes"]),
                        "on_time_pref_pct": pct(f["pref"], f["stops"]),
                        "on_time_acc_pct": pct(f["acc"], f["stops"])}
                 for kind, f in fam.items()}
    return {
        "routes": len(w_util),
        "stops": stops,
        "load_util_weight_pct": pct(sum(w_util), len(w_util)),
        "load_util_volume_pct": pct(sum(v_util), len(v_util)),
        "on_time_pref_pct": pct(on_pref, stops),
        "on_time_acc_pct": pct(on_acc, stops),
        "by_family": by_family,
    }


def assignment_cost(sol):
    """Cheapest legal vehicle-type assignment for fixed route set.

    Returns None if no assignment fits the fleet caps.
    """
    forced_medium = []
    optional = []
    for i, (key, _vt, route) in enumerate(sol):
        prob = PROBS[key]
        small_ok = prob.capacity_ok(route, VEHICLES["small"])
        med_ok = prob.capacity_ok(route, VEHICLES["medium"])
        if not med_ok:
            return None
        if not small_ok:
            forced_medium.append(i)
        else:
            cs = rc(prob, route, "small")["total"]
            cm = rc(prob, route, "medium")["total"]
            optional.append((cm - cs, i))  # penalty of forcing medium
    if len(forced_medium) > MEDIUM_MAX:
        return None
    slots_medium = MEDIUM_MAX - len(forced_medium)
    optional.sort()  # cheapest to promote to medium first
    promote = {i for _d, i in optional[:max(0, len(optional) - SMALL_MAX)]}
    if len(promote) > slots_medium:
        return None
    out = []
    for i, (key, _vt, route) in enumerate(sol):
        if i in forced_medium or i in promote:
            out.append((key, "medium", route))
        else:
            out.append((key, "small", route))
    return out


# ---------------------------------------------------------------- initial
def initial_solution():
    sol = []
    for key, prob in PROBS.items():
        for r in clarke_wright(prob):
            bt = best_type(prob, r)
            if bt is None:
                continue
            sol.append((key, bt[0], list(r)))
    return sol


# ---------------------------------------------------------------- moves
def _intra_2opt(sol):
    improved = False
    for idx in range(len(sol)):
        key, vt, route = sol[idx]
        prob = PROBS[key]
        base = rc(prob, route, vt)["total"]
        n = len(route)
        if n < 3:
            continue
        changed = True
        while changed:
            changed = False
            for a in range(n - 1):
                for b in range(a + 1, n):
                    cand = route[:a] + route[a:b + 1][::-1] + route[b + 1:]
                    if cand == route:
                        continue
                    c = rc(prob, cand, vt)
                    if c and c["total"] < base - 1e-9:
                        sol[idx] = (key, vt, cand)
                        route, base = cand, c["total"]
                        changed = improved = True
    return sol, improved


def _retype(sol):
    """Try flipping a route's vehicle type."""
    for idx in range(len(sol)):
        key, vt, route = sol[idx]
        prob = PROBS[key]
        other = "medium" if vt == "small" else "small"
        if not prob.capacity_ok(route, VEHICLES[other]):
            continue
        old = objective(sol)
        cand = list(sol)
        cand[idx] = (key, other, route)
        if objective(cand) < old - 1e-9:
            return cand, True
    return sol, False


def _relocate(sol):
    for a in range(len(sol)):
        ka, va, ra = sol[a]
        if len(ra) == 1:
            continue
        proba = PROBS[ka]
        for pos in range(len(ra)):
            c = ra[pos]
            new_ra = ra[:pos] + ra[pos + 1:]
            if rc(proba, new_ra, va) is None:
                continue
            for b in range(len(sol)):
                kb, vb, rb = sol[b]
                if ka != kb or a == b:
                    continue
                probb = PROBS[kb]
                for ins in range(len(rb) + 1):
                    new_rb = rb[:ins] + [c] + rb[ins:]
                    if rc(probb, new_rb, vb) is None:
                        continue
                    cand = list(sol)
                    cand[a] = (ka, va, new_ra)
                    cand[b] = (kb, vb, new_rb)
                    if objective(cand) < objective(sol) - 1e-9:
                        return cand, True
    return sol, False


def _swap(sol):
    for a in range(len(sol)):
        for b in range(a + 1, len(sol)):
            ka, va, ra = sol[a]
            kb, vb, rb = sol[b]
            if ka != kb:
                continue
            prob = PROBS[ka]
            for i in range(len(ra)):
                for j in range(len(rb)):
                    na = ra[:i] + [rb[j]] + ra[i + 1:]
                    nb = rb[:j] + [ra[i]] + rb[j + 1:]
                    if rc(prob, na, va) is None or rc(prob, nb, vb) is None:
                        continue
                    cand = list(sol)
                    cand[a] = (ka, va, na)
                    cand[b] = (kb, vb, nb)
                    if objective(cand) < objective(sol) - 1e-9:
                        return cand, True
    return sol, False


def _merge(sol):
    for a in range(len(sol)):
        for b in range(a + 1, len(sol)):
            ka, va, ra = sol[a]
            kb, vb, rb = sol[b]
            if ka != kb:
                continue
            prob = PROBS[ka]
            for cand_route in (ra + rb, ra + rb[::-1], ra[::-1] + rb):
                bt = best_type(prob, cand_route)
                if bt is None:
                    continue
                cand = [s for i, s in enumerate(sol) if i not in (a, b)]
                cand.append((ka, bt[0], cand_route))
                # re-balance vehicle types for the reduced fleet
                fixed = assignment_cost(cand)
                if fixed is None:
                    continue
                if objective(fixed) < objective(sol) - 1e-9:
                    return fixed, True
    return sol, False


def _reduce_medium(sol):
    """While too many medium routes exist, shrink the lightest one."""
    med = [i for i, s in enumerate(sol) if s[1] == "medium"]
    if len(med) <= MEDIUM_MAX:
        return sol, False
    load = lambda idx: sum(PROBS[sol[idx][0]].weight[c] for c in sol[idx][2])
    idx = min(med, key=load)
    key, vt, route = sol[idx]
    prob = PROBS[key]
    cap = VEHICLES["small"]["cap_w"]
    best = None
    for pos in range(len(route)):
        c = route[pos]
        shorter = route[:pos] + route[pos + 1:]
        if sum(prob.weight[x] for x in shorter) > cap:
            continue
        if shorter and rc(prob, shorter, "small") is None:
            continue
        for b in range(len(sol)):
            if b == idx or sol[b][0] != key:
                continue
            kb, vb, rb = sol[b]
            for ins in range(len(rb) + 1):
                nr = rb[:ins] + [c] + rb[ins:]
                nbt = best_type(PROBS[kb], nr)
                if nbt is None:
                    continue
                cand = list(sol)
                if shorter:
                    cand[idx] = (key, "small", shorter)
                    bb = b
                else:
                    cand.pop(idx)
                    bb = b if b < idx else b - 1
                cand[bb] = (kb, nbt[0], nr)
                v = objective(cand)
                if best is None or v < best[0]:
                    best = (v, cand)
    if best is not None and best[0] < objective(sol) - 1e-9:
        return best[1], True
    return sol, False


def _local_search(sol, rounds=6):
    seen = set()
    for _ in range(rounds):
        any_improved = False
        for mover in (_reduce_medium, _intra_2opt, _retype, _relocate, _swap, _merge):
            sol, imp = mover(sol)
            any_improved |= imp
        sig = tuple(sorted((k, v, tuple(r)) for k, v, r in sol))
        if sig in seen or not any_improved:
            break
        seen.add(sig)
    fixed = assignment_cost(sol)
    return fixed if fixed is not None else sol


PROBS = {}


def _shuffle_solution(seed):
    """A randomised restart: shuffle customers, greedily pack again."""
    import random
    rnd = random.Random(seed)
    sol = []
    for key, prob in PROBS.items():
        flat = list(range(prob.n))
        rnd.shuffle(flat)
        routes = []
        for c in flat:
            placed = False
            for r in routes:
                cand = r + [c]
                if best_type(prob, cand) is not None:
                    r.append(c)
                    placed = True
                    break
            if not placed:
                routes.append([c])
        for r in routes:
            bt = best_type(prob, r)
            if bt is not None:
                sol.append((key, bt[0], r))
    return sol


def solve_fleet(problems):
    """problems: dict key -> Problem. Returns the fleet-feasible solution."""
    global PROBS, _cache
    PROBS = problems
    _cache = {}
    best = None
    for seed in range(6):
        sol = initial_solution() if seed == 0 else _shuffle_solution(seed)
        fixed = assignment_cost(sol)
        if fixed is not None:
            sol = fixed
        sol = _local_search(sol)
        v = objective(sol)
        if best is None or v < best[0]:
            best = (v, sol)
    return best[1]


def baseline_fleet(problems):
    """Manual-style baseline, then cheapest legal fleet assignment."""
    global PROBS, _cache
    PROBS = problems
    _cache = {}
    sol = []
    for key, prob in problems.items():
        for r in baseline_solution(prob):
            bt = best_type(prob, r)
            sol.append((key, bt[0], list(r)))
    fixed = assignment_cost(sol)
    if fixed is not None:
        return fixed
    # repair an over-sized manual plan into the physical fleet by merging
    for _ in range(40):
        best = None
        for a in range(len(sol)):
            for b in range(a + 1, len(sol)):
                ka, va, ra = sol[a]
                kb, vb, rb = sol[b]
                if ka != kb:
                    continue
                prob = PROBS[ka]
                for cand_route in (ra + rb, ra + rb[::-1], ra[::-1] + rb):
                    bt = best_type(prob, cand_route)
                    if bt is None:
                        continue
                    cand = [s for i, s in enumerate(sol) if i not in (a, b)]
                    cand.append((ka, bt[0], cand_route))
                    fx = assignment_cost(cand)
                    if fx is None:
                        continue
                    if best is None or objective(fx) < best[0]:
                        best = (objective(fx), fx)
        if best is None:
            break
        sol = best[1]
        if assignment_cost(sol) is not None:
            break
    fixed = assignment_cost(sol)
    return fixed if fixed is not None else sol
