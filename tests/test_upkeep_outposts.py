"""A base that runs on its own between planned trips back, and remote mining when the near patches run out
(no trains: a collector belt, a pole line, a trunk belt and a smelting column)."""

from types import SimpleNamespace

import pytest

from brain import outposts, upkeep
from brain.modules import Site
from brain.world import BuildError
from test_fuel_and_links import GridWorld, _link_crew


def _crew(unlocked=("electric-mining-drill",), researched=("electric-mining-drill",)):
    w = GridWorld()
    crew = _link_crew(w, unlocked=unlocked)
    crew.kn.researched = set(researched)
    crew.kn.resource_name = lambda r: r
    crew.base.powered_poles = lambda exclude=None: [(0, 0)]
    crew.sealed, crew.healthy_since, crew.last_interrupt, crew.closeout_queue = {}, {}, -1e9, []
    crew._patch_full, crew.current = {}, None
    crew.retrofit_due = lambda: []
    crew.power_status = lambda: {"capacity_kw": 9000, "demand_kw": 3000, "stations": 3, "headroom_kw": 6000}
    crew.coal_mines = lambda: []
    minute = {"now": 30.0}
    crew._minute = lambda: minute["now"]
    return crew, w, minute


# ------------------------------------------------------------------ what still needs hands, by the game's curve
def test_hand_fuel_and_burner_drills_are_fine_until_logistics_then_they_go():
    crew, w, _ = _crew()
    burner = crew.base.add(Site("burner_mine", {"resource": "iron-ore", "count": 4}, (0, 0)))
    [n] = upkeep.needs(crew, burner)
    assert n["kind"] == "hands" and upkeep.accepted(crew, n)        # electric drills, but no logistics yet
    crew.kn.unlocked |= {"underground-belt", "long-handed-inserter"}
    assert not upkeep.accepted(crew, n)
    assert n["fix"]["do"] == "replace" and n["fix"]["with"]["module"] == "electric_mine"
    ws = crew.base.add(Site("workshop", {}, (9, 9)))
    assert upkeep.accepted(crew, upkeep.needs(crew, ws)[0])         # the courier is hands on purpose


def test_a_site_that_runs_on_its_own_for_ten_minutes_is_sealed_and_kept():
    crew, w, minute = _crew()
    port = {"item": "iron-plate", "end": [1, 1], "approach": [0, 1], "fed": [3.5, 3.5]}
    f = crew.base.add(Site("factory", {"item": "gear", "ports": [port]}, (5, 5), health={"working": 4}))
    upkeep.update_seals(crew)
    assert not upkeep.is_sealed(crew, f.id)
    minute["now"] = 41.0
    upkeep.update_seals(crew)
    assert upkeep.is_sealed(crew, f.id)
    with pytest.raises(BuildError, match="sealed"):
        crew._sealed_guard(f.id, {"do": "remove", "site": f.id})
    crew._sealed_guard(f.id, {"do": "remove", "site": f.id, "force": True})       # said on purpose
    port["fed"] = None                                                             # broken now: repairs may
    crew._sealed_guard(f.id, {"do": "replace", "site": f.id})                      # replace it
    assert upkeep.is_sealed(crew, f.id)                                            # still sealed meanwhile
    crew.links.append({"source": (3.5, 3.5), "to_site": f.id, "item": "iron-plate", "belts": 5, "minute": 0.0})
    assert not crew._movable(crew.links[0])                       # nobody re-routes a sealed site's belts


def test_a_frontier_build_is_paused_only_for_base_wide_trouble():
    crew, w, minute = _crew()
    crew.current = {"goal": {"do": "build", "module": "outpost", "resource": "iron-ore"}, "urgent": False}
    assert not upkeep.may_interrupt(crew, ["fuel-site-12", "boiler-3,4"])
    assert upkeep.may_interrupt(crew, ["power-capacity"])
    crew.current = {"goal": {"do": "build", "module": "electric_mine"}, "urgent": False}
    assert upkeep.may_interrupt(crew, ["fuel-site-12"])
    crew.last_interrupt = 25.0                                    # paused 5 minutes ago: not again yet
    assert not upkeep.may_interrupt(crew, ["fuel-site-12"])


