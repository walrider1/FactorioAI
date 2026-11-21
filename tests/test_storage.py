"""Storage chests: a yard in a clearing (not beside wherever the character stood), a full inventory left in
storage so removals and mining go on, kits taken back out of storage, and a chest a build needs moved away
(run 20261119-011655: 22 removals stopped on a full inventory, and chests parked around a boiler blocked a pipe)."""

import math
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from brain.crew import INVENTORY_SLOTS, Crew
from brain.modules import Site
from brain.world import BuildError

STACK = {"iron-plate": 100, "stone-furnace": 50, "transport-belt": 100, "iron-chest": 50, "coal": 50,
         "electric-mining-drill": 50, "iron-gear-wheel": 100}


class Kn:
    items = {k: {"stack_size": v} for k, v in STACK.items()}

    def recipe_for(self, item, hand=False):
        return item

    def kit_plan(self, kit, inventory, extra=False, supplied=()):
        return SimpleNamespace(blocked=[], raw={}, steps=[(k, v - inventory.get(k, 0)) for k, v in kit.items()
                                                          if v > inventory.get(k, 0)])


class YardWorld:
    """Open, seen ground with buildings packed around the character; chests behave like the game's."""

    def __init__(self, crowd=4):
        self.kn, self.pos = Kn(), (0.5, 0.5)
        self.inventory = {"iron-chest": 5}
        self.resources, self.research, self.stats, self.speed, self.recorder = {}, {}, {}, 1.0, None
        self.seen = {(x, y) for x in range(-60, 60) for y in range(-60, 60)}
        self.used = {(x, y) for x in range(-crowd, crowd + 1) for y in range(-crowd, crowd + 1)} - {(0, 0)}
        self.keepout, self.movable, self.make_way = set(), set(), None
        self.chests: dict[tuple, dict] = {}
        self.crafted, self.steps = [], []

    @contextmanager
    def step(self, name):
        yield

    def blocked(self, t):
        return t in self.used

    def doing(self, *a):
        pass

    def sealed(self, extra=frozenset()):
        return False

    def observe(self, *a, **k):
        return {}

    def refresh(self):
        return self.inventory

    def now_tick(self):
        return 0

    def craft(self, recipe, n=1):
        self.crafted.append((recipe, n))
        self.inventory[recipe] = self.inventory.get(recipe, 0) + n

    def clear(self, tiles):                         # FreeWorld.clear: a storage chest in the way moves first
        if self.make_way is not None and set(tiles) & self.movable:
            self.make_way(set(tiles))

    def place(self, name, x, y, direction="north", tiles=()):
        self.clear(tiles)
        t = (math.floor(x), math.floor(y))
        assert t not in self.used, f"{name} placed on a taken tile {t}"
        self.used.add(t)
        self.inventory[name] -= 1
        if name.endswith("chest"):
            self.chests[(x, y)] = {}
        if self.recorder is not None:
            self.recorder.append((name, x, y, direction))
        return {"name": name, "x": x, "y": y}

    def slots(self):
        return sum(math.ceil(n / STACK.get(i, 50)) for i, n in self.inventory.items() if n > 0)

    def insert(self, x, y, item, count):
        n = min(count, self.inventory.get(item, 0))
        self.inventory[item] -= n
        held = self.chests[(x, y)]
        held[item] = held.get(item, 0) + n
        return n

    def extract(self, x, y, item, count):
        held = self.chests[(x, y)]
        n = min(count, held.get(item, 0))
        if n and self.slots() >= INVENTORY_SLOTS:
            return 0
        held[item] -= n
        self.inventory[item] = self.inventory.get(item, 0) + n
        return n

    def pickup(self, name, x, y, direction="north"):
        if (x, y) in self.chests:
            assert not any(self.chests[(x, y)].values()), "picked up a chest that still holds things"
            del self.chests[(x, y)]
        elif self.slots() >= INVENTORY_SLOTS:
            raise BuildError("inventory full while removing")
        self.used.discard((math.floor(x), math.floor(y)))
        self.inventory[name] = self.inventory.get(name, 0) + 1
        return True


def _crew(w):
    crew = Crew(w, log=lambda *a: None)
    crew.foreman = SimpleNamespace(note=lambda *a: None, forget=lambda *a: None)
    return crew


