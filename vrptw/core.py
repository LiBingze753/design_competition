# -*- coding: utf-8 -*-
"""Core geometry / scheduling / cost functions for the VRPTW model."""
import math

from data import (
    DEPOT, PARAMS, VEHICLES, UNIT_PRICE,
    DEPOT_OPEN_MIN, DEPOT_CLOSE_MIN, hhmm,
)

EARTH_R = 6371.0


def _rad(x):
    return x * math.pi / 180.0


def haversine(lon1, lat1, lon2, lat2):
    """Great-circle distance in km."""
    dlon = _rad(lon2 - lon1)
    dlat = _rad(lat2 - lat1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(_rad(lat1)) * math.cos(_rad(lat2)) * math.sin(dlon / 2) ** 2)
    return 2 * EARTH_R * math.asin(math.sqrt(a))


# ---------------------------------------------------------------- problem
class Problem:
    """A single-product VRPTW instance (one product family, 30 customers)."""

    def __init__(self, key, rows, kind):
        """
        key  : 'jiangsuan' or 'miantiao'
        rows : raw table rows
        kind : 'jiang' (garlic+ginger) or 'noodle'
        """
        self.key = key
        self.kind = kind
        self.n = len(rows)
        self.ids = [r[0] for r in rows]
        self.lon = [r[1] for r in rows]
        self.lat = [r[2] for r in rows]

        if kind == "jiang":
            self.q_garlic = [float(r[3]) for r in rows]
            self.q_ginger = [float(r[4]) for r in rows]
            self.weight = [g + s for g, s in zip(self.q_garlic, self.q_ginger)]
            self.value = [g * UNIT_PRICE["garlic"] + s * UNIT_PRICE["ginger"]
                          for g, s in zip(self.q_garlic, self.q_ginger)]
            off = 5
        else:
            self.weight = [float(r[3]) for r in rows]
            self.value = [q * UNIT_PRICE["noodle"] for q in self.weight]
            off = 4

        self.pref_open = [hhmm(r[off + 0]) for r in rows]
        self.pref_close = [hhmm(r[off + 1]) for r in rows]
        self.acc_open = [hhmm(r[off + 2]) for r in rows]
        self.acc_close = [hhmm(r[off + 3]) for r in rows]
        self.service = [float(r[off + 4]) for r in rows]

        # assumed cargo density to convert weight -> volume (250 kg/m3)
        self.density = 250.0
        self.volume = [w / self.density for w in self.weight]

        # distance matrix over depot + customers
        pts = [DEPOT] + list(zip(self.lon, self.lat))
        self.dm = [[haversine(*pts[a], *pts[b]) for b in range(len(pts))]
                   for a in range(len(pts))]

    # ---------------------------------------------------- route evaluation
    def _departures(self):
        # minute steps across the loading window; evaluated per route
        return range(int(DEPOT_OPEN_MIN), 8 * 60 + 1, 5)

    def schedule(self, route, depart):
        """Return (feasible, arrival/start list, distance, service_h, penalty)."""
        t = depart
        dist = 0.0
        penalty = 0.0
        starts = []
        prev = 0
        for c in route:
            j = c + 1
            leg = self.dm[prev][j]
            dist += leg
            t += leg / PARAMS["speed"] * 60.0
            arr = t
            start = max(arr, self.acc_open[c])
            if start > self.acc_close[c] + 1e-6:
                return False, starts, dist, 0.0, 0.0
            early = max(0.0, self.pref_open[c] - start)
            late = max(0.0, start - self.pref_close[c])
            penalty += early / 60.0 * PARAMS["early_penalty"]
            penalty += late / 60.0 * PARAMS["late_penalty"]
            starts.append(start)
            t = start + self.service[c]
            prev = j
        dist += self.dm[prev][0]
        service_h = sum(self.service[c] for c in route) / 60.0
        return True, starts, dist, service_h, penalty

    def best_departure(self, route):
        best = None
        for d in self._departures():
            ok, starts, dist, svc_h, pen = self.schedule(route, d)
            if ok and (best is None or pen < best[4]):
                best = (d, starts, dist, svc_h, pen)
        return best

    def capacity_ok(self, route, veh):
        w = sum(self.weight[c] for c in route)
        v = sum(self.volume[c] for c in route)
        return w <= veh["cap_w"] + 1e-6 and v <= veh["cap_v"] + 1e-6

    def pick_vehicle(self, route):
        """Cheapest feasible vehicle for a route; None if none fits."""
        best = None
        for vt in ("small", "medium"):
            veh = VEHICLES[vt]
            if self.capacity_ok(route, veh):
                if best is None or veh["fixed"] < VEHICLES[best]["fixed"]:
                    best = vt
        return best

    def route_cost(self, route, vt=None):
        """Full cost breakdown dict for one route, or None if infeasible."""
        if not route:
            return None
        if vt is None:
            vt = self.pick_vehicle(route)
        if vt is None:
            return None
        veh = VEHICLES[vt]
        if not self.capacity_ok(route, veh):
            return None
        bd = self.best_departure(route)
        if bd is None:
            return None
        depart, starts, dist, svc_h, pen = bd
        drive_h = dist / PARAMS["speed"]
        transport = veh["rate"] * dist
        fixed = veh["fixed"]
        refrig = PARAMS["refrig_drive"] * drive_h + PARAMS["refrig_service"] * svc_h
        loss = sum(self.value[c] for c in route) * (
            PARAMS["loss_transit"] + PARAMS["loss_service"])
        total = fixed + transport + refrig + pen + loss
        return {
            "vehicle": vt, "distance_km": dist, "depart": depart,
            "starts": starts, "drive_h": drive_h, "service_h": svc_h,
            "fixed": fixed, "transport": transport, "refrig": refrig,
            "penalty": pen, "loss": loss, "total": total,
            "load_kg": sum(self.weight[c] for c in route),
            "load_m3": sum(self.volume[c] for c in route),
        }

    def solution_cost(self, routes):
        total = 0.0
        for r in routes:
            rc = self.route_cost(r)
            if rc is None:
                return None
            total += rc["total"]
        return total
