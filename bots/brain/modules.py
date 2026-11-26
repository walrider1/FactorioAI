"""Modules: the chiplets the brain builds with. Each knows its kit (what it costs) and how to build itself.

    burner_mine  burner drills on a patch, each dropping into a furnace (ores) or a chest (coal, stone)
    steam_power  offshore pump, boiler, 1-2 steam engines at the nearest water
    labs         a row of labs passing science packs along with inserters, wired to the power network
    factory      a whole automated factory for an item at a rate: the fab layout engine (mines, assemblers,
                 belts, undergrounds, poles), placed by simulated annealing and routed with A*

A built module is a Site: where it is, what the foreman must fuel, where its output collects. Sites are how
the foreman and the strategist's report see the base. New modules plug in by adding a class to MODULES.
"""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, field

import automation_bot as ab
from fab.build import build as fab_build
from fab.design import design_factory
from fab.grid import Grid

from .world import BuildError, FreeWorld

Tile = tuple[int, int]


ELECTRIC = {"lab", "assembling-machine-1", "assembling-machine-2", "assembling-machine-3", "electric-mining-drill",
            "inserter", "long-handed-inserter", "fast-inserter", "electric-furnace", "chemical-plant", "oil-refinery",
            "pumpjack", "rocket-silo"}
GENERATORS = {"steam-engine", "steam-turbine", "solar-panel"}
UNPLUGGED = "not_plugged_in_electric_network"      # a generator no pole reaches: it powers nothing


# Plate rows and smelting columns put their plates onto one belt (a line) instead of a chest per furnace, once belts
# can cross (docs/logistics-plan.md, phase 1): a line is one source of its whole row, not one link per drill.
PLATE_LINES = True


def lines_ready(kn) -> bool:
    """Plates go onto lines once underground belts let belts cross (as coal belts do)."""
    return PLATE_LINES and coal_belts_ready(kn)


def line_record(item: str, tiles: list, facing: str, start: tuple, rate: float) -> dict:
    """A line: one belt of one item, laid along `tiles` in flow order, its last tile (the tail) facing `start`,
    the tile its first link continues on from. "source" is the tail's centre: the key links and budgets use."""
    tail = tuple(tiles[-1])
    return {"item": item, "tiles": [tuple(t) for t in tiles], "facing": facing, "tail": tail,
            "start": tuple(start), "source": (tail[0] + 0.5, tail[1] + 0.5), "rate": round(rate, 2), "taps": 0}


def coal_belts_ready(kn) -> bool:
    """When coal starts moving by belt, the way players switch: the opening fuels burners and furnaces by hand
    (a stack of coal lasts a stone furnace ~37 minutes); coal belts come with logistics. Before underground belts
    every belt is a wall that cuts the base in two, so a long coal belt boxes in the factories it should serve
    (run 20261119-142858: logistics still unresearched at game minute 107, coal belts walling in the red-science
    factory's plate links). Long-handed inserters (automation) lift coal over a row's plate chests."""
    unlocked = getattr(kn, "unlocked", None)
    return unlocked is None or {"underground-belt", "long-handed-inserter"} <= set(unlocked)


class PumpRejected(BuildError):
    """The game refused the offshore pump on a shore tile (the tile is remembered as bad)."""


class NoRoom(BuildError):
    """A chiplet found no spot at this place; the module tries the next place."""


@dataclass
class Site:
    kind: str
    params: dict
    centre: tuple[float, float]
    burners: list[tuple[float, float]] = field(default_factory=list)      # fuel these (drills, furnaces, boilers)
    plate_furnaces: list[tuple[float, float, str]] = field(default_factory=list)   # (x, y, plate) to collect
    chests: list[tuple[float, float, str]] = field(default_factory=list)   # (x, y, item) output chests
    labs: list[tuple[float, float]] = field(default_factory=list)
    poles: list[Tile] = field(default_factory=list)                       # its poles (anchors once powered)
    note: str = ""
    id: int = 0
    entities: list[tuple[str, float, float, str]] = field(default_factory=list)   # everything it placed
    powered: bool | None = None           # None: nothing electric; False: not on the network (yet)
    health: dict = field(default_factory=dict)     # status -> count, from the foreman's last look
    built_minute: float = 0.0
    cells: list[dict] = field(default_factory=list)  # workshop cells: what the courier feeds and empties

    @property
    def electric(self) -> bool:
        return any(e[0] in ELECTRIC for e in self.entities)

    def brief_params(self) -> dict:
        """Its params for a message: a line as its item and rate, not its tiles."""
        out = {k: v for k, v in self.params.items() if k not in ("line", "gaps")}
        if self.params.get("line"):
            out["line"] = {"item": self.params["line"]["item"], "per_minute": self.params["line"]["rate"]}
        return out

    def brief(self) -> dict:
        params = {k: v for k, v in self.params.items() if k not in ("ports", "line", "gaps")}
        if self.params.get("gaps"):                 # buildings never placed (gaps.py): how many, not the list
            params["missing_buildings"] = len(self.params["gaps"])
        if self.params.get("line"):
            params["line"] = {"item": self.params["line"]["item"], "per_minute": self.params["line"]["rate"]}
        if "ports" in self.params:                 # input ports: which a belt feeds, which still wait for one
            ports = self.params["ports"]
            params["ports_fed"] = sorted(str(pt.get("pack") or pt.get("item")) for pt in ports
                                         if pt.get("pack") or pt.get("fed"))
            params["ports_waiting"] = sorted({pt["item"] for pt in ports if pt.get("item") and not pt.get("fed")})
        out = {"id": self.id, "kind": self.kind, **params, "at": [round(self.centre[0]), round(self.centre[1])],
               "buildings": len(self.entities), "built_minute": self.built_minute}
        if self.powered is not None:
            out["powered"] = self.powered
        if self.health:
            out["health"] = self.health
        if self.note:
            out["note"] = self.note
        if self.cells:
            out["cells"] = len(self.cells)
            out["makes"] = sorted({i for c in self.cells for i in c.get("item_out", {})})[:12]
        return out


class Module:
    name = ""
    describe = ""

    def kit(self, w: FreeWorld, base: "Base", p: dict) -> dict[str, int]:
        raise NotImplementedError

    def recipes(self, w: FreeWorld, base: "Base", p: dict) -> set[str]:
        """Recipes its machines will be set to (must be researched before building)."""
        return set()

    def build(self, w: FreeWorld, base: "Base", p: dict) -> Site:
        raise NotImplementedError


class Base:
    """Every module built so far, and the power network's poles."""

    def __init__(self):
        self.sites: list[Site] = []
        self.power_pole: Tile | None = None
        self.next_id = 1

    def add(self, site: Site) -> Site:
        site.id, self.next_id = self.next_id, self.next_id + 1
        self.sites.append(site)
        return site

    def get(self, site_id) -> Site | None:
        return next((s for s in self.sites if str(s.id) == str(site_id)), None)

    def remove(self, site: Site) -> None:
        self.sites = [s for s in self.sites if s is not site]
        if site.kind == "steam_power" and self.power_pole in site.poles:
            # the grid's anchor moves to a station that is on the grid (an island station would not do)
            others = [q for s in sorted(self.of("steam_power"), key=lambda s: s.powered is not True) for q in s.poles]
            self.power_pole = others[0] if others else None

    def of(self, kind: str) -> list[Site]:
        return [s for s in self.sites if s.kind == kind]

    def count(self, kind: str, **match) -> int:
        n = 0
        for s in self.of(kind):
            if all(s.params.get(k) == v for k, v in match.items()):
                n += int(s.params.get("count", 1))
        return n

    def powered_poles(self, exclude: Site | None = None) -> list[Tile]:
        """Poles known to be on the power network: the grid's first station, and sites checked as powered. A new
        steam station is its own little network until it is wired in, so it is no anchor before that."""
        out = [self.power_pole] if self.power_pole else []
        for s in self.sites:
            if s is not exclude and s.powered:
                out += [tuple(q) for q in s.poles]
        return list(dict.fromkeys(out))

    @property
    def centre(self) -> tuple[float, float] | None:
        pts = [s.centre for s in self.sites]
        if not pts:
            return None
        return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


# ------------------------------------------------------------- burner mine
class BurnerMine(Module):
    name = "burner_mine"
    describe = ("burner drills on the nearest patch of `resource`; iron/copper/stone ore drops into a stone "
                "furnace (plates or bricks), coal into an iron chest. 15 ore/min per drill. "
                "args: {resource: iron-ore|copper-ore|coal|stone (exact names), count (1-8), "
                "into: furnace|chest (default: furnace for ores, chest for coal)}")

    @staticmethod
    def _into(p: dict) -> str:
        return p.get("into") or ("chest" if p["resource"] in ("coal", "stone") else "furnace")

    def kit(self, w, base, p):
        n = int(p.get("count", 2))
        out = {"burner-mining-drill": n}
        if self._into(p) == "furnace":
            out["stone-furnace"] = n
        elif w.trees_near(30):
            out["wooden-chest"] = n                     # 2 wood, against 8 iron plates for an iron chest
        else:
            out["iron-chest"] = n                       # no trees close by: plates beat a long walk for wood
        return out

    # The drill's output goes out of its facing side; the furnace (or chest) sits there. Generated per spot,
    # in all four directions, so irregular patches and crowded ground still get drills.
    OUT = {"north": ((0, -2), [(-1, -3), (0, -3), (-1, -2), (0, -2)], (-1, -2)),
           "east": ((2, 0), [(1, -1), (2, -1), (1, 0), (2, 0)], (1, -1)),
           "south": ((0, 2), [(-1, 1), (0, 1), (-1, 2), (0, 2)], (0, 1)),
           "west": ((-2, 0), [(-3, -1), (-2, -1), (-3, 0), (-2, 0)], (-2, 0))}

    def _spots(self, w: FreeWorld, resource: str, n: int, toward, into: str) -> list[tuple[int, int, str]]:
        """Up to n drill spots (cx, cy, facing): 2x2 drill fully on ore, its output tile(s) free. Greedy from the
        patch edge nearest `toward`, so the mine stays compact; spots never overlap each other's footprints."""
        ore = w.resources.get(resource, set())
        taken: set = set()
        out: list[tuple[int, int, str]] = []
        cands = []
        for tx, ty in ore:
            cx, cy = tx + 1, ty + 1
            drill = {(cx - 1, cy - 1), (cx, cy - 1), (cx - 1, cy), (cx, cy)}
            if not drill <= ore or not w.free(drill):
                continue
            for face, (_, furnace, chest) in self.OUT.items():
                foot = {(cx + dx, cy + dy) for dx, dy in (furnace if into == "furnace" else [chest])}
                if w.free(foot) and not foot & drill:
                    cands.append((math.dist((cx, cy), toward), cx, cy, face, drill | foot))
        for _, cx, cy, face, tiles in sorted(cands):
            if len(out) >= n:
                break
            if tiles & taken:
                continue
            out.append((cx, cy, face))
            taken |= tiles
        return out

    def build(self, w, base, p):
        resource, n = p["resource"], int(p.get("count", 2))
        into = self._into(p)
        patch = w.patch(resource)
        if not patch:
            raise BuildError(f"no {resource} patch known, even after exploring")
        toward = base.centre or w.pos
        # the nearest patch first; when it has no room, the next ones (the ground decides, not a fixed spot)
        here = w.pos
        others = sorted((q for q in w.patches if q["name"] == resource and q["tiles"] >= 8 and q is not patch),
                        key=lambda q: math.dist((q["nearest"]["x"], q["nearest"]["y"]), here))
        tried: list = []
        for patch in [patch] + others[:3 if w.blocking else 2]:
            nearest = (patch["nearest"]["x"], patch["nearest"]["y"])
            tried.append(nearest)
            try:
                return self._build_on(w, base, p, resource, n, into, nearest, toward)
            except NoRoom:
                continue
        ore_known = len(w.resources.get(resource, ()))
        raise BuildError(f"no room for a burner drill on {len(tried)} {resource} patch(es) near {tried} "
                         f"({ore_known} ore tiles known; every 2x2 spot was taken or had no free side)")

    def _build_on(self, w, base, p, resource, n, into, nearest, toward) -> Site:
        if math.dist(nearest, w.pos) > 20:
            w.go(*nearest)
        w.observe(32)
        site = Site(self.name, {"resource": resource, "count": 0, "into": into}, nearest)
        chest = "wooden-chest" if w.inventory.get("wooden-chest", 0) > 0 else "iron-chest"
        plate = {"iron-ore": "iron-plate", "copper-ore": "copper-plate", "stone": "stone-brick"}.get(resource, "")
        placed = 0
        for attempt in range(3):
            spots = self._spots(w, resource, n - placed, toward, into)
            if not spots:
                break
            for cx, cy, face in spots:
                if w.inventory.get("burner-mining-drill", 0) <= 0:
                    break
                (fx, fy), furnace, chest_off = self.OUT[face]
                drill_tiles = {(cx - 1, cy - 1), (cx, cy - 1), (cx - 1, cy), (cx, cy)}
                if into == "furnace":
                    tiles = {(cx + dx, cy + dy) for dx, dy in furnace}
                    w.place("stone-furnace", cx + fx, cy + fy, "north", tiles)
                    site.plate_furnaces.append((cx + fx, cy + fy, plate))
                    site.burners.append((cx + fx, cy + fy))
                else:
                    t = (cx + chest_off[0], cy + chest_off[1])
                    w.place(chest, t[0] + 0.5, t[1] + 0.5, "north", {t})
                    site.chests.append((t[0] + 0.5, t[1] + 0.5, resource))
                w.place("burner-mining-drill", cx, cy, face, drill_tiles)
                site.burners.append((cx, cy))
                w.fuel(cx, cy, 5)
                if into == "furnace":
                    w.fuel(cx + fx, cy + fy, 3)
                placed += 1
            if placed >= n:
                break
            w.observe(32)
        if not placed:
            raise NoRoom(f"no room on the {resource} patch near {nearest}")
        site.params["count"] = placed
        xs = [b[0] for b in site.burners]
        ys = [b[1] for b in site.burners]
        site.centre = (sum(xs) / len(xs), sum(ys) / len(ys))
        return site


