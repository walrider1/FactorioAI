"""Self-upgrade #5 (run 20261122-133537): feeder factories first, burner furnace mines never hold the copper gate,
poles other sites run over stay standing, connect trusts the game's network ids, a replace never leaves a
duplicate, the roadmap goes by measured science, and queued goals are not lost without a trace."""

import json
from types import SimpleNamespace

import pytest

from arena import catalog
from brain.crew import Crew
from brain.modules import MODULES, Base, Site
from brain.world import BuildError

needs_catalog = pytest.mark.skipif(catalog.get().empty, reason="needs config/catalog.json (arena catalog)")


def _kn():
    from brain.knowledge import Knowledge
    kn = Knowledge(json.loads(json.dumps(catalog.get().data)))
    kn.unlocked = set(kn.unlocked) | {"underground-belt"}     # green science comes after logistics
    return kn


# ------------------------------------------------------------------ 1. feeders first
class FeedCrew:
    _intermediates_first = Crew._intermediates_first
    PLATE_ORE = Crew.PLATE_ORE
    INTERMEDIATE_DEPTH = Crew.INTERMEDIATE_DEPTH
    FEEDER_HEADROOM = Crew.FEEDER_HEADROOM

    def __init__(self, kn, spare=None, fail=()):
        self.kn, self.spare, self.fail = kn, spare or {}, set(fail)
        self.built = []
        self.current = {"goal": {"do": "build"}, "tick": 0}

    def spare_rate(self, item):
        return self.spare.get(item, 0.0)

    def log(self, *a):
        pass

    def build(self, goal):
        self.built.append((goal["item"], goal["per_minute"], getattr(self, "_intermediate_depth", 0)))
        if goal["item"] in self.fail:
            raise BuildError("no room on open ground")
        return f"built factory as site 9 {{'item': '{goal['item']}'}}"


GREEN = {"item": "logistic-science-pack", "per_minute": 30.0,
         "inputs": ["electronic-circuit", "iron-gear-wheel", "iron-plate"]}


@needs_catalog
def test_a_factory_with_gear_and_circuit_ports_gets_their_factories_first():
    crew = FeedCrew(_kn())
    note = crew._intermediates_first("factory", dict(GREEN))
    items = {i for i, _, _ in crew.built}
    assert items == {"electronic-circuit", "iron-gear-wheel"}          # never plates (those are _plates_first's)
    _, pl = MODULES["factory"].supplied_for(crew.kn, dict(GREEN))
    for item, rate, depth in crew.built:
        assert rate == pytest.approx(pl.supplied[item] * Crew.FEEDER_HEADROOM, abs=0.2) and depth == 1
    assert note.count("first ") == 2
    assert crew.current["extra_min"] > 0                              # the goal's budget grows by the feeders
    assert getattr(crew, "_intermediate_depth", 0) == 0               # reset after the feeders


@needs_catalog
def test_spare_supply_is_subtracted_and_enough_spare_builds_nothing():
    _, pl = MODULES["factory"].supplied_for(_kn(), dict(GREEN))
    crew = FeedCrew(_kn(), spare={"iron-gear-wheel": 500.0, "electronic-circuit": pl.supplied["electronic-circuit"] - 10})
    crew._intermediates_first("factory", dict(GREEN))
    assert [(i, r) for i, r, _ in crew.built] == [("electronic-circuit", pytest.approx(10 * Crew.FEEDER_HEADROOM))]


@needs_catalog
def test_a_split_chain_without_asked_inputs_gets_feeders_for_what_the_split_took_out():
    kn = _kn()
    big = {"item": "logistic-science-pack", "per_minute": 30.0}
    supplied, _ = MODULES["factory"].supplied_for(kn, big)
    split = {i for i in supplied if i not in ("iron-plate", "copper-plate", "logistic-science-pack")}
    assert split                                                      # over 12 assemblers: split
    crew = FeedCrew(kn)
    crew._intermediates_first("factory", big)
    assert {i for i, _, _ in crew.built} == split


@needs_catalog
def test_a_feeder_that_cannot_be_built_stops_the_build_and_names_the_input():
    crew = FeedCrew(_kn(), fail={"electronic-circuit"})
    with pytest.raises(BuildError, match="electronic-circuit input"):
        crew._intermediates_first("factory", dict(GREEN))
    assert getattr(crew, "_intermediate_depth", 0) == 0


