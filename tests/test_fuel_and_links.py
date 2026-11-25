"""Coal by belt and a link router that escalates (run 20261119-121625: 26 electric-mine furnaces out of fuel
while 112 coal a minute piled up in chests, nobody saw it because no alert covered those furnaces, and the
planner asked for the same failing link over and over)."""

import math
from types import SimpleNamespace

import pytest

from brain import belts
from brain.assess import fuel_alerts
from brain.crew import Crew
from brain.modules import ElectricMine, Site
from brain.world import BuildError


# ------------------------------------------------------------------ the furnace row's coal belt
def test_a_fuel_unit_lifts_coal_from_its_belt_into_its_furnace_and_fits_beside_the_next():
    em = ElectricMine()
    u = em.unit(10, 10, "furnace", fuel=True)
    qx, qy = u["fuel_inserter"]
    assert (qx, qy - 2) in u["belt"]                         # a long-handed inserter picks up 2 tiles north...
    furnace = {(10, 7), (11, 7), (10, 8), (11, 8)}
    assert (qx, qy + 2) in furnace                            # ...and drops 2 tiles south, into the furnace
    parts = [u["drill"], furnace, {u["inserter"]}, {u["chest"]}, {u["fuel_inserter"]}, set(u["belt"])]
    assert sum(len(p) for p in parts) == len(set().union(*parts))     # nothing overlaps
    nxt = em.unit(13, 10, "furnace", fuel=True)
    assert u["belt"][-1][0] + 1 == nxt["belt"][0][0]          # the belt runs on unbroken from unit to unit
    assert (12, 7) not in set().union(nxt["drill"], nxt["rest"])     # the shared pole still has its tile
    end, appr = em.fuel_port(10, 10)
    assert end == (u["belt"][0][0] - 1, u["belt"][0][1]) and appr == (end[0] - 1, end[1])


def test_coal_belts_wait_for_logistics_as_players_do():
    em = ElectricMine()
    w = SimpleNamespace(kn=SimpleNamespace(unlocked={"long-handed-inserter", "underground-belt"}))
    kit = em.kit(w, None, {"resource": "stone", "count": 4, "line": False})
    assert kit["long-handed-inserter"] == 4 and kit["transport-belt"] == 12
    # a bulk ore row is never under its minimum, whoever asks for it (6 drills into furnaces)
    assert em.kit(w, None, {"resource": "iron-ore", "count": 2, "line": False})["electric-mining-drill"] == 6
    line = em.kit(w, None, {"resource": "stone", "count": 4})        # a plate line: its belt, no chests
    assert line["transport-belt"] == 12 + 14 and "iron-chest" not in line
    w.kn.unlocked = {"long-handed-inserter"}                   # automation, but no underground belts yet
    assert "long-handed-inserter" not in em.kit(w, None, {"resource": "iron-ore", "count": 4})
    w.kn.unlocked = {"long-handed-inserter", "underground-belt"}
    assert "transport-belt" not in em.kit(w, None, {"resource": "coal", "count": 4})


def test_a_row_built_before_logistics_gets_its_coal_belt_where_it_stands():
    w = GridWorld()
    crew = _link_crew(w, unlocked={"long-handed-inserter", "underground-belt"})
    em = ElectricMine()
    row = Site("electric_mine", {"resource": "iron-ore", "count": 3, "into": "furnace", "fuel": "hand"}, (13, 8))
    for cx in (10, 13, 16):
        u = em.unit(cx, 10, "furnace")
        row.entities += [("electric-mining-drill", cx + 0.5, 10.5, "north"), ("stone-furnace", cx + 1.0, 8.0, "north"),
                         ("inserter", cx + 0.5, 6.5, "south"), ("iron-chest", cx + 0.5, 5.5, "north")]
        w.used |= u["drill"] | u["rest"]
    crew.base.add(row)
    w.keepout = set()
    crew._retrofit_row(row)
    assert row.params["fuel"] == "belt" and row.params["ports"][0]["item"] == "coal"
    assert sum(1 for e in row.entities if e[0] == "long-handed-inserter") == 3
    assert sum(1 for e in row.entities if e[0] == "transport-belt") == 9
    blocked = Site("electric_mine", {"resource": "iron-ore", "count": 1, "into": "furnace"}, (40, 8),
                   entities=[("electric-mining-drill", 40.5, 10.5, "north")])
    w.used.add((40, 4))                                         # something already stands behind its chest
    with pytest.raises(BuildError):
        crew._retrofit_row(blocked)


