"""Phase 0 of docs/logistics-plan.md: ports and plates one by one, the gate on new consumers, removals that fit,
and link failures reported in full. The base below is run 20261120-092648 at minute 334 in small: an iron mine
whose spare chests sit full with no link, and a green factory whose iron ports no belt reaches."""

import json

import pytest

from arena import catalog
from brain.crew import Crew
from brain.modules import Site
from brain.nodes import GATE_ESCAPE_MIN, consumer_block, plate_ledger, port_report
from brain.world import BuildError

from test_brain_gm import StubWorld

pytestmark = pytest.mark.skipif(catalog.get().empty, reason="needs config/catalog.json (arena catalog)")


@pytest.fixture
def crew():
    from brain.knowledge import Knowledge
    kn = Knowledge(json.loads(json.dumps(catalog.get().data)))
    c = Crew(StubWorld(kn), log=lambda *a: None)
    c.minute = 0.0
    c.w.now_tick = lambda: int(c.minute * 3600)
    mine = Site("electric_mine", {"resource": "iron-ore", "count": 3, "into": "furnace"}, (0, 0),
                entities=[("electric-mining-drill", x, 0, "north") for x in (0, 3, 6)]
                + [("stone-furnace", x, -2, "north") for x in (0, 3, 6)])
    mine.chests = [(0.5, -4.5, "iron-plate"), (3.5, -4.5, "iron-plate"), (6.5, -4.5, "iron-plate")]
    mine.health = {"waiting_for_space_in_destination": 2, "working": 1}
    c.base.add(mine)
    red = Site("factory", {"item": "automation-science-pack", "per_minute": 5,
                           "ports": [{"item": "iron-plate", "end": [20, 0], "approach": [21, 0],
                                      "per_minute": 6.0, "fed": (0.5, -4.5)}]}, (25, 0))
    c.base.add(red)
    green = Site("factory", {"item": "logistic-science-pack", "per_minute": 20,
                             "ports": [{"item": "iron-plate", "end": [50, k], "approach": [51, k],
                                        "per_minute": 18.0, "fed": None} for k in (0, 4)]}, (55, 0))
    c.base.add(green)
    c.links = [{"source": (0.5, -4.5), "item": "iron-plate", "kind": "item", "to_site": red.id,
                "port": {"per_minute": 6.0}, "belts": 20}]
    for k in (0, 4):
        c._link_fails[(50, k)] = {"n": 11, "item": "iron-plate", "site": green.id, "minute": 0.0, "base": None,
                                  "reason": "no belt route from the source to the port even with a wide detour"}
    c.sites = {"mine": mine, "red": red, "green": green}
    return c


def test_the_report_names_unfed_ports_and_the_stranded_chests(crew):
    rep = port_report(crew)
    assert rep["unfed_count"] == 2 and {p["state"] for p in rep["unfed_ports"]} == {"blocked"}
    assert rep["stranded"] == [{"site": crew.sites["mine"].id, "item": "iron-plate", "chests": 2,
                                "per_minute": 37.5}]
    assert any("2 iron-plate port(s) unfed (blocked 2)" in s for s in rep["summary"])
    assert any("2 iron-plate chest(s)" in s and "full with no link" in s for s in rep["summary"])


def test_a_port_with_no_source_to_spare_says_so(crew):
    crew._link_fails.clear()
    crew.links += [{"source": ch[:2], "item": "iron-plate", "kind": "item", "to_site": 99,
                    "port": {"per_minute": 18.0}, "belts": 9} for ch in crew.sites["mine"].chests[1:]]
    states = {p["state"] for p in port_report(crew)["unfed_ports"]}
    assert states == {"no_source"}
    assert port_report(crew)["stranded"] == []              # every chest has its link now


def test_the_ledger_shows_made_against_delivered(crew):
    crew.w.stats = {"items": {"iron-plate": {"made_per_min": 56.0, "used_per_min": 20.0}}}
    row = next(r for r in plate_ledger(crew) if r["item"] == "iron-plate")
    assert row["belted_to_factories"] == 6.0 and row["unfed_ports"] == 2 and row["unfed_want"] == 36.0
    assert row["stranded_per_min"] == 37.5
    assert "made 56.0/min" in row["text"] and "2 unfed port(s) want 36/min" in row["text"]
    assert "sits in full chests nobody links" in row["text"]


