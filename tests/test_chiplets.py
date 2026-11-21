"""Chiplets designed for the ground they get: power station rotations, burner mines facing any way."""

import json

import pytest

from arena import catalog

pytestmark = pytest.mark.skipif(catalog.get().empty, reason="needs config/catalog.json (arena catalog)")


class DummyRun:
    budgets: dict = {}

    def act(self, action):
        raise AssertionError("design only: no game actions")


def world(water=(), ore=None, box=(-40, -40, 60, 60)):
    from brain.knowledge import Knowledge
    from brain.world import FreeWorld
    w = FreeWorld(DummyRun(), Knowledge(json.loads(json.dumps(catalog.get().data))), log=lambda *a: None)
    x0, y0, x1, y1 = box
    w.seen = {(x, y) for x in range(x0, x1) for y in range(y0, y1)}
    w.water = set(water)
    w.resources = ore or {}
    return w


def test_power_station_rotates_to_fit_a_narrow_strip():
    from brain.modules import SteamPower
    # land only in a strip 7 tiles tall between two lakes: a vertical boiler+engine stack (13 tiles) cannot fit
    water = {(x, y) for x in range(-40, 60) for y in range(-40, 60) if not 0 <= y <= 6}
    w = world(water)
    lay = SteamPower().find_layout(w, (10, -2), (10, 2), engines=2)
    assert lay is not None and lay["engines"] == 2
    boiler = next(p for p in lay["parts"] if p[3] == "boiler")
    assert boiler[2] in ("east", "west")                           # turned sideways to fit
    assert not lay["tiles"] & w.water and all(0 <= t[1] <= 6 for t in lay["tiles"])
    assert lay["path"] and not set(lay["path"]) & w.water


def test_power_station_refuses_without_water():
    from brain.modules import SteamPower
    assert SteamPower().find_layout(world(), (10, 10), (0, 0), engines=1) is None


def test_burner_mine_spots_face_any_way_on_a_ragged_patch():
    from brain.modules import BurnerMine
    ore = {(x, y) for x in range(0, 4) for y in range(0, 12)} | {(x, y) for x in range(4, 12) for y in range(0, 4)}
    w = world(ore={"iron-ore": ore})
    spots = BurnerMine()._spots(w, "iron-ore", 8, (20, 20), "furnace")
    assert len(spots) >= 5 and len({face for *_, face in spots}) >= 2
    for cx, cy, face in spots:
        assert {(cx - 1, cy - 1), (cx, cy - 1), (cx - 1, cy), (cx, cy)} <= ore


def test_factory_chiplet_for_a_mall_item():
    from brain.modules import Factory
    from brain.knowledge import Knowledge
    kn = Knowledge(json.loads(json.dumps(catalog.get().data)))
    items = Factory().can_make(kn)
    assert {"electric-mining-drill", "assembling-machine-1", "logistic-science-pack", "lab"} <= set(items)
    assert "steel-plate" not in items and "small-electric-pole" not in items     # need smelting steel / wood


def test_power_station_skips_a_shore_tile_the_game_refused():
    from brain.modules import SteamPower
    water = {(x, y) for x in range(-40, 60) for y in range(-40, 60) if y < 0}   # a straight shore along y = 0
    w = world(water)
    first = SteamPower().find_layout(w, (10, -2), (10, 10), engines=1)
    w.bad_shore.add(first["land"])                              # "no direction fits on this tile"
    second = SteamPower().find_layout(w, (10, -2), (10, 10), engines=1)
    assert second and second["land"] != first["land"]
    w.pump_fits[(30, 0)] = {"north"}                            # the game said this one fits facing north
    third = SteamPower().find_layout(w, (10, -2), (10, 10), engines=1)
    assert third["land"] == (30, 0) and third["dir"] == "north"


def test_resource_names_from_a_planner_never_send_the_crew_exploring():
    w = world()
    kn = w.kn
    assert kn.resource_name("iron") == "iron-ore" and kn.resource_name("Copper Ore") == "copper-ore"
    assert kn.resource_name("coal") == "coal" and kn.resource_name("oil") == "crude-oil"
    assert kn.resource_name("unobtainium") is None
    walked = []
    w.go = lambda x, y: walked.append((x, y))
    assert w.explore("iron") is False and walked == []            # refused on the spot: no 450-tile walk


