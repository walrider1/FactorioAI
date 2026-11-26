"""What changes when the base changes: links cleaned up when a site goes, inputs matched by rate, rates after an
upgrade, the mall, steel by belt, workshop items moved to belts, room for belts, and the oil balance."""

from types import SimpleNamespace

import pytest

from brain import upgrade, upkeep
from brain.assess import oil_alerts, relocate_fix
from brain.modules import Site
from brain.outposts import SmelterColumn
from brain.roadmap import plate_supply
from test_fuel_and_links import GridWorld
from test_upkeep_outposts import _crew, _ore_world, _upgrade_world


def _link(crew, src, to, item, steps, per=10.0, **extra):
    port = {"item": item, "end": list(steps[-1][1]), "approach": list(steps[-2][1]), "per_minute": per, "fed": None}
    site = crew.base.get(to)
    site.params.setdefault("ports", []).append(port)
    ents = [("transport-belt", t[0] + 0.5, t[1] + 0.5, d) for _, t, d in steps]
    crew.w.used |= {s[1] for s in steps}
    rec = {"source": src, "to_site": to, "item": item, "belts": len(steps), "kind": "item", "port": port,
           "steps": steps, "ins": ((int(src[0]) + 1, int(src[1])), "west", "inserter"), "entities": ents,
           "from": "test", "minute": 0.0, **extra}
    crew._register(rec)
    return rec


def _row(x0, x1, y):
    return [("belt", (x, y), "east") for x in range(x0, x1)]


# ------------------------------------------------------------------ 1. a removed site takes its links with it
def test_removing_a_factory_takes_up_its_links_both_ways_and_frees_its_consumers():
    crew, w, _ = _crew()
    crew.removal_blocker = lambda s: None
    crew.foreman.forget = lambda s: None
    crew.removed = []
    w.pos, w.doing = (0, 0), (lambda text: None)
    mine = crew.base.add(Site("electric_mine", {"resource": "iron-ore", "into": "furnace"}, (0, 0),
                              chests=[(0.5, 0.5, "iron-plate")]))
    gears = crew.base.add(Site("factory", {"item": "iron-gear-wheel", "per_minute": 10}, (20, 0),
                               chests=[(20.5, 5.5, "iron-gear-wheel")]))
    belts_ = crew.base.add(Site("factory", {"item": "transport-belt"}, (40, 0)))
    into = _link(crew, (0.5, 0.5), gears.id, "iron-plate", _row(2, 18, 0))
    out = _link(crew, (20.5, 5.5), belts_.id, "iron-gear-wheel", _row(22, 38, 5))
    tap = _link(crew, (30, 5), belts_.id, "iron-gear-wheel", [("belt", (30, 6), "south"), ("belt", (30, 7), "south")],
                tap_of=out)
    crew.base.add(Site("factory", {"item": "iron-gear-wheel", "per_minute": 10}, (60, 0),   # a spare source to
                       chests=[(60.5, 5.5, "iron-gear-wheel")]))                           # feed in later
    crew.boost_links = lambda: ([], [])          # the splice itself: test_a_replaced_mine_feeds_the_old_belts...
    msg = crew.remove(gears.id)
    # the belt into it comes up with it; the belt out of it (and the tap on that belt) stays standing for a new
    # source to be fed into its side: the consumer keeps its belts
    assert len(crew.links) == 2 and crew.links[0] is out and crew.links[1] is tap
    assert "took up 1 belt link(s)" in msg and "1 belt link(s) out of it stay standing" in msg
    assert out["port"]["fed"] and tap["port"]["fed"]
    assert (20.5, 5.5) in crew.gone_roots() and crew._source_rate((20.5, 5.5), "iron-gear-wheel") == 0.0
    picked = {(round(e[1] - 0.5), round(e[2] - 0.5)) for e in w.picked}
    assert {s[1] for s in into["steps"]} <= picked and not {s[1] for s in out["steps"]} & picked
    assert out["ins_removed"]                                            # its inserter at the old chest comes up
    assert crew._free_outputs("iron-plate", belts_)                      # the mine's chest is free again
    # no new source within SPLICE_WAIT_MIN: taken up after all (with its tap), so the ports get linked afresh
    t0 = crew.gone_roots()[(20.5, 5.5)]
    crew._minute = lambda: t0 + crew.SPLICE_WAIT_MIN + 1
    notes = crew.expire_gone()
    assert crew.links == [] and notes and "2 link(s)" in notes[0] and out["port"]["fed"] is None
    assert crew.gone_roots() == {}


