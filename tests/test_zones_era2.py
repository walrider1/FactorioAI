"""The blue era (segment 2): the floorplan reserves its slots from the start; once segment 1 is built and blue's
recipes and head inputs are in, segment 2 is planned again for red, green and blue together. Blocks that keep their
design keep their slot and stay built, changed ones are reworked, new ones take the free slots, and the bus is
patched: pieces no longer planned come up, the rest stay."""

import pickle

import pytest

from brain import zones
from brain.modules import Base, Site
from test_zones import FIXTURE, _map_paths, bus_crew, planned

BLUE = {"chemical-science-pack", "advanced-circuit", "engine-unit"}


def _crew_for(z, base, unlocked=BLUE):
    crew = bus_crew(z, base)
    crew.kn = type("K", (), {"unlocked": set(zones.OPEN_RECIPES[1]) | set(unlocked)})()
    crew.makes = lambda item: item in ("plastic-bar", "sulfur", "steel-plate")
    crew.base.add(Site("electric_mine", {"resource": "iron-ore"}, (0, 0)))
    crew.base.add(Site("steam_power", {"engines": 2}, (0, 0)))
    crew.removed = []
    crew.remove = lambda sid, force=False: crew.removed.append(sid) or f"removed site {sid}"
    crew.w.pickup = lambda *a: True
    return crew


def _segment1_built(path):
    zones.ENABLED = True
    scan, w, base, z, seg = planned(path)
    if seg is None:
        pytest.skip("no segment 1 on this map")
    z2 = pickle.loads(pickle.dumps(z))
    z2._world = w
    z2._seg = {k: v for k, v in z._seg.items() if k[0] == 1}
    for st in z2.opened.values():
        st["built"] = {}
    b = Base()
    for blk in z2.segment(1).blocks:
        if blk.ok and not blk.growth:
            z2.mark_built(blk, (0, 0))
            b.add(Site("labs" if blk.spec.kind == "labs" else "factory",
                       {"item": blk.product, "slot": blk.slot.id, "segment_block": blk.key}, (0, 0)))
    return w, z2, b


@pytest.mark.parametrize("path", _map_paths(), ids=lambda p: p.stem)
def test_segment_two_takes_over_keeping_what_stays_and_reworking_what_changed(path):
    w, z, base = _segment1_built(path)
    crew = _crew_for(z, base)
    ok, why = z.ready(2, crew)
    assert ok, why
    seg1 = z.segment(1)
    seg2 = z.segment(2)
    if seg2.problems and any(b.growth for b in seg2.blocks):       # as after_build does: without its copies
        z.__dict__.setdefault("no_growth", set()).add(2)
        z._seg = {k: v for k, v in z._seg.items() if k[0] != 2}
        seg2 = z.segment(2)
    assert not seg2.problems and all(b.ok for b in seg2.blocks if not b.growth), seg2.problems[:2]
    assert BLUE <= {b.product for b in seg2.blocks if b.ok}
    z.open_segment(2, w, seg2.kit)
    st = z.opened[2]
    assert z.current == 2 and z.opened[1].get("superseded") and st["from"] == 1
    old = {b.key: b for b in seg1.blocks if b.ok and not b.growth}
    reworked = {r["key"] for r in st["rework"]}
    for b in seg2.blocks:
        if b.key in old and not b.growth:
            assert b.slot.id == old[b.key].slot.id                       # every block keeps its slot
            assert (b.key in st["built"]) != (b.key in reworked)         # kept built, or reworked
    for blk in ("make-automation-science-pack#0", "make-logistic-science-pack#0"):
        assert blk in st["built"] or blk in reworked
    new = [b for b in seg2.blocks if b.ok and b.product in BLUE]
    assert all(b.slot.id not in {o.slot.id for o in old.values()} for b in new)   # new blocks in free slots
    # a reworked block waits for its old site to come up; a new one is free at once
    eng = z.slot_block("engine-unit")
    assert eng is not None and eng.n == 2
    for r in st["rework"]:
        if not r.get("labs"):
            assert z.slot_block(r["product"]) is None or z.slot_block(r["product"]).slot.id != r["slot"]
    # the bus goal: the old bus becomes segment 2's, unplanned pieces come up, the reworks are queued
    bs = zones.bus_site(crew, 1, create=True)
    jobs1 = seg1.jobs()
    for job in jobs1.values():
        for s in job:
            bs.params["placed"].append(list(zones.step_key(s)))
            bs.entities.append((s.name, s.x, s.y, s.direction))
    keys2 = {zones.step_key(s) for job in seg2.jobs().values() for s in job}
    stale = sum(1 for k in bs.params["placed"] if tuple(k) not in keys2)
    note = zones.adopt_bus(crew, 2)
    assert bs.params["segment"] == 2 and zones.bus_site(crew, 2) is bs
    assert f"{stale} piece(s) no longer planned taken up" in note
    assert all(tuple(k) in keys2 for k in bs.params["placed"])
    notes = zones.rework_old(crew, 2)
    assert len(crew.removed) == len([r for r in st["rework"]])
    assert all(r["done"] for r in st["rework"])
    if st["rework"]:
        assert any("queued for" in n for n in notes)


