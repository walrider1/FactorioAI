"""Zones: the hybrid floorplan (docs/hybrid-plan.md, section 1). The brain keeps building organically in its START
YARD; from the first electric build an Atlas floorplan only RESERVES land outside it (the spine out of the yard, the
corridor to the oil, the silo spot, the next era's slots); from segment 1 on, the spine's blocks are placed into
their slots, designed with Atlas's strip designer, and the bus between them is laid for that era only.

    z = Zones()                                   # crew.zones; world.zones is the same object
    z.reserve(world, base)                        # start yard + floorplan + reserved tiles (world.free refuses them)
    z.blocks(tiles), z.blocks_belt(tile)          # what FreeWorld.free / free_belt ask
    seg = z.open_segment(1, world)                # slots, strip designs and the bus for red+green 30/min
    z.slot_block(item)                            # the segment's next unbuilt block for this item (or None)
    slot_build(z, block, world)                   # an Atlas block design as what Factory.build places

Atlas (bots/atlas) is used as a LIBRARY: its ledger, floorplan, bus, block designer and production graph. Atlas
itself is not run and nothing inside bots/atlas is changed.

Rules:
- the start yard is never reserved: it is a box around the base centre sized to the opening's buildings;
- nothing is reserved on or next to a building of ours (SITE_MARGIN), nor on any ore tile (the brain mines
  organically wherever it likes), nor on water, cliffs or unseen land;
- buildings may not stand on reserved tiles; belts may cross the corridors (spine, oil) but not the slots;
- a segment is sized to its era's targets (the production graph adds SUPPLY_MARGIN = 1.2 to every intermediate),
  never to the rocket's.

`ENABLED = False` turns all of it off: nothing is reserved, world.free behaves as before, factories go free-form.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# Off until the segment bus and its taps are placed (README "Zones (hybrid)"): without the bus, a segment's
# factories are era-sized blocks fed by point-to-point links across the spine, on 18-23k reserved tiles taken
# from organic builds, a cost with none of the benefit yet. Tests switch it on (tests/test_zones.py).
# BRAIN_ZONES=1 forces it on, BRAIN_ZONES=0 off; unset: DEFAULT_ON. Either way zones only ever start in a fresh
# world: a world whose first zones build comes after START_BY_MIN (a resumed old base) never gets them.
DEFAULT_ON = True                 # on for new worlds since the overnight tests (test 5: green science automated at 199)
ENABLED = {"1": True, "0": False}.get(__import__("os").environ.get("BRAIN_ZONES", ""), DEFAULT_ON)
START_BY_MIN = 90.0

Tile = tuple[int, int]
YARD_MIN_HALF = 36        # the start yard reaches at least this far from the base centre each way
YARD_MAX_HALF = 90        # ... and at most this far
YARD_MARGIN = 10          # beyond the outermost opening building in it
YARD_REACH = 90           # buildings further than this from the centre are not the opening's (outposts, far mines)
SITE_MARGIN = 2           # reserved land keeps this far from any building of ours, its lanes and keepouts
HEAD = 26                 # spine tiles from its start kept for the head terminals (plates come in there by trunk)
HEAD_GAP = 4              # head terminals are this far apart (two bus taps side by side need four)
SPARE_SLOTS = 2           # make slots reserved beyond the era's blocks: a second copy, or a slot a design fits better
# General slots: more reserve slots along the spine for factories no era plans (belts, inserters, pipes, the next
# tiers' chains...). A free-form factory goal is designed into a free one (Factory._general_slot_design) before
# any open ground elsewhere: the base keeps growing along the spine instead of round the start yard.
GENERAL_SLOTS = 4
# Growth: a segment's bus is laid out from the start as if a copy of its science blocks stood in the spare slots
# (re-routing a laid bus for a copy added later moved 337 of 658 bus pieces in the fixture's segment 1). Nothing of
# a copy is built until its block runs flat out: then the copy and its few bus taps go up, nothing re-routed.
GROWTH = True
# No copies of the science blocks: the slot's labs take each pack from one bus line (design_bus keeps one line
# per spine half), the originals feed those lines, and bus-fed labs are not hand-fed, so a science copy's packs
# would only fill its chest. The intermediates every science block draws on (gears, circuits) get the copies.
GROWTH_SKIP = ("automation-science-pack", "logistic-science-pack", "chemical-science-pack",
               "military-science-pack", "production-science-pack", "utility-science-pack")
GROWTH_PRUNE = True       # drop a copy that would not feed the bus beside its original (design_segment)
GROW_FLAT_MIN = 10.0      # a block flat out (machines working, output never backed up) this long gets its copy
GROW_WORKING = 0.8        # share of its assemblers seen working
GROW_FULL = 0.1           # at most this share seen with output backed up
GROW_REPEAT_MIN = 30.0    # the same growth is asked again at most this often (an upgrade that did not happen)
ANCHOR_SHIFTS = (0, -24, 24, -48, 48)    # where along a yard side the spine may start
ANCHOR_GAPS = (1, 24, 48)                # how far out from the yard
KEEP = 2                  # quick layouts finished with the oil corridor (the best by score)
RETRY_MIN = 20.0          # nothing fitted (too little land seen yet): try again after this many game minutes
OIL_LAZY = True           # the oil corridor is reserved only once crude is known (Zones.reserve_oil), not up front
# era segments: the targets each is sized for (items a second; each later segment keeps the earlier ones). Segment 1
# is what the brain's road asks of red and green (roadmap.SCIENCE_RATE: 15/min each), not the whole era's 30
from .roadmap import SCIENCE_RATE as _ROAD  # noqa: E402

SEGMENTS = {1: {"automation-science-pack": _ROAD["automation-science-pack"] / 60,
                "logistic-science-pack": _ROAD["logistic-science-pack"] / 60}}
# Segment 2, the blue era, is cumulative: the whole floorplan planned again for red, green and blue together. Blocks
# that keep their design keep their slot (and stay built); blocks that change (gears and circuits grow for engines
# and advanced circuits, the labs take blue too) are reworked in their slots; the new ones (advanced circuits,
# engines, blue science) take the free make slots; the bus is patched (atlas relay: pieces no longer planned come
# up, missing ones go down). Oil products and steel come into the spine head by trunk, like the plates.
SEGMENTS[2] = {**SEGMENTS[1], "chemical-science-pack": _ROAD["chemical-science-pack"] / 60}
# items a segment takes in at its head that the brain must already make before it opens (else its new blocks wait)
SEGMENT_INPUTS = {2: ("plastic-bar", "sulfur", "steel-plate")}
PLAN_UPTO = 2             # the floorplan reserves land for the eras up to this one (falls back to 1 when it fits not)
SEGMENT_ZONES = ("make", "science")       # smelting stays with the brain's fields; the spine head gets their lines
# a segment opens only once these are unlocked (and see ready()). The strip designer needs assemblers and
# long-handed inserters (automation): the zones test, seed 737373, researched logistics first, and segment 1
# failed "no layout candidate" until automation was in
# Segment 1 also waits for green science: the opening (red science included) stays the brain's own, organic, in the
# start yard; zoning starts with green (run 20261127-145615: its first red factory went 150 tiles out along the
# spine before any bus, the copper belt to it failed twice, and red was automated only at game minute 66)
OPEN_RECIPES = {1: {"underground-belt", "assembling-machine-1", "long-handed-inserter", "logistic-science-pack"},
                2: {"chemical-science-pack", "advanced-circuit", "engine-unit"}}


# ==================================================================== the brain's world as an Atlas map scan
def scan_from_world(world, centre=None, name: str = "brain"):
    """An Atlas MapScan of what the brain has seen: terrain, ore tiles and the game's patch records. A snapshot
    (copies), so a background thread may plan on it."""
    from atlas.world.scan import MapScan, Patch
    ore: dict[Tile, str] = {}
    for res, tiles in (getattr(world, "resources", None) or {}).items():
        for t in tiles:
            ore[(int(t[0]), int(t[1]))] = res
    patches = []
    for p in getattr(world, "patches", None) or []:
        try:
            patches.append(Patch.from_json({"name": p["name"], "tiles": p.get("tiles", 0), "amount": p.get("amount", 0),
                                            "box": p["box"], "centre": p.get("centre") or p.get("nearest")}))
        except (KeyError, TypeError, ValueError):
            continue
    trees = set(getattr(world, "trees", {}) or {})
    sc = MapScan(name, set(world.seen), set(world.water), set(world.cliffs), trees, ore, patches,
                 tuple(centre) if centre else tuple(getattr(world, "pos", (0.0, 0.0))))
    sc.complete_patches()
    return sc


def _dilate(tiles, r: int) -> set[Tile]:
    if r <= 0:
        return set(tiles)
    out: set[Tile] = set()
    offs = [(dx, dy) for dx in range(-r, r + 1) for dy in range(-r, r + 1)]
    for x, y in tiles:
        for dx, dy in offs:
            out.add((x + dx, y + dy))
    return out


def rect_tiles(box) -> set[Tile]:
    x0, y0, x1, y1 = box
    return {(x, y) for x in range(x0, x1) for y in range(y0, y1)}


def site_tiles(world, base) -> set[Tile]:
    """Every tile a building of ours stands on (what the world saw and what the sites recorded), plus the tiles
    kept free around them (lanes, keepout)."""
    out = {tuple(t) for t in getattr(world, "used", ()) or ()}
    fp = getattr(world, "footprint", None)
    for s in getattr(base, "sites", []) if base is not None else []:
        for e in getattr(s, "entities", None) or []:
            try:
                name, x, y, d = e[:4]
                out |= set(fp(name, x, y, d)) if fp is not None else {(math.floor(x), math.floor(y))}
            except Exception:  # noqa: BLE001 - a record we cannot read: its tiles are in world.used anyway
                continue
    out |= {tuple(t) for t in getattr(world, "lanes", ()) or ()}
    out |= {tuple(t) for t in getattr(world, "keepout", ()) or ()}
    return out


def start_yard(world, base, centre) -> tuple[int, int, int, int]:
    """The start yard: a box (x0, y0, x1, y1; x1, y1 exclusive) around the base centre that holds every
    building of the opening (those within YARD_REACH of the centre) plus YARD_MARGIN, at least YARD_MIN_HALF and
    at most YARD_MAX_HALF tiles from the centre each way."""
    cx, cy = int(round(centre[0])), int(round(centre[1]))
    near = [t for t in site_tiles(world, base) if abs(t[0] - cx) <= YARD_REACH and abs(t[1] - cy) <= YARD_REACH]
    x0, y0, x1, y1 = cx - YARD_MIN_HALF, cy - YARD_MIN_HALF, cx + YARD_MIN_HALF, cy + YARD_MIN_HALF
    if near:
        x0 = min(x0, min(t[0] for t in near) - YARD_MARGIN)
        y0 = min(y0, min(t[1] for t in near) - YARD_MARGIN)
        x1 = max(x1, max(t[0] for t in near) + YARD_MARGIN + 1)
        y1 = max(y1, max(t[1] for t in near) + YARD_MARGIN + 1)
    return (max(x0, cx - YARD_MAX_HALF), max(y0, cy - YARD_MAX_HALF),
            min(x1, cx + YARD_MAX_HALF), min(y1, cy + YARD_MAX_HALF))


def planning_scan(scan, blocked: set[Tile]):
    """The scan with `blocked` tiles taken out of `seen`: the floorplan treats them like unsurveyed land, so no
    corridor or slot is ever laid on them."""
    from atlas.world.scan import MapScan
    return MapScan(scan.name, scan.seen - blocked, scan.water, scan.cliffs, scan.trees, scan.ore, scan.patches,
                   scan.spawn)


# ==================================================================== segment blocks (the era's production)
_ERA: dict = {}


def kit_for(world):
    """The Atlas kit the brain's research allows."""
    from atlas.plan.graph import Kit
    kn = getattr(world, "kn", None)
    done = set(getattr(kn, "researched", ()) or ())
    try:
        return Kit.for_research(done)
    except Exception:  # noqa: BLE001 - names Atlas does not know: the plain red-science kit
        return Kit("assembling-machine-1", "stone-furnace", "inserter", "long-handed-inserter")


def era_blocks(n: int, kit):
    """(flows, specs) for segment n's targets with this kit: every block with its verified copies."""
    key = (n, kit)
    if key not in _ERA:
        from atlas.plan.blocks import decompose, verified
        from atlas.plan.graph import expand
        flows = expand(SEGMENTS[n], kit)
        _ERA[key] = (flows, verified(decompose(flows, kit), kit))
    return _ERA[key]


def segment_specs(n: int, kit, z=None) -> list:
    """The blocks of plan n that go into spine slots (make and labs), one entry per copy, feeders first: its era's
    blocks, then the extra blocks it took on (Zones.add_extra). Without `z` plan n is era n."""
    era = z.era_of(n) if z is not None else n
    _, specs = era_blocks(era, kit)
    out = [s.at(max(1, s.copies)) for s in specs if s.zone in SEGMENT_ZONES and s.designable
           for _ in range(max(1, s.copies))]
    out.sort(key=lambda s: (s.depth, s.id))
    for item, per_min in sorted((z.extras_of(n) if z is not None else {}).items()):
        ex = extra_spec(item, per_min, kit)
        if ex is not None:
            out += [ex.at(max(1, ex.copies)) for _ in range(max(1, ex.copies))]
    return out