def test_a_replaced_mine_feeds_the_old_belts_and_no_consumer_is_touched():
    crew, w, _ = _crew()
    crew.removal_blocker = lambda s: None
    crew.foreman.forget = lambda s: None
    crew.removed = []
    w.pos, w.doing = (0, 0), (lambda text: None)
    old = crew.base.add(Site("electric_mine", {"resource": "iron-ore", "into": "furnace"}, (0, 0),
                             chests=[(0.5, 0.5, "iron-plate")], entities=[("stone-furnace", 3.0, 3.0, "north")]))
    w.used |= {(0, 0)}
    fac = crew.base.add(Site("factory", {"item": "iron-gear-wheel", "per_minute": 10}, (30, 0)))
    gear = crew.base.add(Site("factory", {"item": "x"}, (30, 20)))
    ln = _link(crew, (0.5, 0.5), fac.id, "iron-plate", _row(2, 25, 0), per=15.0)
    tap = _link(crew, (12, 0), gear.id, "iron-plate", [("belt", (12, 1), "south"), ("belt", (12, 2), "south"),
                                                      ("belt", (12, 3), "south")], per=3.0, tap_of=ln)
    new = crew.base.add(Site("electric_mine", {"resource": "iron-ore", "into": "furnace"}, (0, 8),
                             chests=[(0.5, 8.5, "iron-plate")], entities=[("stone-furnace", 3.0, 11.0, "north")]))
    w.used |= {(0, 8)}
    crew._replacing = old
    try:
        assert "iron-plate" in crew._makes(old)
    finally:
        crew._replacing = None
    msg = crew.remove(old.id, force=True)
    assert "stay standing" in msg
    boosts = [q for q in crew.links if q.get("boost_into") is ln]
    assert len(boosts) == 1 and tuple(boosts[0]["source"]) == (0.5, 8.5)  # the new mine's chest, into the side
    assert crew.spliced((0.5, 0.5)) and ln in crew.links and tap in crew.links
    assert ln["port"]["fed"] and tap["port"]["fed"]                       # neither consumer was relinked
    load, supply = crew._budget("iron-plate")[(0.5, 0.5)]
    assert load == 18.0 and supply == 18.0     # what the old belts lack comes in; the rest stays spare
    crew._minute = lambda: 1e6
    assert crew.expire_gone() == [] and ln in crew.links                  # spliced: stays for good


def test_a_relink_leaves_a_belt_other_factories_tap_and_feeds_more_into_it():
    crew, w, _ = _crew()
    mine = crew.base.add(Site("electric_mine", {"resource": "iron-ore", "into": "furnace"}, (0, 5),
                              chests=[(0.5, 0.5, "iron-plate"), (0.5, 10.5, "iron-plate")],
                              entities=[("stone-furnace", 3.0, 3.0, "north")]))
    w.used |= {(0, 0), (0, 10)}
    fac = crew.base.add(Site("factory", {"item": "iron-gear-wheel", "per_minute": 20}, (30, 0)))
    gear = crew.base.add(Site("factory", {"item": "x"}, (30, 20)))
    ln = _link(crew, (0.5, 0.5), fac.id, "iron-plate", _row(2, 25, 0), per=15.0)
    tap = _link(crew, (12, 0), gear.id, "iron-plate", [("belt", (12, 1), "south"), ("belt", (12, 2), "south"),
                                                      ("belt", (12, 3), "south")], per=10.0, tap_of=ln)
    msg = crew.relink(fac.id, "iron-plate")
    assert ln in crew.links and tap in crew.links and "shared" in msg      # nothing unplugged
    assert [q for q in crew.links if q.get("boost_into") is ln]           # the spare chest fed in instead
    assert not any(e for e in w.picked if (round(e[1] - 0.5), round(e[2] - 0.5)) in {s[1] for s in ln["steps"]})


