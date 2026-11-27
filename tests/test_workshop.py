"""The workshop offline: the production planner, the pipe router, a whole blue-science floor designed on a fake
map (every fluid net routed, no two fluids touching), and the roadmap's next step. Seconds, no game."""

import json
import math

import pytest

from arena import catalog
from brain.pipes import Nets, route

needs_catalog = pytest.mark.skipif(catalog.get().empty, reason="needs config/catalog.json (arena catalog)")


def _kn(all_tech=True):
    from brain.knowledge import Knowledge
    kn = Knowledge(json.loads(json.dumps(catalog.get().data)))
    if all_tech:
        for t in kn.tech_path("rocket-silo"):
            kn.learn_research([t])
    return kn


# ------------------------------------------------------------------ router
def test_router_keeps_fluids_apart_and_crosses_underground():
    nets = Nets()
    wall = {(5, y) for y in range(-20, 21)}                   # a water pipe all along x = 5
    for t in wall:
        nets.pipe[t] = "water"
    steps = route(nets, "petroleum-gas", (0, 0), lambda t: True, goals={(10, 0)})
    assert steps is not None
    tiles = [s[1] for s in steps]
    assert any(s[0] == "ptg" for s in steps)                  # it had to go under the water pipe
    for s in steps:
        if s[0] == "pipe":                                    # no petroleum pipe beside a water pipe
            assert all((s[1][0] + dx, s[1][1] + dy) not in wall for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))
    assert tiles[0] == (0, 0)


def test_router_never_takes_a_port_reserved_for_another_fluid():
    nets = Nets()
    nets.reserve((2, 0), "water")                             # a machine's water port target on the straight line
    steps = route(nets, "petroleum-gas", (0, 0), lambda t: True, goals={(4, 0)})
    pipes = [s[1] for s in steps if s[0] == "pipe"]
    assert steps and (2, 0) not in [s[1] for s in steps]
    assert not {(1, 0), (3, 0), (2, 1), (2, -1)} & set(pipes)        # never beside it either
    assert route(nets, "petroleum-gas", (1, 0), lambda t: True, goals={(4, 0)}) is None   # a start touching it


# ------------------------------------------------------------------ planner
@needs_catalog
def test_planner_blue_science_chain():
    from brain.production import Planner
    kn = _kn()
    p = Planner(kn, "fast-inserter").plan("chemical-science-pack", 10)
    recipes = {c.recipe for c in p.cells}
    assert {"chemical-science-pack", "advanced-circuit", "plastic-bar", "sulfur", "engine-unit"} <= recipes
    assert any(c.machine == "oil-refinery" for c in p.cells) and "crude-oil" in p.fluids and not p.blocked
    assert set(p.raw) <= {"iron-plate", "copper-plate", "coal", "stone", "iron-ore"}
    spare = Planner(kn).plan("chemical-science-pack", 10, spare={"advanced-circuit": 100})
    assert "advanced-circuit" not in {c.recipe for c in spare.cells}      # built cells' spare is used first


# ------------------------------------------------------------------ a whole floor on a fake map
class FakeWorld:
    """Open flat ground, a lake to the east, nothing built."""

    def __init__(self, kn):
        self.kn, self.pos, self.inventory = kn, (0.0, 0.0), {}
        self.resources = {}
        self.water = {(x, y) for x in range(80, 100) for y in range(-20, 40)}
        self.seen = {(x, y) for x in range(-60, 120) for y in range(-60, 140)}
        self.used, self.keepout, self.cliffs = set(), set(), set()
        self.water_hints = [(80.5, 10.5)]
        self.bad_pumps, self.bad_shore, self.pump_fits = set(), set(), {}
        self.blocking = False
        self.logs = []

    def blocked(self, t):
        return t in self.water or t in self.used or t in self.cliffs

    def free(self, tiles, need_seen=True):
        return all(not self.blocked(t) and t not in self.keepout and t in self.seen for t in tiles)

    def survey_area(self, *a, **k):
        pass

    def scan(self):
        return []

    def log(self, msg):
        self.logs.append(msg)


class PumpBase:
    centre = (0.0, 0.0)

    def __init__(self):
        from brain.modules import Site
        self.sites = [Site("pumpjack", {"count": 1}, (40.5, -40.5), entities=[("pumpjack", 40.5, -40.5, "north")])]

    def of(self, kind):
        return [s for s in self.sites if s.kind == kind]

    def powered_poles(self):
        return [(0, 0)]