def test_new_iron_consumers_wait_while_iron_ports_starve(crew):
    gears = {"do": "build", "module": "factory", "item": "iron-gear-wheel", "per_minute": 30}
    why = consumer_block(crew, gears)
    assert why and why.startswith("held back: 2 iron-plate port(s)") and "full chests" in why
    assert consumer_block(crew, {"do": "build", "module": "electric_mine", "resource": "iron-ore"}) is None
    assert consumer_block(crew, {"do": "build", "module": "smelter_column", "resource": "iron-plate"})
    # moving the starving factory is the fix, never held; growing another one is held
    move = {"do": "replace", "site": crew.sites["green"].id,
            "with": {"do": "build", "module": "factory", "item": "logistic-science-pack", "per_minute": 20}}
    assert consumer_block(crew, move) is None
    grow = {"do": "replace", "site": crew.sites["red"].id,
            "with": {"do": "build", "module": "factory", "item": "automation-science-pack", "per_minute": 15}}
    assert consumer_block(crew, grow)
    # asked for long enough it goes ahead (no deadlock for good)
    crew.minute = GATE_ESCAPE_MIN + 1
    assert consumer_block(crew, gears) is None
    # the ports fed: nothing is held any more
    crew.minute = 0.0
    for pt in crew.sites["green"].params["ports"]:
        pt["fed"] = (3.5, -4.5)
    assert consumer_block(crew, gears) is None


def test_supply_goals_skip_builds_whose_inputs_starve(crew):
    from brain.supply import supply_goal
    g = supply_goal(crew, "iron-gear-wheel", 20)
    assert g is None or g.get("module") not in ("factory", "workshop")
    assert supply_goal(crew, "iron-plate", 20)["module"] in ("electric_mine", "burner_mine")


def test_the_assessor_does_not_queue_a_held_back_consumer(crew):
    from brain.assess import Supervisor
    sup = Supervisor(log=lambda *a: None)
    crew.current, crew.urgent, crew.plan_queue, crew.closeout_queue = None, [], [], []
    sup.apply(crew, {"urgent_goals": [{"do": "build", "module": "factory", "item": "iron-gear-wheel", "per_minute": 30}],
                     "note": "gears short"}, "assessor")
    assert crew.urgent == []
    for pt in crew.sites["green"].params["ports"]:
        pt["fed"] = (3.5, -4.5)
    sup.apply(crew, {"urgent_goals": [{"do": "build", "module": "factory", "item": "iron-gear-wheel", "per_minute": 30}],
                     "note": "gears short"}, "assessor")
    assert len(crew.urgent) == 1                          # fed again: the same ask is queued


def test_a_removal_empties_chests_first_and_keeps_stashing_while_it_frees_room(crew):
    site = Site("factory", {"item": "iron-gear-wheel"}, (0, 0),
                entities=[("iron-chest", 1.5, 1.5, "north")] + [("assembling-machine-1", 5.5, y, "north")
                                                               for y in range(6)])
    crew.base.add(site)
    held = {"iron-plate": 250}
    crew._chest_held = lambda p: dict(held)
    stashed = []
    crew._stash = lambda item, n, exclude=frozenset(): stashed.append((item, n)) or n

    def extract(x, y, item, n):
        take = min(n, 100, held[item])
        held[item] -= take
        return take
    crew.w.extract = extract
    fails = {"n": 6}                                     # more trips to storage than the old cap of four

    def pickup(name, x, y, direction="north"):
        if name.startswith("assembling") and fails["n"]:
            fails["n"] -= 1
            raise BuildError("inventory full while removing")
        return True
    crew.w.pickup = pickup
    crew.ensure_space = lambda min_free=12, force=False: 16
    crew.removal_blocker = lambda s: None
    msg = crew.remove(site.id)
    assert "7 buildings" in msg and crew.base.get(site.id) is None
    assert stashed == [("iron-plate", 100), ("iron-plate", 100), ("iron-plate", 50)]