# ------------------------------------------------------------------ 2. inputs matched by rate, any item
def test_a_plate_belt_feeding_more_than_its_drill_gets_a_spare_chest_fed_in():
    crew, w, _ = _crew()
    mine = crew.base.add(Site("electric_mine", {"resource": "iron-ore", "into": "furnace"}, (0, 5),
                              chests=[(0.5, 0.5, "iron-plate"), (0.5, 10.5, "iron-plate")],
                              entities=[("stone-furnace", 3.0, 3.0, "north")]))
    w.used |= {(0, 0), (0, 10)}
    fac = crew.base.add(Site("factory", {"item": "iron-gear-wheel", "per_minute": 20}, (30, 0)))
    ln = _link(crew, (0.5, 0.5), fac.id, "iron-plate", _row(2, 25, 0), per=40.0)
    assert crew._source_rate((0.5, 0.5), "iron-plate") == 18.75       # one stone furnace's plates
    [c] = crew.short_chains()
    assert c["item"] == "iron-plate" and c["burns_per_min"] == 40 and c["spare_sources"] == 1
    other = crew.base.add(Site("factory", {"item": "x"}, (60, 60)))
    assert crew._belt_taps("iron-plate", other, need=5) == []         # nothing left to lend
    done, failed = crew.boost_links()
    assert len(done) == 1 and crew.links[-1]["boost_into"] is ln and crew._overloaded() == [(0.5, 0.5)]
    mine.entities.append(("steel-furnace", 3.0, 3.0, "north"))       # faster furnaces: 2 x 30 covers 40
    assert crew._overloaded() == []


# ------------------------------------------------------------------ 3. rates after an upgrade
def test_an_upgraded_factory_makes_more_and_asks_more_of_its_inputs():
    crew, w, _ = _crew(unlocked=("assembling-machine-2",))
    _upgrade_world(crew, w)
    port = {"item": "iron-plate", "end": [0, 0], "approach": [-1, 0], "per_minute": 20.0, "fed": None}
    f = crew.base.add(Site("factory", {"item": "iron-gear-wheel", "per_minute": 10.0, "ports": [port]}, (5, 5),
                           health={"working": 2},
                           entities=[("assembling-machine-1", 2.5, 2.5, "north"), ("assembling-machine-1", 6.5, 2.5, "north")]))
    w.entities = {(2.5, 2.5): {"recipe": "iron-gear-wheel"}, (6.5, 2.5): {"recipe": "iron-gear-wheel"}}
    crew.upgrade(f.id)
    assert f.params["per_minute"] == 15.0 and port["per_minute"] == 30.0   # 0.5 -> 0.75 crafting speed
    row = Site("electric_mine", {"resource": "iron-ore", "into": "furnace"}, (0, 0),
               plate_furnaces=[(1, 1, "iron-plate")], entities=[("steel-furnace", 1.0, 1.0, "north")])
    crew.base.add(row)
    assert plate_supply(crew)["iron-plate"] == 30.0                   # one drill caps a steel furnace