@needs_catalog
def test_blue_science_floor_routes_every_fluid():
    from brain.workshop import Workshop
    kn = _kn()
    w, base = FakeWorld(kn), PumpBase()
    ws = Workshop()
    d = ws._design(w, base, {"item": "chemical-science-pack", "per_minute": 10})
    nets = d["nets"]
    fluids = set(d["routes"])
    assert {"water", "crude-oil", "petroleum-gas"} <= fluids
    assert d["water"] and d["kit"]["offshore-pump"] == 1
    # no pipe touches a pipe of another fluid
    for t, f in nets.pipe.items():
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            assert nets.pipe.get((t[0] + dx, t[1] + dy), f) == f, (t, f)
    # nothing is built on a walkway, and no pipe sits under a cell's buildings
    from brain.workshop import footprint
    planned = {q for c in d["cells"] for n, x, y, dd in c["entities"] for q in footprint(kn, n, x, y, dd)}
    assert not (set(nets.pipe) | set(nets.ptg)) & planned
    assert not (set(nets.pipe) | set(nets.ptg) | planned) & nets.walk
    # every consumer port is joined: its target holds a pipe of its fluid
    for c in d["cells"]:
        for pt in c["ports"]:
            if pt["fluid"]:
                assert nets.pipe.get(pt["target"]) == pt["fluid"], (c["recipe"], pt)


# ------------------------------------------------------------------ roadmap
@needs_catalog
def test_roadmap_points_at_the_next_science_factory():
    from brain.crew import Crew
    from brain.modules import Site
    from brain.roadmap import roadmap

    class W:
        speed, research, stats, inventory, pos = 1.0, {}, {}, {}, (0.0, 0.0)

        def __init__(self, kn):
            self.kn = kn

        def now_tick(self):
            return 0

    kn = _kn(all_tech=False)
    kn.learn_research(["automation", "electric-mining-drill", "automation-science-pack", "logistic-science-pack"])
    crew = Crew(W(kn), log=lambda *a: None)
    crew.base.add(Site("steam_power", {"engines": 2}, (0, 0), powered=True))
    for _ in range(2):
        crew.base.add(Site("steam_power", {"engines": 2}, (0, 0), powered=True))
    crew.base.add(Site("labs", {"count": 12}, (10, 0)))
    crew.base.add(Site("factory", {"item": "automation-science-pack", "per_minute": 6}, (20, 0)))
    rm = roadmap(crew)
    assert rm["stages"][0]["status"] == "done" and rm["research"]["techs_left"] > 30
    assert rm["next_step"] == {"do": "build", "module": "factory", "item": "automation-science-pack",
                               "per_minute": 9.0}          # belts, not a courier-fed workshop


@needs_catalog
def test_second_workshop_on_a_floor_with_pipes_already_laid():
    """A battery workshop first (its water line crosses the floor), then plastic with a refinery: the second
    must find clean slots, and no pipe may run through a slot left for later cells."""
    from brain.workshop import Workshop
    kn = _kn()
    w, base = FakeWorld(kn), PumpBase()
    ws = Workshop()
    ws.spare = lambda b, f=None, w=None: {"sulfur": 100.0}
    d1 = ws._design(w, base, {"item": "battery", "per_minute": 4})
    dist = d1["district"]
    dist.nets = d1["nets"]                                  # as if built
    for c in d1["cells"]:
        dist.used[c["slot"]] = -1
    ws.designs.clear()
    ws.spare = lambda b, f=None, w=None: {}
    d2 = ws._design(w, base, {"item": "plastic-bar", "per_minute": 10})
    assert any(c["machine"] == "oil-refinery" for c in d2["cells"])
    for c in d2["cells"]:                                   # every port joined to its fluid
        for pt in c["ports"]:
            if pt["fluid"]:
                assert d2["nets"].pipe.get(pt["target"]) == pt["fluid"], (c["recipe"], pt)
    taken = {c["slot"] for c in d1["cells"] + d2["cells"]}
    for rc in dist.free_slots():
        if rc not in taken:
            assert not (Workshop._slot_tiles(dist, *rc) & d2["nets"].occupied()), rc


