"""Routing: every item net becomes one belt chain, from the cells that make the item to the cells that use it.

Each cell joins a net through a pin (an inserter plus the belt tile beyond it). A* routes the chain leg by leg;
it may dive under other belts with an underground pair. Rules that keep items where they belong:

- one item per belt, so an inserter can never grab the wrong thing;
- a belt chain only ever ends pointing at an empty tile, never into another belt (that would merge nets);
- an underground exit is always followed straight on, since it can only output forwards;
- no underground pair spans another one on the same axis (it would pair with the wrong end).
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field

from .cells import DIRS, OPPOSITE, Cell, PinSite, dir_between
from .grid import Grid
from .place import Net

Tile = tuple[int, int]
UG_MAX = 5                 # an underground exit at most 5 tiles past its entrance (yellow belts)


@dataclass
class Pin:
    cell: str
    site: PinSite
    into_cell: bool            # True: belt -> cell (an ingredient). False: cell -> belt or chest (a product)


@dataclass
class Route:
    item: str
    path: list[Tile] = field(default_factory=list)              # belt tiles in flow order
    kinds: dict[Tile, str] = field(default_factory=dict)        # tile -> "belt" | "ug_in" | "ug_out"
    facing: dict[Tile, str] = field(default_factory=dict)
    pins: list[Pin] = field(default_factory=list)
    chests: list[Tile] = field(default_factory=list)


class Router:
    def __init__(self, grid: Grid, cells: list[Cell], box: tuple[int, int, int, int], max_ug_pairs: int = 10):
        self.grid, self.box = grid, box
        self.cells = {c.id: c for c in cells}
        self.occ: dict[Tile, str] = {}
        for c in cells:
            for t in c.footprint():
                self.occ[t] = f"cell:{c.id}"
        self.nobelt: set[Tile] = set()          # the tile in front of each chain's last belt
        self.spans: list[tuple[Tile, Tile]] = []  # underground pairs (entrance, exit), committed nets
        self.pending: list[tuple[Tile, Tile]] = []  # pairs of the net being routed
        self.ug_left = max_ug_pairs
        self.routes: list[Route] = []
        # a feed's end tile and first belt tile belong to its own item: another net taking either leaves the
        # feed with no way in (it has one pin, not three like a chest)
        self.claimed: dict[Tile, str] = {}
        for c in cells:
            if c.kind == "feed":
                for s in c.pin_sites():
                    self.claimed[s.inserter] = self.claimed[s.outer] = c.product

    # ------------------------------------------------------------------ helpers
    def usable(self, t: Tile) -> bool:
        x0, y0, x1, y1 = self.box
        return x0 <= t[0] <= x1 and y0 <= t[1] <= y1 and self.grid.free(t) and t not in self.occ

    def _sites(self, cell: Cell, avoid: set[Tile]) -> list[PinSite]:
        return [s for s in cell.pin_sites()
                if self.usable(s.inserter) and self.usable(s.outer) and s.inserter not in avoid
                and s.outer not in avoid and s.outer not in self.nobelt]

    def _span_ok(self, e: Tile, x: Tile) -> bool:
        between = _between(e, x)
        for a, b in self.spans + self.pending:
            if a in between or b in between:
                return False
            same_axis = (a[0] == b[0] == e[0] == x[0]) or (a[1] == b[1] == e[1] == x[1])
            if same_axis and (e in _between(a, b) or x in _between(a, b)):
                return False
        return True

    def _step_cost(self, t: Tile) -> float:
        return 1.0 + (2.0 if t in self.grid.obstacles else 0.0) + (0.3 if t in self.grid.ore else 0.0)

    # ------------------------------------------------------------------ A*
    def _astar(self, starts: list[Tile], start_dir: str | None, targets: dict[Tile, PinSite],
               forbid: set[Tile], valid=None) -> list[tuple[Tile, str]] | None:
        """[(tile, how)] from a start to a target; how is 'start', 'step', 'ug_in' or 'ug_out'.

        Nodes are (tile, facing, state): state 'n' after a normal belt, 'x' right after an underground exit,
        'e' an underground entrance (bookkeeping only, never expanded).
        """
        tx = [t[0] for t in targets]
        ty = [t[1] for t in targets]

        def h(t: Tile) -> float:
            return min(abs(t[0] - a) + abs(t[1] - b) for a, b in zip(tx, ty))

        how_of = {"n": "step", "x": "ug_out", "e": "ug_in"}
        frontier: list = []
        came: dict = {}
        best: dict = {}
        n = 0
        for s in starts:
            node = (s, start_dir, "n")
            best[node] = 0.0
            came[node] = None
            heapq.heappush(frontier, (h(s), 0.0, n, node))
            n += 1
        while frontier:
            _, g, _, node = heapq.heappop(frontier)
            if g > best.get(node, math.inf):
                continue
            tile, d, state = node
            if tile in targets and state == "n" and came[node] is not None:
                out = []
                k = node
                while k is not None:
                    out.append((k[0], how_of[k[2]] if came[k] is not None else "start"))
                    k = came[k]
                out.reverse()
                tiles = [t for t, _ in out]
                if len(set(tiles)) == len(tiles) and (valid is None or valid(tiles)):
                    return out
                continue                                 # a loop, or over its own inserter: keep looking
            if g > 400:
                continue
            for name, (dx, dy) in DIRS.items():
                if d is not None and name == OPPOSITE[d]:
                    continue
                if state == "x" and name != d:           # an underground exit only outputs straight on
                    continue
                nt = (tile[0] + dx, tile[1] + dy)
                if not self.usable(nt) or nt in forbid or nt in self.nobelt:
                    continue
                ng = g + self._step_cost(nt) + (0.6 if d is not None and name != d else 0.0)
                nn = (nt, name, "n")
                if ng < best.get(nn, math.inf):
                    best[nn] = ng
                    came[nn] = node
                    heapq.heappush(frontier, (ng + h(nt), ng, n, nn))
                    n += 1
            if d is None or state == "x" or self.ug_left <= 0:
                continue
            dx, dy = DIRS[d]
            e = (tile[0] + dx, tile[1] + dy)
            if not self.usable(e) or e in forbid or e in self.nobelt or e in targets:
                continue
            for k in range(2, UG_MAX + 1):
                x = (e[0] + dx * k, e[1] + dy * k)
                if not self.usable(x) or x in forbid or x in self.nobelt or x in targets:
                    continue
                if not self._span_ok(e, x):
                    continue
                ng = g + 4.0 + k
                xn = (x, d, "x")
                if ng < best.get(xn, math.inf):
                    en = (e, d, "e", tile)               # one entrance node per way in, so paths never mix
                    best[xn] = ng
                    came[en] = node
                    came[xn] = en
                    heapq.heappush(frontier, (ng + h(x), ng, n, xn))
                    n += 1
        return None

    # ------------------------------------------------------------------ nets
    def route_net(self, net: Net) -> tuple[Route | None, str | None]:
        foreign = {t for t, it in self.claimed.items() if it != net.item}
        if not net.sinks:
            return self._chests(net, foreign)
        order = _chain_order(net, self.cells)
        r = Route(net.item)
        self.pending = []
        reserved: set[Tile] = set(foreign)      # inserter tiles taken by this net so far (and other feeds' tiles)
        first = self.cells[order[0]]
        start_sites = {s.outer: s for s in self._sites(first, foreign)}
        if not start_sites:
            return None, f"{net.item}: no free pin on {first.id}"
        cur_starts, cur_dir = list(start_sites), None
        for k, cid in enumerate(order[1:], start=1):
            cell = self.cells[cid]
            path_set = set(r.path)
            targets = {s.outer: s for s in self._sites(cell, path_set | reserved)}
            if not targets:
                return None, f"{net.item}: no free pin on {cid}"
            def valid(tiles, targets=targets, first_leg=(k == 1)):
                if targets[tiles[-1]].inserter in tiles:          # never over the pin it ends at
                    return False
                return not (first_leg and start_sites[tiles[0]].inserter in tiles)

            leg = self._astar(cur_starts, cur_dir, targets, path_set | reserved, valid)
            if leg is None:
                return None, f"{net.item}: no belt route from {order[k - 1]} to {cid}"
            if k == 1:                                   # the start tile picks the first cell's pin
                s0 = start_sites[leg[0][0]]
                r.pins.append(Pin(first.id, s0, into_cell=first.id in net.sinks))
                reserved.add(s0.inserter)
                r.path.append(leg[0][0])
                r.kinds[leg[0][0]] = "belt"
            for t, how in leg[1:]:
                r.path.append(t)
                r.kinds[t] = {"step": "belt", "ug_in": "ug_in", "ug_out": "ug_out"}[how]
            for a, b in zip([t for t, _ in leg], [t for t, _ in leg][1:]):
                if r.kinds.get(a) == "ug_in" and r.kinds.get(b) == "ug_out":
                    if not self._span_ok(a, b) and (a, b) not in self.pending:
                        return None, f"{net.item}: underground pairs overlap near {cid}"
                    self.pending.append((a, b))
            site = targets[leg[-1][0]]
            r.pins.append(Pin(cid, site, into_cell=cid in net.sinks))
            reserved.add(site.inserter)
            cur_starts, cur_dir = [leg[-1][0]], dir_between(leg[-2][0], leg[-1][0]) if len(leg) > 1 else None
            # an inserter must never sit on the chain itself
            if any(p.site.inserter in set(r.path) for p in r.pins):
                return None, f"{net.item}: chain crossed its own inserter near {cid}"
        self._commit(r)
        return r, None

    def _chests(self, net: Net, foreign: set[Tile] = frozenset()) -> tuple[Route | None, str | None]:
        r = Route(net.item)
        for cid in net.sources:
            sites = self._sites(self.cells[cid], set(foreign))
            if not sites:
                return None, f"{net.item}: no room for an output chest at {cid}"
            s = sites[0]
            r.pins.append(Pin(cid, s, into_cell=False))
            r.chests.append(s.outer)
            self.occ[s.inserter] = "inserter"
            self.occ[s.outer] = "chest"
        self.routes.append(r)
        return r, None

    def _commit(self, r: Route) -> None:
        path = r.path
        for i, t in enumerate(path):
            kind = r.kinds[t]
            if kind == "ug_in":
                r.facing[t] = dir_between(t, path[i + 1]) if _adjacent(t, path[i + 1]) else _axis_dir(t, path[i + 1])
            elif kind == "ug_out":
                r.facing[t] = _axis_dir(path[i - 1], t)
            elif i + 1 < len(path):
                nxt = path[i + 1]
                r.facing[t] = dir_between(t, nxt) if _adjacent(t, nxt) else _axis_dir(t, nxt)
            else:                                        # the last belt: point it at a tile with no belt
                came = dir_between(path[i - 1], t) if i > 0 and _adjacent(path[i - 1], t) else "north"
                choices = [came] + [d for d in DIRS if d not in (came, OPPOSITE[came])]
                for d in choices:
                    ahead = (t[0] + DIRS[d][0], t[1] + DIRS[d][1])
                    if (not self.occ.get(ahead, "").startswith("belt") and ahead not in path
                            and ahead not in self.grid.used):   # never into a belt, ours or one built earlier
                        r.facing[t] = d
                        self.nobelt.add(ahead)
                        break
                else:
                    r.facing[t] = came
        for k, (a, b) in enumerate(zip(path, path[1:])):
            if r.kinds[a] == "ug_in" and r.kinds[b] == "ug_out":
                self.spans.append((a, b))
                self.ug_left -= 1
        for t in path:
            self.occ[t] = f"belt:{r.item}"
        for p in r.pins:
            self.occ[p.site.inserter] = "inserter"
        self.routes.append(r)


def _adjacent(a: Tile, b: Tile) -> bool:
    return abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1


def _axis_dir(a: Tile, b: Tile) -> str:
    if a[0] == b[0]:
        return "south" if b[1] > a[1] else "north"
    return "east" if b[0] > a[0] else "west"


def _between(a: Tile, b: Tile) -> set[Tile]:
    if a[0] == b[0]:
        lo, hi = sorted((a[1], b[1]))
        return {(a[0], y) for y in range(lo + 1, hi)}
    if a[1] == b[1]:
        lo, hi = sorted((a[0], b[0]))
        return {(x, a[1]) for x in range(lo + 1, hi)}
    return set()


def _chain_order(net: Net, cells: dict[str, Cell]) -> list[str]:
    """Sources first, then sinks; each next cell the nearest to the previous one."""
    sink_c = [cells[s].centre() for s in net.sinks]
    mid = (sum(p[0] for p in sink_c) / len(sink_c), sum(p[1] for p in sink_c) / len(sink_c))
    srcs = sorted(net.sources, key=lambda s: -math.dist(cells[s].centre(), mid))   # furthest source first
    order = [srcs.pop(0)]
    for group in (srcs, list(net.sinks)):
        left = list(group)
        while left:
            last = cells[order[-1]].centre()
            nxt = min(left, key=lambda s: math.dist(cells[s].centre(), last))
            left.remove(nxt)
            order.append(nxt)
    return order


def route_all(grid: Grid, cells: list[Cell], nets: list[Net], box: tuple[int, int, int, int],
              max_ug_pairs: int = 10, passes: int = 5) -> tuple[Router, list[str]]:
    """Route every net. Rip-up and reroute: nets that failed go first on the next pass (as chip routers do)."""
    by_id = {c.id: c for c in cells}

    def span(net: Net) -> float:
        pts = [by_id[c].centre() for c in net.cells]
        return (max(p[0] for p in pts) - min(p[0] for p in pts)) + (max(p[1] for p in pts) - min(p[1] for p in pts))

    order = sorted([n for n in nets if n.sinks], key=lambda n: (-len(n.cells), -span(n)))
    chests = [n for n in nets if not n.sinks]
    best: tuple[Router, list[str]] | None = None
    for _ in range(passes):
        router = Router(grid, cells, box, max_ug_pairs)
        fails, failed_nets = [], []
        for net in order + chests:
            _, err = router.route_net(net)
            if err:
                fails.append(err)
                failed_nets.append(net)
        if best is None or len(fails) < len(best[1]):
            best = (router, fails)
        if not fails:
            break
        order = [n for n in order if n in failed_nets] + [n for n in order if n not in failed_nets]
    return best