# ------------------------------------------------------------------ 4. the mall
def test_kits_come_out_of_factory_chests_and_hand_crafting_builds_a_mall(monkeypatch):
    crew, w, _ = _crew()
    crew.base.add(Site("factory", {"item": "transport-belt", "per_minute": 6}, (5, 5),
                       chests=[(7.5, 5.5, "transport-belt")]))
    crew.base.add(Site("smelter_column", {"resource": "iron-plate"}, (20, 5), chests=[(15.5, 5.5, "steel-plate")]))
    assert {"transport-belt", "steel-plate"} <= crew.ready_items()
    from brain.modules import MODULES
    monkeypatch.setattr(MODULES["factory"], "can_make", lambda kn: ["inserter", "transport-belt", "pipe"])
    crew._factory_failed, crew.failed_suppliers = {}, {}
    w.stats = {"items": {"inserter": {"hand_made": 120}, "transport-belt": {"hand_made": 300},
                         "pipe": {"hand_made": 10}}}
    assert upgrade.mall_goal(crew) == {"do": "build", "module": "factory", "item": "inserter", "per_minute": 6.0}
    crew._factory_failed["inserter"] = 29.0                          # just failed: not again right away
    assert upgrade.mall_goal(crew) is None


# ------------------------------------------------------------------ 5. steel by belt
def test_a_steel_column_takes_iron_plates_by_link_and_puts_steel_in_chests():
    crew, w, _ = _crew()
    _ore_world(w)
    col = crew.base.add(SmelterColumn().build(w, crew.base, {"resource": "iron-plate", "count": 3}))
    plates, coal = col.params["ports"]
    assert not plates.get("trunk") and plates["per_minute"] == 56.2 and coal["fuel"]
    assert (col, plates) in crew.item_ports() and col.chests[0][2] == "steel-plate"
    assert crew._source_rate(col.chests[0][:2], "steel-plate") == 3.75


# ------------------------------------------------------------------ 6. busy workshop items move to belts
def test_a_busy_workshop_item_gets_a_belt_factory_and_its_cell_retires(monkeypatch):
    crew, w, minute = _crew()
    from brain.modules import MODULES
    monkeypatch.setattr(MODULES["factory"], "can_make", lambda kn: ["advanced-circuit"])
    crew._factory_failed, crew.failed_suppliers = {}, {}
    cell = {"machine": "assembling-machine-2", "recipe": "advanced-circuit", "x": 3, "y": 3, "in": [(1, 3)],
            "out": [(5.5, 3.5)], "item_in": {"plastic-bar": 20}, "item_out": {"advanced-circuit": 10.0}}
    ws = crew.base.add(Site("workshop", {}, (3, 3), cells=[cell], chests=[(5.5, 3.5, "advanced-circuit")]))
    assert upgrade.promote_goal(crew) == {"do": "build", "module": "factory", "item": "advanced-circuit",
                                         "per_minute": 12.5}
    fac = crew.base.add(Site("factory", {"item": "advanced-circuit", "per_minute": 12.5}, (30, 3),
                             health={"working": 4}, chests=[(33.5, 3.5, "advanced-circuit")]))
    fac.built_minute = 10.0
    notes = upgrade.retire_promoted(crew)
    assert cell["retired"] and notes and crew.cells() == [] and ws.chests == []
    assert upgrade.promote_goal(crew) is None


# ------------------------------------------------------------------ 7. room for belts
def test_lanes_keep_buildings_off_but_let_belts_through():
    from brain.world import FreeWorld
    w = FreeWorld.__new__(FreeWorld)
    w.lanes, w.movable, w.keepout, w.seen, w.used = {(5, 5)}, set(), set(), {(5, 5), (6, 6)}, set()
    w.blocked = lambda t: t in w.used
    assert not w.free({(5, 5)}) and w.free_belt((5, 5)) and w.free({(6, 6)})


def test_a_site_keeps_a_margin_for_belts_and_gives_it_back_when_it_goes():
    crew, w, _ = _crew()
    w.lanes = set()
    f = crew.base.add(Site("factory", {"item": "x"}, (5, 5), entities=[("assembling-machine-1", 5.5, 5.5, "north")]))
    w.used.add((5, 5))
    crew._reserve_margin(f)
    assert len(w.lanes) == 8 and (5, 5) not in w.lanes
    crew._release_margin(f)
    assert w.lanes == set()


