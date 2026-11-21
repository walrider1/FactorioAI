"""Phases 3-5 of docs/logistics-plan.md: a fed factory is designed beside the supply it draws on, plates already
made are connected before more are mined, planners see every line, one site's inputs are re-laid without
touching the site, and a line carries what its furnaces make."""

import math
from types import SimpleNamespace

import pytest

from brain.modules import MODULES, Site
from brain.nodes import ledger

from test_fuel_and_links import GridWorld, _link_crew
from test_lines import MineWorld, _line_row

F = MODULES["factory"]


class OpenWorld(SimpleNamespace):
    def __init__(self):
        super().__init__(water=set(), cliffs=set(), used=set(), keepout=set(), resources={},
                         seen={(x, y) for x in range(-100, 100) for y in range(-100, 100)}, pos=(0.0, 0.0))


def test_a_fed_factory_is_placed_beside_the_line_it_draws_on_first():
    from brain.modules import Base
    w = OpenWorld()
    base = Base()
    base.add(Site("steam_power", {"engines": 1}, (0, 0), poles=[(0, 0)], powered=True))
    spot = (60.0, 40.0)
    places = F._fed_places(w, base, {"supply_at": [spot]})
    assert math.dist(places[0], spot) <= 24.5                     # beside the line, not by the base centre
    assert any(math.dist(p, (0, 0)) <= 40 for p in places[2:])    # the base's open ground still follows
    w.used |= {(x, y) for x in range(30, 90) for y in range(10, 70)}   # no room by the line: base ground only
    assert all(math.dist(p, spot) > 24.5 for p in F._fed_places(w, base, {"supply_at": [spot]}))


def test_supply_spots_put_lines_with_spare_first_and_skip_a_drawn_line():
    import json
    from arena import catalog
    if catalog.get().empty:
        pytest.skip("needs config/catalog.json")
    from brain.knowledge import Knowledge
    kn = Knowledge(json.loads(json.dumps(catalog.get().data)))
    F.can_make(kn)
    crew = _link_crew(MineWorld())
    crew.kn = kn
    row = _line_row(crew)
    chest_mine = Site("electric_mine", {"resource": "iron-ore", "into": "furnace"}, (50, 50))
    chest_mine.chests = [(50.5, 45.5, "iron-plate")]
    crew.base.add(chest_mine)
    goal = {"item": "electronic-circuit", "per_minute": 20}
    assert crew.supply_spots(goal) == [tuple(row.params["line"]["start"]), (50.5, 45.5)]
    crew.links.append({"source": row.params["line"]["source"], "item": "iron-plate", "kind": "item",
                       "to_site": 9, "port": {"per_minute": 37.5}, "ins": None})
    assert crew.supply_spots(goal) == [(50.5, 45.5)]             # the line gives all it has already


def test_more_mines_wait_while_made_plates_sit_stranded(monkeypatch):
    import brain.assess as assess
    import brain.nodes as nodes
    from brain.supply import shortfalls
    crew = SimpleNamespace()
    monkeypatch.setattr(assess, "flows", lambda c: [{"item": "iron-plate", "made": 100.0, "used": 110.0}])
    monkeypatch.setattr("brain.supply.supply_options", lambda c, item, gap, h=1.3: [{"do": "build"}])
    monkeypatch.setattr(nodes, "stranded", lambda c: [])
    assert shortfalls(crew) and shortfalls(crew)[0][0] == "iron-plate"
    monkeypatch.setattr(nodes, "stranded", lambda c: [{"item": "iron-plate", "per_minute": 75.0}])
    assert shortfalls(crew) == []                                 # 75/min made and never belted: connect it


def test_the_ledger_shows_each_line_with_what_is_drawn_and_who_draws():
    crew = _link_crew(MineWorld())
    row = _line_row(crew)
    crew.links.append({"source": row.params["line"]["source"], "item": "iron-plate", "kind": "item",
                       "to_site": 7, "port": {"per_minute": 30.0}, "ins": None})
    got = ledger(crew)
    assert got == [{"site": row.id, "item": "iron-plate", "per_minute": 37.5, "drawn": 30.0, "spare": 7.5,
                    "feeds_sites": [7], "taps": 0, "start": list(row.params["line"]["start"])}]


def test_relink_lays_one_sites_inputs_again_and_leaves_the_site_standing():
    crew = _link_crew(GridWorld())
    site = crew.base.add(Site("factory", {"item": "x", "ports": [
        {"item": "iron-plate", "end": [10, 0], "approach": [9, 0], "fed": [0.5, 0.5], "per_minute": 10},
        {"item": "copper-plate", "end": [10, 4], "approach": [9, 4], "fed": [0.5, 4.5], "per_minute": 10}]},
        (12, 2)))
    iron, copper = site.params["ports"]
    crew.links = [{"source": (0.5, 0.5), "item": "iron-plate", "kind": "item", "to_site": site.id, "port": iron,
                   "entities": [], "ins": None},
                  {"source": (0.5, 4.5), "item": "copper-plate", "kind": "item", "to_site": site.id, "port": copper,
                   "entities": [], "ins": None}]
    crew._link_fails[(10, 0)] = {"n": 5}
    laid = []
    crew.link = lambda: laid.append(1) or "linked 1"
    msg = crew.relink(site.id, "iron-plate")
    assert "took up 1 link(s) into its iron-plate ports" in msg and laid == [1]
    assert iron["fed"] is None and copper["fed"] == [0.5, 4.5]   # only the input asked for
    assert (10, 0) not in crew._link_fails and crew.base.get(site.id) is site
    from brain.assess import GOAL_DOS
    from brain.brain import GOAL_HELP
    assert "relink" in GOAL_DOS and '"do": "relink"' in GOAL_HELP


def test_a_line_carries_what_its_furnaces_make_and_an_upgrade_raises_it():
    crew = _link_crew(MineWorld())
    row = _line_row(crew)
    ln = row.params["line"]
    row.entities = [("electric-mining-drill", 15, 8, "north"), ("electric-mining-drill", 18, 8, "north"),
                    ("stone-furnace", 15, 3, "north"), ("stone-furnace", 18, 3, "north")]
    assert crew.line_rate(ln, row) == 37.5
    row.entities[3] = ("steel-furnace", 18, 3, "north")          # the drill's 30 ore a minute caps it
    assert crew.line_rate(ln, row) == 18.75 + 30.0
    assert crew._source_rate(tuple(ln["source"]), "iron-plate") == 48.75
    row.health = {"no_minable_resources": 2}
    assert crew.line_rate(ln, row) == 0.0
