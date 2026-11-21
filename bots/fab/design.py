"""Design a factory for a target item and rate: plan -> place -> route -> power, with retries.

    d = design_factory(grid, "logistic-science-pack", 10)
    print(d.render())          # the floor plan as text
    d.ok, d.problems, d.cost   # a design is ok when every net is routed and every machine powered
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .cells import Cell
from .grid import Grid
from .place import Net, build_nets, place_assemblers, place_mines, place_sources
from .power import plan_poles
from .recipes import ORE_FOR, Plan, plan
from .route import Route, route_all

Tile = tuple[int, int]
LETTER = {"iron-gear-wheel": "G", "copper-cable": "W", "electronic-circuit": "E", "inserter": "N",
          "transport-belt": "B", "automation-science-pack": "R", "logistic-science-pack": "L"}
ORE_CHAR = {"iron-ore": "i", "copper-ore": "c", "coal": "k", "stone": "s"}
ARROW = {"north": "^", "east": ">", "south": "v", "west": "<"}


@dataclass
class Design:
    target: str
    per_minute: float
    plan: Plan
    cells: list[Cell] = field(default_factory=list)
    nets: list[Net] = field(default_factory=list)
    routes: list[Route] = field(default_factory=list)
    poles: list[Tile] = field(default_factory=list)
    box: tuple[int, int, int, int] = (0, 0, 0, 0)
    problems: list[str] = field(default_factory=list)
    placer: str = "search"
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def belts(self) -> int:
        return sum(1 for r in self.routes for k in r.kinds.values() if k == "belt")

    @property
    def undergrounds(self) -> int:
        return sum(1 for r in self.routes for k in r.kinds.values() if k != "belt")

    @property
    def cost(self) -> float:
        """Lower is better: belts, undergrounds, poles, and a big penalty for anything unsolved."""
        return self.belts + 3 * self.undergrounds + 2 * len(self.poles) + 1000 * len(self.problems)

    def inserters(self) -> list[tuple[Tile, str]]:
        feeds = self.feed_ids()                  # a feed's pin is the link's end tile: nothing is built there
        return [(p.site.inserter, p.site.inserter_dir(p.into_cell)) for r in self.routes for p in r.pins
                if p.cell not in feeds]

    def ports(self) -> list[dict]:
        """The input cells' ports: where belts from other factories deliver (steel, plastic, sulfur...), into a
        chest (src) or straight onto this factory's belt (feed)."""
        return [c.port() for c in self.cells if c.kind in ("src", "feed")]

    def feed_ids(self) -> set[str]:
        return {c.id for c in self.cells if c.kind == "feed"}

    def kit(self) -> dict[str, int]:
        """Buildings this design uses."""
        out: dict[str, int] = {}
        for c in self.cells:
            for e in c.entities():
                out[e.name] = out.get(e.name, 0) + 1
        # added to what the cells hold, never in place of it: input cells have a chest and a port inserter of
        # their own (run 20261119-112905: every plate-fed factory was built two pieces short per input cell, so
        # plates sat in the mine chests while its assemblers starved)
        out["inserter"] = out.get("inserter", 0) + len(self.inserters())
        out["transport-belt"] = self.belts
        out["underground-belt"] = self.undergrounds
        out["iron-chest"] = out.get("iron-chest", 0) + sum(len(r.chests) for r in self.routes)
        out["small-electric-pole"] = len(self.poles)
        return {k: v for k, v in out.items() if v}

    def tiles(self) -> set[Tile]:
        """Every tile the design builds on (cells, belts, chests, poles)."""
        out = {t for c in self.cells for t in c.footprint()}
        for r in self.routes:
            out |= set(r.path) | set(r.chests)
        out |= {t for t, _ in self.inserters()} | set(self.poles)
        return out

    def summary(self) -> str:
        s = (f"{self.plan.summary()} | placer {self.placer} | {self.belts} belts, {self.undergrounds} underground, "
             f"{len(self.inserters())} inserters, {len(self.poles)} poles")
        return s + (f" | PROBLEMS: {'; '.join(self.problems)}" if self.problems else " | ok")

    def render(self, grid: Grid | None = None) -> str:
        """The floor plan as text: one character per tile."""
        x0, y0, x1, y1 = self.box
        canvas: dict[Tile, str] = {}
        if grid is not None:
            for x in range(x0, x1 + 1):
                for y in range(y0, y1 + 1):
                    t = (x, y)
                    if t not in grid.seen:
                        canvas[t] = " "
                    elif t in grid.water:
                        canvas[t] = "~"
                    elif t in grid.cliffs or t in grid.used:
                        canvas[t] = "#"
                    elif t in grid.ore:
                        canvas[t] = ORE_CHAR.get(grid.ore[t], "o")
                    elif t in grid.obstacles:
                        canvas[t] = "T"
        for c in self.cells:
            if c.kind == "asm":
                for t in c.footprint():
                    canvas[t] = LETTER.get(c.recipe or "", "A")
            elif c.kind == "src":
                pt = c.port()
                canvas[pt["chest"]], canvas[pt["inserter"]] = "S", "i"
                canvas[pt["end"]] = canvas[pt["approach"]] = ":"
            elif c.kind == "feed":
                pt = c.port()
                canvas[pt["end"]] = canvas[pt["approach"]] = ":"
            else:
                for t in c.drill_tiles():
                    canvas[t] = "D"
                for t in c.footprint() - c.drill_tiles():
                    canvas[t] = "F" if c.ore == "iron-ore" else "f"
        for r in self.routes:
            for t in r.path:
                kind = r.kinds[t]
                canvas[t] = ARROW[r.facing[t]] if kind == "belt" else ("U" if kind == "ug_in" else "u")
            for t in r.chests:
                canvas[t] = "C"
        for t, _ in self.inserters():
            canvas[t] = "I"
        for t in self.poles:
            canvas[t] = "P"
        rows = []
        for y in range(y0, y1 + 1):
            rows.append(f"{y:5d} " + "".join(canvas.get((x, y), ".") for x in range(x0, x1 + 1)))
        head = f"      x from {x0} to {x1}"
        legend = ("legend: G gear, W cable, E circuit, N inserter-asm, B belt-asm, L green, R red, D drill, F/f iron/copper "
                  "furnace, I inserter, C chest, P pole, ^>v< belt, U/u underground in/out, i/c/k/s ore, T tree, ~ water, "
                  "# built")
        return "\n".join([head] + rows + [legend])


