"""Fluid nets: one pipe network per fluid, routed by Dijkstra over tiles, crossing others with pipe-to-ground.

A pipe joins every pipe next to it, so two fluids must never touch. The rules the router keeps:

- a pipe of fluid F never has a pipe of another fluid, or a machine port reserved for one, beside it;
- port targets (the tile outside a machine's pipe connection) are reserved for the fluid that box carries, or
  for nothing at all when the recipe leaves the box unused;
- a pipe-to-ground connects only on its open side, so its pair may pass under anything; its ends never sit on
  another underground's line between that one's ends (they would pair with the wrong partner);
- walkway tiles take no buildings at all, so the courier can always walk the district.

    nets = Nets()
    nets.reserve(port_target, "water")
    steps = route(nets, "water", start=port_target, free=world_free)    # None: no route
    nets.commit("water", steps)                                         # remembered for the next routes

steps: [("pipe", (x, y)), ("ptg", (x, y), "east"), ...] in build order; a pipe-to-ground's direction is the side
its open end faces (the entrance faces back along the route, the exit forward).
"""

from __future__ import annotations

import heapq
import itertools
from typing import Callable, Iterable

Tile = tuple[int, int]
VEC = [(0, -1), (1, 0), (0, 1), (-1, 0)]
NAME = ["north", "east", "south", "west"]
MAX_UG = 10                 # pipe-to-ground reach in 2.1 (catalog: max_underground_distance)
# Factorio 2.x: a fluid segment may stretch at most 320 tiles (utility-constants default_pipeline_extent); past
# that the game says "Pipeline has been overextended" and no fluid flows. A pump starts a new segment. Run
# 20261120-211555: a 460-tile crude line from the oil field fed the blue science workshop nothing at all.
PIPELINE_EXTENT = 320
PUMP_EVERY = 200            # route tiles between pumps on a long line (room left for the floor's own pipes)
PIPE_COST, UG_COST = 1.0, 6.0


def _add(t: Tile, d: int, k: int = 1) -> Tile:
    return t[0] + VEC[d][0] * k, t[1] + VEC[d][1] * k


