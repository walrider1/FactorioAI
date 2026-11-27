"""Placement: mine cells go on the ore, assemblers are arranged by simulated annealing (like a chip placer).

The cost is the half-perimeter wirelength (HPWL) of every net: for each item, the bounding box of the cells
that make it and the cells that use it. Short nets mean short belts. Cells keep a gap so the router has room
for inserters and belts between them.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from .cells import Cell, mining_area
from .grid import Grid
from .recipes import ORE_FOR, RECIPES

Tile = tuple[int, int]


@dataclass
class Net:
    item: str
    sources: list[str]
    sinks: list[str]                 # empty: the final product, which goes into a chest at each source

    @property
    def cells(self) -> list[str]:
        return self.sources + self.sinks


def build_nets(cells: list[Cell]) -> list[Net]:
    nets = []
    for item in dict.fromkeys(c.product for c in cells):
        sources = [c.id for c in cells if c.product == item]
        sinks = [c.id for c in cells if c.recipe and item in RECIPES[c.recipe][0]]
        nets.append(Net(item, sources, sinks))
    return nets


def dilate(tiles: set[Tile], r: int) -> set[Tile]:
    return {(x + dx, y + dy) for x, y in tiles for dx in range(-r, r + 1) for dy in range(-r, r + 1)}


# ------------------------------------------------------------------- mine cells
def mine_candidates(grid: Grid, ore: str, min_ore: int = 7) -> list[Cell]:
    tiles = grid.ore_tiles(ore)
    if not tiles:
        return []
    others = {t for t, n in grid.ore.items() if n != ore}
    xs, ys = [t[0] for t in tiles], [t[1] for t in tiles]
    out = []
    for x in range(min(xs) - 3, max(xs) + 4):
        for y in range(min(ys) - 3, max(ys) + 4):
            for rot in range(4):
                c = Cell("?", "mine", ore.replace("-ore", "-plate"), ore=ore, x=x, y=y, rot=rot)
                drill = c.drill_tiles()
                if len(drill & tiles) < min_ore or mining_area(c) & others:
                    continue
                fp = c.footprint()
                if not all(grid.free(t) for t in fp):
                    continue
                pins = [p for p in c.pin_sites() if grid.free(p.inserter) and grid.free(p.outer)]
                if len(pins) < 2:
                    continue
                out.append(c)
    return out


def place_mines(grid: Grid, counts: dict[str, int], focus: tuple[float, float], taken: set[Tile],
                prefix: str = "M", gap: int = 2, seed: int = 0) -> tuple[list[Cell], list[str]]:
    """Greedy, nearest the focus first. Cells keep `gap` tiles apart (an inserter and a belt fit between), and
    every cell keeps at least two usable pins, the earlier ones included. seed > 0 shuffles near-ties."""
    rng = random.Random(seed)
    placed: list[Cell] = []
    problems = []
    keep = set(taken)                  # footprints grown by the gap: no new footprint here
    solid = set(taken)                 # real footprints: no pin here

    def free_pins(c: Cell, extra: set[Tile]) -> int:
        bad = solid | extra
        return sum(1 for p in c.pin_sites() if p.inserter not in bad and p.outer not in bad)

    n = 0
    for plate, count in counts.items():
        ore = ORE_FOR[plate]
        cands = mine_candidates(grid, ore)
        jitter = {id(c): (rng.random() * 4 if seed else 0.0) for c in cands}
        cands.sort(key=lambda c: math.dist(c.centre(), focus) + 3 * len(c.footprint() & grid.obstacles)
                   + jitter[id(c)])
        got = 0
        for c in cands:
            if got >= count:
                break
            fp = c.footprint()
            if fp & keep or free_pins(c, set()) < 2:
                continue
            if any(free_pins(o, fp) < 2 for o in placed):      # don't wall in a cell placed earlier
                continue
            n += 1
            got += 1
            c.id = f"{prefix}{n}"
            placed.append(c)
            keep |= dilate(fp, gap)
            solid |= fp
        if got < count:
            problems.append(f"only {got} of {count} {plate} mine cells fit on the {ore} seen so far")
    return placed, problems


# ------------------------------------------------------------------- input cells
SIDES = {"south": 0, "west": 1, "north": 2, "east": 3}      # the rotation whose port faces that way


def place_sources(grid: Grid, counts: dict[str, int], focus: tuple[float, float], window: tuple[int, int, int, int],
                  taken: set[Tile], prefix: str = "S", feeds: frozenset | set = frozenset(),
                  side: str | None = None) -> tuple[list[Cell], list[str]]:
    """Input cells near the factory, their ports facing away from its middle, so a belt from outside reaches them
    without crossing this factory's own belts: a chest an item made elsewhere arrives in (src), or for an item in
    `feeds` a belt straight in (feed). side: every port faces that way (the side its supply comes from), so the
    links all arrive on one edge."""
    x0, y0, x1, y1 = window
    keep = dilate(set(taken), 2)
    placed: list[Cell] = []
    problems = []
    n = 0
    vec = {0: (0, 1), 1: (-1, 0), 2: (0, -1), 3: (1, 0)}       # where the port points, per rotation
    rots = [SIDES[side]] if side in SIDES else list(range(4))
    for item, count in counts.items():
        kind = "feed" if item in feeds else "src"
        cands = []
        for x in range(x0 + 4, x1 - 3):
            for y in range(y0 + 4, y1 - 3):
                for rot in rots:
                    c = Cell("?", kind, item, x=x, y=y, rot=rot)
                    fp = c.footprint()
                    if fp & keep or not all(grid.free(t) for t in fp):
                        continue
                    pins = [p for p in c.pin_sites() if grid.free(p.inserter) and grid.free(p.outer)
                            and p.inserter not in keep and p.outer not in keep]
                    if len(pins) < (1 if kind == "feed" else 2):
                        continue
                    ox, oy = x - focus[0], y - focus[1]
                    vx, vy = vec[rot]
                    if ox * vx + oy * vy <= 0:               # the port must face outward
                        continue
                    d = math.hypot(ox, oy)
                    cands.append((abs(d - 9) + 0.2 * sum(t in grid.obstacles for t in fp), c))
        cands.sort(key=lambda q: q[0])
        got = 0
        for _, c in cands:
            if got >= count:
                break
            fp = c.footprint()
            if fp & keep:
                continue
            n += 1
            got += 1
            c.id = f"{prefix}{n}"
            placed.append(c)
            keep |= dilate(fp, 2)
        if got < count:
            problems.append(f"only {got} of {count} input cells for {item} fit"
                            + (f" with their ports facing {side}" if side else ""))
    return placed, problems


# ------------------------------------------------------------------- assemblers
def _hpwl(nets: list[Net], centres: dict[str, tuple[float, float]]) -> float:
    total = 0.0
    for net in nets:
        pts = [centres[c] for c in net.cells if c in centres]
        if len(pts) < 2:
            continue
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        total += (max(xs) - min(xs)) + (max(ys) - min(ys))
    return total


def _asm_gap(a: tuple[int, int], b: tuple[int, int]) -> int:
    """Empty tiles between two 3x3 boxes along the axis where they are furthest apart."""
    gx = max(b[0] - (a[0] + 3), a[0] - (b[0] + 3), -1)
    gy = max(b[1] - (a[1] + 3), a[1] - (b[1] + 3), -1)
    return max(gx, gy)


def asm_candidates(grid: Grid, window: tuple[int, int, int, int], forbidden: set[Tile]) -> list[tuple[int, int]]:
    x0, y0, x1, y1 = window
    out = []
    for x in range(x0, x1 - 1):
        for y in range(y0, y1 - 1):
            fp = [(x + i, y + j) for i in range(3) for j in range(3)]
            if all(grid.free(t) and t not in forbidden for t in fp):
                # the ring around must be mostly usable, or no pin can reach it
                ring = [(x + i, y - 1) for i in range(3)] + [(x + i, y + 3) for i in range(3)] + \
                       [(x - 1, y + j) for j in range(3)] + [(x + 3, y + j) for j in range(3)]
                if sum(grid.free(t) and t not in forbidden for t in ring) >= 6:
                    out.append((x, y))
    return out


def place_assemblers(grid: Grid, asms: list[Cell], fixed: list[Cell], nets: list[Net],
                     window: tuple[int, int, int, int], gap: int = 3, seed: int = 1,
                     iterations: int = 4000, slots: list[tuple[int, int]] | None = None
                     ) -> tuple[list[Cell], list[str]]:
    """slots: anneal over these top-left tiles only (a slot lattice, fab/slots.py: its corridors keep belts
    room by construction), one machine per slot and no gap rule between them."""
    rng = random.Random(seed)
    forbidden = set()
    for c in fixed:
        forbidden |= dilate(c.footprint(), 2)
    cands = list(slots) if slots else asm_candidates(grid, window, forbidden)
    if len(cands) < len(asms):
        return [], [f"only {len(cands)} spots for {len(asms)} assemblers in the area seen"]
    if slots:
        gap = 0                                       # distinct slots never overlap; a shared one is gap -1
    ore_cost = {xy: sum((xy[0] + i, xy[1] + j) in grid.ore for i in range(3) for j in range(3)) * 0.5 +
               sum((xy[0] + i, xy[1] + j) in grid.obstacles for i in range(3) for j in range(3)) * 1.0
               for xy in cands}
    fixed_c = {c.id: c.centre() for c in fixed}

    def centres(pos: dict[str, tuple[int, int]]) -> dict[str, tuple[float, float]]:
        out = dict(fixed_c)
        for k, (x, y) in pos.items():
            out[k] = (x + 1.5, y + 1.5)
        return out

    def cost(pos: dict[str, tuple[int, int]]) -> float:
        return _hpwl(nets, centres(pos)) + sum(ore_cost[p] for p in pos.values())

    def fits(xy, pos, skip=None) -> bool:
        return all(_asm_gap(xy, p) >= gap for k, p in pos.items() if k != skip)

    # greedy start: the most connected first, each at the cheapest spot that fits
    pos: dict[str, tuple[int, int]] = {}
    degree = {c.id: sum(c.id in n.cells for n in nets) for c in asms}
    for c in sorted(asms, key=lambda c: -degree[c.id]):
        best = None
        for xy in cands:
            if not fits(xy, pos):
                continue
            trial = {**pos, c.id: xy}
            v = cost(trial)
            if best is None or v < best[0]:
                best = (v, xy)
        if best is None:
            return [], [f"no room for assembler {c.id} ({c.recipe}) with a {gap}-tile gap"]
        pos[c.id] = best[1]

    # anneal: move one cell nearby, or swap two
    cur = cost(pos)
    best_pos, best_cost = dict(pos), cur
    ids = list(pos)
    span = max(window[2] - window[0], window[3] - window[1])
    for it in range(iterations):
        t = 8.0 * (0.02 ** (it / iterations))            # temperature: 8 -> 0.16
        r = max(2, int(span * (1 - it / iterations) / 2))
        trial = dict(pos)
        if len(ids) > 1 and rng.random() < 0.3:
            a, b = rng.sample(ids, 2)
            trial[a], trial[b] = pos[b], pos[a]
        else:
            k = rng.choice(ids)
            x, y = pos[k]
            near = [xy for xy in rng.sample(cands, min(60, len(cands)))
                    if abs(xy[0] - x) <= r and abs(xy[1] - y) <= r]
            if not near:
                continue
            xy = rng.choice(near)
            if not fits(xy, pos, skip=k):
                continue
            trial[k] = xy
        v = cost(trial)
        if v <= cur or rng.random() < math.exp((cur - v) / t):
            pos, cur = trial, v
            if v < best_cost:
                best_pos, best_cost = dict(trial), v
    out = []
    for c in asms:
        x, y = best_pos[c.id]
        out.append(c.clone(x=x, y=y, placed=True))
    return out, []
