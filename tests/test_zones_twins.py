"""One set of factories, not two (run 4f7178 review, step 2): once a slot factory has run RETIRE_SLOT_MIN, the
start-yard factory for the same item comes down when nothing standing takes from it; a science pack's twin only
once the slot's labs run."""

from brain import zones
from brain.modules import Base, Site
from test_zones import bus_crew, fresh_zones


def _setup(item="iron-gear-wheel"):
    scan, w, z, seg = fresh_zones()
    base = Base()
    crew = bus_crew(z, base)
    slot = base.add(Site("factory", {"item": item, "segment_block": f"make-{item}#0",
                                     "ports": [{"item": "iron-plate", "fed": ["bus", "iron-plate"]}]}, (0, 0)))
    slot.census = {"assembling-machine-1": {"working": 3}}
    old = base.add(Site("factory", {"item": item, "ports": []}, (50, 50)))
    old.chests = [(50.5, 50.5, item)]
    crew.outputs = lambda it: [(x, y, s) for s in base.sites for x, y, what in s.chests if what == it]
    crew.science_links = {}
    return z, crew, slot, old


def removals(crew):
    return [g["site"] for g in crew.urgent if g.get("do") == "remove"]


def test_the_start_yard_twin_goes_once_the_slot_factory_has_run_a_while():
    z, crew, slot, old = _setup()
    assert zones.retire_twins(crew, z, 100.0) == ""                     # just seen running
    assert zones.retire_twins(crew, z, 100.0 + zones.RETIRE_SLOT_MIN) and removals(crew) == [old.id]
    zones.retire_twins(crew, z, 100.0 + zones.RETIRE_SLOT_MIN + 1)
    assert removals(crew) == [old.id]                                   # asked once


def test_a_twin_something_still_takes_from_stays():
    z, crew, slot, old = _setup()
    user = crew.base.add(Site("factory", {"item": "automation-science-pack"}, (60, 60)))
    crew.links = [{"item": "iron-gear-wheel", "source": (50.5, 50.5), "to_site": user.id}]
    zones.retire_twins(crew, z, 0.0)
    assert zones.retire_twins(crew, z, 50.0) == "" and not removals(crew)


def test_a_slot_factory_that_does_not_run_retires_nothing():
    z, crew, slot, old = _setup()
    slot.census = {"assembling-machine-1": {"item_ingredient_shortage": 3}}
    zones.retire_twins(crew, z, 0.0)
    assert zones.retire_twins(crew, z, 50.0) == "" and not removals(crew)


def test_a_science_twin_waits_for_the_slots_labs():
    z, crew, slot, old = _setup("automation-science-pack")
    zones.retire_twins(crew, z, 0.0)
    assert zones.retire_twins(crew, z, 50.0) == ""
    z.labs_running = {"slot": 10.0}
    assert zones.retire_twins(crew, z, 51.0) and removals(crew) == [old.id]


def test_segment_one_asks_for_the_research_it_waits_on_and_no_longer_waits_for_green():
    """Run 4f7178: logistics came only at minute ~240 (the planner dropped it at 23); segment 1 opened at 251."""
    from types import SimpleNamespace
    scan, w, z, seg = fresh_zones()
    z.opened = {}
    crew = bus_crew(z, Base())
    crew.kn = SimpleNamespace(unlocked={"assembling-machine-1", "long-handed-inserter"},
                              researched={"automation", "electric-mining-drill"})
    assert "logistics" in zones.gate_research(crew, z, 30.0)
    assert [g["tech"] for g in crew.urgent if g.get("do") == "research"] == ["logistics"]
    assert zones.gate_research(crew, z, 35.0) == ""                        # once a GATE_ASK_MIN
    crew.kn.unlocked |= {"underground-belt"}
    assert zones.gate_research(crew, z, 100.0) == ""                       # all in: nothing to ask
    assert "logistic-science-pack" not in zones.OPEN_RECIPES[1]
