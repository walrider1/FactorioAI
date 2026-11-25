"""Self-upgrade #3 (run 20261121-140755, minutes 461-827: blue science blocked 260 game minutes). Link kits made
from what is on hand, links of a repair's size and long belts as goals of their own, mines only where their plates
can be belted, science delivery told as it stands, and a handful of pumps made by the crew instead of a factory."""

from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from brain import belts
from brain.assess import relocate_fix
from brain.crew import Crew, Foreman
from brain.modules import ElectricMine, Site
from brain.workshop import Workshop
from brain.world import BuildError
from test_fuel_and_links import GridWorld, _link_crew
from test_upkeep_outposts import _crew, _ore_world


# ------------------------------------------------------------------ kits from what is on hand
def test_a_link_kit_counts_what_is_carried_instead_of_crafting_on_top_of_it():
    crew = Crew.__new__(Crew)
    made = []
    crew.make = lambda kit, extra=False: made.append((kit, extra))
    crew.w = SimpleNamespace(inventory={"underground-belt": 800, "transport-belt": 5})
    crew.kit_hold, crew.reserved = {}, {}
    crew.make_for({"underground-belt": 172, "transport-belt": 40})
    assert made == [({"underground-belt": 172, "transport-belt": 40}, False)]     # a total: 800 on hand cover it
    crew.kit_hold = {"underground-belt": 100}                                      # a build in hand keeps 100
    crew.make_for({"underground-belt": 172})
    assert made[-1] == ({"underground-belt": 272}, False)


def test_a_hoard_of_spare_parts_on_hand_goes_to_storage():
    crew, w, minute = _crew(unlocked=("underground-belt",))
    stashed = []
    crew._stash = lambda item, n, exclude=frozenset(): stashed.append((item, n)) or n
    crew.stored = lambda item: 0
    w.inventory = {"underground-belt": 800}
    fm = Foreman.__new__(Foreman)
    fm.crew = crew
    assert fm._spares() == "770 spare underground-belt into storage" and stashed == [("underground-belt", 770)]


# ------------------------------------------------------------------ links of a repair's size
def test_a_source_beyond_a_links_reach_is_refused_with_a_trunk_goal_named():
    crew = _link_crew(GridWorld())
    site = crew.base.add(Site("factory", {"item": "x"}, (400, 0)))
    port = {"item": "copper-plate", "end": [400, 0], "approach": [399, 0], "fed": None}
    with pytest.raises(BuildError, match='"do": "trunk"') as exc:
        crew._link_port(port, site, [(0.5, 0.5, None)], [], kind="item")
    assert "400 tiles" in str(exc.value)
    assert crew._plan_link(port, [(0.5, 0.5, None)], [], 70, False) is None         # the boost planner too


def test_a_route_that_detours_past_a_links_length_is_left_to_a_trunk():
    # in reach (180 tiles), but going round the wall (inside the 70-tile detour) is ~310 belts, past LINK_ROUTE_MAX
    wall = {(90, y) for y in range(-65, 66)}
    crew = _link_crew(GridWorld(used=wall))
    site = crew.base.add(Site("factory", {"item": "x"}, (181, 0)))
    port = {"item": "iron-plate", "end": [180, 0], "approach": [179, 0], "fed": None}
    with pytest.raises(BuildError, match=f"more than a link's {belts.LINK_ROUTE_MAX}"):
        crew._link_port(port, site, [(0.5, 0.5, None)], [], kind="item")
    assert port["fed"] is None and crew.links == []


def test_a_trunk_goal_lays_one_long_belt_from_the_far_source_into_the_port():
    w = GridWorld()
    w.look = lambda *a, **k: None
    w.doing = lambda *a: None
    crew = _link_crew(w)
    crew._budget = lambda item=None: {}
    crew._fits = lambda budget, root, need, item: True
    src = crew.base.add(Site("electric_mine", {"resource": "copper-ore", "into": "chest"}, (0, 0)))
    src.chests.append((0.5, 0.5, "copper-plate"))
    port = {"item": "copper-plate", "end": [330, 0], "approach": [329, 0], "fed": None}     # past LINK_REACH
    site = crew.base.add(Site("factory", {"item": "copper-cable", "ports": [port]}, (332, 0)))
    msg = crew.trunk(site.id, "copper-plate")
    assert "trunk carries copper-plate" in msg and port["fed"] == [0.5, 0.5]
    [rec] = crew.links
    assert rec["long"] and belts.route_len(rec["steps"]) > belts.LINK_ROUTE_MAX


def test_a_far_patch_gets_no_plate_mine_but_an_outpost_is_named():
    crew, w, _ = _crew()
    _ore_world(w, box=(400, 0, 440, 20))
    w.patch = lambda resource, min_tiles=8: w.patches[0]
    port = {"item": "iron-plate", "end": [0, 0], "approach": [-1, 0], "fed": None}
    crew.base.add(Site("factory", {"item": "iron-gear-wheel", "ports": [port]}, (2, 0)))
    with pytest.raises(BuildError, match='"module": "outpost"'):
        ElectricMine().build(w, crew.base, {"resource": "iron-ore", "count": 2, "into": "furnace"})


