"""One rule for running short of anything: the build that covers the measured gap (with headroom), in the best
module the technology allows. Run 20261120-023103: iron at 150/min against 220/min used for an hour; the only fix
on offer was a burner mine and every iron row was asked for at the habitual 4 drills of 12."""

import json

import pytest

from brain import assess, supply, upkeep
from brain.crew import Crew
from brain.modules import Site
from test_brain_gm import StubWorld


@pytest.fixture(scope="module")
def kn():
    from arena import catalog
    from brain.knowledge import Knowledge
    k = Knowledge(json.loads(json.dumps(catalog.get().data)))
    k.learn_research(["steam-power", "electronics", "automation-science-pack", "automation", "logistics",
                      "electric-mining-drill", "steel-processing"])
    return k


def _crew(kn, electric=True):
    crew = Crew(StubWorld(kn), log=lambda *a: None)
    if electric:
        crew.base.powered_poles = lambda exclude=None: [(0, 0)]
    else:
        crew.base.powered_poles = lambda exclude=None: []
    return crew


def test_any_plate_ore_or_steel_gap_gets_a_build_sized_to_it(kn):
    crew = _crew(kn)
    # once electric drills run, plates and coal come from a dense field near the base (supply.near_field): 70 x
    # 1.3 = 91 ore a minute is 4 drills, and a field is at least NEAR_FIELD_LEAST (4) of them
    g = supply.supply_goal(crew, "iron-plate", 70)
    assert g == {"do": "build", "module": "outpost", "resource": "iron-ore", "count": 4, "near_base": True}
    assert supply.supply_goal(crew, "copper-plate", 30)["count"] == 4          # same rule, other ore
    assert supply.supply_goal(crew, "copper-plate", 150)["count"] == 7         # 195 ore a minute
    assert supply.supply_goal(crew, "coal", 40) == {"do": "build", "module": "outpost", "resource": "coal",
                                                    "count": 4, "near_base": True}
    raw = supply.supply_goal(crew, "copper-ore", 40)                         # raw ore: a compact row into chests
    assert raw["module"] == "electric_mine" and raw["into"] == "chest"
    assert supply.supply_goal(crew, "stone", 50) == {"do": "build", "module": "electric_mine", "resource": "stone",
                                                     "count": 3, "into": "chest"}
    assert supply.supply_goal(crew, "stone-brick", 20)["module"] == "outpost"  # bricks smelted beside the field
    assert supply.supply_goal(crew, "uranium-ore", 20)["module"] == "electric_mine"   # no acid pipes in a field
    assert supply.supply_goal(crew, "steel-plate", 10) == {"do": "build", "module": "smelter_column",
                                                           "resource": "iron-plate", "count": 4}
    gears = supply.supply_goal(crew, "iron-gear-wheel", 20)
    assert gears == {"do": "build", "module": "factory", "item": "iron-gear-wheel", "per_minute": 26.0}
    assert supply.supply_goal(crew, "iron-plate", 500)["count"] == 16          # the biggest field now, more later
    assert supply.supply_goal(crew, "iron-plate", 0) is None
    assert supply.supply_goal(crew, "crude-oil", 30) is None                   # nothing here makes more of it


def test_before_electric_drills_the_same_gap_gets_burner_drills(kn):
    crew = _crew(kn, electric=False)
    g = supply.supply_goal(crew, "iron-plate", 30)             # 39 a minute at 15 a burner drill
    assert g == {"do": "build", "module": "burner_mine", "resource": "iron-ore", "count": 3, "into": "furnace"}
    coal = supply.supply_goal(crew, "coal", 20)                # burners burn some of their own coal
    assert coal["module"] == "burner_mine" and coal["count"] == 3 and coal["into"] == "chest"


def test_a_full_patch_swaps_burners_for_electric_drills_then_goes_farther_out(kn, monkeypatch):
    crew = _crew(kn)
    old = crew.base.add(Site("burner_mine", {"resource": "iron-ore", "count": 4}, (0, 0)))
    crew._patch_full = {"iron-ore": (0.0, crew._patch_sig("iron-ore"))}   # no room left, its mines unchanged
    monkeypatch.setattr(assess, "expand_fix", lambda c, r: {"do": "scout", "resource": r})
    g = supply.supply_goal(crew, "iron-plate", 70)
    assert g["do"] == "replace" and g["site"] == old.id and g["with"]["module"] == "electric_mine"
    old.kind = "electric_mine"                                     # no burners left on the full patch
    # a small electric row on it comes down for a dense field (break before make), before any far trip
    g = supply.supply_goal(crew, "iron-plate", 70)
    assert g["do"] == "replace" and g["site"] == old.id and g["clear_first"] and g["with"]["near_base"]
    crew._clear_failed = {"iron-ore": crew._minute()}             # a clear that just failed: not again for now
    assert supply.supply_goal(crew, "iron-plate", 70) == {"do": "scout", "resource": "iron-ore"}
    crew._clear_failed = {}
    old.params["count"] = 8                                        # a big row stays
    assert supply.supply_goal(crew, "iron-plate", 70) == {"do": "scout", "resource": "iron-ore"}


