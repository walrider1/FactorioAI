"""The floorplan's labs slot (zones.py "the labs slot").

Run 20261127-101219, and every zones run before it: segment 1 designed labs#0 in its science slot, and its bus had
the packs' lines planned from the science slots to it, but only factory goals ever went into slots. Labs went up
wherever there was clear ground (a hand-fed row by the spawn, stations whose long pack belts were walled in or
polluted) and the pack lines were never laid. Now:
- a research_station goal builds the labs block in its slot (Atlas's design: 8 labs for red + green 30/min);
- its pack lines go down right after; a science slot built before the labs (its packs into an output chest on the
  spot where its line injects) switches to the bus: the chest and its inserter come up, with the links out of it;
- science slots built after the labs put their packs on the bus at once (Zones.onto_bus);
- no labs go up outside the floorplan while the slot's labs stand; nobody hand-feeds the slot's labs;
- the zones propose the labs goal, the missing science blocks, and later the other labs' removal.
Offline on the Atlas fixture map (needs the Atlas submodule)."""

import math
from types import SimpleNamespace

import pytest

from test_zones import FIXTURE, KN, bus_crew, fresh_zones, zones_on  # noqa: F401 - zones_on: autouse fixture

pytestmark = pytest.mark.skipif(not FIXTURE.exists(), reason="needs the Atlas submodule (git submodule update --init)")


def _blocks(seg):
    labs = next(b for b in seg.blocks if b.spec.kind == "labs")
    red = next(b for b in seg.blocks if b.product == "automation-science-pack" and not b.growth)
    green = next(b for b in seg.blocks if b.product == "logistic-science-pack" and not b.growth)
    return labs, red, green


def _labs_world(crew):
    """The bus crew's fake world, with what build_labs_slot and switch_to_bus use."""
    w = crew.w
    w.blocked = lambda t: t in w.used
    w.picked = []

    def pickup(name, x, y, direction="north"):
        w.picked.append((name, math.floor(x), math.floor(y)))
        return True
    w.pickup = pickup
    w.logged = []
    w.log = lambda *a: w.logged.append(a)
    return w


def _green_with_chest(z, seg, base):
    """The green slot factory as the live run built it before the labs: packs into a chest on the spine edge,
    and a science link out of that chest to a station elsewhere."""
    from brain import zones
    from brain.modules import Site
    _, _, green = _blocks(seg)
    sb = zones.slot_build(green, seg.consumed())
    assert not sb.bus_out and sb.chests and len(sb.stub) == 2
    z.mark_built(green, sb.focus)
    site = base.add(Site("factory", {"item": green.product, "segment_block": green.key, "slot": green.slot.id,
                                     "bus_out": [], "ports": [dict(p) for p in sb.ports]}, sb.focus))
    site.chests = [(x, y, green.product) for x, y in sb.chests]
    site.entities = [(st.name, st.x, st.y, st.direction) for st in sb.stub]
    return site, sb


def test_onto_bus_adds_the_packs_only_once_the_labs_stand():
    scan, w, z, seg = fresh_zones()
    labs, red, green = _blocks(seg)
    assert labs.ok and labs.slot.zone == "science"
    assert z.labs_block() is labs and z.labs_block(built=True) is None
    before = z.onto_bus(1)
    assert before == seg.consumed() and "logistic-science-pack" not in before
    z.mark_built(labs, (0, 0))
    after = z.onto_bus(1)
    assert after == before | {"automation-science-pack", "logistic-science-pack"}
    assert z.labs_block() is None and z.labs_block(built=True) is labs
    from brain import zones
    sb = zones.slot_build(red, after)                            # a science slot built now: packs onto the bus
    assert sb.bus_out == ["automation-science-pack"] and not sb.chests and not sb.stub


def test_the_planned_bus_has_the_pack_lines_from_the_science_slots_to_the_labs():
    scan, w, z, seg = fresh_zones()
    labs, red, green = _blocks(seg)
    jobs = seg.jobs()
    assert "bus:automation-science-pack" in jobs and "bus:logistic-science-pack" in jobs
    assert {t["item"] for t in labs.taps} == {"automation-science-pack", "logistic-science-pack"}
    assert seg.inputs_of([labs]) == {"automation-science-pack", "logistic-science-pack"}


def test_the_built_science_blocks_feed_the_labs_lines_never_their_planned_copies():
    scan, w, z, seg = fresh_zones()
    labs, red, green = _blocks(seg)
    assert not any(b.growth and b.product in labs.spec.bus_in for b in seg.blocks)   # no science copies (GROWTH_SKIP)
    assert seg.feeds_line(red) and seg.feeds_line(green)
    for b in seg.blocks:
        if b.growth and b.product in labs.spec.bus_in:
            assert not seg.feeds_line(b)
    assert not seg.problems