_EXTRA: dict = {}


def extra_spec(item: str, per_min: float, kit):
    """A block for an item no era plans (a belt, an inserter, a pipe...): its recipe, inlining every ingredient
    that is not a bus item, so it takes only bus items in (base the factory on the bus). None when the strip
    designer lays it out in no slot."""
    key = (item, round(float(per_min), 2), kit)
    if key not in _EXTRA:
        try:
            from atlas.plan.blocks import BlockSpec, _stages, first_guess_copies, verified
            from atlas.plan.graph import expand, game
            gd = game()
            flows = expand({item: float(per_min) / 60}, kit)
            spec = BlockSpec(f"extra-{item}", "make", "make", item, float(per_min) / 60)
            _stages(spec, item, spec.per_s, flows, kit, gd)
            spec.depth = 9                      # after the era's own blocks (they keep their slots first)
            spec.copies = first_guess_copies(spec, gd)
            verified([spec], kit)
            _EXTRA[key] = spec if spec.copies else None
        except Exception:  # noqa: BLE001 - an item Atlas cannot plan: it stays free-form
            _EXTRA[key] = None
    return _EXTRA[key]


def growth_specs(specs, k: int) -> list:
    """Up to k planned copies for the spare slots: the busiest intermediate make blocks (never a science block:
    GROWTH_SKIP)."""
    make = [s for s in specs if s.zone == "make" and s.kind == "make" and s.product not in GROWTH_SKIP]
    out, seen = [], set()
    for s in sorted(make, key=lambda s: -float(getattr(s, "per_s", 0) or 0)):
        if s.id not in seen:
            out.append(s)
            seen.add(s.id)
    return out[:k]


def slot_demand(n: int, kit) -> dict[str, int]:
    """Spine slots segment n needs, by zone (the silo spot is kept from segment 1 on)."""
    specs = segment_specs(n, kit)
    return {"make": sum(1 for s in specs if s.zone == "make"), "reserve": SPARE_SLOTS + GENERAL_SLOTS,
            "science": sum(1 for s in specs if s.zone == "science"), "silo": 1}


# ==================================================================== the floorplan, out of the start yard
def _base_ledger(scan_p):
    """A ledger with every basic ore tile (and FIELD_MARGIN round it) claimed: the spine and slots keep off all
    ore, so the brain can still mine wherever it likes."""
    from atlas.layout.floorplan import FIELD_MARGIN
    from atlas.layout.ledger import Claim, Ledger
    from atlas.world.scan import CRUDE
    led = Ledger(scan_p)
    by: dict[str, set[Tile]] = {}
    for t, name in scan_p.ore.items():
        if name != CRUDE:
            by.setdefault(name, set()).add(t)
    for name, tiles in sorted(by.items()):
        keep = {t for t in _dilate(tiles, FIELD_MARGIN) if led.free(t)}
        if keep:
            led.claim(keep, Claim("field", f"ore-{name}", name))
    return led


def _copy_ledger(led):
    from atlas.layout.ledger import Ledger
    out = Ledger(led.scan)
    out._owner = dict(led._owner)
    out._tiles = {k: set(v) for k, v in led._tiles.items()}
    out._claims = dict(led._claims)
    return out