@needs_catalog
def test_a_full_floor_starts_a_second_floor_with_its_own_fluids():
    """When a floor cannot grow, the next workshop goes on a new floor: fluids made on the first (water,
    petroleum) do not count there, so it gets its own pump and refinery."""
    from brain.workshop import Workshop
    kn = _kn()
    w, base = FakeWorld(kn), PumpBase()
    base.sites.append(type(base.sites[0])("pumpjack", {"count": 1}, (40.5, 60.5),
                                          entities=[("pumpjack", 40.5, 60.5, "north")]))
    ws = Workshop()
    d1 = ws._design(w, base, {"item": "plastic-bar", "per_minute": 5})
    floor1 = d1["district"]
    floor1.nets = d1["nets"]
    for c in d1["cells"]:
        floor1.used[c["slot"]] = -1
    from brain.modules import Site
    s = Site("workshop", {}, (0, 0))
    s.cells = [{**c, "floor": 0} for c in d1["cells"]]
    base.sites.append(s)
    floor1.pumpjacks_joined.add((40.5, -40.5))
    for c in floor1.free_slots():                          # fill it up: nothing left, and no ground to grow on
        floor1.used[c] = -1
    x0, y0, x1, y1 = floor1.box()
    w.seen -= {(x, y) for x in range(x0 - 5, x1 + 6) for y in range(y1 + 1, y1 + 80)}
    ws.designs.clear()
    d2 = ws._design(w, base, {"item": "plastic-bar", "per_minute": 15})     # more than floor 1 has spare
    assert d2["district"] is not floor1 and d2["district"].idx == 1
    assert any(c["machine"] == "oil-refinery" for c in d2["cells"]) and d2["water"]
    for c in d2["cells"]:
        for pt in c["ports"]:
            if pt["fluid"]:
                assert d2["nets"].pipe.get(pt["target"]) == pt["fluid"]


def test_no_underground_end_on_a_port_tile():
    nets = Nets()
    nets.reserve((5, 0), "petroleum-gas")                  # a port target in the way of a straight line
    steps = route(nets, "petroleum-gas", (0, 0), lambda t: True, goals={(12, 0)})
    assert steps and (5, 0) not in [s[1] for s in steps if s[0] == "ptg"]


@needs_catalog
def test_every_cell_is_on_an_unbroken_pole_line():
    """Cells between empty slots must still get power: each used band gets a full line of poles."""
    from brain.workshop import Workshop, footprint
    kn = _kn()
    w, base = FakeWorld(kn), PumpBase()
    d = Workshop()._design(w, base, {"item": "chemical-science-pack", "per_minute": 10})
    poles = set(d["spine"]) | set(d["edge_poles"]) | {(math.floor(x), math.floor(y)) for c in d["cells"]
                                                       for n, x, y, _ in c["entities"] if n == "small-electric-pole"}
    left = [next(iter(poles))]                               # every pole reaches every other by wire
    seen = {left[0]}
    while left:
        p = left.pop()
        for q in poles:
            if q not in seen and math.dist(p, q) <= 7.5:
                seen.add(q)
                left.append(q)
    assert seen == poles
    for c in d["cells"]:                                    # each machine and inserter inside some pole's supply
        for n, x, y, dd in c["entities"]:
            if n in ("iron-chest", "stone-furnace", "steel-furnace"):
                continue
            tiles = footprint(kn, n, x, y, dd)
            assert any(abs(tx - px) <= 2 and abs(ty - py) <= 2 for tx, ty in tiles for px, py in poles), (n, x, y)


@needs_catalog
def test_hand_or_automate_prefers_a_factory_unless_hand_is_cheap_or_the_only_way():
    from brain.crew import Crew
    from brain.modules import Site

    class W:
        speed, research, stats, inventory, pos = 1.0, {}, {}, {}, (0.0, 0.0)

        def __init__(self, kn):
            self.kn = kn

        def now_tick(self):
            return 0

    kn = _kn(all_tech=False)
    kn.learn_research(["automation-science-pack", "automation", "electric-mining-drill", "logistic-science-pack",
                       "electronics", "steam-power", "logistics"])
    crew = Crew(W(kn), log=lambda *a: None)
    choice, _, why = crew.hand_or_automate("automation-science-pack", 10)        # the first research
    assert choice == "hand" and "cheaper" in why
    choice, _, why = crew.hand_or_automate("logistic-science-pack", 100)         # no power yet: nothing can
    assert choice == "hand" and "nothing can automate" in why
    crew.base.add(Site("steam_power", {"engines": 2}, (0, 0), poles=[(0, 0)], powered=True))
    choice, goal, _ = crew.hand_or_automate("logistic-science-pack", 100)
    assert choice == "automate" and goal["module"] == "factory" and goal["item"] == "logistic-science-pack"
    crew._auto_failed["logistic-science-pack"] = 0.0                              # its factory just failed
    assert crew.hand_or_automate("logistic-science-pack", 100)[0] == "hand"
    crew.base.add(Site("factory", {"item": "logistic-science-pack"}, (9, 9), chests=[(9, 9, "logistic-science-pack")]))
    assert crew.hand_or_automate("logistic-science-pack", 100)[0] == "auto"


