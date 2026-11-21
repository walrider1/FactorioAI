"""Chiplets: small fixed building cells with pins, which the placer arranges and the router wires up.

Coordinates are tiles (x grows east, y grows south). A tile (tx, ty) has its centre at (tx + 0.5, ty + 0.5).
A cell is defined facing north at the origin, then rotated (clockwise, in quarter turns) and moved.

Cells:
- mine: an electric drill on ore that drops straight into a stone furnace; plates leave through a pin.
- asm:  one 3x3 assembling machine; any of its 12 edge tiles can take a pin.
- src:  an input: a chest where an item made elsewhere arrives (by a belt from another factory into its port, or
        the courier until that belt exists), with pins that lift it onto this factory's belts. Its port (an
        inserter into the chest, the belt's end tile and the tile a belt approaches from) is part of its footprint,
        so nothing here is built where the link must come in.
- feed: an input that is a belt: the link from outside ends on the feed's end tile, headed inward, and runs on
        into this factory's own belt of that item (its first tile is the pin's belt tile). No chest and no
        inserter in between, so one feed carries a whole lane (docs/logistics-plan.md, phase 2). Nothing is built
        for it: the end tile is the pin's "inserter" tile (kept free, never built), the approach is its footprint.

A pin is an inserter tile next to the cell plus the tile beyond it, in a straight line: a belt tile
(or, for the final product, a chest). The inserter faces the side it picks up from (this API's rule).
"""

from __future__ import annotations

from dataclasses import dataclass, field

Tile = tuple[int, int]
DIR_ORDER = ("north", "east", "south", "west")
DIRS = {"north": (0, -1), "east": (1, 0), "south": (0, 1), "west": (-1, 0)}
OPPOSITE = {"north": "south", "south": "north", "east": "west", "west": "east"}


def rot_point(x: float, y: float, r: int) -> tuple[float, float]:
    """Rotate a point about the origin by r quarter turns clockwise (north -> east)."""
    for _ in range(r % 4):
        x, y = -y, x
    return x, y


def rot_tile(t: Tile, r: int) -> Tile:
    x, y = rot_point(t[0] + 0.5, t[1] + 0.5, r)
    return round(x - 0.5), round(y - 0.5)


def rot_dir(d: str, r: int) -> str:
    return DIR_ORDER[(DIR_ORDER.index(d) + r) % 4]


def dir_between(a: Tile, b: Tile) -> str:
    v = (b[0] - a[0], b[1] - a[1])
    return next(k for k, w in DIRS.items() if w == v)


@dataclass(frozen=True)
class Ent:
    """One building to place: name, centre position, facing."""
    name: str
    x: float
    y: float
    direction: str = "north"
    tiles: frozenset = frozenset()


@dataclass(frozen=True)
class PinSite:
    inserter: Tile
    outer: Tile              # belt tile (or the chest tile for a final product)
    machine: Tile            # the cell tile the inserter reaches into

    def inserter_dir(self, into_cell: bool) -> str:
        """Facing for the inserter: the side it picks up from (the cell for an output, the belt for an input)."""
        towards_cell = dir_between(self.inserter, self.machine)
        return OPPOSITE[towards_cell] if into_cell else towards_cell


def _box(x0: int, y0: int, w: int, h: int) -> list[Tile]:
    return [(x, y) for x in range(x0, x0 + w) for y in range(y0, y0 + h)]


# ------------------------------------------------------------------ local (north-facing) shapes
_MINE_DRILL = _box(0, 0, 3, 3)
_MINE_FURNACE = _box(0, -2, 2, 2)
_MINE_PINS = [((0, -3), (0, -4), (0, -2)), ((1, -3), (1, -4), (1, -2)),        # north of the furnace
              ((-1, -2), (-2, -2), (0, -2)), ((-1, -1), (-2, -1), (0, -1)),    # west
              ((2, -2), (3, -2), (1, -2)), ((2, -1), (3, -1), (1, -1))]        # east (above the drill's corner)
_SRC = [(0, 0), (0, 1), (0, 2), (0, 3)]              # chest, port inserter, port belt end, approach
_SRC_PINS = [((0, -1), (0, -2), (0, 0)), ((-1, 0), (-2, 0), (0, 0)), ((1, 0), (2, 0), (0, 0))]
_FEED = [(0, 2)]                                      # the approach a link arrives over
_FEED_PINS = [((0, 1), (0, 0), (0, 2))]               # the link's end tile, then the factory's first belt
_ASM = _box(0, 0, 3, 3)
_ASM_PINS = ([((k, -1), (k, -2), (k, 0)) for k in range(3)] + [((k, 3), (k, 4), (k, 2)) for k in range(3)] +
             [((-1, k), (-2, k), (0, k)) for k in range(3)] + [((3, k), (4, k), (2, k)) for k in range(3)])


