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
ENABLED = False

Tile = tuple[int, int]
YARD_MIN_HALF = 36        # the start yard reaches at least this far from the base centre each way
YARD_MAX_HALF = 90        # ... and at most this far
YARD_MARGIN = 10          # beyond the outermost opening building in it
YARD_REACH = 90           # buildings further than this from the centre are not the opening's (outposts, far mines)
SITE_MARGIN = 2           # reserved land keeps this far from any building of ours, its lanes and keepouts
HEAD = 26                 # spine tiles from its start kept for the head terminals (plates come in there by trunk)
HEAD_GAP = 4              # head terminals are this far apart (two bus taps side by side need four)
SPARE_SLOTS = 2           # make slots reserved beyond the era's blocks: a second copy, or a slot a design fits better
ANCHOR_SHIFTS = (0, -24, 24, -48, 48)    # where along a yard side the spine may start
ANCHOR_GAPS = (1, 24, 48)                # how far out from the yard
KEEP = 2                  # quick layouts finished with the oil corridor (the best by score)
RETRY_MIN = 20.0          # nothing fitted (too little land seen yet): try again after this many game minutes
# era segments: the targets each is sized for (items a second; each later segment keeps the earlier ones)
SEGMENTS = {1: {"automation-science-pack": 30 / 60, "logistic-science-pack": 30 / 60}}
SEGMENT_ZONES = ("make", "science")       # smelting stays with the brain's fields; the spine head gets their lines
OPEN_RECIPES = {1: {"underground-belt"}}  # a segment opens only once these are unlocked (and see ready())


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


def segment_specs(n: int, kit) -> list:
    """The blocks of segment n that go into spine slots (make and labs), one entry per copy, feeders first."""
    _, specs = era_blocks(n, kit)
    out = [s.at(max(1, s.copies)) for s in specs if s.zone in SEGMENT_ZONES and s.designable
           for _ in range(max(1, s.copies))]
    out.sort(key=lambda s: (s.depth, s.id))
    return out