def test_finishing_a_tier_brings_the_planned_trip_back(monkeypatch):
    crew, w, minute = _crew(unlocked=("electric-mining-drill", "underground-belt", "long-handed-inserter"))
    stage = {"i": 2}
    monkeypatch.setattr(upkeep, "stage_index", lambda c: stage["i"])
    crew.base.add(Site("burner_mine", {"resource": "iron-ore", "count": 4}, (0, 0)))
    crew.base.add(Site("unfinished factory", {}, (20, 0)))
    crew.power_status = lambda: {"capacity_kw": 3600, "demand_kw": 3300, "stations": 2, "headroom_kw": 300}
    assert upkeep.closeout_due(crew) is None                      # the first look only notes where the run is
    stage["i"] = 3
    why = upkeep.closeout_due(crew)
    assert why and "tier 3" in why and upkeep.closeout_due(crew) is None
    goals = upkeep.closeout(crew, why)
    assert [g["do"] for g in goals][:2] == ["remove", "replace"]  # broken first, then what needs hands
    assert {"do": "build", "module": "steam_power", "engines": 2} in [
        {k: v for k, v in g.items() if k != "reflex"} for g in goals]          # and power headroom
    assert all(g["reflex"].startswith("close-out:") for g in goals)
    assert not upkeep.is_frontier(goals[0])


# ------------------------------------------------------------------ outposts
def _ore_world(w, box=(40, 0, 70, 20)):
    w.resources = {"iron-ore": {(x, y) for x in range(box[0], box[2]) for y in range(box[1], box[3])}}
    w.keepout, w.seen = set(), None
    w.kn = SimpleNamespace(unlocked=set())
    w.patches = [{"name": "iron-ore", "tiles": 600, "amount": 900_000, "box": list(box),
                  "centre": {"x": (box[0] + box[2]) / 2, "y": (box[1] + box[3]) / 2}, "distance": 55}]
    w.fuel = lambda x, y, n: None
    w.pos = (0, 0)
    return w


def test_an_outpost_lays_drills_on_one_side_of_a_collector_that_runs_toward_the_base():
    crew, w, _ = _crew()
    _ore_world(w)
    site = outposts.Outpost().build(w, crew.base, {"resource": "iron-ore", "count": 6, "dense": False})
    drills = [e for e in w.placed if e[0] == "electric-mining-drill"]
    belts_ = [e for e in w.placed if e[0] == "transport-belt"]
    # iron: at least OUTPOST_MIN drills asked; the row takes what the patch holds (10 here), 3 belts each
    assert len(drills) == 10 and len(belts_) == 30 and all(e[3] == "west" for e in belts_)   # the base is west
    head = tuple(site.params["head"])
    assert head[0] == min(e[1] for e in belts_) - 0.5 and (head[0] - 1, head[1]) in w.keepout
    assert all(abs(d[2] - 0.5 - (head[1] + 2)) < 1e-9 for d in drills)      # every drill drops on that belt
    assert site.poles and site.params["per_minute"] == 300.0


def test_an_outpost_needs_a_patch_nobody_works():
    crew, w, _ = _crew()
    _ore_world(w)
    crew.base.add(Site("electric_mine", {"resource": "iron-ore", "count": 4}, (55, 10)))
    with pytest.raises(BuildError, match="scout"):
        outposts.Outpost().build(w, crew.base, {"resource": "iron-ore", "count": 6})


def test_the_smelting_column_feeds_ore_and_coal_into_each_furnace_and_plates_onto_one_line():
    crew, w, _ = _crew()
    _ore_world(w)
    # an ore column is a line even before logistics: boxed-in chests no link reaches (run 20261125-164938)
    col = crew.base.add(outposts.SmelterColumn().build(w, crew.base, {"resource": "iron-ore", "count": 4,
                                                                       "toward": [60, 10]}))
    names = [e[0] for e in w.placed]
    assert names.count("stone-furnace") == 4 and names.count("long-handed-inserter") == 4
    assert names.count("inserter") == 8 and col.chests == [] and col.params["into"] == "line"
    assert col.params["line"]["item"] == "iron-plate" and "iron-chest" not in names
    ore, coal = col.params["ports"]
    assert ore["trunk"] and coal["fuel"] and coal["item"] == "coal"
    assert (col, coal) in crew.item_ports() and (col, ore) not in crew.item_ports()   # the outpost feeds that one
    depot = outposts.SmelterColumn().build(w, crew.base, {"resource": "coal", "count": 3})
    assert len(depot.chests) == 3 and depot.chests[0][2] == "coal" and len(depot.params["ports"]) == 1


