"""Integration tests against real headless Factorio 2.1.24 in Docker.

    ARENA_FACTORIO=1 python -m pytest tests/test_factorio.py -v

Each test boots a fresh container (a few seconds). The full file takes ~8 minutes.
"""

import time

import automation_bot
import crew_bot
import pytest
import scripted_bot
from arena_sdk import ArenaClient, ArenaError
from conftest import factorio, make_settings
from fastapi.testclient import TestClient

from arena.controller import RunManager
from arena.gateway import create_app
from arena.replay import replay_run

pytestmark = [pytest.mark.factorio, factorio]


@pytest.fixture
def fsettings(tmp_path):
    return make_settings(tmp_path, backend="factorio")


@pytest.fixture
def fclient(fsettings):
    token = fsettings.add_team("alpha")
    manager = RunManager(fsettings)
    with TestClient(create_app(fsettings, manager, start_reaper=False)) as http:
        try:
            yield ArenaClient(token=token, http=http), manager
        finally:
            manager.stop()      # a failed test must not leave its container holding the ports


def test_world_stays_paused_and_ticks_are_exact(fclient):
    client, manager = fclient
    run = client.start_run("level1-iron", timeout=300)
    t0 = run.observe()["tick"]
    time.sleep(3)
    assert run.observe()["tick"] == t0                    # nothing moves while the AI thinks
    r = run.advance(600)
    assert r["ticks_spent"] == 600 and r["tick"] == t0 + 600
    r = run.move(r["data"].get("x", 12.5), 0.5)
    assert r["ticks_spent"] == r["budgets"]["ticks_used"] - 600
    run.end_and_wait()


def test_scripted_bot_on_real_factorio_and_replay_is_identical(fclient, fsettings):
    client, _ = fclient
    run = client.start_run("level1-iron", timeout=300)
    result = scripted_bot.play(run, log=lambda *a: None)
    assert result["status"] == "finished", result
    assert result["score"] > 900, result
    assert result["anomalies"] == []
    rep = replay_run(fsettings.runs_dir / run.id, fsettings)
    assert rep["match"], rep


def test_level2_mining_and_crafting(fclient):
    client, _ = fclient
    run = client.start_run("level2-bootstrap", timeout=300)
    obs = run.observe(radius=20)["data"]
    stone = obs["resource_summary"]["stone"]["nearest"]
    assert run.move(stone["x"], stone["y"])["ok"]
    r = run.mine(stone["x"], stone["y"], 5)
    assert r["ok"] and r["ticks_spent"] == 600, r
    r = run.craft("stone-furnace")
    assert r["ok"] and r["ticks_spent"] == 30, r
    inv = run.observe(radius=2)["data"]["inventory"]
    assert inv.get("stone-furnace") == 1 and inv.get("stone", 0) == 0
    result = run.end_and_wait()
    assert result["breakdown"]["produced"] == 0


def test_level3_delivery_chest_is_protected(fclient):
    client, _ = fclient
    run = client.start_run("level3-delivery", timeout=300)
    obs = run.observe(radius=20)["data"]
    chest = next((e for e in obs["entities"] if e.get("protected")), None)
    if chest is None:
        run.move(24.5, 0.5)
        obs = run.observe(radius=20)["data"]
        chest = next(e for e in obs["entities"] if e.get("protected"))
    run.move(chest["x"] - 3, chest["y"])
    r = run.pickup(chest["x"], chest["y"])
    assert r["code"] == "forbidden_target"
    r = run.place_line("transport-belt", chest["x"] - 8, chest["y"] + 2, chest["x"] - 2, chest["y"] + 2)
    assert r["ok"] and len(r["data"]["placed"]) == 7, r
    result = run.end_and_wait()
    assert result["score"] == 0 and result["metric"] == "delivered:iron-plate"


