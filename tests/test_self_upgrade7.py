"""Self-upgrade #7 (run 20261122-133537): factories placed only where every input can be belted, outposts and
their columns that work by construction, goals that cannot succeed held whoever asks, repairs that survive a
replan, link joins declared at a line's tail and tile bans that expire, and power counted for what will run."""

from types import SimpleNamespace

import pytest

from brain import belts, nodes, outposts
from brain.crew import Crew
from brain.modules import MODULES, Base, Site
from brain.world import BuildError
from fab.grid import Grid

from test_fuel_and_links import GridWorld, _link_crew
from test_lines import READY, MineWorld, _base
from test_upkeep_outposts import _crew as _outpost_crew, _ore_world

F = MODULES["factory"]


# ------------------------------------------------------------------ problem 3: columns and outposts
def test_a_line_column_puts_every_inserter_inside_a_poles_supply_area():
    w, base = MineWorld(), _base()
    col = outposts.SmelterColumn()
    site = col.build(w, base, {"resource": "iron-ore", "count": 5, "near": [-20, 0]})
    assert site.params["into"] == "line"
    ins = [(int(x - 0.5), int(y - 0.5)) for n, x, y, _ in w.placed if n.endswith("inserter")]
    assert len(ins) == 15                                  # ore in, plates out, coal in: three per furnace
    assert outposts.uncovered(ins, site.poles) == []       # run 20261122-133537: 30 of them stood dark
    # the line, the coal belt and the trunk run on through the pole rows
    ln = site.params["line"]
    xs = {t[0] for t in ln["tiles"]}
    ys = sorted(t[1] for t in ln["tiles"])
    assert len(xs) == 1 and ys == list(range(ys[0], ys[0] + len(ys)))
    belts_at = {(int(x - 0.5), int(y - 0.5)) for n, x, y, _ in w.placed if n == "transport-belt"}
    bx = next(iter(xs)) + 5
    assert all((bx - 6, y) in belts_at and (bx, y) in belts_at for y in ys)


def test_the_old_column_layout_would_leave_the_plate_and_coal_inserters_dark():
    fys = [2 * k for k in range(4)]                        # two rows a furnace, a pole at (bx-1, y+1) every other
    poles = [(-1, y + 1) for k, y in enumerate(fys) if k % 2 == 0]
    dark = outposts.uncovered([(-4, y + d) for y in fys for d in (0, 1)], poles)
    assert len(dark) == 8
    ys, gaps, rows = outposts.SmelterColumn.furnace_rows(4, "line")
    assert outposts.uncovered([(-4, y + d) for y in ys for d in (0, 1)] + [(-1, y) for y in ys],
                              [(-3, g) for g in gaps]) == []
    assert rows == 10 and outposts.SmelterColumn.furnace_rows(4, "furnace")[2] == 8


def test_an_outpost_whose_trunk_cannot_reach_a_column_places_nothing():
    crew, w, _ = _outpost_crew()
    _ore_world(w)
    out = outposts.Outpost()
    out.trunk_probe = lambda p, head, start, taken, n: ("no trunk route from its head", None)
    try:
        with pytest.raises(BuildError, match="nothing placed"):
            out.build(w, crew.base, {"resource": "iron-ore", "count": 6, "dense": False})
        assert w.placed == []
        out.trunk_probe = lambda p, head, start, taken, n: (None, (5, 5))
        site = out.build(w, crew.base, {"resource": "iron-ore", "count": 6, "dense": False})
        assert site.params["column_at"] == [5, 5]          # the column goes where the route was checked
    finally:
        out.trunk_probe = None


