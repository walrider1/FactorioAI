"""Workshop floors over a run's life (run 20261120-211555: a plastic workshop with no crude counted as 30/min spare,
so nothing replaced it; the workshop built after the old one was removed joined crude pipes that were gone; every
resume started a new floor that needed its own crude line)."""

import pickle
from types import SimpleNamespace

from brain.modules import MODULES, Base, Site
from brain.persist import capture, restore
from brain.workshop import District, Workshop


def _plastic_site(built=100.0):
    cells = [{"machine": "chemical-plant", "recipe": "plastic-bar", "x": 10.5, "y": 20.5, "slot": (0, 1), "floor": 0,
              "item_out": {"plastic-bar": 30.0}, "item_in": {}, "fluid_out": {}, "fluid_in": {"petroleum-gas": 600}}]
    s = Site("workshop", {"item": "plastic-bar", "per_minute": 30}, (10, 20), built_minute=built)
    s.cells = cells
    return s


def test_a_cell_that_has_stood_still_for_20_minutes_counts_for_nothing():
    ws, base = Workshop(), Base()
    site = base.add(_plastic_site())
    w = SimpleNamespace(now_tick=lambda: 150 * 3600)
    assert ws.spare(base, None, w).get("plastic-bar") == 30.0          # never watched yet: counted as before
    site.cell_working = {(10.5, 20.5): 140.0}
    assert ws.spare(base, None, w).get("plastic-bar") == 30.0          # worked 10 minutes ago
    site.cell_working = {(10.5, 20.5): 120.0}
    assert not ws.spare(base, None, w).get("plastic-bar")              # 30 minutes still: nothing to count on


def test_removing_a_workshop_frees_its_slots_pipes_and_pumpjacks():
    ws = Workshop()
    d = District(0, 0, 4, 2)
    d.used[(0, 1)] = -1
    d.used[(1, 2)] = -1                                                 # another site's cell stays
    d.nets.pipe.update({(5, 5): "crude-oil", (6, 5): "crude-oil", (30, 30): "water"})
    d.nets.ptg.update({(7, 5): ("crude-oil", 3), (17, 5): ("crude-oil", 1)})
    d.nets.spans.append(("crude-oil", 0, 5, 7, 17))
    d.nets.pumps.update({(20, 5): "crude-oil", (21, 5): "crude-oil"})
    d.pumpjacks_joined = {(-500.5, -112.5)}
    ws.floors, ws.district = [d], d
    site = _plastic_site()
    site.entities = [("pipe", 5.5, 5.5, "north"), ("pipe", 6.5, 5.5, "north"),
                     ("pipe-to-ground", 7.5, 5.5, "west"), ("pipe-to-ground", 17.5, 5.5, "east"),
                     ("pump", 21.0, 5.5, "east"), ("chemical-plant", 10.5, 20.5, "north")]
    ws.forget(site)
    assert (0, 1) not in d.used and (1, 2) in d.used
    assert d.nets.tiles("crude-oil") == set() and d.nets.pipe == {(30, 30): "water"}
    assert d.nets.ptg == {} and d.nets.spans == [] and d.nets.pumps == {}
    assert d.pumpjacks_joined == set()                                  # no crude pipe reaches it any more


def test_the_floor_comes_back_with_a_resume():
    ws = MODULES["workshop"]
    old = (ws.district, ws.floors)
    try:
        d = District(3, 4, 5, 2)
        d.idx, d.used[(0, 0)] = 0, -1
        d.nets.pipe[(1, 1)] = "water"
        ws.district, ws.floors = d, [d]
        crew = SimpleNamespace(log=print)
        data, dropped = capture(crew, SimpleNamespace(tick=1))
        assert "modules.workshop" not in dropped
        ws.district, ws.floors = None, []
        restore(data, SimpleNamespace(), SimpleNamespace())
        assert ws.district is ws.floors[0] and ws.district.x0 == 3 and ws.district.used == {(0, 0): -1}
        assert ws.district.nets.pipe == {(1, 1): "water"}
    finally:
        ws.district, ws.floors = old


def test_the_patrol_keeps_track_of_which_cells_work():
    import test_oil_scout as t
    crew, w, *_ = t._patrol_crew()
    crew.base.sites[:] = []
    site = crew.base.add(_plastic_site())
    site.entities = [("chemical-plant", 10.5, 20.5, "north")]
    now = {"t": 200 * 3600}
    w.now_tick = lambda: now["t"]
    status = {"s": "fluid_ingredient_shortage"}
    w.observe = lambda radius, x=None, y=None, **kw: {"entities": [{"name": "chemical-plant", "x": 10.5, "y": 20.5,
                                                                    "status": status["s"]}]}
    crew.foreman._sites()
    assert site.cell_working == {(10.5, 20.5): 200.0}                  # seeded at the first look: its grace starts
    now["t"] = 230 * 3600
    crew.foreman._visited.clear()
    crew.foreman._sites()
    assert site.cell_working[(10.5, 20.5)] == 200.0                    # still short of fluid: not refreshed
    status["s"] = "working"
    crew.foreman._visited.clear()
    crew.foreman._sites()
    assert site.cell_working[(10.5, 20.5)] == 230.0