def test_time_only_level_on_real_factorio_and_replay(fclient, fsettings):
    client, _ = fclient
    run = client.start_run("level3-delivery-timed", timeout=300)
    assert run.budgets["actions_left"] is None
    t0 = run.observe()["tick"]
    r = run.rotate(500, 500, "east")                       # rejected, but still takes 60 ticks of game time
    assert not r["ok"] and r["ticks_spent"] == 60 and r["tick"] == t0 + 60
    r = run.place("stone-furnace", 3, 3)
    assert r["ok"] and r["ticks_spent"] == 60
    r = run.insert(3, 3, "coal", 5)
    assert r["ok"] and r["ticks_spent"] == 60
    time.sleep(2)
    assert run.observe()["tick"] == t0 + 180               # still paused between calls
    result = run.end_and_wait()
    assert result["status"] == "finished" and result["budgets_used"]["actions"] == 3
    assert result["budgets_used"]["ticks"] == 180
    rep = replay_run(fsettings.runs_dir / run.id, fsettings)
    assert rep["match"], rep


def test_automation_level_rules_on_real_factorio(fclient):
    client, _ = fclient
    run = client.start_run("automation", timeout=300)
    obs = run.observe(radius=20)["data"]
    assert obs["water_summary"]["distance"] < 60 and obs["inventory"]["assembling-machine-1"] == 20
    x, y = obs["position"]["x"] + 4, obs["position"]["y"]
    r = run.place("assembling-machine-1", x + 0.5, y + 0.5)
    assert r["ok"], r
    am = r["data"]["entity"]
    r = run.set_recipe(am["x"], am["y"], "automation-science-pack")
    assert r["ok"] and r["data"]["entity"]["recipe"] == "automation-science-pack", r
    assert sorted(r["data"]["entity"]["missing"]) == ["copper-plate", "iron-gear-wheel"]
    r = run.place("inserter", am["x"] - 2, am["y"], "west")          # picks from the west, drops into it
    assert r["ok"] and r["data"]["entity"]["pickup"][0] < r["data"]["entity"]["drop"][0], r
    r = run.place("offshore-pump", x - 2.5, y + 0.5, "north")       # dry land: refused, with a hint
    assert r["code"] == "cannot_place" and "fits" in r["message"], r
    with pytest.raises(ArenaError) as e:                    # plates can't be fed by hand here
        run.insert(am["x"], am["y"], "iron-plate", 1)
    assert e.value.code == "item_not_allowed"
    with pytest.raises(ArenaError) as e:
        run.craft("iron-gear-wheel")
    assert e.value.code == "verb_not_allowed"
    # the missed corner from the first runs: east line, then a north line starting above its last tile
    run.move(x - 1, y - 12)
    r = run.place_line("transport-belt", x - 6.5, y - 8.5, x - 3.5, y - 8.5)
    assert r["ok"] and "nothing ahead" in r["data"]["links"]["end"], r
    r = run.place_line("transport-belt", x - 3.5, y - 9.5, x - 3.5, y - 12.5)
    assert r["ok"] and "must face north" in r["data"]["links"]["start"], r
    r = run.rotate(x - 3.5, y - 8.5, "north")                        # the fix: the corner belt faces north
    assert r["ok"] and "fed by" in r["data"]["links"]["start"] and "continues" in r["data"]["links"]["end"], r
    # the right way, which runs 2 and 3 used: start the second line ON the corner tile; it turns the corner belt
    r = run.place_line("transport-belt", x - 6.5, y - 14.5, x - 3.5, y - 14.5)
    assert r["ok"], r
    r = run.place_line("transport-belt", x - 3.5, y - 14.5, x - 3.5, y - 17.5)
    assert r["ok"] and len(r["data"]["placed"]) == 4 and r["data"]["turned"] == [[x - 3.5, y - 14.5]], r
    assert "fed by" in r["data"]["links"]["start"], r
    run.move(x - 4, y + 2)
    r = run.place_line("pipe", x - 6.5, y + 6.5, x - 6.5, y + 9.5)
    assert r["ok"] and len(r["data"]["placed"]) == 4, r
    trees = [o for o in obs["obstacles"] if o[2] in ("tree", "simple-entity")]
    if trees:
        tx, ty, _ = min(trees, key=lambda o: (o[0] - obs["position"]["x"]) ** 2 + (o[1] - obs["position"]["y"]) ** 2)
        run.move(tx + 2, ty)
        r = run.pickup(tx, ty)
        assert r["ok"] and r["data"]["cleared"] in ("tree", "simple-entity") and r["data"]["picked_up"], r
    result = run.end_and_wait()
    assert result["metric"] == "automation" and result["breakdown"]["tier"] == 0
    assert result["idle_tail"]["tick_after"] - result["idle_tail"]["tick_before"] == 36_000