# ------------------------------------------------------------- steam power
class SteamPower(Module):
    name = "steam_power"
    describe = ("offshore pump + boiler + steam engines at the nearest water, coal chest and burner inserter for "
                "the boiler, one pole. 900 kW per engine, at most 2 per boiler. args: {engines: 1|2}")

    designer = None          # a designer.ChipletDesigner: the AI draws the station each run (None: the library)

    def kit(self, w, base, p):
        e = max(1, min(2, int(p.get("engines", 2))))
        kit = {"offshore-pump": 1, "pipe": 12}
        for name, *_ in self.template(w, e)["parts"]:
            kit[name] = kit.get(name, 0) + 1
        kit["small-electric-pole"] = kit.get("small-electric-pole", 0) + 1     # one spare, for wiring it in
        return kit

    def library_template(self, engines: int) -> dict:
        """The hand-made station: parts, the water inlet and the open ports a pipe must stay off."""
        return {"parts": self.station_parts(engines), "inlet": self.INLET,
                "clear": [(2.0, 0.5), (0.0, -1.5 - 5 * engines)], "by": "library"}

    def template(self, w, engines: int) -> dict:
        """The station to build: the AI's design for this run when there is a designer, else the library's."""
        if self.designer is None:
            return self.library_template(engines)
        from .designer import DesignError
        try:
            return self.designer.station(w.kn, engines, self.library_template)
        except DesignError as exc:                        # ai-only: no fallback, the goal fails with the reason
            raise BuildError(str(exc)) from None

    def build(self, w, base, p):
        e = max(1, min(2, int(p.get("engines", 2))))
        if not w.water_hints:
            w.scan()
        if not w.water_hints and not w.explore("water"):
            raise BuildError("no water found, even after exploring")
        toward = base.centre or w.pos
        waters: list = []                               # the nearest water, then other lakes 45+ tiles away
        for q in sorted(w.water_hints, key=lambda q: math.dist(q, toward)):
            if all(math.dist(q, o) > 45 for o in waters):
                waters.append(q)
        waters = waters[:3 if w.blocking else 2]
        coal = max(0, min(150, w.inventory.get("coal", 0) - 30))
        pole, layout, refused, last, tapped, tap_failed = None, None, 0, "", None, ""
        # One offshore pump gives 1200 water/s, enough for 20 boilers: a new station taps the water line of one
        # that stands before it looks for shore of its own (no new pump, no new pipe run to the water)
        def tap() -> None:
            nonlocal pole, tapped, layout, tap_failed
            for engines in ([e, 1] if e == 2 else [1]):
                layout = self.find_tap_layout(w, base, toward, engines)
                if layout:
                    break
            if layout:
                w.doing(f"tapping the water line of site {layout['tap_site']} for a new boiler")
                mark = len(w.recorder) if w.recorder is not None else None
                try:
                    pole, tapped = self.place_tap(w, layout, coal), layout["tap_site"]
                except BuildError as exc:
                    # take the half-built tap back up and fall through to a shore and pump of its own
                    # (run 20261119-011655: one failed tap pipe ended 41 builds, so no second pump was ever made)
                    tap_failed = str(exc)[:160]
                    if mark is not None:
                        for ent in list(w.recorder[mark:]):
                            try:
                                w.pickup(*ent)
                            except BuildError:
                                pass
                        del w.recorder[mark:]
                    pole, tapped, layout = None, None, None
                    w.log(f"[power] tapping failed ({tap_failed}): a pump of its own instead")
        # the first few stations share a water line; after that each gets its own pump and shore (the ground
        # around one line fills up, and more pumps cost one offshore pump each), tapping only when no shore fits
        own_shore_first = len(base.of("steam_power")) >= 3
        if base.of("steam_power") and not own_shore_first:
            tap()
        for water in ([] if pole is not None else waters):
            w.doing(f"surveying the shore at ({water[0]:.0f}, {water[1]:.0f})")
            w.survey_area(*water, half=36, step=36)    # know every shore tile before choosing a spot
            while pole is None and refused < self.MAX_PUMP_TRIES:
                layout = None
                for engines in ([e, 1] if e == 2 else [1]):   # a smaller station when two engines fit nowhere
                    layout = self.find_layout(w, water, toward, engines)
                    if layout:
                        break
                if not layout:
                    break                               # this shore is used up: the next water
                try:
                    pole = self.place_layout(w, layout, coal)
                except PumpRejected as exc:             # remembered; the next layout avoids that tile
                    refused, last = refused + 1, str(exc)
            if pole is not None:
                break
        if pole is None and own_shore_first:            # no shore of its own fits: share a line after all
            tap()
        if pole is None and tap_failed:
            last = f"{last}; tapping the existing line failed: {tap_failed}" if last else tap_failed
        if pole is None:
            if refused:
                raise BuildError(f"the game refused a pump on {refused} shore tile(s) (last: {last[:160]}). "
                                 "Those tiles are remembered and skipped, so asking again tries new shore tiles"
                                 + ("" if refused >= self.MAX_PUMP_TRIES else "; no other shore spot fits: "
                                    + self.diagnose(w, base, waters[0], toward, e)))
            raise BuildError("no spot fits a power station. " + (f"(tapping failed: {tap_failed}) " if tap_failed else "")
                             + self.diagnose(w, base, waters[0], toward, e))
        e = layout["engines"]
        boiler = next((bx + dx, by + dy) for (bx, by) in [layout["centre"]]
                      for dx, dy, f, name in layout["parts"] if name == "boiler")
        chest = next((layout["centre"][0] + dx, layout["centre"][1] + dy)
                     for dx, dy, f, name in layout["parts"] if name == "iron-chest")
        # The first station is the grid. Any later one is a network of its own until the crew wires it in
        # (Crew.connect, right after the build) and the game shows its engines on the grid's network.
        first = base.power_pole is None
        if first:
            base.power_pole = pole
        site = Site(self.name, {"engines": e}, (pole[0] + 0.5, pole[1] + 0.5),
                    poles=list(layout.get("poles") or [pole]), powered=True if first else False)
        if tapped is not None:
            site.note = f"water from the line of site {tapped} (no new pump)"
        site.burners.append(boiler)
        site.chests.append((chest[0], chest[1], "coal-in"))        # the boiler's coal chest
        return site

    # The station as a chiplet, relative to the boiler's centre, facing north (engines stacked north of it).
    # Generated in four rotations around every shore spot; the water pipe is routed by A* to the inlet.
    PARTS = [("boiler", 0.0, 0.0, "north"), ("iron-chest", 3.0, -0.5, "north"),
             ("burner-inserter", 2.0, -0.5, "east"), ("small-electric-pole", 2.0, -3.5, "north")]
    INLET = (-2.0, 0.5)                                # where the water pipe enters the boiler

    @classmethod
    def station_parts(cls, engines: int) -> list[tuple[str, float, float, str]]:
        """The parts for 1-2 engines. A small pole supplies 2.5 tiles each way, so the pole beside the boiler
        (y -3.5: covers -6..-1) reaches only the first engine (-6..-1); the second (-11..-6) only touches that
        edge and the game leaves it unplugged. Each further engine gets a pole level with it, 5 tiles on (inside
        the 7.5 wire reach, so the game wires it to the first)."""
        parts = list(cls.PARTS)
        for k in range(engines):
            parts.append(("steam-engine", 0.0, -3.5 - 5 * k, "north"))
            if k:
                parts.append(("small-electric-pole", 2.0, -3.5 - 5 * k, "north"))
        return parts
    DIRS = ["north", "east", "south", "west"]
    MAX_PUMP_TRIES = 16

    @staticmethod
    def pump_spots(w: FreeWorld, water) -> list[tuple[Tile, str]]:
        """Shore spots near the water a pump may take, best first: tiles the game said fit, then straight shore
        with water two deep and three wide ahead (a pump needs that), then the rest. Refused ones are skipped."""
        cands = dict.fromkeys(ab.shore_spots(w))
        cands.update(dict.fromkeys((land, d) for land, ds in w.pump_fits.items() for d in ds))
        out = []
        for land, d in cands:
            if land in w.bad_shore or (land, d) in w.bad_pumps or math.dist(land, water) >= 45:
                continue
            if land in w.pump_fits and d not in w.pump_fits[land]:
                continue
            dx, dy = ab.DIRS[d]
            deep = sum((land[0] + k * dx - s * dy, land[1] + k * dy + s * dx) in w.water
                       for k in (1, 2) for s in (-1, 0, 1))
            out.append((0 if land in w.pump_fits else 1 if deep == 6 else 2, (land, d)))
        return out                                  # (tier, (land, dir)): lower tier first

    @classmethod
    def _rot(cls, dx: float, dy: float, face: str, r: int) -> tuple[float, float, str]:
        for _ in range(r):
            dx, dy = -dy, dx                           # a quarter turn clockwise (y grows south)
        return dx, dy, cls.DIRS[(cls.DIRS.index(face) + r) % 4]

    def find_layout(self, w: FreeWorld, water, toward, engines: int) -> dict | None:
        """The best legal station near the water: every shore spot x four rotations, the first fit for each."""
        tpl = self.template(w, engines)
        ranked = sorted(self.pump_spots(w, water), key=lambda q: (q[0], math.dist(q[1][0], toward)))[:40]
        tier = {sp: t for t, sp in ranked}
        found = []
        for land, d in (sp for _, sp in ranked):
            dx0, dy0 = ab.DIRS[d]
            out = (land[0] - dx0, land[1] - dy0)          # the pump outputs on its back side
            if w.blocked(out) or out in w.water:
                continue
            for r in range(4):
                hit = self._first_fit(w, land, d, out, r, tpl, engines, toward)
                if hit:
                    hit["score"] += 1000 * tier[(land, d)]     # a tile the game said fits, then straight shore
                    found.append(hit)
            if len(found) >= 8:
                break
        return min(found, key=lambda f: f["score"]) if found else None

    def _first_fit(self, w: FreeWorld, land, d, out, r: int, tpl: dict, engines: int, toward,
                   avoid_extra: set = frozenset(), keep_off: set = frozenset()) -> dict | None:
        """The nearest legal station for one water source tile `out` and rotation r. avoid_extra: tiles the
        pipe may not use; keep_off: tiles the buildings may not cover (next to another station's fluid boxes)."""
        rparts = [self._rot(dx, dy, f, r) + (name,) for name, dx, dy, f in tpl["parts"]]
        ix, iy, _ = self._rot(*tpl["inlet"], "north", r)
        clear = [self._rot(cx, cy, "north", r)[:2] for cx, cy in tpl["clear"]]
        half = 0.5 if r % 2 == 0 else 0.0                    # boiler centre: (i+.5, j) or (i, j+.5)
        for rad in range(2, 10):
            ring = [(out[0] + oi, out[1] + oj) for oi in range(-rad, rad + 1) for oj in (-rad, rad)]
            ring += [(out[0] + oj, out[1] + oi) for oi in range(-rad + 1, rad) for oj in (-rad, rad)]
            for i, j in sorted(ring, key=lambda q: math.dist(q, toward)):
                bx, by = i + half, j + (0.5 - half)
                tiles: set = set()
                for dx, dy, f, name in rparts:
                    tiles |= w.footprint(name, bx + dx, by + dy, f)
                inlet = (math.floor(bx + ix), math.floor(by + iy))
                if land in tiles or out in tiles or inlet in tiles or not w.free(tiles | {inlet})                         or (tiles | {inlet}) & keep_off:
                    continue
                avoid = tiles | {land} | {(math.floor(bx + cx), math.floor(by + cy)) for cx, cy in clear}
                avoid |= avoid_extra
                path = ab.route(w, out, inlet, avoid=avoid, margin=6)
                if path is None or any(t in w.water for t in path):
                    continue
                return {"land": land, "dir": d, "centre": (bx, by), "parts": rparts, "path": path,
                        "engines": engines, "tiles": tiles,
                        "score": math.dist((bx, by), toward) + 0.5 * len(path)}
        return None

    def place_layout(self, w: FreeWorld, lay: dict, coal: int) -> Tile:
        land, d = lay["land"], lay["dir"]
        w.near(*ab.centre(land))
        r = w.run.place("offshore-pump", *ab.centre(land), d)
        if not r["ok"]:
            msg = r.get("message", "") or ""
            w.bad_pumps.add((land, d))
            fits = re.findall(r"north|east|south|west", msg.split("facing", 1)[1]) if "facing" in msg else []
            if fits:
                w.pump_fits[land] = set(fits) - {d}
            elif "no direction fits" in msg:
                w.bad_shore.add(land)
            raise PumpRejected(f"the shore tile {land} did not take a pump: {r['code']} {msg}")
        w.used.add(land)
        ab.lay(w, "pipe", lay["path"])
        pole = self._place_parts(w, lay, coal)
        w.log(f"[power] pump at {land} facing {d}, {lay['engines']} engine(s), {len(lay['path'])} pipes")
        return pole

    def place_tap(self, w: FreeWorld, lay: dict, coal: int) -> Tile:
        """A station on the water line of another: the pipe (whose first tile joins the old line), then the rest."""
        ab.lay(w, "pipe", lay["path"])
        pole = self._place_parts(w, lay, coal)
        w.log(f"[power] tapped the water line of site {lay['tap_site']}: {lay['engines']} engine(s), "
              f"{len(lay['path'])} pipes, no new pump")
        return pole

    def _place_parts(self, w: FreeWorld, lay: dict, coal: int) -> Tile:
        bx, by = lay["centre"]
        lay["poles"] = []
        for dx, dy, f, name in sorted(lay["parts"], key=lambda q: q[3] != "boiler"):
            x, y = bx + dx, by + dy
            w.place(name, x, y, f, w.footprint(name, x, y, f))
            if name == "small-electric-pole":
                lay["poles"].append((math.floor(x), math.floor(y)))
            if name == "boiler":
                w.fuel(x, y, 20)
            if name == "burner-inserter":
                w.fuel(x, y, 2)
            if name == "iron-chest" and coal:
                w.insert(x, y, "coal", coal)
        return lay["poles"][0]

    @staticmethod
    def fluid_tiles(w: FreeWorld, base: "Base") -> tuple[dict[Tile, int], set[Tile]]:
        """Water pipes of our power stations (tile -> site id), and the tiles of their boilers and engines."""
        pipes: dict[Tile, int] = {}
        machines: set[Tile] = set()
        for s in base.of("steam_power"):
            for name, x, y, d in s.entities:
                if name == "pipe":
                    pipes[(math.floor(x), math.floor(y))] = s.id
                elif name in ("boiler", "steam-engine", "offshore-pump"):
                    machines |= w.footprint(name, x, y, d)
        return pipes, machines

    def find_tap_layout(self, w: FreeWorld, base: "Base", toward, engines: int) -> dict | None:
        """A station whose pipe starts next to an existing water pipe. Its pipe never runs beside the old
        boilers and engines (a pipe joins every fluid box it touches: steam must not reach the water), and its
        buildings keep a tile away from every old pipe and machine."""
        pipes, machines = self.fluid_tiles(w, base)
        if not pipes:
            return None
        near = lambda ts: {(x + dx, y + dy) for x, y in ts for dx, dy in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1))}
        near_machine = near(machines)
        keep_off = near(set(pipes) | machines)
        tpl = self.template(w, engines)
        starts = []
        for p in pipes:
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                n = (p[0] + dx, p[1] + dy)
                if n in pipes or n in near_machine or n in w.water or w.blocked(n) or n not in w.seen:
                    continue
                starts.append((math.dist(n, toward), p, n))
        found = []
        for _, p, n in sorted(starts)[:24]:
            for r in range(4):
                hit = self._first_fit(w, p, None, n, r, tpl, engines, toward,
                                      avoid_extra=near_machine | set(pipes), keep_off=keep_off)
                if hit:
                    hit["tap_site"] = pipes[p]
                    found.append(hit)
            if len(found) >= 6:
                break
        return min(found, key=lambda f: f["score"]) if found else None

    @staticmethod
    def diagnose(w: FreeWorld, base: "Base", water, toward, engines: int) -> str:
        """Why no spot fit, in words the strategist can act on (and not guess from)."""
        owner: dict[Tile, int] = {}
        for s in base.sites:
            for name, x, y, d in s.entities:
                for t in w.footprint(name, x, y, d):
                    owner[t] = s.id
        spots = [s for s in ab.shore_spots(w) if math.dist(s[0], water) < 45]
        if not spots:
            return (f"Cause: no shoreline known near the water at ({water[0]:.0f}, {water[1]:.0f}) "
                    f"({sum(1 for t in w.water if math.dist(t, water) < 45)} water tiles known there). "
                    "Not caused by any of our sites.")
        blocked = {"water/cliff": 0, "unexplored": 0, "our buildings": 0}
        sites: dict[int, int] = {}
        for land, d in sorted(spots, key=lambda s: math.dist(s[0], toward))[:60]:
            dx, dy = ab.DIRS[d]
            out = (land[0] - dx, land[1] - dy)
            for r in (2, 4, 6):
                for i, j in ((out[0] + r, out[1]), (out[0] - r, out[1]), (out[0], out[1] + r), (out[0], out[1] - r)):
                    for t in ab.power_tiles(i, j, engines):
                        if t in w.water or t in w.cliffs:
                            blocked["water/cliff"] += 1
                        elif t not in w.seen:
                            blocked["unexplored"] += 1
                        elif t in owner:
                            blocked["our buildings"] += 1
                            sites[owner[t]] = sites.get(owner[t], 0) + 1
        main = max(blocked, key=blocked.get)
        msg = (f"{len(spots)} shore tiles known; the pump, boiler and {engines} engine(s) need a land strip "
               f"3 x {2 + 5 * engines} next to the pump. Most blocked tiles were {main} ({blocked}).")
        if main == "our buildings" and sites:
            ids = ", ".join(f"site {k}" for k, _ in sorted(sites.items(), key=lambda kv: -kv[1])[:3])
            msg += f" In the way: {ids}."
        elif main == "unexplored":
            msg += " The shore needs exploring; none of our sites is in the way."
        else:
            msg += " The terrain is the limit, not our sites: try engines 1, or another shore."
        return msg