def slot_demand(n: int, kit) -> dict[str, int]:
    """Spine slots segment n needs, by zone (the silo spot is kept from segment 1 on)."""
    specs = segment_specs(n, kit)
    return {"make": sum(1 for s in specs if s.zone == "make"), "reserve": SPARE_SLOTS,
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
    anchor, then the best KEEP finished with the oil corridor. None when no spine fits anywhere."""
    scan_p = planning_scan(scan, blocked | rect_tiles(yard))
    base_led = _base_ledger(scan_p)
    quick = []
    for anchor, heading in _anchors(yard):
        fp = _layout(scan_p, base_led, anchor, heading, want, oil=False)
        if fp is not None:
            quick.append((_score(fp, want, centre), anchor, heading))
    quick.sort(key=lambda q: -q[0])
    best = None
    for _, anchor, heading in quick[:KEEP]:
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
                "want": slot_demand(1, kit_for(world))}

    def plan(self, world=None, base=None, log=None, snap: dict | None = None):
        """The start yard and the floorplan from a snapshot (no game actions): (centre, yard, floorplan or None).
        With `snap` it touches nothing live, so it may run on a worker thread; apply() puts it into effect."""
        snap = snap or self.snapshot(world, base)
        fp = plan_floor(snap["scan"], snap["yard"], snap["blocked"], snap["want"], snap["centre"], log=log)
        return snap["centre"], snap["yard"], fp

    def apply(self, centre, yard, fp, log=None) -> int:
        """Keep the floorplan's corridors and slots and reserve their tiles. Returns how many tiles."""
        self.centre, self.yard = tuple(centre), tuple(yard)
        if fp is None or "spine" not in fp.corridors:
            self.warnings.append("no spine fits out of the start yard: nothing reserved")
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
        self.slot_only = self.block - self.belt_ok
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
        self.slot_only = self.block - self.belt_ok

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

    def ready(self, n: int, crew) -> tuple[bool, str]:
        """May segment n open? Segment 1: reservations made, underground belts unlocked, electric mining and a
        steam station built."""
        if not ENABLED:
            return False, "zones are off"
        if not self.reserved:
            return False, "nothing reserved yet"
        if n not in SEGMENTS:
            return False, f"segment {n} is not planned yet (hybrid-plan step 6)"
        if n > 1 and not self.is_open(n - 1):
            return False, f"segment {n - 1} first"
        kn = getattr(crew, "kn", None)
        unlocked = set(getattr(kn, "unlocked", ()) or ())
        if not OPEN_RECIPES.get(n, set()) <= unlocked:
            return False, f"needs {sorted(OPEN_RECIPES[n] - unlocked)} unlocked"
        base = getattr(crew, "base", None)
        kinds = {s.kind for s in getattr(base, "sites", [])}
        if not kinds & {"electric_mine", "outpost"}:
            return False, "no electric mining yet"
        if "steam_power" not in kinds:
            return False, "no steam station yet"
        return True, ""

    def segment(self, n: int, world=None, kit=None, scan=None) -> Segment:
        """Segment n laid out: every block copy in a slot (make blocks in make slots, labs in science slots, the
        reserve slots when a design does not fit its own), designed with the strip designer, ports on the spine;
        then the bus for these terminals only (head terminals for the plates the brain's lines bring in). Pure
        computation on a snapshot; cached until the next reserve()."""
        if kit is None:
            kit = (self.opened.get(n) or {}).get("kit") or kit_for(world or self.__dict__.get("_world"))
        key = (n, kit)
        if key in self._seg:
            return self._seg[key]
        seg = design_segment(self, n, kit, self.floorplan(world, scan))
        self._seg[key] = seg
        return seg

    def open_segment(self, n: int, world=None, kit=None, minute: float = 0.0, log=None) -> Segment:
        seg = self.segment(n, world, kit)
        self.opened.setdefault(n, {"built": {}, "minute": minute, "kit": seg.kit})
        if log:
            log(f"[zones] segment {n} open: {len(seg.blocks)} blocks "
                f"({', '.join(b.key for b in seg.blocks)}), {len(seg.problems)} problem(s)"
                + (f": {seg.problems[0]}" if seg.problems else ""))
        return seg

    def slot_block(self, item: str, world=None, kit=None) -> SegBlock | None:
        """The open segment's first block of this item not built yet, with a good design (None: none, so the
        factory goes free-form)."""
        if not ENABLED:
            return None
        for n in sorted(self.opened):
            seg = self.segment(n, world, kit)
            built = self.opened[n].get("built", {})
            for b in seg.blocks:
                if b.product == item and b.key not in built and b.ok:
                    return b
        return None

    def mark_built(self, block: SegBlock, where=None) -> None:
        st = self.opened.get(block.n)
        if st is not None:
            st.setdefault("built", {})[block.key] = tuple(where) if where else True

    def summary(self) -> dict:
        return {"reserved_tiles": len(self.block), "yard": self.yard,
                "spine_cells": len(self.corridors["spine"].cells) if "spine" in self.corridors else 0,
                "slots": {z: sum(1 for s in self.slots if s.zone == z) for z in sorted({s.zone for s in self.slots})},
                "oil_corridor": "oil" in self.corridors, "segments_open": sorted(self.opened),
                "warnings": self.warnings[-3:]}


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


def design_segment(z: Zones, n: int, kit, fp) -> Segment:
    from atlas.design.memory import Memory
    from atlas.layout.bus import Terminal, design_bus
    from atlas.sim.base import world_check, _reach
    from atlas.sim.blocks import place
    from atlas.design.model import E
    seg = Segment(n, dict(SEGMENTS.get(n, {})), kit)
    if "spine" not in fp.corridors:
        seg.problems.append("no spine reserved")
        return seg
    specs = segment_specs(n, kit)
    mem = Memory()
    by_zone = {zone: sorted((s for s in fp.slots if s.zone == zone and s.corridor == "spine"), key=lambda s: s.order)
               for zone in ("make", "science", "reserve")}
    spare = list(by_zone["reserve"])
    used: set[str] = set()
    counts: dict[str, int] = {}
    for spec in specs:
        copy = counts.get(spec.id, 0)
        counts[spec.id] = copy + 1
        key = f"{spec.id}#{copy}"
        own = [s for s in by_zone.get(spec.zone, []) if s.id not in used]
        tries = own[:1] + [s for s in spare if s.id not in used]
        best = None
        for slot in tries:
            wd, errs = place(fp, spec, slot, mem, kit)
            if wd is not None and not errs:
                best = SegBlock(key, spec, slot, wd, n=n)
                break
            if best is None:
                best = SegBlock(key, spec, slot, None, problems=errs or [f"{key}: no design in {slot.id}"], n=n)
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
    for b in seg.blocks:
        if not b.ok:
            continue
        for p, tile in b.wd.port_tiles():
            for item in p.items:
                b.ports.append({"io": p.io, "item": item, "tile": tile, "x": p.x, "rate": p.rate.get(item, 0.0)})
                terms.append(Terminal(item, "src" if p.io == "out" else "sink", tile, p.rate.get(item, 0.0),
                                      owner=f"{b.key}@{b.slot.id}"))
    if not any(t.io == "sink" for t in terms):
        return seg
    bus = design_bus(fp, terms, kit)
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
    return seg


# ==================================================================== a slot block as what Factory.build places
@dataclass
class SlotBuild:
    """An Atlas block design in its slot, ready for Factory.build: steps in walking order, the output's stand-in
    chest on the spine edge (until the bus takes the product, phase B's stub), and the brain's input ports."""
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


def slot_build(block: SegBlock) -> SlotBuild:
    """Convert a placed Atlas block into a SlotBuild: compile its entities to API steps (inserters face their
    pickup side, undergrounds carry their belt type), add an inserter and a chest past each out port (the
    product collects there until the bus carries it), and turn each in port into a brain feed port whose last
    link belt stands on the spine edge tile and faces into the block."""
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
        if p.io == "out":
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
        if z.reserved and not z.is_open(1):
            ok, _ = z.ready(1, crew)
            if ok:
                kit = kit_for(w)
                scan = scan_from_world(w, z.centre)
                think = getattr(w, "think", None)
                if think is not None:
                    think("laying out segment 1 (slots, blocks and bus)", z.segment, 1, None, kit, scan)
                z.open_segment(1, w, kit, minute=crew._minute() if hasattr(crew, "_minute") else 0.0, log=crew.log)
                note = (note + "; " if note else "") + "zones: segment 1 open"
        return note
    except Exception as exc:  # noqa: BLE001 - never let the planner break a build
        try:
            crew.log(f"[zones] skipped: {type(exc).__name__}: {str(exc)[:160]}")
        except Exception:  # noqa: BLE001
            pass
        return ""


def survey(world, base, half: int = 160, step: int = 64) -> None:
    """Remote looks (no walking) over the land round the base, so the floorplan has ground to plan on. Points the
    level's observe range does not reach are skipped."""
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
            around = [(math.floor(x) + ox, math.floor(y) + oy) for ox in (-20, 0, 20) for oy in (-20, 0, 20)]
            if all(t in world.seen for t in around):
                continue
            try:
                look(32, x=x, y=y, include=["entities", "resources", "obstacles", "water"])
            except Exception:  # noqa: BLE001 - a refused look: plan on what is known
                continue