def test_a_trunk_goal_into_a_column_lays_the_outposts_trunk(monkeypatch):
    crew, w, _ = _outpost_crew()
    outpost = crew.base.add(Site("outpost", {"resource": "iron-ore", "head": [90, 0], "head_dir": "west",
                                             "trunk_to": None}, (95, 0)))
    port = {"item": "iron-ore", "end": [0, 9], "approach": [0, 8], "trunk": True, "fed": None}
    col = crew.base.add(Site("smelter_column", {"resource": "iron-ore", "ports": [port], "outpost": outpost.id},
                             (-3, 14)))
    laid = []
    monkeypatch.setattr(outposts, "lay_trunk", lambda c, o, s: laid.append((o.id, s.id)) or {"belts": 120})
    msg = crew.trunk(col.id, "iron-ore")                   # was: "site 33 has no unfed input port for iron-ore"
    assert laid == [(outpost.id, col.id)] and "120-belt trunk" in msg


def test_a_column_with_no_ore_coming_in_counts_as_no_supply():
    from brain.roadmap import supplying
    crew = _link_crew(GridWorld())
    port = {"item": "iron-ore", "end": [0, 9], "approach": [0, 8], "trunk": True, "fed": None}
    col = Site("smelter_column", {"resource": "iron-ore", "ports": [port], "line": {"rate": 281}}, (0, 0),
               entities=[("stone-furnace", 0, 0, "north")] * 15)
    assert crew.line_rate(col.params["line"], col) == 0.0 and not supplying(col)
    port["fed"] = (5.5, 5.5)
    assert crew.line_rate(col.params["line"], col) > 200 and supplying(col)
    col.health = {"no_power": 30, "working": 15}
    assert not supplying(col)


def test_connect_covers_dark_machines_before_it_says_already_powered():
    crew = _link_crew(GridWorld())
    site = crew.base.add(Site("smelter_column", {"resource": "iron-ore"}, (0, 0), poles=[(0, 0)], powered=True))
    crew.base.powered_poles = lambda exclude=None: [(9, 9)]

    def check(s):
        crew._power_by_grid = True                         # its poles are on the grid, 30 machines dark
        return True
    crew.check_power = check
    crew.short_note = lambda: ""
    crew._cover_unpowered = lambda s: setattr(crew, "_cover_left", 30) or 0
    with pytest.raises(BuildError, match="no free tile for a pole"):
        crew._connect(site)                                # was "already powered", 7 times
    crew._power_by_grid = False
    crew.check_power = lambda s: True
    assert crew._connect(site) == "already powered"


# ------------------------------------------------------------------ problem 1: where a factory may stand
class _D:
    """A design stub: one iron input port, a box, no buildings in the way."""

    def __init__(self, x, tiles=()):
        self.x, self.ok, self.notes, self.placer, self.routes = x, True, [], "search", []
        self.box = (x, 0, x + 4, 4)
        self._tiles = set(tiles)

    def ports(self):
        return [{"item": "iron-plate", "end": [self.x, 0], "approach": [self.x - 1, 0], "chest": None,
                 "inserter": None}]

    def tiles(self):
        return self._tiles


def _probe_crew(monkeypatch):
    w = GridWorld()
    w.keepout = set()
    crew = _link_crew(w, unlocked={"underground-belt"})
    crew.log = lambda *a: None
    mine = crew.base.add(Site("electric_mine", {"resource": "iron-ore", "into": "furnace"}, (0, 0)))
    mine.chests = [(0.5, 0.5, "iron-plate")]
    monkeypatch.setattr(F, "supplied_for", lambda kn, p: ({"iron-plate"}, None))
    return crew


def _grid(wall=()):
    return Grid(seen={(x, y) for x in range(-60, 120) for y in range(-60, 60)}, used=set(wall))


def test_the_route_probe_names_an_input_no_belt_can_reach(monkeypatch):
    crew = _probe_crew(monkeypatch)
    probe = crew.route_probe({"item": "electronic-circuit", "per_minute": 20})
    assert probe(_grid(), _D(40)) == []
    wall = {(x, y) for x in range(20, 27) for y in range(-60, 60)}      # wider than an underground's reach
    bad = probe(_grid(wall), _D(40))
    assert bad and bad[0].startswith("iron-plate into its port") and "no belt route" in bad[0]