def test_an_old_world_with_a_segment_one_floorplan_stays_in_segment_one():
    w, z, base = _segment1_built(FIXTURE)
    crew = _crew_for(z, base)
    make = sorted((s for s in z.slots if s.zone == "make"), key=lambda s: s.order)[4:]     # an old world's plan:
    spare = sorted((s for s in z.slots if s.zone == "reserve"), key=lambda s: s.order)[2:]  # 4 make, 2 spare
    gone = {s.id for s in make + spare}
    z.slots = [s for s in z.slots if s.id not in gone]
    z._fp = None
    z._seg = {k: v for k, v in z._seg.items() if k[0] == 1}
    ok, why = z.ready(2, crew)
    assert not ok


def test_segment_two_waits_for_blue_recipes_but_not_for_its_head_inputs():
    """Run 4f7178: blue was researched at minute 560 and the era stayed shut, silently, because the sulfur
    workshop kept failing. It opens on its recipes now; what nothing makes yet is queued when it opens."""
    w, z, base = _segment1_built(FIXTURE)
    crew = _crew_for(z, base, unlocked=set())
    assert not z.ready(2, crew)[0] and "unlocked" in z.ready(2, crew)[1]
    crew = _crew_for(z, base)
    crew.makes = lambda item: item != "sulfur"
    assert z.ready(2, crew)[0]
    assert z.head_short(2, crew) == ["sulfur"]


def test_a_factory_no_era_plans_takes_a_general_slot_and_later_eras_plan_around_it():
    w, z, base = _segment1_built(FIXTURE)
    free = z.general_slots("transport-belt")
    assert len(free) >= 2                                     # reserve slots beyond the planned copies
    assert z.general_slots("automation-science-pack") == []   # an era plans it: it waits for its own slot
    opened, z.opened = z.opened, {}
    assert z.general_slots("transport-belt") == []            # before segment 1 opens: no bus, no general slot
    z.opened = opened
    slot = free[0]
    assert slot.tiles <= z.block
    z.claim(slot, "transport-belt")
    assert not (slot.tiles & z.block) and slot.id not in {s.id for s in z.general_slots()}
    seg2 = z.segment(2)
    assert slot.id not in {b.slot.id for b in seg2.blocks}    # the blue era keeps off it


def test_a_factory_no_era_plans_joins_the_zones_bus_fed():
    """The bus is the zones' only way in: a transport-belt factory becomes an extra block of the live plan (gears
    and iron off the bus), every block keeps its slot and stays built, the bus is patched for its inputs."""
    w, z, base = _segment1_built(FIXTURE)
    crew = _crew_for(z, base)
    crew._minute = lambda: 50.0
    old = {b.key: b.slot.id for b in z.segment(1).blocks if b.ok and not b.growth}
    blk = zones.add_extra(crew, z, "transport-belt", 15)
    assert blk is not None and blk.slot.zone == "reserve" and z.current == 2 and z.era_of(2) == 1
    seg = z.segment(2)
    assert not seg.problems
    assert set(blk.spec.bus_in) <= {"iron-plate", "iron-gear-wheel"}            # bus items in, nothing else
    assert any(p["io"] == "in" for p in blk.ports) and blk.taps                # its inputs tapped off the bus
    st = z.opened[2]
    for b in seg.blocks:
        if b.key in old:
            assert b.slot.id == old[b.key] and b.key in st["built"]            # nothing else moved or reworked
    assert not st["rework"]
    assert z.slot_block("transport-belt").key == blk.key and "transport-belt" in z.planned_items()
    assert zones.add_extra(crew, z, "transport-belt", 15) is None               # planned now: no second one
