"""Fixes from run 20261119-011655 (793 game minutes, 2 steam stations, nothing after steel at minute 85): the
character standing on its own pipe line, a failed tap ending every power build, idle machines on a short grid
called unwired, an extract of 0 refused by the server, and an oil search that never went farther."""

import json
import math
from types import SimpleNamespace

import pytest

import automation_bot as ab
from brain.crew import Crew
from brain.modules import Base, Site, SteamPower
from brain.world import BuildError, FreeWorld


# ------------------------------------------------------------------ lines
class LineWorld:
    def __init__(self, blocked=(), pos=(48.5, -4.5)):
        self.used, self.pos, self.placed, self.walks = set(blocked), pos, [], []
        self.run = SimpleNamespace(place=self._place, place_line=self._line)

    def blocked(self, t):
        return t in self.used

    def clear(self, tiles):
        pass

    def go(self, x, y):
        self.walks.append((x, y))
        self.pos = (x, y)

    def _place(self, entity, x, y, d):
        return self._line(entity, x, y, x, y)

    def _line(self, entity, x0, y0, x1, y1):
        here = (math.floor(self.pos[0]), math.floor(self.pos[1]))
        line = {(math.floor(x0), y) for y in range(min(math.floor(y0), math.floor(y1)), max(math.floor(y0), math.floor(y1)) + 1)}
        if here in line:
            return {"ok": False, "code": "cannot_place", "message": f"tile 1 ({x0}, {y0}) is blocked"}
        self.placed.append((x0, y0, x1, y1))
        return {"ok": True}


def test_the_character_never_stands_on_the_pipe_it_lays():
    # the four spots beside the middle of the line taken (storage chests, a boiler): it used to stand on the line
    chunk = [(48, -4), (48, -5), (48, -6)]
    w = LineWorld(blocked={(45, -5), (51, -5), (44, -5), (52, -5)})
    ab.lay(w, "pipe", chunk)
    assert w.placed and (math.floor(w.pos[0]), math.floor(w.pos[1])) not in chunk
    assert all(math.dist(w.pos, ab.centre(t)) <= ab.LINE_REACH for t in chunk)


def test_a_tile_the_game_refuses_is_remembered_so_the_next_layout_avoids_it():
    w = LineWorld(pos=(40.5, -5.5))
    w.run.place_line = lambda *a: {"ok": False, "code": "cannot_place", "message": "tile 2 (48.5, -5.5) is blocked"}
    with pytest.raises(BuildError, match="remembered as blocked"):
        ab.lay(w, "pipe", [(48, -4), (48, -5), (48, -6)])
    assert (48, -6) in w.used


def test_no_stand_spot_is_a_clear_failure_not_a_pipe_under_the_character():
    chunk = [(0, 0), (0, 1)]
    w = LineWorld(blocked={(x, y) for x in range(-12, 13) for y in range(-12, 13)} - set(chunk), pos=(0.5, 0.5))
    with pytest.raises(BuildError, match="no free tile to stand on"):
        ab.lay(w, "pipe", chunk)


# ------------------------------------------------------------------ extract 0
def test_an_extract_of_nothing_is_never_sent():
    sent = []
    w = SimpleNamespace(near=lambda *a, **k: None, act=lambda a: sent.append(a) or {"ok": True, "data": {}},
                        inventory={})
    assert FreeWorld.extract(w, 1.5, 1.5, "coal", 0) == 0 and sent == []


