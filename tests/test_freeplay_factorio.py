"""The freeplay verbs on real Factorio 2.1.24 (Docker): scan, stats, wide observes, the tech tree, batches.

    ARENA_FACTORIO=1 python -m pytest tests/test_freeplay_factorio.py -v

One realtime game from an empty inventory, about two minutes.
"""

import math
import time

import pytest
from arena_sdk import ArenaClient, ArenaError, RealtimeRun
from conftest import factorio, make_settings
from fastapi.testclient import TestClient

from arena.controller import RunManager
from arena.gateway import create_app

pytestmark = [pytest.mark.factorio, factorio]


@pytest.fixture
def fclient(tmp_path):
    s = make_settings(tmp_path, backend="factorio")
    token = s.add_team("alpha")
    manager = RunManager(s)
    with TestClient(create_app(s, manager, start_reaper=False)) as http:
        try:
            yield ArenaClient(token=token, http=http), manager
        finally:
            manager.stop()


def _walk_next_to(run, x, y, gap=1.6):
    p = run.observe(4, include=["entities"])["data"]["position"]
    d = math.dist((p["x"], p["y"]), (x, y))
    if d > gap:
        f = (d - gap) / d
        r = run.move(p["x"] + (x - p["x"]) * f, p["y"] + (y - p["y"]) * f)
        assert r["ok"], r                                  # walked around water and buildings, not through
        assert r["data"]["path_length"] >= r["data"]["straight"] - 1.5


def _mine(run, patch, n):
    t = patch["nearest"]
    _walk_next_to(run, t["x"], t["y"])
    r = run.mine(t["x"], t["y"], n)
    assert r["ok"], r
    time.sleep(max(0, r["data"]["hands_free_tick"] - r["tick"]) / 60 + 0.1)
    return r


def test_freeplay_verbs_from_nothing(fclient):
    client, _ = fclient
    assert "lab" in client.catalog()["recipes"]
    run = RealtimeRun.wrap(client.start_run("freeplay-green", timeout=300))

    o = run.observe(32)["data"]
    assert o["inventory"] == {} and o["radius"] == 32
    brief = run.observe(32, x=40, y=0, detail="brief", include=["obstacles", "water"])
    assert brief["ok"] and "entities" not in brief["data"] and brief["data"]["centre"]["x"] == 40
    far = run.observe(10, x=200, y=0)
    assert not far["ok"] and far["code"] == "out_of_range"

    scan = run.scan(150)
    assert scan["charged"] == "observe"
    patches = scan["data"]["patches"]
    by = {}
    for p in patches:
        by.setdefault(p["name"], p)                      # nearest patch of each resource
    assert {"iron-ore", "copper-ore", "coal", "stone"} <= set(by), [p["name"] for p in patches]
    assert by["iron-ore"]["tiles"] > 20 and scan["data"]["water"]

    # the real tech tree: lab is locked, smelting is not hand-craftable, electronics is a trigger tech
    assert run.craft("lab")["code"] in ("recipe_locked", "not_enough_ingredients")
    r = run.research(["electronics"])
    assert r["code"] == "trigger_technology", r
    r = run.research(["automation"])
    assert not r["ok"] and r["code"] in ("cannot_research",), r

    # hand-mine stone, craft furnaces, place two in one batch
    _mine(run, by["stone"], 10)
    r = run.craft("stone-furnace", 2)
    assert r["ok"], r
    time.sleep(1.2)
    with pytest.raises(ArenaError) as e:                 # smelting is never hand-craftable: refused up front
        run.craft("iron-plate")
    assert e.value.code == "recipe_not_allowed"
    _mine(run, by["coal"], 6)
    _mine(run, by["copper-ore"], 10)
    p = run.observe(6, include=["entities"])["data"]["position"]
    fx, fy = math.floor(p["x"]) + 3, math.floor(p["y"])
    b = run.batch([{"type": "place", "entity": "stone-furnace", "x": fx, "y": fy},
                   {"type": "insert", "x": fx, "y": fy, "item": "coal", "count": 3},
                   {"type": "insert", "x": fx, "y": fy, "item": "copper-ore", "count": 10},
                   {"type": "place", "entity": "stone-furnace", "x": fx + 3, "y": fy}])
    assert b["ok"] and b["data"]["ops_ok"] == 4, b
    assert b["budgets"]["actions_used"] >= 4
    assert b["data"]["next_action_tick"] - b["tick"] == 4 * 3                 # 4 ops x batch_op_gap

    # a big batch (the RCON payload) with failing ops that keep going
    ops = [{"type": "rotate", "x": 500 + i, "y": 500, "direction": "east"} for i in range(50)]
    big = run.batch(ops, stop_on_error=False)
    assert big["ok"] and big["data"]["ops_run"] == 50 and big["data"]["ops_ok"] == 0, big

    # 10 copper plates complete the electronics trigger tech; the milestone is recorded within 10 s
    deadline = time.time() + 70
    st = None
    while time.time() < deadline:
        st = run.stats(minutes=1)["data"]
        if "electronics" in st.get("researched", []) and "electronics" in (st.get("milestones") or {}):
            break
        time.sleep(3)
    assert "electronics" in st["researched"], st.get("researched")
    assert st["items"]["copper-plate"]["made"] >= 10
    assert st["items"]["stone-furnace"]["hand_made"] == 2
    assert "electronics" in st["milestones"]
    assert run.craft("copper-cable")["code"] in ("not_enough_ingredients", None)   # unlocked now (no plates on us)
    res = run.end_and_wait()
    assert res["status"] == "finished" and res["breakdown"]["reached"] >= 1, res