def _focus(grid: Grid, near: tuple[float, float] | None) -> tuple[float, float]:
    """Between the iron and copper nearest to `near` (or the snapshot origin)."""
    here = near or grid.origin
    pts = []
    for ore in ("iron-ore", "copper-ore"):
        tiles = grid.ore_tiles(ore)
        if tiles:
            t = min(tiles, key=lambda t: math.dist((t[0] + 0.5, t[1] + 0.5), here))
            pts.append((t[0] + 0.5, t[1] + 0.5))
    if not pts:
        return here
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def _box_around(cells: list[Cell], focus: tuple[float, float], margin: int, grid: Grid) -> tuple[int, int, int, int]:
    xs = [int(focus[0])] + [t[0] for c in cells for t in c.footprint()]
    ys = [int(focus[1])] + [t[1] for c in cells for t in c.footprint()]
    sx = [t[0] for t in grid.seen] or xs
    sy = [t[1] for t in grid.seen] or ys
    return (max(min(xs) - margin, min(sx)), max(min(ys) - margin, min(sy)),
            min(max(xs) + margin, max(sx)), min(max(ys) + margin, max(sy)))


UG_JUMP = 5                     # an underground belt reaches 5 tiles: up to 4 in between


def port_escapes(grid: Grid, d: "Design", underground: bool = True) -> dict[tuple, list | None]:
    """For each input port (by its approach tile): the tiles a belt from outside the design's box takes to reach
    it (over free ground, or under the floor with underground belts), or None when there is no way in."""
    ports = d.ports()
    out: dict[tuple, list | None] = {}
    if not ports:
        return out
    taken = set(d.tiles())
    for pt in ports:
        taken |= {tuple(pt[k]) for k in ("chest", "inserter") if pt.get(k) is not None}
    x0, y0, x1, y1 = d.box
    lo_x, lo_y, hi_x, hi_y = x0 - 6, y0 - 6, x1 + 6, y1 + 6
    ground = lambda t: (t in grid.seen and t not in grid.water and t not in grid.cliffs and t not in grid.used
                        and lo_x <= t[0] <= hi_x and lo_y <= t[1] <= hi_y)
    outside = lambda t: t[0] < x0 - 1 or t[0] > x1 + 1 or t[1] < y0 - 1 or t[1] > y1 + 1
    for pt in ports:
        mine = {tuple(pt["end"]), tuple(pt["approach"])}
        others = {tuple(q[k]) for q in ports if q is not pt for k in ("end", "approach")}
        free = lambda t: t in mine or (ground(t) and t not in taken and t not in others)
        start = tuple(pt["approach"])
        parent: dict = {start: None}
        queue, head, found = [start], 0, None
        while head < len(queue) and found is None:      # breadth first: the shortest way out
            t = queue[head]
            head += 1
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nxt = (t[0] + dx, t[1] + dy)
                if not free(nxt):
                    continue
                hops = [nxt] + ([(nxt[0] + dx * k, nxt[1] + dy * k) for k in range(2, UG_JUMP + 1)]
                                if underground else [])
                for q in hops:                        # a plain belt to nxt, or an underground from nxt to q
                    if q in parent or not free(q):
                        continue
                    parent[q] = t if q == nxt else nxt
                    if q != nxt and nxt not in parent:
                        parent[nxt] = t
                    if outside(q):
                        found = q
                        break
                    queue.append(q)
                if found is not None:
                    break
        if found is None:
            out[start] = None
            continue
        path, t = [], found
        while t is not None:
            path.append(t)
            t = parent[t]
        out[start] = path[::-1]
    return out