# ------------------------------------------------------------------ alerts
class FuelCrew:
    def __init__(self, sites, links=(), minute=50.0):
        self.base = SimpleNamespace(sites=list(sites), powered_poles=lambda: [(0, 0)])
        self.kn = SimpleNamespace(unlocked={"electric-mining-drill", "underground-belt", "long-handed-inserter"})   # logistics in: fields
        self.links, self.minute = list(links), minute

    def coal_mines(self):
        return [s for s in self.base.sites if s.params.get("resource") == "coal"]

    def _minute(self):
        return self.minute


def _row(sid, port=None, dry=4):
    s = Site("electric_mine", {"resource": "iron-ore", "count": 4, "into": "furnace"}, (0, 0), id=sid,
             entities=[("stone-furnace", k * 3 + 1.0, 0.0, "north") for k in range(4)],
             health={"no_fuel": dry, "waiting_for_space_in_destination": dry})
    if port is not None:
        s.params["ports"] = [port]
    return s


def test_smelters_out_of_fuel_raise_the_alert_that_fixes_the_cause():
    unlinked = _row(18, {"item": "coal", "end": [0, 0], "approach": [-1, 0], "fuel": True, "fed": None})
    port = {"item": "coal", "end": [5, 0], "approach": [4, 0], "fuel": True, "fed": [9.5, 9.5]}
    empty = _row(19, port)
    by_hand = _row(22)
    crew = FuelCrew([unlinked, empty, by_hand], links=[{"port": port, "minute": 10.0}])
    alerts = {a["key"]: a for a in fuel_alerts(crew)}
    assert alerts["fuel-belt-18"]["fix"] == {"do": "link"}
    dry = alerts["fuel-dry-19"]["fix"]                              # more coal: a dense coal field near the base
    assert dry["module"] == "outpost" and dry["resource"] == "coal" and dry["near_base"]
    assert alerts["fuel-site-22"]["fix"]["do"] == "feed"
    assert all(a["level"] == "urgent" and "not a belt one" in a["what"] for a in alerts.values())


def test_a_coal_belt_linked_moments_ago_is_given_time_to_fill():
    port = {"item": "coal", "end": [5, 0], "approach": [4, 0], "fuel": True, "fed": [9.5, 9.5]}
    crew = FuelCrew([_row(19, port)], links=[{"port": port, "minute": 49.0}])
    assert fuel_alerts(crew) == []


# ------------------------------------------------------------------ the router
def test_route_belt_starts_from_whichever_source_is_reachable():
    walled = {(1, 0), (-1, 0), (0, 1), (0, -1)}               # around a start at (0, 0)... and (0, 0) itself taken
    free = lambda t: t not in walled and t != (0, 0)
    steps = belts.route_belt(free, {(0, 0): 0.0, (0, 6): 0.0}, (9, 6), (10, 6), underground=False)
    assert steps and steps[0][1] == (0, 6) and steps[-1][1] == (10, 6)


def test_route_belt_crosses_soft_tiles_only_when_it_must_and_pays_for_them():
    wall = {(5, y) for y in range(-60, 61)}
    free = lambda t: t not in wall
    assert belts.route_belt(free, (0, 0), (9, 0), (10, 0), underground=False, margin=20) is None
    soft = {t: 15.0 for t in wall}
    steps = belts.route_belt(free, (0, 0), (9, 0), (10, 0), underground=False, margin=20, soft=soft)
    assert steps and sum(1 for s in steps if s[1] in wall) == 1    # straight through, one tile of the wall


