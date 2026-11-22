"""Basic oil processing takes crude in the refinery's second input and gives petroleum from its third output (the
recipe's fluidbox_index). Run 20261121-140755 piped crude into the first input: the refinery never ran, both
pumpjacks stood full for hours, and plastic and blue science waited behind it."""

import json
from types import SimpleNamespace

import pytest

from arena import catalog

pytestmark = pytest.mark.skipif(catalog.get().empty, reason="needs config/catalog.json (arena catalog)")


def _kn():
    from brain.knowledge import Knowledge
    return Knowledge(json.loads(json.dumps(catalog.get().data)))


def test_basic_oil_processing_uses_the_refinerys_crude_input_and_petroleum_output():
    kn = _kn()
    assert kn.fluid_slots("oil-refinery", "basic-oil-processing") == {2: "crude-oil", 5: "petroleum-gas"}
    assert kn.fluid_slots("oil-refinery", "advanced-oil-processing") == {
        1: "water", 2: "crude-oil", 3: "heavy-oil", 4: "light-oil", 5: "petroleum-gas"}
    assert kn.fluid_slots("chemical-plant", "plastic-bar")[1] == "petroleum-gas"


def test_a_refinery_piped_on_the_old_connections_is_piped_again():
    """The live layout of site 81: refinery at (-108.5, 69.5), crude pipe at the first input's target (-110, 72)
    fed by an underground from the west, petroleum pipe at the first output's target (-111, 66)."""
    from brain.workshop import District, Workshop
    from brain.modules import Site
    kn = _kn()
    ws = Workshop()
    d = District(-150, 40, 6, 4)
    d.idx = 0
    old = {1: "crude-oil", 3: "petroleum-gas"}                       # what the floor was piped for
    for pt in kn.ports("oil-refinery", -108.5, 69.5, "north"):
        d.nets.reserved[tuple(pt["target"])] = old.get(pt["box"], "")
    d.nets.pipe.update({(-110, 72): "crude-oil", (-111, 66): "petroleum-gas", (-101, 66): "petroleum-gas"})
    d.nets.ptg.update({(-111, 72): ("crude-oil", 1), (-110, 66): ("petroleum-gas", 3),
                       (-102, 66): ("petroleum-gas", 1)})
    d.nets.spans.append(("petroleum-gas", 0, 66, -110, -102))
    ws.floors, ws.district = [d], d
    site = Site("workshop", {"item": "plastic-bar"}, (-100, 70))
    site.id = 81
    site.cells = [{"machine": "oil-refinery", "recipe": "basic-oil-processing", "x": -108.5, "y": 69.5, "floor": 0}]
    refinery = {(x, y) for x in range(-111, -106) for y in range(67, 72)}
    w = SimpleNamespace(kn=kn, free=lambda ts: not (set(ts) & refinery))

    bad = ws.misports(w, site)
    assert {(b["target"], b["fluid"]) for b in bad if b["fluid"]} == {((-108, 72), "crude-oil"),
                                                                     ((-107, 66), "petroleum-gas")}
    gaps = ws.repipe_plan(w, site)
    tiles = {tuple(g[2]): g[0] for g in gaps}
    assert tiles[(-108, 72)] == "crude-oil" and tiles[(-107, 66)] == "petroleum-gas"
    assert ws.misports(w, site) == []                                # the floor's books are right now
    assert ws.repipe_plan(w, site) == []


def test_the_rules_repipe_a_mispiped_workshop_at_once():
    import brain.assess as a
    from brain.modules import MODULES
    crew = SimpleNamespace(base=SimpleNamespace(sites=[], of=lambda k: [SimpleNamespace(id=81)] if k == "workshop" else []),
                           w=SimpleNamespace())
    ws = MODULES["workshop"]
    old = ws.misports
    ws.misports = lambda w, s: [{"fluid": "crude-oil", "target": (-108, 72), "machine": "oil-refinery"}]
    try:
        alerts = a.pipe_alerts(crew)
    finally:
        ws.misports = old
    al = next(x for x in alerts if x["key"] == "pipe-ports-81")
    assert al["level"] == "urgent" and al["fix"] == {"do": "repipe", "site": 81}
    assert al["key"].startswith(a.CRITICAL) and "repipe" in a.GOAL_DOS


def test_a_starved_factory_gets_its_chest_input_carried_in_when_a_kit_needs_its_product(monkeypatch):
    """Run 20261121-140755: "stock pump" failed with "nothing we built makes it" while the engine-unit factory
    waited on steel its belt never brought; the crew now carries the steel in and waits for the engines."""
    from brain.crew import Crew
    import brain.crew as crew_mod
    monkeypatch.setattr(crew_mod.time, "sleep", lambda s: None)
    kn = _kn()
    fac = SimpleNamespace(id=103, kind="factory", chests=[(-213.5, 43.5, "engine-unit")], health={},
                          params={"item": "engine-unit", "ports": [
                              {"item": "steel-plate", "chest": (-219, 51)}, {"item": "iron-plate", "chest": None}]})
    out = {"engine-unit": 0}
    inv = {}
    put = []

    def insert(x, y, item, n):
        put.append((x, y, item, n))
        inv[item] -= n
        out["engine-unit"] += n                       # the factory turns each steel into an engine
        return n

    def grab(item, n):
        k = min(n, out[item])
        out[item] -= k
        return k

    def gather(item, total):
        inv[item] = max(inv.get(item, 0), total)

    me = SimpleNamespace(kn=kn, base=SimpleNamespace(of=lambda k: [fac] if k == "factory" else []),
                         _grab=grab, gather=gather, log=lambda *a: None, checkpoint=lambda: None,
                         foreman=SimpleNamespace(note=lambda *a: None, patrol=lambda **k: None),
                         w=SimpleNamespace(inventory=inv, insert=insert, speed=1000.0, doing=lambda *a: None),
                         FACTORY_WAIT_MIN=Crew.FACTORY_WAIT_MIN)
    assert Crew.from_factory(me, "engine-unit", 3) == 3
    assert put == [(-218.5, 51.5, "steel-plate", 3)]  # only the chest port, only what 3 engines take