def test_a_research_station_goal_builds_the_labs_block_in_its_slot():
    from brain import zones
    from brain.modules import MODULES, Base
    scan, w, z, seg = fresh_zones()
    labs, _, _ = _blocks(seg)
    crew = bus_crew(z, Base())
    fw = _labs_world(crew)
    fw.powered = True
    base = SimpleNamespace(powered_poles=lambda: [(0, 0)], of=lambda kind: [], centre=(0, 0))
    rs = MODULES["research_station"]
    d = zones.labs_slot_design(fw)
    assert d is not None and d.block is labs
    assert rs.kit(fw, base, {"count": 6}) == d.kit() and d.kit()["lab"] == labs.spec.labs
    site = rs.build(fw, base, {"count": 6})
    assert site.kind == "labs" and site.params["segment_block"] == labs.key and site.params["bus_fed"]
    assert not site.params.get("station")                        # no science link, no feeder ever goes in
    assert site.params["count"] == len(site.labs) == labs.spec.labs
    assert {pt["item"] for pt in site.params["ports"]} == {"automation-science-pack", "logistic-science-pack"}
    assert all(pt["feed"] and not pt["fed"] for pt in site.params["ports"])
    x0, y0, wd, ht = labs.slot.rect
    assert all(x0 <= x < x0 + wd and y0 <= y < y0 + ht for x, y in site.labs)   # inside the science slot
    assert z.is_built(labs) and zones.labs_slot_design(fw) is None


def test_the_labs_lay_their_pack_lines_and_a_science_slot_switches_from_its_chest_to_the_bus():
    from brain import zones
    from brain.modules import Base, Site
    scan, w, z, seg = fresh_zones()
    labs, red, green = _blocks(seg)
    base = Base()
    crew = bus_crew(z, base)
    fw = _labs_world(crew)
    gsite, gsb = _green_with_chest(z, seg, base)
    chest = gsite.chests[0][:2]
    unlinked = []
    station_link = {"source": chest, "kind": "science", "item": green.product, "to_site": 99}
    crew.links = [station_link]
    crew._unlink = lambda ln: unlinked.append(ln) or crew.links.remove(ln)
    # the labs stand (built by the goal above): after_build lays the pack lines
    z.mark_built(labs, (0, 0))
    lsite = base.add(Site("labs", {"segment_block": labs.key, "slot": labs.slot.id, "bus_fed": True,
                                   "ports": [dict(p) for p in zones.slot_build(labs).ports]}, (0, 0)))
    msg = zones.lay_bus(crew, 1, seg.inputs_of(z.active_blocks(1)))
    laid = set(zones.bus_site(crew, 1).params["laid"])
    assert {"automation-science-pack", "logistic-science-pack"} <= laid
    # green's output chest and inserter came up from where its line injects, and the link out of the chest
    stub_tiles = {(st.name, st.tiles[0][0], st.tiles[0][1]) for st in gsb.stub}
    assert set(fw.picked) == stub_tiles and unlinked == [station_link]
    assert gsite.params["bus_out"] == [green.product] and not gsite.chests and not gsite.entities
    assert "puts its logistic-science-pack on the bus" in msg
    # the green line's pieces went down on those tiles (nothing refused there)
    green_line = {tuple(t) for s in seg.jobs()["bus:logistic-science-pack"] for t in s.tiles}
    assert {(x, y) for _, x, y in stub_tiles} <= green_line
    assert not zones.bus_site(crew, 1).params.get("gaps")
    # the labs' ports are fed by the taps: the link step never belts into them
    assert all(pt["fed"] == ["bus", pt["item"]] for pt in lsite.params["ports"])
    # laid once: nothing switches or is placed again
    n = len(crew.w.run.placed)
    zones.lay_bus(crew, 1, seg.inputs_of(z.active_blocks(1)))
    assert len(crew.w.run.placed) == n and len(fw.picked) == 2


def test_no_labs_go_up_outside_the_floorplan_while_the_slot_labs_stand():
    from brain.modules import MODULES, Base, Site
    from brain.world import BuildError
    base = Base()
    base.add(Site("labs", {"segment_block": "labs#0", "slot": "science-0", "count": 8, "bus_fed": True}, (0, 0)))
    fw = SimpleNamespace(zones=None, log=lambda *a: None)
    base.powered_poles = lambda: [(0, 0)]
    for mod in ("research_station", "labs"):
        with pytest.raises(BuildError, match="floorplan's labs"):
            MODULES[mod].build(fw, base, {"count": 4})


