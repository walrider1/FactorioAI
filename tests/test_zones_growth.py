"""Growth: a segment plans copies of its science blocks in the spare slots from the start (its bus routed for them,
nothing of them built), and a block that runs flat out for GROW_FLAT_MIN gets its copy. Re-routing a laid bus for
a copy added later would move about half of it (337 of 658 pieces in the fixture's segment 1)."""

import pickle

import pytest

from brain import zones
from brain.modules import Base, Site
from test_zones import FIXTURE, _map_paths, bus_crew, planned


def _zones_copy(z, w):
    z2 = pickle.loads(pickle.dumps(z))
    z2._world = w
    z2._seg = dict(z._seg)
    for st in z2.opened.values():
        st["built"] = {}
    return z2


def _unpruned(z, w, seg):
    """Segment 1 with its copies kept whatever the bus makes of them (the growth logic, apart from the map)."""
    old = zones.GROWTH_PRUNE
    zones.GROWTH_PRUNE = False
    try:
        return zones.design_segment(z, 1, seg.kit, z.floorplan(w), growth=True)
    finally:
        zones.GROWTH_PRUNE = old


def test_copies_go_in_the_spare_slots_and_leave_the_era_blocks_where_they_were():
    zones.ENABLED = True
    scan, w, base, z, seg = planned(FIXTURE)
    assert not seg.problems
    plain = zones.design_segment(z, 1, seg.kit, z.floorplan(w), growth=False)
    era = [(b.key, b.slot.id) for b in seg.blocks if not b.growth]
    assert era == [(b.key, b.slot.id) for b in plain.blocks]                  # same blocks, same slots
    copies = [b for b in _unpruned(z, w, seg).blocks if b.growth]            # (the fixture's bus keeps none)
    assert copies and all(b.ok and b.slot.zone == "reserve" for b in copies)
    assert not {b.product for b in copies} & set(zones.GROWTH_SKIP)          # never a science block
    assert {b.product for b in copies} <= seg.items()                        # only copies of era blocks


@pytest.mark.parametrize("path", _map_paths(), ids=lambda p: p.stem)
def test_with_copies_every_original_still_starts_its_bus_line(path):
    """design_bus keeps one line per spine half: a copy must never become the only source of an item an original
    makes for the bus (the labs-slot work found green's line running from its copy, nothing from the original)."""
    zones.ENABLED = True
    scan, w, base, z, seg = planned(path)
    if seg is None or seg.bus is None:
        pytest.skip("no segment on this map")
    assert not seg.problems
    srcs = {t.owner for ln in seg.bus.lines.values() for t in ln.terms if t.io == "src"}
    for b in seg.blocks:
        if b.ok and not b.growth and b.spec.kind == "make" and any(p["io"] == "out" for p in b.ports):
            if any(t.owner == f"{b.key}@{b.slot.id}" for ln in seg.bus.lines.values() for t in ln.terms):
                assert f"{b.key}@{b.slot.id}" in srcs, (path.stem, b.key)


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
    z2.opened[1]["growth"] = True
    z2._seg = {(1, seg.kit, True): _unpruned(z, w, seg)}                    # a map whose bus keeps a copy
    copy = next(b for b in z2.segment(1).blocks if b.growth)
    item = copy.product
    green = next(b for b in z2.segment(1).blocks if b.product == item and not b.growth)   # its original
    assert z2.slot_block(item).key == green.key                                # the era block first
    z2.mark_built(green, (0, 0))
    assert z2.slot_block(item).key == copy.key                                 # then its copy
    b = Base()
    site = b.add(Site("factory", {"item": item, "slot": green.slot.id, "segment_block": green.key}, (0, 0)))
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
    assert f"more {item}" in note and copy.slot.id in note
    assert any(g.get("module") == "factory" and g.get("item") == item for g in crew.urgent)
    assert zones.grow_check(crew, z2, 70.0) == ""                             # asked once


def test_a_bus_fed_slot_factory_counts_as_making_its_item():
    """Run 20261127-101219: with red and green on the bus (no output chests), "automation first" saw no factory
    making them and built a second chain free-form."""
    from types import SimpleNamespace
    from brain.crew import Crew
    b = Base()
    b.add(Site("factory", {"item": "automation-science-pack", "slot": "make-2", "segment_block": "x",
                           "bus_out": ["automation-science-pack"]}, (0, 0)))
    crew = SimpleNamespace(base=b)
    assert Crew.makes(crew, "automation-science-pack") and not Crew.makes(crew, "logistic-science-pack")


def test_more_of_a_slot_item_grows_its_slot_factory_instead_of_a_free_form_one():
    scan, w, base, z, seg = planned(FIXTURE)
    z2 = _zones_copy(z, w)
    red = next(b for b in z2.segment(1).blocks if b.product == "automation-science-pack" and not b.growth)
    b = Base()
    site = b.add(Site("factory", {"item": red.product, "slot": red.slot.id, "segment_block": red.key}, (0, 0),
                      entities=[("assembling-machine-1", 1.5, 1.5, "north"), ("assembling-machine-1", 5.5, 1.5, "north")]))
    crew = bus_crew(z2, b)
    upgraded = []
    crew.upgrade = lambda sid: upgraded.append(sid) or f"upgraded 2 machine(s) in site {sid}"
    crew.kn = type("K", (), {"unlocked": {"assembling-machine-2"}})()
    assert zones.grow_slot(crew, z2, red.product) is None                  # its slot block is not built yet
    z2.mark_built(red, (0, 0))
    note = zones.grow_slot(crew, z2, red.product)
    assert upgraded == [site.id] and "grew instead of a new factory" in note
    assert zones.grow_slot(crew, z2, "transport-belt") is None             # no floorplan block: free-form
    crew.kn = type("K", (), {"unlocked": set()})()                         # nothing faster unlocked
    import pytest as _p
    from brain.world import BuildError
    with _p.raises(BuildError, match="fastest machines"):
        zones.grow_slot(crew, z2, red.product)
    # the flat-out trigger asks for the same: faster machines in its slot when no copy is planned
    crew.kn = type("K", (), {"unlocked": {"assembling-machine-2"}})()
    site.census = {"assembling-machine-1": {"working": 2}}
    assert zones.grow_check(crew, z2, 100.0) == ""
    note = zones.grow_check(crew, z2, 111.0)
    assert f"faster machines in site {site.id}" in note
    assert any(g.get("do") == "upgrade" and g.get("site") == site.id for g in crew.urgent)


def test_only_science_goals_turn_into_slot_growth():
    from types import SimpleNamespace
    from brain.crew import Crew
    scan, w, base, z, seg = planned(FIXTURE)
    z2 = _zones_copy(z, w)
    for blk in z2.segment(1).blocks:
        z2.mark_built(blk, (0, 0))
    calls = []
    crew = SimpleNamespace(zones=z2, log=lambda *a: None, _intermediate_depth=0)
    real = zones.grow_slot
    zones.grow_slot = lambda c, zz, item, why="": calls.append(item) or f"grew {item}"
    try:
        assert Crew._grow_slot_instead(crew, {"item": "automation-science-pack"}) == "grew automation-science-pack"
        assert Crew._grow_slot_instead(crew, {"item": "iron-gear-wheel"}) is None        # a free-form consumer's
        crew._intermediate_depth = 1
        assert Crew._grow_slot_instead(crew, {"item": "logistic-science-pack"}) is None  # a feeder: free-form
    finally:
        zones.grow_slot = real
    assert calls == ["automation-science-pack"]
