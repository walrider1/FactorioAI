"""Modular factories (run 20261119-093646: six green-science and circuit factories failed, all 'ore taken', because
every factory put its own drills on the only iron and copper near the base). Now a factory is a block with any
inputs, plates come in by default, it goes on open ground off the ore, and electric mines feed its ports."""

import math
import string
from types import SimpleNamespace

import pytest

from arena import catalog
from brain.crew import Crew
from brain.modules import MODULES, Base, Site
from brain.world import BuildError

pytestmark = pytest.mark.skipif(catalog.get().empty, reason="needs config/catalog.json (arena catalog)")
F = MODULES["factory"]


def _kn():
    import json

    from brain.knowledge import Knowledge
    return Knowledge(json.loads(json.dumps(catalog.get().data)))


# ------------------------------------------------------------------ plan and inputs
def test_plates_as_inputs_get_input_cells_and_no_mines():
    from fab import recipes as fr
    F.can_make(_kn())
    pl = fr.plan("logistic-science-pack", 15, supplied={"iron-plate", "copper-plate"})
    assert set(pl.supplied) == {"iron-plate", "copper-plate"}
    # one input cell per furnace's worth (each is belted from one mine chest)
    assert pl.machines["iron-plate"] == math.ceil(pl.supplied["iron-plate"] / fr.FURNACE_PER_MIN)
    mine = fr.plan("logistic-science-pack", 15)
    assert "iron-plate" not in mine.supplied and mine.machines["iron-plate"] >= 1      # the old way still works


def test_a_goal_names_any_inputs_and_plates_come_in_by_default():
    kn = _kn()
    assert {"iron-plate", "copper-plate"} <= F.asked_inputs(kn, {"item": "logistic-science-pack"})
    got = F.asked_inputs(kn, {"item": "logistic-science-pack", "inputs": ["iron-gear-wheel"], "plates": "mine"})
    assert got == {"iron-gear-wheel"}
    supplied, pl = F.supplied_for(kn, {"item": "logistic-science-pack", "per_minute": 10,
                                       "inputs": ["iron-gear-wheel", "electronic-circuit"]})
    assert {"iron-gear-wheel", "electronic-circuit"} <= supplied
    assert "iron-gear-wheel" not in {k for k, v in pl.machines.items() if k not in pl.supplied}   # not made here
    with pytest.raises(BuildError, match="unknown input"):
        F.asked_inputs(kn, {"item": "logistic-science-pack", "inputs": ["unobtainium"]})
    assert F.rate({"per_second": 0.25}) == 15.0 and F.rate({"per_minute": 8}) == 8.0


def test_a_plate_fed_factory_designs_on_ground_with_no_ore_at_all():
    from fab.design import design_factory
    from fab.grid import Grid
    F.can_make(_kn())
    grid = Grid(seen={(x, y) for x in range(-40, 40) for y in range(-40, 40)})
    d = design_factory(grid, "automation-science-pack", 5, near=(0.0, 0.0), margin=24, max_ug_pairs=0,
                       supplied={"iron-plate", "copper-plate"}, max_attempts=4)
    assert d.ok, d.problems
    assert not [c for c in d.cells if c.kind == "mine"]
    assert {pt["item"] for pt in d.ports()} == {"iron-plate", "copper-plate"}


def test_a_plate_fed_factory_stays_where_it_was_asked_not_by_the_ore():
    # run 20261119-112905: asked for (2, -8), built around (-36, -5) between the iron and copper patches
    from fab.design import design_factory
    from fab.grid import Grid
    F.can_make(_kn())
    ore = {(x, y): "iron-ore" for x in range(-70, -55) for y in range(-10, 5)}
    ore.update({(x, y): "copper-ore" for x in range(-70, -55) for y in range(10, 20)})
    grid = Grid(seen={(x, y) for x in range(-80, 40) for y in range(-40, 40)}, ore=ore)
    grid.keepout |= set(ore)
    d = design_factory(grid, "automation-science-pack", 5, near=(0.0, 0.0), margin=24, max_ug_pairs=0,
                       supplied={"iron-plate", "copper-plate"}, max_attempts=4)
    assert d.ok, d.problems
    asm = [c.centre() for c in d.cells if c.kind == "asm"]
    assert asm and all(math.dist(c, (0.0, 0.0)) < 30 for c in asm)


# ------------------------------------------------------------------ where it goes
def test_open_places_keep_off_the_ore():
    ore = {(x, y) for x in range(-10, 40) for y in range(-50, -10)}                  # one big patch north-east
    w = SimpleNamespace(resources={"iron-ore": ore}, water=set(), cliffs=set(), used=set(), keepout=set(),
                        seen={(x, y) for x in range(-150, 150) for y in range(-150, 150)}, pos=(0.0, 0.0))
    places = F._open_places(w, Base())
    assert places
    for x, y in places:
        box = {(int(x) + dx, int(y) + dy) for dx in range(-14, 15) for dy in range(-14, 15)}
        assert len(box & ore) / len(box) < 0.3


