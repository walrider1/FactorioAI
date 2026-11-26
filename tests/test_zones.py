"""The hybrid floorplan (bots/brain/zones.py, docs/hybrid-plan.md steps 4-5): the start yard stays organic, the
floorplan only reserves land outside it, world.free refuses reserved tiles, and segment 1 (red + green 30/min)
places every block in a slot with its ports on the spine, DRC and bus clean.

Offline on real maps: the Atlas fixture (factorio-123) always; all 22 generated maps (runs/atlas/maps of the main
checkout) with ZONES_ALL_MAPS=1 (about 2 minutes)."""

import os
import pickle
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "bots" / "atlas" / "tests" / "fixtures" / "atlas" / "factorio-123.json"
pytestmark = pytest.mark.skipif(not FIXTURE.exists(), reason="needs the Atlas submodule (git submodule update --init)")


def _maps_dir() -> Path | None:
    here = ROOT / "runs" / "atlas" / "maps"
    if here.exists():
        return here
    try:                                   # a git worktree: the maps live in the main checkout's runs/
        common = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=ROOT, capture_output=True, text=True,
                                timeout=10).stdout.strip()
        main = (ROOT / common).resolve().parent / "runs" / "atlas" / "maps"
        return main if main.exists() else None
    except Exception:  # noqa: BLE001
        return None


def _map_paths() -> list[Path]:
    if os.environ.get("ZONES_ALL_MAPS") == "1" and (d := _maps_dir()) is not None:
        return sorted(d.glob("*.json"))
    return [FIXTURE]


class DummyRun:
    budgets: dict = {}

    def act(self, action):
        raise AssertionError("planning only: no game actions")


KN = SimpleNamespace(researched={"automation", "logistics", "electronics", "logistic-science-pack"},
                     unlocked={"underground-belt", "long-handed-inserter", "transport-belt"})


def brain_world(scan, sites: bool = True):
    """A FreeWorld that has seen the whole map scan, with an opening in its start area and one mine outside it."""
    from brain.modules import Base, Site
    from brain.world import FreeWorld
    w = FreeWorld(DummyRun(), KN, log=lambda *a: None)
    w.seen, w.water, w.cliffs = set(scan.seen), set(scan.water), set(scan.cliffs)
    res: dict = {}
    for t, n in scan.ore.items():
        res.setdefault(n, set()).add(t)
    w.resources = res
    w.patches = [{"name": p.name, "tiles": p.tiles, "amount": p.amount, "box": list(p.box),
                  "centre": {"x": p.centre[0], "y": p.centre[1]}} for p in scan.patches]
    cx, cy = int(scan.spawn[0]), int(scan.spawn[1])
    w.pos = (cx, cy)
    base = Base()
    if sites:
        # the opening near spawn: a steam station and a few furnaces
        for k, (x, y) in enumerate(((cx - 12, cy - 6), (cx + 4, cy + 8), (cx - 2, cy - 20))):
            tiles = {(x + i, y + j) for i in range(6) for j in range(4)}
            w.used |= tiles
            s = base.add(Site("steam_power" if k == 0 else "burner_mine", {}, (x + 3, y + 2)))
            s.entities = [("stone-furnace", x + 1, y + 1, "north")]
        # an outpost far from spawn, on open ground: its land is never reserved either
        far = _open_spot(w, (cx + 150, cy), (cx - 150, cy), (cx, cy + 150), (cx, cy - 150))
        if far is not None:
            tiles = {(far[0] + i, far[1] + j) for i in range(8) for j in range(8)}
            w.used |= tiles
            base.add(Site("electric_mine", {}, (far[0] + 4, far[1] + 4)))
    return w, base


def _open_spot(w, *cands):
    for x, y in cands:
        box = {(x + i, y + j) for i in range(8) for j in range(8)}
        if box <= w.seen and not box & w.water and not box & w.cliffs:
            return (x, y)
    return None


_CACHE: dict = {}


def planned(path: Path):
    """(scan, world, base, zones, segment 1) for a map, computed once per test session."""
    if path not in _CACHE:
        from atlas.world.scan import MapScan
        from brain import zones
        scan = MapScan.load(path)
        w, base = brain_world(scan)
        z = zones.Zones()
        zones.attach(SimpleNamespace(w=w), z)
        z.reserve(w, base)
        seg = z.open_segment(1, w) if z.reserved else None
        _CACHE[path] = (scan, w, base, z, seg)
    return _CACHE[path]


@pytest.fixture(autouse=True)
def zones_on():
    from brain import zones
    old = zones.ENABLED
    zones.ENABLED = True
    yield
    zones.ENABLED = old