@dataclass
class Cell:
    """A placed (or to-be-placed) chiplet."""
    id: str
    kind: str                         # "mine" | "asm" | "src" | "feed"
    product: str                      # item it makes (a plate for mine cells, the recipe's product for asm)
    recipe: str | None = None
    ore: str | None = None
    x: int = 0
    y: int = 0
    rot: int = 0
    placed: bool = False
    fixed: bool = False               # placed by an earlier step, the placer must not move it
    meta: dict = field(default_factory=dict)

    # ---- geometry
    def _t(self, t: Tile) -> Tile:
        rx, ry = rot_tile(t, self.rot)
        return rx + self.x, ry + self.y

    def footprint(self) -> set[Tile]:
        local = (_MINE_DRILL + _MINE_FURNACE if self.kind == "mine" else _SRC if self.kind == "src"
                 else _FEED if self.kind == "feed" else _ASM)
        return {self._t(t) for t in local}

    def drill_tiles(self) -> set[Tile]:
        return {self._t(t) for t in _MINE_DRILL} if self.kind == "mine" else set()

    def pin_sites(self) -> list[PinSite]:
        local = (_MINE_PINS if self.kind == "mine" else _SRC_PINS if self.kind == "src"
                 else _FEED_PINS if self.kind == "feed" else _ASM_PINS)
        return [PinSite(self._t(i), self._t(o), self._t(m)) for i, o, m in local]

    def centre(self) -> tuple[float, float]:
        fp = self.footprint()
        return (sum(t[0] for t in fp) / len(fp) + 0.5, sum(t[1] for t in fp) / len(fp) + 0.5)

    def port(self) -> dict | None:
        """An input cell's port: where another factory's belt delivers into its chest (src), or runs on into
        this factory's belt (feed: no chest, no inserter)."""
        if self.kind == "feed":
            end, appr = self._t((0, 1)), self._t((0, 2))
            return {"chest": None, "inserter": None, "face": rot_dir("south", self.rot), "end": end,
                    "approach": appr, "item": self.product, "feed": True}
        if self.kind != "src":
            return None
        chest, ins, end, appr = (self._t(t) for t in _SRC)
        return {"chest": chest, "inserter": ins, "face": rot_dir("south", self.rot), "end": end, "approach": appr,
                "item": self.product}

    def entities(self) -> list[Ent]:
        if self.kind == "feed":
            return []                                 # the link's belt and this factory's belt are all there is
        if self.kind == "src":
            pt = self.port()
            return [Ent("iron-chest", pt["chest"][0] + 0.5, pt["chest"][1] + 0.5, "north", frozenset({pt["chest"]})),
                    Ent("inserter", pt["inserter"][0] + 0.5, pt["inserter"][1] + 0.5, pt["face"],
                        frozenset({pt["inserter"]}))]
        if self.kind == "asm":
            return [Ent("assembling-machine-1", self.x + 1.5, self.y + 1.5, "north", frozenset(self.footprint()))]
        dx, dy = rot_point(1.5, 1.5, self.rot)
        fx, fy = rot_point(1.0, -1.0, self.rot)
        return [Ent("electric-mining-drill", dx + self.x, dy + self.y, rot_dir("north", self.rot),
                    frozenset(self.drill_tiles())),
                Ent("stone-furnace", fx + self.x, fy + self.y, "north",
                    frozenset(self.footprint() - self.drill_tiles()))]

    def furnace(self) -> Ent | None:
        return self.entities()[1] if self.kind == "mine" else None

    def clone(self, **kw) -> "Cell":
        c = Cell(self.id, self.kind, self.product, self.recipe, self.ore, self.x, self.y, self.rot,
                 self.placed, self.fixed, dict(self.meta))
        for k, v in kw.items():
            setattr(c, k, v)
        return c


def mining_area(cell: Cell) -> set[Tile]:
    """The 5x5 tiles an electric drill mines (its 3x3 plus one ring)."""
    xs = [t[0] for t in cell.drill_tiles()]
    ys = [t[1] for t in cell.drill_tiles()]
    return {(x, y) for x in range(min(xs) - 1, max(xs) + 2) for y in range(min(ys) - 1, max(ys) + 2)}