class GridWorld:
    def __init__(self, used=()):
        self.used = set(used)
        self.water, self.cliffs = set(), set()
        self.recorder = None
        self.placed, self.picked = [], []
        self.fail_at = None

    def free(self, tiles, need_seen=True):
        return not (set(tiles) & self.used)

    def footprint(self, name, x, y, d="north"):
        return {(math.floor(x), math.floor(y))}

    def survey_area(self, *a, **k):
        pass

    def place(self, name, x, y, d="north", tiles=()):
        t = (math.floor(x), math.floor(y))
        if t == self.fail_at:
            exc = BuildError("refused")
            exc.tile = t
            raise exc
        self.used.add(t)
        self.placed.append((name, x, y, d))
        if self.recorder is not None:
            self.recorder.append((name, x, y, d))
        return {"x": x, "y": y}

    def pickup(self, name, x, y, d="north"):
        self.used.discard((math.floor(x), math.floor(y)))
        self.picked.append((name, x, y, d))
        return True

    def step(self, name):
        import contextlib
        return contextlib.nullcontext()


def _link_crew(w, unlocked=()):
    crew = Crew.__new__(Crew)
    crew.w, crew.kn = w, SimpleNamespace(unlocked=set(unlocked))
    from brain.modules import Base
    crew.base, crew.links, crew._bad_belt = Base(), [], set()
    crew.science_links, crew.removed, crew._link_fails, crew._link_failed = {}, [], {}, {}
    crew.removals = 0
    crew.unlock_items = lambda items: None
    crew.make = lambda kit, extra=False: None
    crew._pole_plan = lambda t, avoid=frozenset(): []      # every inserter is covered by a pole
    crew._power_tile = lambda t, avoid=frozenset(), plan=None: None
    crew._minute = lambda: 30.0
    crew.foreman = SimpleNamespace(note=lambda *a: None)
    return crew


def test_a_link_walled_off_by_another_link_moves_that_link_instead_of_failing():
    wall = [("transport-belt", 5.5, y + 0.5, "north") for y in range(-90, 91)]
    w = GridWorld(used={(5, y) for y in range(-90, 91)})
    crew = _link_crew(w)
    consumer = Site("factory", {"item": "x"}, (12, 0))
    crew.base.add(consumer)
    other_port = {"item": "copper-plate", "end": [5, 91], "approach": [5, 92], "fed": [5.5, -95.5]}
    blocker = {"source": (5.5, -95.5), "to_site": consumer.id, "item": "copper-plate", "kind": "item",
               "port": other_port, "entities": wall, "steps": [], "ins": ((5, -94), "north", "inserter")}
    crew.links.append(blocker)
    port = {"item": "iron-plate", "end": [10, 0], "approach": [9, 0], "fed": None}
    srcs = [(0.5, 0.5, None)]
    assert crew._plan_link(port, srcs, [], 70, False) is None          # no way round, no undergrounds yet
    plan = crew._plan_link(port, srcs, [], 70, True)
    assert plan is not None and plan["rip"] == [blocker]
    # where the moved link goes is planned before anything is touched: it avoids the new belt
    new_tiles = {s[1] for s in plan["steps"]} | {plan["ins"]}
    assert plan["relay"] is not None and not ({s[1] for s in plan["relay"]["steps"]} & new_tiles)


def test_only_a_short_link_nobody_taps_and_not_moved_lately_may_be_moved():
    crew = _link_crew(GridWorld())
    ok = {"belts": 20, "minute": 0.0}
    assert crew._movable(ok)
    assert not crew._movable({**ok, "belts": 105})              # a long coal line stays where it is
    assert not crew._movable({**ok, "taps": 1})                 # others take from it
    assert not crew._movable({**ok, "minute": 25.0})            # moved or laid 5 minutes ago