def test_a_walled_in_consumer_is_rebuilt_where_its_inputs_are():
    crew, w, _ = _crew()
    st = crew.base.add(Site("labs", {"station": True, "count": 4}, (5, 5)))
    assert relocate_fix(crew, {"into_site": st.id}) == {"do": "replace", "site": st.id,
                                                       "with": {"do": "build", "module": "research_station",
                                                                "count": 4}}
    crew.sealed[st.id] = 1.0
    assert relocate_fix(crew, {"into_site": st.id}) is None
    crew.base.add(Site("factory", {"item": "automation-science-pack"}, (40, 40), chests=[(40.5, 40.5,
                                                                                          "automation-science-pack")]))
    assert crew._inputs_centre("research_station", {}) == (40.5, 40.5)


# ------------------------------------------------------------------ 8. the oil balance
def test_a_refinery_with_a_full_output_raises_the_oil_balance_alert():
    crew, w, _ = _crew()
    w.entities = {(1, 1): {"name": "oil-refinery", "status": "full_output"}}
    [a] = oil_alerts(crew)
    assert a["key"] == "oil-balance" and a["fix"]["item"] == "solid-fuel"
    w.entities = {(1, 1): {"name": "oil-refinery", "status": "working"}}
    assert oil_alerts(crew) == []


# ------------------------------------------------------------------ oil: a refused pipe, and repairing it
def test_a_refused_placement_looks_again_and_tries_once_more(monkeypatch):
    import automation_bot as ab
    from brain.world import FreeWorld, BuildError as BE
    w = FreeWorld.__new__(FreeWorld)
    w.inventory, w.entities, w.movable = {"pipe-to-ground": 2}, {}, set()
    w.doing, w.near, w.sealed, w.why = (lambda t: None), (lambda x, y: None), (lambda *a: False), (lambda m: m)
    looks, tries = [], []
    w.observe = lambda r, **kw: looks.append(kw)

    def place(self, entity, x, y, direction="north", tiles=()):
        tries.append((x, y))
        if len(tries) == 1:
            raise BE("place pipe-to-ground at (-202.5, -138.5) north: cannot_place ")
        return {"x": x, "y": y}
    monkeypatch.setattr(ab.World, "place", place)
    w.place("pipe-to-ground", -202.5, -138.5, "north", {(-203, -139)})
    assert len(tries) == 2 and looks and w.inventory["pipe-to-ground"] == 1


def test_a_pipe_gap_raises_the_alert_and_the_repair_closes_it():
    from brain.assess import pipe_alerts
    crew, w, _ = _crew()
    _upgrade_world(crew, w)
    ws = crew.base.add(Site("workshop", {"item": "plastic-bar",
                                         "pipe_gaps": [["crude-oil", "ug", [-203, -139], "north"]]}, (0, 0)))
    pj = crew.base.add(Site("pumpjack", {"count": 2}, (-274, -238), health={"waiting_for_space_in_destination": 2},
                            entities=[("pumpjack", -274.5, -238.5, "north")]))
    [a] = pipe_alerts(crew)
    assert a["key"].startswith("pipe") and a["fix"] == {"do": "repair", "site": ws.id}
    assert any(n["fix"] == {"do": "repair", "site": ws.id} for n in upkeep.needs(crew, ws))
    assert "closed 1" in crew.repair(ws.id) and "pipe_gaps" not in ws.params
    assert [a["key"] for a in pipe_alerts(crew)] == [f"crude-stuck-{pj.id}"]   # no gap left: say what else


# ------------------------------------------------------------------ research stations
def test_science_fills_one_station_with_every_colour_before_the_next():
    crew, w, _ = _crew()
    port = lambda: {"end": [0, 0], "approach": [0, 1], "pack": None}
    a = crew.base.add(Site("labs", {"station": True, "ports": [{"end": [0, 0], "approach": [0, 1],
                                                              "pack": "automation-science-pack"}, port()]}, (50, 0)))
    b = crew.base.add(Site("labs", {"station": True, "ports": [port(), port()]}, (5, 0)))
    crew.unlinked_sources = lambda: [(0.5, 0.5, "logistic-science-pack")]
    chosen = []

    def link_port(pt, st, srcs, taps, kind="item", pack=None):
        chosen.append(st.id)
        return {"to_site": st.id, "belts": 3}, ""
    crew._link_port = link_port
    crew.link_science()
    assert chosen == [a.id]                       # farther, but it has red already: green completes it


