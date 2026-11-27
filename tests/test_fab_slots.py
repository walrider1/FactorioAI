"""The LLM placer's lane template (slot lattice), three layouts per reply, and repair rounds, with a fake model.
Small synthetic plate-fed grids; a few seconds; no game, no network."""

import json
import re

from fab import design as fab_design
from fab.cells import Cell
from fab.check import check
from fab.design import design_factory
from fab.grid import Grid
from fab.place import dilate
from fab.slots import ROW_PITCH, blocked_by, lattice

PLATES = {"iron-plate", "copper-plate"}


def open_grid(w: int = 70, h: int = 70, water: set = frozenset()) -> Grid:
    g = Grid()
    for x in range(-w // 2, w // 2):
        for y in range(-h // 2, h // 2):
            g.seen.add((x, y))
    g.water |= set(water)
    return g


def drc(d, g):
    """check(), minus its false alarms for input cells (no drill tiles; a feed's end tile is never built)."""
    inputs = {c.id for c in d.cells if c.kind in ("src", "feed")}
    ends = {str(s.inserter) for c in d.cells if c.kind == "feed" for s in c.pin_sites()}
    return [e for e in check(d, g) if not (e.endswith(" has no power") and
                                           (e.split()[0] in inputs or e[9:-13] in ends))]


class Fake:
    """A deterministic model: replies are functions of the messages so far (or plain strings)."""
    model, effort, max_cost = "fake", None, 10.0

    def __init__(self, *replies):
        self.replies = list(replies)
        self.seen = []
        self.cost_usd = 0.0

    def chat(self, messages):
        self.seen.append([dict(m) for m in messages])
        self.cost_usd += 0.01
        r = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return r(messages) if callable(r) else r


def slot_ids(messages) -> list[str]:
    """The slot ids listed in the first user message, in order."""
    return re.findall(r"\b(P\d+) (?:col|row) \d+", messages[1]["content"])


def test_slots_keep_corridors_and_stay_clear_of_input_cells():
    g = open_grid(water={(x, y) for x in range(-4, 2) for y in range(-4, 2)})      # a pond in the middle
    box = (-24, -24, 24, 24)
    src = Cell("S1", "src", "iron-plate", x=-20, y=10, rot=1)                      # port facing west
    feed = Cell("S2", "feed", "copper-plate", x=-20, y=-5, rot=1)
    lat = lattice(g, box, [src, feed], need=6)
    assert lat is not None and len(lat.slots) >= 6
    assert lat.orient == "h"                     # ports face west: rows (and corridors) run east-west
    near = dilate(blocked_by([src, feed]), 2)
    corridors = lat.corridors()
    seen = set()
    for s in lat.slots:
        fp = s.footprint()
        assert all(g.free(t) for t in fp) and not fp & near and not fp & corridors and not fp & seen
        seen |= fp
    # rows are ROW_PITCH apart: a 4-tile corridor (inserter, two belts, inserter) between machine rows
    ys = sorted({s.y for s in lat.slots})
    assert all((b - a) % ROW_PITCH == 0 for a, b in zip(ys, ys[1:]))
    for y in range(ys[0] + 3, ys[0] + ROW_PITCH):
        assert all((x, y) in corridors for x in range(box[0], box[2] + 1))
    # neighbours in a row keep a gap for belts and poles
    for a in lat.slots:
        for b in lat.slots:
            if a is not b and a.line == b.line:
                assert abs(a.x - b.x) >= 5
    # too many machines for the box: no lattice (the placer falls back to free col/row placement)
    assert lattice(g, (-6, -6, 6, 6), [], need=6) is None


def test_three_layouts_first_invalid_second_routes():
    g = open_grid()

    def reply(messages):
        p = slot_ids(messages)
        return json.dumps({"layouts": [
            {"note": "bad", "machines": [{"id": "A1", "slot": p[0]}, {"id": "A2", "slot": p[0]},
                                         {"id": "A3", "slot": "P999"}]},
            {"note": "a row", "machines": [{"id": f"A{k + 1}", "slot": p[k]} for k in range(4)]},
            {"note": "spread", "machines": [{"id": f"A{k + 1}", "slot": p[2 * k]} for k in range(4)]}]})
    f = Fake(reply)
    d = design_factory(g, "electronic-circuit", 20, near=(0, 0), placer="llm", llm=f, supplied=PLATES,
                       feeds=PLATES, feed_side="west", max_attempts=1)
    assert sum(c.kind == "asm" for c in d.cells) == 4
    assert d.ok and d.placer == "llm", d.problems
    assert drc(d, g) == []
    assert len(f.seen) == 1                               # one call: layout 1 was dropped, layout 2 wired
    assert any("layout 2 of 3 routed (slots)" in n for n in d.notes), d.notes
    first = f.seen[0]
    assert "slot" in first[0]["content"] and "=" in first[1]["content"] and "Slots" in first[1]["content"]


def test_all_layouts_invalid_gets_the_errors_back():
    g = open_grid()
    good = {}

    def reply(messages):
        p = slot_ids(messages)
        good["l"] = {"note": "ok", "machines": [{"id": f"A{k + 1}", "slot": p[k]} for k in range(4)]}
        if len(messages) == 2:
            return json.dumps({"layouts": [{"note": "dup", "machines": [{"id": f"A{k + 1}", "slot": p[0]}
                                                                        for k in range(4)]}]})
        return json.dumps({"layouts": [good["l"]]})
    f = Fake(reply)
    d = design_factory(g, "electronic-circuit", 20, near=(0, 0), placer="llm", llm=f, supplied=PLATES,
                       feeds=PLATES, feed_side="west", max_attempts=1)
    assert d.ok, d.problems
    assert "both in slot" in f.seen[1][-1]["content"]


def test_a_repair_round_moves_one_machine_and_then_routes(monkeypatch):
    """The router fails the cable belt while A3 stands in its first slot; the model is told so in its own
    terms (machine and slot ids, the item) and moves A3 only; the moved layout routes."""
    g = open_grid()
    bad = {}
    real = fab_design.route_all

    def route_all(grid, cells, nets, box, *a, **kw):
        router, fails = real(grid, cells, nets, box, *a, **kw)
        a3 = next(c for c in cells if c.id == "A3")
        if (a3.x, a3.y) == bad.get("xy"):
            return router, fails + ["copper-cable: no belt route from A1 to A3"]
        return router, fails
    monkeypatch.setattr(fab_design, "route_all", route_all)

    def first(messages):
        p = slot_ids(messages)
        bad["p"] = p
        return json.dumps({"layouts": [{"note": "a row", "machines": [{"id": f"A{k + 1}", "slot": p[k]}
                                                                      for k in range(4)]}]})

    def moves(messages):
        return json.dumps({"note": "A3 next to the cable", "moves": [{"id": "A3", "slot": bad["p"][5]}]})
    f = Fake(first, moves)
    import fab.llm_place as lp
    real_lattice = lp.lattice

    def spy_lattice(*a, **kw):
        lat = real_lattice(*a, **kw)
        s = lat.by_id()["P3"]                    # A3's first slot (the row's third)
        bad["xy"] = (s.x, s.y)
        return lat
    monkeypatch.setattr(lp, "lattice", spy_lattice)
    d = design_factory(g, "electronic-circuit", 20, near=(0, 0), placer="llm", llm=f, supplied=PLATES,
                       feeds=PLATES, feed_side="west", max_attempts=1)
    assert d.ok and d.placer == "llm", d.problems
    assert drc(d, g) == []
    assert len(f.seen) == 2
    ask = f.seen[1][-1]["content"]
    assert "copper cable: no belt route from A1 (slot P1) to A3 (slot P3)" in ask
    assert "Machines involved: A1, A3" in ask and "Free slots:" in ask
    assert "P3" not in ask.split("Free slots:")[1].split(".")[0]
    wiring = ask.split("as wired so far")[1]
    assert "A3#" in wiring and "A1#" in wiring and "P3=" not in wiring and "P5=" in wiring and "I" in wiring
    assert any(ch in wiring.split("\n", 3)[3] for ch in "<>^v")
    assert any(n.startswith("repair round 1 moved A3: routed") for n in d.notes), d.notes
    a3 = next(c for c in d.cells if c.id == "A3")
    assert (a3.x, a3.y) != bad["xy"]


def test_free_placement_when_the_box_has_too_few_slots():
    """No lattice holds the machines: the model places by col/row as before (and the old reply format works)."""
    from fab.llm_place import LayoutSession
    g = open_grid(20, 20)
    asms = [Cell(f"A{k}", "asm", "iron-gear-wheel", recipe="iron-gear-wheel") for k in range(1, 7)]
    s = LayoutSession(None, g, asms, [], [], (-6, -6, 6, 6), 3)
    assert s.lat is None and s.mode == "free" and '"col"' in s.messages[0]["content"]
    assert s.propose() == ([], ["no LLM designer configured"])


def test_an_input_whose_first_belt_opens_into_a_dead_end_is_placed_again():
    from fab.design import _place_inputs, _pockets
    g = open_grid()
    feed = Cell("S1", "feed", "iron-plate", x=0, y=0, rot=1)
    out = feed.pin_sites()[0].outer
    box = (-24, -24, 24, 24)
    assert _pockets(g, [feed], [], box) == set()
    # wall the belt start into a 3x3 pocket (the feed's own tiles stay free)
    ring = {(out[0] + dx, out[1] + dy) for dx in range(-2, 3) for dy in range(-2, 3)} - \
           {(out[0] + dx, out[1] + dy) for dx in range(-1, 2) for dy in range(-1, 2)}
    g.water |= ring - feed.footprint() - {s.inserter for s in feed.pin_sites()}
    assert out in _pockets(g, [feed], [], box)
    srcs, probs = _place_inputs(g, {"iron-plate": 1, "copper-plate": 1}, (0.0, 0.0), box, [], PLATES, "west")
    assert not probs and len(srcs) == 2 and _pockets(g, srcs, [], box) == set()


def test_a_repair_that_does_not_help_is_told_back(monkeypatch):
    """Two repair rounds: A3's first move lands on another failing slot; the second ask says it was tried."""
    import fab.llm_place as lp
    g = open_grid()
    bad: dict = {}
    real, real_lattice = fab_design.route_all, lp.lattice

    def route_all(grid, cells, nets, box, *a, **kw):
        router, fails = real(grid, cells, nets, box, *a, **kw)
        a3 = next(c for c in cells if c.id == "A3")
        return router, fails + (["copper-cable: no belt route from A1 to A3"] if (a3.x, a3.y) in bad["xy"] else [])

    def spy_lattice(*a, **kw):
        lat = real_lattice(*a, **kw)
        bad["xy"] = {(lat.by_id()[k].x, lat.by_id()[k].y) for k in ("P3", "P6")}
        return lat
    monkeypatch.setattr(fab_design, "route_all", route_all)
    monkeypatch.setattr(lp, "lattice", spy_lattice)
    first = json.dumps({"layouts": [{"note": "row", "machines": [{"id": f"A{k}", "slot": f"P{k}"}
                                                                 for k in range(1, 5)]}]})
    f = Fake(first, json.dumps({"moves": [{"id": "A3", "slot": "P6"}]}),
             json.dumps({"moves": [{"id": "A3", "slot": "P7"}]}))
    d = design_factory(g, "electronic-circuit", 20, near=(0, 0), placer="llm", llm=f, supplied=PLATES,
                       feeds=PLATES, feed_side="west", max_attempts=1)
    assert d.ok, d.problems
    assert len(f.seen) == 3 and "Already tried" not in f.seen[1][-1]["content"]
    assert "Already tried, it did not help (do not repeat it): moving A3 to P6" in f.seen[2][-1]["content"]
    assert [n.split(":")[0] for n in d.notes if n.startswith("repair")] == ["repair round 1 moved A3",
                                                                            "repair round 2 moved A3"]