def test_a_link_the_game_refuses_half_way_is_taken_up_and_its_tile_avoided():
    w = GridWorld()
    crew = _link_crew(w)
    site = Site("factory", {"item": "x"}, (12, 0))
    crew.base.add(site)
    port = {"item": "iron-plate", "end": [10, 0], "approach": [9, 0], "fed": None}
    plan = crew._plan_link(port, [(0.5, 0.5, None)], [], 30, False)
    w.fail_at = plan["steps"][3][1]
    with pytest.raises(BuildError):
        crew._build_link(plan, port, site, "item", None)
    assert w.placed and sorted(w.picked) == sorted(w.placed)            # nothing half-built is left behind
    assert w.fail_at in crew._bad_belt and port["fed"] is None and crew.links == []
    w.fail_at = None
    plan2 = crew._plan_link(port, [(0.5, 0.5, None)], [], 30, False)
    assert plan2["steps"][3][1] != plan["steps"][3][1] or all(s[1] not in crew._bad_belt for s in plan2["steps"])
    rec = crew._build_link(plan2, port, site, "item", None)
    assert port["fed"] == [0.5, 0.5] and rec in crew.links and set(rec["entities"]) <= set(site.entities)


def test_an_inserter_no_pole_can_reach_is_found_before_any_belt_is_laid():
    # run 20261121-140755: a science link laid its whole belt, found no room for the inserter's pole, took it all
    # up again, and repeated that 56 times in two hours
    w = GridWorld(used={(0, 0)})                                        # the source chest
    crew = _link_crew(w)
    site = Site("factory", {"item": "x"}, (12, 0))
    crew.base.add(site)
    port = {"item": "iron-plate", "end": [10, 0], "approach": [9, 0], "fed": None}
    plan = crew._plan_link(port, [(0.5, 0.5, None)], [], 30, False)

    def boxed_in(t, avoid=frozenset()):
        raise BuildError(f"no room for a pole to power the link's inserter at {t}")
    crew._pole_plan = boxed_in
    with pytest.raises(BuildError, match="no room for a pole"):
        crew._build_link(plan, port, site, "item", None)
    assert w.placed == [] and crew.links == []                          # nothing laid, nothing to take up
    assert crew._plan_link(port, [(0.5, 0.5, None)], [], 30, False) is None   # the planner skips such starts
    crew._pole_plan = lambda t, avoid=frozenset(): [] if t[1] < 0 else boxed_in(t)   # only the north side powers
    plan2 = crew._plan_link(port, [(0.5, 0.5, None)], [], 30, False)
    assert plan2 is not None and plan2["ins"][1] < 0                    # another side of the source instead


def test_a_new_link_can_tap_a_belt_that_already_carries_the_item():
    w = GridWorld()
    crew = _link_crew(w)
    a, b = Site("factory", {"item": "x"}, (0, 0)), Site("factory", {"item": "y"}, (0, 20))
    crew.base.add(a)
    crew.base.add(b)
    steps = [("belt", (x, 10), "east") for x in range(0, 15)]
    crew.links.append({"source": (-1.5, 10.5), "to_site": a.id, "item": "iron-plate", "kind": "item",
                       "port": {}, "steps": steps, "entities": []})
    taps = crew._belt_taps("iron-plate", b)
    assert len(taps) == 14 and crew._belt_taps("iron-plate", a) == []
    port = {"item": "iron-plate", "end": [7, 16], "approach": [7, 15], "fed": None}
    plan = crew._plan_link(port, [], taps, 30, False)
    assert plan is not None and plan["tap"] is crew.links[0]


