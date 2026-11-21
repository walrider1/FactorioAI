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
    g = supply.supply_goal(crew, "iron-plate", 70)            # 70 x 1.3 = 91 a minute at 18.75 a drill
    assert g == {"do": "build", "module": "electric_mine", "resource": "iron-ore", "count": 5, "into": "furnace"}
    assert supply.supply_goal(crew, "copper-plate", 30)["count"] == 3          # same rule, other ore
    assert supply.supply_goal(crew, "coal", 40) == {"do": "build", "module": "electric_mine", "resource": "coal",
                                                    "count": 2, "into": "chest"}
    assert supply.supply_goal(crew, "stone", 50) == {"do": "build", "module": "electric_mine", "resource": "stone",
                                                     "count": 3, "into": "chest"}
    assert supply.supply_goal(crew, "stone-brick", 20)["count"] == 2           # 2 stone a brick: 15 a drill
    assert supply.supply_goal(crew, "steel-plate", 10) == {"do": "build", "module": "smelter_column",
                                                           "resource": "iron-plate", "count": 4}
    gears = supply.supply_goal(crew, "iron-gear-wheel", 20)
    assert gears == {"do": "build", "module": "factory", "item": "iron-gear-wheel", "per_minute": 26.0}
    assert supply.supply_goal(crew, "iron-plate", 500)["count"] == 12          # the biggest row now, more later
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
    crew._patch_full = {"iron-ore": (0.0, crew._base_sig())}     # no room left, and the base has not changed
    monkeypatch.setattr(assess, "expand_fix", lambda c, r: {"do": "scout", "resource": r})
    g = supply.supply_goal(crew, "iron-plate", 70)
    assert g["do"] == "replace" and g["site"] == old.id and g["with"]["module"] == "electric_mine"
    old.kind = "electric_mine"                                     # no burners left on the full patch
    assert supply.supply_goal(crew, "iron-plate", 70) == {"do": "scout", "resource": "iron-ore"}


def test_a_module_the_overseer_forbids_is_skipped_for_the_next_option(kn):
    crew = _crew(kn)
    crew.overseer_forbid = [{"match": {"do": "build", "module": "electric_mine"}, "until": 1e9, "why": "x"}]
    assert supply.supply_goal(crew, "iron-plate", 30)["module"] == "burner_mine"
    crew.overseer_forbid.append({"match": {"do": "build", "module": "burner_mine"}, "until": 1e9, "why": "y"})
    assert supply.supply_goal(crew, "iron-plate", 30) is None


def test_the_alerts_and_the_close_out_use_the_same_sizing_for_every_short_item(kn):
    crew = _crew(kn)
    crew.w.stats = {"items": {"iron-plate": {"made_per_min": 150, "used_per_min": 220},
                              "copper-plate": {"made_per_min": 60, "used_per_min": 40},      # plenty
                              "iron-gear-wheel": {"made_per_min": 10, "used_per_min": 30}}}
    gaps = dict(supply.shortfalls(crew, headroom=1.25))
    assert set(gaps) == {"iron-plate", "iron-gear-wheel"} and gaps["iron-plate"] == 100.0
    fix = assess.supply_fix(crew, "iron-plate", 70)
    assert fix["module"] == "electric_mine" and fix["count"] == 5            # not a burner mine
    crew.power_status = lambda: {"capacity_kw": 9000, "demand_kw": 1000, "stations": 2, "headroom_kw": 8000}
    goals = upkeep.headroom_goals(crew)
    iron = next(g for g in goals if g.get("resource") == "iron-ore")
    assert iron["count"] == 7                                                # (220 x 1.25 - 150) / 18.75
    assert any(g.get("item") == "iron-gear-wheel" for g in goals)
