"""Repairs that fit the cause, mines rebuilt before they run dry, spare parts on hand, and dense ore fields set
aside for big builds (run 20261121-140755: 300 of 570 game minutes went into rework)."""

from types import SimpleNamespace

from brain import outposts, triage, upkeep
from brain.assess import RuleAssessor, rebuild_soon_alerts
from brain.crew import Crew, Foreman
from brain.modules import Site, reserved, thin_ground
from test_upkeep_outposts import _crew, _ore_world


def _mine(crew, count=2, x0=10, y0=10, into="furnace", resource="iron-ore", health=None):
    ents = [("electric-mining-drill", x0 + 3 * k + 0.5, y0 + 0.5, "north") for k in range(count)]
    ents += [("stone-furnace", x0 + 3 * k + 1.0, y0 - 2.0, "north") for k in range(count)]
    return crew.base.add(Site("electric_mine", {"resource": resource, "count": count, "into": into}, (x0, y0),
                              entities=ents, health=dict(health or {"working": count})))


def _amounts(w, tiles, each):
    w.resources = {"iron-ore": set(tiles)}
    w.ore_amount = {t: each for t in tiles}


# ------------------------------------------------------------------ mines rebuilt before they run dry
def test_runway_comes_from_the_ore_under_the_drills_and_what_was_mined_since_the_look():
    crew, w, minute = _crew()
    m = _mine(crew)
    area = {(x, y) for x in range(8, 16) for y in range(8, 13)}
    _amounts(w, area, 100)                         # 40 tiles x 100 under two drills' 5x5 areas (overlapping)
    assert upkeep.mine_rate(m) == 37.5             # two drills slowed to their stone furnaces
    assert upkeep.mine_ore_left(w, m) == 4000
    assert round(upkeep.mine_runway(crew, m)) == round(4000 / 37.5)
    m.params["ore_look"] = 30.0
    minute["now"] = 90.0                           # an hour since the look: an hour of mining comes off
    assert round(upkeep.mine_runway(crew, m)) == round(4000 / 37.5 - 60)


def test_a_mine_low_on_ore_is_rebuilt_ahead_by_the_rule_floor_without_pausing_anything():
    crew, w, _ = _crew()
    m = _mine(crew)
    _amounts(w, {(x, y) for x in range(8, 16) for y in range(8, 13)}, 30)     # 1200 ore: ~32 minutes
    assert "game minutes of ore left" in upkeep.rebuild_due(crew, m)
    assert rebuild_soon_alerts(crew) == []                     # nobody draws on it yet: not worth parts
    m.chests.append((11.5, 5.5, "iron-plate"))
    crew.links.append({"source": (11.5, 5.5), "to_site": 99, "item": "iron-plate", "port": {}, "steps": []})
    [a] = rebuild_soon_alerts(crew)
    assert a["key"] == f"rebuild-{m.id}" and a["fix"]["do"] == "replace" and a["fix"]["early"]
    assert a["fix"]["with"]["module"] == "electric_mine" and a["fix"]["with"]["resource"] == "iron-ore"
    d = RuleAssessor().decide({}, [a], set())
    assert d["urgent_goals"] == [a["fix"]] and not d["interrupt"]     # queued, nothing paused for it
    fuel = {"level": "urgent", "key": "fuel-site-3", "what": "x", "fix": {"do": "feed", "site": 3}}
    assert RuleAssessor().decide({}, [a, fuel], set())["interrupt"]   # fuel still pauses as before