def _anchors(yard) -> list[tuple[Tile, tuple[int, int]]]:
    """Spine starts just outside each side of the yard, heading away from it."""
    from atlas.layout.floorplan import SPINE_W
    x0, y0, x1, y1 = yard
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    W = SPINE_W
    out = []
    for gap in ANCHOR_GAPS:
        for s in ANCHOR_SHIFTS:
            out.append(((x1 + gap, cy - W // 2 + s), (1, 0)))
            out.append(((x0 - gap - W, cy - W // 2 + s), (-1, 0)))
            out.append(((cx - W // 2 + s, y1 + gap), (0, 1)))
            out.append(((cx - W // 2 + s, y0 - gap - W), (0, -1)))
    return out


def _layout(scan_p, base_led, anchor, heading, want: dict[str, int], oil: bool):
    """One floorplan: the spine from `anchor`, the oil corridor (optional), then the slots past the head."""
    from atlas.layout.bus import runs_of
    from atlas.layout.floorplan import MAX_SPINE, SLOTS_PER_CELL, Floorplan, grow_spine, place_oil, place_slots
    fp = Floorplan(scan_p, _copy_ledger(base_led), anchor, heading, demand=dict(want))
    fp.demand.setdefault("oil", 0)
    cells = min(MAX_SPINE, math.ceil(sum(want.values()) / SLOTS_PER_CELL) + 3)
    sp = grow_spine(fp, anchor, heading, None, 1, cells)
    if sp is None:
        return None
    if oil:
        place_oil(fp)
    lo = runs_of(sp)[0].span()[0]
    place_slots(fp, sp, want, after=(0, lo + HEAD))
    return fp


def _score(fp, want: dict[str, int], centre) -> float:
    got = sum(min(len(fp.zone(z)), n) for z, n in want.items())
    sp = fp.corridors.get("spine")
    head = sp.cells[0] if sp and sp.cells else fp.anchor
    s = 10.0 * got + (8.0 if "oil" in fp.corridors else 0.0)
    s -= 0.02 * math.dist((head[0] + 20, head[1] + 20), centre)
    s -= 0.01 * sum(c.length for c in fp.corridors.values() if c.kind == "oil")
    return s


def plan_floor(scan, yard, blocked: set[Tile], want: dict[str, int], centre, log=None):
    """The best floorplan whose spine starts at the start yard's edge: quick layouts (spine and slots) from every
    anchor; unless OIL_LAZY, the best KEEP are then finished with the oil corridor. None when no spine fits."""
    scan_p = planning_scan(scan, blocked | rect_tiles(yard))
    base_led = _base_ledger(scan_p)
    quick = []
    for anchor, heading in _anchors(yard):
        fp = _layout(scan_p, base_led, anchor, heading, want, oil=False)
        if fp is not None:
            quick.append((_score(fp, want, centre), anchor, heading, fp))
    quick.sort(key=lambda q: -q[0])
    best = None
    if OIL_LAZY:                  # the oil corridor comes later, once crude is known (Zones.reserve_oil)
        best = quick[0][3] if quick else None
        if log and best is not None:
            log(f"[zones] spine from {best.anchor} heading {best.heading}: {best.metrics['slots']}")
        return best
    for _, anchor, heading, _fp in quick[:KEEP]:
        fp = _layout(scan_p, base_led, anchor, heading, want, oil=True)
        if fp is not None and (best is None or _score(fp, want, centre) > _score(best, want, centre)):
            best = fp
    if log and best is not None:
        log(f"[zones] spine from {best.anchor} heading {best.heading}: {best.metrics['slots']}")
    return best


# ==================================================================== what a segment returns
@dataclass
class SegBlock:
    key: str                         # "<block id>#<copy>": stable across re-plans
    spec: object                     # atlas BlockSpec (this copy's rate)
    slot: object                     # atlas Slot
    wd: object | None = None         # atlas WorldDesign, None when nothing fits
    ports: list[dict] = field(default_factory=list)   # {"io", "item", "tile" (spine edge), "x", "rate"}
    taps: list[dict] = field(default_factory=list)    # per input port: {"item", "line", "lat", "tile", "splitter"}
    problems: list[str] = field(default_factory=list)
    n: int = 1                       # the segment it belongs to
    growth: bool = False             # a planned copy for when the block falls short (built only then)

    @property
    def product(self) -> str:
        return self.spec.product

    @property
    def rect(self) -> tuple[int, int, int, int]:
        return self.slot.rect

    @property
    def ok(self) -> bool:
        return self.wd is not None and not self.problems


@dataclass
class Segment:
    n: int
    targets: dict
    kit: object
    blocks: list[SegBlock] = field(default_factory=list)
    head: list = field(default_factory=list)          # atlas Terminals: plates in at the spine head (by trunk)
    bus: object | None = None
    problems: list[str] = field(default_factory=list)

    def items(self) -> set[str]:
        return {b.product for b in self.blocks if b.spec.kind == "make"}

    def consumed(self) -> set[str]:
        """Items a make block takes off the bus (the blocks the brain builds; labs are not one of them). A block
        whose product is one of these puts it on the bus; any other product goes into its output chest."""
        return {p["item"] for b in self.blocks if b.ok and b.spec.kind == "make" for p in b.ports if p["io"] == "in"}

    def feeds_line(self, block) -> bool:
        """Does a planned bus line start at this block's output (a source terminal of a kept line)? Atlas keeps
        one line per spine half and drops one with nobody to feed, so a block's output may have none."""
        if self.bus is None:
            return False
        owner = f"{block.key}@{block.slot.id}"
        return any(t.io == "src" and t.owner == owner for ln in self.bus.lines.values() for t in ln.terms)

    def inputs_of(self, blocks) -> set[str]:
        """The bus lines these blocks draw on (their input ports' items)."""
        return {p["item"] for b in blocks if b.ok for p in b.ports if p["io"] == "in"}

    def jobs(self) -> dict[str, list]:
        """The planned bus as placement steps, one list per belt network ("bus:<item>": the item's lines, their
        transfers, taps and injections; "bus:poles": the poles along the spine and across it)."""
        if self.bus is None:
            return {}
        cache = self.__dict__.get("_jobs")
        if cache is None:
            from atlas.build.compile import bus_jobs
            cache = self.__dict__["_jobs"] = {j.name: j.steps for j in bus_jobs(self.bus)}
        return cache

    def demand(self, blocks) -> dict[str, float]:
        """What these blocks take a second of each item no block of the segment makes (the head's plates)."""
        made = {b.product for b in self.blocks if b.ok}
        out: dict[str, float] = {}
        for b in blocks:
            for item, rate in b.spec.bus_in.items():
                if item not in made:
                    out[item] = out.get(item, 0.0) + rate
        return out

    def head_ports(self, spine) -> list[dict]:
        """The head terminals as brain feed ports: a trunk's last belt stands just outside the spine's edge and
        faces into it (the bus's injection belt starts on the edge tile)."""
        from atlas.layout.bus import runs_of
        runs = runs_of(spine)
        W = spine.width
        out = []
        for t in self.head:
            r = runs[t.run]
            lat_out = -1 if t.side < 0 else W
            end, appr = r.tile(t.pos, lat_out), r.tile(t.pos, lat_out - 1 if t.side < 0 else lat_out + 1)
            face = r.side_dir(t.side)
            from atlas.design.model import opposite
            out.append({"item": t.item, "end": list(end), "approach": list(appr), "chest": None, "inserter": None,
                        "face": opposite(face), "feed": True, "per_minute": round(t.rate * 60, 1), "fed": None,
                        "bus_head": True})
        return out


# ==================================================================== the zones themselves
class Zones:
    """The ledger side of the hybrid: the start yard, the reserved land, the era segments. Pickled with the crew
    (persist.py): only plain state and the Atlas corridor and slot records; the floorplan's ledger and every
    design are rebuilt from the world when needed."""

    def __init__(self):
        self.yard: tuple[int, int, int, int] | None = None
        self.centre: tuple[float, float] | None = None
        self.corridors: dict = {}            # atlas Corridor by id (the spine, the oil corridor and its spurs)
        self.slots: list = []                # atlas Slot (every zone: make, reserve, science, silo, oil)
        self.block: set[Tile] = set()        # no buildings here
        self.belt_ok: set[Tile] = set()      # ... but belts may cross these (the corridors)
        self.slot_only: set[Tile] = set()    # block - belt_ok: the slots, where not even a belt goes
        self.anchor: Tile | None = None
        self.heading: tuple[int, int] | None = None
        self.opened: dict[int, dict] = {}    # segment -> {"built": {block key: site centre}, "minute": ...}
        self.warnings: list[str] = []
        self.tried_minute: float | None = None
        self.bus_tiles: set[Tile] = set()    # every tile of an open segment's planned bus: no organic belt either
        self.tried_open: float | None = None  # the minute a segment was last worked out to open (once a minute)
        self.oil_tried: float | None = None  # when the lazy oil corridor was last tried (OIL_LAZY)
        self._fp = None                      # transient: the rebuilt Floorplan
        self._seg: dict = {}                 # transient: computed Segments

    # ------------------------------------------------------------ persistence
    def __getstate__(self):
        st = dict(self.__dict__)
        st["_fp"] = None
        st["_seg"] = {}
        st.pop("_world", None)
        return st

    def __setstate__(self, st):
        self.__dict__.update(st)
        self.__dict__.setdefault("_fp", None)
        self.__dict__.setdefault("_seg", {})
        for k, v in (("bus_tiles", set()), ("oil_tried", None), ("tried_open", None)):
            self.__dict__.setdefault(k, v)

    # ------------------------------------------------------------ queries (the hot path of world.free)
    @property
    def reserved(self) -> bool:
        return bool(self.block)

    def blocks(self, tiles) -> bool:
        """Would a building on these tiles stand on reserved land?"""
        if not ENABLED or not self.block:
            return False
        return not self.block.isdisjoint(tiles)

    def refused(self) -> set[Tile]:
        """Every tile no building may use (empty while zones are off). Planners that work on their own snapshot
        of the ground (fab's Grid, the workshop floor) add it to their keepout."""
        return self.block if ENABLED else set()

    def refused_slots(self) -> set[Tile]:
        """Reserved slot tiles: not even a belt or a pole may use them (corridors they may cross)."""
        return self.__dict__.get("slot_only", set()) if ENABLED else set()

    def blocks_belt(self, t) -> bool:
        """Would a belt on this tile stand on a reserved slot (corridors are fine)?"""
        if not ENABLED or not self.block:
            return False
        return (t[0], t[1]) in self.__dict__.get("slot_only", ())

    def in_yard(self, t) -> bool:
        if self.yard is None:
            return False
        x0, y0, x1, y1 = self.yard
        return x0 <= t[0] < x1 and y0 <= t[1] < y1

    def zone_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self.slots:
            out[s.zone] = out.get(s.zone, 0) + 1
        return out

    def slot_of(self, sid: str):
        return next((s for s in self.slots if s.id == sid), None)

    # ------------------------------------------------------------ reservations
    def snapshot(self, world, base) -> dict:
        """What planning needs, copied from the live world (on the crew's thread: the foreman keeps observing
        while a worker plans, so the worker must never read the world's own sets)."""
        centre = (base.centre if base is not None else None) or getattr(world, "pos", (0.0, 0.0))
        return {"centre": tuple(centre), "yard": start_yard(world, base, centre),
                "scan": scan_from_world(world, centre), "blocked": _dilate(site_tiles(world, base), SITE_MARGIN),
                "want": slot_demand(PLAN_UPTO, kit_for(world)), "want_min": slot_demand(1, kit_for(world))}

    def plan(self, world=None, base=None, log=None, snap: dict | None = None):
        """The start yard and the floorplan from a snapshot (no game actions): (centre, yard, floorplan or None).
        With `snap` it touches nothing live, so it may run on a worker thread; apply() puts it into effect."""
        snap = snap or self.snapshot(world, base)
        fp = plan_floor(snap["scan"], snap["yard"], snap["blocked"], snap["want"], snap["centre"], log=log)
        if fp is None and snap["want"].get("reserve", 0) > SPARE_SLOTS:
            # no room for the general slots too: the eras' slots and their spares only
            fp = plan_floor(snap["scan"], snap["yard"], snap["blocked"], {**snap["want"], "reserve": SPARE_SLOTS},
                            snap["centre"], log=log)
        if fp is None and snap.get("want_min") and snap["want_min"] != snap["want"]:
            # no room for the later eras' slots: segment 1's alone (that world stays in segment 1)
            fp = plan_floor(snap["scan"], snap["yard"], snap["blocked"], snap["want_min"], snap["centre"], log=log)
        return snap["centre"], snap["yard"], fp

    def apply(self, centre, yard, fp, log=None) -> int:
        """Keep the floorplan's corridors and slots and reserve their tiles. Returns how many tiles."""
        self.centre, self.yard = tuple(centre), tuple(yard)
        if fp is None or "spine" not in fp.corridors:
            self.warnings.append("no spine fits out of the start yard: nothing reserved")
            return 0
        if not [q for q in fp.slots if q.zone != "power"]:
            # a spine stub with no slot is land taken for nothing, and as "reserved" it was never planned again
            # (the zones test, seed 626262: 1600 tiles, a 1-cell spine, 0 slots, at the first electric build with
            # little land seen): nothing is reserved, so the next try (RETRY_MIN) plans on more land
            self.warnings.append("no slot fits yet (too little land seen): nothing reserved, tried again later")
            if log:
                log(f"[zones] no slot fits yet ({len(fp.corridors['spine'].cells)}-cell spine): nothing reserved, "
                    f"tried again in {RETRY_MIN:.0f} min")
            return 0
        keep = {cid: c for cid, c in fp.corridors.items() if c.kind in ("spine", "oil")}
        self.corridors = keep
        self.slots = [s for s in fp.slots if s.zone != "power"]
        self.anchor, self.heading = fp.anchor, fp.heading
        ytiles = rect_tiles(self.yard)
        corr = set().union(*(c.tiles for c in keep.values())) - ytiles
        slots = set().union(*(s.tiles for s in self.slots)) - ytiles if self.slots else set()
        self.block = corr | slots
        self.belt_ok = corr - slots
        self._refresh()
        self.warnings += list(fp.warnings)
        self._fp, self._seg = None, {}
        if log:
            log(f"[zones] reserved {len(self.block)} tiles: spine {len(keep['spine'].cells)} cells, "
                f"{len(self.slots)} slots ({', '.join(sorted({s.zone for s in self.slots}))}), "
                f"oil corridor {'yes' if 'oil' in keep else 'no'}; start yard {self.yard}")
        return len(self.block)

    def reserve(self, world, base, log=None) -> int:
        """plan() and apply() in one go (tests, and callers without a worker thread)."""
        if not ENABLED:
            return 0
        centre, yard, fp = self.plan(world, base, log=log)
        return self.apply(centre, yard, fp, log=log)

    def release(self, tiles) -> None:
        """Give tiles back (a slot the segment does not need after all)."""
        tiles = set(tiles)
        self.block -= tiles
        self.belt_ok -= tiles
        self._refresh()

    def _refresh(self) -> None:
        """slot_only: where not even a belt or pole may go (the slots, and an open segment's planned bus)."""
        self.slot_only = (self.block - self.belt_ok) | (self.__dict__.get("bus_tiles") or set())

    # ------------------------------------------------------------ the oil corridor, once crude is known
    def oil_snapshot(self, world, base) -> dict | None:
        """What reserve_oil plans on (copied on the crew's thread), or None when no crude is known yet."""
        crude = bool((getattr(world, "resources", None) or {}).get("crude-oil")) or any(p.get("name") == "crude-oil" for p in getattr(world, "patches", None) or [])
        if not crude or "spine" not in self.corridors:
            return None
        snap = self.snapshot(world, base)
        snap["yard"] = self.yard or snap["yard"]
        return snap

    def plan_oil(self, snap: dict):
        """The oil corridor from the nearest crude field to the spine (atlas floorplan.place_oil) on today's
        ground, around everything reserved and built: (corridors, slots) to add, both empty when none fits."""
        from atlas.layout.floorplan import Floorplan, place_oil
        scan_p = planning_scan(snap["scan"], snap["blocked"] | rect_tiles(snap["yard"]) | self._head_keep())
        led = _base_ledger(scan_p)
        for cid, c in self.corridors.items():
            claim = _claim("corridor", cid, c.kind)
            led._claims[cid], led._tiles[cid] = claim, set(c.tiles)
            for t in c.tiles:
                led._owner[t] = claim
        for s in self.slots:
            claim = _claim("slot", s.id, s.zone)
            led._claims[s.id], led._tiles[s.id] = claim, set(s.tiles)
            for t in s.tiles:
                led._owner[t] = claim
        fp = Floorplan(scan_p, led, self.anchor or (0, 0), self.heading or (1, 0), dict(self.corridors),
                       list(self.slots), demand={"oil": 0})
        place_oil(fp)
        corr = {cid: c for cid, c in fp.corridors.items() if c.kind == "oil" and cid not in self.corridors}
        slots = [s for s in fp.slots if s.zone == "oil" and s not in self.slots]
        return corr, slots

    def apply_oil(self, corr: dict, slots: list, log=None) -> int:
        if not corr:
            self.warnings.append("no oil corridor fits yet")
            return 0
        self.corridors.update(corr)
        self.slots += slots
        ctiles = set().union(*(c.tiles for c in corr.values()))
        stiles = set().union(*(s.tiles for s in slots)) if slots else set()
        self.block |= ctiles | stiles
        self.belt_ok |= ctiles - stiles
        self._refresh()
        self._fp = None
        if log:
            log(f"[zones] oil corridor reserved: {len(ctiles)} tiles, {len(slots)} oil slot(s)")
        return len(ctiles | stiles)

    def reserve_oil(self, world, base, log=None) -> int:
        snap = self.oil_snapshot(world, base)
        if snap is None:
            return 0
        return self.apply_oil(*self.plan_oil(snap), log=log)

    def _head_keep(self) -> set[Tile]:
        """The head terminals' trunk ends and the land just outside them: the oil corridor keeps off."""
        out: set[Tile] = set()
        for seg in self._seg.values():          # (keys: n, kit, growth)
            if "spine" in self.corridors:
                for hp in seg.head_ports(self.corridors["spine"]):
                    out |= _dilate({tuple(hp["end"]), tuple(hp["approach"])}, 2)
        if not out and "spine" in self.corridors:       # no segment worked out yet: the head's first stretch
            from atlas.layout.bus import runs_of
            sp = self.corridors["spine"]
            r = runs_of(sp)[0]
            lo = r.span()[0]
            for pos in range(lo, lo + HEAD):
                for lat in (-3, -2, -1, sp.width, sp.width + 1, sp.width + 2):
                    out.add(r.tile(pos, lat))
        return out

    # ------------------------------------------------------------ the floorplan, rebuilt from the world
    def floorplan(self, world=None, scan=None):
        """An Atlas Floorplan holding the kept corridors and slots in a ledger (for the bus and the designs)."""
        if self._fp is not None:
            return self._fp
        from atlas.layout.floorplan import Floorplan
        from atlas.layout.ledger import Ledger
        if scan is None:
            scan = scan_from_world(world if world is not None else self.__dict__["_world"], self.centre)
        led = Ledger(scan)
        for cid, c in self.corridors.items():
            claim = _claim("corridor", cid, c.kind)
            led._claims[cid] = claim
            led._tiles[cid] = set(c.tiles)
            for t in c.tiles:
                led._owner[t] = claim
        for s in self.slots:
            claim = _claim("slot", s.id, s.zone)
            led._claims[s.id] = claim
            led._tiles[s.id] = set(s.tiles)
            for t in s.tiles:
                led._owner[t] = claim
        fp = Floorplan(scan, led, self.anchor or (0, 0), self.heading or (1, 0), dict(self.corridors), list(self.slots))
        self._fp = fp
        return fp

    # ------------------------------------------------------------ segments
    def is_open(self, n: int) -> bool:
        return ENABLED and n in self.opened

    def era_of(self, n: int) -> int:
        """The era plan n belongs to (a plan that only took on extra blocks keeps its era)."""
        return int(((self.__dict__.get("plans") or {}).get(n) or {}).get("era", n))

    def extras_of(self, n: int) -> dict:
        return dict(((self.__dict__.get("plans") or {}).get(n) or {}).get("extras") or {})

    @property
    def current(self) -> int | None:
        """The live segment: the latest opened (an earlier one is superseded, its blocks carried over or reworked)."""
        return max(self.opened) if self.opened else None

    def live(self) -> list[int]:
        return [self.current] if self.opened else []

    def ready(self, n: int, crew) -> tuple[bool, str]:
        """May segment n open? Segment 1: reservations made, underground belts unlocked, electric mining and a
        steam station built."""
        if not ENABLED:
            return False, "zones are off"
        if not self.reserved:
            return False, "nothing reserved yet"
        era = self.era_of(n)
        if era not in SEGMENTS:
            return False, f"segment {n} is not planned yet (hybrid-plan step 6)"
        if n > 1 and not self.is_open(n - 1):
            return False, f"segment {n - 1} first"
        if n > 1 and self.current != n - 1:
            return False, f"segment {self.current} is live"
        kn = getattr(crew, "kn", None)
        unlocked = set(getattr(kn, "unlocked", ()) or ())
        if not OPEN_RECIPES.get(era, set()) <= unlocked:
            return False, f"needs {sorted(OPEN_RECIPES[era] - unlocked)} unlocked"
        base = getattr(crew, "base", None)
        kinds = {s.kind for s in getattr(base, "sites", [])}
        if not kinds & {"electric_mine", "outpost"}:
            return False, "no electric mining yet"
        if "steam_power" not in kinds:
            return False, "no steam station yet"
        if n > 1:
            prev = self.segment(n - 1)
            left = [b.key for b in prev.blocks if b.ok and not b.growth and b.spec.kind == "make" and not self.is_built(b)]
            if left:
                return False, f"segment {n - 1}'s blocks first ({', '.join(left[:3])})"
            makes = getattr(crew, "makes", None)
            short = [i for i in SEGMENT_INPUTS.get(era, ())
                     if not ((makes(i) if makes is not None else False) or _spare(crew, i) > 0)]
            if short:
                return False, f"its head takes {', '.join(short)}: nothing makes them yet"
            try:
                fp = self.floorplan()
                need = slot_demand(era, kit_for(self.__dict__.get("_world")))
                have = {z: sum(1 for s in fp.slots if s.zone == z and s.corridor == "spine") for z in ("make", "reserve")}
                if have["make"] + have["reserve"] < need["make"]:
                    return False, f"the floorplan has {have['make'] + have['reserve']} make slots, it needs {need['make']}"
            except Exception:  # noqa: BLE001 - no floorplan at hand: design_segment says why
                pass
        return True, ""

    def segment(self, n: int, world=None, kit=None, scan=None) -> Segment:
        """Segment n laid out: every block copy in a slot (make blocks in make slots, labs in science slots, the
        reserve slots when a design does not fit its own), designed with the strip designer, ports on the spine;
        then the bus for these terminals only (head terminals for the plates the brain's lines bring in). Pure
        computation on a snapshot; cached until the next reserve()."""
        if kit is None:
            kit = (self.opened.get(n) or {}).get("kit") or kit_for(world or self.__dict__.get("_world"))
        growth = self.growth_on(n)
        key = (n, kit, growth)
        if key in self._seg:
            return self._seg[key]
        seg = design_segment(self, n, kit, self.floorplan(world, scan), growth=growth)
        self._seg[key] = seg
        return seg

    def growth_on(self, n: int) -> bool:
        """Does segment n plan copies in its spare slots? An opened segment keeps what it opened with (a segment
        opened by older code has a laid bus without them: never re-planned); one not open yet follows GROWTH."""
        if n in self.opened:
            return bool(self.opened[n].get("growth", False))
        return GROWTH and n not in self.__dict__.get("no_growth", set())

    def open_segment(self, n: int, world=None, kit=None, minute: float = 0.0, log=None) -> Segment:
        seg = self.segment(n, world, kit)
        fresh = n not in self.opened
        self.opened.setdefault(n, {"built": {}, "minute": minute, "kit": seg.kit,
                                   "growth": any(b.growth for b in seg.blocks)})
        if fresh and n > 1 and (n - 1) in self.opened:
            self._carry_over(n - 1, n, seg, log)
        if seg.bus is not None:                  # the planned bus's land: no organic belt or pole crosses it now
            self.bus_tiles = set(self.__dict__.get("bus_tiles") or set()) | \
                {t for e in seg.bus.entities for t in e.tiles}
            self._refresh()
        if log:
            log(f"[zones] segment {n} open: {len(seg.blocks)} blocks "
                f"({', '.join(b.key for b in seg.blocks)}), {len(seg.problems)} problem(s)"
                + (f": {seg.problems[0]}" if seg.problems else ""))
        return seg

    def _carry_over(self, old: int, new: int, seg: Segment, log=None) -> None:
        """Segment `new` replaces `old`: a block whose design is the same in the same slot stays built; a built
        block whose design changed (or that the new plan drops) is listed for rework (rework_old: its site comes
        up, then the new block goes into the slot)."""
        prev = self.segment(old)
        st_old, st_new = self.opened[old], self.opened[new]
        built_old = st_old.get("built", {})
        by_key = {b.key: b for b in prev.blocks if b.ok}
        rework, kept = [], []
        for b in seg.blocks:
            p = by_key.get(b.key)
            if p is None or p.key not in built_old:
                continue
            if b.ok and _same_block(p, b):
                st_new.setdefault("built", {})[b.key] = built_old[p.key]
                kept.append(b.key)
            else:
                rework.append({"key": p.key, "slot": p.slot.id, "product": p.product, "labs": p.spec.kind == "labs"})
        planned = {b.key for b in seg.blocks if b.ok}
        for k in built_old:
            if k not in planned and k in by_key:
                rework.append({"key": k, "slot": by_key[k].slot.id, "product": by_key[k].product,
                               "labs": by_key[k].spec.kind == "labs", "dropped": True})
        st_new["from"] = old
        st_new["rework"] = rework
        st_old["superseded"] = True
        if log:
            log(f"[zones] segment {new} takes over from segment {old}: kept {', '.join(kept) or 'nothing'}; "
                f"reworked {', '.join(r['key'] for r in rework) or 'nothing'}")

    def slot_block(self, item: str, world=None, kit=None) -> SegBlock | None:
        """The live segment's first block of this item not built yet, with a good design (None: none, so the
        factory goes free-form). A block still to be reworked (its old site stands in the slot) waits."""
        if not ENABLED:
            return None
        for n in self.live():
            pending = {r["slot"] for r in (self.opened[n].get("rework") or []) if not r.get("done")}
            seg = self.segment(n, world, kit)
            built = self.opened[n].get("built", {})
            for b in seg.blocks:
                if b.product == item and b.key not in built and b.ok and b.slot.id not in pending:
                    return b
        return None

    def mark_built(self, block: SegBlock, where=None) -> None:
        st = self.opened.get(block.n)
        if st is not None:
            st.setdefault("built", {})[block.key] = tuple(where) if where else True

    def block_by_key(self, key: str) -> SegBlock | None:
        for n in self.live():
            for b in self.segment(n).blocks:
                if b.key == key:
                    return b
        return None

    def active_blocks(self, n: int, extra=()) -> list[SegBlock]:
        """The segment's blocks that are built, plus `extra` (the one being built now): the bus serves these."""
        built = (self.opened.get(n) or {}).get("built", {})
        keys = set(built) | {b.key for b in extra}
        return [b for b in self.segment(n).blocks if b.key in keys and b.ok]

    def made_by(self, n: int, item: str) -> SegBlock | None:
        """The segment block that makes this item (None: the head brings it in, or nothing does)."""
        return next((b for b in self.segment(n).blocks if b.ok and b.product == item), None)

    def is_built(self, block: SegBlock) -> bool:
        return block.key in (self.opened.get(block.n) or {}).get("built", {})

    def labs_block(self, built: bool | None = False) -> SegBlock | None:
        """An open segment's labs block (the science slot): unbuilt (built=False), built (True) or either (None).
        None when no open segment has one with a good design."""
        if not ENABLED:
            return None
        for n in self.live():
            pending = {r["slot"] for r in (self.opened[n].get("rework") or []) if not r.get("done")}
            for b in self.segment(n).blocks:
                if b.spec.kind == "labs" and b.ok and not b.growth and b.slot.id not in pending and \
                        (built is None or self.is_built(b) == built):
                    return b
        return None

    def planned_items(self) -> set[str]:
        """Every item a floorplan era (or an extra block of the live plan) makes in a slot (their factories wait
        for the slot, organic until then)."""
        out: set[str] = set(self.extras_of(self.current)) if self.opened else set()
        for n in SEGMENTS:
            try:
                out |= {s.product for s in segment_specs(n, kit_for(self.__dict__.get("_world")))}
            except Exception:  # noqa: BLE001 - an era that does not plan here: nothing of it counted
                continue
        return out

    def general_slots(self, item: str | None = None) -> list:
        """Reserve slots on the spine that no block of the live segment (copies included) uses and no factory has
        claimed: where a factory no era plans goes. Nearest the spine's start first. None before segment 1 opens
        (no bus yet: a slot out on the spine is only long belts away from the start yard), and none for an item an
        era plans (it waits for its own slot)."""
        if not ENABLED or not self.reserved or not self.opened:
            return []
        if item is not None and item in self.planned_items():
            return []
        claimed = self.__dict__.get("claimed") or {}
        used = set()
        for n in self.live():
            used |= {b.slot.id for b in self.segment(n).blocks if b.ok}
        return sorted((s for s in self.slots if s.zone == "reserve" and s.corridor == "spine" and s.id not in used
                       and s.id not in claimed), key=lambda s: s.order)

    def claim(self, slot, item: str, site_id=None) -> None:
        """A free-form factory takes a general slot: its land is given back for buildings (the factory stands on
        it) and no segment plans a block there any more."""
        self.__dict__.setdefault("claimed", {})[slot.id] = {"item": item, "site": site_id}
        self.release(slot.tiles)
        self._seg = {k: v for k, v in self._seg.items() if k[0] in self.opened}   # later eras plan around it

    def onto_bus(self, n: int, block: SegBlock | None = None) -> set[str]:
        """Products a slot factory of segment n puts on the bus instead of into an output chest: what the make
        blocks take (Segment.consumed), and once the segment's labs stand, the packs they take. Before the labs
        are built the packs go into chests, where the stations elsewhere get them by belt; the planned bus has
        their lines all along (Atlas designs the labs block's pack terminals with the rest). With `block`: the
        packs only if a planned line starts at that block's output (Segment.feeds_line)."""
        seg = self.segment(n)
        out = set(seg.consumed())
        for b in seg.blocks:
            if b.spec.kind == "labs" and b.ok and self.is_built(b):
                packs = {p["item"] for p in b.ports if p["io"] == "in"}
                if block is not None and not seg.feeds_line(block):
                    packs = set()
                out |= packs
        return out

    def summary(self) -> dict:
        return {"reserved_tiles": len(self.block), "yard": self.yard,
                "spine_cells": len(self.corridors["spine"].cells) if "spine" in self.corridors else 0,
                "slots": {z: sum(1 for s in self.slots if s.zone == z) for z in sorted({s.zone for s in self.slots})},
                "oil_corridor": "oil" in self.corridors, "segments_open": sorted(self.opened),
                "warnings": self.warnings[-3:]}


def report_of(crew) -> dict | None:
    """The floorplan for the deciders' reports: what is reserved, the bus sites and the slot factories (None: no
    floorplan in this run)."""
    z = getattr(crew, "zones", None)
    if z is None or not getattr(z, "reserved", False):
        return None
    try:
        out = z.summary()
        out["bus_sites"] = [s.id for s in crew.base.sites if s.kind == "bus"]
        out["slot_factories"] = {s.id: f"{s.params.get('item')} in {s.params['slot']}" for s in crew.base.sites
                                 if (s.params or {}).get("slot")}
        return out
    except Exception:  # noqa: BLE001 - a report never fails on the floorplan
        return None


def _claim(kind: str, cid: str, role: str):
    from atlas.layout.ledger import Claim
    return Claim(kind, cid, role)


# ==================================================================== designing a segment
def _head_terminals(fp, items: list[str], rates: dict[str, float]) -> list:
    """Source terminals at the spine's head, left edge first, HEAD_GAP apart: where trunks bring plates in."""
    from atlas.layout.bus import TAIL, Terminal, runs_of
    sp = fp.corridors["spine"]
    runs = runs_of(sp)
    r = runs[0]
    lo = r.span()[0]
    out = []
    for k, item in enumerate(items):
        pos = lo + TAIL + 2 + HEAD_GAP * k
        out.append(Terminal(item, "src", r.tile(pos, 0), rates.get(item, 0.0), owner=f"head:{item}"))
    if out and lo + TAIL + 2 + HEAD_GAP * (len(items) - 1) > lo + HEAD - 4:
        out = out[:max(1, (HEAD - TAIL - 6) // HEAD_GAP + 1)]
    return out


def _same_block(a: SegBlock, b: SegBlock) -> bool:
    """The same design in the same slot (a block carried over from one segment to the next stays built)."""
    if a.slot.id != b.slot.id or a.wd is None or b.wd is None:
        return False
    if abs(float(a.spec.per_s) - float(b.spec.per_s)) > 0.01 * max(float(a.spec.per_s), 1e-9):
        return False

    def ents(blk):
        return sorted((e["name"], tuple(sorted(tuple(t) for t in e["tiles"])), e["dir"], e.get("recipe"))
                      for e in blk.wd.entities())
    return ents(a) == ents(b)


def _spare(crew, item: str) -> float:
    try:
        return float(crew.spare_rate(item) or 0.0)
    except Exception:  # noqa: BLE001 - a stub crew
        return 0.0


def design_segment(z: Zones, n: int, kit, fp, growth: bool = False, skip: frozenset = frozenset(),
                   fresh_bus: bool = False) -> Segment:
    from atlas.design.memory import Memory
    from atlas.layout.bus import Terminal, design_bus
    from atlas.sim.base import world_check, _reach
    from atlas.sim.blocks import place
    from atlas.design.model import E
    seg = Segment(n, dict(SEGMENTS.get(n, {})), kit)
    if "spine" not in fp.corridors:
        seg.problems.append("no spine reserved")
        return seg
    specs = segment_specs(n, kit, z)
    mem = Memory()
    by_zone = {zone: sorted((s for s in fp.slots if s.zone == zone and s.corridor == "spine"), key=lambda s: s.order)
               for zone in ("make", "science", "reserve")}
    spare = list(by_zone["reserve"])
    used: set[str] = set((z.__dict__.get("claimed") or {}).keys())     # general slots a free-form factory took
    counts: dict[str, int] = {}
    prev = None
    if n > 1:                              # a later era: every block of the last one keeps its slot
        try:
            prev = z.segment(n - 1)
        except Exception:  # noqa: BLE001 - no earlier segment to build on: laid out on its own
            prev = None
    keep = {b.key: b.slot for b in (prev.blocks if prev is not None else []) if b.ok and not b.growth}
    plan = [(s, False) for s in specs]
    if growth:                             # copies for the spare slots the era's own blocks leave free
        plan += [(s, True) for s in growth_specs([s for s in specs if s.id not in skip],
                                                 min(SPARE_SLOTS, len([sl for sl in spare if sl.id not in used])))]
    for spec, grow in plan:
        copy = counts.get(spec.id, 0)
        counts[spec.id] = copy + 1
        key = f"{spec.id}#{copy}"
        extra = spec.id.startswith("extra-")       # extras use the spare slots: the make slots are the eras'
        own = [s for s in by_zone.get(spec.zone, []) if s.id not in used] if not (grow or extra) else []
        kept_slot = keep.get(key)
        held = {sl.id for k2, sl in keep.items() if k2 != key}        # slots the last era's blocks keep
        if kept_slot is not None and kept_slot.id not in used:
            tries = [kept_slot]
        else:
            own = [sl for sl in own if sl.id not in held] or own
            tries = own[:1] + [sl for sl in spare if sl.id not in used and sl.id not in held]
        best = None
        for slot in tries:
            wd, errs = place(fp, spec, slot, mem, kit)
            if wd is not None and not errs:
                best = SegBlock(key, spec, slot, wd, n=n, growth=grow)
                break
            if best is None:
                best = SegBlock(key, spec, slot, None, problems=errs or [f"{key}: no design in {slot.id}"], n=n,
                                growth=grow)
        if grow and (best is None or not best.ok):
            continue                       # a copy that fits nowhere is simply not planned
        if best is None:
            seg.problems.append(f"no slot for {key}")
            continue
        if best.ok:
            used.add(best.slot.id)
        else:
            seg.problems += best.problems
        seg.blocks.append(best)
    made = {b.product for b in seg.blocks if b.ok}
    need: dict[str, float] = {}
    for b in seg.blocks:
        for item, rate in b.spec.bus_in.items():
            if item not in made:
                need[item] = need.get(item, 0.0) + rate
    order = [i for i in ("iron-plate", "copper-plate", "steel-plate", "stone-brick", "coal", "stone") if i in need]
    order += sorted(i for i in need if i not in order)
    seg.head = _head_terminals(fp, order, need)
    terms = list(seg.head)                 # the same objects: design_bus locates them (run, pos, side)
    # a planned copy's packs are no source of the labs' lines: Atlas keeps one line per spine half and drops one
    # with no labs on its side, so a copy on the labs' side took the line and the original block (the one that is
    # built) had none (the fixture map: green's line ran from its copy in reserve-0, nothing from make-3)
    lab_packs = {i for b in seg.blocks if b.ok and b.spec.kind == "labs" for i in b.spec.bus_in}
    for b in seg.blocks:
        if not b.ok:
            continue
        for p, tile in b.wd.port_tiles():
            for item in p.items:
                b.ports.append({"io": p.io, "item": item, "tile": tile, "x": p.x, "rate": p.rate.get(item, 0.0)})
                if b.growth and p.io == "out" and item in lab_packs:
                    continue
                terms.append(Terminal(item, "src" if p.io == "out" else "sink", tile, p.rate.get(item, 0.0),
                                      owner=f"{b.key}@{b.slot.id}"))
    if not any(t.io == "sink" for t in terms):
        return seg
    patched = prev is not None and prev.bus is not None and not fresh_bus
    if patched:
        # the last era's bus stands: re-planned around it, its untouched lines kept on their lanes
        from atlas.layout.bus import relay
        bus = relay(fp, prev.bus, terms, kit)[0]
    else:
        bus = design_bus(fp, terms, kit)
    if growth and GROWTH_PRUNE:
        # design_bus keeps one line per spine half: a copy may take an item's line from its original, or feed no
        # line at all (6 of 22 maps: the circuit copy sourced the line, the original nothing). A copy is kept only
        # when it and its original both feed the bus; otherwise it is left out and the bus laid out again
        srcs = {t.owner for ln in bus.lines.values() for t in ln.terms if t.io == "src"}
        bad = set()
        for b in seg.blocks:
            if not (b.ok and b.growth):
                continue
            orig = [o for o in seg.blocks if o.ok and not o.growth and o.spec.id == b.spec.id]
            if f"{b.key}@{b.slot.id}" not in srcs or any(f"{o.key}@{o.slot.id}" not in srcs for o in orig):
                bad.add(b.spec.id)
        if bad:
            return design_segment(z, n, kit, fp, growth=True, skip=frozenset(skip) | bad, fresh_bus=fresh_bus)
    seg.bus = bus
    seg.problems += bus.problems
    for b in seg.blocks:
        owner = f"{b.key}@{b.slot.id}"
        for ln in bus.lines.values():
            for t in ln.terms:
                if t.owner == owner and t.io == "sink":
                    split = bus.runs[t.run].tile(t.pos - 1, ln.lat) if ln.lat >= 0 else None
                    b.taps.append({"item": t.item, "line": ln.key, "lat": ln.lat, "tile": t.tile,
                                   "splitter": split})
    # the whole segment as one world: no clashes, every lane one item, every input port gets exactly its item
    ents, ports = [], []
    for b in seg.blocks:
        if not b.ok:
            continue
        for e in b.wd.entities():
            ts = e["tiles"]
            xs, ys = [t[0] for t in ts], [t[1] for t in ts]
            ents.append(E(e["name"], min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1, e["dir"],
                          e["kind"], e["recipe"], _reach(e["name"])))
        ports += [(f"{b.key}@{b.slot.id}", b.wd.tile((p.x, 0)), next(iter(p.items)))
                  for p in b.wd.design.ports if p.io == "in"]
    seeds = dict(bus.seeds)
    for t in seg.head:                     # the trunks hand their plates onto the head terminals' edge tiles
        seeds[t.tile] = {"left": t.item, "right": t.item}
    seg.problems += world_check(ents + bus.entities, ports, seeds, kit)
    if seg.problems and patched:
        # the lanes kept from the last era clash with the new lines (map 3505858087): the whole bus planned again
        return design_segment(z, n, kit, fp, growth=growth, skip=skip, fresh_bus=True)
    return seg


# ==================================================================== a slot block as what Factory.build places
@dataclass
class SlotBuild:
    """An Atlas block design in its slot, ready for Factory.build: steps in walking order, an output chest on the
    spine edge for a product no other slot takes off the bus, and the brain's input ports (fed by bus taps)."""
    item: str
    per_minute: float
    block: SegBlock
    steps: list = field(default_factory=list)        # atlas build.compile.Step
    stub: list = field(default_factory=list)         # Steps: per out port, an inserter and a chest on the spine edge
    chests: list = field(default_factory=list)       # (x, y) centre of each output chest
    ports: list = field(default_factory=list)        # brain feed ports: {"item", "end", "approach", "feed": True...}
    poles: list = field(default_factory=list)        # tiles
    focus: tuple = (0.0, 0.0)
    placer: str = "atlas-strip"
    notes: list = field(default_factory=list)
    bus_out: list = field(default_factory=list)      # products that go onto the bus (no output chest)

    def all_steps(self) -> list:
        return list(self.steps) + list(self.stub)

    def kit(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self.all_steps():
            out[s.name] = out.get(s.name, 0) + 1
        return out

    def recipes(self) -> set[str]:
        return {s.recipe for s in self.steps if s.recipe}

    def tiles(self) -> set[Tile]:
        return {tuple(t) for s in self.all_steps() for t in s.tiles}


def slot_build(block: SegBlock, onto_bus=frozenset()) -> SlotBuild:
    """Convert a placed Atlas block into a SlotBuild: compile its entities to API steps (inserters face their
    pickup side, undergrounds carry their belt type) and turn each in port into a brain feed port on the spine
    edge tile (a bus tap's branch ends there). An out port whose item is in `onto_bus` (another make block of the
    segment takes it off the bus: Segment.consumed) runs straight into the bus's injection belt; any other gets an
    inserter and an output chest on the spine edge, where links and the courier take the product as from any
    factory."""
    from atlas.build.compile import from_world, step_of, walking_order
    wd = block.wd
    steps = walking_order(from_world(wd, "make"))
    sb = SlotBuild(block.product, round(block.spec.per_s * 60, 2), block, steps)
    x0, y0, w, h = wd.rect
    sb.focus = (x0 + w / 2, y0 + h / 2)
    sb.poles = [tuple(s.tiles[0]) for s in steps if s.kind == "pole"]
    for p in wd.design.ports:
        item = next(iter(p.items))
        rate = round(sum(p.rate.values()) * 60, 2)
        if p.io == "out" and item in onto_bus:
            sb.bus_out.append(item)
        elif p.io == "out":
            ins_t, chest_t = wd.tile((p.x, -1)), wd.tile((p.x, -2))
            sb.stub.append(step_of("inserter", "inserter", [ins_t], wd.direction("north"), group="make"))
            sb.stub.append(step_of("iron-chest", "chest", [chest_t], "north", group="make"))
            sb.chests.append((chest_t[0] + 0.5, chest_t[1] + 0.5))
        else:
            end, appr = wd.tile((p.x, -1)), wd.tile((p.x, -2))
            sb.ports.append({"item": item, "end": list(end), "approach": list(appr), "chest": None, "inserter": None,
                             "face": wd.direction("south"), "feed": True, "per_minute": rate, "fed": None,
                             "slot": block.slot.id})
    sb.notes.append(f"segment block {block.key} in {block.slot.id} (Atlas strip design)")
    return sb


# ==================================================================== the labs slot
# Run 20261127-101219 (and every zones run before it): segment 1 designed labs#0 in its science slot, with the
# packs' bus lines planned from the science slots to it, but only factory goals ever went into slots. Every lab
# went up wherever there was clear ground (a hand-fed row by the spawn, then stations whose long pack belts were
# walled in or polluted), and the planned pack lines were never laid. Now a research_station goal builds the labs
# block in its slot; the pack lines go down with it and the science slots switch from output chests to the bus.
LABS_ASK_GAP_MIN = 30.0    # a labs, science or retire goal the zones proposed is proposed again after this long
RETIRE_AFTER_MIN = 10.0    # the slot's labs work this long, every pack port fed, before the other labs go


def labs_slot_design(w) -> SlotBuild | None:
    """The open segment's unbuilt labs block as a SlotBuild, or None (zones off, no such block, or its ground
    taken since: then the research station goes up free-form as before)."""
    z = getattr(w, "zones", None)
    if not ENABLED or z is None or not z.reserved:
        return None
    try:
        block = z.labs_block()
        if block is None:
            return None
        d = slot_build(block, z.onto_bus(block.n))
    except Exception as exc:  # noqa: BLE001 - the zones are an aid: any trouble and the labs go free-form
        w.log(f"[zones] no labs slot build ({type(exc).__name__}: {str(exc)[:120]}): free-form")
        return None
    taken = {t for t in d.tiles() if w.blocked(t) and t not in w.movable}
    if taken:
        w.log(f"[zones] labs slot {block.slot.id} lost {len(taken)} tiles: free-form instead")
        return None
    return d


def labs_slot_site(base):
    """The labs a segment's science slot holds (None: not built)."""
    if not hasattr(base, "of"):
        return None                           # a stub base in a test
    return next((s for s in base.of("labs") if (s.params or {}).get("segment_block")), None)


def build_labs_slot(w, d: SlotBuild):
    """Place the labs block's Atlas design step by step (a refused building is a gap that repairs place later).
    Its pack ports are feed ports on the spine edge: the bus's pack lines tap into them (laid right after, by
    after_build). The site is kind "labs" but no station: no science link and no foreman feeder ever goes in."""
    import automation_bot as ab
    from .modules import Site
    try:
        from . import viz
        viz.design_slot(d.block)
    except Exception:  # noqa: BLE001 - the stream's view is never a reason to fail
        pass
    placed, failed, gaps = place_steps(w, d.all_steps())
    block = d.block
    labs = [(s.x, s.y) for s in d.all_steps() if s.name == "lab"]
    site = Site("labs", {"count": len(labs), "slot": block.slot.id, "segment_block": block.key, "bus_out": [],
                         "bus_fed": True}, d.focus)
    site.labs = labs
    site.poles = [tuple(q) for q in d.poles]
    ports = [dict(pt) for pt in d.ports]
    for pt in ports:
        w.keepout |= {tuple(pt["end"]), tuple(pt["approach"])}
    site.params["ports"] = ports
    site.params["inputs"] = sorted({pt["item"] for pt in ports})
    site.port_lanes = sorted({tuple(t) for pt in ports for t in (pt["end"], pt["approach"])})
    site.note = f"{placed} built in zone slot {block.slot.id} ({len(labs)} labs, packs by bus)" + (
        f", {len(failed)} failed (first: {failed[0][:120]})" if failed else "")
    if gaps:
        from .gaps import record
        record(site, gaps)
    if failed and placed < len(failed) * 2:
        raise ab.BuildError(f"labs slot mostly failed: {failed[0][:120]}")
    w.zones.mark_built(block, d.focus)
    w.log(f"[zones] labs block {block.key} built in {block.slot.id}: {len(labs)} labs, packs come by bus")
    return site


def switch_to_bus(crew, n: int, items, leftovers_only: bool = False) -> list[str]:
    """Built slot factories of segment n whose product is one of `items` and still goes into an output chest
    (built before the labs: Zones.onto_bus) put it on the bus from now on. Their output inserter and chest stand
    where the planned bus line's injection goes: both come up (with the packs in the chest), and so does every
    belt link out of that chest (the stations elsewhere lose that colour; the slot's labs take it by bus).
    leftovers_only: only sites switched before whose inserter or chest still stands (a take-up that failed):
    taken up again, so the bus repair can place the line's belt there. The take-up is a "remove" step (never
    paused): run 20261127-101219 had the overseer's pause land inside it, green's inserter stayed on its line's
    first tile and the bus repair failed there with cannot_place."""
    from .world import Preempted
    z = crew.zones
    seg = z.segment(n)
    items = set(items)
    notes = []
    by_key = {b.key: b for b in seg.blocks}
    for s in list(crew.base.sites):
        key = (s.params or {}).get("segment_block")
        b = by_key.get(key)
        if b is None or b.spec.kind != "make" or b.product not in items:
            continue
        switched = b.product in (s.params.get("bus_out") or [])
        if switched != leftovers_only:
            continue
        if not switched and b.product not in seg.consumed() and not seg.feeds_line(b):
            continue                          # no planned line starts at its output: its chest stays
        stub = slot_build(b, frozenset()).stub
        tiles = {tuple(t) for st in stub for t in st.tiles}
        left = [e for e in s.entities if (math.floor(e[1]), math.floor(e[2])) in tiles
                and e[0] in ("inserter", "iron-chest")]
        if switched and not left:
            continue
        chests = [(x, y, what) for x, y, what in s.chests if (math.floor(x), math.floor(y)) in tiles]
        gone = []
        with crew.w.step("remove"):
            for x, y, _ in chests:
                for ln in [q for q in getattr(crew, "links", []) if tuple(q.get("source") or ()) == (x, y)]:
                    try:
                        crew._unlink(ln)
                    except Preempted:
                        raise
                    except Exception as exc:  # noqa: BLE001 - one stuck link never keeps the packs off the bus
                        crew.log(f"[zones] taking up a link out of site {s.id}'s chest failed: {str(exc)[:100]}")
            for e in left:
                try:
                    crew.w.pickup(*e)
                except Preempted:
                    raise
                except Exception as exc:  # noqa: BLE001 - taken up again from the next science check
                    crew.log(f"[zones] taking up {e[0]} at ({e[1]}, {e[2]}) failed: {str(exc)[:100]}")
                    continue
                gone.append(e)
        s.entities[:] = [e for e in s.entities if e not in gone]
        s.chests[:] = [c for c in s.chests if c not in chests]
        if not switched:
            s.params.setdefault("bus_out", []).append(b.product)
            notes.append(f"site {s.id} puts its {b.product} on the bus now ({len(gone)} buildings of its output "
                         "chest taken up)")
        elif gone:
            notes.append(f"site {s.id}: {len(gone)} leftover building(s) of its old output chest taken up; "
                         + _lay_freed(crew, n, b.product, {(math.floor(e[1]), math.floor(e[2])) for e in gone}))
        if notes:
            crew.log(f"[zones] {notes[-1]}")
    return notes


def _lay_freed(crew, n: int, item: str, tiles: set) -> str:
    """The item's bus line pieces on tiles just freed, placed now. Their gaps leave the bus site's repair list;
    any the game still refuses get their repair tries back (a gap tried twice counts as blocked for good)."""
    site = bus_site(crew, n)
    steps = [st for st in (crew.zones.segment(n).jobs().get(f"bus:{item}") or [])
             if {tuple(t) for t in st.tiles} & tiles]
    if site is None or not steps:
        return f"no {item} line piece there"
    done = {tuple(k) for k in site.params.setdefault("placed", [])}

    def rec(st) -> None:
        site.params["placed"].append(list(step_key(st)))
        site.entities.append((st.name, st.x, st.y, st.direction))
    with crew.w.step("build"):
        placed, failed, _ = place_steps(crew.w, steps, done, rec)
    keys = {step_key(st) for st in steps}
    gaps = site.params.get("gaps") or []
    put = {tuple(k) for k in site.params["placed"]}
    site.params["gaps"] = [g for g in gaps if (g.get("name"), round(float(g.get("x", 0)) * 2) / 2,
                                               round(float(g.get("y", 0)) * 2) / 2) not in put]
    for g in site.params["gaps"]:
        if (g.get("name"), round(float(g.get("x", 0)) * 2) / 2, round(float(g.get("y", 0)) * 2) / 2) in keys:
            g["tries"] = 0
    return f"{placed} {item} line piece(s) laid there" + (f", {len(failed)} refused (repair retries)" if failed else "")


def _propose(crew, goal: dict) -> bool:
    try:
        from .agenda import agenda_of
        ok, _ = agenda_of(crew).propose(crew, goal, who="zones")
        return bool(ok)
    except Exception:  # noqa: BLE001 - a crew without an agenda: queue it plainly
        urgent = getattr(crew, "urgent", None)
        if urgent is None:
            return False
        urgent.append(goal)
        return True


def science_check(crew, z, now: float) -> str:
    """The labs slot's own goals, each proposed once per LABS_ASK_GAP_MIN:
    - the labs block, once a science slot factory stands (a research_station goal: ResearchStation builds the
      slot's design);
    - a science block not built yet, once the labs stand (its factory goal goes into its slot, onto the bus);
    - the other labs, once the slot's labs have worked RETIRE_AFTER_MIN with every pack port fed: removed (their
      packs now go to the slot, so they would stand idle)."""
    asked = z.__dict__.setdefault("labs_asked", {})
    due = lambda k: now - asked.get(k, -1e9) >= LABS_ASK_GAP_MIN
    notes = []
    lb = z.labs_block(built=None)
    if lb is None:
        return ""
    seg = z.segment(lb.n)
    packs = {p["item"] for p in lb.ports if p["io"] == "in"}
    makers = [b for b in seg.blocks if b.spec.kind == "make" and b.ok and not b.growth and b.product in packs]
    if not z.is_built(lb):
        if any(z.is_built(b) for b in makers) and due("labs"):
            goal = {"do": "build", "module": "research_station", "count": int(getattr(lb.spec, "labs", 0) or 6),
                    "reflex": f"zones: the science slot {lb.slot.id} takes the labs ({lb.key}); the packs come "
                              "to them by bus"}
            if _propose(crew, goal):
                asked["labs"] = now
                notes.append(f"zones: labs block {lb.key} queued for {lb.slot.id}")
        return "; ".join(notes)
    # the labs stand: their pack lines must be laid, and no old output chest may stand on them (self-heal)
    laid = laid_items(crew, lb.n)
    if not packs <= laid and due("pack-lines") and propose_bus(crew, lb.n, "the labs' pack lines"):
        asked["pack-lines"] = now
        notes.append("zones: the labs' pack lines queued (not laid yet)")
    notes += switch_to_bus(crew, lb.n, packs & laid, leftovers_only=True)
    for b in makers:
        if not z.is_built(b) and due(b.key):
            goal = {"do": "build", "module": "factory", "item": b.product,
                    "per_minute": round(float(b.spec.per_s) * 60, 1),
                    "reflex": f"zones: the slot's labs take {b.product} by bus: its block {b.key} goes into "
                              f"{b.slot.id}"}
            if _propose(crew, goal):
                asked[b.key] = now
                notes.append(f"zones: {b.product} queued for {b.slot.id}")
    site = labs_slot_site(crew.base)
    if site is None:
        return "; ".join(notes)
    fed = all(pt.get("fed") for pt in site.params.get("ports", []))
    working = (site.health or {}).get("working", 0) > 0
    since = z.__dict__.setdefault("labs_running", {})
    if not (fed and working):
        since.pop("slot", None)
        return "; ".join(notes)
    t0 = since.setdefault("slot", now)
    if now - t0 < RETIRE_AFTER_MIN:
        return "; ".join(notes)
    for other in crew.base.of("labs"):
        if other is site or (other.params or {}).get("segment_block") or not due(f"retire-{other.id}"):
            continue
        goal = {"do": "remove", "site": other.id,
                "reflex": f"zones: the floorplan's labs (site {site.id} in {site.params.get('slot')}) take every "
                          "pack by bus; these labs stand idle"}
        if _propose(crew, goal):
            asked[f"retire-{other.id}"] = now
            notes.append(f"zones: labs site {other.id} retired (the slot's labs run)")
    return "; ".join(notes)


# ==================================================================== the crew's hooks
def attach(crew, zones: Zones | None) -> None:
    """One Zones object for the crew, its world (world.free asks it) and the factory module."""
    crew.zones = zones
    crew.w.zones = zones
    if zones is not None:
        zones._world = crew.w                 # transient: a segment re-planned after a resume reads the live world
    try:
        from .modules import MODULES
        MODULES["factory"].zones = zones
    except Exception:  # noqa: BLE001
        pass


def after_build(crew, site) -> str:
    """Called by the crew after every build. The first electric build reserves the floorplan; once segment 1's
    conditions hold, the segment opens. Never raises: zones are an aid, never a reason for a build to fail."""
    if not ENABLED:
        return ""
    try:
        w = crew.w
        z = getattr(crew, "zones", None)
        if z is None:
            now0 = crew._minute() if hasattr(crew, "_minute") else 0.0
            if now0 > START_BY_MIN:
                # an old base (resumed past the opening): the floorplan would be drawn round a finished base and
                # its slots would only copy what stands; it stays organic
                return ""
            z = Zones()
            attach(crew, z)
        elif getattr(w, "zones", None) is not z:
            attach(crew, z)
        note = ""
        now = crew._minute() if hasattr(crew, "_minute") else 0.0
        due = (z.tried_minute is None and (site.electric or site.kind == "steam_power")) or \
            (z.tried_minute is not None and now - z.tried_minute >= RETRY_MIN)
        if not z.reserved and due:
            z.tried_minute = now
            survey(w, crew.base)
            snap = z.snapshot(w, crew.base)
            think = getattr(w, "think", None)
            res = think("planning the floorplan out of the start yard", z.plan, None, None, None, snap) \
                if think is not None else z.plan(snap=snap)
            n = z.apply(*res, log=crew.log)
            note = f"zones: {n} tiles reserved out of the start yard"
        if z.reserved and OIL_LAZY and "oil" not in z.corridors and \
                (z.oil_tried is None or now - z.oil_tried >= RETRY_MIN):
            snap = z.oil_snapshot(w, crew.base)          # crude known: keep its corridor free from now on
            if snap is not None:
                z.oil_tried = now
                think = getattr(w, "think", None)
                res = think("planning the oil corridor", z.plan_oil, snap) if think is not None else z.plan_oil(snap)
                if z.apply_oil(*res, log=crew.log):
                    note = (note + "; " if note else "") + "zones: oil corridor reserved"
        if z.reserved and not z.is_open(1) and z.tried_open != now:
            ok, _ = z.ready(1, crew)
            if ok:
                z.tried_open = now
                kit = kit_for(w)
                scan = scan_from_world(w, z.centre)
                think = getattr(w, "think", None)
                seg = think("laying out segment 1 (slots, blocks and bus)", z.segment, 1, None, kit, scan) \
                    if think is not None else z.segment(1, None, kit, scan)
                if seg.problems and any(b.growth for b in seg.blocks):
                    # the planned copies must never keep a segment shut: plan it without them
                    z.__dict__.setdefault("no_growth", set()).add(1)
                    crew.log(f"[zones] segment 1 with copies in its spare slots does not check out "
                             f"({seg.problems[0][:120]}): planned without them")
                    if think is not None:
                        seg = think("laying out segment 1 (slots, blocks and bus)", z.segment, 1, None, kit, scan)
                    else:
                        seg = z.segment(1, None, kit, scan)
                if seg.problems:                         # never open a segment whose bus does not check out
                    crew.log(f"[zones] segment 1 not opened: {seg.problems[0][:160]}")
                    z.warnings.append(f"segment 1: {seg.problems[0][:160]}")
                else:
                    z.open_segment(1, w, kit, minute=now, log=crew.log)
                    queued = propose_bus(crew, 1, "segment 1 opened")
                    note = (note + "; " if note else "") + "zones: segment 1 open" + (
                        ", its bus queued" if queued else "")
        if site.kind == "labs" and site.params.get("segment_block"):
            # the slot's labs stand: their pack lines go down now (the science slots' chests switch to the bus)
            lb = z.block_by_key(site.params["segment_block"])
            if lb is not None:
                laid = lay_bus(crew, lb.n, z.segment(lb.n).inputs_of(z.active_blocks(lb.n)))
                note = (note + "; " if note else "") + f"zones: labs' pack lines: {laid}"
        nxt = (z.current or 0) + 1
        era_next = z.era_of(z.current) + 1 if z.current else 0
        tried = z.__dict__.setdefault("tried_era", {})
        if z.reserved and z.current and era_next in SEGMENTS and now - tried.get(era_next, -1e9) >= RETRY_MIN:
            z.__dict__.setdefault("plans", {})[nxt] = {"era": era_next, "extras": z.extras_of(z.current)}
            ok, why_not = z.ready(nxt, crew)
            if not ok:
                z.plans.pop(nxt, None)
            if ok:
                tried[era_next] = now
                kit = kit_for(w)
                think = getattr(w, "think", None)
                seg = think(f"laying out segment {nxt} (slots, blocks and the bus patched)", z.segment, nxt, None,
                            kit) if think is not None else z.segment(nxt, None, kit)
                bad = [p for p in seg.problems] + [f"{b.key}: {b.problems[0] if b.problems else 'no design'}"
                                                   for b in seg.blocks if not b.ok and not b.growth]
                if bad and any(b.growth for b in seg.blocks):
                    # the planned copies must never keep an era shut: planned without them
                    z.__dict__.setdefault("no_growth", set()).add(nxt)
                    z._seg = {k: v for k, v in z._seg.items() if k[0] != nxt}
                    seg = think(f"laying out segment {nxt} without copies", z.segment, nxt, None, kit)                         if think is not None else z.segment(nxt, None, kit)
                    bad = [p for p in seg.problems] + [f"{b.key}: {b.problems[0] if b.problems else 'no design'}"
                                                       for b in seg.blocks if not b.ok and not b.growth]
                if bad:
                    crew.log(f"[zones] segment {nxt} not opened: {bad[0][:160]}")
                    z.warnings.append(f"segment {nxt}: {bad[0][:160]}")
                    z._seg = {k: v for k, v in z._seg.items() if k[0] != nxt}   # designed again next time
                    z.plans.pop(nxt, None)
                else:
                    z.open_segment(nxt, w, kit, minute=now, log=crew.log)
                    queued = propose_bus(crew, nxt, f"segment {nxt} opened")
                    note = (note + "; " if note else "") + f"zones: segment {nxt} open" + (
                        ", its bus and reworks queued" if queued else "")
        if site.params.get("segment_block"):            # a slot factory: ports on laid lines are fed by their taps
            for n in z.live():
                feed_ports(crew, n)
        grown = grow_check(crew, z, now)
        if grown:
            note = (note + "; " if note else "") + grown
        sci = science_check(crew, z, now)
        if sci:
            note = (note + "; " if note else "") + sci
        return note
    except Exception as exc:  # noqa: BLE001 - never let the planner break a build
        try:
            crew.log(f"[zones] skipped: {type(exc).__name__}: {str(exc)[:160]}")
        except Exception:  # noqa: BLE001
            pass
        return ""


def _flat_out(site) -> bool | None:
    """Is a slot factory running flat out on the last patrol's look (most assemblers working, almost none with
    their output backed up)? None: not looked at yet."""
    census = getattr(site, "census", None) or {}
    st: dict[str, int] = {}
    for name, c in census.items():
        if "assembling-machine" in name:
            for k, v in c.items():
                st[k] = st.get(k, 0) + int(v)
    total = sum(st.values())
    if not total:
        return None
    full = st.get("full_output", 0) + st.get("waiting_for_space_in_destination", 0)
    return st.get("working", 0) / total >= GROW_WORKING and full / total <= GROW_FULL


def slot_sites(crew, z, item: str) -> list:
    """The built slot factories (sites) of the open segments' blocks making this item, originals first."""
    keys = []
    for n in z.live():
        for b in z.segment(n).blocks:
            if b.ok and b.product == item and z.is_built(b):
                keys.append(b.key)
    by_key = {(s.params or {}).get("segment_block"): s for s in crew.base.sites}
    return [by_key[k] for k in keys if k in by_key]


def grow_slot(crew, z, item: str, why: str = "") -> str | None:
    """More of a floorplan item: its slot factory grows instead of a free-form factory going up elsewhere.
    None when this is not for the slot (the item is no block of an open segment, or one of its blocks, a planned
    copy included, is still unbuilt: Factory._slot_design builds that). Otherwise the slot factory's machines are
    swapped for faster ones in place (upgrade.py; assembling machine 1 -> 2 is 1.5x); raises BuildError when the
    slot factory is already at the fastest machines unlocked (then a free-form factory may cover the rest)."""
    if not any(b.ok and b.product == item for n in z.live() for b in z.segment(n).blocks):
        return None
    if z.slot_block(item) is not None:
        return None                                  # a slot or a planned copy is free: Factory builds it
    from . import upgrade
    for site in slot_sites(crew, z, item):
        if upgrade.candidates(crew, site):
            note = crew.upgrade(site.id)
            crew.log(f"[zones] {item}: more asked ({why}); slot factory site {site.id} grows in place: {note}")
            return f"{item} slot factory site {site.id} grew instead of a new factory: {note}"
    from .world import BuildError
    raise BuildError(f"every {item} slot factory already has the fastest machines unlocked")


def add_extra(crew, z, item: str, per_min: float) -> SegBlock | None:
    """A factory for an item no era plans joins the zones bus-fed: the live plan is planned again with an extra
    block for it (its inputs bus items only), every block keeping its slot, the extra in a free one, the bus
    patched for its inputs (the take-over of an era change: adopt_bus, rework_old). Returns the block to build
    (Factory._slot_design builds it), or None: no live plan, the item already planned, no slot or no design
    (then the factory goes free-form outside the zones, as before)."""
    if not ENABLED or not z.opened or item in z.planned_items():
        return None
    if not z.general_slots():
        return None
    cur = z.current
    nxt = cur + 1
    plans = z.__dict__.setdefault("plans", {})
    plans[nxt] = {"era": z.era_of(cur), "extras": {**z.extras_of(cur), item: round(float(per_min), 1)}}
    try:
        w = crew.w
        think = getattr(w, "think", None)
        seg = think(f"planning the zones again with a {item} block", z.segment, nxt) if think is not None \
            else z.segment(nxt)
    except Exception as exc:  # noqa: BLE001
        crew.log(f"[zones] no {item} block for the zones ({type(exc).__name__}: {str(exc)[:100]}): free-form")
        plans.pop(nxt, None)
        z._seg = {k: v for k, v in z._seg.items() if k[0] != nxt}
        return None
    blk = next((b for b in seg.blocks if b.product == item and b.ok and not b.growth), None)
    bad = list(seg.problems) + [b.key for b in seg.blocks if not b.ok and not b.growth]
    if blk is None or bad:
        crew.log(f"[zones] no {item} block fits the zones ({(bad or ['no design'])[0][:120]}): free-form")
        plans.pop(nxt, None)
        z._seg = {k: v for k, v in z._seg.items() if k[0] != nxt}
        return None
    now = crew._minute() if hasattr(crew, "_minute") else 0.0
    z.open_segment(nxt, crew.w, seg.kit, minute=now, log=crew.log)
    propose_bus(crew, nxt, f"a {item} block joined the zones")
    crew.log(f"[zones] {item} joins the zones as {blk.key} in {blk.slot.id}, fed by the bus")
    return blk


def grow_check(crew, z, now: float) -> str:
    """A built slot block that has run flat out for GROW_FLAT_MIN gets its planned copy: a factory goal for its
    item (Factory._slot_design puts it in the copy's spare slot, the bus taps come with it). Returns a note."""
    watch = z.__dict__.setdefault("grow_watch", {})       # block key -> minute first seen flat out
    asked = z.__dict__.setdefault("grow_asked", {})       # copy key -> minute its goal was proposed
    sites = {s.params.get("segment_block"): s for s in crew.base.sites if (s.params or {}).get("segment_block")}
    notes = []
    for n in z.live():                           # (a segment without planned copies still grows by upgrades)
        seg = z.segment(n)
        for b in seg.blocks:
            if b.growth or not b.ok or not z.is_built(b):
                continue
            copy = next((c for c in seg.blocks if c.growth and c.ok and c.spec.id == b.spec.id
                         and not z.is_built(c)), None)
            site = sites.get(b.key)
            if site is None:
                continue
            if copy is None:                       # no copy planned (or built): faster machines in place
                from . import upgrade
                if not upgrade.candidates(crew, site):
                    continue
            flat = _flat_out(site)
            if flat is None:
                continue
            if not flat:
                watch.pop(b.key, None)
                continue
            since = watch.setdefault(b.key, now)
            ask_key = copy.key if copy is not None else f"upgrade:{site.id}"
            if now - since < GROW_FLAT_MIN or (ask_key in asked and now - asked[ask_key] < GROW_REPEAT_MIN):
                continue
            if copy is not None:
                rate = round(float(copy.spec.per_s) * 60, 1)
                goal = {"do": "build", "module": "factory", "item": b.product, "per_minute": rate,
                        "reflex": f"zones: {b.key} has run flat out for {now - since:.0f} min: its copy in "
                                  f"{copy.slot.id} goes up"}
            else:
                goal = {"do": "upgrade", "site": site.id,
                        "reflex": f"zones: {b.key} has run flat out for {now - since:.0f} min: faster machines "
                                  "in its slot"}
            try:
                from .agenda import agenda_of
                ok, _ = agenda_of(crew).propose(crew, goal, who="zones")
            except Exception:  # noqa: BLE001
                urgent = getattr(crew, "urgent", None)
                ok = urgent is not None
                if ok:
                    urgent.append(goal)
            if ok:
                asked[ask_key] = now
                where = f"the copy goes into {copy.slot.id}" if copy is not None else \
                    f"faster machines in site {site.id}"
                notes.append(f"zones: more {b.product} (its block ran flat out {now - since:.0f} min; {where})")
                crew.log(f"[zones] growing {b.key}: flat out for {now - since:.0f} min, {where}")
    return "; ".join(notes)


def survey(world, base, half: int = 160, step: int = 40) -> None:
    """Remote looks (no walking) over the land round the base, so the floorplan has ground to plan on. Points the
    level's observe range does not reach are skipped. The looks (radius 32) overlap: 64 apart they left a diamond of
    unseen land between every four, and a 40-wide spine never crossed one (the zones test, seed 626262: 107k tiles
    seen out to 200 tiles, yet every spine stopped after 1-2 cells)."""
    centre = (base.centre if base is not None else None) or getattr(world, "pos", None)
    look = getattr(world, "observe", None)
    if centre is None or look is None:
        return
    rng = float(getattr(world, "observe_range", 60)) - 2
    pos = getattr(world, "pos", centre)
    for dx in range(-half, half + 1, step):
        for dy in range(-half, half + 1, step):
            x, y = centre[0] + dx, centre[1] + dy
            if math.dist((x, y), pos) > rng:
                continue
            around = [(math.floor(x) + ox, math.floor(y) + oy) for ox in (-16, 0, 16) for oy in (-16, 0, 16)]
            if all(t in world.seen for t in around):
                continue
            try:
                look(32, x=x, y=y, include=["entities", "resources", "obstacles", "water"])
            except Exception:  # noqa: BLE001 - a refused look: plan on what is known
                continue


# ==================================================================== the segment bus, built
BUS_BUDGET_MIN = 30.0     # game minutes a bus goal may take (crew.GOAL_BUDGET_MIN["bus"])
HEAD_SHORT = 0.5          # a head port's sources give less than this share of its demand: plates first


def step_key(s) -> tuple:
    return (s.name, round(s.x * 2) / 2, round(s.y * 2) / 2)


def place_steps(w, steps, done: set | None = None, on_placed=None) -> tuple[int, list[str], list[dict]]:
    """Place atlas build steps one by one: (placed, failures, gaps). Steps whose key is in `done` are skipped
    (a resumed job) and every step placed is added to it. A building the game refuses is a gap (fab.build's
    format, so Crew.repair places it later); an assembler gets its recipe once it stands."""
    import automation_bot as ab
    from fab.build import _gap, _retry
    placed, failed, gaps = 0, [], []
    for s in steps:
        k = step_key(s)
        if done is not None and k in done:
            continue
        tiles = {tuple(t) for t in s.tiles}
        try:
            if s.belt_type:                      # an underground: the API takes its belt type
                w.clear(tiles)
                w.near(s.x, s.y)
                if any(abs(w.pos[0] - t[0] - 0.5) < 1.2 and abs(w.pos[1] - t[1] - 0.5) < 1.2 for t in tiles):
                    w.step_out(tiles)
                r = _retry(w, tiles, lambda s=s: w.run.place(s.name, s.x, s.y, s.direction, s.belt_type))
                if not r.get("ok"):
                    raise ab.BuildError(f"place {s.name} at ({s.x}, {s.y}): {r.get('code')} {r.get('message', '')}")
                w.used.update(tiles)
                w.inventory[s.name] = max(0, w.inventory.get(s.name, 0) - 1)
            else:
                w.place(s.name, s.x, s.y, s.direction, tiles)
        except ab.BuildError as exc:
            failed.append(f"{s.name} at ({s.x}, {s.y}): {str(exc)[:120]}")
            gaps.append(_gap(s.name, s.x, s.y, s.direction, str(exc), recipe=s.recipe, type=s.belt_type))
            continue
        placed += 1
        if done is not None:
            done.add(k)
        if on_placed is not None:
            on_placed(s)
        if s.recipe and s.name.startswith("assembling-machine"):
            r = w.run.set_recipe(s.x, s.y, s.recipe)
            if not r.get("ok"):
                failed.append(f"recipe {s.recipe} at ({s.x}, {s.y}): {r.get('code')}")
                gaps.append(_gap(s.name, s.x, s.y, s.direction, f"recipe: {r.get('code')}", recipe=s.recipe,
                                 placed=True))
    return placed, failed, gaps


def bus_site(crew, n: int, create: bool = False):
    """The site that records segment n's bus: what was placed (so a paused or failed bus goal resumes where it
    stopped), which item networks are laid, its gaps (Crew.repair), and the head ports trunks feed."""
    site = next((s for s in crew.base.of("bus") if s.params.get("segment") == n), None)
    if site is None and create:
        from .modules import Site
        sp = crew.zones.corridors["spine"]
        cx = sum(c[0] for c in sp.cells) / len(sp.cells) + sp.width / 2
        cy = sum(c[1] for c in sp.cells) / len(sp.cells) + sp.width / 2
        site = crew.base.add(Site("bus", {"segment": n, "laid": [], "placed": [], "ports": []}, (cx, cy)))
        try:
            site.built_minute = crew._minute()
        except Exception:  # noqa: BLE001
            pass
        site.note = f"segment {n} bus along the spine"
    return site


def laid_items(crew, n: int) -> set[str]:
    site = bus_site(crew, n)
    return set(site.params.get("laid") or ()) if site is not None else set()


def lay_bus(crew, n: int, items, extra=()) -> str:
    """Place the poles and the belt networks of `items` from segment n's planned bus (each item: its lines,
    transfers, taps and injections), skipping what already stands. Only the lines the open slots use: the caller
    passes their input items. Then the ports those lines tap are fed, and the head ports updated."""
    from collections import Counter
    z = crew.zones
    seg = z.segment(n)
    jobs = seg.jobs()
    if not jobs:
        return "segment has no bus"
    site = bus_site(crew, n, create=True)
    laid = set(site.params.setdefault("laid", []))
    names = ["bus:poles"] + [f"bus:{i}" for i in sorted(items) if f"bus:{i}" in jobs]
    todo = [nm for nm in names if nm.split(":", 1)[1] not in laid]
    msg = ""
    if todo:
        # a slot factory built before the labs has its output chest where its product's line injects: the chest
        # comes up first, and the factory puts it on the bus from now on
        switched = switch_to_bus(crew, n, {nm.split(":", 1)[1] for nm in todo} & z.onto_bus(n))
        if switched:
            msg = "; ".join(switched) + "; "
        done = {tuple(k) for k in site.params.setdefault("placed", [])}
        steps = [s for nm in todo for s in jobs[nm] if step_key(s) not in done]
        kit = dict(Counter(s.name for s in steps))
        if kit:
            with crew.w.step("unlock"):
                crew.unlock_items(list(kit))
            with crew.w.step("make"):
                crew.make(kit)

        def rec(s) -> None:
            site.params["placed"].append(list(step_key(s)))
            site.entities.append((s.name, s.x, s.y, s.direction))
            if s.kind == "pole":
                site.poles.append(tuple(s.tiles[0]))

        crew.w.doing(f"laying segment {n}'s bus: {', '.join(nm[4:] for nm in todo)}")
        with crew.w.step("build"):
            placed, failed, gaps = place_steps(crew.w, steps, done, rec)
        if gaps:
            from .gaps import record
            record(site, gaps)
        for nm in todo:                          # a network is laid once all of it stands or is a gap to repair
            site.params["laid"].append(nm.split(":", 1)[1])
        msg += f"laid {placed} bus pieces ({', '.join(nm[4:] for nm in todo)})" + (
            f", {len(failed)} refused (gaps for repair; first: {failed[0][:100]})" if failed else "")
        if site.poles:
            try:
                msg += "; " + crew.connect(site)
            except Exception as exc:  # noqa: BLE001 - the poles stand; connecting is retried by the crew
                msg += f"; not connected yet ({str(exc)[:80]})"
    fed = feed_ports(crew, n)
    head = update_head(crew, n, extra)
    if fed:
        msg += f"; {fed} slot port(s) fed by bus taps"
    if head:
        msg += f"; head ports for {', '.join(head)}"
    return msg or "bus already laid"


def feed_ports(crew, n: int) -> int:
    """Every slot factory's input port on a laid line is fed by its tap: marked like a link's (port["fed"]), so
    the link step, relink, triage and the stale checks never belt into it. Returns how many were marked now."""
    laid = laid_items(crew, n)
    count = 0
    for s in crew.base.sites:
        if not s.params.get("segment_block"):
            continue
        for pt in s.params.get("ports", []):
            if pt.get("item") in laid and not pt.get("fed"):
                pt["fed"] = ["bus", pt["item"]]
                pt["bus_tap"] = True
                count += 1
    return count


def update_head(crew, n: int, extra=()) -> list[str]:
    """The bus site's head ports: one per plate line that is laid, sized to what the active slots (built, and the
    one being built) take. Existing port dicts are kept (a link that feeds one keeps its port)."""
    z = crew.zones
    seg = z.segment(n)
    site = bus_site(crew, n)
    if site is None:
        return []
    laid = set(site.params.get("laid") or ())
    want = seg.demand(z.active_blocks(n, extra))
    ports = site.params.setdefault("ports", [])
    have = {pt["item"]: pt for pt in ports}
    out = []
    for hp in seg.head_ports(z.corridors["spine"]):
        item = hp["item"]
        if item not in laid or item not in want:
            continue
        rate = round(want[item] * 60, 1)
        if item in have:
            have[item]["per_minute"] = rate
        else:
            hp["per_minute"] = rate
            ports.append(hp)
        out.append(item)
    return out


def head_supply(crew, n: int) -> str:
    """Plates into the head: for each unfed head port, the plates first (mines or a dense field, when the spare
    sources give less than HEAD_SHORT of its demand), then a trunk goal (crew.trunk: a long belt from the nearest
    spare source, or a link when one is in reach) proposed through the agenda."""
    site = bus_site(crew, n)
    if site is None:
        return ""
    notes = []
    for pt in site.params.get("ports", []):
        if pt.get("fed") or not pt.get("item"):
            continue
        item, want = pt["item"], float(pt.get("per_minute") or 0)
        more = getattr(crew, "_more_plates", None)
        try:
            spare = crew.spare_rate(item)
        except Exception:  # noqa: BLE001 - a stub crew
            spare = want
        if more is not None and item in getattr(crew, "PLATE_ORE", {}) and spare < HEAD_SHORT * want:
            try:
                got = more(item, want - spare)
                if got:
                    notes.append(got)
            except Exception as exc:  # noqa: BLE001 - the trunk still takes whatever plates there are
                notes.append(f"plates for the head failed: {str(exc)[:100]}")
        if propose_trunk(crew, site, item):
            notes.append(f"trunk of {item} into the bus head queued")
    return "; ".join(notes)


def propose_trunk(crew, site, item: str) -> bool:
    goal = {"do": "trunk", "site": site.id, "item": item, "reflex": "zones: the bus head takes plates"}
    try:
        from .agenda import agenda_of
        # first in line after the rule floor's fuel and power: every slot factory on the bus waits on it (the
        # zones test, seed 737373: queued at the back, it was still waiting when another decider dropped it)
        ok, _ = agenda_of(crew).propose(crew, goal, who="zones", front=True)
        return ok
    except Exception:  # noqa: BLE001 - a crew without an agenda: queue it plainly
        urgent = getattr(crew, "urgent", None)
        if urgent is not None and not any(g.get("do") == "trunk" and g.get("site") == site.id
                                          and g.get("item") == item for g in urgent):
            urgent.append(goal)
            return True
        return False


def propose_bus(crew, n: int, why: str) -> bool:
    goal = {"do": "bus", "segment": n, "reflex": f"zones: {why}"}
    try:
        from .agenda import agenda_of
        ok, _ = agenda_of(crew).propose(crew, goal, who="zones")
        return ok
    except Exception:  # noqa: BLE001
        urgent = getattr(crew, "urgent", None)
        if urgent is not None and not any(g.get("do") == "bus" and g.get("segment") == n for g in urgent):
            urgent.append(goal)
            return True
        return False


def adopt_bus(crew, n: int) -> str:
    """Segment n took over from n-1: the old bus site becomes n's. Pieces n's bus no longer plans come up; the rest
    stay as placed (lay_bus skips them) and every network is laid again for n (only what is missing goes down)."""
    z = crew.zones
    old = (z.opened.get(n) or {}).get("from")
    if old is None or bus_site(crew, n) is not None:
        return ""
    site = bus_site(crew, old)
    if site is None:
        return ""
    keys = {step_key(st) for job in z.segment(n).jobs().values() for st in job}
    placed = [tuple(k) for k in site.params.get("placed") or []]
    stale = [k for k in placed if k not in keys]
    w, up, failed = crew.w, 0, []
    staleset = set(stale)
    keep_ents = []
    for e in list(site.entities):
        k = (e[0], round(e[1] * 2) / 2, round(e[2] * 2) / 2)
        if k not in staleset:
            keep_ents.append(e)
            continue
        try:
            w.doing(f"taking up segment {old}'s bus piece {e[0]} at ({e[1]:.0f}, {e[2]:.0f}): segment {n} re-routes")
            if w.pickup(e[0], e[1], e[2], e[3]):
                up += 1
                continue
            failed.append(f"{e[0]} at ({e[1]:.0f}, {e[2]:.0f})")
        except Exception as exc:  # noqa: BLE001 - a piece that will not come up stays recorded
            failed.append(f"{e[0]}: {str(exc)[:60]}")
        keep_ents.append(e)
    site.entities = keep_ents
    site.params["segment"] = n
    site.params["laid"] = []
    site.params["placed"] = [list(k) for k in placed if k in keys]
    site.params.pop("gaps", None)
    site.note = f"segment {n} bus along the spine (from segment {old}'s)"
    return f"segment {old}'s bus is segment {n}'s now: {up} piece(s) no longer planned taken up" + (
        f", {len(failed)} would not come up" if failed else "")


def rework_old(crew, n: int) -> list[str]:
    """The blocks segment n reworks: each old slot site comes up, then its new design is queued (a factory goal,
    or the labs). Done once per block (the rework entry is marked)."""
    z = crew.zones
    st = z.opened.get(n) or {}
    notes = []
    for r in st.get("rework") or []:
        if r.get("done"):
            continue
        site = next((s for s in crew.base.sites if (s.params or {}).get("segment_block") == r["key"]
                     and (s.params or {}).get("slot") == r["slot"]), None)
        if site is not None:
            try:
                crew.remove(site.id, force=True)
                notes.append(f"site {site.id} ({r['key']}) taken up for segment {n}'s design")
            except Exception as exc:  # noqa: BLE001 - tried again on the next bus goal
                notes.append(f"site {site.id} would not come up: {str(exc)[:80]}")
                continue
        r["done"] = True
        if r.get("dropped"):
            continue
        blk = next((b for b in z.segment(n).blocks if b.slot.id == r["slot"] and b.ok and not b.growth), None)
        if blk is None:
            continue
        if blk.spec.kind == "labs":
            goal = {"do": "build", "module": "research_station", "count": int(getattr(blk.spec, "labs", 0) or 8),
                    "reflex": f"zones: segment {n}'s labs ({blk.key}) go into {blk.slot.id}"}
        else:
            goal = {"do": "build", "module": "factory", "item": blk.product,
                    "per_minute": round(float(blk.spec.per_s) * 60, 1),
                    "reflex": f"zones: segment {n} reworks {blk.key} in {blk.slot.id}"}
        if _propose(crew, goal):
            notes.append(f"{blk.key} queued for {blk.slot.id}")
    return notes


def run_bus_goal(crew, goal: dict) -> str:
    """The "bus" goal: segment n's poles, the lines its built slots use, their taps, and trunks into the head. A
    segment that took over from the last one first adopts its bus and reworks the blocks whose design changed."""
    z = getattr(crew, "zones", None)
    n = int(goal.get("segment") or 1)
    if z is None or not z.is_open(n):
        import automation_bot as ab
        raise ab.BuildError(f"segment {n} is not open (zones off, or not ready yet)")
    seg = z.segment(n)
    pre = [x for x in [adopt_bus(crew, n)] + rework_old(crew, n) if x]
    msg = ("; ".join(pre) + "; " if pre else "") + lay_bus(crew, n, seg.inputs_of(z.active_blocks(n)))
    h = head_supply(crew, n)
    return msg + (f"; {h}" if h else "")


def bus_first(crew, module: str, params: dict) -> str:
    """Before a slot factory is built: the bus lines it and the other built slots draw on (with their taps), and
    plates into the head. Returns a note ("" for anything that is not a slot factory)."""
    if module != "factory" or not ENABLED or getattr(crew, "zones", None) is None:
        return ""
    try:
        from .modules import MODULES
        block = MODULES["factory"].slot_block(params, crew.w)
    except Exception:  # noqa: BLE001
        return ""
    if block is None:
        return ""
    z = crew.zones
    seg = z.segment(block.n)
    active = z.active_blocks(block.n, extra=[block])
    note = lay_bus(crew, block.n, seg.inputs_of(active), extra=[block])
    h = head_supply(crew, block.n)
    return f"bus first: {note}" + (f"; {h}" if h else "") + "; "