# ------------------------------------------------------------------ tap fallback
def test_a_failed_tap_takes_its_pieces_back_and_builds_with_a_pump_of_its_own():
    sp = SteamPower()
    base = Base()
    base.add(Site("steam_power", {"engines": 1}, (0, 0), poles=[(2, -4)], powered=True))
    base.power_pole = (2, -4)
    picked, rec = [], []
    w = SimpleNamespace(water_hints=[(10, -2)], blocking=False, inventory={"coal": 0}, pos=(0, 0), recorder=rec,
                        doing=lambda *a: None, log=lambda *a: None, survey_area=lambda *a, **k: None,
                        pickup=lambda *e: picked.append(e) or True)
    tap = {"tap_site": 1, "engines": 2, "path": [], "parts": [], "centre": (0, 0)}
    own = {"land": (10, 0), "dir": "north", "engines": 2, "centre": (12.5, 6.0), "poles": [(14, 2)],
           "parts": [(0.0, 0.0, "north", "boiler"), (3.0, -0.5, "north", "iron-chest")]}

    def place_tap(w_, lay, coal):
        rec.append(("pipe", 5.5, 5.5, "north"))           # half a tap laid, then the game refuses a pipe
        raise BuildError("pipe line (48, -4)->(48, -6): cannot_place tile 1 is blocked")
    sp.find_tap_layout = lambda *a: tap
    sp.place_tap = place_tap
    sp.find_layout = lambda *a: own
    sp.place_layout = lambda w_, lay, coal: (14, 2)
    site = sp.build(w, base, {"engines": 2})
    assert picked == [("pipe", 5.5, 5.5, "north")] and rec == []          # the half-built tap came back up
    assert site.poles == [(14, 2)] and "line of site" not in site.note    # its own pump and shore


# ------------------------------------------------------------------ short grid
class ShortGridCrew:
    check_power, on_grid, station_on_grid = Crew.check_power, Crew.on_grid, Crew.station_on_grid

    def __init__(self, networks):
        self.base, self.networks = Base(), networks
        self.base.power_pole = (2, -4)

    def look(self, site, names):
        return {(e[1], e[2]): "no_power" for e in site.entities if e[0] in names}

    def pole_networks(self, tiles):
        return {tuple(t): self.networks.get(tuple(t)) for t in tiles}


def test_idle_machines_on_the_grids_network_count_as_wired_in():
    site = Site("factory", {}, (30, 0), poles=[(30, 2)], entities=[("assembling-machine-1", 30.5, 0.5, "north")])
    assert ShortGridCrew({(2, -4): 1, (30, 2): 1}).check_power(site) is True     # wired: the grid is short
    assert ShortGridCrew({(2, -4): 1, (30, 2): 5}).check_power(site) is False    # really cut off
    assert ShortGridCrew({}).check_power(site) is False                           # no ids: as before


# ------------------------------------------------------------------ oil search
class OilRun:
    budgets: dict = {}

    def act(self, action):
        raise AssertionError(action)

    def scan(self, radius, resources=None, min_tiles=4):
        return {"ok": True, "data": {"patches": []}}


class Kn:
    resource_names = {"crude-oil"}

    def size(self, name):
        return (1, 1)


def test_the_oil_search_remembers_how_far_it_went_and_goes_farther_each_try():
    w = FreeWorld(OilRun(), Kn(), log=lambda *a: None)
    w.scan_radius, w.pos = 384, (0.0, 0.0)
    stops = []
    w.go = lambda x, y: (stops.append((x, y)), setattr(w, "pos", (x, y)))
    w.sleep_ticks = lambda t: None
    w._seen_tick = lambda res: None
    assert w.oil_wells(near=(0.0, 0.0)) == [] and w.oil_rings == 2
    first = [math.dist(s, (0, 0)) for s in stops]
    assert max(first) == pytest.approx(2 * w.oil_step(), rel=0.01)
    stops.clear()
    w.oil_wells(near=(0.0, 0.0))
    assert w.oil_rings == 3 and all(math.dist(s, (0, 0)) == pytest.approx(3 * w.oil_step(), rel=0.01) for s in stops)
    for _ in range(5):
        w.oil_wells(near=(0.0, 0.0))
    assert w.oil_rings == w.OIL_MAX_RINGS
    pj = __import__("brain.modules", fromlist=["MODULES"]).MODULES["pumpjack"]
    base = Base()
    base.add(Site("steam_power", {"engines": 1}, (0, 0), poles=[(2, -4)], powered=True))
    base.power_pole = (2, -4)
    with pytest.raises(BuildError, match="no oil in reach"):
        pj.build(w, base, {"count": 1})