def test_a_third_of_a_row_dry_starts_the_rebuild_but_one_end_drill_or_plenty_of_ore_does_not():
    crew, w, _ = _crew()
    m = _mine(crew, count=6, health={"working": 5, "no_minable_resources": 1})
    assert upkeep.rebuild_due(crew, m) is None             # one end drill of six on bare ground: the row mines on
    m.health = {"working": 4, "no_minable_resources": 2}
    assert "already on mined-out ground" in upkeep.rebuild_due(crew, m)
    full = _mine(crew, count=2, x0=40)
    _amounts(w, {(x, y) for x in range(38, 46) for y in range(8, 13)}, 5000)
    assert upkeep.rebuild_due(crew, full) is None
    half = _mine(crew, count=2, x0=60, health={"no_minable_resources": 1, "working": 1})
    assert upkeep.rebuild_due(crew, half) is None          # mined out already: the depleted alert has it


def test_new_drills_keep_off_thin_ground_and_reserved_fields():
    w = SimpleNamespace(ore_amount={(x, y): 50 for x in range(-2, 3) for y in range(-2, 3)})
    assert thin_ground(w, 0, 0)                            # 1250 ore in its area: rebuilt again within the hour
    w.ore_amount = {(x, y): 400 for x in range(-2, 3) for y in range(-2, 3)}
    assert not thin_ground(w, 0, 0)
    assert not thin_ground(SimpleNamespace(), 0, 0)        # never measured: no verdict
    w.ore_fields = {"plan-iron-ore": [10, 10, 20, 20]}
    assert reserved(w, {(12, 12)}) and not reserved(w, {(30, 30)})
    assert not reserved(w, {(12, 12)}, own="plan-iron-ore")


# ------------------------------------------------------------------ repairs that fit the cause
def _consumer(crew, item="iron-plate", fed=(3.5, 3.5), health=None):
    port = {"item": item, "end": [20, 1], "approach": [19, 1], "fed": list(fed) if fed else None, "per_minute": 60}
    s = crew.base.add(Site("factory", {"item": "gear", "ports": [port]}, (22, 2),
                           health=dict(health or {"waiting_for_source_items": 4})))
    return s, port


def _link(crew, src, site, port, item="iron-plate"):
    ln = {"source": src, "to_site": site.id, "item": item, "port": port, "kind": "item", "steps": [], "entities": []}
    crew.links.append(ln)
    return ln


def test_an_unfed_port_is_a_missing_belt_and_the_relink_is_the_repair():
    crew, w, _ = _crew()
    s, _ = _consumer(crew, fed=None)
    d = triage.diagnose(crew, s.id, "iron-plate")
    assert d["cause"] == "no-link" and d["fix"] is None


def test_a_dry_source_mine_gets_rebuilt_instead_of_relinked():
    crew, w, _ = _crew()
    mine = _mine(crew, health={"no_minable_resources": 2})
    mine.chests.append((11.5, 5.5, "iron-plate"))
    s, port = _consumer(crew)
    _link(crew, (11.5, 5.5), s, port)
    d = triage.diagnose(crew, s.id, "iron-plate")
    assert d["cause"] == "source-dead" and d["fix"]["do"] == "replace" and d["fix"]["site"] == mine.id
    fix, note, _ = triage.route(crew, {"do": "relink", "site": s.id, "item": "iron-plate"})
    assert fix["do"] == "replace" and "mined out" in note


def test_a_source_shared_too_thin_gets_more_supply_not_the_same_belt_again(monkeypatch):
    crew, w, _ = _crew()
    mine = _mine(crew, count=1)
    mine.chests.append((11.5, 5.5, "iron-plate"))
    s, port = _consumer(crew)
    _link(crew, (11.5, 5.5), s, port)             # one drill into a stone furnace (18.75/min) for a 60/min port
    import brain.supply as supply
    monkeypatch.setattr(supply, "supply_goal", lambda c, item, short: {"do": "build", "module": "electric_mine",
                                                                       "resource": "iron-ore", "count": 4})
    d = triage.diagnose(crew, s.id, "iron-plate")
    assert d["cause"] == "source-short" and d["fix"]["module"] == "electric_mine"
    assert "feeds 60/min" in d["why"]