@needs_catalog
def test_green_science_factory_waits_for_underground_belts_red_does_not():
    from brain.modules import MODULES
    kn = _kn(all_tech=False)

    class W:
        pass
    w = W()
    w.kn = kn
    f = MODULES["factory"]
    assert f.needs(w, None, {"item": "logistic-science-pack", "per_minute": 15}) == {"underground-belt"}
    assert f.needs(w, None, {"item": "automation-science-pack", "per_minute": 15}) == set()


@needs_catalog
def test_book_builds_a_research_station_then_belts_science_to_it():
    from brain.crew import Crew
    from brain.modules import Site
    from brain.roadmap import next_step, roadmap

    class W:
        speed, research, stats, inventory, pos = 1.0, {}, {}, {}, (0.0, 0.0)

        def __init__(self, kn):
            self.kn = kn

        def now_tick(self):
            return 0

    kn = _kn(all_tech=False)
    kn.learn_research(["automation", "electric-mining-drill", "automation-science-pack"])
    crew = Crew(W(kn), log=lambda *a: None)
    for _ in range(3):
        crew.base.add(Site("steam_power", {"engines": 2}, (0, 0), poles=[(0, 0)], powered=True))
    crew.base.add(Site("labs", {"count": 2}, (10, 0)))                     # the early hand-fed row
    crew.base.add(Site("factory", {"item": "automation-science-pack", "per_minute": 15}, (20, 0),
                       chests=[(20.5, 0.5, "automation-science-pack")]))
    rm = roadmap(crew)
    assert rm["next_step"]["module"] == "research_station"
    ports = [{"inserter": (0, 0), "end": (1, 0), "approach": (2, 0), "pack": None}]
    crew.base.add(Site("labs", {"count": 6, "station": True, "ports": ports}, (40, 0), labs=[(40.5, 0.5)]))
    assert roadmap(crew)["next_step"] == {"do": "link"}
    crew.science_links[(20.5, 0.5)] = {"station": 6, "pack": "automation-science-pack"}
    ports[0]["pack"] = "automation-science-pack"
    assert crew.belted_packs() == {"automation-science-pack"}
    assert roadmap(crew)["next_step"] != {"do": "link"}



@needs_catalog
def test_a_courier_fed_workshop_never_stands_in_for_a_belt_factory():
    from brain.crew import Crew
    from brain.modules import Site

    class W:
        speed, research, stats, inventory, pos = 1.0, {}, {}, {}, (0.0, 0.0)

        def __init__(self, kn):
            self.kn = kn

        def now_tick(self):
            return 0

    kn = _kn(all_tech=False)
    kn.learn_research(["automation-science-pack", "automation", "electronics", "steam-power"])
    crew = Crew(W(kn), log=lambda *a: None)
    crew.base.add(Site("steam_power", {"engines": 2}, (0, 0), poles=[(0, 0)], powered=True))
    assert crew.automation_goal("automation-science-pack") is None          # no electric drills yet: not a workshop
    kn.learn_research(["electric-mining-drill"])
    assert crew.automation_goal("automation-science-pack")["module"] == "factory"
    crew._factory_failed["automation-science-pack"] = 0.0                   # its layout just failed: still no workshop
    assert crew.automation_goal("automation-science-pack") is None


def test_every_ai_prompt_builds():
    """Literal JSON braces in a prompt broke str.format twice (the run died at start): build all three."""
    from brain.assess import LLMAssessor
    from brain.brain import GOAL_HELP, PROMPT
    from brain.fixer import LLMFixer

    class C:
        pass
    assert "Reply with JSON only" in PROMPT.format(title="t", summary="s", milestones="m", goals=GOAL_HELP)
    assert LLMFixer(C(), "m").system and LLMAssessor(C(), "m").system