class _LadderWorld(SimpleNamespace):
    def __init__(self):
        super().__init__(water=set(), cliffs=set(), used=set(), keepout=set(), resources={}, obstacles=set(),
                         seen={(x, y) for x in range(-10, 10) for y in range(-10, 10)}, pos=(0.0, 0.0),
                         blocking=False, logs=[])

    def think(self, what, fn, *a, **k):
        return fn(*a, **k)

    def log(self, msg):
        self.logs.append(msg)


def _ladder(monkeypatch, probe, avoid=()):
    w = _LadderWorld()
    monkeypatch.setattr(F, "_survey", staticmethod(lambda w, focus: None))
    monkeypatch.setattr(F, "_save_case", lambda *a, **k: None)
    monkeypatch.setattr(F, "_design_at", lambda w, base, grid, item, r, focus, *a, **k: _D(focus[0]))
    monkeypatch.setattr(F, "probe", probe)
    monkeypatch.setattr(F, "avoid", tuple(avoid))
    F.designs.clear()
    return w, F._ladder(w, Base(), "electronic-circuit", 20.0, {"iron-plate"}, [(0, 0), (50, 0)], 10,
                        ("electronic-circuit", 20.0), off_ore=True)


def test_a_place_that_walls_an_input_off_is_skipped_for_the_next(monkeypatch):
    _, got = _ladder(monkeypatch, lambda grid, d: ["iron-plate walled in"] if d.x == 0 else [])
    assert not isinstance(got, str) and got[2] == (50, 0)


def test_with_no_place_passing_the_best_is_built_with_a_warning_but_never_where_it_failed(monkeypatch):
    w, got = _ladder(monkeypatch, lambda grid, d: ["iron-plate into its port: no belt route"])
    assert not isinstance(got, str) and got[2] == (0, 0) and "route probe failed" in got[0].notes[-1]
    assert any("WARNING" in m for m in w.logs)
    _, got = _ladder(monkeypatch, lambda grid, d: ["iron-plate into its port: no belt route"],
                     avoid=[((5.0, 3.0), "iron-plate")])
    assert isinstance(got, str) and "not built there again" in got and "iron-plate" in got


def test_supply_places_score_every_plate_and_the_consumer_together():
    w = _LadderWorld()
    w.seen = {(x, y) for x in range(-150, 150) for y in range(-150, 150)}
    spots = [(100, 0, "copper-plate"), (-100, 0, "iron-plate"), (0, 60, "out")]
    places = F._supply_places(w, spots)
    assert places and abs(places[0][0]) < 40               # between iron and copper, not beside copper
    assert F._supply_places(w, [(100.0, 0.0)])[0][0] > 70  # one plate only: beside it, as before


def test_a_factory_taken_down_walled_in_is_remembered_by_its_spot():
    crew = _link_crew(GridWorld())
    crew.log = lambda *a: None
    port = {"item": "iron-plate", "end": [3, 3], "approach": [2, 3], "fed": None}
    f = crew.base.add(Site("factory", {"item": "electronic-circuit", "ports": [port]}, (10, 10)))
    crew._link_fails[(3, 3)] = {"n": 3, "reason": "no belt route from the source to the port", "item": "iron-plate"}
    crew._note_bad_place(f)
    crew._note_bad_place(f)                                # the replace notes it, then its removal: once
    assert crew.bad_places("electronic-circuit") == [((10, 10), "iron-plate")]
    crew._link_fails[(3, 3)]["reason"] = "no source of iron-plate with any to spare"
    g = crew.base.add(Site("factory", {"item": "iron-gear-wheel", "ports": [dict(port)]}, (40, 10)))
    crew._note_bad_place(g)                                # short of supply, not walled in
    assert crew.bad_places("iron-gear-wheel") == []