@needs_catalog
def test_feeders_stop_at_the_depth_cap_and_other_modules_get_none():
    crew = FeedCrew(_kn())
    crew._intermediate_depth = Crew.INTERMEDIATE_DEPTH
    assert crew._intermediates_first("factory", dict(GREEN)) == "" and not crew.built
    crew._intermediate_depth = 0
    assert crew._intermediates_first("electric_mine", {"resource": "iron-ore"}) == "" and not crew.built


def test_the_factory_description_says_feeders_come_first():
    assert "feeder factories the build makes FIRST" in MODULES["factory"].describe


# ------------------------------------------------------------------ 2. burner furnace mines and the gate
def _gate_crew():
    return SimpleNamespace(base=Base(), w=SimpleNamespace(now_tick=lambda: 100 * 3600))


def _burner(crew, resource="copper-ore", full=True, built=1.0):
    s = Site("burner_mine", {"resource": resource, "count": 4, "into": "furnace"}, (0, 0), built_minute=built)
    s.health = {"waiting_for_space_in_destination": 4, "full_output": 4} if full else {"working": 4}
    return crew.base.add(s)


def _electric(crew, resource="copper-ore"):
    s = Site("electric_mine", {"resource": resource, "count": 2, "into": "furnace"}, (0, 0))
    s.health = {"waiting_for_space_in_destination": 2}
    s.chests = [(0.5, 0.5, "copper-plate")]
    return crew.base.add(s)


def test_full_burner_furnace_mines_do_not_hold_new_copper_mines():
    from brain.nodes import full_producers, surplus_block, unlinkable_full
    crew = _gate_crew()
    a, b = _burner(crew, built=14.0), _burner(crew, built=27.0)
    goal = {"do": "build", "module": "electric_mine", "resource": "copper-ore", "count": 4, "into": "furnace"}
    assert full_producers(crew, "copper-plate") == []
    assert surplus_block(crew, goal) is None                         # the run held this 6 times for sites 4, 12
    assert [s.id for s in unlinkable_full(crew, "copper-plate")] == [a.id, b.id]


def test_linkable_full_producers_still_hold_and_the_hint_names_the_burner_swap():
    from brain.nodes import surplus_block, swap_goal
    crew = _gate_crew()
    old = _burner(crew)
    _electric(crew), _electric(crew)
    goal = {"do": "build", "module": "electric_mine", "resource": "copper-ore", "count": 4, "into": "furnace"}
    why = surplus_block(crew, goal)
    assert "already stand full" in why and f"Sites [{old.id}]" in why and '"replace"' in why
    g = swap_goal(old)
    assert g["do"] == "replace" and g["site"] == old.id and g["with"]["module"] == "electric_mine" \
        and g["with"]["into"] == "furnace" and g["with"]["resource"] == "copper-ore"


def test_short_copper_with_full_burner_mines_raises_a_swap_the_rule_floor_queues():
    from brain.assess import RuleAssessor, burner_swap_alerts
    crew = _gate_crew()
    old = _burner(crew)
    factory = crew.base.add(Site("factory", {"item": "automation-science-pack"}, (9, 9)))
    port = {"item": "copper-plate", "per_minute": 50.0, "fed": True, "end": [9, 9]}
    crew.item_ports = lambda: [(factory, port)]
    crew.kn = SimpleNamespace(unlocked={"electric-mining-drill"})
    crew.base.power_pole = (0, 0)
    crew.w.stats = {"items": {"copper-plate": {"made_per_min": 38.2}}}
    alerts = burner_swap_alerts(crew)
    assert [a["key"] for a in alerts] == [f"swap-{old.id}"] and alerts[0]["fix"]["do"] == "replace"
    d = RuleAssessor().decide({}, alerts, set())
    assert d["urgent_goals"] == [alerts[0]["fix"]] and not d["interrupt"]     # queued, nothing paused
    crew.w.stats = {"items": {"copper-plate": {"made_per_min": 80.0}}}        # enough copper: leave them be
    assert burner_swap_alerts(crew) == []