def port_access_problems(grid: Grid, d: "Design", underground: bool = True) -> list[str]:
    """Input ports a belt from outside cannot reach (see port_escapes). The design's own buildings hemmed in 20
    of the 22 ports that never got a belt in run 20261119-202811, and those factories starved until they were
    torn down and built again (seven of them, ~1900 buildings)."""
    return [f"the input port at {list(a)} is walled in by the factory itself: no belt from outside reaches it"
            for a, path in port_escapes(grid, d, underground).items() if path is None]


def design_factory(grid: Grid, target: str, per_minute: float, near: tuple[float, float] | None = None,
                   placer: str = "search", llm=None, log=lambda *a: None, seed: int = 1,
                   max_ug_pairs: int = 10, anchors: list[Tile] = (), margin: int = 14,
                   max_attempts: int | None = None, supplied: frozenset | set = frozenset(),
                   feeds: frozenset | set = frozenset(), feed_side: str | None = None) -> Design:
    """placer: 'search' (simulated annealing), 'llm' (a model draws the floor plan), 'best' (both, keep the cheaper).
    margin: tiles of room around the mine cells for assemblers and belts (more room routes more easily).
    max_attempts: cap on the re-placements tried (an LLM placer pays a model call or three for each).
    supplied: items other factories deliver (steel, plastic...): each gets input cells with a port for a belt.
    feeds: supplied items that come in on a belt straight onto this factory's own (one feed per item, not one
    chest per furnace's worth); feed_side: the edge every input port faces (where the supply comes from)."""
    if placer == "best":
        a = design_factory(grid, target, per_minute, near, "search", llm, log, seed, max_ug_pairs, anchors, margin)
        b = design_factory(grid, target, per_minute, near, "llm", llm, log, seed, max_ug_pairs, anchors, margin)
        log(f"[design] search cost {a.cost:.0f}, llm cost {b.cost:.0f}")
        return a if a.cost <= b.cost else b
    p = plan(target, per_minute, supplied=set(supplied), feeds=set(feeds) & set(supplied))
    # a factory with its own mines centres between iron and copper; one fed from outside goes where it was asked
    # (run 20261119-112905: plate-fed factories asked for open ground were pulled 40 tiles back to the ore)
    own_mines = any(k in ORE_FOR and k not in p.supplied for k in p.machines)
    focus = _focus(grid, near) if own_mines or near is None else (float(near[0]), float(near[1]))
    d = Design(target, per_minute, p, placer=placer)
    mine_counts = {k: v for k, v in p.machines.items() if k in ORE_FOR and k not in p.supplied}   # plates fed in: no mines
    src_counts = {k: v for k, v in p.machines.items() if k in p.supplied}
    asms = []
    for recipe, n in p.machines.items():
        if recipe in ORE_FOR or recipe in p.supplied:
            continue
        for _ in range(n):
            asms.append(Cell(f"A{len(asms) + 1}", "asm", recipe, recipe=recipe))
    best: Design | None = None
    # (gap between mine cells, gap between assemblers, seed): each attempt re-places everything
    attempts = ([(2, 3, seed + k) for k in range(4)] + [(2, 4, seed + 4 + k) for k in range(3)] +
                [(3, 4, seed + 7 + k) for k in range(3)])[:max_attempts]   # None: all of them
    for mine_gap, gap, s in attempts:
        cand = Design(target, per_minute, p, placer=placer, notes=list(d.notes))
        mines, probs = place_mines(grid, mine_counts, focus, set(), gap=mine_gap, seed=s - seed)
        box = _box_around(mines, focus, margin, grid)
        if src_counts and not probs:                 # inputs from other factories, ports facing outward
            srcs, sprobs = place_sources(grid, src_counts, focus, box, {t for c in mines for t in c.footprint()},
                                         feeds=set(feeds), side=feed_side)
            if sprobs and feed_side:                 # no room with every port on that side: any side then
                srcs, sprobs = place_sources(grid, src_counts, focus, box,
                                             {t for c in mines for t in c.footprint()}, feeds=set(feeds))
            mines, probs = mines + srcs, sprobs
        cand.cells, cand.box = list(mines), box
        if probs:
            cand.problems = probs
            log(f"[design] attempt {mine_gap}/{gap}/{s}: {probs[0]}")
            if best is None or cand.cost < best.cost:
                best = cand
            continue
        nets = build_nets(mines + asms)
        cand.nets = nets
        if placer == "llm":
            from .llm_place import llm_place_assemblers
            fb = [n[len("routing failed: "):] for n in d.notes if n.startswith("routing failed: ")][-1:]
            placed, probs, notes = llm_place_assemblers(llm, grid, asms, mines, nets, box, gap, feedback=fb, log=log)
            d.notes += notes
            cand.notes = list(d.notes)
            if probs and probs[0].startswith(("designer unavailable", "no LLM")):
                log(f"[design] {probs[0]}; using the search placer")
                placed, probs = place_assemblers(grid, asms, mines, nets, box, gap=gap, seed=s)
                placer = cand.placer = "search"
        else:
            placed, probs = place_assemblers(grid, asms, mines, nets, box, gap=gap, seed=s)
        cand.cells = mines + placed
        if probs:
            cand.problems = probs
        else:
            router, fails = route_all(grid, cand.cells, nets, box, max_ug_pairs)
            cand.routes = router.routes
            cand.problems = fails
            if not fails:
                occupied = set(router.occ) | router.nobelt
                consumers = [set(c.footprint()) for c in cand.cells if c.kind == "asm"]
                consumers += [set(c.drill_tiles()) for c in cand.cells if c.kind == "mine"]
                consumers += [{t} for t, _ in cand.inserters()]
                consumers += [{c.port()["inserter"]} for c in cand.cells if c.kind == "src"]   # input ports
                poles, pprobs = plan_poles(grid, consumers, occupied, box, list(anchors))
                cand.poles, cand.problems = poles, pprobs
                if not pprobs:
                    cand.problems = port_access_problems(grid, cand, underground=max_ug_pairs > 0)
        log(f"[design] attempt {mine_gap}/{gap}/{s}: {cand.summary()}")
        if best is None or cand.cost < best.cost:
            best = cand
        if cand.ok:
            break
        if placer == "llm" and not probs:
            d.notes.append("routing failed: " + "; ".join(cand.problems))
    return best
