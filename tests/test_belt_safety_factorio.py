"""Belt merges, the isolated guard, declared contents and lane contents on real Factorio 2.1.24 (Docker).

    ARENA_FACTORIO=1 python -m pytest tests/test_belt_safety_factorio.py -v

One sandbox game, under a minute: an iron belt tagged carries=[iron-plate]; a belt pointed into its side is
refused when isolated, and reported as a side-load merge when not; coal put on that belt reaches the iron one and
shows in its lanes and as foreign_items (run 20261120-211555: coal strips spilled onto iron feeds unseen).
"""

import math
import time

import pytest
from arena_sdk import ArenaClient, RealtimeRun
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
            yield ArenaClient(token=token, http=http)
        finally:
            manager.stop()


def _belts_at(run, x, y, r=4):
    d = run.observe(r, x=x, y=y, include=["entities"], names=["transport-belt"], belts=True)["data"]
    return {(math.floor(e["x"]), math.floor(e["y"])): e for e in d["entities"]}


def test_merges_guard_and_foreign_items(fclient):
    run = RealtimeRun.wrap(fclient.start_run("sandbox-workshop", timeout=300))
    p = run.observe(4, include=["entities"])["data"]["position"]
    x0, y0 = math.floor(p["x"]) + 3, math.floor(p["y"]) - 3          # a few tiles off, inside reach

    line = run.place_line("transport-belt", x0 + 0.5, y0 + 0.5, x0 + 4.5, y0 + 0.5, carries=["iron-plate"])
    assert line["ok"], line
    assert line["data"]["merges"] in ([], {}), line["data"]          # nothing else around: no joins

    # a belt south of the line, facing north: it would feed into the side of the line's belt at (x0 + 2, y0)
    side = (x0 + 2.5, y0 + 1.5)
    refused = run.place("transport-belt", *side, "north", isolated=True, carries=["coal"])
    assert not refused["ok"] and refused["code"] == "would_merge", refused
    assert (x0 + 2, y0 + 1) not in _belts_at(run, x0 + 2, y0), "the refused belt was taken back"

    allowed = run.place("transport-belt", *side, "north", isolated=True, carries=["coal"],
                        allow=[[x0 + 2, y0]])
    assert allowed["ok"], allowed
    [m] = allowed["data"]["merges"]
    assert m["way"] == "into" and m["side"] is True and [math.floor(v) for v in m["other"]] == [x0 + 2, y0]

    # coal on the side belt (a chest and a burner inserter below it) runs onto the iron line: its lanes show it,
    # and the tagged belts flag it. The inserter faces the side it picks up from (this API's rule)
    chest, ins = (x0 + 2.5, y0 + 3.5), (x0 + 2.5, y0 + 2.5)
    assert run.place("iron-chest", *chest)["ok"]
    assert run.insert(*chest, "coal", 20)["ok"]
    assert run.place("burner-inserter", *ins, "south")["ok"]
    assert run.insert(*ins, "coal", 2)["ok"]
    end = time.time() + 20
    seen = {}
    while time.time() < end:
        seen = _belts_at(run, x0 + 2, y0)
        if any(e.get("status") == "foreign_items" for t, e in seen.items() if t[1] == y0):
            break
        time.sleep(1)
    on_line = [e for t, e in seen.items() if t[1] == y0]
    assert any("coal" in (e.get("foreign") or {}) for e in on_line), on_line
    assert all(e.get("carries") == ["iron-plate"] for e in on_line)
    lanes = [lane for e in on_line for lane in (e.get("lanes") or [])]
    assert any(isinstance(lane, dict) and lane.get("coal") for lane in lanes), lanes
    feeder = seen[(x0 + 2, y0 + 1)]
    assert [[math.floor(v) for v in q] for q in feeder["feeds"]] == [[x0 + 2, y0]]
    probs = run.observe(4, x=x0 + 2, y=y0, include=["entities"], only_problems=True)["data"]["entities"]
    assert any(e.get("status") == "foreign_items" for e in probs)

    # turning the feeder away reports no join any more
    r = run.rotate(*side, "east")
    assert r["ok"] and r["data"]["merges"] in ([], {}), r