def test_a_producer_that_loses_its_only_consumer_is_an_orphan():
    from brain.assess import orphan_alerts
    crew = _link_crew(GridWorld())
    cable = crew.base.add(Site("factory", {"item": "copper-cable"}, (0, 0)))
    cable.chests = [(1.5, 1.5, "copper-cable")]
    circuits = crew.base.add(Site("factory", {"item": "electronic-circuit"}, (20, 0)))
    crew.links = [{"source": (1.5, 1.5), "item": "copper-cable", "kind": "item", "to_site": circuits.id}]
    feeders = crew._feeders_of(circuits)
    assert feeders == {cable.id: "copper-cable"}
    crew.links = []                                        # the removal took its links up
    crew.base.remove(circuits)
    assert "lost its only consumer" in crew._note_orphans(feeders, circuits)
    [a] = orphan_alerts(crew)
    assert a["key"] == f"orphan-{cable.id}" and a["fix"] == {"do": "remove", "site": cable.id}


# ------------------------------------------------------------------ problem 2: goals that cannot succeed
def _clock_crew(minute=100.0, sig=(10, 0)):
    c = SimpleNamespace(fail_log=[], fix_log=[], base_sig=sig)
    c.w = SimpleNamespace(now_tick=lambda: int(c.minute * 3600))
    c.minute = minute
    c._base_sig = lambda: c.base_sig
    return c


def test_the_same_goal_failing_the_same_way_twice_is_held_whoever_asks():
    c = _clock_crew()
    trunk = {"do": "trunk", "site": 33, "item": "iron-ore", "reflex": "assessor: x"}
    nodes.note_failure(c, trunk, "site 33 has no unfed input port for iron-ore")
    assert nodes.futile_block(c, trunk) is None
    c.minute = 104.0
    nodes.note_failure(c, {**trunk, "reflex": "overseer: y"}, "site 33 has no unfed input port for iron-ore")
    why = nodes.futile_block(c, {**trunk, "reflex": "strategist"})
    assert why and why.startswith("held back: this goal failed 2x")
    c.base_sig = (11, 0)                                   # a site built since: one more try
    assert nodes.futile_block(c, trunk) is None
    nodes.note_failure(c, trunk, "site 33 has no unfed input port for iron-ore")
    assert nodes.futile_block(c, trunk)                    # third time: held for the window
    c.minute = 140.0
    assert nodes.futile_block(c, trunk) is None            # the window passed
    assert nodes.futile_block(c, {"do": "feed", "site": 3, "item": "coal"}) is None


def test_a_finished_job_takes_its_waiting_copies_out_of_the_queues():
    done = {"do": "build", "module": "factory", "item": "iron-gear-wheel", "per_minute": 65, "reflex": "rules"}
    plan = [{"do": "build", "module": "factory", "item": "iron-gear-wheel", "per_minute": 65},
            {"do": "build", "module": "factory", "item": "iron-gear-wheel", "per_minute": 300},
            {"do": "link"}]
    gone = nodes.drop_done_copies(done, plan)
    assert len(gone) == 1 and [g.get("per_minute") for g in plan] == [300, None]


def test_a_relink_survives_a_replan_while_its_port_is_unfed():
    from brain.overseer import carry_over
    w = GridWorld()
    w.now_tick = lambda: 100 * 3600
    crew = _link_crew(w)
    crew.urgent, crew.current = [], None
    crew.overseer_forbid = []
    port = {"item": "electronic-circuit", "end": [3, 3], "approach": [2, 3], "fed": None}
    green = crew.base.add(Site("factory", {"item": "logistic-science-pack", "ports": [port]}, (10, 10)))
    relink = {"do": "relink", "site": green.id, "item": "electronic-circuit", "reflex": "strategist"}
    new = [{"do": "build", "module": "factory", "item": "iron-gear-wheel", "per_minute": 10}]
    assert carry_over(crew, [relink], new, "strategist plan")      # was: dropped, "not in the new plan"
    assert relink in new
    port["fed"] = (5.5, 5.5)
    new = []
    assert carry_over(crew, [relink], new, "strategist plan") == []


def test_spare_science_is_no_failed_link():
    st = Site("labs", {"station": True, "ports": [{"pack": "logistic-science-pack"},
                                                  {"pack": "automation-science-pack"}]}, (0, 0))
    assert Crew._pack_everywhere("logistic-science-pack", [st])
    st2 = Site("labs", {"station": True, "ports": [{"pack": "automation-science-pack"}]}, (9, 0))
    assert not Crew._pack_everywhere("logistic-science-pack", [st, st2])