def test_link_says_why_and_what_unblocks_it_instead_of_reporting_success():
    w = GridWorld()
    crew = _link_crew(w)
    crew._link_fails[(10, 0)] = {"n": 3, "item": "iron-plate", "site": 30, "reason": "walled in by site 30",
                                 "minute": 29.0, "base": crew._base_sig()}
    crew.base.add(Site("factory", {"item": "x", "ports": [{"item": "iron-plate", "end": [10, 0],
                                                           "approach": [9, 0], "fed": None}]}, (12, 0)))
    with pytest.raises(BuildError) as e:
        crew.link()
    assert "failed 3x" in str(e.value) and "retried by itself" in str(e.value)


# ------------------------------------------------------------------ coal belts carry what their chest gets
# (run 20261119-154451: three boilers and four furnace rows tapped one chest's belt and ran dry in turn, while
# each new coal mine stood unlinked because every port already counted as fed)
def _coal_crew(w):
    crew = _link_crew(w)
    crew._patch_full = {}
    mine = Site("electric_mine", {"resource": "coal", "count": 2, "into": "chest"}, (0, 5),
                chests=[(0.5, 0.5, "coal"), (0.5, 10.5, "coal")])
    b1, b2 = Site("steam_power", {"engines": 2}, (30, 0)), Site("steam_power", {"engines": 2}, (30, 20))
    for s in (mine, b1, b2):
        crew.base.add(s)
    w.used |= {(0, 0), (0, 10)}
    steps = [("belt", (x, 0), "east") for x in range(2, 25)]
    w.used |= {s[1] for s in steps}
    first = {"source": (0.5, 0.5), "to_site": b1.id, "item": "coal", "kind": "item", "steps": steps, "entities": [],
             "port": {"item": "coal", "end": [25, 0], "approach": [24, 0], "fuel": True, "per_minute": 30}}
    crew.links.append(first)
    return crew, mine, b1, b2, first


def test_a_coal_belt_takes_only_the_ports_its_chest_can_feed():
    crew, mine, b1, b2, first = _coal_crew(GridWorld())
    free = crew._free_outputs("coal", b2, need=30)
    assert [(x, y) for x, y, _ in free] == [(0.5, 10.5)]          # the first chest already feeds a boiler
    assert crew._belt_taps("coal", b2, need=30) == []              # so its belt lends no coal to a second one
    first["port"]["per_minute"] = 5.6                              # a furnace row leaves room for more
    assert crew._belt_taps("coal", b2, need=5.6)


def test_an_overloaded_coal_belt_gets_a_spare_chest_fed_in_from_the_side():
    w = GridWorld()
    crew, mine, b1, b2, first = _coal_crew(w)
    tapper = {"source": (12, 0), "to_site": b2.id, "item": "coal", "kind": "item", "steps": [], "entities": [],
              "tap_of": first, "port": {"item": "coal", "end": [12, 19], "approach": [12, 18], "fuel": True,
                                        "per_minute": 30}}
    crew._register(tapper)
    assert crew._coal_root(tapper) == (0.5, 0.5) and crew._overloaded() == [(0.5, 0.5)]
    chain = crew.coal_chains()[0]
    assert chain["burns_per_min"] == 60 and chain["gets_per_min"] == 30 and chain["spare_coal_chests"] == 1
    done, failed = crew.boost_coal()
    assert len(done) == 1 and not failed
    boost = crew.links[-1]
    assert boost["boost_into"] is first and tuple(boost["source"]) == (0.5, 10.5)
    last = boost["steps"][-1]
    nxt = (last[1][0] + belts.VEC[last[2]][0], last[1][1] + belts.VEC[last[2]][1])
    joined = next(s for s in first["steps"] if s[1] == nxt)
    assert last[2] not in (joined[2], belts.LEFT[belts.LEFT[joined[2]]])   # it joins from the side
    assert crew._overloaded() == [] and first["taps"] == 2 and not crew._movable(boost)
    assert crew._free_outputs("coal", b2, need=1) == []           # the spare chest gives all of its coal to the chain


