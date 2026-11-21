"""Simulator fidelity: Factorio 2.0 rates, caps and geometry."""

import math

from pydantic import TypeAdapter

from arena.backends.sim import SimBackend
from arena.models import Action

A = TypeAdapter(Action)


def act(b, max_ticks=10**9, **kw):
    return b.act(A.validate_python(kw), max_ticks)


def fresh(settings, task="level1-iron", seed=101):
    b = SimBackend()
    b.setup(settings.tasks[task], seed)
    return b


def iron_drill_site(b):
    """A 2x2 drill spot fully on iron with a free spot above it, plus where to stand."""
    for (tx, ty), (name, _) in sorted(b.resources.items()):
        cx, cy = tx + 1, ty + 1
        tiles = [(cx - 1, cy - 1), (cx, cy - 1), (cx - 1, cy), (cx, cy)]
        if all(b.resources.get(t, ["", 0])[0] == "iron-ore" for t in tiles):
            return cx, cy
    raise AssertionError("no iron site")


def build_pair(b, coal_drill=50, coal_furnace=30):
    cx, cy = iron_drill_site(b)
    assert act(b, type="move", x=cx + 3.5, y=cy + 0.5).ok
    assert act(b, type="place", entity="burner-mining-drill", x=cx, y=cy, direction="north").ok
    assert act(b, type="place", entity="stone-furnace", x=cx, y=cy - 2).ok
    assert act(b, type="insert", x=cx, y=cy, item="coal", count=coal_drill).ok
    assert act(b, type="insert", x=cx, y=cy - 2, item="coal", count=coal_furnace).ok
    return cx, cy


def test_map_is_deterministic(settings):
    a, b = fresh(settings, seed=7), fresh(settings, seed=7)
    assert a.resources == b.resources
    assert fresh(settings, seed=8).resources != a.resources


def test_drill_furnace_rate_matches_factorio(settings):
    b = fresh(settings)
    build_pair(b)
    start = b.tick()
    b.run_ticks(60 * 60 * 5)    # 5 minutes
    plates = b.produced["iron-plate"]
    # 0.25 ore/s for 300 s = 75, minus pipeline delay (first ore 240 ticks, first plate +192)
    assert 72 <= plates <= 75, plates
    assert b.tick() - start == 18000


def test_furnace_output_caps_at_100(settings):
    b = fresh(settings)
    cx, cy = build_pair(b)
    b.run_ticks(60 * 60 * 9)    # would make ~135 without the cap
    f = b._entity_at(cx, cy - 2)
    assert f.output["iron-plate"] == 100
    assert f.status == "full_output"
    assert b.produced["iron-plate"] == 100
    r = act(b, type="extract", x=cx, y=cy - 2, item="iron-plate", count=100)
    assert r.ok and r.data["extracted"] == 100
    b.run_ticks(600)
    assert b.produced["iron-plate"] > 100


def test_fuel_runs_out(settings):
    b = fresh(settings)
    cx, cy = build_pair(b, coal_drill=1, coal_furnace=1)
    b.run_ticks(60 * 60 * 3)
    drill = b._entity_at(cx, cy)
    assert drill.status == "no_fuel"
    # one coal = 4 MJ = 26.7 s of drilling = 6 ore
    assert b.produced["iron-ore"] == 6


def test_drop_position_by_direction(settings):
    b = fresh(settings)
    cx, cy = iron_drill_site(b)
    act(b, type="move", x=cx + 4.5, y=cy + 0.5)
    act(b, type="place", entity="burner-mining-drill", x=cx, y=cy, direction="east")
    act(b, type="place", entity="stone-furnace", x=cx + 2, y=cy)
    act(b, type="insert", x=cx, y=cy, item="coal", count=5)
    act(b, type="insert", x=cx + 2, y=cy, item="coal", count=5)
    b.run_ticks(600)
    assert b._entity_at(cx + 2, cy).source["iron-ore"] + b.produced["iron-plate"] >= 1


def test_reach_and_collision(settings):
    b = fresh(settings)
    assert act(b, type="place", entity="stone-furnace", x=40, y=40).code == "out_of_reach"
    assert act(b, type="place", entity="stone-furnace", x=4, y=4).ok
    assert act(b, type="place", entity="stone-furnace", x=5, y=5).code == "cannot_place"
    assert act(b, type="place", entity="stone-furnace", x=0, y=0).code == "cannot_place"  # character there


def test_move_costs_ticks_and_respects_budget(settings):
    b = fresh(settings)
    r = act(b, type="move", x=15.5, y=0.5)
    assert r.ok and b.tick() == math.ceil(15 / 0.15)
    t = b.tick()
    r = act(b, max_ticks=10, type="move", x=-30, y=0.5)
    assert r.code == "insufficient_ticks" and b.tick() == t


def test_hand_mining_and_crafting(settings):
    b = fresh(settings, task="level2-bootstrap", seed=201)
    stone = next(t for t, (n, _) in sorted(b.resources.items()) if n == "stone")
    act(b, type="move", x=stone[0] + 0.5, y=stone[1] + 0.5)
    t = b.tick()
    r = act(b, type="mine", x=stone[0] + 0.5, y=stone[1] + 0.5, count=5)
    assert r.ok and b.inv["stone"] == 5 and b.tick() - t == 600
    r = act(b, type="craft", recipe="stone-furnace")
    assert r.ok and b.inv["stone-furnace"] == 1 and b.inv["stone"] == 0
    assert act(b, type="craft", recipe="iron-gear-wheel").code == "not_enough_ingredients"


def test_pickup_returns_contents(settings):
    b = fresh(settings)
    cx, cy = build_pair(b, coal_drill=10, coal_furnace=10)
    before = b.inv["coal"]
    r = act(b, type="pickup", x=cx, y=cy - 2)
    assert r.ok and b.inv["stone-furnace"] == 10 and b.inv["coal"] == before + 10