def test_a_relink_that_did_not_help_is_tried_again_from_another_source():
    crew, w, minute = _crew()
    mine = _mine(crew, count=4)
    for k in range(4):
        mine.chests.append((11.5 + 3 * k, 5.5, "iron-plate"))
    s, port = _consumer(crew)
    port["per_minute"] = 10
    _link(crew, (11.5, 5.5), s, port)
    from brain.overseer import same_target
    crew.fix_log = [(repr(same_target({"do": "relink", "site": s.id, "item": "iron-plate"})), 20.0, True)]
    d = triage.diagnose(crew, s.id, "iron-plate")
    assert d["cause"] == "no-effect" and d["avoid"] == [(11.5, 5.5)]
    crew._avoid_roots = {(11.5, 5.5)}
    assert not crew._fits({}, (11.5, 5.5), 10, "iron-plate")      # the link step will not take it again
    assert crew._fits({}, (14.5, 5.5), 10, "iron-plate")
    crew.fix_log = [(crew.fix_log[0][0], -40.0, True)]             # long ago: just a relink again
    assert triage.diagnose(crew, s.id, "iron-plate")["cause"] == "unknown"


def test_a_relink_goal_queues_the_fitting_repair_in_its_place():
    crew, w, _ = _crew()
    mine = _mine(crew, health={"no_minable_resources": 2})
    mine.chests.append((11.5, 5.5, "iron-plate"))
    s, port = _consumer(crew)
    _link(crew, (11.5, 5.5), s, port)
    crew.urgent, logs = [], []
    crew.log = logs.append
    crew.relink = lambda *a, **k: (_ for _ in ()).throw(AssertionError("the relink must not run"))
    msg = Crew.triaged_relink(crew, {"do": "relink", "site": s.id, "item": "iron-plate"})
    assert "goes first instead" in msg and crew.urgent[0]["do"] == "replace"
    assert crew.urgent[0]["reflex"].startswith("triage:")
    Crew.triaged_relink(crew, {"do": "relink", "site": s.id, "item": "iron-plate"})
    assert len(crew.urgent) == 1                                   # queued once, not again


def test_the_planners_see_each_starving_site_and_its_cause():
    crew, w, _ = _crew()
    mine = _mine(crew, health={"no_minable_resources": 2})
    mine.chests.append((11.5, 5.5, "iron-plate"))
    s, port = _consumer(crew)
    _link(crew, (11.5, 5.5), s, port)
    [d] = triage.diagnoses(crew)
    assert d["site"] == s.id and d["cause"] == "source-dead" and d["repair"]["do"] == "replace"


# ------------------------------------------------------------------ spare parts
def test_the_foreman_tops_up_spares_only_from_plates_already_on_hand():
    crew, w, minute = _crew(unlocked=("transport-belt", "inserter"))
    made = []
    crew.make = lambda kit, extra=False: made.append(kit)
    crew.stored = lambda item: 0
    crew.ready_items = lambda: set()
    crew.kn.kit_plan = lambda kit, have, supplied=(): SimpleNamespace(raw={"iron-plate": 3 * sum(kit.values())})
    w.inventory = {"iron-plate": 10}
    fm = Foreman.__new__(Foreman)
    fm.crew = crew
    assert fm._spares() is None and not made                       # not enough iron: no gathering for spares
    w.inventory = {"iron-plate": 1000}
    minute["now"] += 10
    assert fm._spares() == "100 transport-belt" and made == [{"transport-belt": 100}]
    assert fm._spares() is None                                    # one batch per few minutes


