"""The designer rework, brain side: factories take plates by belt (mines are modules of their own), a place needs
a clear square the size of the factory (run 20261127-101219: green science designed four times on top of the
red-science factory, all four refused on belt routes), and chains split into blocks of 6 machines / 3 recipes."""

import json
from types import SimpleNamespace

import pytest

from arena import catalog
from brain.modules import MODULES

needs_catalog = pytest.mark.skipif(catalog.get().empty, reason="needs config/catalog.json (arena catalog)")
F = MODULES["factory"]


def _kn():
    from brain.knowledge import Knowledge
    return Knowledge(json.loads(json.dumps(catalog.get().data)))


def test_a_place_moves_onto_the_nearest_clear_square():
    built = {(x, y) for x in range(-20, 21) for y in range(-20, 21) if (x * 7 + y * 3) % 4}   # scattered buildings
    w = SimpleNamespace(water=set(), used=built, keepout=set(), cliffs=set())
    c = F._clear_centre(w, (0, 0), 16, set(), 30)
    assert c is not None
    x0, y0 = int(c[0] - 8), int(c[1] - 8)
    assert not any((x, y) in built for x in range(x0, x0 + 16) for y in range(y0, y0 + 16))
    assert F._clear_centre(w, (0, 0), 16, set(), 5) is None             # nothing clear within reach
    ore = {(x, y) for x in range(20, 60) for y in range(-40, 40)}
    c2 = F._clear_centre(SimpleNamespace(water=set(), used=built, keepout=set(), cliffs=set()), (0, 0), 16, ore, 40)
    assert c2 is not None and not any((x, y) in ore for x in range(int(c2[0] - 8), int(c2[0] + 8))
                                      for y in range(int(c2[1] - 8), int(c2[1] + 8)))


@needs_catalog
def test_chains_split_into_small_blocks_and_plates_always_come_in():
    kn = _kn()
    from fab import recipes as fr
    early, _ = F.free_supplied(kn, {"item": "automation-science-pack", "per_minute": 30})
    assert "iron-gear-wheel" in early               # no undergrounds yet: gears always from a feeder
    kn.unlocked = set(kn.unlocked) | {"underground-belt"}
    for item, rate in (("logistic-science-pack", 15), ("chemical-science-pack", 10), ("automation-science-pack", 15)):
        sup, pl = F.free_supplied(kn, {"item": item, "per_minute": rate})
        asm = {k: v for k, v in pl.machines.items() if k in fr.RECIPES and k not in sup}
        assert {"iron-plate", "copper-plate"} <= sup or item == "chemical-science-pack"
        assert len(asm) <= F.MAX_RECIPES and (sum(asm.values()) <= F.MAX_ASM or asm.get(item, 0) >= F.MAX_ASM)
    sup, _ = F.free_supplied(kn, {"item": "logistic-science-pack", "per_minute": 15})
    assert {"iron-gear-wheel", "electronic-circuit"} <= sup                # their own feeder factories
    big, _ = F.free_supplied(kn, {"item": "logistic-science-pack", "per_minute": 30})   # 8 green machines alone:
    assert "inserter" not in big and "transport-belt" not in big           # no pointless split for size
    assert {"iron-plate", "copper-plate"} <= F.asked_inputs(kn, {"item": "inserter", "plates": "mine"})
    w = SimpleNamespace(kn=kn)
    assert 16 <= F._side_for(w, {"item": "logistic-science-pack", "per_minute": 15}) <= 34