def test_automation_bot_reaches_tier1_and_replay_is_identical(fclient, fsettings):
    client, _ = fclient
    run = client.start_run("automation", timeout=300)
    result = automation_bot.play(run, log=lambda *a: None)
    assert result["status"] == "finished", result
    b = result["breakdown"]
    assert b["tier"] == 1 and result["score"] > 100_000, b
    assert b["tiers"][0]["per_minute"] >= 5 and b["tiers"][0]["chain_short"] == []
    rep = replay_run(fsettings.runs_dir / run.id, fsettings)
    assert rep["match"], rep


def test_realtime_clock_walking_rate_limit_and_replay(fclient, fsettings):
    """Realtime level: the world runs on its own, walking takes game time, actions are rate-limited,
    hands are busy during a job, and the run replays exactly from the logged ticks."""
    client, _ = fclient
    run = client.start_run("automation-realtime", timeout=300)
    assert run.budgets["clock"].startswith("realtime (starts")
    obs = run.observe(20)                                   # the first call starts the clock
    t0 = obs["tick"]
    time.sleep(2)
    t1 = run.observe(5)["tick"]
    assert 90 <= t1 - t0 <= 180                             # ~60 ticks a second, nobody waiting
    p = obs["data"]["position"]
    r = run.move(p["x"] + 15, p["y"])
    assert r["ok"] and r["data"]["arrives_tick"] - r["tick"] == 100      # 15 tiles at 0.15 tiles/tick
    fast = run.place("iron-chest", p["x"] + 2.5, p["y"] + 2.5)        # right after the move: too fast
    assert fast["code"] == "too_fast" and fast["charged"] is None
    mid = run.observe(5)["data"]["position"]
    assert p["x"] < mid["x"] < p["x"] + 15                  # still walking
    time.sleep(2)
    assert abs(run.observe(5)["data"]["position"]["x"] - (p["x"] + 15)) < 0.6
    trees = [o for o in obs["data"]["obstacles"] if o[2] == "tree"]
    if trees:
        x, y = trees[0][0], trees[0][1]
        run.move(x + 1.5, y)
        time.sleep(3)
        first = run.pickup(x, y)
        if first["ok"]:
            assert first["data"]["hands_free_tick"] > first["tick"]
    res = run.end_and_wait()
    assert res["status"] == "finished" and res["end_reason"] == "ended_by_team", res
    assert abs(res["idle_tail"]["tick_before"] - (t0 + 72_000)) <= 2   # the window still starts at minute 20
    rep = replay_run(fsettings.runs_dir / run.id, fsettings)
    assert rep["match"], rep


def test_crew_bot_builds_tier1_in_real_time_and_replay_is_identical(fclient, fsettings):
    """The crew bot with the free scripted director: plans, builds two red blocks in real time, keeps them fuelled."""
    client, _ = fclient
    run = client.start_run("automation-realtime", timeout=300)
    lines = []
    result = crew_bot.play(run, crew_bot.ScriptedDirector(blocks=2), log=lines.append)
    assert result["status"] == "finished", (result, lines)
    b = result["breakdown"]
    assert b["tier"] == 1 and result["score"] > 100_000, (b, lines)
    assert result["bot"]["director_calls"] >= 2
    print("\n".join(map(str, lines)))
    print("score", result["score"], "red/min", b["tiers"][0]["per_minute"], "build ticks", result["budgets_used"],
          "wall", result["wall_seconds"], "fuel fixes", result["bot"]["foreman_fuel_fixes"])
    rep = replay_run(fsettings.runs_dir / run.id, fsettings)
    assert rep["match"], rep