def test_a_long_trunk_is_routed_in_a_corridor_and_recorded_as_a_link():
    crew, w, _ = _crew()
    _ore_world(w)
    out = crew.base.add(Site("outpost", {"resource": "iron-ore", "head": [300, 40], "head_dir": "west",
                                         "trunk_to": None}, (310, 42)))
    port = {"item": "iron-ore", "end": [0, 9], "approach": [0, 8], "trunk": True, "fed": None}
    col = crew.base.add(Site("smelter_column", {"resource": "iron-ore", "ports": [port]}, (-3, 14)))
    w.look = lambda x, y, radius=32: None
    w.doing = lambda text: None
    w.used |= {(150, y) for y in range(-200, 30)}                 # a wall the corridor goes around
    rec = outposts.lay_trunk(crew, out, col)
    assert rec["kind"] == "trunk" and rec["belts"] > 300 and port["fed"] == [300, 40]
    assert out.params["trunk_to"] == col.id and not crew._movable(rec)
    assert rec["steps"][-1][1] == (0, 9) and rec["steps"][-1][2] == "south"
    assert crew._belt_taps("iron-ore", None) == []                # nobody taps a trunk


def test_the_outlook_prepares_the_next_patch_before_the_drills_stop():
    crew, w, _ = _crew()
    _ore_world(w, box=(-20, -10, 0, 10))
    w.patches[0]["amount"] = 20_000
    w.patches.append({"name": "iron-ore", "tiles": 500, "amount": 800_000, "box": [200, 0, 230, 30],
                      "centre": {"x": 215, "y": 15}, "distance": 215})
    w.rate = lambda r: 120.0 if r == "iron-ore" else 0.0
    crew.base.add(Site("electric_mine", {"resource": "iron-ore", "count": 4}, (-10, 0)))
    crew.patch_full = lambda r: False
    o = outposts.outlook(crew)["iron-ore"]
    assert o["minutes_left"] == 167 and 200 < o["next_patch"]["distance"] < 240
    assert o["fix"] == {"do": "build", "module": "outpost", "resource": "iron-ore", "count": 16}
    w.patches.pop()                                               # nothing else known: go and look
    assert outposts.outlook(crew)["iron-ore"]["fix"] == {"do": "scout", "resource": "iron-ore"}


# ------------------------------------------------------------------ changing what stands
from brain import upgrade  # noqa: E402


def _upgrade_world(crew, w):
    w.inventory = {}
    w.entities = {}
    w.acts = []
    w.look = lambda x, y, radius=24: None
    w.doing = lambda text: None
    w.fuel = lambda x, y, n: w.acts.append(("fuel", x, y))
    w.act = lambda a: (w.acts.append(a), {"ok": True})[1]
    orig_pickup, orig_place = w.pickup, w.place

    def pickup(name, x, y, d="north"):
        w.inventory[name] = w.inventory.get(name, 0) + 1
        return orig_pickup(name, x, y, d)

    def place(name, x, y, d="north", tiles=()):
        w.inventory[name] = w.inventory.get(name, 0) - 1
        return orig_place(name, x, y, d, tiles)
    w.pickup, w.place = pickup, place
    crew.make = lambda kit, extra=False: [w.inventory.__setitem__(k, w.inventory.get(k, 0) + v) for k, v in kit.items()]


def test_faster_machines_go_where_the_old_ones_stood_with_their_recipes():
    crew, w, _ = _crew(unlocked=("assembling-machine-2", "steel-furnace", "fast-inserter"))
    _upgrade_world(crew, w)
    f = crew.base.add(Site("factory", {"item": "iron-gear-wheel"}, (5, 5), health={"working": 3},
                           entities=[("assembling-machine-1", 2.5, 2.5, "north"), ("assembling-machine-1", 6.5, 2.5, "north"),
                                     ("inserter", 4.5, 2.5, "east"), ("transport-belt", 4.5, 4.5, "east")]))
    w.used |= {(2, 2), (6, 2), (4, 2), (4, 4)}
    w.entities = {(2.5, 2.5): {"recipe": "iron-gear-wheel"}, (6.5, 2.5): {"recipe": "iron-gear-wheel"}}
    link = {"source": (0.5, 2.5), "to_site": f.id, "item": "iron-plate", "entities": [("inserter", 4.5, 2.5, "east")],
            "ins": ((4, 2), "east", "inserter")}
    crew.links.append(link)
    msg = crew.upgrade(f.id)
    assert "upgraded 3" in msg
    names = sorted(e[0] for e in f.entities)
    assert names == ["assembling-machine-2", "assembling-machine-2", "fast-inserter", "transport-belt"]
    assert [a["recipe"] for a in w.acts if isinstance(a, dict)] == ["iron-gear-wheel", "iron-gear-wheel"]
    assert link["ins"][2] == "fast-inserter" and link["entities"][0][0] == "fast-inserter"
    assert all(e[1:] in {(2.5, 2.5, "north"), (6.5, 2.5, "north"), (4.5, 2.5, "east"), (4.5, 4.5, "east")}
               for e in f.entities)                                     # same tiles, same facing