# ------------------------------------------------------------------ supply
class SupplyCrew:
    PLATE_ORE = Crew.PLATE_ORE
    _plates_first, _free_outputs, outputs = Crew._plates_first, Crew._free_outputs, Crew.outputs
    lines, line_at, line_rate, spare_rate = Crew.lines, Crew.line_at, Crew.line_rate, Crew.spare_rate
    _budget, _fits, _source_rate, _source_site, _coal_root = (Crew._budget, Crew._fits, Crew._source_rate,
                                                              Crew._source_site, Crew._coal_root)
    COAL_PER_CHEST, DRILL_PER_MIN, FURNACE_PER_MIN = Crew.COAL_PER_CHEST, Crew.DRILL_PER_MIN, Crew.FURNACE_PER_MIN
    STEEL_PER_MIN = Crew.STEEL_PER_MIN
    TAGS = Crew.TAGS

    def __init__(self, kn, chests=0, links=0):
        self.kn, self.base, self.links, self.built = kn, Base(), [], []
        if chests:
            self.base.add(Site("electric_mine", {"resource": "iron-ore"}, (0, 0),
                               chests=[(k + 0.5, 0.5, "iron-plate") for k in range(chests)]))
        self.links = [{"source": (k + 0.5, 0.5)} for k in range(links)]
        self.log = lambda *a: None
        self.foreman = SimpleNamespace(note=lambda *a: None)

    no_field = False

    def build(self, goal):
        if goal["module"] == "outpost" and self.no_field:
            from brain.world import BuildError
            raise BuildError("no room for a dense field")
        self.built.append(goal)
        return f"built {goal['module']} {goal['resource']}"


def test_plates_first_builds_the_mines_a_plate_fed_factory_lacks():
    kn = _kn()
    _, pl = F.supplied_for(kn, {"item": "logistic-science-pack", "per_minute": 15})
    crew = SupplyCrew(kn, chests=3, links=1)                     # 3 iron chests, 1 already belted somewhere
    note = crew._plates_first("factory", {"item": "logistic-science-pack", "per_minute": 15})
    iron = next(g for g in crew.built if g["resource"] == "iron-ore")
    assert iron["module"] == "electric_mine"                       # no long-handed inserters yet: a row
    crew.built.clear()
    kn.unlocked = set(kn.unlocked) | {"long-handed-inserter"}     # automation in: a dense field near the base
    note = crew._plates_first("factory", {"item": "logistic-science-pack", "per_minute": 15})
    iron = next(g for g in crew.built if g["resource"] == "iron-ore")
    assert iron["module"] == "outpost" and iron["near_base"]
    assert any(g["resource"] == "copper-ore" for g in crew.built) and note.startswith("first ")
    crew.built.clear()
    crew.no_field = True                                          # no field fits: a mine row as before
    crew._plates_first("factory", {"item": "logistic-science-pack", "per_minute": 15})
    iron = next(g for g in crew.built if g["resource"] == "iron-ore")
    assert iron["count"] == max(2, pl.machines["iron-plate"] - 2) and iron["into"] == "furnace"
    crew.no_field = False
    crew.built.clear()
    crew._plates_first("factory", {"item": "logistic-science-pack", "per_minute": 15, "plates": "mine"})
    assert crew.built == []                                       # its own mines: nothing to feed


def test_a_mine_plate_chest_feeds_one_port_and_a_factory_chest_two():
    crew = SupplyCrew(_kn(), chests=2, links=1)
    assert len(crew._free_outputs("iron-plate", None)) == 1
    crew.base.add(Site("factory", {"item": "iron-gear-wheel"}, (9, 9), chests=[(9.5, 9.5, "iron-gear-wheel")]))
    crew.links.append({"source": (9.5, 9.5)})
    assert len(crew._free_outputs("iron-gear-wheel", None)) == 1


def test_a_plate_port_with_every_mine_chest_taken_asks_for_more_mines():
    from brain.roadmap import supplier_needed
    crew = SupplyCrew(_kn(), chests=1, links=1)
    port = {"item": "iron-plate", "per_minute": 18.75, "fed": None, "end": (40, 40)}
    crew.base.add(Site("factory", {"item": "automation-science-pack", "ports": [port]}, (40, 40)))
    crew.item_ports = lambda: [(s, pt) for s in crew.base.sites for pt in s.params.get("ports", [])]
    crew._minute = lambda: 100.0
    crew.failed_suppliers = {}
    goal = supplier_needed(crew)
    # more mines on iron (electric or burner: whatever the technology allows, sized by supply.supply_goal)
    assert goal["module"] in ("electric_mine", "burner_mine") and goal["resource"] == "iron-ore"


def test_the_strategist_prompt_still_formats():
    from brain.brain import PROMPT
    fields = {f for _, f, _, _ in string.Formatter().parse(PROMPT) if f}
    text = PROMPT.format(**{f: "" for f in fields})
    assert '"inputs"' in text and "Ore is for drills only" in text


