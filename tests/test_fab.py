"""The chiplet layout engine, offline: recipe maths, placement, routing, power, the design-rule check,
and the LLM designer's loop (with a fake model). A few seconds; no game needed."""

import json
import random
from pathlib import Path

import pytest

from fab.cells import DIRS, Cell
from fab.check import check
from fab.design import design_factory
from fab.grid import Grid
from fab.recipes import plan

FIXTURE = Path(__file__).parent / "fixtures" / "map_1008.json"


def synth(seed: int = 3) -> Grid:
    rng = random.Random(seed)
    g = Grid()
    for x in range(-60, 60):
        for y in range(-50, 50):
            g.seen.add((x, y))
            if (x + 10) ** 2 / 100 + (y - 32) ** 2 / 36 < 1:
                g.water.add((x, y))
            elif (x + 22) ** 2 + (y + 2) ** 2 < 110:
                g.ore[(x, y)] = "iron-ore"
            elif (x - 22) ** 2 + (y - 6) ** 2 < 50:
                g.ore[(x, y)] = "copper-ore"
            elif rng.random() < 0.03:
                g.obstacles.add((x, y))
    return g


def test_plan_counts_for_green():
    p = plan("logistic-science-pack", 10)
    assert p.machines["logistic-science-pack"] == 3 and p.machines["iron-plate"] == 4
    assert p.rate["iron-plate"] == 55 and p.rate["copper-cable"] == 30
    assert sum(v for k, v in p.machines.items() if "plate" not in k) == 8      # assemblers