def test_short_coal_belts_raise_the_alert_with_the_fix_for_the_cause():
    from brain.assess import coal_chain_alerts, coal_fix
    w = GridWorld()
    crew, mine, b1, b2, first = _coal_crew(w)
    crew.kn.unlocked = {"electric-mining-drill", "underground-belt", "long-handed-inserter"}
    crew.base.powered_poles = lambda: [(0, 0)]
    crew._register({"source": (12, 0), "to_site": b2.id, "item": "coal", "kind": "item", "steps": [],
                    "entities": [], "tap_of": first, "port": {"item": "coal", "end": [12, 19], "approach": [12, 18],
                                                              "fuel": True, "per_minute": 30}})
    [a] = coal_chain_alerts(crew)
    assert a["fix"] == {"do": "link"} and a["key"].startswith("coal") and "60" in a["what"]
    mine.chests.pop()                                                # no spare chest: more drills
    assert coal_chain_alerts(crew)[0]["fix"]["module"] == "outpost"     # a dense coal field near the base
    burner = crew.base.add(Site("burner_mine", {"resource": "coal", "count": 2, "into": "chest"}, (-5, 0)))
    crew._patch_full["coal"] = (30.0, crew._patch_sig("coal"))              # and no room for them on the patch
    assert coal_fix(crew, 30) == {"do": "replace", "site": burner.id, "with": {
        "do": "build", "module": "electric_mine", "resource": "coal", "count": 4, "into": "chest"}}
    crew.base.remove(burner)
    crew._patch_full["coal"] = (30.0, crew._patch_sig("coal"))
    # the 2-drill coal row on the full patch comes down for a dense field (break before make)
    clear = coal_fix(crew, 30)
    assert clear["do"] == "replace" and clear["site"] == mine.id and clear["clear_first"]
    assert clear["with"]["module"] == "outpost" and clear["with"]["near_base"]
    mine.params["count"] = 10                                        # a big row stays: nothing left to do
    assert coal_fix(crew, 30) is None and coal_chain_alerts(crew)[0]["fix"] is None


def test_a_coal_row_of_drills_into_chests_keeps_a_tile_for_its_pole():
    em = ElectricMine()
    ore = {(x, y) for x in range(0, 30) for y in range(0, 30)}
    used = set()
    w = SimpleNamespace(resources={"coal": ore}, free=lambda tiles: not (set(tiles) & used))
    (cx, cy), = em._rows(w, "coal", 1, "chest", (5, 5))
    used.add((cx + 2, cy - 2))                                       # something stands where its pole would go
    (cx2, cy2), = em._rows(w, "coal", 1, "chest", (5, 5))
    assert (cx2, cy2) != (cx, cy) and (cx2 + 2, cy2 - 2) not in used


def test_one_patch_seen_twice_is_one_patch():
    from brain.world import _same_patch
    assert _same_patch([-39, 27, -19, 46], [-39, 27, -18, 46])
    assert not _same_patch([-39, 27, -19, 46], [1, 29, 32, 61])


def test_failed_links_of_a_removed_site_are_forgotten():
    """run 20261119-202811: links into site 15 stayed "stuck" 70 minutes after site 15 was removed, and every
    link goal failed at once with "nothing new to try" on them."""
    w = GridWorld()
    crew = _link_crew(w)
    live = crew.base.add(Site("factory", {"item": "x", "ports": [{"item": "iron-plate", "end": [10, 0],
                                                                  "approach": [9, 0], "fed": None}]}, (12, 0)))
    for key, sid in (((10, 0), live.id), ((40, 0), 99), (("trunk", 98), 98)):
        crew._link_fails[key] = {"n": 4, "item": "iron-plate", "site": sid, "reason": "no belt route",
                                 "minute": 29.0, "base": crew._base_sig()}
        crew._link_failed[key] = 29.0
    assert [q["into_site"] for q in crew.stuck_links()] == [live.id]   # sites 98 and 99 are gone
    assert set(crew._link_fails) == {(10, 0)} and set(crew._link_failed) == {(10, 0)}