def test_storage_chests_go_into_a_yard_in_a_clearing_not_beside_the_character():
    w = YardWorld()
    w.inventory.update({"iron-plate": 6500, "coal": 300})                  # 65 + 6 slots: room is short
    crew = _crew(w)
    crew.ensure_space(12)
    spots = [(math.floor(x), math.floor(y)) for x, y in crew.storage_chests()]
    assert spots and crew.yard is not None
    assert all(s in crew._yard_tiles(crew.yard) for s in spots)            # one yard, chests side by side
    x0, y0 = crew.yard
    ring = {(x0 + i, y0 + j) for i in range(-2, 6) for j in range(-2, 4)} - set(crew._yard_tiles(crew.yard))
    assert not ring & w.used                                               # free ground all round it
    assert INVENTORY_SLOTS - w.slots() >= 12 and w.inventory["coal"] >= 200   # fuel stays on hand


def test_a_removal_that_fills_the_inventory_leaves_things_in_storage_and_goes_on():
    w = YardWorld(crowd=0)
    w.inventory.update({"stone-furnace": 40 * 50, "transport-belt": 40 * 100})   # all 80 slots taken
    crew = _crew(w)
    drills = [("electric-mining-drill", 20.5 + 3 * k, 20.5, "north") for k in range(6)]
    for _, x, y, _d in drills:
        w.used.add((math.floor(x), math.floor(y)))
    site = crew.base.add(Site("electric_mine", {}, (25, 20), entities=list(drills), built_minute=-100))
    msg = crew.remove(site.id, force=True)
    assert "6 buildings back" in msg and crew.base.get(site.id) is None
    assert w.inventory["electric-mining-drill"] == 6
    assert sum(crew.storage_held[p].get("stone-furnace", 0) + crew.storage_held[p].get("transport-belt", 0)
               for p in crew.storage_held) > 0


def test_a_kit_comes_back_out_of_storage_instead_of_being_crafted_again():
    w = YardWorld()
    crew = _crew(w)
    pos = crew._new_storage()
    w.inventory["electric-mining-drill"] = 4
    crew._stash("electric-mining-drill", 4)
    assert w.inventory["electric-mining-drill"] == 0 and crew.stored("electric-mining-drill") == 4
    crew.make({"electric-mining-drill": 4})
    assert w.inventory["electric-mining-drill"] == 4 and w.chests[pos]["electric-mining-drill"] == 0
    assert not [c for c in w.crafted if c[0] == "electric-mining-drill"]


def test_a_build_that_needs_a_storage_chests_tile_moves_the_chest_with_its_contents():
    w = YardWorld()
    crew = _crew(w)
    first = crew._new_storage()
    w.inventory["iron-plate"] = 900
    crew._stash("iron-plate", 900)
    tile = (math.floor(first[0]), math.floor(first[1]))
    assert tile in w.movable                                               # layouts may plan over it
    w.inventory["stone-furnace"] = 1
    w.place("stone-furnace", first[0], first[1], "north", {tile})           # the build clears the tile
    assert first not in w.chests and first not in crew.storage_chests()
    assert tile not in w.movable
    assert sum(h.get("iron-plate", 0) for h in w.chests.values()) == 900   # nothing lost on the way
    assert crew.stored("iron-plate") == 900
    # the build took the yard: the next yard is well away from it
    new = crew.storage_chests()
    assert new and all(math.dist(p, first) > crew.YARD_AWAY for p in new)


def test_storage_tiles_are_free_to_plan_on_but_still_block_walking_and_standing():
    import json

    from arena import catalog
    from brain.knowledge import Knowledge
    from brain.world import FreeWorld
    if catalog.get().empty:
        pytest.skip("needs config/catalog.json")

    class NoRun:
        budgets: dict = {}

        def act(self, action):
            raise AssertionError(action)

    fw = FreeWorld(NoRun(), Knowledge(json.loads(json.dumps(catalog.get().data))), log=lambda *a: None)
    fw.seen = {(x, y) for x in range(-5, 5) for y in range(-5, 5)}
    fw.used = {(1, 1), (2, 2)}
    fw.movable = {(1, 1)}                                                  # a storage chest; (2, 2) is not
    assert fw.free({(1, 1), (0, 0)}) and not fw.free({(2, 2)})
    assert fw.blocked((1, 1))
    moved = []
    fw.make_way = lambda tiles: moved.append(set(tiles))
    fw.obstacles = {}
    fw.clear({(1, 1), (0, 1)})
    assert moved == [{(1, 1), (0, 1)}]


def test_the_storage_site_is_never_removed():
    crew = _crew(YardWorld())
    crew._new_storage()
    site = crew.base.of("storage")[0]
    with pytest.raises(BuildError, match="storage chests move by themselves"):
        crew.remove(site.id)