# ------------------------------------------------------------------ run 20261119-112905: built short, redesigned
def _plate_fed_design():
    from fab.design import design_factory
    from fab.grid import Grid
    F.can_make(_kn())
    grid = Grid(seen={(x, y) for x in range(-40, 40) for y in range(-40, 40)})
    d = design_factory(grid, "automation-science-pack", 5, near=(0.0, 0.0), margin=24, max_ug_pairs=0,
                       supplied={"iron-plate", "copper-plate"}, max_attempts=4)
    assert d.ok, d.problems
    return d, grid


def test_the_kit_counts_every_input_cells_chest_and_inserter():
    d, _ = _plate_fed_design()
    need: dict = {}
    for c in d.cells:
        for e in c.entities():
            need[e.name] = need.get(e.name, 0) + 1
    need["inserter"] = need.get("inserter", 0) + len(d.inserters())
    need["iron-chest"] = need.get("iron-chest", 0) + sum(len(r.chests) for r in d.routes)
    kit = d.kit()
    srcs = [c for c in d.cells if c.kind == "src"]
    assert srcs and kit["inserter"] == need["inserter"] and kit["iron-chest"] == need["iron-chest"]
    assert kit["iron-chest"] >= len(srcs) + sum(len(r.chests) for r in d.routes)


def test_a_paused_factory_goal_keeps_its_design_while_the_ground_is_free():
    d, grid = _plate_fed_design()
    w = SimpleNamespace(used=set(), movable=set(), blocked=lambda t: t in w.used, log=lambda *a: None,
                        kn=_kn())
    F.designs[("automation-science-pack", 5.0)] = (d, grid, (0.0, 0.0))
    try:
        assert F._design(w, Base(), {"item": "automation-science-pack", "per_minute": 5})[0] is d
        w.used.add(next(iter(d.tiles())))                     # something got built on it meanwhile
        with pytest.raises(Exception):                        # (a fresh design needs the real world: not here)
            F._design(w, Base(), {"item": "automation-science-pack", "per_minute": 5})
        assert ("automation-science-pack", 5.0) not in F.designs
    finally:
        F.designs.clear()


# ------------------------------------------------------------------ background designs
def _open_world():
    kn = _kn()
    kn.unlocked = set(getattr(kn, "unlocked", set()))
    w = SimpleNamespace(kn=kn, seen={(x, y) for x in range(-100, 100) for y in range(-100, 100)}, water=set(),
                        cliffs=set(), used=set(), keepout=set(), obstacles={}, resources={}, pos=(0.0, 0.0),
                        movable=set(), logs=[])
    w.blocked = lambda t: t in w.used
    w.log = w.logs.append
    w.think = lambda label, fn, *a, **k: fn(*a, **k)
    return w


def test_a_queued_factory_is_designed_in_the_background_and_used_when_its_goal_comes_up():
    w, base = _open_world(), Base()
    goal = {"item": "automation-science-pack", "per_minute": 5}
    F.designer, F.placer = None, "search"
    try:
        assert F.prefetch(w, base, goal)
        assert not F.prefetch(w, base, goal)                   # one at a time, never twice
        F._worker.result(timeout=120)
        d, grid, focus = F._design(w, base, goal)
        assert d.ok and any("in the background" in m for m in w.logs)
        assert ("automation-science-pack", 5.0) not in F.ready
    finally:
        F.designs.clear()
        F.ready.clear()
        F._worker = F._worker_key = None


def test_a_background_design_whose_ground_got_built_on_is_not_used():
    w, base = _open_world(), Base()
    goal = {"item": "automation-science-pack", "per_minute": 5}
    F.designer, F.placer = None, "search"
    try:
        assert F.prefetch(w, base, goal)
        d, _, _, _ = F._worker.result(timeout=120)
        w.used |= set(list(d.tiles())[:3])                      # something else was built there meanwhile
        supplied, _ = F.supplied_for(w.kn, goal)
        assert F._take_ready(w, ("automation-science-pack", 5.0), supplied) is None
        assert any("lost" in m for m in w.logs)
    finally:
        F.designs.clear()
        F.ready.clear()
        F._worker = F._worker_key = None


def test_the_crew_starts_the_next_queued_factory_design_but_not_the_current_goal(monkeypatch):
    started = []
    monkeypatch.setattr(F, "prefetch", lambda w, base, p, probe=None: started.append(p["item"]) or True)
    current = {"do": "build", "module": "factory", "item": "iron-gear-wheel", "per_minute": 6}
    crew = SimpleNamespace(urgent=[current], log=lambda *a: None, w=None, base=None,
                           plan_queue=[{"do": "feed", "site": 2},
                                       {"do": "build", "module": "factory", "item": "electronic-circuit",
                                        "per_minute": 6, "reflex": "assessor"},
                                       {"do": "build", "module": "factory", "item": "logistic-science-pack"}])
    assert Crew.prefetch_designs(crew, current) == "electronic-circuit" and started == ["electronic-circuit"]
