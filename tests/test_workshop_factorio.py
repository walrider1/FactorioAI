"""The workshop, electric mines and the silo in a real game (Factorio 2.1.24 in Docker), from the sandbox task.

    ARENA_FACTORIO=1 python -m pytest tests/test_workshop_factorio.py -v -s

One run, a few minutes: steam power; a two-cell workshop for batteries (water from an offshore pump into a
chemical plant making sulfuric acid, piped into a second chemical plant making batteries, the courier filling
both input chests); a row of electric drills on iron; the rocket silo crafting rocket parts from its chests.
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


def _wait(pred, seconds, step=2.0):
    end = time.time() + seconds
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return pred()


def test_workshop_mine_and_silo(fclient, tmp_path):
    from brain.crew import Crew
    from brain.knowledge import Knowledge
    from brain.modules import MODULES
    from brain.world import FreeWorld

    run = RealtimeRun.wrap(fclient.start_run("sandbox-workshop", timeout=300), speed=2)
    kn = Knowledge(fclient.catalog())
    kn.learn_research(list(kn.techs))                     # tech: all
    w = FreeWorld(run, kn, log=print, speed=2)
    MODULES["workshop"].case_dir = tmp_path / "floors"       # a failed floor is saved here for offline replay
    print(f"floors saved to {tmp_path / 'floors'}")
    w.scan_radius = 256
    w.observe(32)
    w.scan()
    crew = Crew(w, log=print)

    rec: list = []
    w.recorder = rec
    power = MODULES["steam_power"].build(w, crew.base, {"engines": 2})
    w.recorder = None
    power.entities = rec
    crew.base.add(power)

    rec = []
    w.recorder = rec                                      # a second station: the oil cells draw ~1.5 MW more
    power2 = MODULES["steam_power"].build(w, crew.base, {"engines": 2})
    w.recorder = None
    power2.entities = rec
    crew.base.add(power2)

    # a research station, and a "science factory" (a chest of red packs) belted to one of its ports
    from brain.modules import Site
    print(crew.build({"do": "build", "module": "research_station", "count": 3}))
    station = next(s for s in crew.base.of("labs") if s.params.get("station"))
    sx, sy = station.labs[0]
    src = (math.floor(sx) - 14.5, math.floor(sy) + 9.5)       # a clear-ish tile some way off the head lab
    w.observe(16, x=src[0], y=src[1])
    w.place("iron-chest", src[0], src[1], "north", {(math.floor(src[0]), math.floor(src[1]))})
    w.insert(src[0], src[1], "automation-science-pack", 40)
    crew.base.add(Site("factory", {"item": "automation-science-pack"}, src,
                       chests=[(src[0], src[1], "automation-science-pack")]))
    print(crew.run_goal({"do": "link_science"}))
    assert crew.belted_packs() == {"automation-science-pack"}

    def lab_packs():
        d = w.observe(8, x=sx, y=sy, include=["entities"], names=["lab"])
        return sum(((e.get("inventories") or {}).get("input") or {}).get("automation-science-pack", 0)
                   for e in d.get("entities") or [])
    got = _wait(lab_packs, 60)
    if not got:                                           # what the link looks like before failing
        d = w.observe(24, x=(sx + src[0]) / 2, y=(sy + src[1]) / 2, include=["entities"],
                      names=["fast-inserter", "inserter", "transport-belt", "iron-chest", "lab"])
        print([(e["name"], e["x"], e["y"], e.get("direction"), e.get("status"), e.get("inventories"))
               for e in d.get("entities") or [] if e["name"] != "transport-belt" or e.get("inventories")][:30])
    assert got > 0, "no red packs reached the station's labs by belt"

    # factories feed factories: a low density structure belt factory (copper from its own drills) whose steel
    # and plastic come in through input cells, belted from two chests standing in for the factories that make them
    print(crew.build({"do": "build", "module": "factory", "item": "low-density-structure", "per_minute": 2}))
    lds = crew.base.of("factory")[-1]
    assert {pt["item"] for pt in lds.params.get("ports", [])} == {"steel-plate", "plastic-bar"}
    for k, item in enumerate(("steel-plate", "plastic-bar")):
        px, py = math.floor(sx) - 14.5 - 6 * k, math.floor(sy) + 16.5
        w.observe(16, x=px, y=py)
        w.place("iron-chest", px, py, "north", {(math.floor(px), math.floor(py))})
        w.insert(px, py, item, 60)
        crew.base.add(Site("factory", {"item": item}, (px, py), chests=[(px, py, item)]))
    print(crew.run_goal({"do": "link"}))
    assert all(pt.get("fed") for pt in lds.params["ports"]), lds.params["ports"]
    ox, oy, _ = lds.chests[0]

    def lds_made():
        d = w.observe(8, x=ox, y=oy, include=["entities"])
        return next((((e.get("inventories") or {}).get("contents") or {}).get("low-density-structure", 0)
                     for e in d.get("entities") or [] if (e["x"], e["y"]) == (ox, oy)), 0)
    assert _wait(lds_made, 180) > 0, "the chained factory made no low density structure"

    ws = MODULES["workshop"]
    ws.spare = lambda base, floor=None: {"sulfur": 100.0}             # we carry sulfur: no oil needed for this check
    print(crew.build({"do": "build", "module": "workshop", "item": "battery", "per_minute": 4}))
    site = crew.base.of("workshop")[0]
    assert {c["recipe"] for c in site.cells} == {"sulfuric-acid", "battery"}
    print(crew.courier(force=True))
    batt = next(c for c in site.cells if c["recipe"] == "battery")
    ox, oy = batt["out"][0]

    def batteries():
        d = w.observe(8, x=ox, y=oy, include=["entities"])
        return next((((e.get("inventories") or {}).get("contents") or {}).get("battery", 0)
                     for e in d.get("entities") or [] if (e["x"], e["y"]) == (ox, oy)), 0)
    got = _wait(batteries, 90)
    if not got:                                           # say what the cells look like before failing
        cx, cy = site.centre
        d = w.observe(32, x=cx, y=cy, include=["entities"], names=["chemical-plant", "offshore-pump", "pipe"])
        print([(e["name"], e["x"], e["y"], e.get("status"), e.get("fluids")) for e in d.get("entities") or []][:30])
        print(list(w.refused)[-10:])
    assert got > 0, "no batteries: water, acid or the inserters do not flow"

    # oil, when the map has crude in range: pumpjack -> refinery (advanced) -> cracking -> plastic
    if w.oil_wells():
        print(crew.build({"do": "build", "module": "pumpjack", "count": 2}))
        ws.spare = lambda base, floor=None: {}
        print(crew.build({"do": "build", "module": "workshop", "item": "plastic-bar", "per_minute": 10}))
        oil = crew.base.of("workshop")[-1]
        assert any(c["machine"] == "oil-refinery" for c in oil.cells)
        print(crew.courier(force=True))
        pc = next(c for c in oil.cells if c["recipe"] == "plastic-bar")
        px, py = pc["out"][0]

        def plastic():
            d = w.observe(8, x=px, y=py, include=["entities"])
            return next((((e.get("inventories") or {}).get("contents") or {}).get("plastic-bar", 0)
                         for e in d.get("entities") or [] if (e["x"], e["y"]) == (px, py)), 0)
        got = _wait(plastic, 150)
        if not got:
            cx, cy = oil.centre
            d = w.observe(40, x=cx, y=cy, include=["entities"], names=["oil-refinery", "chemical-plant", "pumpjack"])
            print([(e["name"], e["x"], e["y"], e.get("status"), e.get("fluids"), e.get("recipe")) for e in d.get("entities") or []])
        assert got > 0, "no plastic: crude, the refinery's ports or the petroleum net do not flow"
    else:
        print("no crude oil within the scan radius on this map: the oil chain is not checked")

    print(crew.build({"do": "build", "module": "electric_mine", "resource": "iron-ore", "count": 2}))
    mine = crew.base.of("electric_mine")[0]

    def plates():
        total = 0
        for x, y, _ in mine.chests:
            d = w.observe(4, x=x, y=y, include=["entities"])
            total += sum(((e.get("inventories") or {}).get("contents") or {}).get("iron-plate", 0)
                         for e in d.get("entities") or [] if (e["x"], e["y"]) == (x, y))
        return total
    assert _wait(plates, 60) > 0, "electric mine: no plates reached its chests"

    print(crew.build({"do": "build", "module": "silo"}))
    silo = crew.base.of("silo")[0]
    print(crew.courier(force=True))
    sx, sy = silo.cells[0]["x"], silo.cells[0]["y"]

    def parts():
        d = w.observe(8, x=sx, y=sy, include=["entities"], names=["rocket-silo"])
        return next((e.get("rocket_parts") or 0 for e in d.get("entities") or []), 0)
    made = _wait(parts, 90)
    if not made:
        d = w.observe(10, x=sx, y=sy, include=["entities"])
        print([(e["name"], e["x"], e["y"], e.get("status"), e.get("inventories"), e.get("rocket_parts"))
               for e in d.get("entities") or []])
        print(list(w.refused)[-10:], silo.entities)
    assert made >= 1, "the silo made no rocket part from its chests"
    run.end_and_wait()
