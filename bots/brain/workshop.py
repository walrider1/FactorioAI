"""The workshop: any item in the game, made in cells on one growing floor, fed and emptied by the courier.

A cell is one machine with an input chest and inserter on its west side and an output inserter and chest on its
east side (refineries have only pipes). Fluids come through pipes: one net per fluid, routed around the cells by
pipes.py, from an offshore pump (water), the pumpjacks (crude oil) and the cells that make them (refineries,
chemical plants). The production planner (production.py) says which cells an item at a rate needs; the courier
(Crew.courier) carries items between chests. No belts, no main bus: growing the base is adding cells.

The floor, seen from above (one slot of a band; bands repeat every 11 rows, corridors between them):

    corridor   . . . . . . . . .    pipes, pipe-to-ground; the middle row is a walkway (nothing built on it)
    band row 0 . . p . p . . . .    p: port targets (pipes join the machine here)
    band row 1 . P M M M P . . .    P: poles
    band row 2 C I M M M I C . .    C chest, I inserter, M machine (3x3; 2x2 furnaces, 5x5 refineries)
    band row 3 . . M M M . . . .
    band row 4 . . p . p . . . .
    corridor   . . . . . . . . .

    {"do": "build", "module": "workshop", "item": "chemical-science-pack", "per_minute": 10}
    {"do": "build", "module": "silo"}              # the rocket silo, fed like a cell; rockets launch on their own
"""

from __future__ import annotations

import math
import time

import automation_bot as ab

from .modules import Module, NoRoom, Site, SteamPower
from .pipes import NAME, PUMP_EVERY, Nets, add_pumps, route, route_length
from .production import Planner, SUPPLIED
from .world import BuildError, FreeWorld

Tile = tuple[int, int]
CW, BH, CH = 9, 5, 8                   # slot width, band height, corridor height (walkway in its middle:
                                       # 4 rows above it and 3 below, room for several fluid lanes)
PITCH = BH + CH
POLE = "small-electric-pole"


def inserter_kind(kn) -> str:
    return "fast-inserter" if "fast-inserter" in kn.unlocked else "inserter"


def cell_layout(kn, machine: str, recipe: str | None, sx: int, b0: int) -> dict:
    """Entities and ports of one cell in the slot at column x `sx`, band top row `b0` (machine facing north)."""
    w, h = kn.size(machine)
    ents: list[tuple[str, float, float, str]] = []
    chests_in, chests_out = [], []
    belt = None                                    # a belt port into the input chest: (inserter, end, approach)
    if w == 5:                                     # a refinery: pipes only
        cx, cy = sx + 3.5, b0 + 2.5
        ents.append((machine, cx, cy, "north"))
    elif w == 2:                                   # a furnace: chest, inserter, furnace, inserter, chest
        cx, cy = sx + 3.0, b0 + 2.0
        ents.append((machine, cx, cy, "north"))
        chests_in, chests_out = [(sx, b0 + 1)], [(sx + 5, b0 + 1)]
        ents += [("iron-chest", sx + 0.5, b0 + 1.5, "north"), ("INS", sx + 1.5, b0 + 1.5, "west"),
                 ("INS", sx + 4.5, b0 + 1.5, "west"), ("iron-chest", sx + 5.5, b0 + 1.5, "north")]
        belt = ((sx, b0 + 2), (sx, b0 + 3), (sx, b0 + 4))       # under the input chest; the spine pole powers it
    else:                                          # 3x3: assemblers, chemical plants
        cx, cy = sx + 3.5, b0 + 2.5
        ents.append((machine, cx, cy, "north"))
        chests_in, chests_out = [(sx, b0 + 2)], [(sx + 6, b0 + 2)]
        ents += [("iron-chest", sx + 0.5, b0 + 2.5, "north"), ("INS", sx + 1.5, b0 + 2.5, "west"),
                 ("INS", sx + 5.5, b0 + 2.5, "west"), ("iron-chest", sx + 6.5, b0 + 2.5, "north")]
        belt = ((sx, b0 + 3), (sx, b0 + 4), (sx, b0 + 5))       # under the input chest, its belt from the corridor
        ents.append((POLE, sx + 1.5, b0 + 4.5, "north"))         # powers the port's inserter
    if belt is not None:                           # the port's inserter picks from the belt end below the chest
        ents.append(("INS", belt[0][0] + 0.5, belt[0][1] + 0.5, "south"))
    ins = inserter_kind(kn)
    ents = [(ins if n == "INS" else n, x, y, d) for n, x, y, d in ents]
    # the band's power line: every slot has poles at these two spots (clear of every machine size), placed for
    # the whole row at once, so a cell between empty slots is never cut off
    poles = spine_poles(sx, b0)
    slots = kn.fluid_slots(machine, recipe)
    ports = [{**pt, "fluid": slots.get(pt["box"], "")} for pt in kn.ports(machine, cx, cy, "north")]
    if w == 3:
        poles = poles + [(sx + 1, b0 + 4)]
    out = {"machine": machine, "recipe": recipe, "x": cx, "y": cy, "entities": ents, "poles": poles,
           "in": [(t[0] + 0.5, t[1] + 0.5) for t in chests_in], "out": [(t[0] + 0.5, t[1] + 0.5) for t in chests_out],
           "ports": ports}
    if belt is not None:
        out["belt_port"] = {"inserter": belt[0], "end": belt[1], "approach": belt[2], "chest": chests_in[0],
                            "item": None, "per_minute": 0.0, "fed": None}
    return out


def spine_poles(sx: int, b0: int) -> list[Tile]:
    """A slot's two poles, on its port row beside the machine: 6 apart, 3 to the next slot's (wire reach 7.5),
    and each covers the inserter, the chest-side columns and the machine edge on its side."""
    return [(sx, b0), (sx + 6, b0)]


def footprint(kn, name: str, x: float, y: float, d: str = "north") -> set[Tile]:
    w, h = kn.size(name)
    if d in ("east", "west"):
        w, h = h, w
    return {(tx, ty) for tx in range(math.floor(x - w / 2 + 0.01), math.floor(x + w / 2 - 0.01) + 1)
            for ty in range(math.floor(y - h / 2 + 0.01), math.floor(y + h / 2 - 0.01) + 1)}


