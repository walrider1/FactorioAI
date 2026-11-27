"""Everything later is zoned: purple, yellow and the rocket are floorplan eras like blue; an era that needs more
slots than the floorplan holds grows the spine past its end (new slots, the built bus kept: only its pole rows go
on); a factory for an item a later era plans waits for that era's slot once its recipes are in (never round the
start yard), at most ERA_WAIT_MAX; and the silo goes into the silo slot."""

from types import SimpleNamespace

import pytest

from brain import zones
from test_zones import FIXTURE
from test_zones_era2 import _crew_for, _segment1_built


def test_purple_yellow_and_the_rocket_are_eras_whose_assembler_parts_go_into_slots():
    kit = zones.kit_for(SimpleNamespace(kn=SimpleNamespace(researched={"automation-2", "advanced-material-processing"})))
    products = {e: {s.product for s in zones.segment_specs(e, kit)} for e in (2, 3, 4, 5)}
    assert {"rail", "electric-furnace", "productivity-module", "production-science-pack"} <= products[3]
    assert {"low-density-structure", "flying-robot-frame", "utility-science-pack"} <= products[4]
    assert products[2] <= products[3] <= products[4] <= products[5]          # each era keeps the earlier ones
    assert not {"battery", "processing-unit", "electric-engine-unit"} & products[4]   # fluids: workshops, bus head
    assert {"battery", "processing-unit"} <= set(zones.SEGMENT_INPUTS[4])
    assert all(e in zones.OPEN_RECIPES for e in zones.SEGMENTS)


def test_an_era_short_of_slots_grows_the_spine_and_keeps_the_built_bus():
    w, z, base = _segment1_built(FIXTURE)
    kit = zones.kit_for(w)
    have, _ = z.room(2, kit)
    before = z.segment(1).jobs()
    cells = len(z.corridors["spine"].cells)
    snap = z.snapshot(w, base)
    snap["yard"] = z.yard
    spine, slots = z.plan_more(snap, 3)
    if spine is None:
        pytest.skip("no land past this fixture's spine end")
    assert len(spine.cells) > cells and spine.cells[:cells] == z.corridors["spine"].cells
    assert len(slots) >= 3 and {s.zone for s in slots} <= {"make", "reserve"}
    old_ids = {s.id for s in z.slots}
    assert not old_ids & {s.id for s in slots}                                # numbering carries on
    assert z.apply_more(spine, slots) == len(slots)
    assert z.room(2, kit)[0] == have + len(slots)
    assert all(t in z.block for s in slots for t in s.tiles)                  # reserved like the first slots
    z._seg = {}
    after = z.segment(1).jobs()
    for name in set(before) | set(after):
        a, b = set(map(str, before.get(name, []))), set(map(str, after.get(name, [])))
        assert a <= b, name                                                   # nothing built comes up
        if name != "bus:poles":
            assert a == b, name                                               # belts as they were


def test_a_later_eras_factory_waits_for_its_slot_once_the_era_is_near_then_gives_up():
    w, z, base = _segment1_built(FIXTURE)
    crew = _crew_for(z, base, unlocked=set())
    minute = {"now": 100.0}
    crew._minute = lambda: minute["now"]
    assert zones.waits_for_zone(crew, "chemical-science-pack") is None          # blue's recipes not in: as asked
    crew.kn.unlocked |= zones.OPEN_RECIPES[2]
    why = zones.waits_for_zone(crew, "chemical-science-pack")
    assert why and "era 2" in why
    assert zones.waits_for_zone(crew, "transport-belt") is None                  # no era plans it
    minute["now"] += zones.ERA_WAIT_MAX + 1
    assert zones.waits_for_zone(crew, "chemical-science-pack") is None          # it could not open: free-form


def test_the_silo_goes_into_the_floorplans_silo_slot():
    from brain.workshop import Silo
    w, z, base = _segment1_built(FIXTURE)
    slot = next(s for s in z.slots if s.zone == "silo")
    spot = Silo._slot_spot(SimpleNamespace(zones=z))
    x0, y0, sw, sh = slot.rect
    assert spot is not None and x0 <= spot[0] and spot[0] + 15 <= x0 + sw and y0 <= spot[1] and spot[1] + 13 <= y0 + sh
    assert slot.id in z.claimed and not set(slot.tiles) & z.block             # its land is for the silo now
    assert Silo._slot_spot(SimpleNamespace(zones=z)) is None                  # once only


def test_a_built_block_that_still_covers_the_next_eras_rate_is_kept_not_torn_down():
    """Run 4f7178: blue's take-over reworked red and green science (15/min in both eras) only because the kit had
    moved on to faster machines. They stay built now; the blocks whose rate grew are reworked."""
    w, z, base = _segment1_built(FIXTURE)
    crew = _crew_for(z, base)
    assert z.ready(2, crew)[0]
    logs = []
    z.open_segment(2, w, z.segment(1).kit, log=logs.append)
    kept = set(z.opened[2]["built"])
    reworked = {r["key"] for r in z.opened[2]["rework"]}
    assert {"make-automation-science-pack#0", "make-logistic-science-pack#0"} <= kept
    assert "labs#0" in reworked                                    # the labs take blue too: a new design
    assert z.opened[2]["keep_cover"]


def test_a_workshop_goal_for_a_slot_item_becomes_a_factory_goal_fluid_items_stay_workshops():
    from brain.crew import Crew
    w, z, base = _segment1_built(FIXTURE)
    crew = Crew.__new__(Crew)
    crew.zones, crew.log = z, (lambda *a: None)
    goal = {"do": "build", "module": "workshop", "item": "chemical-science-pack", "per_minute": 10}
    assert crew._slot_not_workshop(goal)["module"] == "factory"
    assert crew._slot_not_workshop({**goal, "item": "sulfur"})["module"] == "workshop"
    assert crew._slot_not_workshop({**goal, "item": "processing-unit"})["module"] == "workshop"