# ------------------------------------------------------------------ dense ore fields
def test_a_dense_field_puts_drills_on_both_sides_of_one_belt_with_poles_in_the_gaps():
    crew, w, _ = _crew()
    _ore_world(w, box=(40, 0, 90, 30))
    site = outposts.Outpost().build(w, crew.base, {"resource": "iron-ore", "count": 12})
    drills = [e for e in w.placed if e[0] == "electric-mining-drill"]
    belts_ = [e for e in w.placed if e[0] == "transport-belt"]
    poles = [e for e in w.placed if e[0] == outposts.POLE]
    # an iron outpost is at least OUTPOST_MIN drills (16), whatever was asked
    assert len(drills) == 16 and site.params["dense"] and site.params["count"] == 16
    belt_y = {e[2] for e in belts_}
    assert len(belt_y) == 1 and all(e[3] == "west" for e in belts_)            # one belt toward the base (west)
    by = belt_y.pop()
    north = [d for d in drills if d[3] == "south"]
    south = [d for d in drills if d[3] == "north"]
    assert len(north) == len(south) == 8
    assert all(d[2] == by - 2 for d in north) and all(d[2] == by + 2 for d in south)   # both drop on the belt
    xs = sorted(e[1] for e in belts_)
    assert all(b - a == 1 for a, b in zip(xs, xs[1:]))                          # unbroken
    px = sorted(p[1] for p in poles)
    assert all(b - a <= 7 for a, b in zip(px, px[1:]))                          # within wire reach
    for d in drills:                                                            # every drill in a pole's supply
        assert any(abs(d[1] - p[1]) <= 3.5 and abs(d[2] - p[2]) <= 3.5 for p in poles)
    assert site.params["per_minute"] == 480.0 and site.params["box"]
    assert any(k.startswith("field-iron-ore") for k in w.ore_fields)


def test_the_old_single_row_outpost_is_still_there_on_request():
    crew, w, _ = _crew()
    _ore_world(w)
    outposts.Outpost().build(w, crew.base, {"resource": "iron-ore", "count": 6, "dense": False})
    assert len({e[2] for e in w.placed if e[0] == "electric-mining-drill"}) == 1


def test_fields_are_planned_and_set_aside_once_blue_science_is_researched():
    crew, w, _ = _crew()
    _ore_world(w, box=(40, 0, 90, 30))
    w.patches[0]["tiles"] = 1500
    assert upkeep.field_goals(crew) == []
    crew.kn.researched |= {"chemical-science-pack"}
    goals = upkeep.field_goals(crew)
    iron = next(g for g in goals if g.get("resource") == "iron-ore")
    assert iron["module"] == "outpost" and iron["field"] and iron["dense"] and iron["box"] == [40, 0, 90, 30]
    assert {"do": "scout", "resource": "copper-ore"} in goals                  # none known: look for one
    assert w.ore_fields["plan-iron-ore"] == [40, 0, 90, 30]                    # ordinary mines keep off it
    site = outposts.Outpost().build(w, crew.base, {k: v for k, v in iron.items() if k != "do"})
    assert site.params["field"] and "plan-iron-ore" not in w.ore_fields
    crew.base.add(site)
    assert all(g.get("resource") != "iron-ore" for g in upkeep.field_goals(crew))


def test_a_belt_from_a_source_that_is_gone_is_relinked_from_a_living_one():
    crew, w, _ = _crew()
    s, port = _consumer(crew)
    _link(crew, (99.5, 99.5), s, port)                         # its mine was rebuilt elsewhere
    d = triage.diagnose(crew, s.id, "iron-plate")
    assert d["cause"] == "source-gone" and d["avoid"] == [(99.5, 99.5)] and d["fix"] is None
    [r] = triage.diagnoses(crew)
    assert r["repair"] == {"do": "relink", "site": s.id, "item": "iron-plate"}


def test_a_source_that_looks_big_enough_on_paper_but_starves_itself_counts_as_short():
    crew, w, _ = _crew()
    mine = _mine(crew, count=2, health={"working": 2, "waiting_for_source_items": 2})   # half its machines idle
    mine.chests.append((11.5, 5.5, "iron-plate"))
    s, port = _consumer(crew)
    port["per_minute"] = 15                                    # nominal 18.75/min would cover it
    _link(crew, (11.5, 5.5), s, port)
    d = triage.diagnose(crew, s.id, "iron-plate")
    assert d["cause"] == "source-short" and "gets 9/min" in d["why"]