# ------------------------------------------------------------------ 3. poles and the game's network
def test_a_link_pole_another_site_is_wired_over_stays_standing_and_becomes_its_pole():
    class PoleCrew:
        _pole_bridges = Crew._pole_bridges
        WIRE_REACH = Crew.WIRE_REACH

        def __init__(self, base, extra):
            self.base, self.extra = base, extra

        def existing_poles(self):
            return {tuple(q) for s in self.base.sites for q in s.poles} | set(self.extra)

        def log(self, *a):
            pass

    base = Base()
    base.add(Site("steam_power", {}, (0, 0), poles=[(0, 0)], powered=True))
    base.power_pole = (0, 0)
    iron = base.add(Site("electric_mine", {"resource": "iron-ore"}, (12, 0), poles=[(12, 0), (16, 0)], powered=True))
    link_pole = ("small-electric-pole", 6.5, 0.5, "north")
    crew = PoleCrew(base, {(6, 0)})
    assert crew._pole_bridges([link_pole], own_site=99) == [link_pole]
    assert (6, 0) in iron.poles and tuple(link_pole) in iron.entities
    # with another way round to the grid, the pole may come up
    base2 = Base()
    base2.add(Site("steam_power", {}, (0, 0), poles=[(0, 0), (6, 4)], powered=True))
    base2.power_pole = (0, 0)
    base2.add(Site("electric_mine", {}, (12, 0), poles=[(12, 0)], powered=True))
    assert PoleCrew(base2, {(6, 0)})._pole_bridges([link_pole], own_site=99) == []


def test_connect_routes_to_the_real_grid_when_the_poles_next_door_are_cut_off():
    from test_power_wiring import GridCrew
    base = Base()
    base.add(Site("steam_power", {"engines": 1}, (0, 0), poles=[(2, -4)], powered=True,
                  entities=[("steam-engine", 0.5, -3.5, "north")]))
    base.power_pole = (2, -4)
    # site 19 of the run: its records say powered, the game says its poles are a network of their own
    dark = base.add(Site("electric_mine", {"resource": "iron-ore"}, (38, -4), poles=[(38, -4)], powered=True,
                         entities=[("electric-mining-drill", 38.5, -1.5, "north")]))
    new = base.add(Site("factory", {"item": "x"}, (44, -4), poles=[(44, -4)], powered=False,
                        entities=[("assembling-machine-1", 44.5, -1.5, "north")]))
    crew = GridCrew(base, {(44.5, -1.5): "no_power"}, {(2, -4): 1, (38, -4): 9, (44, -4): 9})
    real_place = crew.w.place

    def place(name, x, y, d, tiles):
        real_place(name, x, y, d, tiles)
        crew.networks.update({(44, -4): 1})
        crew.statuses[(44.5, -1.5)] = "working"
    crew.w.place = place
    msg = crew._connect(new)
    assert crew.placed and "new pole" in msg and new.powered is True      # the run placed 0 poles here, 4 times
    assert dark.powered is False                                          # the rewire takes it from here


def test_patrol_heals_the_powered_flag_once_the_game_shows_power():
    from brain.crew import heal_powered
    s = Site("electric_mine", {}, (0, 0), powered=False, entities=[("electric-mining-drill", 0.5, 0.5, "north")])
    s.health = {"no_power": 3, "working": 1}
    heal_powered(s)
    assert s.powered is False
    s.health = {"working": 4}
    heal_powered(s)
    assert s.powered is True
    station = Site("steam_power", {}, (0, 0), powered=False, entities=[("steam-engine", 0.5, 0.5, "north")])
    station.health = {"working": 1}
    heal_powered(station)
    assert station.powered is False                                       # stations go by their network id


# ------------------------------------------------------------------ 4. replace: complete or nothing
class ReplaceCrew:
    stale_replace = Crew.stale_replace
    _undo_replacement = Crew._undo_replacement

    def __init__(self):
        self.base = Base()
        self.removed = []

    def inputs_stuck(self, site_id):
        return False

    def remove(self, site_id, force=False):
        s = self.base.get(site_id)
        self.removed.append((site_id, force))
        self.base.remove(s)
        return f"removed site {site_id}"


def _red(crew, fed=True):
    s = Site("factory", {"item": "automation-science-pack", "per_minute": 15.0,
                         "ports": [{"item": "iron-plate", "fed": fed}, {"item": "copper-plate", "fed": True}]},
             (7, 12), powered=True, entities=[("assembling-machine-1", 7.5, 12.5, "north")])
    s.health = {"working": 3}
    return crew.base.add(s)


def test_a_failed_replacement_comes_down_again_and_its_feeders_stay():
    crew = ReplaceCrew()
    old = _red(crew)
    before = {s.id for s in crew.base.sites}
    mine = crew.base.add(Site("electric_mine", {"resource": "copper-ore"}, (0, 0)))       # built first for it
    dup = crew.base.add(Site("factory", {"item": "automation-science-pack", "per_minute": 15.0}, (-9, -81)))
    new = {"do": "build", "module": "factory", "item": "automation-science-pack", "per_minute": 15}
    msg = crew._undo_replacement(new, before, old, BuildError("site 22: placed 0 pole(s)"))
    assert crew.removed == [(dup.id, True)] and crew.base.get(old.id) and crew.base.get(mine.id)
    assert "taken down again" in msg and "placed 0 pole" in msg