def test_more_labs_only_when_the_ones_there_are_busy():
    from brain.roadmap import _labs_busy
    crew, w, _ = _crew()
    assert _labs_busy(crew) is None
    crew.base.add(Site("labs", {"station": True}, (0, 0), health={"missing_science_packs": 4, "working": 1}))
    assert _labs_busy(crew) == 0.2


def test_a_station_with_no_science_goes_idle_and_one_missing_a_colour_says_which():
    from brain.assess import station_alerts
    crew, w, minute = _crew()
    st = crew.base.add(Site("labs", {"station": True, "ports": [{"pack": None}]}, (0, 0),
                            health={"missing_science_packs": 4}))
    upgrade.update_idle(crew)
    minute["now"] += upgrade.IDLE_MIN
    assert [r["site"] for r in upgrade.idle_sites(crew)] == [st.id]
    st.params["ports"] = [{"pack": "automation-science-pack"}]
    w.research = {"current": "sulfur-processing"}
    crew.kn.research_cost = lambda t: {"automation-science-pack": 150, "logistic-science-pack": 150}
    [a] = station_alerts(crew)
    assert "logistic-science-pack" in a["what"] and a["fix"] == {"do": "link"}


# ------------------------------------------------------------------ one ore per drill, and long power lines
def test_no_drill_goes_where_it_would_also_mine_a_neighbouring_ore():
    from brain.modules import ElectricMine, foreign_ore, mixes_ore
    from brain.outposts import Outpost
    iron = {(x, y) for x in range(0, 30) for y in range(0, 12)}
    copper = {(x, y) for x in range(0, 30) for y in range(12, 20)}     # touching along y = 12
    used = set()
    w = SimpleNamespace(resources={"iron-ore": iron, "copper-ore": copper, "crude-oil": {(5, 5)}},
                        free=lambda tiles: not (set(tiles) & used))
    foreign = foreign_ore(w, "iron-ore")
    assert (5, 5) not in foreign and mixes_ore(10, 10, foreign) and not mixes_ore(10, 9, foreign)
    for cx, cy in ElectricMine()._rows(w, "iron-ore", 4, "chest", (10, 10)):
        assert not mixes_ore(cx, cy, foreign)                           # 5 x 5 clear of copper
    for cx, cy in Outpost()._row(w, "iron-ore", 4, 1, box=[0, 0, 30, 12]):
        assert not mixes_ore(cx, cy, foreign)


