"""The lane template: a lattice of legal 3x3 machine slots in rows, with belt corridors between the rows.

A free floor plan packs machines so tightly that belts find no way between them (65 of 90 failed LLM designs
said "no belt route"). Slots keep that room by construction: machines stand in rows, and between two rows run

    machine row (3) | inserter row (1) | two belt lanes (2) | inserter row (1) | next machine row

so rows repeat every 7 tiles. Inside a row the slots stand 5 or 6 tiles apart: the 2-3 tile gaps hold poles
and let belts cross from one corridor to the next. A placer (the model, or the annealer) only picks which
machine goes in which slot; whatever it picks, the corridors stay open.

    lat = lattice(grid, box, fixed_cells, need=6, prefer="h")
    lat.slots[0].id, lat.slots[0].x, lat.slots[0].y      # 'P1', top-left tile
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .cells import Cell
from .grid import Grid
from .place import dilate

Tile = tuple[int, int]
ROW_PITCH = 7                    # 3 machine + 1 inserter + 2 belts + 1 inserter
COL_PITCHES = (6, 5)             # slot to slot inside a row: a 3 or 2 tile gap
EDGE = 2                         # an inserter and a belt between a slot and the box edge


@dataclass(frozen=True)
class Slot:
    id: str
    x: int                       # top-left tile
    y: int
    line: int                    # which machine row (or column, for vertical lattices), from 0
    pos: int                     # index along that row

    def footprint(self) -> set[Tile]:
        return {(self.x + i, self.y + j) for i in range(3) for j in range(3)}


@dataclass
class Lattice:
    orient: str                  # "h": machine rows run east-west, corridors between them too; "v": north-south
    pitch: int                   # slot to slot inside a row
    origin: tuple[int, int]      # top-left tile of the lattice's (0, 0) slot (may lie outside the box)
    box: tuple[int, int, int, int]
    slots: list[Slot] = field(default_factory=list)

    def by_id(self) -> dict[str, Slot]:
        return {s.id: s for s in self.slots}

    def at(self) -> dict[tuple[int, int], Slot]:
        return {(s.x, s.y): s for s in self.slots}

    def bands(self) -> set[Tile]:
        """Every box tile inside a machine row (where any slot of the lattice, legal or not, would stand)."""
        x0, y0, x1, y1 = self.box
        ox, oy = self.origin
        out = set()
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                along = (y - oy) if self.orient == "h" else (x - ox)
                if along % ROW_PITCH < 3:
                    out.add((x, y))
        return out

    def corridors(self) -> set[Tile]:
        """The box tiles between machine rows: inserter rows and belt lanes, never a slot."""
        x0, y0, x1, y1 = self.box
        every = {(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)}
        return every - self.bands()

    def describe(self, box: tuple[int, int, int, int], taken: dict[str, str] | None = None) -> str:
        """The slots as text, one line per machine row, in the map's col/row numbers. taken: slot id -> machine."""
        x0, y0 = box[0], box[1]
        lines = []
        rows: dict[int, list[Slot]] = {}
        for s in self.slots:
            rows.setdefault(s.line, []).append(s)
        for k, line in enumerate(sorted(rows), start=1):
            ss = sorted(rows[line], key=lambda s: s.pos)
            if self.orient == "h":
                head = f"slot row {k} (map rows {ss[0].y - y0}-{ss[0].y - y0 + 2}):"
                items = [f"{s.id} col {s.x - x0}" for s in ss]
            else:
                head = f"slot column {k} (map cols {ss[0].x - x0}-{ss[0].x - x0 + 2}):"
                items = [f"{s.id} row {s.y - y0}" for s in ss]
            if taken:
                items = [it + (f" [{taken[s.id]}]" if s.id in taken else "") for it, s in zip(items, ss)]
            lines.append(head + " " + ", ".join(items))
        return "\n".join(lines)


def blocked_by(fixed: list[Cell]) -> set[Tile]:
    """Tiles no slot may come within 2 of: mine/input cell footprints, and an input's port and first belt."""
    tiles: set[Tile] = set()
    for c in fixed:
        tiles |= c.footprint()
        pt = c.port() if c.kind in ("src", "feed") else None
        if pt:
            tiles |= {tuple(pt["end"]), tuple(pt["approach"])}
        if c.kind == "feed":
            tiles |= {s.outer for s in c.pin_sites()}
    return tiles