def test_fab_pieces_on_real_factorio(fclient):
    """The engine's assumptions, checked against the game: a mine cell's drill drops into its furnace,
    pin inserters face the right way, and underground belts pair up when placed with belt_type."""
    import automation_bot as ab
    from arena_sdk import RealtimeRun
    from fab.cells import Cell, PinSite
    from fab.grid import Grid
    from fab.place import place_mines
    client, _ = fclient
    run = RealtimeRun.wrap(client.start_run("automation-realtime", timeout=300))
    w = ab.World(run, log=lambda *a: None)
    w.observe(20)
    iron = w.summary["iron-ore"]["nearest"]
    w.go(iron["x"], iron["y"])
    w.observe(20)
    mines, probs = place_mines(Grid.from_world(w), {"iron-plate": 1}, w.pos, set())
    assert mines and not probs, probs
    m = mines[0]
    drill, furnace = m.entities()
    d = w.place(drill.name, drill.x, drill.y, drill.direction, set(drill.tiles))
    w.place(furnace.name, furnace.x, furnace.y, "north", set(furnace.tiles))
    assert ab.tile(*d["drop"]) in furnace.tiles, (d, furnace)
    # an assembler with one input pin (belt -> machine) and one output pin (machine -> chest)
    x0, y0 = int(w.pos[0]) + 4, int(w.pos[1]) - 12
    w.observe(20)
    a = Cell("A", "asm", "iron-gear-wheel", recipe="iron-gear-wheel", x=x0, y=y0)
    tiles = a.footprint() | {s.inserter for s in a.pin_sites()} | {s.outer for s in a.pin_sites()}
    assert all(w.free([t]) for t in tiles), "test spot not clear"
    e = a.entities()[0]
    w.place(e.name, e.x, e.y, "north", set(e.tiles))
    s_in, s_out = a.pin_sites()[0], a.pin_sites()[3]          # north (input) and south (output)
    r = w.place("inserter", s_in.inserter[0] + 0.5, s_in.inserter[1] + 0.5, s_in.inserter_dir(True), {s_in.inserter})
    assert ab.tile(*r["pickup"]) == s_in.outer and ab.tile(*r["drop"]) == s_in.machine, r
    r = w.place("inserter", s_out.inserter[0] + 0.5, s_out.inserter[1] + 0.5, s_out.inserter_dir(False),
                {s_out.inserter})
    assert ab.tile(*r["pickup"]) == s_out.machine and ab.tile(*r["drop"]) == s_out.outer, r
    # underground pair east of the assembler, both facing east
    ux, uy = x0 + 6, y0
    w.near(ux + 2, uy)
    r1 = run.place("underground-belt", ux + 0.5, uy + 0.5, "east", "input")
    r2 = run.place("underground-belt", ux + 4.5, uy + 0.5, "east", "output")
    assert r1["ok"] and r2["ok"], (r1, r2)
    print("ug", r1["data"]["entity"], r2["data"]["entity"])
    assert r2["data"]["entity"].get("pairs_with") == [ux + 0.5, uy + 0.5], r2
    assert r2["data"]["entity"]["belt_type"] == "output"
    run.end_and_wait()


def test_capture_map_snapshot(fclient):
    """Walk the factory area of practice map 1008 and save what the bot saw: the offline engine tests use it."""
    import crew_bot
    from arena_sdk import RealtimeRun
    from fab.grid import Grid
    from pathlib import Path
    client, _ = fclient
    run = RealtimeRun.wrap(client.start_run("automation-realtime", timeout=300))
    crew = crew_bot.Crew(run, log=lambda *a: None)
    crew.start()
    w = crew.world
    iron, copper = w.nearest("iron-ore"), w.nearest("copper-ore")
    focus = ((iron[0] + copper[0]) / 2, (iron[1] + copper[1]) / 2)
    crew.survey(focus)
    out = Path(__file__).parent / "fixtures" / "map_1008.json"
    Grid.from_world(w).save(out)
    print("saved", out, len(w.seen), "tiles seen, focus", focus)
    run.end_and_wait()