class Nets:
    def __init__(self):
        self.pipe: dict[Tile, str] = {}                     # normal pipe -> fluid
        self.ptg: dict[Tile, tuple[str, int]] = {}          # pipe-to-ground end -> (fluid, open side 0..3)
        self.reserved: dict[Tile, str] = {}                 # port target -> fluid ("" = nothing may go there)
        self.spans: list[tuple[str, int, int, int, int]] = []   # (fluid, axis 0=x/1=y, fixed, lo, hi)
        self.walk: set[Tile] = set()                        # walkways: no buildings
        self.noend: set[Tile] = set()                       # no pipe-to-ground end here (keeps lanes open)
        self.lanes: dict[Tile, str] = {}                    # a port's exit lane (2 tiles straight out): its fluid
        self.pumps: dict[Tile, str] = {}                    # pump tiles (two each): joined only at their ends

    def reserve(self, t: Tile, fluid: str) -> None:
        if self.reserved.get(t) not in (None, "") and self.reserved[t] != fluid and fluid:
            raise ValueError(f"port target {t} is already reserved for {self.reserved[t]}")
        if fluid or t not in self.reserved:
            self.reserved[t] = fluid

    def tiles(self, fluid: str) -> set[Tile]:
        return {t for t, f in self.pipe.items() if f == fluid}

    def occupied(self) -> set[Tile]:
        return set(self.pipe) | set(self.ptg) | set(self.pumps)

    def commit(self, fluid: str, steps: list[tuple]) -> None:
        ends = []
        for s in steps:
            if s[0] == "pipe":
                self.pipe[s[1]] = fluid
            elif s[0] == "pump":
                for t in s[1]:
                    self.pumps[t] = fluid
            else:
                d = NAME.index(s[2])
                self.ptg[s[1]] = (fluid, d)
                ends.append(s[1])
        for a, b in zip(ends[::2], ends[1::2]):
            axis = 0 if a[1] == b[1] else 1
            fixed = a[1] if axis == 0 else a[0]
            lo, hi = sorted((a[axis], b[axis]))
            self.spans.append((fluid, axis, fixed, lo, hi))

    def count(self) -> dict[str, int]:
        return {"pipe": len(self.pipe), "pipe-to-ground": len(self.ptg), "pump": len(self.pumps) // 2}


def route(nets: Nets, fluid: str, start: Tile, free: Callable[[Tile], bool], goals: Iterable[Tile] | None = None,
          margin: int = 24, max_ug: int = MAX_UG, limit: int = 250_000) -> list[tuple] | None:
    """Pipes (and pipe-to-ground pairs) from `start` (a port target, where a pipe must stand) to the fluid's net.
    With no net yet, the start pipe alone seeds it. Returns the steps, or None when nothing fits."""
    goals = set(goals) if goals is not None else nets.tiles(fluid)
    if start in goals:
        return []
    if not _pipe_ok(nets, fluid, start, free, allow_reserved_self=True):
        return None
    if not goals:
        return [("pipe", start)]
    if any(_add(start, d) in goals for d in range(4)):
        return [("pipe", start)]
    xs = [t[0] for t in goals] + [start[0]]
    ys = [t[1] for t in goals] + [start[1]]
    box = (min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin)
    gx0, gy0, gx1, gy1 = min(xs[:-1]), min(ys[:-1]), max(xs[:-1]), max(ys[:-1])

    def h(t: Tile) -> float:                                # to the goals' bounding box: admissible
        return max(gx0 - t[0], 0, t[0] - gx1) + max(gy0 - t[1], 0, t[1] - gy1)

    def inside(t: Tile) -> bool:
        return box[0] <= t[0] <= box[2] and box[1] <= t[1] <= box[3]

    tie = itertools.count()
    startst = ("p", start, -1)
    best = {startst: 0.0}
    parent: dict[tuple, tuple] = {startst: (None, [("pipe", start)])}
    heap = [(h(start), next(tie), 0.0, startst)]
    n = 0
    while heap:
        _, _, g, st = heapq.heappop(heap)
        if g > best.get(st, 1e18):
            continue
        n += 1
        if n > limit:
            return None
        kind, t, din = st
        dirs = range(4) if kind == "p" else [din]           # an underground exit only goes on forward
        for d in dirs:
            nxt = _add(t, d)
            if nxt in goals:                                # joins the net right here
                return _unwind(parent, st)
            moves = []
            if inside(nxt) and _pipe_ok(nets, fluid, nxt, free):
                moves.append((("p", nxt, d), PIPE_COST, [("pipe", nxt)]))
                if any(_add(nxt, e) in goals for e in range(4)):
                    return _unwind(parent, st) + [("pipe", nxt)]
            if inside(nxt) and _end_ok(nets, fluid, nxt, free):           # an entrance, open side back to t
                for k in range(2, max_ug + 1):
                    b = _add(nxt, d, k)
                    if not inside(b):
                        break
                    if not _end_ok(nets, fluid, b, free) or not _span_ok(nets, nxt, b, d, fluid):
                        continue
                    fwd = _add(b, d)                         # the exit's open side: must not meet another fluid
                    if nets.pipe.get(fwd) not in (None, fluid) or nets.reserved.get(fwd) not in (None, "", fluid):
                        continue
                    if fwd in nets.ptg:
                        continue
                    steps = [("ptg", nxt, NAME[(d + 2) % 4]), ("ptg", b, NAME[d])]
                    moves.append((("x", b, d), UG_COST + 0.1 * k, steps))
            for ns, cost, steps in moves:
                ng = g + cost
                if ng < best.get(ns, 1e18):
                    best[ns] = ng
                    parent[ns] = (st, steps)
                    heapq.heappush(heap, (ng + h(ns[1]), next(tie), ng, ns))
            # an exit next to the net joins it through its open side
        if kind == "x" and _add(t, din) in goals:
            return _unwind(parent, st)
    return None


def _unwind(parent: dict, st: tuple) -> list[tuple]:
    out: list[tuple] = []
    while st is not None:
        prev, steps = parent[st]
        out = steps + out
        st = prev
    return out


def _pipe_ok(nets: Nets, fluid: str, t: Tile, free, allow_reserved_self: bool = False) -> bool:
    if t in nets.walk or t in nets.pipe or t in nets.ptg or t in getattr(nets, "pumps", {}) or not free(t):
        return False
    r = nets.reserved.get(t)
    if r is not None and r != fluid:
        return False
    if nets.lanes.get(t, fluid) != fluid:
        return False
    for d in range(4):
        m = _add(t, d)
        if nets.pipe.get(m) not in (None, fluid):
            return False
        if nets.reserved.get(m) not in (None, "", fluid):
            return False
        if nets.lanes.get(m, fluid) != fluid:              # another port's lane will hold its fluid
            return False
        end = nets.ptg.get(m)
        if end and end[0] != fluid and _add(m, end[1]) == t:   # another fluid's open end faces this tile
            return False
    return True


def _end_ok(nets: Nets, fluid: str, t: Tile, free) -> bool:
    if t in nets.noend or t in nets.walk or t in nets.pipe or t in nets.ptg or t in getattr(nets, "pumps", {}) \
            or not free(t):
        return False
    if t in nets.reserved:                                   # never on a port target, even of the same fluid:
        return False                                         # an end there would face away from the port
    if nets.lanes.get(t, fluid) != fluid:                    # its own lane may start an underground, no other
        return False
    for f, axis, fixed, lo, hi in nets.spans:                # never on another underground's line
        if (t[1] if axis == 0 else t[0]) == fixed and lo < t[axis] < hi:
            return False
    return True


def _span_ok(nets: Nets, a: Tile, b: Tile, d: int, fluid: str | None = None) -> bool:
    """No other pipe-to-ground end between a and b on that line (it would pair with ours), and no other fluid's
    port lane under it (that port's own underground would have nowhere to start: it seals the port in)."""
    k = abs(b[0] - a[0]) + abs(b[1] - a[1])
    for i in range(1, k):
        t = _add(a, d, i)
        if t in nets.ptg:
            return False
        if fluid is not None and nets.lanes.get(t, fluid) != fluid:
            return False
    axis = 0 if d in (1, 3) else 1
    fixed = a[1] if axis == 0 else a[0]
    lo, hi = sorted((a[axis], b[axis]))
    for f, ax, fx, l2, h2 in nets.spans:                     # overlapping parallel undergrounds on one line
        if ax == axis and fx == fixed and not (hi < l2 or h2 < lo):
            return False
    return True


# ------------------------------------------------------------------ long lines: pumps against the extent limit
def route_length(steps: list[tuple]) -> int:
    """Tiles a route spans end to end (underground stretches included)."""
    at = [s[1][0] if s[0] == "pump" else s[1] for s in steps]
    return sum(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in zip(at, at[1:]))


def _dir(a: Tile, b: Tile) -> int:
    """The direction index from a toward b (they share a row or a column)."""
    dx, dy = (b[0] > a[0]) - (b[0] < a[0]), (b[1] > a[1]) - (b[1] < a[1])
    return VEC.index((dx, dy))


def add_pumps(steps: list[tuple], free: Callable[[Tile], bool], flow_to_start: bool = True,
              every: int = PUMP_EVERY) -> tuple[list[tuple], list[dict]]:
    """A long route with a pump every `every` tiles, so no segment overextends. flow_to_start: the fluid enters at
    the route's last step and flows toward its first (a consumer routed to its source's net); else the other way.

    A pump takes two tiles in a straight line and joins only at its ends. It goes where the line is straight: two
    plain pipes in a row (the line going on straight either side), or the upstream end of a pipe-to-ground pair
    of span 4+, which moves two tiles downstream (the pump stands on its old tile and the next one, over what
    was underground: that tile must be free). Returns (the new steps, the changes: [{"pump": (input tile, output
    tile), "facing": direction name, "move": (old tile, new tile, open side) or None}])."""
    if route_length(steps) <= every:
        return list(steps), []
    seq = list(reversed(steps)) if flow_to_start else list(steps)       # in flow order: the source first
    out: list[tuple] = []
    changes: list[dict] = []
    run, i = 0, 0
    prev_at = None
    while i < len(seq):
        s = seq[i]
        if prev_at is not None:
            run += abs(s[1][0] - prev_at[0]) + abs(s[1][1] - prev_at[1])
        nxt = seq[i + 1] if i + 1 < len(seq) else None
        due = run >= every
        if due and nxt is not None and s[0] == "ptg" and nxt[0] == "ptg":
            p, q = s[1], nxt[1]
            k = abs(q[0] - p[0]) + abs(q[1] - p[1])
            f = _dir(p, q) if p != q else 0
            mid = _add(p, f)
            # the upstream end of a pair: its open side faces back up the line
            if k >= 4 and NAME.index(s[2]) == (f + 2) % 4 and NAME.index(nxt[2]) == f and free(mid):
                moved = _add(p, f, 2)
                out.append(("pump", (p, mid), NAME[f]))
                out.append(("ptg", moved, s[2]))
                changes.append({"pump": (p, mid), "facing": NAME[f], "move": (p, moved, s[2])})
                run, prev_at, i = 0, moved, i + 1
                continue
        if due and nxt is not None and s[0] == "pipe" and nxt[0] == "pipe" and out \
                and i + 2 < len(seq) and seq[i + 2][0] != "pump":
            p, q = s[1], nxt[1]
            before = out[-1][1] if out[-1][0] != "pump" else out[-1][1][1]
            after = seq[i + 2][1]
            if abs(q[0] - p[0]) + abs(q[1] - p[1]) == 1:
                f = _dir(p, q)
                if _add(before, f) == p and _add(q, f) == after:
                    out.append(("pump", (p, q), NAME[f]))
                    changes.append({"pump": (p, q), "facing": NAME[f], "move": None})
                    run, prev_at, i = 0, q, i + 2
                    continue
        out.append(s)
        prev_at = s[1]
        i += 1
    return (list(reversed(out)) if flow_to_start else out), changes


def line_from_entities(entities: list[tuple], near: tuple[float, float]) -> list[tuple] | None:
    """The longest pipe line among laid pipes and pipes-to-ground ((name, x, y, direction) as sites record them),
    as route steps from the end nearest `near` (the floor) to the far end. None when there is none."""
    pipe = {(int(x - 0.5), int(y - 0.5)) for n, x, y, _ in entities if n == "pipe"}
    ptg = {(int(x - 0.5), int(y - 0.5)): NAME.index(d) for n, x, y, d in entities if n == "pipe-to-ground"}
    nodes = pipe | set(ptg)
    if len(nodes) < 2:
        return None

    def partner(t: Tile) -> Tile | None:
        o = ptg[t]
        for k in range(1, MAX_UG + 2):
            q = _add(t, (o + 2) % 4, k)
            if q in ptg:
                return q if ptg[q] == (o + 2) % 4 else None
        return None

    def joins(t: Tile) -> list[Tile]:
        out = []
        if t in pipe:
            for d in range(4):
                q = _add(t, d)
                if q in pipe or (q in ptg and _add(q, ptg[q]) == t):
                    out.append(q)
        else:
            q = _add(t, ptg[t])
            if q in pipe or (q in ptg and _add(q, ptg[q]) == t):
                out.append(q)
            m = partner(t)
            if m is not None:
                out.append(m)
        return out

    def reach(src: Tile) -> dict[Tile, Tile | None]:
        par: dict[Tile, Tile | None] = {src: None}
        todo = [src]
        while todo:
            nxt = []
            for t in todo:
                for q in joins(t):
                    if q not in par:
                        par[q] = t
                        nxt.append(q)
            todo = nxt
        return par

    # the nets of a floor never join (one per fluid): take the one that reaches farthest, from its floor end
    best, left = None, set(nodes)
    while left:
        comp = set(reach(next(iter(left))))
        left -= comp
        xs, ys = [t[0] for t in comp], [t[1] for t in comp]
        span = max(xs) - min(xs) + max(ys) - min(ys)
        if best is None or span > best[0]:
            best = (span, comp)
    comp = best[1]
    start = min(comp, key=lambda t: (t[0] + 0.5 - near[0]) ** 2 + (t[1] + 0.5 - near[1]) ** 2)
    par = reach(start)
    far = max(par, key=lambda t: abs(t[0] - start[0]) + abs(t[1] - start[1]))
    path = [far]
    while par[path[-1]] is not None:
        path.append(par[path[-1]])
    path.reverse()                                          # from the floor end to the far end
    return [("pipe", t) if t in pipe else ("ptg", t, NAME[ptg[t]]) for t in path]