def test_a_move_whose_cause_is_gone_is_dropped_but_upgrades_never():
    crew = ReplaceCrew()
    old = _red(crew)
    move = {"do": "replace", "site": old.id, "with": {"do": "build", "module": "factory",
                                                      "item": "automation-science-pack", "per_minute": 15}}
    assert "no longer needs moving" in crew.stale_replace(move)
    old.params["ports"][0]["fed"] = None
    assert crew.stale_replace(move) is None                               # an unfed port: the move stands
    old.params["ports"][0]["fed"] = True
    old.health = {"no_power": 3}
    assert crew.stale_replace(move) is None
    burner = crew.base.add(Site("burner_mine", {"resource": "iron-ore"}, (3, 3)))
    swap = {"do": "replace", "site": burner.id, "with": {"do": "build", "module": "electric_mine",
                                                         "resource": "iron-ore", "count": 4}}
    assert crew.stale_replace(swap) is None and crew.stale_replace({**move, "force": True}) is None


# ------------------------------------------------------------------ 5. measured science
def test_the_roadmap_goes_by_measured_science_and_shows_the_planned_rate():
    from brain.roadmap import pack_rate, planned_rate
    base = Base()
    base.add(Site("factory", {"item": "logistic-science-pack", "per_minute": 30.0}, (0, 0)))
    crew = SimpleNamespace(base=base, cell_rate=lambda item: 0.0,
                           w=SimpleNamespace(stats={"items": {"automation-science-pack": {"made_per_min": 6.6}}}))
    assert pack_rate(crew, "logistic-science-pack") == 0.0 and planned_rate(crew, "logistic-science-pack") == 30.0
    assert pack_rate(crew, "automation-science-pack") == 6.6
    crew.w.stats = {}                                                     # no game read yet: the plan stands in
    assert pack_rate(crew, "logistic-science-pack") == 30.0


# ------------------------------------------------------------------ 6. no goal lost without a trace
def _queue_crew():
    return SimpleNamespace(base=Base(), urgent=[], current=None, overseer_forbid=[],
                           w=SimpleNamespace(now_tick=lambda: 100 * 3600, inventory={}))


def test_a_new_plan_keeps_what_the_overseer_fixer_and_close_out_asked_for():
    from brain.overseer import carry_over
    crew = _queue_crew()
    events = []
    ev = lambda kind, **d: events.append((kind, d))
    poles = {"do": "stock", "item": "small-electric-pole", "count": 20, "reflex": "overseer: stock poles"}
    swap = {"do": "replace", "site": 3, "with": {"do": "build", "module": "electric_mine", "resource": "iron-ore"},
            "reflex": "close-out: logistics researched"}
    crew.base.add(Site("burner_mine", {"resource": "iron-ore"}, (0, 0)))
    crew.base.sites[0].id = 3
    gone = {"do": "remove", "site": 21, "force": True, "reflex": "close-out: tier 4"}      # site 21 no longer stands
    own = {"do": "build", "module": "factory", "item": "iron-gear-wheel", "per_minute": 10}  # the old plan's own
    retry = {"do": "connect", "site": 3}
    crew.carry_keys = {json.dumps(retry, sort_keys=True, default=str)}                     # the fixer's retry
    new = [{"do": "build", "module": "factory", "item": "logistic-science-pack", "per_minute": 30}]
    kept = carry_over(crew, [poles, swap, gone, own, retry], new, "strategist plan", ev)
    assert new[1:] == [poles, swap, retry] and len(kept) == 3
    dropped = {d["goal"].split(" ")[0]: d["why"] for k, d in events if k == "dropped"}
    assert set(dropped) == {"remove", "build"}
    assert "site 21 is gone" in dropped["remove"] and dropped["build"] == "not in the new plan"


def test_a_goal_the_new_plan_covers_is_not_doubled_and_forbidden_ones_go():
    from brain.overseer import carry_over
    crew = _queue_crew()
    events = []
    poles = {"do": "stock", "item": "small-electric-pole", "count": 20, "reflex": "overseer: stock poles"}
    new = [{"do": "stock", "item": "small-electric-pole", "count": 40}]
    carry_over(crew, [poles], new, "overseer plan", lambda kind, **d: events.append(d))
    assert len(new) == 1 and not events
    crew.overseer_forbid = [{"match": {"do": "stock"}, "until": 1e12, "why": "no"}]
    carry_over(crew, [poles], [], "overseer plan", lambda kind, **d: events.append(d))
    assert events and "forbidden" in events[0]["why"]