# ------------------------------------------------------------------ A: reservations
@pytest.mark.parametrize("path", _map_paths(), ids=lambda p: p.stem)
def test_reservations_stay_out_of_the_start_yard_and_off_our_buildings(path):
    from brain import zones
    scan, w, base, z, _ = planned(path)
    assert z.reserved, z.warnings
    yard = zones.rect_tiles(z.yard)
    assert not z.block & yard                                    # the start yard stays organic
    for s in base.sites:                                         # every opening building sits inside the yard
        if s.kind != "electric_mine":
            assert z.in_yard(s.centre)
    near_ours = zones._dilate(w.used, zones.SITE_MARGIN)
    assert not z.block & near_ours                               # never on or next to a building of ours
    assert not z.block & set(scan.ore)                           # never on ore: the brain mines where it likes
    assert all(scan.buildable(t) for t in z.block)               # never on water, cliffs or unseen land
    assert "spine" in z.corridors and z.zone_counts()["silo"] == 1
    assert z.belt_ok <= z.block and z.slot_only == z.block - z.belt_ok
    # the floorplan passes Atlas's independent check (overlaps, terrain, slots on their corridor)
    from atlas.sim.check import check
    errs, _ = check(z.floorplan(w))
    assert not errs, errs[:3]


def test_world_free_refuses_reserved_tiles_for_buildings_and_slots_for_belts():
    from brain import zones
    scan, w, base, z, _ = planned(FIXTURE)
    corridor = sorted(z.belt_ok)[len(z.belt_ok) // 2]
    slot = sorted(z.slot_only)[len(z.slot_only) // 2]
    assert not w.free({corridor}) and not w.free([slot])
    assert not w.free(t for t in (corridor,))                     # a generator is read once, still refused
    assert w.free_belt(corridor)                                  # belts may cross a corridor
    assert not w.free_belt(slot)                                  # never a reserved slot
    yard_tile = next(t for t in sorted(zones.rect_tiles(z.yard)) if w.free({t}))
    assert w.free({yard_tile})                                    # the yard is the brain's, as before
    # fab's design grid and the workshop floor keep off reserved land too
    from fab.grid import Grid
    g = Grid.from_world(w)
    assert corridor in g.keepout and slot in g.keepout and not g.free(slot)
    from brain.workshop import Workshop
    assert not Workshop()._area_ok(w, [slot])
    # the switch: zones off, everything is free again
    zones.ENABLED = False
    assert w.free({corridor}) and w.free_belt(slot) and not Grid.from_world(w).keepout & {corridor}


def test_free_is_unchanged_without_zones():
    from atlas.world.scan import MapScan
    scan = MapScan.load(FIXTURE)
    w, _ = brain_world(scan, sites=False)
    t = (int(scan.spawn[0]) + 3, int(scan.spawn[1]) + 3)
    assert w.zones is None and w.free({t}) == w._free_ground({t}, True)


def test_reserve_on_a_tiny_world_reserves_nothing_and_never_raises():
    from brain import zones
    from brain.modules import Base, Site
    from brain.world import FreeWorld
    w = FreeWorld(DummyRun(), KN, log=lambda *a: None)
    w.seen = {(x, y) for x in range(-30, 30) for y in range(-30, 30)}
    base = Base()
    site = base.add(Site("steam_power", {}, (0, 0)))
    crew = SimpleNamespace(w=w, base=base, kn=KN, log=lambda *a: None, zones=None, _minute=lambda: 5.0)
    note = zones.after_build(crew, site)                       # the first electric build: tries to reserve
    assert crew.zones is not None and w.zones is crew.zones
    assert not crew.zones.reserved and "nothing reserved" in " ".join(crew.zones.warnings)
    assert w.free({(0, 0)}) and isinstance(note, str)
    assert zones.after_build(crew, site) == ""                 # tried once: not again at every build


def test_the_crew_hook_reserves_at_the_first_electric_build_then_opens_segment_one():
    from atlas.world.scan import MapScan
    from brain import zones
    from brain.modules import MODULES, Site
    scan = MapScan.load(FIXTURE)
    w, base = brain_world(scan)
    logs: list = []
    crew = SimpleNamespace(w=w, base=base, kn=KN, log=logs.append, zones=None, _minute=lambda: 12.0)
    old = MODULES["factory"].zones
    try:
        mine = next(s for s in base.sites if s.kind == "electric_mine")
        base.sites.remove(mine)                                  # no electric mining yet: reserve only
        note = zones.after_build(crew, base.sites[0])
        z = crew.zones
        assert z.reserved and "reserved" in note and not z.opened and MODULES["factory"].zones is z
        base.sites.append(mine)
        note = zones.after_build(crew, mine)                     # electric mining in: segment 1 opens
        assert z.is_open(1) and "segment 1 open" in note
        assert MODULES["factory"].slot_block({"item": "logistic-science-pack"}, w) is not None
    finally:
        MODULES["factory"].zones = old


def test_zones_ride_along_in_the_crew_save():
    from brain import persist, zones
    from brain.crew import Crew
    scan, w, base, z, seg = planned(FIXTURE)
    crew = Crew.__new__(Crew)
    crew.base, crew.links, crew.zones = base, [], z
    crew.foreman = SimpleNamespace(crew=crew)
    world = SimpleNamespace(zones=z, tick=1, _tick_wall=0.0, kn=SimpleNamespace(researched=set()))
    data, dropped = persist.capture(crew, world)
    assert not [d for d in dropped if "zones" in d]
    snap = pickle.loads(data)
    assert snap["crew"]["zones"]._fp is None and snap["crew"]["zones"]._seg == {}   # only plain state is saved
    fresh = Crew.__new__(Crew)
    fresh.foreman = SimpleNamespace(crew=fresh)
    w2 = SimpleNamespace(tick=0, _tick_wall=1.0, kn=SimpleNamespace(learn_research=lambda r: None))
    persist.restore(data, fresh, w2)
    z2 = fresh.zones
    assert w2.zones is z2                                        # still one object for crew and world
    assert z2.block == z.block and z2.yard == z.yard and z2.opened.keys() == z.opened.keys()
    # a resumed bot re-plans the segment from the saved floorplan and gets the same blocks in the same slots
    z2._world = w
    seg2 = z2.segment(1)
    assert [(b.key, b.slot.id) for b in seg2.blocks] == [(b.key, b.slot.id) for b in seg.blocks]
    assert len(pickle.dumps(z)) < 2_000_000


# ------------------------------------------------------------------ B: segment 1
@pytest.mark.parametrize("path", _map_paths(), ids=lambda p: p.stem)
def test_segment_one_puts_every_block_in_a_slot_with_ports_on_the_spine(path):
    from atlas.layout.bus import locate, runs_of
    from brain import zones
    scan, w, base, z, seg = planned(path)
    assert seg is not None and not seg.problems, seg.problems[:3]   # DRC, bus and lane check all clean
    want = {s.id for s in zones.segment_specs(1, seg.kit)}
    assert {b.spec.id for b in seg.blocks} == want == {
        "make-iron-gear-wheel", "make-electronic-circuit", "make-automation-science-pack",
        "make-logistic-science-pack", "labs"}
    sp = z.corridors["spine"]
    runs = runs_of(sp)
    yard = zones.rect_tiles(z.yard)
    taken: set = set()
    for b in seg.blocks:
        assert b.ok, b.problems
        tiles = b.wd.tiles()
        assert tiles <= b.slot.tiles and not tiles & yard and not tiles & zones._dilate(w.used, 1)
        assert not tiles & taken                                  # blocks never overlap
        taken |= b.slot.tiles
        for p in b.ports:                                          # every port meets the spine's tappable edge
            assert p["tile"] in sp.tiles and locate(runs, p["tile"], sp.width) is not None
        ins = [p for p in b.ports if p["io"] == "in"]
        assert len(b.taps) == len(ins)                             # each input is a bus tap
    for t in seg.head:                                             # the plates come in at the spine head
        assert t.tile in sp.tiles and t.run == 0
    assert {t.item for t in seg.head} >= {"iron-plate", "copper-plate"}
    for hp in seg.head_ports(sp):                                  # a trunk ends just outside the spine
        assert tuple(hp["end"]) not in sp.tiles and tuple(hp["end"]) not in yard
        assert tuple(hp["end"]) not in z.slot_only


def test_a_slot_block_converts_to_what_factory_build_places():
    from brain import zones
    scan, w, base, z, seg = planned(FIXTURE)
    for b in seg.blocks:
        sb = zones.slot_build(b)
        outs = [p for p in b.wd.design.ports if p.io == "out"]
        assert len(sb.steps) == len(b.wd.design.entities)
        assert len(sb.stub) == 2 * len(outs) and len(sb.chests) == len(outs)
        assert {s.name for s in sb.stub} <= {"inserter", "iron-chest"}
        kit = sb.kit()
        assert sum(kit.values()) == len(sb.steps) + len(sb.stub)
        assert sb.tiles() - b.slot.tiles <= {tuple(t) for s in sb.stub for t in s.tiles}
        for s in sb.steps:
            if s.kind in ("ug-in", "ug-out"):
                assert s.belt_type in ("input", "output")
            if s.name.startswith("assembling-machine"):
                assert s.recipe
        for pt in sb.ports:                                       # feed ports: the link's last belt on the edge
            assert pt["feed"] and tuple(pt["end"]) in z.corridors["spine"].tiles
            assert tuple(pt["end"]) in z.belt_ok and tuple(pt["approach"]) in z.belt_ok
        if b.spec.kind == "make":
            assert sb.recipes() and b.product in {r for r in sb.recipes()} | {b.product}


def test_factory_asks_the_zones_first_and_falls_back_free_form():
    from brain import zones
    from brain.modules import MODULES, SlotPlan
    scan, w, base, z, seg = planned(FIXTURE)
    fac = MODULES["factory"]
    old = fac.zones
    try:
        fac.zones = z
        p = {"item": "iron-gear-wheel", "per_minute": 20}
        blk = fac.slot_block(p, w)
        assert blk is not None and blk.product == "iron-gear-wheel"
        supplied, pl = fac.supplied_for(w.kn, p)
        assert supplied == {"iron-plate"} and isinstance(pl, SlotPlan) and pl.supplied["iron-plate"] > 0
        got = fac._slot_design(w, p)
        assert got is not None and isinstance(got[0], zones.SlotBuild)
        assert fac.slot_block({"item": "iron-gear-wheel", "plates": "mine"}, w) is None     # own mines: free-form
        assert fac.slot_block({"item": "stone-furnace"}, w) is None                        # not in the segment
        z.mark_built(blk, (0, 0))
        assert fac.slot_block(p, w) is None                        # its one block is built: the next goes free-form
        z.opened[1]["built"].pop(blk.key)
        zones.ENABLED = False
        assert fac.slot_block(p, w) is None and fac.zones.blocks(z.block) is False
    finally:
        fac.zones = old


def test_a_slot_build_places_every_building_and_keeps_the_rest_as_gaps():
    from brain import zones
    from brain.modules import MODULES, Base
    scan, w, base, z, seg = planned(FIXTURE)
    blk = next(b for b in seg.blocks if b.product == "electronic-circuit")
    sb = zones.slot_build(blk)
    one = next(s for s in sb.steps if not s.belt_type)
    placed, recipes, refused = [], [], {(one.name, one.x, one.y)}

    class GameRun:
        def place(self, name, x, y, direction="north", belt_type=None, **kw):
            placed.append((name, x, y, direction, belt_type))
            return {"ok": True, "data": {"entity": {"name": name, "x": x, "y": y}}}

        def set_recipe(self, x, y, recipe):
            recipes.append(recipe)
            return {"ok": True}

    class FakeWorld:
        run = GameRun()
        pos = (0.0, 0.0)
        movable: set = set()
        inventory: dict = {}

        def __init__(self):
            self.used, self.keepout, self.zones, self.logged = set(), set(), z, []

        def clear(self, tiles):
            pass

        def near(self, x, y, dist=8):
            pass

        def step_out(self, tiles):
            pass

        def place(self, name, x, y, direction="north", tiles=()):
            from brain.world import BuildError
            if (name, x, y) in refused:
                raise BuildError("cannot_place: a tree")
            placed.append((name, x, y, direction, None))
            self.used |= set(tiles)
            return {"name": name, "x": x, "y": y}

        def log(self, *a):
            self.logged.append(a)

    fw = FakeWorld()
    fac = MODULES["factory"]
    site = fac._build_slot(fw, Base(), {"item": "electronic-circuit", "per_minute": 36}, sb)
    assert len(placed) == len(sb.all_steps()) - 1                 # one refused: a gap, the rest built
    assert len(site.params.get("gaps", [])) == 1
    assert recipes and set(recipes) <= sb.recipes()
    assert site.chests and site.chests[0][2] == "electronic-circuit"
    assert {pt["item"] for pt in site.params["ports"]} == {"iron-plate", "copper-plate"}
    assert all(pt["feed"] for pt in site.params["ports"])
    assert any(b[4] == "input" for b in placed) or not any(s.belt_type for s in sb.steps)
    assert blk.key in z.opened[1]["built"]                        # the slot is taken: the next goal goes free-form
    z.opened[1]["built"].pop(blk.key, None)


def test_segment_opens_only_when_its_conditions_hold():
    from brain.modules import Base, Site
    scan, w, base, z, seg = planned(FIXTURE)
    b = Base()
    crew = SimpleNamespace(kn=KN, base=b)
    ok, why = z.ready(1, crew)
    assert not ok and "electric mining" in why
    b.add(Site("electric_mine", {}, (0, 0)))
    assert z.ready(1, crew)[1] == "no steam station yet"
    b.add(Site("steam_power", {}, (0, 0)))
    assert z.ready(1, crew) == (True, "")
    assert not z.ready(1, SimpleNamespace(kn=SimpleNamespace(unlocked=set()), base=b))[0]   # no undergrounds
    assert not z.ready(2, crew)[0]                                 # segment 2 is hybrid-plan step 6


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