def test_nobody_feeds_the_slot_labs_by_hand():
    from brain.modules import Base, Site
    from brain.upkeep import needs
    base = Base()
    slot = base.add(Site("labs", {"segment_block": "labs#0", "bus_fed": True, "ports": []}, (0, 0)))
    row = base.add(Site("labs", {"count": 2}, (10, 10)))
    crew = SimpleNamespace(base=base, w=SimpleNamespace(research={}), sealed={})
    kinds = lambda s: [n["kind"] for n in needs(crew, s)]
    assert "hands" not in kinds(slot) and "hands" in kinds(row)


def _science_crew(z, base, now=40.0):
    crew = SimpleNamespace(base=base, zones=z, urgent=[], log=lambda *a: None, _minute=lambda: now)
    return crew


def test_the_zones_queue_the_labs_then_the_missing_science_then_retire_the_other_labs():
    from brain import zones
    from brain.modules import Base, Site
    scan, w, z, seg = fresh_zones()
    labs, red, green = _blocks(seg)
    base = Base()
    old = base.add(Site("labs", {"count": 2, "station": True}, (50, 50)))
    crew = _science_crew(z, base)
    assert zones.science_check(crew, z, 40.0) == "" and not crew.urgent       # no science slot yet: nothing
    z.mark_built(green, (0, 0))
    note = zones.science_check(crew, z, 41.0)
    assert [g["module"] for g in crew.urgent] == ["research_station"] and "labs block" in note
    assert zones.science_check(crew, z, 42.0) == "" and len(crew.urgent) == 1   # not again within the gap
    # the labs stand: red's block is missing, so its factory goal comes (it lands in red's slot, onto the bus)
    z.mark_built(labs, (0, 0))
    slot = base.add(Site("labs", {"segment_block": labs.key, "slot": labs.slot.id, "bus_fed": True,
                                  "ports": [{"item": "automation-science-pack", "fed": None},
                                            {"item": "logistic-science-pack", "fed": None}]}, (0, 0)))
    zones.science_check(crew, z, 43.0)
    goals = [(g.get("module"), g.get("item")) for g in crew.urgent]
    assert ("factory", "automation-science-pack") in goals and ("factory", "logistic-science-pack") not in goals
    # retire the other labs only after the slot's labs run fed for RETIRE_AFTER_MIN
    zones.science_check(crew, z, 44.0)
    assert not [g for g in crew.urgent if g["do"] == "remove"]
    for pt in slot.params["ports"]:
        pt["fed"] = ["bus", pt["item"]]
    slot.health = {"working": 6}
    zones.science_check(crew, z, 45.0)
    assert not [g for g in crew.urgent if g["do"] == "remove"]           # running, but not long enough yet
    zones.science_check(crew, z, 45.0 + zones.RETIRE_AFTER_MIN)
    assert [g["site"] for g in crew.urgent if g["do"] == "remove"] == [old.id]


def test_the_build_hook_lays_the_pack_lines_when_the_labs_site_goes_up():
    from brain import zones
    from brain.modules import Base, Site
    scan, w, z, seg = fresh_zones()
    labs, _, _ = _blocks(seg)
    base = Base()
    crew = bus_crew(z, base)
    _labs_world(crew)
    z.mark_built(labs, (0, 0))
    site = base.add(Site("labs", {"segment_block": labs.key, "slot": labs.slot.id, "bus_fed": True,
                                  "ports": [dict(p) for p in zones.slot_build(labs).ports]}, (0, 0)))
    note = zones.after_build(crew, site)
    assert "labs' pack lines" in note
    assert {"automation-science-pack", "logistic-science-pack"} <= set(zones.bus_site(crew, 1).params["laid"])
    assert all(pt["fed"] for pt in site.params["ports"])


def test_once_the_slot_labs_stand_nobody_asks_for_stations_and_the_refusal_names_the_idle_science():
    from brain.modules import Base, ResearchStation, Site
    base = Base()
    assert ResearchStation.slot_labs_refusal(base) is None
    base.add(Site("labs", {"segment_block": "labs#0", "slot": "science-0", "count": 8, "bus_fed": True}, (0, 0)))
    green = base.add(Site("factory", {"item": "logistic-science-pack"}, (5, 5)))
    green.health = {"waiting_for_source_items": 13, "item_ingredient_shortage": 3}
    why = ResearchStation.slot_labs_refusal(base)
    assert f"site {green.id} (logistic-science-pack) make(s) nothing" in why
    green.health = {"working": 10}
    assert "make(s) nothing" not in ResearchStation.slot_labs_refusal(base)