def test_mine_cell_drops_into_its_furnace_in_every_rotation():
    for r in range(4):
        drill, furnace = Cell("m", "mine", "iron-plate", ore="iron-ore", x=5, y=7, rot=r).entities()
        vx, vy = DIRS[drill.direction]
        drop = (int((drill.x + vx * 1.85) // 1), int((drill.y + vy * 1.85) // 1))
        assert drop in furnace.tiles


@pytest.mark.parametrize("seed", [1, 4])
def test_design_on_a_synthetic_map_passes_the_rule_check(seed):
    g = synth(seed)
    d = design_factory(g, "logistic-science-pack", 10, near=(0, 0), seed=seed)
    assert d.ok, d.problems
    assert check(d, g) == []
    kit = d.kit()
    assert kit["assembling-machine-1"] == 8 and kit["underground-belt"] <= 20 and kit["transport-belt"] < 300


def test_design_on_the_real_map_snapshot():
    g = Grid.load(FIXTURE)
    d = design_factory(g, "logistic-science-pack", 10, near=(12.0, -26.0))
    assert d.ok, d.problems
    assert check(d, g) == []
    assert "L" in d.render(g)


def test_rule_check_catches_a_belt_feeding_another_net():
    g = synth(1)
    d = design_factory(g, "logistic-science-pack", 10, near=(0, 0))
    r = next(r for r in d.routes if r.path and len(r.path) > 3)
    other = next(o for o in d.routes if o is not r and o.path)
    t = r.path[-1]
    # point the last belt straight at a tile of another net, as a broken router would
    d.routes[d.routes.index(other)].path.append((t[0] + 1, t[1]))
    other.kinds[(t[0] + 1, t[1])] = "belt"
    other.facing[(t[0] + 1, t[1])] = "east"
    r.facing[t] = "east"
    assert any("feeds into" in e or "points into" in e or "overlap" in e for e in check(d, g))


def test_llm_designer_loop_with_a_fake_model():
    g = synth(1)
    ref = design_factory(g, "logistic-science-pack", 10, near=(0, 0))
    x0, y0 = ref.box[:2]
    good = {"note": "copied", "machines": [{"id": c.id, "col": c.x - x0, "row": c.y - y0}
                                           for c in ref.cells if c.kind == "asm"]}

    class Fake:
        def __init__(self):
            self.replies = [json.dumps({"note": "too eager", "machines": [{"id": "A1", "col": 0, "row": 0}]}),
                            json.dumps(good)]
            self.seen = []

        def chat(self, messages):
            self.seen.append([dict(m) for m in messages])   # a copy: the list grows later
            return self.replies.pop(0) if self.replies else json.dumps(good)
    f = Fake()
    d = design_factory(g, "logistic-science-pack", 10, near=(0, 0), placer="llm", llm=f)
    assert d.ok and d.placer == "llm" and check(d, g) == []
    assert "missing machines" in f.seen[1][-1]["content"]          # the validator's feedback went back
    assert "Connections" in f.seen[0][1]["content"] and "D" in f.seen[0][1]["content"]


def test_llm_designer_falls_back_to_search_without_a_model():
    d = design_factory(synth(1), "logistic-science-pack", 10, near=(0, 0), placer="llm", llm=None)
    assert d.ok and d.placer == "search"


def test_underground_needs_a_belt_type():
    from pydantic import TypeAdapter, ValidationError

    from arena.models import Action
    ta = TypeAdapter(Action)
    ok = ta.validate_python({"type": "place", "entity": "underground-belt", "x": 1, "y": 1, "direction": "east",
                             "belt_type": "input"})
    assert ok.belt_type == "input"
    with pytest.raises(ValidationError):
        ta.validate_python({"type": "place", "entity": "underground-belt", "x": 1, "y": 1})
    with pytest.raises(ValidationError):
        ta.validate_python({"type": "place", "entity": "transport-belt", "x": 1, "y": 1, "belt_type": "input"})


def test_realtime_tiers_and_kit(settings):
    rt = settings.tasks["automation-realtime"]
    assert [t.per_minute for t in rt.score.tiers] == [3, 10, 30]
    assert {"craft", "mine"} <= set(rt.verbs) and rt.insert_items == ["coal", "wood"]
    assert rt.start_inventory["coal"] == 2000 and rt.start_inventory["underground-belt"] == 20
    assert "assembling-machine-1" in rt.recipes and "logistic-science-pack" not in rt.recipes


def test_design_on_the_map_where_a_live_run_failed():
    """Map 1012 rebuilt from a live run's log (with the red block already built): mine cells packed with a
    1-tile gap walled each other in. Now they keep 2 tiles and their pins free."""
    g = Grid.load(Path(__file__).parent / "fixtures" / "map_1012_after_tier1.json")
    d = design_factory(g, "logistic-science-pack", 10, near=(-37.5, -10.5))
    assert d.ok, d.problems
    assert check(d, g) == []


def test_chiplet_bench_scores_a_model_against_search(tmp_path):
    """The bench: cases from saved terrain, each placer designs, the rule check judges, a report comes out.
    A model that is unavailable falls back to search inside design_factory; the bench counts that as a fail."""
    from fab.bench import load_cases, run_bench
    g = synth(1)
    g.save(tmp_path / "01_red.grid.json")
    (tmp_path / "01_red.json").write_text(json.dumps({"map": "01_red.grid.json", "item": "logistic-science-pack",
                                                       "per_minute": 10, "near": [0, 0], "outcome": "failed"}))
    cases = load_cases([str(tmp_path)], "automation-science-pack", 5)
    assert [c["name"] for c in cases] == ["01_red"] and cases[0]["per_minute"] == 10   # the map is not a case

    ref = design_factory(g, "logistic-science-pack", 10, near=(0, 0))
    x0, y0 = ref.box[:2]
    good = json.dumps({"note": "copied", "machines": [{"id": c.id, "col": c.x - x0, "row": c.y - y0}
                                                       for c in ref.cells if c.kind == "asm"]})

    class Model:
        model, effort, cost_usd, max_cost = "fake", None, 0.0, 1.0

        def chat(self, messages):
            self.cost_usd += 0.01
            return good
    rep = run_bench(cases, ["search", "llm"], Model(), log=lambda *a: None, out_dir=tmp_path, max_attempts=3)
    assert rep["summary"]["search"]["pass"] == 1 and rep["summary"]["llm"]["pass"] == 1
    assert rep["summary"]["llm"]["usd"] > 0 and (tmp_path / "01_red.llm.txt").exists()

    class Down(Model):
        def chat(self, messages):
            raise RuntimeError("no network")
    rep = run_bench(cases, ["llm"], Down(), log=lambda *a: None)
    assert rep["summary"]["llm"]["pass"] == 0 and "fell back" in rep["rows"][0]["problems"][0]


# ------------------------------------------------------------------ one output belt, one chest (route.OUT_CELL)
@pytest.mark.parametrize("item,rate,supplied,seed", [
    ("electronic-circuit", 30, {"iron-plate", "copper-plate"}, 7),
    ("electronic-circuit", 30, {"iron-plate", "copper-plate"}, 5),
    ("iron-gear-wheel", 30, {"iron-plate"}, 1),
    ("automation-science-pack", 15, {"iron-plate", "copper-plate"}, 2)])
def test_the_product_of_several_machines_rides_one_belt_into_one_chest(item, rate, supplied, seed):
    from fab.route import OUT_CELL
    g = synth(seed)
    d = design_factory(g, item, rate, near=(0, 0), seed=seed, supplied=frozenset(supplied))
    assert d.ok, d.problems
    makers = [c for c in d.cells if c.kind == "asm" and c.product == item]
    out = next(r for r in d.routes if r.item == item)
    assert len(makers) > 1 and len(out.chests) == 1 and out.path          # every machine's output in one chest
    assert sum(p.cell == OUT_CELL for p in out.pins) == 1
    assert {p.cell for p in out.pins} >= {c.id for c in makers}
    errs = [e for e in check(d, g) if "has no power" not in e]           # input ports' power: checked elsewhere
    assert errs == []


def test_a_fast_factory_keeps_a_chest_per_machine():
    g = synth(3)
    d = design_factory(g, "copper-cable", 120, near=(0, 0), seed=3, supplied=frozenset({"copper-plate"}))
    out = next(r for r in d.routes if r.item == "copper-cable")
    assert not out.path and len(out.chests) > 1                      # one inserter could not fill one chest fast enough