# ------------------------------------------------------------------ science delivery told as it stands
def _labs_crew(fails: int):
    crew = _link_crew(GridWorld())
    crew.urgent, crew.plan_queue, crew.current, crew.log = [], [], None, (lambda *a: None)
    crew.base.add(Site("labs", {"station": True, "ports": [{"pack": None, "end": [200, 0], "approach": [199, 0]}]},
                       (205, 0)))
    sci = crew.base.add(Site("factory", {"item": "automation-science-pack"}, (0, 0)))
    sci.chests.append((0.5, 0.5, "automation-science-pack"))
    crew._link_fails[(0.5, 0.5)] = {"n": fails, "item": "automation-science-pack", "site": 1, "reason": "no route",
                                    "minute": 29.0, "base": crew._base_sig()}
    return crew


def test_science_waiting_on_a_back_off_is_not_reported_as_delivered():
    crew = _labs_crew(fails=1)
    with pytest.raises(BuildError, match="reach no research station"):
        crew.link_science()
    assert crew.urgent == []                                     # one failure: retried, no new labs yet


def test_a_science_link_that_failed_three_times_brings_labs_to_the_science():
    crew = _labs_crew(fails=3)
    with pytest.raises(BuildError):
        crew.link_science()
    [goal] = crew.urgent
    assert goal["module"] == "research_station" and goal["do"] == "build"
    with pytest.raises(BuildError):
        crew.link_science()
    assert len(crew.urgent) == 1                                 # queued once


def test_a_sealed_site_whose_inputs_failed_three_times_may_move():
    crew = SimpleNamespace(sealed={7: 10.0})
    labs = Site("labs", {"station": True, "count": 6}, (0, 0))
    labs.id = 7
    crew.base = SimpleNamespace(get=lambda sid: labs if sid == 7 else None)
    assert relocate_fix(crew, {"into_site": 7, "failed": 2, "at": [0, 0]}) is None
    fix = relocate_fix(crew, {"into_site": 7, "failed": 3, "at": [0, 0]})
    assert fix["do"] == "replace" and fix["with"]["module"] == "research_station"
    boost = relocate_fix(crew, {"into_site": 7, "failed": 13, "at": ["boost", 1, 2], "item": "iron-plate"})
    assert boost == {"do": "relink", "site": 7, "item": "iron-plate"}
    stuck = SimpleNamespace(stuck_links=lambda: [{"into_site": 7, "failed": 3}], INPUT_STUCK_FAILS=3)
    assert Crew.inputs_stuck(stuck, 7) and not Crew.inputs_stuck(stuck, 8)


# ------------------------------------------------------------------ a handful of pumps, made by the crew
def _pump_crew(blocked=()):
    made = []
    w = SimpleNamespace(inventory={}, small_needs={"pump": 3}, step=lambda name: nullcontext(), rate=lambda i: 0.1)
    kn = SimpleNamespace(kit_plan=lambda kit, have, supplied=(): SimpleNamespace(blocked=list(blocked)))
    crew = SimpleNamespace(w=w, kn=kn, log=lambda *a: None, stored=lambda item: 0, ready_items=lambda: set(),
                           base=SimpleNamespace(of=lambda kind: []), unlock_items=lambda items: None,
                           make=lambda kit, extra=False: made.append(kit), SMALL_NEED_MAX=20,
                           SMALL_NEED_DEFAULT=Crew.SMALL_NEED_DEFAULT)
    crew.small_need = lambda item: Crew.small_need(crew, item)
    return crew, made


def test_the_pumps_a_line_needs_are_made_instead_of_a_pump_factory():
    crew, made = _pump_crew()
    assert "pump is needed 3x" in Crew._make_instead(crew, "pump") and made == [{"pump": 3}]
    crew.w.small_needs = {"pump": 40}
    assert Crew.small_need(crew, "pump") is None                  # many: a factory may be right
    crew.w.small_needs = {}
    assert Crew.small_need(crew, "pump") == 2 and Crew.small_need(crew, "iron-gear-wheel") is None


def test_pumps_without_engine_units_say_what_to_build_first():
    crew, made = _pump_crew(blocked=["engine-unit"])
    with pytest.raises(BuildError, match="engine-unit") as exc:
        Crew._make_instead(crew, "pump")
    assert "3 pump" in str(exc.value) and not made


def test_a_far_fluid_line_records_how_many_pumps_it_needs():
    w = SimpleNamespace()
    Workshop._need_pumps(w, 3)
    assert w.small_needs == {"pump": 3}
    base = SimpleNamespace(of=lambda kind: [Site("factory", {"item": "engine-unit"}, (0, 0))])
    assert "0.4/min" in Workshop._engine_note(SimpleNamespace(rate=lambda i: 0.4), base)
