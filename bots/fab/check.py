"""An independent design-rule check (DRC), the way a chip flow checks a layout before tape-out.

It knows nothing about how the design was made; it only checks the physics the game will apply.
Returns a list of violations (empty = clean).
"""

from __future__ import annotations

import math

from .cells import DIRS, mining_area
from .grid import Grid
from .route import OUT_CELL
from .power import SUPPLY, WIRE, _components

Tile = tuple[int, int]


def check(design, grid: Grid) -> list[str]:
    errs: list[str] = []
    owner: dict[Tile, str] = {}

    def claim(t: Tile, who: str) -> None:
        if t in owner:
            errs.append(f"overlap at {t}: {owner[t]} and {who}")
        owner[t] = who
        if not grid.free(t):
            errs.append(f"{who} on a blocked tile {t}")

    cells = {c.id: c for c in design.cells}
    for c in design.cells:
        for t in c.footprint():
            claim(t, f"cell {c.id}")
        if c.kind == "mine":
            ore = grid.ore_tiles(c.ore)
            if len(c.drill_tiles() & ore) < 7:
                errs.append(f"{c.id}: drill not on enough {c.ore}")
            foreign = {t for t in mining_area(c) if t in grid.ore and grid.ore[t] != c.ore}
            if foreign:
                errs.append(f"{c.id}: mining area has other ore {sorted(foreign)[:3]}")
    belt_of: dict[Tile, tuple[str, str, str]] = {}          # tile -> (item, kind, facing)
    for r in design.routes:
        for t in r.path:
            claim(t, f"belt {r.item}")
            belt_of[t] = (r.item, r.kinds[t], r.facing[t])
        for t in r.chests:
            claim(t, f"chest {r.item}")
    ins_tiles = []
    for r in design.routes:
        for p in r.pins:
            s = p.site
            claim(s.inserter, f"inserter {r.item}->{p.cell}")
            ins_tiles.append(s.inserter)
            if p.cell == OUT_CELL:                   # the product belt's end: belt tile, inserter, chest in a line
                line = (s.machine[0] - s.inserter[0], s.machine[1] - s.inserter[1])
                if not r.path or s.machine != r.path[-1] or abs(line[0]) + abs(line[1]) != 1 or \
                        (s.inserter[0] - s.outer[0], s.inserter[1] - s.outer[1]) != line or s.outer not in r.chests:
                    errs.append(f"{r.item}: the output chest is not fed from the end of its belt")
                continue
            c = cells[p.cell]
            if s.machine not in c.footprint():
                errs.append(f"pin of {p.cell} does not reach the cell")
            line = (s.machine[0] - s.inserter[0], s.machine[1] - s.inserter[1])
            if (s.inserter[0] - s.outer[0], s.inserter[1] - s.outer[1]) != line or abs(line[0]) + abs(line[1]) != 1:
                errs.append(f"pin of {p.cell} is not a straight machine-inserter-belt line")
            if r.chests and not r.path:
                if s.outer not in r.chests:
                    errs.append(f"{p.cell}: product pin does not end at a chest")
            elif belt_of.get(s.outer, ("",))[0] != r.item or belt_of[s.outer][1] != "belt":
                errs.append(f"{p.cell}: pin for {r.item} does not touch its own belt")
            needs = c.recipe and r.item in __import__("fab.recipes", fromlist=["RECIPES"]).RECIPES[c.recipe][0]
            if p.into_cell != bool(needs):
                errs.append(f"{p.cell}: inserter for {r.item} points the wrong way")
    # belt physics
    for r in design.routes:
        path = r.path
        for i, t in enumerate(path):
            item, kind, face = belt_of[t]
            dx, dy = DIRS[face]
            ahead = (t[0] + dx, t[1] + dy)
            if kind == "ug_in":
                nxt = path[i + 1]
                if belt_of.get(nxt, ("", ""))[1] != "ug_out":
                    errs.append(f"{item}: underground entrance {t} without an exit")
                    continue
                dist = abs(nxt[0] - t[0]) + abs(nxt[1] - t[1])
                if not (nxt[0] == t[0] or nxt[1] == t[1]) or dist > 5 or belt_of[nxt][2] != face:
                    errs.append(f"{item}: underground {t}->{nxt} misaligned or too long")
                lo_x, hi_x = sorted((t[0], nxt[0]))
                lo_y, hi_y = sorted((t[1], nxt[1]))
                for u, (it2, k2, f2) in belt_of.items():        # same facing in the span: it would pair wrongly
                    if (k2 != "belt" and u not in (t, nxt) and lo_x <= u[0] <= hi_x and lo_y <= u[1] <= hi_y
                            and f2 == face):
                        errs.append(f"{item}: another underground {u} inside the span {t}->{nxt}")
                continue
            if i + 1 < len(path):
                if kind == "belt" and ahead != path[i + 1]:
                    errs.append(f"{item}: belt {t} faces {face} but the chain continues at {path[i + 1]}")
                if kind == "ug_out" and ahead != path[i + 1]:
                    errs.append(f"{item}: underground exit {t} not followed straight on")
            if ahead in belt_of and belt_of[ahead][0] != item:
                errs.append(f"{item}: belt {t} feeds into the {belt_of[ahead][0]} belt at {ahead}")
            if i + 1 == len(path) and ahead in belt_of:
                errs.append(f"{item}: chain end {t} points into a belt at {ahead}")
    # power
    poles = list(design.poles)
    for t in poles:
        claim(t, "pole")

    def powered(tiles) -> bool:
        return any(max(abs(t[0] - p[0]), abs(t[1] - p[1])) <= SUPPLY for t in tiles for p in poles)
    for c in design.cells:
        tiles = c.footprint() if c.kind == "asm" else c.drill_tiles()
        if not powered(tiles):
            errs.append(f"{c.id} has no power")
    for t in ins_tiles:
        if not powered([t]):
            errs.append(f"inserter {t} has no power")
    if poles and len(set(_components(poles).values())) > 1:
        errs.append("poles form more than one network")
    if poles and math.inf in [WIRE]:
        pass
    return errs
