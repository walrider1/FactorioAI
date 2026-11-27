"""Phase 2 of docs/logistics-plan.md: a plate-fed factory takes each plate on one belt straight onto its own (a
feed), not one chest per furnace's worth, and every input port faces the side its supply comes from."""

import json
import math
from types import SimpleNamespace

import pytest

from arena import catalog
from brain.modules import MODULES, Base, Site, line_record

pytestmark = pytest.mark.skipif(catalog.get().empty, reason="needs config/catalog.json (arena catalog)")
F = MODULES["factory"]
READY = SimpleNamespace(unlocked={"underground-belt", "long-handed-inserter"})
PLATES = {"iron-plate", "copper-plate"}


@pytest.fixture(scope="module", autouse=True)
def catalog_loaded():
    from brain.knowledge import Knowledge
    F.can_make(Knowledge(json.loads(json.dumps(catalog.get().data))))


def _grid():
    from fab.grid import Grid
    return Grid(seen={(x, y) for x in range(-50, 50) for y in range(-50, 50)})


def test_a_fed_plate_is_one_feed_not_one_cell_per_furnace():
    from fab import recipes as fr
    old = fr.plan("logistic-science-pack", 20, supplied=PLATES)
    new = fr.plan("logistic-science-pack", 20, supplied=PLATES, feeds=PLATES)
    assert old.machines["iron-plate"] == math.ceil(old.supplied["iron-plate"] / fr.FURNACE_PER_MIN) > 5
    assert new.machines["iron-plate"] == new.machines["copper-plate"] == 1
    assert new.supplied == old.supplied                       # the same plates, on one belt each


def test_green_science_designs_with_two_feeds_on_one_side_where_chests_found_no_room():
    from fab.design import design_factory
    old = design_factory(_grid(), "logistic-science-pack", 20, near=(0.0, 0.0), margin=24, max_ug_pairs=20,
                         supplied=PLATES, max_attempts=6)
    new = design_factory(_grid(), "logistic-science-pack", 20, near=(0.0, 0.0), margin=24, max_ug_pairs=20,
                         supplied=PLATES, max_attempts=6, feeds=PLATES, feed_side="west")
    # nine chests to belt in, from every side (they found no room before the search placer got the slot lattice,
    # fab/slots.py; now they may fit, but every one still needs a belt from outside)
    assert len(old.ports()) >= 8
    assert new.ok, new.problems
    ports = new.ports()
    assert len(ports) == 2 and all(p["feed"] and p["chest"] is None for p in ports)
    assert {p["face"] for p in ports} == {"west"}
    # nothing is built on a feed: the link's belt ends on its end tile and runs on into the factory's belt
    ends = {tuple(p["end"]) for p in ports}
    assert not ends & {t for t, _ in new.inserters()} and "iron-chest" not in {
        e.name for c in new.cells if c.kind == "feed" for e in c.entities()}
    for p in ports:
        route = next(r for r in new.routes if r.item == p["item"])
        e, a = p["end"], p["approach"]
        assert route.path[0] == (2 * e[0] - a[0], 2 * e[1] - a[1])   # straight on from the link's last belt


def test_the_feed_side_points_at_the_nearest_line_of_its_plate():
    base = Base()
    row = Site("electric_mine", {"resource": "iron-ore", "into": "line"}, (-40, 0))
    row.params["line"] = line_record("iron-plate", [(-35, 0), (-34, 0)], "east", (-33, 0), 37.5)
    base.add(row)
    assert F.feed_side(base, (0.0, 0.0), frozenset(PLATES)) == "west"
    assert F.feed_side(base, (-33.0, 30.0), frozenset(PLATES)) == "north"
    assert F.feed_side(Base(), (0.0, 0.0), frozenset(PLATES)) is None
    w = SimpleNamespace(kn=READY)
    assert F.feeds_for(w, {"iron-plate", "steel-plate"}) == {"iron-plate"}     # steel keeps its chest
    assert F.feeds_for(SimpleNamespace(kn=SimpleNamespace(unlocked=set())), PLATES) == frozenset()


def test_a_feed_tile_of_one_item_is_never_taken_by_another_net():
    from fab.cells import Cell
    from fab.place import Net
    from fab.route import Router
    feed = Cell("S1", "feed", "copper-plate", x=0, y=0, rot=0)
    r = Router(_grid(), [feed], (-20, -20, 20, 20))
    pin = feed.pin_sites()[0]
    assert r.claimed[pin.outer] == r.claimed[pin.inserter] == "copper-plate"
    other = Cell("A1", "asm", "iron-gear-wheel", recipe="iron-gear-wheel", x=-1, y=-6)
    r2 = Router(_grid(), [feed, other], (-20, -20, 20, 20))
    route, err = r2.route_net(Net("iron-gear-wheel", ["A1"], []))
    assert err is None and pin.outer not in route.chests


def test_a_link_into_a_feed_registers_without_a_chest():
    from brain.crew import Crew
    from test_fuel_and_links import GridWorld, _link_crew
    crew = _link_crew(GridWorld())
    site = Site("factory", {"item": "x"}, (12, 0))
    site.cells = [{"machine": "input", "in": [(1.5, 1.5)], "port": None}]
    crew.base.add(site)
    port = {"item": "iron-plate", "end": [10, 0], "approach": [9, 0], "fed": None, "chest": None,
            "inserter": None, "feed": True}
    plan = crew._plan_link(port, [(0.5, 0.5, None)], [], 30, False)
    crew._build_link(plan, port, site, "item", None)
    assert port["fed"] == [0.5, 0.5]
    assert plan["steps"][-1][1] == (10, 0) and plan["steps"][-1][2] == "east"   # headed into the factory
    assert Crew._group_fails(["a: b"])