def test_every_removal_lets_failed_links_be_tried_again():
    crew = _link_crew(GridWorld())
    crew.removed = [{}] * 10                        # the list stops at 10: the count must keep going
    before = crew._base_sig()
    crew.removals += 1
    assert crew._base_sig() != before


def test_a_link_that_keeps_failing_waits_for_its_back_off_whatever_the_base_does():
    # run 20261121-140755: any site built anywhere let a hopeless link try again: 2 recorded failures, 56 attempts
    crew = _link_crew(GridWorld())
    site = Site("labs", {}, (0, 0))
    crew._note_link_fail((5, 5), "logistic-science-pack", site, "no room for a pole")
    crew.removals += 1
    assert crew._may_retry((5, 5))                      # first failure: a changed base may have unblocked it
    crew._note_link_fail((5, 5), "logistic-science-pack", site, "no room for a pole")
    crew.removals += 1
    assert not crew._may_retry((5, 5))                  # second: only the back-off lets it try again
    crew._minute = lambda: 30.0 + 30.0
    assert crew._may_retry((5, 5))


def test_a_link_rolled_back_without_a_tile_to_avoid_rests_its_start():
    # a failure that names no refused tile would be planned the same way again: count it, then rest the start
    w = GridWorld(used={(0, 0)})
    crew = _link_crew(w)
    notes = []
    crew.foreman = SimpleNamespace(note=notes.append)
    site = Site("factory", {"item": "x"}, (12, 0))
    crew.base.add(site)
    port = {"item": "iron-plate", "end": [10, 0], "approach": [9, 0], "fed": None}
    plan = crew._plan_link(port, [(0.5, 0.5, None)], [], 30, False)

    def no_power(t, avoid=frozenset(), plan=None):
        raise BuildError("the pole would not go down")     # after the belt is laid, no tile to blame
    crew._power_tile = no_power
    for _ in range(crew.ROLLBACK_LIMIT):
        with pytest.raises(BuildError):
            crew._build_link(plan, port, site, "item", None)
    assert sorted(w.picked) == sorted(w.placed)                         # each try left nothing behind
    assert plan["ins"] in crew._resting_starts() and any("build loop stopped" in n for n in notes)
    again = crew._plan_link(port, [(0.5, 0.5, None)], [], 30, False)
    assert again is None or again["ins"] != plan["ins"]                 # never the same start again soon
    crew._minute = lambda: 30.0 + crew.ROLLBACK_REST_MIN
    assert plan["ins"] not in crew._resting_starts()                    # it rests, it is not banned


class PoleWorld(GridWorld):
    """A grid world that knows which tiles it has seen, for pole routing."""
    def __init__(self, used=()):
        super().__init__(used)
        self.seen = {(x, y) for x in range(-40, 41) for y in range(-40, 41)}
        self.keepout, self.entities = set(), {}

    def blocked(self, t):
        return t in self.used


def test_an_inserter_boxed_in_on_every_side_still_gets_a_pole_two_tiles_away():
    # run 20261121-140755: only the 8 tiles touching the inserter were tried; all were taken
    ring = {(dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)}
    w = PoleWorld(used=ring)
    crew = _link_crew(w)
    del crew._pole_plan                                                 # the real one, not the stub
    crew.existing_poles = lambda: {(20, 0)}
    crew.base.powered_poles = lambda: [(20, 0)]
    plan = crew._pole_plan((0, 0))
    kind, spot = plan[0]
    assert kind == "small-electric-pole" and max(abs(spot[0]), abs(spot[1])) == 2 and spot not in ring
    w.used |= {(x, y) for x in range(-2, 3) for y in range(-2, 3)}     # nothing within a small pole's reach
    with pytest.raises(BuildError):
        crew._pole_plan((0, 0))
    crew.kn.unlocked.add("medium-electric-pole")                        # a medium pole reaches 3 tiles
    kind, spot = crew._pole_plan((0, 0))[0]
    assert kind == "medium-electric-pole" and max(abs(spot[0]), abs(spot[1])) == 3
