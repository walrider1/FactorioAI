"""Steam stations that feed the grid (run 20261118-233202: every second engine unplugged, and stations built out of
wire reach of the grid counted as powered, so `connect` answered "already powered" and nothing was ever wired)."""

import json
import math
import time
from types import SimpleNamespace

import pytest

from arena import catalog
from brain.crew import Crew
from brain.modules import UNPLUGGED, Base, Site, SteamPower

pytestmark = pytest.mark.skipif(catalog.get().empty, reason="needs config/catalog.json (arena catalog)")


def _world(water=()):
    from brain.knowledge import Knowledge
    from brain.world import FreeWorld

    class NoRun:
        budgets: dict = {}

        def act(self, action):
            raise AssertionError("design only")

    w = FreeWorld(NoRun(), Knowledge(json.loads(json.dumps(catalog.get().data))), log=lambda *a: None)
    w.seen = {(x, y) for x in range(-40, 60) for y in range(-40, 60)}
    w.water = set(water)
    return w


def _supplied(w, pole, engine) -> bool:
    """Does a small pole at this centre reach any tile of the engine (supply area 2.5 each way)?"""
    px, py = pole
    area = {(x, y) for x in range(math.floor(px - 2.5), math.ceil(px + 2.5))
            for y in range(math.floor(py - 2.5), math.ceil(py + 2.5))}
    return bool(area & w.footprint("steam-engine", *engine))


@pytest.mark.parametrize("r", range(4))
def test_every_engine_of_a_two_engine_station_has_a_pole_in_reach(r):
    w = _world()
    sp = SteamPower()
    parts = [sp._rot(dx, dy, f, r) + (name,) for name, dx, dy, f in sp.station_parts(2)]
    bx, by = (10.5, 10.0) if r % 2 == 0 else (10.0, 10.5)
    poles = [(bx + dx, by + dy) for dx, dy, f, name in parts if name == "small-electric-pole"]
    engines = [(bx + dx, by + dy, f) for dx, dy, f, name in parts if name == "steam-engine"]
    assert len(engines) == 2 and len(poles) == 2
    for e in engines:
        assert any(_supplied(w, p, e) for p in poles), f"engine at {e} has no pole in reach"
    assert math.dist(*poles) <= 7.5                              # the game wires the two poles together


def test_layout_for_two_engines_places_both_poles_on_free_ground():
    water = {(x, y) for x in range(-40, 60) for y in range(-40, 60) if y < 0}   # a straight shore along y = 0
    w = _world(water)
    lay = SteamPower().find_layout(w, (10, -2), (10, 10), engines=2)
    assert lay and lay["engines"] == 2
    assert sum(1 for p in lay["parts"] if p[3] == "small-electric-pole") == 2
    assert not lay["tiles"] & w.water


def test_a_station_not_yet_wired_in_is_no_anchor_for_other_sites():
    base = Base()
    base.add(Site("steam_power", {"engines": 1}, (0, 0), poles=[(2, -4)], powered=True))
    base.power_pole = (2, -4)
    island = base.add(Site("steam_power", {"engines": 2}, (60, 0), poles=[(62, -4), (62, -9)], powered=False))
    assert base.powered_poles() == [(2, -4)]
    island.powered = True
    assert (62, -4) in base.powered_poles()