# -------------------------------------------------------------------- labs
class Labs(Module):
    name = "labs"
    describe = ("a small row of labs on the power network (the early game, before science is automated): a feeder "
                "chest at its head that the foreman fills in bulk. Later, build a research_station instead and link "
                "the science factories to it by belt. args: {count: 1-10}")

    def kit(self, w, base, p):
        n = max(1, min(10, int(p.get("count", 2))))
        line = 0
        if base.powered_poles():
            line = 6
        return {"lab": n, "inserter": n, "iron-chest": 1, "small-electric-pole": n + 1 + line}

    def build(self, w, base, p):
        n = max(1, min(10, int(p.get("count", 2))))
        anchors = base.powered_poles()
        if not anchors:
            raise BuildError("labs need power: build steam_power first")
        home = base.centre or w.pos
        # adapt to the ground: every powered pole (nearest the base first), then a shorter row; when the labs
        # block progress, every anchor and down to one lab
        order = sorted(anchors, key=lambda a: math.dist(a, home))[:None if w.blocking else 3]
        best = None
        for size in range(n, 0 if w.blocking else max(0, n // 2 - 1), -1):
            width = 4 * size
            for anchor in order:
                if math.dist(anchor, w.pos) > 40:
                    w.go(anchor[0] + 0.5, anchor[1] + 0.5)
                w.observe(32, x=anchor[0] + 0.5, y=anchor[1] + 0.5)
                for r in range(4, 30):
                    for ox in range(-r, r + 1):
                        for oy in (-r, r):
                            for x0, y0 in ((anchor[0] + ox, anchor[1] + oy), (anchor[0] + oy, anchor[1] + ox)):
                                if w.free_box(x0 - 2, y0 - 2, width + 2, 6):     # the feeder too
                                    d = math.dist((x0 + width / 2, y0), home)
                                    if best is None or d < best[0]:
                                        best = (d, x0, y0)
                    if best:
                        break
                if best:
                    break
            if best:
                n = size
                break
        if not best:
            raise BuildError(f"no room for the labs near any of {len(order)} powered poles, even for a shorter row")
        _, x0, y0 = best
        width = 4 * n
        site = Site(self.name, {"count": n}, (x0 + width / 2, y0 + 1.5))
        for k in range(n):
            lx = x0 + 4 * k
            w.place("lab", lx + 1.5, y0 + 1.5, "north", {(x, y) for x in range(lx, lx + 3) for y in range(y0, y0 + 3)})
            site.labs.append((lx + 1.5, y0 + 1.5))
            pole = (lx + 3, y0 - 1)
            w.place("small-electric-pole", pole[0] + 0.5, pole[1] + 0.5, "north", {pole})
            site.poles.append(pole)
            if k < n - 1:          # this API: an inserter faces the side it picks up from
                w.place("inserter", lx + 3.5, y0 + 1.5, "west", {(lx + 3, y0 + 1)})
        self.add_feeder(w, site)
        return site                      # the crew wires it to the network and checks it has power

    @staticmethod
    def feeder_tiles(site: Site) -> dict:
        """Where a lab row's feeder goes: a chest two tiles west of the first lab, an inserter between, a pole."""
        lx, ly = site.labs[0]
        x0, y0 = math.floor(lx - 1.5), math.floor(ly - 1.5)
        return {"chest": (x0 - 2, y0 + 1), "inserter": (x0 - 1, y0 + 1), "pole": (x0 - 1, y0 - 1)}

    @classmethod
    def add_feeder(cls, w: FreeWorld, site: Site) -> bool:
        """A feeder chest and inserter at the head of the row (and a pole for the inserter). False: no room."""
        if not site.labs or any(what == "lab-feed" for *_, what in site.chests):
            return False
        t = cls.feeder_tiles(site)
        if not w.free({t["chest"], t["inserter"]}):
            return False
        c, i, q = t["chest"], t["inserter"], t["pole"]
        w.place("iron-chest", c[0] + 0.5, c[1] + 0.5, "north", {c})
        w.place("inserter", i[0] + 0.5, i[1] + 0.5, "west", {i})     # picks from the chest, drops into lab 1
        if w.free({q}):
            w.place("small-electric-pole", q[0] + 0.5, q[1] + 0.5, "north", {q})
            site.poles.append(q)
        site.chests.append((c[0] + 0.5, c[1] + 0.5, "lab-feed"))
        return True


# --------------------------------------------------------- research station
class ResearchStation(Module):
    name = "research_station"
    describe = ("a research station on its own clear ground: a row of labs passing packs along with inserters, and 7 "
                "input ports at its head lab. Each science factory's output is then linked to a port by belt "
                "({\"do\": \"link_science\"}, also done on its own), so the labs run without the crew carrying "
                "packs. Its labs count as labs everywhere. args: {count: 2-10}")
    # the station site is kind "labs" (params station: True), so everything that counts or feeds labs sees it

    def kit(self, w, base, p):
        n = max(2, min(10, int(p.get("count", 6))))
        ins = "fast-inserter" if "fast-inserter" in w.kn.unlocked else "inserter"
        return {"lab": n, ins: n - 1 + len(self.PORTS), "small-electric-pole": n + 2 + 6}

    # head ports around the first lab (its top-left tile x0, y0): (inserter tile, facing = the side it picks
    # from, belt end tile, approach tile). Belts arrive head-on, so no port's belt ever points into another's.
    PORTS = [((-1, 0), "west", (-2, 0), (-3, 0)), ((-1, 1), "west", (-2, 1), (-3, 1)), ((-1, 2), "west", (-2, 2), (-3, 2)),
             ((0, -1), "north", (0, -2), (0, -3)), ((1, -1), "north", (1, -2), (1, -3)),
             ((0, 3), "south", (0, 4), (0, 5)), ((1, 3), "south", (1, 4), (1, 5))]

    @staticmethod
    def area(x0: int, y0: int, n: int) -> set[Tile]:
        """Everything the station keeps clear: labs, ports, approach tiles and a ring for belts to come in."""
        return {(x, y) for x in range(x0 - 4, x0 + 4 * n + 1) for y in range(y0 - 4, y0 + 7)}

    LANE = 6                                  # tiles of free ground kept straight out from each port's approach

    @classmethod
    def port_lanes(cls, x0: int, y0: int) -> set[Tile]:
        """Straight out from every port's approach, the way its belt comes in: kept free (a belt lane), so later
        builds cannot wall a port in (run 20261120-023103: both stations' science links found no way in)."""
        out = set()
        for (ix, iy), face, end, appr in cls.PORTS:
            dx, dy = appr[0] - end[0], appr[1] - end[1]
            out |= {(x0 + appr[0] + dx * k, y0 + appr[1] + dy * k) for k in range(1, cls.LANE + 1)}
        return out

    def build(self, w, base, p):
        n = max(2, min(10, int(p.get("count", 6))))
        if not base.powered_poles():
            raise BuildError("a research station needs power: build steam_power first")
        home = tuple(p.get("near") or base.centre or w.pos)   # near the science factories when known
        spot = None
        for size in range(n, 1, -1):          # its own clear ground near the base; a shorter row if need be
            for ring in range(0, 7):
                r = 12 * ring
                cands = [(int(home[0] + dx), int(home[1] + dy)) for dx in range(-r, r + 1, 12)
                         for dy in range(-r, r + 1, 12) if not ring or max(abs(dx), abs(dy)) == r]
                for x0, y0 in sorted(cands, key=lambda q: math.dist(q, home)):
                    w.survey_area(x0 + 2 * size, y0 + 1, half=max(12, 2 * size + 6), step=32)
                    lanes_free = all(w.free_belt(t) for t in self.port_lanes(x0, y0))                         if hasattr(w, "free_belt") else True
                    if w.free(self.area(x0, y0, size)) and lanes_free:
                        spot = (x0, y0)
                        break
                if spot:
                    break
            if spot:
                n = size
                break
        if spot is None:
            raise BuildError("no clear ground for a research station near the base (it needs about "
                             f"{4 * n + 5} x 11 free tiles)")
        x0, y0 = spot
        ins = "fast-inserter" if "fast-inserter" in w.kn.unlocked else "inserter"
        site = Site("labs", {"count": n, "station": True}, (x0 + 2 * n, y0 + 1.5))
        for k in range(n):
            lx = x0 + 4 * k
            w.place("lab", lx + 1.5, y0 + 1.5, "north", {(x, y) for x in range(lx, lx + 3) for y in range(y0, y0 + 3)})
            site.labs.append((lx + 1.5, y0 + 1.5))
            pole = (lx + 3, y0 - 1)
            w.place("small-electric-pole", pole[0] + 0.5, pole[1] + 0.5, "north", {pole})
            site.poles.append(pole)
            if k < n - 1:                     # packs pass down the row (fast inserters keep up with 5 colours)
                w.place(ins, lx + 3.5, y0 + 1.5, "west", {(lx + 3, y0 + 1)})
        ports = []
        for (ix, iy), face, end, appr in self.PORTS:
            t = (x0 + ix, y0 + iy)
            w.place(ins, t[0] + 0.5, t[1] + 0.5, face, {t})
            ports.append({"inserter": t, "end": (x0 + end[0], y0 + end[1]),
                          "approach": (x0 + appr[0], y0 + appr[1]), "pack": None})
        for q in ((x0 - 1, y0 - 1), (x0 - 1, y0 + 3)):   # power for the ports around the head lab
            w.place("small-electric-pole", q[0] + 0.5, q[1] + 0.5, "north", {q})
            site.poles.append(q)
        site.params["ports"] = ports
        # the belt tiles stay free for the links (nothing else may build on the ports' end and approach tiles)
        for pt in ports:
            w.keepout |= {pt["end"], pt["approach"]}
        site.port_lanes = sorted(self.port_lanes(x0, y0))   # kept as belt lanes by the crew (_reserve_margin)
        return site


# ----------------------------------------------------------------- factory
class Factory(Module):
    name = "factory"
    describe = ("a whole automated factory for one `item` at `per_minute` (real automation: belts, no carrying), "
                "designed by the layout engine for the ground it gets: an assembler per intermediate, belts, "
                "undergrounds (once logistics is researched), poles; the product collects in an output chest. "
                "Any item made in assemblers (see factory_can_make in the report), e.g. science, circuits, or "
                "buildings for a mall. A modular block: `inputs` lists what comes in from outside through input "
                "ports, any items at all (plates, gears, circuits, steel...); the rest of its chain it makes "
                "itself, up to 12 assemblers: a bigger chain is split, and its biggest intermediates (gears, "
                "circuits...) come in through ports too, from feeder factories the build makes FIRST (sized to "
                "what is not already spare); if a feeder cannot be built the build fails and names it, rather than "
                "leave ports nothing can feed. Ore is for drills only: by default iron and copper plates are inputs too, so the "
                "factory goes on open ground off the ore, and electric_mine rows supply its plates (built first "
                "when short; then \"link\" belts each mine chest to a port, the courier carries until then). "
                "Items no assembler makes from plates (steel, plastic, sulfur...) are always inputs. "
                "plates: \"mine\" puts its own drills and furnaces on iron and copper instead (needs free ore). "
                "Needs electric mining and automation researched, and power. "
                "args: {item, per_minute (or per_second), inputs?: [items], plates?: \"in\"|\"mine\"}")
    def __init__(self):
        self.designs: dict[tuple, object] = {}
        self.items: list[str] = []
        # designs of factory goals still in the queue, worked out on a thread of their own while the crew does
        # other goals: key -> (design, grid, focus, inputs); and the one being worked out now
        self.ready: dict[tuple, tuple] = {}
        self._pool = None
        self._worker = None
        self._worker_key: tuple | None = None
        self.designer = None                 # fab.llm_place.LLMDesigner: a model that draws floor plans
        self.placer = "fallback"             # with a designer: search | fallback | llm (see _design_at)
        self.case_dir = None                 # a Path: every design's terrain is saved there for the bench

    MAX_ASM = 12                             # bigger chains are split into factories that feed each other
    FEEDS = True                             # plates come in on one belt each (a feed), once lines exist
    # Set by the crew around one build (Crew._set_probes): probe(grid, design) -> [why an input or the output
    # cannot be belted there]; avoid: [(centre, input)] where this item's factory already stood walled in
    probe = None
    avoid: tuple = ()
    AVOID_TILES = 20.0
    # reach(focus) -> [inputs no spare source of which is within a link's reach of the place]: checked BEFORE a
    # design is worked out there (Crew._set_probes). quick: a big chain's first try, at the two best places at the
    # full rate only, so a block that does not fit is split at once (Crew.build) instead of after every place
    reach = None
    quick = False

    def feeds_for(self, w, supplied) -> frozenset:
        """Supplied items that come in on a belt straight onto the factory's own (docs/logistics-plan.md, phase
        2): the plates, once belts can cross. Others (steel, plastic...) keep a chest the courier can stock."""
        if not self.FEEDS or not lines_ready(getattr(w, "kn", None)):
            return frozenset()
        return frozenset(set(supplied) & set(self.PLATES))

    @staticmethod
    def feed_side(base, focus, feeds) -> str | None:
        """The edge the factory's input ports face: toward the nearest source of its first fed plate (a line's
        tail first, else a chest of it), so every link arrives on that side instead of all round."""
        if not feeds or focus is None:
            return None
        pts = []
        for item in sorted(feeds):
            pts += [s.params["line"]["start"] for s in base.sites
                    if s.params.get("line") and s.params["line"]["item"] == item]
            if not pts:
                pts += [(x, y) for s in base.sites for x, y, what in s.chests if what == item]
            if pts:
                break
        if not pts:
            return None
        x, y = min(pts, key=lambda q: math.dist(q, focus))
        dx, dy = x - focus[0], y - focus[1]
        if abs(dx) >= abs(dy):
            return "east" if dx > 0 else "west"
        return "south" if dy > 0 else "north"

    def can_make(self, kn) -> list[str]:
        """Items the layout engine can design a belt factory for: chains of assembler recipes from iron and copper
        plates, plus inputs other factories supply (steel, plastic, sulfur, processing units...: input cells)."""
        if not self.items:
            from fab import recipes
            recipes.load_catalog(kn.cat)
            self.items = sorted(i for i in recipes.RECIPES if "wood" not in recipes.inputs_needed(i))
        return self.items

    PLATES = ("iron-plate", "copper-plate")

    @staticmethod
    def rate(p: dict) -> float:
        """Items a minute asked for (per_minute, or per_second for those who think in items/s)."""
        if p.get("per_minute") is None and p.get("per_second") is not None:
            return round(float(p["per_second"]) * 60, 2)
        return float(p.get("per_minute", 5))

    def plates_in(self, p: dict) -> bool:
        """Plates come in from outside (the default: ore is for drills), unless asked for its own mines."""
        return str(p.get("plates", "in")).lower() != "mine"

    def asked_inputs(self, kn, p: dict) -> set[str]:
        """What the goal says comes in from outside: `inputs` (and the older `split`), plus the plates by
        default. Names checked here, so a made-up item fails the goal with the real choices."""
        self.can_make(kn)
        from fab import recipes as fr
        raw = p.get("inputs") or []
        if isinstance(raw, str):
            raw = [raw]
        if isinstance(raw, dict):                    # {"iron-plate": 90, ...}: the rates are the factory's to plan
            raw = list(raw)
        names = {str(x) for x in raw} | {str(x) for x in (p.get("split") or ())}
        bad = sorted(n for n in names if n not in kn.items and n not in fr.RECIPES)
        if bad:
            raise BuildError(f"unknown input item(s) {bad}: inputs are item names, e.g. iron-plate, iron-gear-wheel, "
                             "electronic-circuit, steel-plate")
        if self.plates_in(p):
            names |= set(self.PLATES)
        return names

    def inputs(self, kn, item: str, rate: float, split=()) -> tuple[set[str], object]:
        """What a belt factory for `item` takes from other factories, and its plan. Items no assembler makes from
        plates always; then, while the chain has more than MAX_ASM assemblers, its biggest intermediate becomes an
        input of this one (a chain of smaller factories routes where one big one cannot). Nothing here builds the
        feeder: Crew._intermediates_first does, before this factory, for every such input that has an assembler
        recipe and is not already spare (run 20261122-133537: green science came out with gear and circuit ports
        nothing fed, 49 of 51 machines idle)."""
        from fab import recipes as fr
        self.can_make(kn)
        supplied = set(fr.inputs_needed(item)) | set(split)
        while True:
            pl = fr.plan(item, rate, supplied=supplied)
            asm = {k: v for k, v in pl.machines.items() if k in fr.RECIPES and k not in supplied}
            if sum(asm.values()) <= self.MAX_ASM:
                return supplied, pl
            cands = [(v, k) for k, v in asm.items() if k != item]
            if not cands:
                return supplied, pl
            supplied.add(max(cands)[1])

    def supplied_for(self, kn, p: dict) -> tuple[set[str], object]:
        """The goal's inputs and its plan (what the crew sizes plate supply and power from, before any layout)."""
        item = p["item"]
        return self.inputs(kn, item, self.rate(p), sorted(self.asked_inputs(kn, p) - {item}))

    def _design(self, w: FreeWorld, base: Base, p: dict):
        item, rate = p["item"], self.rate(p)
        if item not in self.can_make(w.kn):
            raise BuildError(f"no belt factory for {item}: it is made in a furnace, chemical plant or refinery, "
                             "or needs wood: a workshop makes it")
        key = (item, rate)                   # cleared when the goal ends (Crew.build), kept over a pause
        if key in self.designs:
            d, _, _ = self.designs[key]
            # a goal paused and resumed keeps its design while its ground is still free (a fresh design took
            # 10+ minutes in run 20261119-112905 and was thrown away at every pause, so none was ever built)
            taken = {t for t in d.tiles() if w.blocked(t) and t not in w.movable}
            if not taken:
                return self.designs[key]
            w.log(f"[fab] the kept {item} design lost {len(taken)} tiles to other builds: designing again")
            del self.designs[key]
        supplied, _ = self.supplied_for(w.kn, p)
        got = self._take_ready(w, key, supplied)
        if got is not None:
            self.designs[key] = got
            return got
        fed = bool(set(self.PLATES) & supplied)
        if not fed and (not w.patch("iron-ore") or not w.patch("copper-ore")):
            raise BuildError("a factory with its own mines needs iron and copper patches known (or plates: \"in\")")
        ug = 10 if "underground-belt" in w.kn.unlocked else 0
        failures: list[str] = []
        if fed:
            # plates come in: open ground near the base, off the ore (ore is for drills)
            got = self._ladder(w, base, item, rate, supplied, self._fed_places(w, base, p), ug, key, off_ore=True)
            if not isinstance(got, str):
                return got
            failures.append(f"plate-fed: {got}")
            if "plates" in p:                # asked for plates in by name: no mines of its own instead
                raise BuildError(f"no {item} factory fits. {failures[0]}")
            # the default was plates in: try the older design with its own mines on iron and copper too
            supplied = supplied - set(self.PLATES)
            if not w.patch("iron-ore") or not w.patch("copper-ore"):
                raise BuildError(f"no {item} factory fits. {failures[0]}")
        got = self._ladder(w, base, item, rate, supplied, self._places(w, base)[:5], ug, key, off_ore=False)
        if not isinstance(got, str):
            return got
        failures.append(f"with its own mines: {got}")
        raise BuildError(f"no {item} factory fits. " + " | ".join(failures))

    # ------------------------------------------------------------ background designs
    def prefetch(self, w: FreeWorld, base: Base, p: dict, probe=None) -> bool:
        """Start designing a factory goal that is still in the queue, on a thread of its own, from a snapshot of
        the ground seen so far (no walking to look), so the design is ready when the goal comes up instead of
        the crew waiting minutes for it then. One at a time. Returns whether one was started."""
        item = p.get("item")
        if not item or item not in self.can_make(w.kn):
            return False
        rate = self.rate(p)
        key = (item, rate)
        if key in self.designs or key in self.ready or (self._worker is not None and not self._worker.done()):
            return False
        try:
            supplied, _ = self.supplied_for(w.kn, p)
        except BuildError:
            return False
        fed = bool(set(self.PLATES) & supplied)
        places = (self._fed_places(w, base, p) if fed else self._places(w, base))[:6]
        if probe is not None and getattr(probe, "reach", None) is not None:
            places = [f for f in places if not probe.reach(f)]
        places = places[:3]
        if not places:
            return False
        grid = Grid.from_world(w)
        if fed:
            grid.keepout |= set(grid.ore)
        ug = 10 if "underground-belt" in w.kn.unlocked else 0
        anchors, seed = list(base.powered_poles()), len(base.sites) + 1
        if self.designer is None or self.placer == "search":
            order = ["search"]
        else:
            order = ["llm", "search"] if self.placer == "llm" else ["search", "llm"]
        designer = self.designer
        feeds = self.feeds_for(w, supplied)
        sides = {f: self.feed_side(base, f, feeds) for f in places}

        def job():
            for focus in places:
                for placer in order:
                    if placer == "llm" and getattr(designer, "cost_usd", 0) >= getattr(designer, "max_cost", 1e9):
                        continue
                    d = design_factory(grid, item, rate, near=focus, placer=placer,
                                       llm=designer if placer == "llm" else None, log=lambda *a: None, seed=seed,
                                       max_ug_pairs=ug * 2, anchors=anchors, margin=24,
                                       max_attempts=3 if placer == "llm" else None, supplied=supplied,
                                       feeds=feeds, feed_side=sides[focus])
                    if d.ok and probe is not None and probe(grid, d):
                        break                            # an input walled off here: the next place
                    if d.ok:
                        return (d, grid, focus, frozenset(supplied))
            return None

        if self._pool is None:
            from concurrent.futures import ThreadPoolExecutor
            self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="factory-design")
        self._worker, self._worker_key = self._pool.submit(job), key
        w.log(f"[fab] designing the queued {item} factory ({rate}/min) in the background")
        return True

    def _take_ready(self, w: FreeWorld, key: tuple, supplied: set):
        """The background design of this goal, if there is one that still fits: finishes waiting for it when it
        is still being worked out (the base kept running meanwhile), then checks nothing was built on its
        ground since. None: design now."""
        if self._worker is not None and self._worker_key == key and not self._worker.done():
            try:
                w.think(f"finishing the {key[0]} factory design started in the background", self._worker.result)
            except Exception:  # noqa: BLE001 - a failed background design: design now instead
                pass
        if self._worker is not None and self._worker_key == key and self._worker.done():
            try:
                res = self._worker.result()
            except Exception:  # noqa: BLE001
                res = None
            self._worker, self._worker_key = None, None
            if res is not None:
                self.ready[key] = res
        res = self.ready.pop(key, None)
        if res is None:
            return None
        d, grid, focus, inputs = res
        if set(inputs) != set(supplied):
            return None
        taken = {t for t in d.tiles() if w.blocked(t) and t not in w.movable}
        if taken:
            w.log(f"[fab] the background {key[0]} design lost {len(taken)} tiles to other builds: designing again")
            return None
        if self.probe is not None:               # its inputs checked on today's ground, not the snapshot's
            bad = w.think(f"checking the belt routes into the background {key[0]} design", self.probe,
                          Grid.from_world(w), d)
            if bad:
                w.log(f"[fab] the background {key[0]} design fails the route probe ({bad[0][:100]}): designing again")
                return None
        w.log(f"[fab] using the {key[0]} factory designed in the background ({d.placer}, at "
              f"({focus[0]:.0f}, {focus[1]:.0f}))")
        self._save_case(grid, key[0], key[1], focus, f"{d.placer}: ok (background)")
        return (d, grid, focus)

    def _ladder(self, w: FreeWorld, base: Base, item: str, rate: float, supplied: set, places: list, ug: int,
                key, off_ore: bool):
        """The design ladder at these places: the design that works, or why none did (a string)."""
        # The rate asked for at every place, then with more room, then smaller factories. The first design that
        # works is built; a smaller one says so, so another can cover the rest. When this factory blocks
        # progress, the ladder goes on: every place with more room, down to a third of the rate, with more time
        # to design. Any factory that runs beats none.
        if not places:
            return "no open ground known near the base"
        if self.reach is not None:
            # a place too far from a source of one of its inputs never passes the route probe, however well the
            # factory fits: skipped before any design (run 20261125-214645: advanced-circuit designs that took
            # 47-94 s each were thrown away for plastic 223-354 tiles off)
            far = {tuple(f): self.reach(f) for f in places}
            ok = [f for f in places if not far[tuple(f)]]
            if not ok:
                f0 = places[0]
                return (f"every place is beyond a link's reach of an input ({'; '.join(far[tuple(f0)][:2])} at "
                        f"({f0[0]:.0f}, {f0[1]:.0f})): build that input next to here, or this factory next to it")
            if len(ok) < len(places):
                w.log(f"[fab] {len(places) - len(ok)} of {len(places)} places for the {item} factory are beyond a "
                      f"link's reach of an input ({'; '.join(next(v for v in far.values() if v)[:1])}): skipped")
            places = ok
        wide = places if w.blocking else places[:3]
        # Room first: replays of failed runs show the roomy rung (margin 24, 20 underground pairs) designs green
        # science where the tight one (14, 10) finds no route, so the tight rung only gets a quick try at the
        # nearest place. The designs run in the background (FreeWorld.think), so a longer budget costs no upkeep.
        ladder = [(rate, 24, ug * 2, places), (rate, 14, ug, places[:1])]
        ladder += [(round(rate * f, 1), 24, ug * 2, wide)
                   for f in ((2 / 3, 1 / 2, 1 / 3) if w.blocking else (2 / 3, 1 / 2)) if rate * f >= 1]
        budget = 420 if w.blocking else 300
        if self.quick:                           # a big chain: two tries, then it is split (Crew.build)
            ladder = [(rate, 24, ug * 2, places[:2])]
        tried: list[str] = []
        causes: dict[str, int] = {}
        first = None
        probe = self.probe
        walled: dict[tuple, str] = {}            # places whose design left an input (or the output) unroutable
        fallback = None                          # the best design the probe refused: built only as a last resort
        t_start = time.monotonic()
        for r, margin, pairs, spots in ladder:
            for focus in spots:
                if time.monotonic() - t_start > budget:          # never freeze the crew designing for long
                    break
                if tuple(focus) in walled:                       # the place walls an input off at any rate
                    continue
                self._survey(w, focus)
                grid = Grid.from_world(w)
                if off_ore:                                       # leave every ore tile for drills
                    grid.keepout |= set(grid.ore)
                d = self._design_at(w, base, grid, item, r, focus, pairs, margin, full=(r == rate),
                                    supplied=supplied)
                if first is None:
                    first = (grid, focus, r)
                if d.ok and probe is not None:
                    bad = w.think(f"checking the belt routes into a {item} factory at ({focus[0]:.0f}, "
                                  f"{focus[1]:.0f})", probe, grid, d)
                    if bad:
                        walled[tuple(focus)] = bad[0]
                        causes["walled-in input"] = causes.get("walled-in input", 0) + 1
                        tried.append(f"{r}/min at ({focus[0]:.0f}, {focus[1]:.0f}) [probe: {bad[0][:90]}]")
                        if fallback is None:
                            fallback = (d, grid, focus, r, bad)
                        continue
                if d.ok:
                    d.notes.append(f"asked {rate}/min, designed {r}/min" if r < rate else "")
                    if off_ore:
                        d.notes.append("plates come in through input ports")
                    self.designs[key] = (d, grid, focus)
                    self._save_case(grid, item, r, focus, f"{d.placer}: ok" + (" (plate-fed)" if off_ore else ""))
                    return self.designs[key]
                for prob in d.problems:
                    kind = ("ore taken" if "mine cells fit" in prob else "input cells" if "input cells" in prob
                            else "no belt route" if "route" in prob
                            else "no room for assemblers" if "assembler" in prob else "power" if "pole" in prob
                            else "other")
                    causes[kind] = causes.get(kind, 0) + 1
                tried.append(f"{r}/min at ({focus[0]:.0f}, {focus[1]:.0f})")
        if fallback is not None and not self.quick:
            # no place passed the route probe. The best design still goes up (a factory that runs beats none)
            # with a warning, unless it stands where this factory already failed walled in: then nothing is
            # placed and the error names the input (run 20261122-133537: four circuit factories, ~700 buildings)
            d, grid, focus, r, bad = fallback
            near = [(c, inp) for c, inp in (self.avoid or ()) if math.dist(c, focus) <= self.AVOID_TILES]
            if near:
                return (f"no place lets every input reach it by belt: {bad[0]}. The best one, ({focus[0]:.0f}, "
                        f"{focus[1]:.0f}), is within {self.AVOID_TILES:.0f} tiles of where a {item} factory stood "
                        f"with its {near[0][1]} walled in, so it is not built there again (nothing placed). Tried: "
                        f"{', '.join(tried[:4])}. A source of the walled-in input next to the consumer, or room "
                        "cleared between it and its sources, lets the next try pass")
            w.log(f"[fab] WARNING: no place for the {item} factory passes the route probe; building the best one "
                  f"at ({focus[0]:.0f}, {focus[1]:.0f}) anyway: {'; '.join(bad[:2])}")
            d.notes.append(f"route probe failed: {bad[0][:120]}")
            if r < rate:
                d.notes.append(f"asked {rate}/min, designed {r}/min")
            self.designs[key] = (d, grid, focus)
            self._save_case(grid, item, r, focus, f"{d.placer}: ok, route probe failed")
            return self.designs[key]
        if first is not None:                    # the terrain it failed on: a case for the chiplet bench
            self._save_case(first[0], item, first[2], first[1], f"failed: {'; '.join(tried[:3])}"
                            + (" (plate-fed)" if off_ore else ""))
        main = max(causes, key=causes.get) if causes else "unknown"
        advice = {"ore taken": "the ore near these places is covered (burner mines?): replacing burner mines on "
                               "iron and copper frees it, or explore for another patch",
                  "input cells": "no room for its input chests: more open ground, or fewer inputs",
                  "no belt route": "the ground is too crowded to route belts: research logistics for undergrounds, "
                                   "or remove what crowds the spot",
                  "no room for assemblers": "not enough free land: clear ground, or split the chain (inputs)",
                  "power": "the poles could not be planned: check the power network",
                  "walled-in input": "every place leaves an input or the output with no belt route: a source of "
                                     "it next to the consumer, or room cleared between them"}.get(main, "")
        return (f"tried {len(tried)} designs ({', '.join(tried[:6])}). Main cause: {main} ({causes}). {advice}")

    @staticmethod
    def _openness(w, x: float, y: float, ore: set) -> float:
        """Share of the ground within 14 tiles that is free, seen and off ore (unseen counts half)."""
        pts = [(int(x) + dx, int(y) + dy) for dx in range(-14, 15, 2) for dy in range(-14, 15, 2)]
        score = 0.0
        for t in pts:
            if t in w.water or t in w.cliffs or t in w.used or t in ore or t in w.keepout:
                continue
            score += 1.0 if t in w.seen else 0.5
        return score / len(pts)

    def _supply_places(self, w, spots) -> list[tuple[float, float]]:
        """Open ground beside the factory's supply (docs/logistics-plan.md, phase 3): 16-24 tiles from each
        spot (a line's start, a free plate chest), the roomiest first, nearest spot first."""
        if not spots:
            return []
        ore = set().union(*(set(v) for v in w.resources.values())) if w.resources else set()
        groups: dict[str, list[tuple[float, float]]] = {}
        for s in spots:                          # (x, y, what): one group per plate, "out" for its consumer
            groups.setdefault(str(s[2]) if len(s) > 2 else "", []).append((float(s[0]), float(s[1])))
        if len(groups) > 1:
            return self._between(w, groups, ore)
        spots = [(float(s[0]), float(s[1])) for s in spots]
        out: list[tuple[float, float]] = []
        for k, (sx, sy) in enumerate(spots[:3]):
            cands = []
            for r in (16, 24):
                for a in range(8):
                    x = sx + r * math.cos(math.pi * a / 4)
                    y = sy + r * math.sin(math.pi * a / 4)
                    score = self._openness(w, x, y, ore)
                    if score >= 0.6:
                        cands.append((-score, r, (x, y)))
            for _, _, c in sorted(cands)[:2]:
                if all(math.dist(c, q) >= 12 for q in out):
                    out.append(c)
        return out

    def _between(self, w, groups: dict, ore: set, n: int = 4) -> list[tuple[float, float]]:
        """Open ground scored by the summed distance to every group of spots (each plate's sources, the
        consumer's port): a factory with iron on one side of the base and copper on the other goes between them,
        not beside the one it needs most with the other walled off (run 20261122-133537, circuits)."""
        firsts = [pts[0] for pts in groups.values()]
        mid = (sum(p[0] for p in firsts) / len(firsts), sum(p[1] for p in firsts) / len(firsts))
        centres = [mid] + [p for pts in groups.values() for p in pts[:2]]
        cands: dict[tuple, float] = {}
        for cx, cy in centres:
            for r in ((0, 12, 24) if (cx, cy) == mid else (16, 24)):
                for a in (range(8) if r else range(1)):
                    x = cx + r * math.cos(math.pi * a / 4)
                    y = cy + r * math.sin(math.pi * a / 4)
                    score = self._openness(w, x, y, ore)
                    if score >= 0.6:
                        cands[(round(x, 1), round(y, 1))] = score
        cost = lambda c: sum(min(math.dist(c, q) for q in pts) for pts in groups.values())   # noqa: E731
        out: list[tuple[float, float]] = []
        for c in sorted(cands, key=lambda c: (round(cost(c) / 8), -cands[c])):
            if all(math.dist(c, q) >= 12 for q in out):
                out.append(c)
            if len(out) >= n:
                break
        return out

    def _fed_places(self, w, base, p: dict, n: int = 6) -> list[tuple[float, float]]:
        """Where a plate-fed factory may go: beside its supply first, then the open ground near the base."""
        near = self._supply_places(w, p.get("supply_at") or [])
        rest = [c for c in self._open_places(w, base, n) if all(math.dist(c, q) >= 12 for q in near)]
        return (near + rest)[:max(n, len(near))]

    @staticmethod
    def _open_places(w: FreeWorld, base: Base, n: int = 6) -> list[tuple[float, float]]:
        """Where a factory fed from outside could go: the most open ground near the base (free, seen and off ore
        within 14 tiles), nearest first among the roomy ones. Unseen ground counts as half open: a survey looks."""
        home = base.centre or w.pos
        ore = set().union(*(set(v) for v in w.resources.values())) if w.resources else set()
        out: list[tuple[float, float, float]] = []
        for r in (25, 40, 55, 70, 90):
            k = max(8, int(2 * math.pi * r / 20))
            for a in range(k):
                x, y = home[0] + r * math.cos(2 * math.pi * a / k), home[1] + r * math.sin(2 * math.pi * a / k)
                pts = [(int(x) + dx, int(y) + dy) for dx in range(-14, 15, 2) for dy in range(-14, 15, 2)]
                score = 0.0
                for t in pts:
                    if t in w.water or t in w.cliffs or t in w.used or t in ore or t in w.keepout:
                        continue
                    score += 1.0 if t in w.seen else 0.5
                out.append((score / len(pts), r, (x, y)))
        roomy = [q for q in out if q[0] >= 0.7] or sorted(out, key=lambda q: -q[0])[:n]
        roomy.sort(key=lambda q: (q[1], -q[0]))
        picked: list[tuple[float, float]] = []
        for _, _, c in roomy:                    # spread out: no two places within 20 tiles
            if all(math.dist(c, q) >= 20 for q in picked):
                picked.append(c)
            if len(picked) >= n:
                break
        return picked

    def _design_at(self, w: FreeWorld, base: Base, grid: Grid, item: str, r: float, focus, pairs: int,
                   margin: int, full: bool, supplied=frozenset()):
        """One design at one place, by the placers in order: 'search' (simulated annealing only), 'fallback'
        (search; the designer model when search finds nothing at the full rate), 'llm' (the model draws the
        floor plan first; search when it fails)."""
        if self.designer is None or self.placer == "search":
            order = ["search"]
        elif self.placer == "llm":
            order = ["llm", "search"]
        else:
            order = ["search", "llm"] if full else ["search"]   # a smaller factory search finds beats a call
        d = None
        for placer in order:
            if placer == "llm" and getattr(self.designer, "cost_usd", 0) >= getattr(self.designer, "max_cost", 1e9):
                continue                                         # the designer's budget is spent
            t0 = time.monotonic()
            # pure computation on a snapshot of the ground (and a model call for the llm placer): it runs in the
            # background while the foreman keeps the base fuelled, instead of freezing the crew for minutes
            who = "the designer model" if placer == "llm" else "layout search"
            feeds = self.feeds_for(w, supplied)
            # one belt per plate first; a chain one belt cannot thread through every consumer falls back to the
            # chest inputs (replays of saved terrain: feeds designed 41 of 54 cases, chests 38, not the same ones)
            for fed in ((feeds, frozenset()) if feeds else (frozenset(),)):
                d = w.think(f"designing a {item} factory ({who}): {r}/min at ({focus[0]:.0f}, {focus[1]:.0f})",
                            design_factory, grid, item, r, near=focus, placer=placer,
                            llm=self.designer if placer == "llm" else None, log=lambda *a: None,
                            seed=len(base.sites) + 1, max_ug_pairs=pairs, anchors=base.powered_poles(),
                            margin=margin, max_attempts=3 if placer == "llm" else None, supplied=supplied,
                            feeds=fed, feed_side=self.feed_side(base, focus, fed))
                if d.ok:
                    break
            who = f"designer {self.designer.model}" if d.placer == "llm" else "search"
            w.log(f"[fab] {who}: {r}/min, room {margin} at ({focus[0]:.0f}, {focus[1]:.0f}): {d.summary()} "
                  f"({time.monotonic() - t0:.1f} s" + (f", ${self.designer.cost_usd:.3f} so far)"
                                                         if self.designer is not None else ")"))
            if d.ok:
                break
        return d

    def _save_case(self, grid: Grid, item: str, rate: float, focus, outcome: str) -> None:
        """The terrain this factory was designed on, as a chiplet-bench case (runs/<run>/chiplets/)."""
        if self.case_dir is None:
            return
        try:
            self.case_dir.mkdir(parents=True, exist_ok=True)
            n = len(list(self.case_dir.glob("*.grid.json"))) + 1
            stem = f"{n:02d}_{item}_{rate:g}"
            grid.save(self.case_dir / f"{stem}.grid.json")
            (self.case_dir / f"{stem}.json").write_text(json.dumps(
                {"map": f"{stem}.grid.json", "item": item, "per_minute": rate, "near": list(focus),
                 "outcome": outcome}), encoding="utf-8")
        except OSError:
            pass                                 # a full disk never stops the crew

    @staticmethod
    def _places(w: FreeWorld, base: Base) -> list[tuple[float, float]]:
        """Where a factory could go: between each nearby iron and copper patch pair (nearest the base first),
        then the same spots shifted off the busy middle."""
        home = base.centre or w.pos
        near = lambda name: sorted((p for p in w.patches if p["name"] == name and p["tiles"] >= 30),
                                   key=lambda p: math.dist((p["centre"]["x"], p["centre"]["y"]), home))[:3]
        pairs = [((i["centre"]["x"] + c["centre"]["x"]) / 2, (i["centre"]["y"] + c["centre"]["y"]) / 2)
                 for i in near("iron-ore") for c in near("copper-ore")]
        pairs.sort(key=lambda q: math.dist(q, home))
        out = list(pairs[:3])
        if pairs:
            x, y = pairs[0]
            out += [(x + dx, y + dy) for dx, dy in ((30, 0), (-30, 0), (0, 30), (0, -30))]
        return out

    @staticmethod
    def _survey(w: FreeWorld, focus, radius: int = 40, step: int = 36) -> None:
        pts = [(focus[0] + dx, focus[1] + dy) for dx in range(-radius, radius + 1, step)
               for dy in range(-radius, radius + 1, step)]
        for x, y in sorted(pts, key=lambda q: math.dist(q, w.pos)):
            around = [(int(x) + ox, int(y) + oy) for ox in (-12, 0, 12) for oy in (-12, 0, 12)]
            if sum(t in w.seen for t in around) >= 8:
                continue
            if math.dist((x, y), w.pos) <= 60:         # observe_range: look there without walking
                try:
                    w.observe(32, x=x, y=y)
                    continue
                except BuildError:
                    pass
            w.go(x, y)
            w.observe(32)

    def needs(self, w, base, p) -> set[str]:
        """Recipes to unlock BEFORE designing: underground belts once the chain has more than a few belt lines.
        Green science has seven that must cross; without undergrounds no layout exists at any rate (replayed on
        the terrain of a run that tried it before logistics was in)."""
        from fab import recipes as fr
        self.can_make(w.kn)
        lines = len(fr.plan(p["item"], float(p.get("per_minute", 5))).machines)
        return {"underground-belt"} if lines > 4 else set()

    def recipes(self, w, base, p):
        d, _, _ = self._design(w, base, p)
        return {c.recipe for c in d.cells if c.kind == "asm" and c.recipe}

    def kit(self, w, base, p):
        d, _, focus = self._design(w, base, p)
        kit = d.kit()
        anchors = base.powered_poles()
        if anchors and d.poles:                  # the line back to the network: ~one pole per 6 tiles
            gap = min(math.dist(a, q) for a in anchors for q in d.poles)
            kit["small-electric-pole"] = kit.get("small-electric-pole", 0) + math.ceil(gap / 6) + 2
        return kit

    def build(self, w, base, p):
        if not base.powered_poles():
            raise BuildError("a factory needs power: build steam_power first")
        d, grid, focus = self._design(w, base, p)
        report = fab_build(w, d, w.log, anchors=())     # the crew runs the line to the network after
        site = Site(self.name, {"item": p["item"], "per_minute": float(d.per_minute)}, focus)
        for c in d.cells:
            if c.kind != "asm":
                for e in c.entities():
                    if e.name == "stone-furnace":
                        site.burners.append((e.x, e.y))
        for r in d.routes:
            for t in r.chests:
                site.chests.append((t[0] + 0.5, t[1] + 0.5, p["item"]))
        site.poles = [tuple(q) for q in d.poles]
        # input ports: where belts from the factories that make its inputs deliver; until one arrives, the courier
        # keeps each input chest stocked (a stand-in cell, so the courier and the planner's books see it)
        ports = []
        for pt in d.ports():
            count = sum(1 for q in d.ports() if q["item"] == pt["item"]) or 1
            per = round(d.plan.supplied.get(pt["item"], 0) / count, 2)
            port = {"inserter": pt["inserter"], "end": pt["end"], "approach": pt["approach"], "chest": pt["chest"],
                    "item": pt["item"], "per_minute": per, "fed": None}
            ports.append(port)
            w.keepout |= {tuple(pt["end"]), tuple(pt["approach"])}
            if pt.get("feed"):                   # a belt straight in: nothing for the courier to stock
                port["feed"] = True
                continue
            cx, cy = pt["chest"][0] + 0.5, pt["chest"][1] + 0.5
            site.cells.append({"machine": "input", "recipe": None, "x": cx, "y": cy, "in": [(cx, cy)], "out": [],
                               "item_in": {pt["item"]: per}, "item_out": {}, "fluid_in": {}, "fluid_out": {},
                               "slot": None, "sink": False, "port": port})
        if ports:
            site.params["ports"] = ports
            site.params["inputs"] = sorted({pt["item"] for pt in ports})
            # each port's way out of the floor stays a lane (belts only): later buildings walled ports in
            from fab.design import port_escapes
            try:
                ways = port_escapes(grid, d, underground="underground-belt" in w.kn.unlocked)
            except Exception:  # noqa: BLE001 - a lane is a courtesy: the build stands without it
                ways = {}
            site.port_lanes = sorted({tuple(t) for path in ways.values() if path for t in path})
        site.note = f"{report['placed']} built" + (f", {len(report['failed'])} failed (first: "
                                                   f"{report['failed'][0][:120]})" if report["failed"] else "")
        if report.get("gaps"):                   # kept on the site: the gap alert has repair place them
            from .gaps import record
            record(site, report["gaps"])
        if d.per_minute < self.rate(p):
            site.note += (f"; designed for {d.per_minute}/min of the {self.rate(p)}/min asked "
                          "(the largest that fit): build another for the rest")
        self.designs.clear()
        if report["failed"] and report["placed"] < len(report["failed"]) * 2:
            raise BuildError(f"factory mostly failed: {report['failed'][0][:120]}")
        return site


# ----------------------------------------------------------- electric mine
DRILL_REACH = 2              # an electric drill mines a 5 x 5 area around its centre (mining_radius 2.49)


def reserved(w, tiles, own=None) -> bool:
    """Is any of these tiles inside an ore field set aside for a big build (outposts: a dense field planned ahead
    of purple and yellow science)? Ordinary mines keep off it, so the field gets the clean vein it was planned on.
    `own`: the key of the field being built, which may of course use its own ground."""
    fields = getattr(w, "ore_fields", None)
    if not fields:
        return False
    boxes = [f for k, f in fields.items() if k != own]
    return any(f[0] <= x <= f[2] and f[1] <= y <= f[3] for f in boxes for x, y in tiles)


def foreign_ore(w, resource: str) -> set:
    """Tiles of every other ore a drill could mine (crude oil is pumped, not mined)."""
    out: set = set()
    for name, tiles in (getattr(w, "resources", {}) or {}).items():
        if name not in (resource, "crude-oil"):
            out |= set(tiles)
    return out


def mixes_ore(cx: int, cy: int, foreign: set) -> bool:
    """Would a drill centred here also mine another ore? Patches that touch are common (the start area has all
    four close together), and a drill on the edge sends both down one belt: copper plates in an iron chest, a
    factory's input belt jammed with what its inserters do not take."""
    return bool(foreign) and any((cx + dx, cy + dy) in foreign for dx in range(-DRILL_REACH, DRILL_REACH + 1)
                                 for dy in range(-DRILL_REACH, DRILL_REACH + 1))


# A drill put on ground that is nearly mined out runs dry within the hour, and its mine is rebuilt again (run
# 20261121-140755: replacements went next to the old rows, onto the thin edge they had left). Where the game told
# us the amounts, a drill goes only where its 5x5 mining area holds at least this much ore: about three hours of a
# drill into a furnace.
MIN_DRILL_ORE = 3500


def drill_ore(w, cx: int, cy: int, reach: int = DRILL_REACH) -> int | None:
    """Ore in a drill's mining area as of the last look (None: never measured there)."""
    amounts = getattr(w, "ore_amount", None)
    if not amounts:
        return None
    vals = [amounts[(cx + dx, cy + dy)] for dx in range(-reach, reach + 1) for dy in range(-reach, reach + 1)
            if (cx + dx, cy + dy) in amounts]
    return sum(vals) if vals else None


def thin_ground(w, cx: int, cy: int) -> bool:
    got = drill_ore(w, cx, cy)
    return got is not None and got < MIN_DRILL_ORE


class ElectricMine(Module):
    name = "electric_mine"
    describe = ("a row of electric drills on the nearest patch of `resource`, each dropping into a furnace whose "
                "plates an inserter puts in a chest (iron, copper, stone -> bricks), or straight into a chest (coal, "
                "or `into: chest`). ~19 plates or 30 ore a minute each, buffers of 3200 per chest. Once logistics "
                "is researched its furnaces burn coal from a belt behind the chests (a long-handed inserter each) "
                "that the crew links to an electric coal mine right away; before that they are fed by hand, and "
                "get their coal belt later where they stand. The plate supply for factories and "
                "workshops. Needs electric-mining-drill researched and power. For iron and copper plates and "
                "for coal a dense field near the base ({\"module\": \"outpost\", \"near_base\": true}) mines the "
                "same patch twice as densely and smelts beside it: use rows for stone, raw ore, or where the "
                "field finds no room. args: {resource, count (2-12), into: furnace|chest}")
    # a north-facing unit, relative to the drill's centre tile (cx, cy): the drill drops on (cx, cy - 2)
    # (catalog drop_vector 0, -1.85), the furnace covers it, an inserter above lifts plates into a chest
    UNIT_W = 3

    @staticmethod
    def _into(p: dict) -> str:
        return p.get("into") or ("chest" if p["resource"] in ("coal",) else "furnace")

    def layout(self, w, p: dict) -> str:
        """chest, furnace (a plate chest per unit) or line (every unit's plates onto one belt): plate rows are
        lines once belts can cross, unless the goal says line: false."""
        into = self._into(p)
        if into == "furnace" and p.get("line", True) and lines_ready(getattr(w, "kn", None)):
            return "line"
        return into

    def _count(self, p: dict) -> int:
        """Drills asked for, at least the bulk minimum whoever asks (the planners, the tidy-up, a swap, a rebuild:
        run 20261124-015025 had 11 of 20 mines at 1-2 drills, each running dry and moving belts within hours)."""
        from .supply import least_units
        lo = least_units(self.name, p.get("resource"), self._into(p))
        return max(lo, min(12, int(p.get("count") or 4)))

    def kit(self, w, base, p):
        n = self._count(p)
        lay = self.layout(w, p)
        out = {"electric-mining-drill": n, "small-electric-pole": n // 2 + 2}
        if lay != "line":
            out["iron-chest"] = n
        else:
            out["transport-belt"] = 3 * n + 2
        if lay in ("furnace", "line"):
            out.update({"stone-furnace": n, "inserter": n})
            if self.fuel_belt(w):
                out["long-handed-inserter"] = n
                out["transport-belt"] = out.get("transport-belt", 0) + 3 * n
        return out

    @staticmethod
    def fuel_belt(w) -> bool:
        """Furnace rows get a coal belt once coal belts are worth it (see coal_belts_ready)."""
        return coal_belts_ready(getattr(w, "kn", None))

    def unit(self, cx: int, cy: int, into: str, fuel: bool = False) -> dict:
        """One drill's tiles, north-facing. With fuel: a coal belt runs east along row cy - 6, behind the plate
        chests, and a long-handed inserter at (cx + 1, cy - 4) lifts coal from it over the chest row into the
        furnace (it picks up 2 tiles north, drops 2 tiles south)."""
        drill = {(cx + dx, cy + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)}
        if into == "chest":
            return {"drill": drill, "rest": {(cx, cy - 2)}, "chest": (cx, cy - 2)}
        furnace = {(cx, cy - 3), (cx + 1, cy - 3), (cx, cy - 2), (cx + 1, cy - 2)}
        u = {"drill": drill, "rest": furnace | {(cx, cy - 4), (cx, cy - 5)}, "furnace": (cx + 1, cy - 2),
             "inserter": (cx, cy - 4), "chest": (cx, cy - 5)}
        if into == "line":                       # the chest tile and the two beside it carry the row's line
            u["line"] = [(cx, cy - 5), (cx + 1, cy - 5), (cx + 2, cy - 5)]
            u["rest"] = u["rest"] | set(u["line"])
        if fuel:
            u["fuel_inserter"] = (cx + 1, cy - 4)
            u["belt"] = [(cx - 1, cy - 6), (cx, cy - 6), (cx + 1, cy - 6)]
            u["rest"] = u["rest"] | {u["fuel_inserter"]} | set(u["belt"])
        return u

    @staticmethod
    def fuel_port(first_cx: int, cy: int) -> tuple[Tile, Tile]:
        """Where a coal link joins a row's fuel belt: the link's last belt (end) points east into the first fuel
        belt tile and is reached head-on from the approach tile west of it."""
        return (first_cx - 2, cy - 6), (first_cx - 3, cy - 6)

    def _rows(self, w: FreeWorld, resource: str, n: int, into: str, toward,
              fuel: bool = False) -> list[tuple[int, int]]:
        """The best run of up to n units side by side: drills on ore, everything free, nearest `toward`."""
        ore = w.resources.get(resource, set())
        foreign = foreign_ore(w, resource)
        best: list = []
        best_score = None
        starts = sorted(ore, key=lambda t: math.dist(t, toward))[:600]
        for sx, sy in starts:
            run: list = []
            cx = sx
            while len(run) < n:
                u = self.unit(cx, sy, into, fuel)
                pole = ({(cx + 2, sy - 3)} if into in ("furnace", "line") else {(cx + 2, sy - 2)}) if len(run) % 2 == 0                     else set()          # a chest row's pole too (site 50 of run 20261119-154451 got none)
                port = set(self.fuel_port(cx, sy)) if fuel and not run else set()
                if sum(t in ore for t in u["drill"]) < 7 or mixes_ore(cx, sy, foreign) or thin_ground(w, cx, sy) \
                        or reserved(w, u["drill"]) or not w.free(u["drill"] | u["rest"] | pole | port):
                    break
                run.append((cx, sy))
                cx += self.UNIT_W
            if not run:
                continue
            score = (-len(run), math.dist(run[0], toward))
            if best_score is None or score < best_score:
                best, best_score = run, score
                if len(best) >= n:
                    break
        return best

    def build(self, w, base, p):
        resource, n, into = p["resource"], self._count(p), self._into(p)
        if not base.powered_poles():
            raise BuildError("electric drills need power: build steam_power first")
        patch = w.patch(resource, min_tiles=30)
        if not patch:
            raise BuildError(f"no {resource} patch of 30+ tiles known")
        others = sorted((q for q in w.patches if q["name"] == resource and q["tiles"] >= 30),
                        key=lambda q: math.dist((q["centre"]["x"], q["centre"]["y"]), base.centre or w.pos))
        plate = {"iron-ore": "iron-plate", "copper-ore": "copper-plate", "stone": "stone-brick"}.get(resource, resource)
        from .belts import LINK_REACH
        made = plate if into == "furnace" else resource
        # where what it makes is wanted: plates in chests no link can reach help nobody (run 20261121-140755:
        # ~150 copper a minute sat in full chests at x~390 while every copper consumer starved near x~-80)
        wanted = [tuple(pt["end"]) for s in base.sites for pt in s.params.get("ports", [])
                  if pt.get("item") == made and not pt.get("trunk")] if resource != "coal" else []
        too_far: list[float] = []
        for q in others[:3]:
            c = (q["centre"]["x"], q["centre"]["y"])
            if wanted and min(abs(c[0] - e[0]) + abs(c[1] - e[1]) for e in wanted) > LINK_REACH + 60:
                too_far.append(min(abs(c[0] - e[0]) + abs(c[1] - e[1]) for e in wanted))
                continue                                 # a big patch's edge may still be in reach: 60 tiles slack
            w.survey_area(*c, half=min(40, max(12, int(math.sqrt(q["tiles"])))), step=32)
            lay = self.layout(w, p)
            fuel = lay in ("furnace", "line") and self.fuel_belt(w) and w.inventory.get("long-handed-inserter", 0) >= 1
            units = self._rows(w, resource, n, lay, base.centre or c, fuel)
            if not units:
                continue
            if wanted and min(abs(units[0][0] - e[0]) + abs(units[0][1] - e[1]) for e in wanted) > LINK_REACH:
                too_far.append(min(abs(units[0][0] - e[0]) + abs(units[0][1] - e[1]) for e in wanted))
                continue
            site = Site(self.name, {"resource": resource, "count": 0, "into": lay}, c)
            from . import belts
            strip = [t for cx, cy in units for t in self.unit(cx, cy, lay, fuel).get("belt", [])] if fuel else []
            if strip and belts.strip_blocked(w, strip) is not None:
                continue                             # its coal strip would end against a belt and spill onto it
            with belts.job_for(w, ["coal"], strip):          # its fuel strip ends dead: it joins no belt
                for k, (cx, cy) in enumerate(units):
                    u = self.unit(cx, cy, lay, fuel)
                    ch = u["chest"]
                    if lay in ("furnace", "line"):
                        fx, fy = u["furnace"]
                        w.place("stone-furnace", fx, fy, "north", {t for t in u["rest"] if cy - 3 <= t[1] <= cy - 2})
                        site.plate_furnaces.append((fx, fy, plate))
                        site.burners.append((fx, fy))
                        w.fuel(fx, fy, 10)                   # enough until the coal belt is linked
                        ix, iy = u["inserter"]
                        w.place("inserter", ix + 0.5, iy + 0.5, "south", {(ix, iy)})   # picks from the furnace below
                        if fuel:
                            qx, qy = u["fuel_inserter"]      # picks from the coal belt 2 tiles north
                            w.place("long-handed-inserter", qx + 0.5, qy + 0.5, "north", {(qx, qy)})
                            for t in u["belt"]:
                                w.place("transport-belt", t[0] + 0.5, t[1] + 0.5, "east", {t})
                    if lay != "line":
                        w.place("iron-chest", ch[0] + 0.5, ch[1] + 0.5, "north", {ch})
                        site.chests.append((ch[0] + 0.5, ch[1] + 0.5, plate if into == "furnace" else resource))
                    w.place("electric-mining-drill", cx + 0.5, cy + 0.5, "north", u["drill"])
                    if k % 2 == 0:
                        pole = (cx + 2, cy - 3) if lay in ("furnace", "line") else (cx + 2, cy - 2)
                        if w.free({pole}):
                            w.place("small-electric-pole", pole[0] + 0.5, pole[1] + 0.5, "north", {pole})
                            site.poles.append(pole)
                    site.params["count"] += 1
            w.keepout |= set(belts.strip_ends(strip))       # no later route passes in front of its dead end
            xs = [u[0] for u in units]
            site.centre = (sum(xs) / len(xs) + 0.5, units[0][1] - 1.5)
            if lay == "line":
                site.params["line"] = self.lay_line(w, base, units, plate)
            if fuel:                                     # the coal input: the crew belts coal into it right away
                end, appr = self.fuel_port(*units[0])
                site.params["ports"] = [{"item": "coal", "end": list(end), "approach": list(appr), "fuel": True,
                                         "per_minute": round(1.4 * len(units), 1), "fed": None}]
                site.params["fuel"] = "belt"
                w.keepout |= {end, appr}
            elif into == "furnace":
                site.params["fuel"] = "hand"
            return site
        if too_far:
            raise BuildError(f"the {resource} patches with room are {min(too_far):.0f}+ tiles from every {made} "
                             f"consumer, beyond a belt link's {LINK_REACH}: its {made} would sit in chests. Build "
                             f'{{"do": "build", "module": "outpost", "resource": "{resource}"}} (its trunk belts '
                             "the ore home to a smelting column), or the consumer beside the patch")
        raise BuildError(f"no room for a row of electric drills on the {resource} patches near the base")

    PLATE_RATE = 18.75           # a stone furnace's plates a minute: the slower half of a drill-into-furnace unit

    def lay_line(self, w, base, units: list, plate: str) -> dict:
        """The row's line: a belt along the plate side of the units (y = cy - 5) flowing toward the base, its
        first link continuing straight on from the tail (no inserter between: a whole row through one belt).
        The tiles beyond the tail stay a lane, so nothing but a belt is built there."""
        y = units[0][1] - 5
        x0, x1 = units[0][0], units[-1][0]
        home = base.centre or w.pos
        east_first = home[0] >= (x0 + x1) / 2
        free = getattr(w, "free_belt", None) or (lambda t: True)
        east = east_first
        for cand in ((True, False) if east_first else (False, True)):
            if free((x1 + 1, y) if cand else (x0 - 1, y)):
                east = cand
                break
        xs = list(range(x0, x1 + 1)) if east else list(range(x1, x0 - 1, -1))
        facing = "east" if east else "west"
        tiles = [(x, y) for x in xs]
        from . import belts
        with belts.job_for(w, [plate], tiles):            # its links run on from the tail: they declare that join
            for t in tiles:
                w.place("transport-belt", t[0] + 0.5, t[1] + 0.5, facing, {t})
        step = 1 if east else -1
        start = (tiles[-1][0] + step, y)
        lanes = getattr(w, "lanes", None)
        if lanes is not None:
            lanes |= {(start[0] + step * k, y) for k in range(3)}
        return line_record(plate, tiles, facing, start, len(units) * self.PLATE_RATE)


# ---------------------------------------------------------------- pumpjack
class Pumpjack(Module):
    name = "pumpjack"
    describe = ("pumpjacks on the nearest crude-oil wells, each with a pole, wired to the power network. Pumping "
                "crude oil completes the oil-processing trigger technology (refinery, chemical plant). The oil "
                "stays in the pumpjacks: no module pipes it to a refinery yet. Needs oil-gathering researched "
                "(red + green; the crew queues it) and 5 steel per pumpjack (the crew smelts it). args: {count: 1-4}")

    def kit(self, w, base, p):
        n = max(1, min(4, int(p.get("count", 1))))
        return {"pumpjack": n, "small-electric-pole": n}

    def build(self, w, base, p):
        n = max(1, min(4, int(p.get("count", 1))))
        if not base.powered_poles():
            raise BuildError("a pumpjack needs power: build steam_power first")
        home = base.centre or w.pos
        wells = w.oil_wells(near=home)
        if not wells:
            far = w.oil_searched()
            if w.oil_rings >= w.OIL_MAX_RINGS:
                raise BuildError(f"no crude oil within ~{far} tiles of the base after searching {w.oil_rings} rings: "
                                 "this map has no oil in reach; plan the rest without oil (no plastics, blue science)")
            raise BuildError(f"no crude oil within ~{far} tiles of the base yet ({w.oil_rings} of {w.OIL_MAX_RINGS} "
                             "search rings walked); the next pumpjack goal searches farther out")
        taken = {(e[1], e[2]) for s in base.sites for e in s.entities if e[0] == "pumpjack"}
        if not [q for q in wells if q not in taken and not any(w.blocked(t) for t in w.footprint("pumpjack", *q))]:
            # every well we know is pumped: a workshop that needs crude must still get it, so the search goes on
            # outward (run 20261124-015025: 2 wells, both pumped for plastic; chemical science never got crude)
            w.doing("every known crude-oil well is pumped: searching farther out for another field")
            wells = w.oil_wells(near=home, more=True)
            if not [q for q in wells if q not in taken]:
                raise BuildError(f"all {len(wells)} known crude-oil wells are pumped and no other field within "
                                 f"~{w.oil_searched()} tiles of the base ({w.oil_rings} of {w.OIL_MAX_RINGS} rings "
                                 "searched); the next pumpjack goal searches farther out")
        site = Site(self.name, {"count": 0}, wells[0])
        last, placed = "", []
        for x, y in wells:
            if site.params["count"] >= n or w.inventory.get("pumpjack", 0) <= 0:
                break
            if (x, y) in taken:
                continue
            tiles = w.footprint("pumpjack", x, y)
            if math.dist((x, y), w.pos) > 40:
                w.go(x, y + 3)
            w.observe(16, x=x, y=y)
            if any(w.blocked(t) for t in tiles):
                last = f"well at ({x}, {y}) is covered"
                continue
            try:
                w.place("pumpjack", x, y, "north", tiles)
            except BuildError as exc:                   # wells too close to each other, cliffs: the next one
                last = str(exc)
                continue
            site.params["count"] += 1
            placed.append((x, y))
            pole = next((t for t in self._beside(tiles) if w.free({t})), None)
            if pole is not None:
                w.place("small-electric-pole", pole[0] + 0.5, pole[1] + 0.5, "north", {pole})
                site.poles.append(pole)
        if not site.params["count"]:
            raise BuildError(f"no pumpjack could be placed on {len(wells)} known crude-oil wells (last: {last[:160]})")
        site.centre = (sum(q[0] for q in placed) / len(placed), sum(q[1] for q in placed) / len(placed))
        return site                      # the crew wires it to the network and checks it has power

    @staticmethod
    def _beside(tiles: set) -> list[Tile]:
        """Tiles next to a footprint's sides (a small pole there powers the whole 3x3 pumpjack)."""
        xs, ys = [t[0] for t in tiles], [t[1] for t in tiles]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        mx, my = (x0 + x1) // 2, (y0 + y1) // 2
        return [(x0 - 1, my), (x1 + 1, my), (mx, y0 - 1), (mx, y1 + 1),
                (x0 - 1, y0), (x1 + 1, y0), (x0 - 1, y1), (x1 + 1, y1)]


MODULES: dict[str, Module] = {m.name: m for m in (BurnerMine(), SteamPower(), Labs(), ResearchStation(), Factory(),
                                                  Pumpjack(), ElectricMine())}

import sys as _sys  # noqa: E402

if f"{__package__}.workshop" not in _sys.modules:      # workshop and silo register themselves in MODULES
    from . import workshop as _workshop  # noqa: E402,F401
if f"{__package__}.outposts" not in _sys.modules:      # so do the outpost and its smelting column
    from . import outposts as _outposts  # noqa: E402,F401