def test_a_site_that_only_waits_on_its_output_or_a_fluid_gets_no_relink():
    crew, w, _ = _crew()
    mine = _mine(crew, count=4)
    for k in range(4):
        mine.chests.append((11.5 + 3 * k, 5.5, "iron-plate"))
    s, port = _consumer(crew, health={"waiting_for_space_in_destination": 9, "waiting_for_source_items": 2,
                                      "working": 1})
    port["per_minute"] = 5
    _link(crew, (11.5, 5.5), s, port)
    assert triage.diagnose(crew, s.id, "iron-plate")["cause"] == "output-full"
    crew.urgent, crew.log = [], (lambda *a: None)
    crew.relink = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no relink for a full output"))
    assert Crew.triaged_relink(crew, {"do": "relink", "site": s.id, "item": "iron-plate"}).endswith("no relink")
    s.health = {"fluid_ingredient_shortage": 3, "waiting_for_source_items": 1}
    assert triage.diagnose(crew, s.id, "iron-plate")["cause"] == "no-fluid"


def test_a_source_without_fuel_gets_its_coal_input_fixed():
    crew, w, _ = _crew()
    mine = _mine(crew, count=2, health={"working": 1, "no_fuel": 2})
    mine.params["fuel"] = "belt"
    mine.params["ports"] = [{"item": "coal", "end": [5, 5], "approach": [4, 5], "fed": None, "fuel": True}]
    mine.chests.append((11.5, 5.5, "iron-plate"))
    s, port = _consumer(crew)
    _link(crew, (11.5, 5.5), s, port)
    d = triage.diagnose(crew, s.id, "iron-plate")
    assert d["cause"] == "source-unfuelled" and d["fix"] == {"do": "relink", "site": mine.id, "item": "coal"}


def test_a_smelting_column_beside_a_field_stays_off_the_ore():
    crew, w, _ = _crew()
    _ore_world(w, box=(40, 0, 90, 30))
    col = outposts.SmelterColumn().build(w, crew.base, {"resource": "iron-ore", "count": 4, "near": [60, 15],
                                                        "toward": [60, 15]})
    placed = {(int(e[1]), int(e[2])) for e in w.placed}
    assert not placed & w.resources["iron-ore"] and col.params["ports"]


# ------------------------------------------------------------------ fluids out of reach fail before any work
def test_a_workshop_whose_water_is_out_of_reach_fails_before_power_is_built(monkeypatch):
    """Run 20261121-140755: blue science built a steam station for 15 game minutes, then failed on a shore 407 tiles
    away. The design runs first now, so that failure costs nothing."""
    from contextlib import contextmanager
    from brain.modules import MODULES, BuildError
    called = []

    @contextmanager
    def step(name):
        yield
    crew = SimpleNamespace(
        _check_forbidden=lambda g: None, w=SimpleNamespace(step=step), base=None,
        _coal_first=lambda *a: called.append("coal") or "", _power_first=lambda *a: called.append("power") or "",
        _fuel_first=lambda *a: called.append("fuel") or "", _plates_first=lambda *a: called.append("plates") or "")

    def no_water(w, base, p):
        raise BuildError("chemical-science-pack needs water and the nearest shore is 407 tiles from workshop floor 1")
    monkeypatch.setattr(MODULES["workshop"], "kit", no_water)
    try:
        Crew._build(crew, {"do": "build", "module": "workshop", "item": "chemical-science-pack", "per_minute": 12})
        assert False, "the build should fail"
    except BuildError as exc:
        assert "407 tiles" in str(exc)
    assert called == []


def test_a_pump_factory_becomes_the_crew_making_the_pumps_the_line_needs():
    made = []
    crew = SimpleNamespace(_check_forbidden=lambda g: None, small_need=lambda item: 3 if item == "pump" else None,
                           _make_instead=lambda item: made.append(item) or f"made {item}")
    assert Crew._build(crew, {"do": "build", "module": "factory", "item": "pump", "per_minute": 1}) == "made pump"
    assert made == ["pump"]