@needs_catalog
def test_big_chains_are_split_into_factories_that_feed_each_other():
    from brain.modules import MODULES
    kn = _kn()
    f = MODULES["factory"]
    sup, plan = f.inputs(kn, "chemical-science-pack", 10)
    assert {"plastic-bar", "steel-plate", "sulfur"} <= sup              # what assemblers cannot make from plates
    assert len(sup) > 3                                                  # and at least one intermediate split off
    from fab import recipes as fr
    assert sum(v for k, v in plan.machines.items() if k in fr.RECIPES and k not in sup) <= f.MAX_ASM
    sup_red, _ = f.inputs(kn, "automation-science-pack", 15)
    assert sup_red == set()                                              # red stays one factory from ore


@needs_catalog
def test_book_puts_power_first_then_replaces_burner_mines_then_feeds_ports():
    from brain.crew import Crew
    from brain.modules import Site
    from brain.roadmap import roadmap

    class W:
        speed, research, stats, inventory, pos = 1.0, {}, {}, {}, (0.0, 0.0)

        def __init__(self, kn):
            self.kn = kn

        def now_tick(self):
            return 0

    kn = _kn(all_tech=False)
    kn.learn_research(["automation", "electric-mining-drill", "automation-science-pack"])
    crew = Crew(W(kn), log=lambda *a: None)
    crew.base.add(Site("steam_power", {"engines": 1}, (0, 0), poles=[(0, 0)], powered=True))
    crew.base.add(Site("labs", {"count": 12}, (10, 0), entities=[("lab", 10 + k, 0, "north") for k in range(12)]))
    assert roadmap(crew)["next_step"]["module"] == "steam_power"           # 900 kW for 12 labs: power first
    for _ in range(2):
        crew.base.add(Site("steam_power", {"engines": 2}, (0, 0), poles=[(0, 0)], powered=True))
    crew.base.add(Site("burner_mine", {"resource": "iron-ore", "count": 6, "into": "furnace"}, (30, 0)))
    step = roadmap(crew)["next_step"]
    assert step["do"] == "replace" and step["with"]["module"] == "electric_mine"
    crew.base.sites = [s for s in crew.base.sites if s.kind != "burner_mine"]
    port = {"inserter": (0, 0), "end": (1, 0), "approach": (2, 0), "item": "plastic-bar", "per_minute": 8, "fed": None}
    crew.base.add(Site("factory", {"item": "chemical-science-pack", "per_minute": 10, "ports": [port]}, (50, 0)))
    step = roadmap(crew)["next_step"]
    assert step == {"do": "build", "module": "workshop", "item": "plastic-bar", "per_minute": 8.8}   # nothing makes it


def test_no_underground_of_another_fluid_crosses_a_port_lane():
    """A real floor sealed a refinery's petroleum port: light oil went under the tile above its lane, the walkway
    above that, so petroleum had no tile left to start its own underground from."""
    from brain.pipes import _span_ok
    nets = Nets()
    nets.lanes[(7, 55)] = nets.lanes[(7, 54)] = "petroleum-gas"
    assert not _span_ok(nets, (6, 54), (8, 54), 1, "light-oil")          # over the lane: refused
    assert _span_ok(nets, (6, 53), (8, 53), 1, "light-oil")              # beside it: fine
    nets.walk |= {(x, 53) for x in range(0, 15)}
    steps = route(nets, "petroleum-gas", (7, 55), lambda t: True, goals={(7, 50)})
    assert steps and any(s[0] == "ptg" and s[1] == (7, 54) for s in steps)   # its own lane starts the underground