# ------------------------------------------------------------------ problem 4: gates
def test_full_sources_no_route_can_reach_hold_no_new_mine():
    crew = _link_crew(GridWorld())
    mine = crew.base.add(Site("electric_mine", {"resource": "copper-ore", "into": "furnace"}, (0, 0),
                              health={"waiting_for_space_in_destination": 9, "working": 1}))
    mine.chests = [(0.5, 0.5, "copper-plate")]
    crew.outputs = lambda item: [(0.5, 0.5, mine)] if item == "copper-plate" else []
    assert nodes.full_producers(crew, "copper-plate") == [mine]
    crew._link_fails[("boost", 9.5, 9.5)] = {"n": 1, "item": "copper-plate",
                                             "reason": "no belt route from a spare copper-plate source to the "
                                                       "side of its belts"}
    assert nodes.full_producers(crew, "copper-plate") == []


# ------------------------------------------------------------------ problem 5: power for what will run
def test_power_headroom_counts_what_fed_factories_will_draw():
    import json
    from arena import catalog
    if catalog.get().empty:
        pytest.skip("needs config/catalog.json")
    from brain.knowledge import Knowledge
    from test_brain_gm import StubWorld
    kn = Knowledge(json.loads(json.dumps(catalog.get().data)))
    crew = Crew(StubWorld(kn), log=lambda *a: None)
    crew.base.add(Site("steam_power", {"engines": 2}, (50, 0), powered=True))
    port = {"item": "iron-plate", "end": [0, 9], "approach": [0, 8], "fed": None}
    green = crew.base.add(Site("factory", {"item": "logistic-science-pack", "ports": [port]}, (0, 0),
                               entities=[("assembling-machine-1", x * 3, 0, "north") for x in (range(20))],
                               health={"no_ingredients": 20}))
    assert crew.power_status()["expected_kw"] == 0              # starving: it will not draw until fed
    port["fed"] = (5.5, 5.5)
    ps = crew.power_status()
    assert ps["expected_kw"] >= 0.7 * 20 * 70 and ps["headroom_kw"] == ps["capacity_kw"] - ps["expected_kw"]
    assert green.id


# ------------------------------------------------------------------ the organizer's join and ban fixes
def test_a_link_that_turns_on_its_first_tile_declares_the_lines_tail_as_its_join():
    steps = [("belt", (37, 0), "south"), ("belt", (37, 1), "south")]
    assert belts.link_joins(steps, from_belt=True) == [(37, 2), (37, -1)]          # the old guess: wrong tile
    assert belts.link_joins(steps, from_belt=True, tail=(38, 0)) == [(37, 2), (38, 0)]
    crew = _link_crew(MineWorld())
    from brain.modules import line_record
    row = crew.base.add(Site("electric_mine", {"resource": "iron-ore", "into": "line"}, (40, 0)))
    row.params["line"] = line_record("iron-plate", [(40, 0), (39, 0), (38, 0)], "west", (37, 0), 37.5)
    assert crew.link_tail({"source": row.params["line"]["source"]}) == (38, 0)
    assert crew.link_tail({"kind": "trunk", "source": (-208, 114)}) == (-208, 114)


def test_a_banned_belt_tile_is_tried_again_after_its_rest():
    crew = _link_crew(GridWorld())
    now = {"m": 100.0}
    crew._minute = lambda: now["m"]
    crew.ban_tile((35.4, -15.2))
    assert (35, -15) in crew.bad_tiles()
    now["m"] = 100.0 + Crew.BAD_BELT_REST_MIN
    assert (35, -15) not in crew.bad_tiles()
    crew._bad_belt.add((1, 1))                             # an older save's ban: rests from first seen
    assert (1, 1) in crew.bad_tiles()
    now["m"] += Crew.BAD_BELT_REST_MIN
    assert (1, 1) not in crew.bad_tiles()