class District:
    """The workshop floor: a rectangle of slots, bands and corridors, with its fluid nets."""

    def __init__(self, x0: int, y0: int, cols: int, rows: int):
        self.x0, self.y0, self.cols, self.rows = x0, y0, cols, rows
        self.used: dict[tuple[int, int], int] = {}          # (row, col) -> site id
        self.nets = Nets()
        self.water: Tile | None = None                      # the offshore pump's land tile, once placed
        self.pumpjacks_joined: set[tuple[float, float]] = set()
        self.extra_rows = 0                                 # silo bands below the grid
        self.idx = 0                                        # which floor this is
        self.poles_done: set[Tile] = set()                  # band and edge poles already standing
        for t in self.walkways():
            self.nets.walk.add(t)
        self.nets.noend |= self.gap_tiles()

    def gap_tiles(self) -> set[Tile]:
        """The gaps between cells along each band: vertical pipes pass there, so no underground ends sit in
        them (a line hopping from gap to gap along a band would close every gap it touches)."""
        out = set()
        for r in range(self.rows):
            b0 = self.band(r)
            for c in range(-1, self.cols):
                gx = self.slot_x(c) + 7
                out |= {(x, y) for x in (gx, gx + 1) for y in range(b0, b0 + BH)}
        return out

    @property
    def width(self) -> int:
        return self.cols * CW + 4

    @property
    def height(self) -> int:
        return CH + self.rows * PITCH

    def box(self) -> tuple[int, int, int, int]:
        return self.x0 - 2, self.y0, self.x0 + self.cols * CW + 2, self.y0 + self.height + self.extra_rows

    def band(self, row: int) -> int:
        return self.y0 + CH + row * PITCH

    def walkways(self) -> set[Tile]:
        x0, y0, x1, y1 = self.box()
        out = set()
        for r in range(self.rows + 1):                      # the middle of every corridor
            y = self.y0 + r * PITCH + CH // 2
            out |= {(x, y) for x in range(x0, x1 + 1)}
        for x in (x0, x1):                                   # down both sides
            out |= {(x, y) for y in range(y0, y1 + 1)}
        return out

    def free_slots(self) -> list[tuple[int, int]]:
        return [(r, c) for r in range(self.rows) for c in range(self.cols) if (r, c) not in self.used]

    def slot_x(self, col: int) -> int:
        return self.x0 + col * CW


