"""The trap watch (run 20261119-105322: the character sat in a 7x4 pocket of its own burner mines and furnaces
for good). Trapped is seen by the record (far walks refused, none got away) as well as by probes; the way out is
dug by the map (the fewest buildings between the character and open ground) and kept open; and when that is not
enough, or the same trap keeps coming back, the fixer (an LLM) is asked with a map of the ground around it."""

import json
import math
from types import SimpleNamespace

import pytest

from arena import catalog
from brain import fixer
from brain.world import BuildError, FreeWorld

pytestmark = pytest.mark.skipif(catalog.get().empty, reason="needs config/catalog.json (arena catalog)")

POCKET = {(x, y) for x in range(11, 19) for y in range(-33, -29)}
# a band of stone furnaces two tiles thick all round the pocket (a furnace at (x, y) covers x-1..x, y-1..y)
WALLS = {(float(x), -34.0) for x in range(10, 22, 2)} | {(float(x), -28.0) for x in range(10, 22, 2)} \
    | {(10.0, -32.0), (10.0, -30.0), (20.0, -32.0), (20.0, -30.0)}


def _kn():
    from brain.knowledge import Knowledge
    return Knowledge(json.loads(json.dumps(catalog.get().data)))


class TrapRun:
    """Moves inside the pocket work; anything else is no_path until one wall furnace is picked up."""
    budgets: dict = {}

    def __init__(self, walls=WALLS, opens=True):
        self.walls = set(walls)
        self.taken, self.opens, self.pos = [], opens, (18.5, -30.5)

    def is_open(self):
        return self.opens and bool(self.taken)

    def move(self, x, y):
        if self.is_open() or (math.floor(x), math.floor(y)) in POCKET:
            self.pos = (x, y)
            return {"ok": True, "data": {"position": {"x": x, "y": y}}}
        return {"ok": False, "code": "no_path", "data": {}}

    def observe(self, radius, **opts):
        ents = [{"name": "stone-furnace", "x": x, "y": y, "direction": "north"} for x, y in self.walls]
        return {"ok": True, "data": {"position": {"x": self.pos[0], "y": self.pos[1]}, "entities": ents}}

    def act(self, a):
        if a["type"] == "pickup":
            p = (a["x"], a["y"])
            if p not in self.walls:
                return {"ok": False, "code": "no_entity", "data": {}}
            self.walls.discard(p)
            self.taken.append(p)
            return {"ok": True, "data": {"picked_up": {"stone-furnace": 1}}}
        if a["type"] == "observe":
            return self.observe(a.get("radius", 8))
        raise AssertionError(a)

    def pickup(self, x, y):
        return self.act({"type": "pickup", "x": x, "y": y})


def _world(run=None):
    run = run or TrapRun()
    w = FreeWorld(run, _kn(), log=lambda *a: None)
    w.seen = {(x, y) for x in range(-20, 60) for y in range(-70, 10)}
    w.pos = run.pos
    w.observe(24, include=["entities"])
    return w, run


def test_the_dig_plan_opens_the_pocket_through_one_building():
    w, _ = _world()
    plan = w.dig_plan()
    assert len(plan) == 1 and plan[0][0] == "stone-furnace"
    assert (plan[0][1], plan[0][2]) in WALLS
    assert math.dist((plan[0][1], plan[0][2]), w.pos) < 6           # the near side, not across the pocket


def test_a_trapped_character_digs_a_door_and_keeps_it_open():
    w, run = _world()
    dug = []
    w.on_dug = dug.append
    assert w.go(40.5, -30.5) is None and w.pos == (40.5, -30.5)       # out, and on to where it was going
    assert len(run.taken) == 1 and w.unstucks == 1
    door = w.footprint("stone-furnace", *run.taken[0])
    assert door <= w.keepout                                         # nothing is built in the door again
    assert dug and dug[0][0][0] == "stone-furnace"                   # the crew forgets it in its site
    assert w.trap_notes and "dug out" in w.trap_notes[-1]["note"]


def test_far_walks_refused_again_and_again_mean_trapped_whatever_a_probe_says():
    w, run = _world()
    w.boxed_in = lambda: False                                      # a probe fooled by the pocket
    for _ in range(w.TRAP_FAILS - 1):
        run_taken = len(run.taken)
        w._step(40.5, -30.5)
        assert len(run.taken) == run_taken                           # not yet: could be a one-off
        w._step(12.5, -31.5)                                          # a short walk inside the pocket works...
    w._step(40.5, -30.5)                                              # ...but does not reset the record
    assert run.taken and w.pos == (40.5, -30.5)


def test_the_fixer_is_asked_when_digging_out_fails_or_the_trap_keeps_coming_back():
    w, run = _world(TrapRun(opens=False))                            # nothing we can pick up opens it
    asked = []
    w.on_trapped = asked.append
    w.go(40.5, -30.5)
    assert asked and "still trapped" in asked[0]
    w2, run2 = _world()
    asked2 = []
    w2.on_trapped = asked2.append
    for _ in range(w2.TRAP_REPEAT):
        run2.taken.clear()                                            # shut in again (a new wall each time)
        run2.walls = set(WALLS)
        run2.pos = w2.pos = (18.5, -30.5)
        w2.go(40.5, -30.5)
    assert len(asked2) == 1 and f"{w2.TRAP_REPEAT} dig-out(s)" in asked2[0]


def test_the_fixer_sees_a_map_and_can_order_a_dig_out():
    w, run = _world()
    text = w.local_map(10)
    rows = text.split("\n")[1:]
    assert len(rows) == 21 and rows[10][10] == "@" and "S" in text  # S: the stone furnaces round it
    crew = SimpleNamespace(w=w, base=SimpleNamespace(sites=[]), slots_used=lambda: 10)
    sit = fixer.situation(crew, "could not dig out: trapped", {"do": "unstuck"}, [])
    assert sit["map_around_character"] and "@" in sit["map_around_character"]
    assert fixer.RuleFixer().diagnose(sit)["fixes"][0] == {"fix": "escape"}
    done = fixer.apply(crew, [{"fix": "escape"}], [], {}, "k")
    assert done[0].startswith("dug out") and run.taken