@needs_catalog
def test_sulfur_draws_on_the_plastic_refinerys_spare_gas():
    """The overnight zones test (seed 737373): a refinery built for 30 plastic a minute has room for ~210 more gas,
    yet every sulfur build planned a second refinery with its own crude line and failed on the pipe route for 800
    game minutes. Sulfur now goes on that floor with no refinery and no crude of its own, piped from the gas net."""
    from brain.modules import Site
    from brain.workshop import Workshop
    kn = _kn()
    kn.unlocked.discard("advanced-oil-processing")          # as in the run: basic oil processing only
    w, base = FakeWorld(kn), PumpBase()
    ws = Workshop()
    d1 = ws._design(w, base, {"item": "plastic-bar", "per_minute": 30})
    dist = d1["district"]
    dist.nets = d1["nets"]                                  # as if built
    for c in d1["cells"]:
        dist.used[c["slot"]] = -1
    dist.pumpjacks_joined = {(40.5, -40.5)}
    site = Site("workshop", {"item": "plastic-bar", "per_minute": 30}, (0, 0))
    site.cells = [{k: c[k] for k in ("machine", "recipe", "x", "y", "item_in", "item_out", "fluid_in", "fluid_out",
                                     "slot", "sink")} | {"floor": dist.idx} for c in d1["cells"]]
    base.sites.append(site)
    refinery = next(c for c in site.cells if c["machine"] == "oil-refinery")
    room = ws._headroom(w, refinery)
    assert 150 < room["petroleum-gas"][0] < 260 and room["petroleum-gas"][1] == pytest.approx(100 / 45)
    assert ws.spare(base, dist, w)["petroleum-gas"] == pytest.approx(room["petroleum-gas"][0], abs=1)
    assert "petroleum-gas" not in ws.spare(base, dist, w, headroom=False)
    ws.designs.clear()
    d2 = ws._design(w, base, {"item": "sulfur", "per_minute": 6.5})
    assert d2["district"] is dist
    assert [c["recipe"] for c in d2["cells"]] == ["sulfur"]                 # no second refinery
    assert "crude-oil" not in d2["plan"].fluids
    assert '"module": "pumpjack"' in d2["crude_short"]     # one well (600/min) is short of the refinery's 733+238
    base.sites.append(Site("pumpjack", {"count": 1}, (44.5, -40.5), entities=[("pumpjack", 44.5, -40.5, "north")]))
    ws.designs.clear()                                      # a second well nobody joined: piped into the crude line
    d3 = ws._design(w, base, {"item": "sulfur", "per_minute": 6.5})
    port = ws._pumpjack_ports(w, base, dist)
    assert d3["routes"].get("crude-oil") and port and d3["nets"].pipe.get(port[0]) == "crude-oil"
    assert [c["recipe"] for c in d3["cells"]] == ["sulfur"]
    dist.pumpjacks_joined.add((44.5, -40.5))
    ws.designs.clear()
    d2 = ws._design(w, base, {"item": "sulfur", "per_minute": 6.5})
    assert [c["recipe"] for c in d2["cells"]] == ["sulfur"] and not d2.get("crude_short")   # two wells cover it
    for c in d2["cells"]:                                   # its gas and water joined to their nets
        for pt in c["ports"]:
            if pt["fluid"]:
                assert d2["nets"].pipe.get(pt["target"]) == pt["fluid"], pt
    # more gas than the refinery has room for still plans a refinery of its own
    ws.designs.clear()
    big = ws._design(w, base, {"item": "sulfur", "per_minute": 60})
    assert any(c["machine"] == "oil-refinery" for c in big["cells"])


@needs_catalog
def test_gas_a_refinery_burns_into_solid_fuel_is_spare():
    """Advanced oil processing: petroleum nobody uses goes into solid fuel only so the refinery never stalls. A gas
    user takes that gas first; it gets no refinery of its own."""
    from brain.modules import Site
    from brain.workshop import Workshop
    kn = _kn()
    w, base = FakeWorld(kn), PumpBase()
    ws = Workshop()
    d1 = ws._design(w, base, {"item": "plastic-bar", "per_minute": 30})
    assert any(c.get("sink") for c in d1["cells"])
    dist = d1["district"]
    dist.nets = d1["nets"]
    for c in d1["cells"]:
        dist.used[c["slot"]] = -1
    site = Site("workshop", {"item": "plastic-bar", "per_minute": 30}, (0, 0))
    site.cells = [{k: c[k] for k in ("machine", "recipe", "x", "y", "item_in", "item_out", "fluid_in", "fluid_out",
                                     "slot", "sink")} | {"floor": dist.idx} for c in d1["cells"]]
    base.sites.append(site)
    assert ws.spare(base, dist, w)["petroleum-gas"] > 200
    ws.designs.clear()
    d2 = ws._design(w, base, {"item": "sulfur", "per_minute": 6.5})
    assert [c["recipe"] for c in d2["cells"]] == ["sulfur"] and "crude-oil" not in d2["plan"].fluids