def test_a_long_line_uses_medium_poles_but_meets_small_ones_within_their_reach():
    from brain.power import MEDIUM_REACH, REACH, route_poles
    free = lambda t: True
    small = route_poles([(0, 0)], [(70, 0)], set(), free, margin=4)
    medium = route_poles([(0, 0)], [(70, 0)], set(), free, margin=4, reach=MEDIUM_REACH, old_reach=REACH)
    assert len(medium) < len(small)
    chain = [(0, 0)] + medium + [(70, 0)]
    gaps = [((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5 for a, b in zip(chain, chain[1:])]
    assert gaps[0] <= REACH and gaps[-1] <= REACH and max(gaps) <= MEDIUM_REACH


def test_medium_poles_only_once_they_can_be_made():
    crew, w, _ = _crew(unlocked=("medium-electric-pole",))
    crew.stored = lambda item: 0
    w.inventory = {}
    assert crew._line_pole(200) == "small-electric-pole"            # no steel anywhere
    w.inventory = {"steel-plate": 200}
    assert crew._line_pole(200) == "medium-electric-pole" and crew._line_pole(20) == "small-electric-pole"
    crew.kn.unlocked = set()
    assert crew._line_pole(200) == "small-electric-pole"


def test_a_removed_source_with_nothing_to_feed_in_takes_its_belts_out_at_once():
    """No replacement and no spare source of the item: keeping the belts only starves their factories for
    SPLICE_WAIT_MIN (run 20261124-015025: 5 of 8 kept belt sets), so they come up now and get linked afresh."""
    crew, w, _ = _crew()
    crew.removal_blocker = lambda s: None
    crew.foreman.forget = lambda s: None
    crew.removed = []
    w.pos, w.doing = (0, 0), (lambda text: None)
    gears = crew.base.add(Site("factory", {"item": "iron-gear-wheel", "per_minute": 10}, (20, 0),
                               chests=[(20.5, 5.5, "iron-gear-wheel")]))
    belts_ = crew.base.add(Site("factory", {"item": "transport-belt"}, (40, 0)))
    out = _link(crew, (20.5, 5.5), belts_.id, "iron-gear-wheel", _row(22, 38, 5))
    crew.remove(gears.id)
    assert crew.links == [] and out["port"]["fed"] is None and crew.gone_roots() == {}


# ------------------------------------------------------------------ hand-fed labs retire once a station is belted
def _research(crew, w):
    w.research = {"current": "steel-processing"}
    crew.kn.research_cost = lambda t: {"automation-science-pack": 50}


def test_hand_fed_labs_come_down_once_a_station_gets_every_pack_by_belt():
    from brain.assess import RuleAssessor, retire_alerts
    crew, w, _ = _crew()
    _research(crew, w)
    spawn = crew.base.add(Site("labs", {"count": 2}, (30, 0)))
    st = crew.base.add(Site("labs", {"station": True, "count": 10, "ports": [{"pack": None}]}, (0, 0),
                            powered=True))
    assert retire_alerts(crew) == []                       # the station has no belt yet: the spawn labs stay
    st.params["ports"] = [{"pack": "automation-science-pack"}]
    [a] = retire_alerts(crew)
    assert a["key"] == f"retire-labs-{spawn.id}" and a["fix"] == {"do": "remove", "site": spawn.id}
    got = RuleAssessor().decide({}, [a], set())
    assert got["urgent_goals"] == [a["fix"]] and not got["interrupt"]     # queued, the goal in hand goes on


def test_a_second_station_missing_a_colour_comes_down_after_it_settles():
    from brain.assess import RETIRE_SETTLE_MIN, retire_alerts
    crew, w, minute = _crew()
    _research(crew, w)
    crew.kn.research_cost = lambda t: {"automation-science-pack": 50, "logistic-science-pack": 50}
    both = [{"pack": "automation-science-pack"}, {"pack": "logistic-science-pack"}]
    crew.base.add(Site("labs", {"station": True, "count": 10, "ports": both}, (0, 0), powered=True))
    short = crew.base.add(Site("labs", {"station": True, "count": 2, "ports": [both[0], {"pack": None}]},
                               (40, 0), powered=True, built_minute=minute["now"]))
    assert retire_alerts(crew) == []                       # just built: its last link gets its tries
    minute["now"] += RETIRE_SETTLE_MIN
    [a] = retire_alerts(crew)
    assert a["key"] == f"retire-station-{short.id}" and "logistic-science-pack" in a["what"]
    short.params["count"] = 20                             # a bigger station never comes down for a small one
    assert retire_alerts(crew) == []


def test_the_lab_round_never_empties_a_chest_a_science_belt_starts_at():
    crew, w, _ = _crew()
    w.research = {"current": "steel-processing"}
    fab = crew.base.add(Site("factory", {"item": "automation-science-pack"}, (0, 0),
                             chests=[(1.5, 1.5, "automation-science-pack")]))
    crew.base.add(Site("labs", {"count": 2}, (30, 0), labs=[(30.5, 0.5)],
                       health={"missing_science_packs": 2}))
    crew.kn.techs = {"steel-processing": {"unit_ingredients": [{"name": "automation-science-pack"}]}}
    from brain.crew import Foreman
    fm = Foreman(crew)
    w.inventory = {}
    crew.science_links[(1.5, 1.5)] = {"station": 99, "pack": "automation-science-pack"}
    assert not fm._labs_starving()                         # the packs there are the station's
    del crew.science_links[(1.5, 1.5)]
    assert fm._labs_starving() and fab.chests


# ------------------------------------------------------------------ a line lends only past its last furnace
def _copper_row(crew):
    """Site 19 of run 20261125-214645: six furnaces below a line running east along y = 84, x -24..-9; each
    furnace's output inserter (facing south: it picks from the furnace) drops onto the line at x -24, -21 .. -9."""
    from brain.modules import line_record
    tiles = [(x, 84) for x in range(-24, -8)]
    ln = line_record("copper-plate", tiles, "east", (-8, 84), 112.5)
    ents = [("inserter", x + 0.5, 85.5, "south") for x in range(-24, -8, 3)]          # plates out, onto the line
    ents += [("inserter", x + 1.5, 85.5, "north") for x in range(-24, -8, 3)]         # fuel in, off the line
    return crew.base.add(Site("electric_mine", {"resource": "copper-ore", "line": ln}, (-16, 88), entities=ents)), ln


def test_a_line_lends_to_taps_only_past_its_last_furnace():
    crew, w, _ = _crew()
    site, ln = _copper_row(crew)
    assert crew._line_drops(site, ln) == 15                    # x = -9: the tail
    assert crew.line_taps(site, ln) == [(-9, 84)]             # nothing upstream, where one furnace of six drops
    site.entities = []
    assert crew.line_taps(site, ln) == [(x, 84) for x in range(-14, -9)]   # unknown drops: the last third


def test_a_link_laid_on_the_first_tile_of_a_line_is_taken_up_once_to_be_laid_again():
    crew, w, _ = _crew()
    site, ln = _copper_row(crew)
    fab = crew.base.add(Site("factory", {"item": "automation-science-pack"}, (-30, 40)))
    port = {"item": "copper-plate", "end": (-30, 50), "approach": (-30, 51)}
    rec = {"source": (-24, 84), "to_site": fab.id, "item": "copper-plate", "kind": "item", "port": port,
           "steps": [], "entities": [], "ins": ((-24.5, 84.5), "east", "inserter"), "tap_of": ln}
    crew.links.append(rec)
    taken = []
    crew._unlink = lambda q: (taken.append(q), crew.links.remove(q))
    [q] = crew.starved_taps()
    assert q["at"] == 0 and q["last"] == 15
    [note] = crew._retake_starved()
    assert taken == [rec] and "tile 1 of 16" in note
    crew.links.append(dict(rec))                                # laid there again somehow: not taken up twice
    assert crew._retake_starved() == []


def test_no_pole_goes_on_a_line_head_and_a_starved_tap_stays_while_the_line_has_no_better_spot():
    crew, w, _ = _crew()
    site, ln = _copper_row(crew)
    assert crew.line_heads() == {(-8, 84), (-7, 84), (-6, 84)}
    w.seen, w.blocked, w.keepout = {(-8, 84), (-8, 80)}, (lambda t: False), set()
    ok = crew._pole_freer()
    assert not ok((-8, 84)) and ok((-8, 80))
    fab = crew.base.add(Site("factory", {"item": "automation-science-pack"}, (-30, 40)))
    rec = {"source": (-24, 84), "to_site": fab.id, "item": "copper-plate", "kind": "item",
           "port": {"item": "copper-plate", "end": (-30, 50), "approach": (-30, 51)},
           "steps": [], "entities": [], "ins": ((-24.5, 84.5), "east", "inserter")}
    crew.links.append(rec)
    crew._belt_free = lambda t: False                           # a pole on its start, the tail boxed in
    assert crew._retake_starved() == [] and crew.links == [rec]