class Workshop(Module):
    name = "workshop"
    describe = ("courier-fed cells for items belts cannot make yet: anything with fluids or oil (plastic, sulfur, "
                "acid, blue, purple and yellow science, rocket parts, concrete). One machine per cell; the character "
                "carries plates, coal and ore into its chests, and pipes bring water and oil. NOT automation for "
                "iron and copper chains (red and green science, circuits, gears, belts): use the factory module "
                "there. Needs power and, for oil, pumpjacks. args: {item, per_minute}")

    def __init__(self):
        self.designs: dict[tuple, dict] = {}
        self.district: District | None = None      # the floor new cells go on
        self.floors: list[District] = []           # every floor so far (fluids never cross between floors)

    # ------------------------------------------------------------ design
    CELL_STALL_MIN = 20.0    # a cell not seen working (or merely full) this long makes nothing worth counting

    @classmethod
    def stalled(cls, site, cell: dict, now: float | None) -> bool:
        """Has this cell stood still for CELL_STALL_MIN game minutes? Only judged once the patrol has watched the
        site (Foreman._sites keeps site.cell_working). Run 20261120-211555: a plastic workshop with no crude
        counted as 30/min "spare", so neither a replacement nor a new one would be built."""
        seen = getattr(site, "cell_working", None)
        if now is None or seen is None:
            return False
        last = seen.get((cell.get("x"), cell.get("y")))
        return last is not None and now - last > cls.CELL_STALL_MIN

    def spare(self, base, floor: District | None = None, w=None) -> dict[str, float]:
        """What built cells make beyond what other cells use (per minute). Items count from every floor (the
        courier carries them); fluids only from the floor the new cells go on (pipes do not join floors). Cells
        that have stood still for CELL_STALL_MIN count for nothing."""
        made: dict[str, float] = {}
        used: dict[str, float] = {}
        here = floor.idx if floor is not None else None
        now = w.now_tick() / 3600 if w is not None and hasattr(w, "now_tick") else None
        for s in base.sites:
            for c in s.cells:
                if self.stalled(s, c, now):
                    continue
                same = c.get("floor", 0) == here
                for k, v in c.get("item_out", {}).items():
                    made[k] = made.get(k, 0.0) + v
                for k, v in c.get("item_in", {}).items():
                    used[k] = used.get(k, 0.0) + v
                if same:
                    for k, v in c.get("fluid_out", {}).items():
                        made[k] = made.get(k, 0.0) + v
                    for k, v in c.get("fluid_in", {}).items():
                        used[k] = used.get(k, 0.0) + v
        return {k: v - used.get(k, 0.0) for k, v in made.items() if v - used.get(k, 0.0) > 0.5}

    def _design(self, w: FreeWorld, base, p: dict) -> dict:
        item, rate = p["item"], float(p.get("per_minute", 5))
        key = (item, rate)
        if key in self.designs:
            return self.designs[key]
        planner = Planner(w.kn, inserter_kind(w.kn))

        def plan_for(floor):
            pl = planner.plan(item, rate, spare=self.spare(base, floor, w))
            if pl.blocked:
                raise BuildError(f"no recipe a cell can use for {pl.blocked} (needed for {item})")
            if not pl.cells:
                raise BuildError(f"{item}: the cells already built make {rate}/min spare; nothing new to build")
            return pl
        d = self.district
        plan = plan_for(d)
        n = sum(c.count for c in plan.cells)
        if d is not None and not self._fits(w, base, d, n):
            w.log(f"[workshop] floor {d.idx} is full and cannot grow: a new floor for {item}")
            d = self.district = None                 # its fluids do not reach the new floor: plan again
            plan = plan_for(None)
            n = sum(c.count for c in plan.cells)
        if "crude-oil" in plan.fluids and not base.of("pumpjack"):
            k = max(1, math.ceil(plan.fluids["crude-oil"] / 300))
            raise BuildError(f"{item} needs {plan.fluids['crude-oil']:.0f} crude oil a minute: build pumpjacks "
                             f"first ({{\"do\": \"build\", \"module\": \"pumpjack\", \"count\": {k}}})")
        if "crude-oil" in plan.fluids and not self._crude_free(w, base, d):
            # every pumpjack feeds another floor: a floor that has crude already shares it (its cells and these
            # draw on one net; more pumpjacks join that net). Run 20261124-015025: chemical science failed five
            # times on "every pumpjack already feeds another floor" while the plastic floor had a crude line.
            oil = next((f for f in self.floors if f is not d and f.nets.tiles("crude-oil")
                        and self._fits(w, base, f, n)), None)
            if oil is None:
                raise BuildError("every pumpjack already feeds another floor: build more "
                                 '({"do": "build", "module": "pumpjack", "count": 2})')
            w.log(f"[workshop] {item} goes on floor {oil.idx}, which has a crude line already (every pumpjack "
                  "feeds a floor)")
            d = oil
            plan = plan_for(d)
            n = sum(c.count for c in plan.cells)
        # the crude line's pumps, counted like the water line's: one per PUMP_EVERY tiles (a detour adds one)
        far = self._crude_gap(base, d, home=base.centre or w.pos) if "crude-oil" in plan.fluids else 0.0
        crude_pumps = int(far // PUMP_EVERY) + 1
        if far > self.OIL_FLOOR_REACH:
            self._need_pumps(w, crude_pumps)
        if far > self.OIL_FLOOR_REACH and not self._pumps_ok(w, crude_pumps):
            # crude only reaches a floor by pipe, and a pipe past one segment needs pumps (engine units) the
            # crew cannot make yet: an oil floor goes beside the pumpjacks, and the courier carries what it
            # makes home. Run 20261121-140755 (and 20261120-211555): the plastics workshop stood in the base,
            # 441 pipe tiles from the oil field, and got no crude at all
            oil = next((f for f in self.floors if f is not d and self._crude_gap(base, f) <= self.OIL_FLOOR_REACH
                        and self._fits(w, base, f, n)), None)
            if oil is None:
                home_floor = self.district
                oil = self._new_floor(w, base, n, home=self._oil_home(base, w))
                self.district = home_floor           # cells without crude still go on the home floor
                if self._crude_gap(base, oil) > self.OIL_FLOOR_REACH:
                    self.floors.remove(oil)
                    raise BuildError(f"{item} needs crude oil, the oil field is {far:.0f} tiles from the "
                                     f"workshop floor ({crude_pumps} pumps needed: {crude_pumps} engine units"
                                     f"{self._engine_note(w, base)}) and no free area for a floor was found "
                                     "beside the pumpjacks")
            w.log(f"[workshop] {item} goes on oil floor {oil.idx} beside the pumpjacks (the home floor is "
                  f"{far:.0f} tiles from the crude, too far without {crude_pumps} pumps)")
            d = oil
            plan = plan_for(d)
            n = sum(c.count for c in plan.cells)
        if d is None:
            d = self._new_floor(w, base, n)
        if "water" in plan.fluids and d.water is None:
            # the same limit for water: a shore farther than one segment needs pumps, as many as the line has
            # segments (a detour adds one). Build nothing that would stand dry: say what unblocks it (engine units
            # need no oil, so pumps come before any oil product)
            x0, y0, x1, y1 = d.box()
            c = ((x0 + x1) / 2, (y0 + y1) / 2)
            shore = self._nearest_water(w, c)
            gap = abs(shore[0] - c[0]) + abs(shore[1] - c[1]) if shore is not None else 0.0
            pumps = int(gap // PUMP_EVERY) + 1
            if gap > self.OIL_FLOOR_REACH:
                self._need_pumps(w, pumps)
            if gap > self.OIL_FLOOR_REACH and not self._pumps_ok(w, pumps):
                raise BuildError(f"{item} needs water and the nearest shore is {gap:.0f} tiles from workshop floor "
                                 f"{d.idx}: too far to pipe without about {pumps} pumps (one engine unit each)"
                                 f"{self._engine_note(w, base)}. The crew hand-crafts the pumps, so no pump factory "
                                 f"is needed: once {pumps} engine units are made, build this again and the water "
                                 "line gets a pump every 200 tiles")
        design = self._place_plan(w, base, d, plan)
        design["plan"] = plan
        design["power_kw"] = planner.power_kw(plan)
        self.designs[key] = design
        return design

    def _usable(self, w, d: District) -> list[tuple[int, int]]:
        busy = d.nets.occupied()
        return [rc for rc in d.free_slots() if not (self._slot_tiles(d, *rc, ports=True) & busy)
                and not any(w.blocked(t) for t in self._slot_tiles(d, *rc) if t not in d.poles_done)]

    def _fits(self, w, base, d: District, n: int) -> bool:
        free = len(self._usable(w, d))
        return free >= n or self._grow(w, base, n - free, d)

    # a crude line spans one fluid segment without a pump: PUMP_EVERY route tiles, less what detours add
    OIL_FLOOR_REACH = 160          # Manhattan tiles from a floor's centre to its nearest pumpjack

    @staticmethod
    def _pumps_ok(w, n: int = 2) -> bool:
        can = getattr(w, "obtainable", None)
        return bool(can is not None and can("pump", n))

    @staticmethod
    def _need_pumps(w, n: int) -> None:
        """Say how many pumps a fluid line of ours needs: a pump factory goal becomes the crew making that many
        (Crew.small_need), and the planners see the number."""
        needs = getattr(w, "small_needs", None)
        if needs is None:
            try:
                needs = w.small_needs = {}
            except AttributeError:                     # a world that takes no new attributes (a test stub)
                return
        needs["pump"] = max(int(n), 1)

    @staticmethod
    def _engine_note(w, base) -> str:
        """Where the engine units come from: the engine-unit factory's measured output, or what to build."""
        if any(s.params.get("item") == "engine-unit" for s in base.of("factory")):
            rate = getattr(w, "rate", None)
            got = rate("engine-unit") if rate is not None else 0.0
            return (f"; our engine-unit factory makes {got:.1f}/min now (its inputs decide that: see its site's "
                    "health)")
        return ('; first {"do": "build", "module": "factory", "item": "engine-unit", "per_minute": 2} (engine '
                "units need no oil)")

    @staticmethod
    def _jacks(base) -> list[tuple[float, float]]:
        return [(x, y) for s in base.of("pumpjack") for name, x, y, _ in s.entities if name == "pumpjack"]

    def _crude_gap(self, base, d: District | None, home=None) -> float:
        """Manhattan tiles from a floor (or, with none yet, from `home`) to the nearest pumpjack."""
        jacks = self._jacks(base)
        if not jacks:
            return 0.0
        if d is not None:
            x0, y0, x1, y1 = d.box()
            home = ((x0 + x1) / 2, (y0 + y1) / 2)
        if home is None:
            return 0.0
        return min(abs(home[0] - x) + abs(home[1] - y) for x, y in jacks)

    @staticmethod
    def _nearest_water(w, at) -> tuple[float, float] | None:
        hints = list(getattr(w, "water_hints", None) or [])
        if not hints and hasattr(w, "scan"):
            w.scan()
            hints = list(getattr(w, "water_hints", None) or [])
        return min(hints, key=lambda q: math.dist(q, at)) if hints else None

    def _oil_home(self, base, w=None) -> tuple[float, float]:
        """Where an oil floor goes: beside the oil field nearest the base, toward the nearest shore when one is
        within two pipe reaches (sulfur, acid and advanced oil processing need water on the same floor, and a
        floor between the two gets both without pumps); else on the base's side of the field."""
        jacks = self._jacks(base)
        hub = base.centre or (0.0, 0.0)
        first = min(jacks, key=lambda j: math.dist(j, hub))
        field = [j for j in jacks if math.dist(j, first) <= 24]
        cx, cy = sum(x for x, _ in field) / len(field), sum(y for _, y in field) / len(field)
        water = self._nearest_water(w, (cx, cy)) if w is not None else None
        reach = self.OIL_FLOOR_REACH / math.sqrt(2)   # straight-line tiles that stay in reach on a diagonal
        if water is not None and math.dist(water, (cx, cy)) <= 2 * reach - 30:
            gap = math.dist(water, (cx, cy))
            k = max(0.0, min(gap - 20, max(45.0, gap - reach + 30)))   # in reach of both, nearest the oil
            return cx + (water[0] - cx) / gap * k, cy + (water[1] - cy) / gap * k
        dx, dy = hub[0] - cx, hub[1] - cy
        k = math.hypot(dx, dy) or 1.0
        return cx + dx / k * 45, cy + dy / k * 45

    def _new_floor(self, w: FreeWorld, base, cells: int, home=None) -> District:
        if home is None:
            home = base.centre or w.pos
            if self.floors:                          # beside the last floor, not on top of it
                x0, y0, x1, y1 = self.floors[-1].box()
                home = (x1 + 40, (y0 + y1) / 2)
        for cols, rows in ((8, max(3, math.ceil(cells / 8) + 2)), (6, max(3, math.ceil(cells / 6) + 2)),
                           (5, math.ceil(cells / 5) + 1), (4, math.ceil(cells / 4))):
            spot = self._find_area(w, home, cols * CW + 6, CH + rows * PITCH + 2)
            if spot:
                d = District(spot[0] + 3, spot[1] + 1, cols, rows)
                d.idx = len(self.floors)
                self.floors.append(d)
                self.district = d
                w.log(f"[workshop] floor {d.idx} at {spot}: {cols} x {rows} slots")
                return d
        raise BuildError("no flat free area near the base for a workshop floor (it needs about 60 x 50 tiles "
                         "without water, ore or buildings)")

    def _grow(self, w, base, more: int, d: District | None = None) -> bool:
        d = d or self.district
        add = math.ceil(more / d.cols)
        x0, y0, x1, y1 = d.box()
        w.survey_area((x0 + x1) / 2, y1 + add * PITCH / 2, half=max(20, add * PITCH), step=32)
        tiles = {(x, y) for x in range(x0, x1 + 1) for y in range(y1 + 1, y1 + 1 + add * PITCH)}
        if not self._area_ok(w, tiles):
            return False
        for t in d.walkways():
            d.nets.walk.discard(t)
        d.rows += add
        d.nets.walk |= d.walkways()
        d.nets.noend |= d.gap_tiles()
        return True

    def _area_ok(self, w, tiles) -> bool:
        ore = set().union(*w.resources.values()) if w.resources else set()
        z = getattr(w, "zones", None)
        zb = z.refused() if z is not None else ()     # land the hybrid floorplan reserves (zones.py)
        return all(t in w.seen and not w.blocked(t) and t not in ore and t not in w.keepout and t not in zb
                   for t in tiles)

    def _find_area(self, w: FreeWorld, home, W: int, H: int):
        """The free rectangle nearest home (top-left tile), searched on rings of candidate corners."""
        for ring in range(0, 6):
            cands = []
            r = ring * 30
            for dx in range(-r, r + 1, 30):
                for dy in range(-r, r + 1, 30):
                    if ring and max(abs(dx), abs(dy)) != r:
                        continue
                    cands.append((int(home[0] + dx - W / 2), int(home[1] + dy - H / 2)))
            for x0, y0 in sorted(cands, key=lambda q: math.dist((q[0] + W / 2, q[1] + H / 2), home)):
                w.survey_area(x0 + W / 2, y0 + H / 2, half=max(W, H) // 2 + 4, step=36)
                tiles = {(x, y) for x in range(x0, x0 + W) for y in range(y0, y0 + H)}
                if self._area_ok(w, tiles):
                    return (x0, y0)
                # slide a little inside the candidate: often one corner touches ore or a lake
                for ox, oy in ((10, 0), (-10, 0), (0, 10), (0, -10)):
                    t2 = {(x + ox, y + oy) for x, y in tiles}
                    if self._area_ok(w, t2):
                        return (x0 + ox, y0 + oy)
        return None

    def _place_plan(self, w: FreeWorld, base, d: District, plan) -> dict:
        """Slots for every new cell, their ports reserved, and every fluid net routed. Nothing is built yet."""
        kn = w.kn
        # a slot is only usable while nothing stands in it (an older pipe may cross it): its band and port rows
        free = self._usable(w, d)
        cells: list[dict] = []
        k = 0
        if len(free) < sum(n.count for n in plan.cells) and self._grow(w, base, sum(n.count for n in plan.cells) - len(free), d):
            free = self._usable(w, d)
        for need in plan.cells:
            for i in range(need.count):
                if k >= len(free):
                    raise BuildError("ran out of free slots on the workshop floor (it could not grow)")
                row, col = free[k]
                k += 1
                lay = cell_layout(kn, need.machine, need.recipe, d.slot_x(col), d.band(row))
                share = 1.0 / need.count
                lay.update({"slot": (row, col), "item_in": {a: round(b * share, 2) for a, b in need.item_in.items()},
                            "item_out": {a: round(b * share, 2) for a, b in need.item_out.items()},
                            "fluid_in": {a: round(b * share, 2) for a, b in need.fluid_in.items()},
                            "fluid_out": {a: round(b * share, 2) for a, b in need.fluid_out.items()},
                            "sink": need.sink})
                bp = lay.get("belt_port")
                if bp is not None:
                    ins_ = {k: v for k, v in lay["item_in"].items() if k != "coal"} or lay["item_in"]
                    if ins_:
                        bp["item"] = max(ins_, key=ins_.get)
                        bp["per_minute"] = ins_[bp["item"]]
                    else:
                        lay.pop("belt_port")
                cells.append(lay)
        planned: set[Tile] = set()
        for c in cells:
            for n, x, y, dd in c["entities"]:
                planned |= footprint(kn, n, x, y, dd)
            if c.get("belt_port"):                     # the belt's way in stays clear of pipes
                planned |= {c["belt_port"]["end"], c["belt_port"]["approach"]}
        rows = sorted({c["slot"][0] for c in cells})
        spine = [t for r in rows for col in range(d.cols) for t in spine_poles(d.slot_x(col), d.band(r))
                 if t not in d.poles_done and not w.blocked(t)]
        edge = [t for r in rows for dy in (1, 7) for t in [(d.x0 - 1, d.band(r) + dy)]
                if t not in d.poles_done and t not in d.nets.occupied() and t not in d.nets.walk and not w.blocked(t)]
        planned |= set(spine) | set(edge)
        nets = _copy_nets(d.nets)
        for c in cells:
            for pt in c["ports"]:
                nets.reserve(pt["target"], pt["fluid"])
        # every used port also keeps the next tile straight out: otherwise a neighbouring net may wrap around
        # its target (touching it only diagonally) and seal it in (a refinery's three outputs are 2 tiles apart)
        for c in cells:
            for pt in c["ports"]:
                if not pt["fluid"]:
                    continue
                (ix, iy), (tx, ty) = pt["inside"], pt["target"]
                for k in (1, 2):                       # two tiles straight out: room to start an underground too
                    lane = (tx + k * (tx - ix), ty + k * (ty - iy))
                    if lane in planned or nets.reserved.get(lane) not in (None, "")                             or nets.lanes.get(lane, pt["fluid"]) != pt["fluid"]:
                        break
                    nets.lanes[lane] = pt["fluid"]
        # slots still empty after this plan stay clear for later cells: pipes keep to corridors and gaps
        taken = {c["slot"] for c in cells}
        later: set[Tile] = set()
        for rc in d.free_slots():
            if rc not in taken:
                later |= self._slot_tiles(d, *rc)

        def free_tile(t: Tile) -> bool:
            return t not in planned and t not in later and w.free({t})

        # one net per fluid: its sources first (pump, pumpjacks, producing cells), then every consumer
        # long lines first (water from the shore, crude from the oil field): they have the fewest ways in,
        # and the short nets between cells route around them
        fluids = sorted({pt["fluid"] for c in cells for pt in c["ports"] if pt["fluid"]},
                        key=lambda f: (f not in ("water", "crude-oil"), f))
        routes: dict[str, list[tuple]] = {}
        unpumped: list[str] = []                       # long lines laid without pumps for now
        water_pump = None
        for fl in fluids:
            terminals: list[Tile] = []
            if fl == "water" and not d.nets.tiles("water"):
                water_pump = self._water_spot(w, d, planned)
                if water_pump is None:
                    raise BuildError("no shore for the workshop's water pump within reach of the floor")
                terminals.append(water_pump["target"])
                self._survey_line(w, d, terminals, fl)       # a far shore: the router needs the ground seen
            if fl == "crude-oil":
                for t in self._pumpjack_ports(w, base, d):
                    terminals.append(t)
                if not terminals and not d.nets.tiles("crude-oil"):
                    raise BuildError("every pumpjack already feeds another floor: build more "
                                     '({"do": "build", "module": "pumpjack", "count": 2})')
                self._survey_line(w, d, terminals)
            outs = [pt["target"] for c in cells for pt in c["ports"] if pt["fluid"] == fl and pt["production"] == "output"]
            ins = [pt["target"] for c in cells for pt in c["ports"] if pt["fluid"] == fl and pt["production"] != "output"]
            steps_all: list[tuple] = []
            seen_any = bool(nets.tiles(fl))
            for t in terminals + outs + ins:
                if t in nets.tiles(fl):
                    continue
                steps = route(nets, fl, t, free_tile, margin=40 if t in terminals else 24)
                if steps is None:                             # boxed in: cross an empty slot rather than fail
                    steps = route(nets, fl, t, lambda q: q not in planned and w.free({q}),
                                  margin=40 if t in terminals else 30)   # (that slot then takes no cell)
                if steps is None:
                    if t in terminals and seen_any:           # an extra pumpjack that does not fit: skip it
                        continue
                    self._save_floor(w, d, nets, planned, fl, t)
                    far = [q for q in nets.tiles(fl) | set(terminals) if math.dist(q, t) > self.SURVEY_MIN]
                    if fl == "crude-oil" and far:
                        src = min(far, key=lambda q: math.dist(q, t))
                        raise BuildError(f"no pipe route for crude-oil from the floor to its crude line (nearest at "
                                         f"{src}, {round(math.dist(src, t))} tiles): the way between is blocked by "
                                         "water, cliffs or buildings. The crude oil itself is found and pumped: "
                                         "scouting for more does not help")
                    raise BuildError(f"no pipe route for {fl} from {t} to its net (the floor is too crowded or "
                                     "cut off by water or buildings)")
                if fl in ("crude-oil", "water") and route_length(steps) > PUMP_EVERY:
                    # a line longer than a fluid segment may stretch: a pump every PUMP_EVERY tiles (pipes.py), when
                    # the crew can get pumps (an engine unit each, machine-made); else laid without, and pumped later
                    # (Crew.split_pipelines). Run 20261120-211555: every oil workshop failed on "cannot hand-craft
                    # engine-unit" for 90 minutes once pumps were planned before anything made engines
                    need = route_length(steps) // PUMP_EVERY
                    can = getattr(w, "obtainable", None)
                    if can is None or can("pump", need):
                        def pump_free(q, nets=nets):
                            return free_tile(q) and q not in nets.occupied() and q not in nets.reserved \
                                and q not in nets.walk
                        steps, _ = add_pumps(steps, pump_free, flow_to_start=t not in terminals)
                    else:
                        unpumped.append(fl)
                nets.commit(fl, steps)
                steps_all += steps
                seen_any = True
            routes[fl] = steps_all
        kit: dict[str, int] = {}
        for c in cells:
            for n, *_ in c["entities"]:
                kit[n] = kit.get(n, 0) + 1
        npipe = sum(1 for st in routes.values() for s in st if s[0] == "pipe")
        nptg = sum(1 for st in routes.values() for s in st if s[0] == "ptg")
        npump = sum(1 for st in routes.values() for s in st if s[0] == "pump")
        if npump:
            kit["pump"] = kit.get("pump", 0) + npump
        if npipe:
            kit["pipe"] = kit.get("pipe", 0) + npipe + 4
        if nptg:
            kit["pipe-to-ground"] = kit.get("pipe-to-ground", 0) + nptg
        if water_pump:
            kit["offshore-pump"] = 1
        kit[POLE] = kit.get(POLE, 0) + len(spine) + len(edge) + 2
        return {"cells": cells, "nets": nets, "routes": routes, "kit": kit, "water": water_pump, "edge_poles": edge,
                "spine": spine, "district": d, "later": later, "unpumped": unpumped}

    case_dir = None                             # set by the bot: failed floors are saved there for offline replay

    def _save_floor(self, w, d: District, nets: Nets, planned: set, fluid: str, start: Tile) -> None:
        """The floor a route failed on (the ground, the nets, what was planned), for replaying offline."""
        if self.case_dir is None:
            return
        try:
            import json
            x0, y0, x1, y1 = d.box()
            box = {(x, y) for x in range(x0 - 40, x1 + 41) for y in range(y0 - 40, y1 + 41)}
            data = {"fluid": fluid, "start": list(start), "box": [x0, y0, x1, y1],
                    "blocked": sorted([list(t) for t in box if w.blocked(t)]),
                    "keepout": sorted([list(t) for t in box if t in w.keepout]),
                    "unseen": sorted([list(t) for t in box if t not in w.seen]),
                    "planned": sorted([list(t) for t in planned]),
                    "pipe": [[t[0], t[1], f] for t, f in nets.pipe.items()],
                    "ptg": [[t[0], t[1], f, dd] for t, (f, dd) in nets.ptg.items()],
                    "reserved": [[t[0], t[1], f] for t, f in nets.reserved.items()]}
            self.case_dir.mkdir(parents=True, exist_ok=True)
            n = len(list(self.case_dir.glob("floor_*.json"))) + 1
            (self.case_dir / f"floor_{n:02d}_{fluid}.json").write_text(json.dumps(data), encoding="utf-8")
        except Exception:  # noqa: BLE001 - debugging help only
            pass

    @staticmethod
    def _slot_tiles(d: District, row: int, col: int, ports: bool = False) -> set[Tile]:
        """A slot's band (7 wide, 5 tall); with ports, also the rows above and below where 5x5 ports sit."""
        sx, b0 = d.slot_x(col), d.band(row)
        ys = range(b0 - 1, b0 + 6) if ports else range(b0, b0 + 5)
        return {(x, y) for x in range(sx, sx + 7) for y in ys}

    def _water_spot(self, w: FreeWorld, d: District, planned: set) -> dict | None:
        """An offshore pump spot near the floor (the shore tiles the game refused are skipped)."""
        x0, y0, x1, y1 = d.box()
        centre = ((x0 + x1) / 2, (y0 + y1) / 2)
        if not w.water_hints:
            w.scan()
        if not w.water_hints:
            return None
        water = min(w.water_hints, key=lambda q: math.dist(q, centre))
        w.survey_area(*water, half=24, step=32)
        spots = sorted(SteamPower.pump_spots(w, water), key=lambda q: (q[0], math.dist(q[1][0], centre)))
        for _, (land, dn) in spots[:30]:
            dx, dy = ab.DIRS[dn]
            out = (land[0] - dx, land[1] - dy)
            if land in planned or out in planned or w.blocked(out) or out in w.water:
                continue
            return {"land": land, "dir": dn, "target": out}
        return None

    SURVEY_MIN = 48          # a crude source farther than this from the floor: look along the way first
    SURVEY_STEP = 40         # tiles between looks along it (each look sees 24 tiles around)

    def _survey_line(self, w: FreeWorld, d: District, terminals: list[Tile], fluid: str = "crude-oil") -> None:
        """Look along the way from the floor to its nearest far crude source, so the pipe router knows those tiles.
        It routes only over tiles seen (w.free); run 20261120-211555's pumpjack stood ~470 tiles from the floor
        over ground nobody had looked at, every plastic workshop failed with "no pipe route", and the planners
        read that as "find more oil" and sent a scout. Each line is surveyed once."""
        x0, y0, x1, y1 = d.box()
        c = ((x0 + x1) / 2, (y0 + y1) / 2)
        far = [t for t in terminals if math.dist(t, c) > self.SURVEY_MIN]
        look = getattr(w, "look", None)
        if not far or look is None:                  # a stub world in a test cannot look
            return
        t = min(far, key=lambda q: math.dist(q, c))
        done = w.__dict__.setdefault("surveyed_lines", set())
        key = (round(c[0] / 8), round(c[1] / 8), round(t[0] / 8), round(t[1] / 8))
        if key in done:
            return
        n = max(1, int(math.dist(c, t) // self.SURVEY_STEP))
        what = "water" if fluid == "water" else "crude"
        getattr(w, "doing", lambda *a: None)(f"surveying the way for a {what} pipe from {t} to the workshop floor")
        for k in range(n + 1):
            px, py = c[0] + (t[0] - c[0]) * k / n, c[1] + (t[1] - c[1]) * k / n
            seen = getattr(w, "seen", set())
            if all((math.floor(px) + ox, math.floor(py) + oy) in seen for ox in (-16, 0, 16) for oy in (-16, 0, 16)):
                continue
            try:
                look(px, py, radius=24)
            except BuildError:
                continue                           # water or cliffs in the way: the router goes around
        done.add(key)

    def forget(self, site) -> None:
        """A workshop site is gone: its slots are free again, its pipes leave the floor's nets, and pumpjacks no
        crude pipe touches any more are free to join a floor. Run 20261120-211555: after the plastic workshop with
        the long crude line was removed, the next one joined the floor's crude net, which no longer existed in the
        game, and its refinery never saw crude; its pumpjacks stayed "already feeding another floor"."""
        cells = getattr(site, "cells", []) or []
        idx = {c.get("floor") for c in cells} or {getattr(self.district, "idx", None)}
        for d in [f for f in (self.floors or [self.district]) if f is not None and f.idx in idx]:
            self._forget_on(d, site, cells)
        self.designs.clear()

    def _forget_on(self, d: District, site, cells: list) -> None:
        for c in cells:
            if c.get("floor", d.idx) == d.idx and c.get("slot") is not None:
                d.used.pop(tuple(c["slot"]), None)
        gone = {(int(x - 0.5), int(y - 0.5)) for n, x, y, _ in site.entities if n in ("pipe", "pipe-to-ground")}
        for n, x, y, _ in site.entities:
            if n == "pump":                                   # the two tiles under its centre
                gone |= {(math.floor(x + dx), math.floor(y + dy)) for dx in (-0.5, 0.49) for dy in (-0.5, 0.49)}
            if n == "offshore-pump":
                d.water = None
        nets = d.nets
        for t in gone:
            nets.pipe.pop(t, None)
            nets.ptg.pop(t, None)
            nets.pumps.pop(t, None)
        def ends(sp):                                         # (fluid, axis 0=x/1=y, fixed, lo, hi)
            _, axis, fixed, lo, hi = sp
            return ((lo, fixed), (hi, fixed)) if axis == 0 else ((fixed, lo), (fixed, hi))
        nets.spans = [sp for sp in nets.spans if not any(t in gone for t in ends(sp))]
        crude = nets.tiles("crude-oil")
        d.pumpjacks_joined = {p for p in d.pumpjacks_joined
                              if any(abs(t[0] + 0.5 - p[0]) <= 3 and abs(t[1] + 0.5 - p[1]) <= 3 for t in crude)}

    def misports(self, w: FreeWorld, site) -> list[dict]:
        """Ports of a workshop's machines whose fluid is not what the floor was piped for: the floor reserved each
        port target for the fluid its box was thought to carry; a box the game assigns otherwise (a recipe's
        fluidbox_index, knowledge.FLUIDBOX_INDEX) leaves the pipe on a connection the fluid never takes."""
        out = []
        floors = {f.idx: f for f in (self.floors or [self.district]) if f is not None}
        for c in getattr(site, "cells", []) or []:
            d = floors.get(c.get("floor", getattr(self.district, "idx", None)))
            if d is None or not c.get("recipe"):
                continue
            slots = w.kn.fluid_slots(c["machine"], c["recipe"])
            for pt in w.kn.ports(c["machine"], c["x"], c["y"], "north"):
                t, fl = tuple(pt["target"]), slots.get(pt["box"], "")
                if t in d.nets.reserved and d.nets.reserved[t] != fl:
                    out.append({"floor": d, "target": t, "fluid": fl, "was": d.nets.reserved[t],
                                "machine": c["machine"]})
        return out

    def repipe_plan(self, w: FreeWorld, site) -> list[list]:
        """Pipes that join each mis-piped port to its fluid's net, as pipe gaps for Crew.repair (it places them).
        The floor's books take the right fluids first; the old pipe stays on the box the recipe leaves unused."""
        bad = self.misports(w, site)
        for b in bad:
            b["floor"].nets.reserved[b["target"]] = b["fluid"]
        gaps: list[list] = []
        for b in bad:
            d, fl, t = b["floor"], b["fluid"], b["target"]
            if not fl or t in d.nets.tiles(fl):
                continue
            if not d.nets.tiles(fl):
                raise BuildError(f"site {site.id}: no {fl} pipe on its floor to join {t} to")
            steps = route(d.nets, fl, t, lambda q: w.free({q}), margin=16)
            if steps is None:
                raise BuildError(f"site {site.id}: no pipe route from its {b['machine']}'s {fl} connection at {t} to "
                                 f"the {fl} line (the tiles around it are taken)")
            d.nets.commit(fl, steps)
            gaps += [[fl, s[0], list(s[1]), s[2] if len(s) > 2 else "north"] for s in steps]
        self.designs.clear()
        return gaps

    def _crude_free(self, w: FreeWorld, base, d: District | None) -> bool:
        """Crude this floor can get: a crude line of its own, or a pumpjack whose output no other floor's pipe
        takes yet (with no floor yet: a pumpjack no floor has joined)."""
        if getattr(getattr(w, "kn", None), "ports", None) is None:
            return True                                # no catalog to read ports from (stub worlds): as before
        if d is not None:
            return bool(d.nets.tiles("crude-oil")) or bool(self._pumpjack_ports(w, base, d))
        joined = set().union(*[f.pumpjacks_joined for f in self.floors]) if self.floors else set()
        return bool(self._jack_ports(w, base, joined))

    def _pumpjack_ports(self, w: FreeWorld, base, d: District) -> list[Tile]:
        return self._jack_ports(w, base, d.pumpjacks_joined)

    def _jack_ports(self, w: FreeWorld, base, joined: set) -> list[Tile]:
        out = []
        for s in base.of("pumpjack"):
            for name, x, y, dd in s.entities:
                if name != "pumpjack" or (x, y) in joined:
                    continue
                for pt in w.kn.ports("pumpjack", x, y, dd):
                    if not w.blocked(pt["target"]):
                        out.append(pt["target"])
        return out

    # ------------------------------------------------------------ module API
    def recipes(self, w, base, p):
        return {c["recipe"] for c in self._design(w, base, p)["cells"] if c.get("recipe")}

    def kit(self, w, base, p):
        return dict(self._design(w, base, p)["kit"])

    def build(self, w, base, p):
        design = self._design(w, base, p)
        d: District = design["district"]
        kn = w.kn
        site = Site(self.name, {"item": p["item"], "per_minute": float(p.get("per_minute", 5))},
                    ((d.box()[0] + d.box()[2]) / 2, (d.box()[1] + d.box()[3]) / 2))
        # the water pump first: when the game refuses its tile, the next spot is tried and its pipe re-routed
        if design["water"]:
            self._place_water(w, d, design)
        failed = []
        refused = []                                  # buildings the game did not take: gaps a repair places
        for c in design["cells"]:
            for n, x, y, dd in c["entities"]:
                # a furnace picks its recipe from what goes in: the game takes set_recipe on assemblers only (a
                # furnace answers no_entity). Run 20261124-120854: workshop 64's two steel furnaces were placed,
                # yet counted as never placed, and every repair retried the same refused call
                recipe = (c["recipe"] if n == c["machine"] and c.get("recipe") and n != "rocket-silo"
                          and not n.endswith("furnace") else None)
                try:
                    w.place(n, x, y, dd, footprint(kn, n, x, y, dd))
                except BuildError as exc:
                    failed.append(str(exc)[:120])
                    refused.append({"name": n, "x": x, "y": y, "dir": dd, "why": str(exc)[:120], "tries": 0,
                                    **({"recipe": recipe} if recipe else {})})
                    continue
                if recipe:
                    r = w.act({"type": "set_recipe", "x": x, "y": y, "recipe": recipe})
                    if not r.get("ok"):
                        failed.append(f"set_recipe {c['recipe']}: {r.get('code')}")
                        refused.append({"name": n, "x": x, "y": y, "dir": dd, "recipe": recipe, "placed": True,
                                        "why": f"set_recipe {recipe}: {r.get('code')}", "tries": 0})
            site.poles += [tuple(q) for q in c["poles"]]
            d.used[c["slot"]] = -1
        gaps = []                                     # pipes the game refused: the fluid stops there
        for fl, steps in design["routes"].items():
            for s in steps:
                t = s[1]
                try:
                    if s[0] == "pipe":
                        w.place("pipe", t[0] + 0.5, t[1] + 0.5, "north", {t})
                    elif s[0] == "pump":                  # two tiles; the crew powers it after the build
                        a, b = s[1]
                        w.place("pump", (a[0] + b[0]) / 2 + 0.5, (a[1] + b[1]) / 2 + 0.5, s[2], {a, b})
                        site.params.setdefault("pumps", []).append(list(a))
                        continue
                    else:
                        w.place("pipe-to-ground", t[0] + 0.5, t[1] + 0.5, s[2], {t})
                except BuildError as exc:
                    failed.append(f"{fl} pipe at {t}: {str(exc)[:80]}")
                    gaps.append([fl, s[0], list(t), s[2] if len(s) > 2 else "north"])
        for t in design.get("spine", []) + design["edge_poles"]:     # the band lines and the edge column
            try:
                w.place(POLE, t[0] + 0.5, t[1] + 0.5, "north", {t})
                site.poles.append(t)
                d.poles_done.add(t)
            except BuildError as exc:
                failed.append(f"pole at {t}: {str(exc)[:80]}")
                refused.append({"name": POLE, "x": t[0] + 0.5, "y": t[1] + 0.5, "dir": "north",
                                "why": f"pole at {t}: {str(exc)[:80]}", "tries": 0})
        d.nets = design["nets"]
        if not any(g[0] == "crude-oil" for g in gaps):  # a broken crude line joins nothing: repair it, or route again
            for s in base.of("pumpjack"):
                for name, x, y, _ in s.entities:
                    if name == "pumpjack":
                        d.pumpjacks_joined.add((x, y))
        site.cells = [{k: c[k] for k in ("machine", "recipe", "x", "y", "in", "out", "item_in", "item_out",
                                         "fluid_in", "fluid_out", "slot", "sink")} | {"floor": d.idx, "port": c.get("belt_port")}
                      for c in design["cells"]]
        if gaps:
            site.params["pipe_gaps"] = gaps
        if refused:                                    # gaps.py: the gap alert has repair place them
            from .gaps import record
            record(site, refused)
        if design.get("unpumped"):                     # Crew.split_pipelines pumps it once pumps can be made
            site.params["needs_pumps"] = True
        ports = [c["port"] for c in site.cells if c.get("port")]
        for pt in ports:
            w.keepout |= {tuple(pt["end"]), tuple(pt["approach"])}
        if ports:
            site.params["ports"] = ports
        for c in site.cells:
            for x, y in c["out"]:
                for item in c["item_out"]:
                    site.chests.append((x, y, item))
            if c["machine"] in ("stone-furnace", "steel-furnace"):
                site.burners.append((c["x"], c["y"]))
        plan = design["plan"]
        site.note = (f"{len(site.cells)} cells, {sum(1 for _ in design['routes'])} fluid nets, "
                     f"~{design['power_kw'] / 1000:.1f} MW at full work" + (f"; {len(failed)} failed: {failed[0]}" if failed else ""))
        self.designs.clear()
        if failed and len(failed) * 3 > sum(len(c["entities"]) for c in design["cells"]):
            raise BuildError(f"workshop mostly failed: {failed[0]}")
        w.log(f"[workshop] {plan.summary()[:300]}")
        return site

    def _place_water(self, w: FreeWorld, d: District, design: dict) -> None:
        wp = design["water"]
        for _ in range(8):
            land, dn = wp["land"], wp["dir"]
            w.near(*ab.centre(land))
            r = w.run.place("offshore-pump", *ab.centre(land), dn)
            if r.get("ok"):                     # the run's act wrapper records it for the site
                w.used.add(land)
                d.water = land
                return
            w.bad_pumps.add((land, dn))
            planned = {t for c in design["cells"] for n, x, y, dd in c["entities"] for t in footprint(w.kn, n, x, y, dd)}
            nwp = self._water_spot(w, d, planned)
            if nwp is None:
                break
            # re-route the water source to the new spot (the rest of the net stays)
            nets = design["nets"]
            old = [s for s in design["routes"].get("water", [])]
            for s in old:
                nets.pipe.pop(s[1], None)
                nets.ptg.pop(s[1], None)
            starts = [pt["target"] for c in design["cells"] for pt in c["ports"] if pt["fluid"] == "water"]
            steps_all = []
            for t in [nwp["target"]] + starts:
                steps = route(nets, "water", t, lambda q: q not in planned and q not in design.get("later", ())
                              and w.free({q}), margin=40)
                if steps is None:
                    raise BuildError("the water pump's shore moved and no new pipe route fits")
                nets.commit("water", steps)
                steps_all += steps
            design["routes"]["water"] = steps_all
            design["water"] = wp = nwp
        raise BuildError("the game refused every shore tile tried for the workshop's water pump")


def _copy_nets(n: Nets) -> Nets:
    c = Nets()
    c.pipe, c.ptg, c.reserved = dict(n.pipe), dict(n.ptg), dict(n.reserved)
    c.spans, c.walk, c.noend, c.lanes = list(n.spans), set(n.walk), set(n.noend), dict(n.lanes)
    c.pumps = dict(getattr(n, "pumps", {}))
    return c


# -------------------------------------------------------------------- silo
class Silo(Module):
    name = "silo"
    describe = ("the rocket silo (needs rocket-silo researched; its kit is 1000 steel, 1000 concrete, 200 processing "
                "units, 200 electric engines and 100 pipes, taken from workshop chests), with three input chests and "
                "inserters. The courier keeps it fed with processing units, low density structures and rocket fuel "
                "(10 each per part, 100 parts a rocket); a finished rocket launches on its own. args: {}")
    PARTS_PER_MIN = 1.0

    def kit(self, w, base, p):
        return {"rocket-silo": 1, "iron-chest": 3, inserter_kind(w.kn): 6, POLE: 6}

    def recipes(self, w, base, p):
        return {"rocket-part"}

    def build(self, w, base, p):
        if base.of("silo"):
            raise BuildError("a silo already stands; feed it (the courier does) instead of building another")
        from .modules import MODULES
        wsm = MODULES.get("workshop")
        d = getattr(wsm, "district", None)
        home = base.centre or w.pos
        if d is not None:                       # right below the workshop floor, where the courier works
            x0, y0, x1, y1 = d.box()
            home = ((x0 + x1) / 2, y1 + 10)
        spot = wsm._find_area(w, home, 15, 13) if wsm else None
        if spot is None:
            raise BuildError("no free 15 x 13 area for the rocket silo near the base")
        sx, sy = spot[0] + 1, spot[1] + 1
        cx, cy = sx + 3 + 4.5, sy + 1 + 4.5          # silo tiles sx+3 .. sx+11, sy+1 .. sy+9
        ins = inserter_kind(w.kn)
        site = Site(self.name, {"parts_per_rocket": int((w.kn.entities.get("rocket-silo") or {}).get(
            "rocket_parts_required") or 100)}, (cx, cy))
        w.place("rocket-silo", cx, cy, "north", footprint(w.kn, "rocket-silo", cx, cy))
        ins_x = sx + 2
        chests, ports = [], []
        for k, item in enumerate(("processing-unit", "low-density-structure", "rocket-fuel")):
            y = sy + 3 + 2 * k
            w.place("iron-chest", sx + 1.5, y + 0.5, "north", {(sx + 1, y)})
            w.place(ins, ins_x + 0.5, y + 0.5, "west", {(ins_x, y)})
            chests.append((sx + 1.5, y + 0.5))
            # a port into each chest: the factory that makes the item belts it here (the courier until then)
            if w.free({(sx, y)}):
                w.place(ins, sx + 0.5, y + 0.5, "west", {(sx, y)})
                ports.append({"inserter": (sx, y), "end": (sx - 1, y), "approach": (sx - 2, y),
                              "chest": (sx + 1.5, y + 0.5), "item": item, "per_minute": 10 * self.PARTS_PER_MIN,
                              "fed": None})
                w.keepout |= {(sx - 1, y), (sx - 2, y)}
        # inserters on rows sy+3, +5, +7: poles on sy+4 and sy+8 reach all three (a small pole covers 2 tiles around)
        for q in ((sx + 2, sy + 4), (sx + 2, sy + 8), (sx + 12, sy + 5)):
            if w.free({q}):
                w.place(POLE, q[0] + 0.5, q[1] + 0.5, "north", {q})
                site.poles.append(q)
        rate = self.PARTS_PER_MIN
        site.cells = [{"machine": "rocket-silo", "recipe": "rocket-part", "x": cx, "y": cy, "in": chests, "out": [],
                       "item_in": {"processing-unit": 10 * rate, "low-density-structure": 10 * rate,
                                   "rocket-fuel": 10 * rate},
                       "item_out": {}, "fluid_in": {}, "fluid_out": {}, "slot": None,
                       "in_map": {"processing-unit": 0, "low-density-structure": 1, "rocket-fuel": 2}}]
        if ports:
            site.params["ports"] = ports
        return site


def _register() -> None:
    from .modules import MODULES
    for m in (Workshop(), Silo()):
        MODULES.setdefault(m.name, m)


_register()