def test_link_failures_are_listed_in_full():
    failed = [f"iron-plate into site 37: no belt route {k}" for k in range(5)] + \
             [f"iron-plate into site 56: no belt route {k}" for k in range(3)] + ["copper-plate into site 37: walled"]
    text = Crew._group_fails(failed)
    assert text.startswith("iron-plate into site 37 x5, iron-plate into site 56 x3, copper-plate into site 37")
    assert "First: iron-plate into site 37: no belt route 0" in text


def test_supply_goals_offer_the_consumer_again_once_fed(crew):
    from brain.supply import supply_goal
    for pt in crew.sites["green"].params["ports"]:
        pt["fed"] = (3.5, -4.5)
    assert supply_goal(crew, "iron-gear-wheel", 20)["module"] in ("factory", "workshop")


# run 20261121-140755: six ore-into-chest mines stood full while more were ordered, and repairs repeated without effect
def _gate_crew(minute=100.0):
    from types import SimpleNamespace
    from brain.modules import Base
    crew = SimpleNamespace(base=Base(), w=SimpleNamespace(now_tick=lambda: minute * 3600))
    return crew


def _full_mine(crew, resource="iron-ore", into="chest", full=True):
    from brain.modules import Site
    s = Site("electric_mine", {"resource": resource, "count": 2, "into": into}, (0, 0))
    s.health = {"waiting_for_space_in_destination": 2} if full else {"working": 2}
    return crew.base.add(s)


def test_more_producers_of_an_item_wait_while_the_ones_standing_are_full():
    from brain.nodes import surplus_block, GATE_ESCAPE_MIN
    crew = _gate_crew()
    goal = {"do": "build", "module": "electric_mine", "resource": "iron-ore", "count": 2, "into": "chest"}
    _full_mine(crew)
    assert surplus_block(crew, goal) is None                            # one full site is not a pattern
    _full_mine(crew)
    assert "already stand full" in surplus_block(crew, goal)
    assert surplus_block(crew, {**goal, "into": "line"}) is None        # plates are another item
    assert surplus_block(crew, {"do": "replace", "site": 1, "with": goal}) is None
    crew.w.now_tick = lambda: (100.0 + GATE_ESCAPE_MIN) * 3600
    assert surplus_block(crew, goal) is None                            # held long enough: it goes ahead


def test_a_repair_that_keeps_coming_back_is_held_for_a_different_fix():
    from brain.nodes import repeat_block, REPEAT_WINDOW_MIN
    from brain.overseer import same_target
    crew = _gate_crew()
    goal = {"do": "relink", "site": 56, "item": "iron-plate"}
    key = repr(same_target(goal))
    crew.fix_log = [(key, 80.0, True)]
    assert repeat_block(crew, goal) is None
    crew.fix_log.append((key, 90.0, True))
    assert "does not work" in repeat_block(crew, goal)
    assert repeat_block(crew, {"do": "relink", "site": 42, "item": "iron-plate"}) is None   # another site
    assert repeat_block(crew, {"do": "feed", "site": 56, "item": "coal"}) is None           # not a repair
    crew.w.now_tick = lambda: (90.0 + REPEAT_WINDOW_MIN + 1) * 3600
    assert repeat_block(crew, goal) is None                                                 # old news


def test_engine_units_are_built_ahead_once_the_engine_is_researched():
    from types import SimpleNamespace
    from brain.modules import Base, Site
    from brain.upkeep import prerequisite_goals
    crew = SimpleNamespace(base=Base(), kn=SimpleNamespace(researched=set()), cells=lambda: [])
    assert prerequisite_goals(crew) == []                                   # not researched yet
    crew.kn.researched.add("engine")
    assert prerequisite_goals(crew) == [{"do": "build", "module": "factory", "item": "engine-unit", "per_minute": 2}]
    crew.base.add(Site("factory", {"item": "engine-unit", "per_minute": 2}, (0, 0)))
    assert prerequisite_goals(crew) == []                                   # one already makes them