def test_a_kept_belt_whose_source_came_down_is_spliced_not_relinked():
    crew, w, _ = _crew()
    fac = crew.base.add(Site("factory", {"item": "iron-gear-wheel", "per_minute": 10}, (30, 0),
                             health={"waiting_for_source_items": 2}))
    port = {"item": "iron-plate", "end": [25, 0], "approach": [24, 0], "per_minute": 15.0, "fed": [0.5, 0.5]}
    fac.params["ports"] = [port]
    crew.links = [{"source": (0.5, 0.5), "to_site": fac.id, "item": "iron-plate", "kind": "item", "port": port,
                   "steps": [], "entities": []}]
    crew.gone_roots()[(0.5, 0.5)] = 0.0
    d = triage.diagnose(crew, fac.id, "iron-plate")
    assert d["cause"] == "source-gone" and d["fix"] == {"do": "link"}     # the link step feeds a source in
    fix, note, avoid = triage.route(crew, {"do": "relink", "site": fac.id, "item": "iron-plate"})
    assert fix == {"do": "link"}                                          # no relink: it would unplug its taps
    crew.links.append({"source": (0.5, 8.5), "to_site": fac.id, "item": "iron-plate", "kind": "item",
                       "port": {"item": "iron-plate", "end": [5, 0], "approach": [5, 1], "boost": True},
                       "steps": [], "entities": [], "boost_into": crew.links[0], "boost_of": (0.5, 0.5)})
    assert triage.diagnose(crew, fac.id, "iron-plate")["cause"] != "source-gone"   # spliced: judged by supply


# ------------------------------------------------------------------ dense fields near the base
def test_a_near_field_goes_on_the_free_ground_of_a_patch_we_already_mine():
    crew, w, _ = _crew()
    _ore_world(w, box=(40, 0, 90, 30))
    old = _mine(crew, count=3, x0=42, y0=2)                           # a small row on the patch already
    taken = {(42 + 3 * k + dx, y) for k in range(3) for dx in (-1, 0, 1) for y in range(-1, 4)}
    w.used |= taken                                                   # its drills and furnaces stand there
    assert outposts.worked(crew.base, w.patches[0]) and not outposts.spare_patches(w, crew.base, "iron-ore")
    site = outposts.Outpost().build(w, crew.base, {"resource": "iron-ore", "count": 8, "near_base": True})
    drills = [e for e in w.placed if e[0] == "electric-mining-drill"]
    assert len(drills) == 8 and site.params["near_base"] and site.params["field"]   # sized to the gap, smelted beside
    for d in drills:                                                  # none on the old row's ground
        assert not outposts.Outpost.drill_tiles(int(d[1]), int(d[2])) & taken


def test_a_near_field_with_no_room_says_so_and_never_walks_to_a_far_patch():
    crew, w, _ = _crew()
    _ore_world(w, box=(40, 0, 70, 20))
    w.patches[0]["centre"] = {"x": 900, "y": 0}                        # the only patch is far out
    w.patches[0]["nearest"] = {"x": 880, "y": 0}
    import pytest
    from brain.world import BuildError
    with pytest.raises(BuildError, match="no room"):
        outposts.Outpost().build(w, crew.base, {"resource": "iron-ore", "count": 8, "near_base": True})
    assert not [e for e in w.placed if e[0] == "electric-mining-drill"]