def test_a_module_the_overseer_forbids_is_skipped_for_the_next_option(kn):
    crew = _crew(kn)
    crew.overseer_forbid = [{"match": {"do": "build", "module": "outpost"}, "until": 1e9, "why": "x"}]
    assert supply.supply_goal(crew, "iron-plate", 30)["module"] == "electric_mine"   # no field: a row
    crew.overseer_forbid.append({"match": {"do": "build", "module": "electric_mine"}, "until": 1e9, "why": "x"})
    # once electric drills run there is no going back to hand-fed burner mines (run 20261124-015025)
    assert supply.supply_goal(crew, "iron-plate", 30) is None
    crew.overseer_forbid = [{"match": {"do": "build", "module": "outpost", "count": 99}, "until": 1e9}]
    assert supply.supply_goal(crew, "iron-plate", 30)["module"] == "outpost"


def test_the_alerts_and_the_close_out_use_the_same_sizing_for_every_short_item(kn):
    crew = _crew(kn)
    crew.w.stats = {"items": {"iron-plate": {"made_per_min": 150, "used_per_min": 220},
                              "copper-plate": {"made_per_min": 60, "used_per_min": 40},      # plenty
                              "iron-gear-wheel": {"made_per_min": 10, "used_per_min": 30}}}
    gaps = dict(supply.shortfalls(crew, headroom=1.25))
    assert set(gaps) == {"iron-plate", "iron-gear-wheel"} and gaps["iron-plate"] == 100.0
    fix = assess.supply_fix(crew, "iron-plate", 70)
    assert fix["module"] == "outpost" and fix["near_base"] and fix["count"] == 4   # not a burner mine
    crew.power_status = lambda: {"capacity_kw": 9000, "demand_kw": 1000, "stations": 2, "headroom_kw": 8000}
    goals = upkeep.headroom_goals(crew)
    iron = next(g for g in goals if g.get("resource") == "iron-ore")
    assert iron["module"] == "outpost" and iron["count"] == 5               # (220 x 1.25 - 150) / 30
    assert any(g.get("item") == "iron-gear-wheel" for g in goals)


def test_red_science_is_automated_only_once_the_games_milestone_says_so(kn):
    from brain import roadmap
    crew = _crew(kn)
    crew.milestone_ladder = [{"id": "red-tech", "tech": "automation-science-pack"},
                             {"id": "red-auto", "item": "automation-science-pack", "per_minute": 5}]
    crew.w.stats = {"items": {"automation-science-pack": {"made_per_min": 12, "hand_made": 300}}}
    crew.w.milestones = {"red-tech": 1}
    # 12 a minute, all of it crafted by the crew: not automated
    assert not roadmap.automated(crew, "automation-science-pack", 5)
    crew.w.milestones["red-auto"] = 2
    assert roadmap.automated(crew, "automation-science-pack", 5)
    crew.milestone_ladder = []                                     # no milestone in the task: less the hand rate
    tick = {"t": 0}
    crew.w.now_tick = lambda: tick["t"]
    crew.w.stats["items"]["automation-science-pack"] = {"made_per_min": 6, "hand_made": 0}
    assert roadmap.automated(crew, "automation-science-pack", 5)           # first reading: no hand rate known
    tick["t"] = 3600 * 2
    crew.w.stats["items"]["automation-science-pack"] = {"made_per_min": 6, "hand_made": 10}   # 5 a minute by hand
    assert not roadmap.automated(crew, "automation-science-pack", 5)


def test_no_field_before_its_column_can_lift_coal_and_early_rows_are_swapped_later(kn):
    crew = _crew(kn)
    crew.kn = type("K", (), {"unlocked": set(kn.unlocked) - {"long-handed-inserter"}, "recipes": kn.recipes,
                             "researched": kn.researched})()
    assert supply.near_field(crew, "iron-ore", 60, "furnace") is None      # a row until automation...
    assert supply.near_field(crew, "coal", 60, "chest") is None             # ...and logistics: belts are walls till then
    crew.kn = kn
    row = crew.base.add(Site("electric_mine", {"resource": "iron-ore", "count": 6, "into": "furnace"}, (0, 0)))
    assert [s.id for s in supply.small_mines(crew, "iron-ore")] == [row.id]