def test_second_power_station_taps_the_first_ones_water_line():
    from brain.modules import Base, Site, SteamPower
    water = {(x, y) for x in range(-40, 60) for y in range(-40, 60) if y < 0}   # straight shore along y = 0
    w = world(water)
    sp = SteamPower()
    first = sp.find_layout(w, (10, -2), (10, 10), engines=1)
    ents = [("offshore-pump", first["land"][0] + 0.5, first["land"][1] + 0.5, first["dir"])]
    ents += [("pipe", t[0] + 0.5, t[1] + 0.5, "north") for t in first["path"]]
    bx, by = first["centre"]
    ents += [(name, bx + dx, by + dy, f) for dx, dy, f, name in first["parts"]]
    base = Base()
    base.add(Site("steam_power", {"engines": 1}, (bx, by), entities=ents, powered=True))
    for name, x, y, d in ents:                                   # the first station stands
        w.used |= w.footprint(name, x, y, d)
    lay = sp.find_tap_layout(w, base, (10, 10), engines=1)
    assert lay is not None and lay["tap_site"] == 1
    pipes, machines = sp.fluid_tiles(w, base)
    start = lay["path"][0]
    assert any(abs(start[0] - p[0]) + abs(start[1] - p[1]) == 1 for p in pipes)   # joins the old line
    near = {(x + dx, y + dy) for x, y in machines for dx, dy in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1))}
    assert not set(lay["path"]) & near                           # never beside the old boiler or engine
    assert not lay["tiles"] & w.water and not lay["tiles"] & w.used


class PocketRun:
    """The character squeezed into a gap between its own drills and chests (run 20261118-163803: two hours stuck).
    Every move fails with no_path until enough of the buildings around it are picked up."""
    budgets: dict = {}

    def __init__(self, walls, free_after=2):
        self.walls = dict(walls)                 # (x, y) -> entity name
        self.taken, self.placed, self.out = 0, [], False
        self.free_after = free_after
        self.pos = (1.5, -34.05)

    def move(self, x, y):
        if not self.out and self.taken < self.free_after:
            return {"ok": False, "code": "no_path", "data": {}}
        self.out, self.pos = True, (x, y)
        return {"ok": True, "data": {"position": {"x": x, "y": y}}}

    def observe(self, radius, **opts):
        ents = [{"name": n, "x": x, "y": y, "direction": "north",
                 "inventories": {"fuel": {"coal": 5}} if n == "burner-mining-drill" else {}}
                for (x, y), n in self.walls.items()]
        return {"ok": True, "data": {"position": {"x": self.pos[0], "y": self.pos[1]}, "entities": ents,
                                     "inventory": {"coal": 20}}}

    def place(self, entity, x, y, direction="north"):
        return self.act({"type": "place", "entity": entity, "x": x, "y": y, "direction": direction})

    def act(self, a):
        if a["type"] == "pickup":
            n = self.walls.pop((a["x"], a["y"]), None)
            if n is None:
                return {"ok": False, "code": "nothing_there", "data": {}}
            self.taken += 1
            got = {n: 1, **({"coal": 5} if n == "burner-mining-drill" else {})}
            return {"ok": True, "data": {"picked_up": got}}
        if a["type"] == "place":
            self.placed.append((a["entity"], a["x"], a["y"]))
            return {"ok": True, "data": {"entity": {"name": a["entity"], "x": a["x"], "y": a["y"]}}}
        if a["type"] == "insert":
            return {"ok": True, "data": {"inserted": a["count"]}}
        raise AssertionError(a)


TRAP = {(2.0, -33.0): "burner-mining-drill", (1.0, -35.0): "burner-mining-drill",
        (2.5, -35.5): "iron-chest", (3.5, -33.5): "iron-chest"}


def _trap_world(run=None):
    w = world()
    if run is not None:
        w.run = run
    for (x, y), n in TRAP.items():
        w.used |= w.footprint(n, x, y)
    w.pos = (1.5, -34.05)
    return w


def test_never_walks_onto_a_buildings_tile():
    w = _trap_world()
    x, y = w.stand_spot(2.0, -33.55)                 # the move that put the character in the crack
    t = (int(x // 1), int(y // 1))
    assert t not in w.used
    assert sum((t[0] + dx, t[1] + dy) not in w.used for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))) >= 2
    assert w.stand_spot(10.5, -20.5) == (10.5, -20.5)   # open ground: the spot itself


def test_a_building_that_would_wall_the_character_in_is_seen_first():
    w = _trap_world()
    drill = w.footprint("burner-mining-drill", 4.0, -35.0)   # the drill that closed the pocket
    assert not w.sealed()
    assert w.sealed(drill)


def test_walled_in_character_digs_out_and_puts_the_buildings_back():
    run = PocketRun(TRAP)
    w = _trap_world(run)
    w.go(20.5, -20.5)
    assert run.out and w.pos == (20.5, -20.5) and w.walk_fails == 0
    assert w.unstucks == 1 and run.taken == 2
    # the building the map chose as the door stays out (the way stays open); the one dug by hand went back
    assert len(run.placed) == 1
    assert {(x, y) for _, x, y in run.placed} <= set(TRAP)


def test_out_of_reach_says_the_walk_failed():
    w = _trap_world()
    w.walk_fails, w.walk_fail = 4, "could not walk to (2, -13): no_path"
    assert "no_path" in w.why("place iron-chest at (2.5, -12.5) north: out_of_reach")
    assert w.why("craft 1 x: missing") == "craft 1 x: missing"