def test_only_a_site_that_waits_on_its_machines_is_worth_upgrading():
    crew, w, _ = _crew(unlocked=("steel-furnace",))
    row = crew.base.add(Site("electric_mine", {"resource": "iron-ore"}, (0, 0),
                             entities=[("stone-furnace", 1.0, 1.0, "north")],
                             health={"working": 2, "waiting_for_space_in_destination": 2}))   # drills back up
    starved = crew.base.add(Site("factory", {"item": "x"}, (9, 9), entities=[("stone-furnace", 9.0, 9.0, "north")],
                                 health={"waiting_for_source_items": 5, "working": 1}))
    assert upgrade.upgradable_sites(crew) == [row] and not upgrade.worth_it(starved)
    assert upgrade.best(crew.kn, "assembling-machine-1") is None       # nothing better unlocked


def test_a_sealed_site_may_be_rebuilt_bigger_but_not_into_something_else():
    crew, w, minute = _crew()
    f = crew.base.add(Site("factory", {"item": "electronic-circuit", "per_minute": 20.0}, (5, 5),
                           health={"working": 4}))
    crew.sealed[f.id] = 10.0
    bigger = {"do": "build", "module": "factory", "item": "electronic-circuit", "per_minute": 45}
    other = {"do": "build", "module": "factory", "item": "iron-gear-wheel", "per_minute": 45}
    crew._sealed_guard(f.id, {"do": "replace", "site": f.id, "with": bigger})
    for goal in ({"do": "replace", "site": f.id, "with": other}, {"do": "remove", "site": f.id},
                 {"do": "replace", "site": f.id, "with": {**bigger, "per_minute": 10}}):
        with pytest.raises(BuildError, match="upgrade"):
            crew._sealed_guard(f.id, goal)
    b = Site("burner_mine", {"resource": "coal", "count": 2}, (0, 0))
    assert upgrade.successor_ok(b, {"do": "build", "module": "electric_mine", "resource": "coal", "count": 2})


def test_a_factory_nobody_takes_from_goes_idle_loses_its_seal_and_may_go():
    crew, w, minute = _crew()
    crew.kn.entities = {"transport-belt": {}}
    f = crew.base.add(Site("factory", {"item": "iron-gear-wheel"}, (5, 5), chests=[(7.5, 5.5, "iron-gear-wheel")],
                           health={"full_output": 3, "working": 1}))
    kit = crew.base.add(Site("factory", {"item": "transport-belt"}, (25, 5), chests=[(27.5, 5.5, "transport-belt")],
                             health={"full_output": 4}))
    crew.sealed = {f.id: 1.0, kit.id: 1.0}
    upkeep.update_seals(crew)
    minute["now"] = 30.0 + upgrade.IDLE_MIN
    upkeep.update_seals(crew)
    assert [r["site"] for r in upgrade.idle_sites(crew)] == [f.id]      # the builder takes belts by hand
    assert not upkeep.is_sealed(crew, f.id) and upkeep.status(crew)["idle"][0]["fix"]["do"] == "remove"
    crew.links.append({"source": (7.5, 5.5), "to_site": 99, "item": "iron-gear-wheel"})   # someone takes it now
    upkeep.update_seals(crew)
    assert upgrade.idle_sites(crew) == []


def test_a_better_machine_unlocked_brings_no_close_out_upgrades_are_the_planners_call(monkeypatch):
    """Progress first: faster machines are optional; the close-out no longer stops the run for them."""
    crew, w, minute = _crew(unlocked=("electric-mining-drill",))
    monkeypatch.setattr(upkeep, "stage_index", lambda c: 4)
    crew.base.add(Site("electric_mine", {"resource": "iron-ore"}, (0, 0), entities=[("stone-furnace", 1.0, 1.0, "north")],
                       health={"working": 2}))
    assert upkeep.closeout_due(crew) is None
    crew.kn.unlocked |= {"steel-furnace"}
    assert upkeep.closeout_due(crew) is None
    assert {"do": "upgrade", "site": 1} not in [{k: v for k, v in g.items() if k != "reflex"}
                                               for g in upkeep.closeout(crew, "tier 5 reached")]
