"""Saved factory designs (docs/hybrid-plan.md, section 2): a factory that was built is kept under
runs/local/designs/<item>/, and the next goal for it reuses the layout on free ground instead of designing again."""
import json
from types import SimpleNamespace

import pytest

from brain import design_memory as dm
from brain import modules
from brain.modules import MODULES
from fab.design import design_factory
from fab.grid import Grid

ITEM, PLATES = "iron-gear-wheel", {"iron-plate"}


def _world(n=90):
    w = SimpleNamespace(seen={(x, y) for x in range(-n, n) for y in range(-n, n)}, water=set(), cliffs=set(),
                        used=set(), keepout=set(), lanes=set(), obstacles=set(), resources={}, movable=set(),
                        pos=(0.0, 0.0), blocking=False, logs=[], kn=SimpleNamespace(unlocked={"underground-belt"}))
    w.log = lambda m: w.logs.append(m)
    w.think = lambda msg, fn, *a, **k: fn(*a, **k)
    w.blocked = lambda t: t in w.water or t in w.cliffs or t in w.used
    return w


@pytest.fixture(scope="module")
def gear_design():
    g = Grid()
    g.seen = {(x, y) for x in range(-40, 40) for y in range(-40, 40)}
    d = design_factory(g, ITEM, 10, near=(0, 0), supplied=set(PLATES))
    assert d.ok, d.problems
    return d


@pytest.fixture
def fac(monkeypatch, tmp_path):
    fac = MODULES["factory"]
    monkeypatch.setattr(fac, "case_dir", tmp_path / "local" / "run1" / "chiplets")
    monkeypatch.setattr(fac, "designs", {})
    monkeypatch.setattr(fac, "probe", None)
    monkeypatch.setattr(fac, "reach", None)
    monkeypatch.setattr(fac, "quick", False)
    monkeypatch.setattr(fac, "_survey", lambda w, f: None)
    monkeypatch.setattr(fac, "_save_case", lambda *a, **k: None)
    monkeypatch.setattr(fac, "supplied_for", lambda kn, p: (set(PLATES), None))
    designed = []

    def design_at(w, base, grid, item, r, focus, pairs, margin, full, supplied):
        designed.append((tuple(focus), r))
        return SimpleNamespace(ok=False, notes=[], problems=["no room for assemblers"])
    monkeypatch.setattr(fac, "_design_at", design_at)
    fac.designed = designed
    yield fac
    del fac.designed


def _ladder(fac, w, rate=10.0, supplied=PLATES, places=((50, 0),)):
    return fac._ladder(w, None, ITEM, rate, set(supplied), list(places), 10, (ITEM, rate), off_ore=True)


def _built(fac, monkeypatch, d, w=None):
    """Factory.build with the game stubbed: the design goes up and is saved."""
    w = w or _world()
    monkeypatch.setattr(fac, "_design", lambda w, base, p: (d, Grid.from_world(w), (0, 0)))
    monkeypatch.setattr(modules, "fab_build", lambda w, d, log, anchors=(): {"placed": 30, "failed": []})
    base = SimpleNamespace(powered_poles=lambda: [(0, 0)])
    site = fac.build(w, base, {"item": ITEM, "per_minute": 10})
    return site, w


def test_a_built_factory_is_saved_relative_to_its_anchor(fac, monkeypatch, tmp_path, gear_design):
    site, w = _built(fac, monkeypatch, gear_design)
    files = list((tmp_path / "local" / "designs" / ITEM).glob("*.json"))
    assert len(files) == 1 and site.params["item"] == ITEM
    rec = json.loads(files[0].read_text(encoding="utf-8"))
    assert rec["inputs"] == ["iron-plate"] and rec["plates"] == "in" and rec["builds"] == 1
    assert rec["kit"]["asm"] == "assembling-machine-1" and rec["ports"]
    back = dm.from_record(rec["layout"], tuple(rec["at"]))
    assert back.tiles() == gear_design.tiles() and back.kit() == gear_design.kit()
    assert [p["item"] for p in back.ports()] == [p["item"] for p in gear_design.ports()]
    _built(fac, monkeypatch, gear_design)                     # the same factory again: one more build
    assert json.loads(files[0].read_text(encoding="utf-8"))["builds"] == 2