def test_a_small_mine_cleared_for_a_field_goes_back_up_when_the_field_does_not_fit():
    from brain.world import BuildError
    crew, w, _ = _crew()
    old = _mine(crew, count=3)
    done = []
    crew.remove = lambda sid, force=False, keep_poles=True: done.append(("remove", sid)) or f"removed {sid}"

    def build(goal):
        done.append(("build", goal["module"]))
        if goal["module"] == "outpost":
            raise BuildError("no room for a dense iron-ore field")
        return "built the row again"
    crew.build = build
    field = {"do": "build", "module": "outpost", "resource": "iron-ore", "count": 8, "near_base": True}
    import pytest
    with pytest.raises(BuildError, match="did not fit"):
        crew._clear_and_build(old, field)
    assert done == [("remove", old.id), ("build", "outpost"), ("build", "electric_mine")]   # break, try, put back
    assert crew._clear_failed["iron-ore"] == crew._minute()


def test_a_storage_chest_never_takes_an_iron_chest_the_build_in_hand_needs():
    crew, w, _ = _crew()
    crafted = []
    w.inventory = {"iron-chest": 13, "iron-plate": 0, "wood": 10}
    w.craft = lambda recipe, n: crafted.append(recipe) or w.inventory.__setitem__("wooden-chest", 1)
    crew.kit_hold = {"iron-chest": 13}                                # a smelting column's 13 chests
    crew.kn.recipe_for = lambda item, hand=True: item
    crew._storage_spot = lambda avoid: (500, 500)
    try:
        crew._new_storage()
    except Exception:  # noqa: BLE001 - the stub world stops after the chest; what was chosen is what counts
        pass
    assert ("iron-chest", 500.5, 500.5, "north") not in w.placed and crafted == ["wooden-chest"]


def test_a_half_built_site_is_built_again_once_then_only_taken_up():
    from brain.assess import unfinished_alerts
    crew, w, minute = _crew()
    half = crew.base.add(Site("unfinished factory", {"item": "automation-science-pack", "per_minute": 15}, (0, 0),
                              entities=[("assembling-machine-1", 0.5, 0.5, "north")], built_minute=29.5))
    assert unfinished_alerts(crew) == []                               # its own goal may still retry it
    minute["now"] = 33.0
    [a] = unfinished_alerts(crew)
    assert a["key"].startswith("rebuild-") and a["fix"]["do"] == "replace" and a["fix"]["clear_first"]
    assert a["fix"]["with"] == {"do": "build", "module": "factory", "item": "automation-science-pack", "per_minute": 15}
    from brain.assess import unfinished_key
    crew._unfinished_retried = {unfinished_key("factory", {"item": "automation-science-pack", "per_minute": 15})}
    [a] = unfinished_alerts(crew)                                      # tried once already: parts back only
    assert a["fix"] == {"do": "remove", "site": half.id, "force": True}
    col = crew.base.add(Site("unfinished smelter_column", {"resource": "iron-ore", "count": 13}, (5, 5),
                             built_minute=20.0))
    fixes = {x["fix"]["site"]: x["fix"] for x in unfinished_alerts(crew)}
    assert fixes[col.id] == {"do": "remove", "site": col.id, "force": True}   # its outpost builds its column


def test_a_factory_whose_plates_sit_unlinked_in_chests_is_rebuilt_next_to_them(monkeypatch):
    from brain import assess, nodes
    crew, w, _ = _crew()
    fac = crew.base.add(Site("factory", {"item": "automation-science-pack", "per_minute": 15,
                                          "ports": [{"item": "iron-plate", "per_minute": 16.5, "fed": None,
                                                     "end": [75, -2], "approach": [75, -3]}]}, (73, 10)))
    q = {"at": [75, -2], "item": "iron-plate", "into_site": fac.id, "failed": 2, "why": "no belt route"}
    monkeypatch.setattr(nodes, "stranded", lambda c: [{"site": 23, "item": "iron-plate", "chests": 13,
                                                        "per_minute": 244.0}])
    assert assess._strands(crew, q)
    fix = assess.relocate_fix(crew, q)
    assert fix["do"] == "replace" and fix["site"] == fac.id and fix["with"]["item"] == "automation-science-pack"
    monkeypatch.setattr(nodes, "stranded", lambda c: [])            # nothing waiting in chests: retries as before
    assert not assess._strands(crew, q)