class GridCrew:
    """Just the parts of the crew the power checks use, with the game's answers set by the test."""
    station_on_grid = Crew.station_on_grid
    check_power = Crew.check_power
    _connect = Crew._connect
    power_status = Crew.power_status
    measured_power = Crew.measured_power
    _draw_kw = Crew._draw_kw
    INSERTER_DUTY, UTIL_FLOOR = Crew.INSERTER_DUTY, Crew.UTIL_FLOOR
    on_grid = Crew.on_grid
    short_note = Crew.short_note
    _off_grid_anchors = Crew._off_grid_anchors
    _cover_unpowered = Crew._cover_unpowered
    _pole_freer, line_heads, lines = Crew._pole_freer, Crew.line_heads, Crew.lines

    def __init__(self, base, statuses, networks):
        self.base, self.statuses, self.networks = base, statuses, networks
        self.placed = []
        self.w = SimpleNamespace(seen={(x, y) for x in range(-80, 80) for y in range(-40, 40)},
                                 blocked=lambda t: False, keepout=set(), inventory={"small-electric-pole": 50},
                                 speed=1000.0, doing=lambda *a: None, look=lambda *a, **k: {}, recorder=None,
                                 place=lambda name, x, y, d, tiles: self.placed.append((x, y)))
        self.kn = SimpleNamespace(entities={})
        self.foreman = SimpleNamespace(note=lambda *a: None)

    def look(self, site, names):
        return {(e[1], e[2]): self.statuses.get((e[1], e[2]), "working") for e in site.entities if e[0] in names}

    def pole_networks(self, tiles):
        return {tuple(t): self.networks.get(tuple(t)) for t in tiles}

    def _plug_generators(self, site):
        return 0

    def existing_poles(self):
        return {tuple(q) for s in self.base.sites for q in s.poles}

    def unlock_items(self, items):
        pass

    def make(self, items):
        pass


def _two_stations():
    base = Base()
    grid = base.add(Site("steam_power", {"engines": 1}, (0, 0), poles=[(2, -4)], powered=True,
                         entities=[("steam-engine", 0.5, -3.5, "north"), ("small-electric-pole", 2.5, -3.5, "north")]))
    base.power_pole = (2, -4)
    new = base.add(Site("steam_power", {"engines": 2}, (40, 0), poles=[(42, -4), (42, -9)], powered=False,
                        entities=[("steam-engine", 40.5, -3.5, "north"), ("steam-engine", 40.5, -8.5, "north")]))
    return base, grid, new


def test_station_on_its_own_network_is_not_on_the_grid():
    base, grid, new = _two_stations()
    crew = GridCrew(base, {}, {(2, -4): 1, (42, -4): 7, (42, -9): 7})
    assert crew.station_on_grid(grid)                            # the grid is the grid
    assert not crew.station_on_grid(new)                         # plugged in, but an island
    crew.networks.update({(42, -4): 1, (42, -9): 1})
    assert crew.station_on_grid(new)


def test_an_unplugged_engine_means_the_station_is_not_feeding_the_grid():
    base, grid, new = _two_stations()
    crew = GridCrew(base, {(40.5, -8.5): UNPLUGGED}, {(2, -4): 1, (42, -4): 1, (42, -9): 1})
    assert not crew.station_on_grid(new)


def test_connect_wires_a_new_station_to_the_grid_instead_of_calling_it_powered():
    base, grid, new = _two_stations()
    crew = GridCrew(base, {}, {(2, -4): 1, (42, -4): 7, (42, -9): 7})
    real_place = crew.w.place

    def place(name, x, y, d, tiles):                             # once the line is up, the game merges the networks
        real_place(name, x, y, d, tiles)
        crew.networks.update({(42, -4): 1, (42, -9): 1})
    crew.w.place = place
    msg = crew._connect(new)
    assert crew.placed and "new pole" in msg and new.powered is True


def test_connect_fails_loudly_when_the_station_stays_an_island():
    from brain.world import BuildError
    base, grid, new = _two_stations()
    crew = GridCrew(base, {}, {(2, -4): 1, (42, -4): 7, (42, -9): 7})
    with pytest.raises(BuildError, match="not on the grid"):
        crew._connect(new)
    assert new.powered is False


def test_capacity_counts_only_engines_that_feed_the_grid():
    base, grid, new = _two_stations()
    crew = GridCrew(base, {}, {})
    assert crew.power_status()["capacity_kw"] == 900             # the island adds nothing
    new.powered = True
    new.health = {UNPLUGGED: 1, "working": 2}
    assert crew.power_status()["capacity_kw"] == 1800            # one of its two engines is unplugged
