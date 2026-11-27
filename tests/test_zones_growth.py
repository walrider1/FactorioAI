"""Growth: a segment plans copies of its science blocks in the spare slots from the start (its bus routed for them,
nothing of them built), and a block that runs flat out for GROW_FLAT_MIN gets its copy. Re-routing a laid bus for
a copy added later would move about half of it (337 of 658 pieces in the fixture's segment 1)."""

import pickle

from brain import zones
from brain.modules import Base, Site
from test_zones import FIXTURE, bus_crew, planned


def _zones_copy(z, w):
    z2 = pickle.loads(pickle.dumps(z))
    z2._world = w
    z2._seg = dict(z._seg)
    for st in z2.opened.values():
        st["built"] = {}
    return z2


def test_copies_go_in_the_spare_slots_and_leave_the_era_blocks_where_they_were():
    zones.ENABLED = True
    scan, w, base, z, seg = planned(FIXTURE)
    assert z.growth_on(1) and not seg.problems
    plain = zones.design_segment(z, 1, seg.kit, z.floorplan(w), growth=False)
    era = [(b.key, b.slot.id) for b in seg.blocks if not b.growth]
    assert era == [(b.key, b.slot.id) for b in plain.blocks]                  # same blocks, same slots
    copies = [b for b in seg.blocks if b.growth]
    assert copies and all(b.ok and b.slot.zone == "reserve" for b in copies)
    assert copies[0].product == "logistic-science-pack"                      # science first
    assert {b.product for b in copies} <= seg.items()                        # only copies of era blocks


def test_a_segment_opened_without_copies_is_never_replanned_with_them():
    scan, w, base, z, seg = planned(FIXTURE)
    z2 = _zones_copy(z, w)
    z2.opened[1].pop("growth", None)                                          # opened by older code
    z2._seg = {}
    assert not z2.growth_on(1)
    assert not any(b.growth for b in z2.segment(1).blocks)


def test_a_block_flat_out_for_ten_minutes_gets_its_copy_and_later_goals_build_it():
    scan, w, base, z, seg = planned(FIXTURE)
    z2 = _zones_copy(z, w)
    green = next(b for b in z2.segment(1).blocks if b.product == "logistic-science-pack" and not b.growth)
    copy = next(b for b in z2.segment(1).blocks if b.product == "logistic-science-pack" and b.growth)
    assert z2.slot_block("logistic-science-pack").key == green.key             # the era block first
    z2.mark_built(green, (0, 0))
    assert z2.slot_block("logistic-science-pack").key == copy.key              # then its copy
    b = Base()
    site = b.add(Site("factory", {"item": "logistic-science-pack", "slot": green.slot.id,
                                  "segment_block": green.key}, (0, 0)))
    crew = bus_crew(z2, b)
    site.census = {"assembling-machine-1": {"working": 3, "item_ingredient_shortage": 1}}   # 75%: not flat out
    assert zones.grow_check(crew, z2, 40.0) == ""
    site.census = {"assembling-machine-1": {"working": 5}}
    assert zones.grow_check(crew, z2, 41.0) == ""                             # flat out: the clock starts
    assert zones.grow_check(crew, z2, 48.0) == ""                             # 7 minutes: not yet
    site.census = {"assembling-machine-1": {"working": 4, "full_output": 1}}  # backed up once: the clock resets
    assert zones.grow_check(crew, z2, 49.0) == ""
    site.census = {"assembling-machine-1": {"working": 5}}
    assert zones.grow_check(crew, z2, 50.0) == ""
    note = zones.grow_check(crew, z2, 61.0)
    assert "logistic-science-pack x2" in note and copy.slot.id in note
    assert any(g.get("module") == "factory" and g.get("item") == "logistic-science-pack" for g in crew.urgent)
    assert zones.grow_check(crew, z2, 70.0) == ""                             # asked once