def feed_orient(fixed: list[Cell]) -> str | None:
    """Rows that run toward the input ports: belts from an east/west port run along east-west corridors."""
    faces = [c.port()["face"] for c in fixed if c.kind in ("src", "feed")]
    if not faces:
        return None
    ew = sum(f in ("east", "west") for f in faces)
    return "h" if ew * 2 >= len(faces) else "v"


def _slot_ok(grid: Grid, x: int, y: int, forbidden: set[Tile]) -> bool:
    for i in range(3):
        for j in range(3):
            t = (x + i, y + j)
            if not grid.free(t) or t in forbidden:
                return False
    ring = [(x + i, y - 1) for i in range(3)] + [(x + i, y + 3) for i in range(3)] + \
           [(x - 1, y + j) for j in range(3)] + [(x + 3, y + j) for j in range(3)]
    return sum(grid.free(t) and t not in forbidden for t in ring) >= 6


def _build(grid: Grid, box, forbidden, orient: str, pitch: int, ox: int, oy: int) -> Lattice:
    """One lattice: slots every `pitch` along a row, rows every ROW_PITCH, offset (ox along, oy across)."""
    x0, y0, x1, y1 = box
    if orient == "h":
        origin = (x0 + ox, y0 + oy)
        span_along, span_across = (x0, x1), (y0, y1)
    else:
        origin = (x0 + oy, y0 + ox)
        span_along, span_across = (y0, y1), (x0, x1)
    lat = Lattice(orient, pitch, origin, box)
    a0 = origin[0] if orient == "h" else origin[1]
    c0 = origin[1] if orient == "h" else origin[0]
    line = 0
    c = c0
    while c + 2 <= span_across[1] - EDGE:
        if c >= span_across[0] + EDGE:
            pos = 0
            a = a0
            while a + 2 <= span_along[1] - EDGE:
                if a >= span_along[0] + EDGE:
                    x, y = (a, c) if orient == "h" else (c, a)
                    if _slot_ok(grid, x, y, forbidden):
                        lat.slots.append(Slot("", x, y, line, pos))
                    pos += 1
                a += pitch
            line += 1
        c += ROW_PITCH
    return lat


def lattice(grid: Grid, box: tuple[int, int, int, int], fixed: list[Cell], need: int,
            prefer: str | None = None, anchor: tuple[float, float] | None = None) -> Lattice | None:
    """The best slot lattice for `need` machines in this box, or None when no lattice holds that many.

    Tries both orientations, both in-row pitches and every offset; keeps the one with enough slots (a few to
    spare), rows facing the input ports (prefer: "h" | "v", default from the input cells), the roomier pitch,
    and the slots nearest the anchor (the input cells, else the box centre)."""
    forbidden = dilate(blocked_by(fixed), 2)
    prefer = prefer or feed_orient(fixed)
    if anchor is None:
        ins = [c.centre() for c in fixed if c.kind in ("src", "feed")] or [c.centre() for c in fixed]
        anchor = ((sum(p[0] for p in ins) / len(ins), sum(p[1] for p in ins) / len(ins)) if ins else
                  ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2))
    best, best_key = None, None
    for orient in ("h", "v"):
        for pitch in COL_PITCHES:
            for oy in range(ROW_PITCH):
                for ox in range(pitch):
                    lat = _build(grid, box, forbidden, orient, pitch, ox, oy)
                    n = len(lat.slots)
                    if n < need:
                        continue
                    near = sorted(math.dist((s.x + 1.5, s.y + 1.5), anchor) for s in lat.slots)[:max(need, 1)]
                    key = (min(n, need + 3), orient == prefer, pitch, -sum(near) / len(near))
                    if best_key is None or key > best_key:
                        best, best_key = lat, key
    if best is None:
        return None
    # ids in reading order: row by row, then along the row
    best.slots.sort(key=lambda s: (s.line, s.pos))
    best.slots = [Slot(f"P{k}", s.x, s.y, s.line, s.pos) for k, s in enumerate(best.slots, start=1)]
    return best