def test_the_next_goal_reuses_the_saved_design_on_free_ground(fac, monkeypatch, gear_design):
    _built(fac, monkeypatch, gear_design)
    w = _world()
    d, grid, at = _ladder(fac, w)
    assert fac.designed == []                                 # nothing designed at all
    assert any("reusing the saved iron-gear-wheel design (1 earlier build)" in m for m in w.logs)
    assert d.placer == "memory" and d.kit() == gear_design.kit()
    assert d.tiles() != gear_design.tiles() and len(d.tiles()) == len(gear_design.tiles())
    assert all(t in w.seen and not w.blocked(t) for t in d.tiles())
    assert fac.designs[(ITEM, 10.0)][0] is d


def test_a_rate_within_the_bucket_reuses_and_says_so_a_far_one_designs(fac, monkeypatch, gear_design):
    _built(fac, monkeypatch, gear_design)
    w = _world()
    _ladder(fac, w, rate=12.0)
    assert fac.designed == [] and any("12/min asked (within 0.8x-1.25x)" in m for m in w.logs)
    w = _world()
    _ladder(fac, w, rate=20.0)
    assert fac.designed and not any("reusing" in m for m in w.logs)


def test_a_taken_tile_refuses_the_saved_design(fac, monkeypatch, gear_design):
    _built(fac, monkeypatch, gear_design)
    monkeypatch.setattr(fac, "RECALL_SHIFTS", ((0, 0),))
    rec = dm.load(dm.store_dir(fac.case_dir), ITEM)[0]
    w = _world()
    w.used.add(sorted(dm.place(rec, (50, 0)).tiles())[0])    # one tile of it built on since
    _ladder(fac, w)
    assert fac.designed and not any("reusing" in m for m in w.logs)
    assert any("fit none of 1 places" in m and "taken: 1" in m for m in w.logs)
    w = _world()
    w.lanes.add(sorted(dm.place(rec, (50, 0)).tiles())[-1])  # a lane counts as taken too
    fac.designed.clear()
    _ladder(fac, w)
    assert fac.designed


def test_different_inputs_or_a_failed_route_probe_design_fresh(fac, monkeypatch, gear_design):
    _built(fac, monkeypatch, gear_design)
    w = _world()
    _ladder(fac, w, supplied=PLATES | {"copper-plate"})
    assert fac.designed and not any("reusing" in m for m in w.logs)
    fac.designed.clear()
    monkeypatch.setattr(fac, "probe", lambda grid, d: ["iron-plate: no belt route"])
    w = _world()
    _ladder(fac, w)
    assert fac.designed and any("route probe: 1" in m for m in w.logs)


def test_no_memory_without_a_case_dir(fac, monkeypatch, tmp_path, gear_design):
    monkeypatch.setattr(fac, "case_dir", None)
    _built(fac, monkeypatch, gear_design)
    assert not (tmp_path / "local" / "designs").exists()
    w = _world()
    _ladder(fac, w)
    assert fac.designed and not any("saved" in m for m in w.logs)


def test_underground_belts_and_feeds_are_part_of_the_kit():
    rec = {"inputs": ["iron-plate"], "plates": "in", "per_minute": 10.0,
           "kit": {"ug": True, "feeds": ["iron-plate"], "asm": "assembling-machine-1"}}
    assert dm.why_not(rec, 10, {"iron-plate"}, True, ug=True, feeds={"iron-plate"}) is None
    assert "underground" in dm.why_not(rec, 10, {"iron-plate"}, True, ug=False, feeds={"iron-plate"})
    assert "belts" in dm.why_not(rec, 10, {"iron-plate"}, True, ug=True, feeds=set())
    assert "mines" in dm.why_not(rec, 10, {"iron-plate"}, False, ug=True, feeds={"iron-plate"})
    assert dm.rate_ok(8, 10) and dm.rate_ok(12.5, 10) and not dm.rate_ok(7.9, 10) and not dm.rate_ok(12.6, 10)
