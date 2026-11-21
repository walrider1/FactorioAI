"""Where the brain bot used to get stuck for good: unreachable ore, an unreachable storage chest retried hundreds
of times, one crew fault failing every goal, and the oil-processing trigger (steel and a pumpjack)."""

import json
import math

import pytest

from arena import catalog
from brain.bot import _signature
from brain.crew import Crew
from brain.modules import MODULES
from brain.world import BuildError, FreeWorld


# ------------------------------------------------------------------ fakes
class FakeRun:
    """Just enough of the arena for FreeWorld.mine: moves always arrive, some tiles are out of reach."""

    def __init__(self, unreachable=()):
        self.unreachable = set(unreachable)
        self.budgets = {"ticks_left": 10 ** 6}
        self.mined = []

    def move(self, x, y):
        return {"ok": True, "data": {"position": {"x": x, "y": y}}}

    def act(self, action):
        if action["type"] == "mine":
            t = (int(action["x"] // 1), int(action["y"] // 1))
            if t in self.unreachable:
                return {"ok": False, "code": "out_of_reach", "message": "hand mining reach is 3 tiles"}
            self.mined.append(t)
            return {"ok": True, "data": {"mined": {"stone": action["count"]}, "hands_free_tick": 0}}
        raise AssertionError(action)


class SizeKn:
    def size(self, name):
        return {"pumpjack": (3, 3), "stone-furnace": (2, 2)}.get(name, (1, 1))


def _world(run):
    w = FreeWorld(run, SizeKn(), log=lambda *a: None)
    w.pos = (0.5, 0.5)
    return w


# ------------------------------------------------------------------ mining
def test_mine_skips_tiles_it_cannot_reach():
    run = FakeRun(unreachable={(1, 0)})
    w = _world(run)
    w.resources["stone"] = {(1, 0), (6, 0)}
    assert w.mine("stone", 5) == 5
    assert run.mined == [(6, 0)]                       # the walled-in tile was skipped, not a failed goal


def test_mine_skips_ore_under_our_buildings():
    run = FakeRun()
    w = _world(run)
    w.resources["stone"] = {(1, 0), (6, 0)}
    w.used.add((1, 0))                                 # a burner drill stands on it
    w.mine("stone", 3)
    assert run.mined == [(6, 0)]


def test_mine_says_why_when_no_tile_is_reachable():
    w = _world(FakeRun(unreachable={(1, 0), (6, 0)}))
    w.resources["stone"] = {(1, 0), (6, 0)}
    with pytest.raises(BuildError, match="could reach none"):
        w.mine("stone", 5)


# ------------------------------------------------------------------ storage chest
class StorageWorld:
    """A stub world for Crew._new_storage: the game refuses the first spot it is offered, and only the ground
    right around the character has been seen (no clearing for a storage yard)."""

    def __init__(self):
        self.pos, self.inventory, self.resources, self.research = (10.5, 10.5), {"iron-chest": 2}, {}, {}
        self.stats, self.speed, self.recorder, self.kn = {}, 1.0, None, None
        self.placed, self.refusals = [], 1
        self.seen = {(x, y) for x in range(3, 18) for y in range(3, 18)}
        self.keepout, self.used = set(), set()

    def observe(self, *a, **k):
        return {}

    def blocked(self, t):
        return t in self.used

    def free(self, tiles):
        return True

    def sealed(self, extra=frozenset()):
        return False

    def place(self, name, x, y, direction="north", tiles=()):
        if self.refusals:
            self.refusals -= 1
            raise BuildError(f"place {name} at ({x}, {y}) north: out_of_reach out_of_reach")
        self.placed.append((x, y))
        return {"name": name, "x": x, "y": y}

    def now_tick(self):
        return 0


def test_storage_chest_goes_within_reach_and_never_retries_a_refused_tile():
    w = StorageWorld()
    crew = Crew(w, log=lambda *a: None)
    with pytest.raises(BuildError):
        crew._new_storage()
    refused = next(iter(crew._bad_storage))
    x, y = crew._new_storage()
    assert (x - 0.5, y - 0.5) != refused                        # another tile this time
    assert max(abs(x - w.pos[0]), abs(y - w.pos[1])) <= 6      # no clearing known: beside the character


# ------------------------------------------------------------------ circuit breaker
def test_error_signature_ignores_numbers():
    a = "place iron-chest at (2.5, -12.5) north: out_of_reach out_of_reach"
    b = "place iron-chest at (7.5, 3.5) north: out_of_reach out_of_reach"
    assert _signature(a) == _signature(b)
    assert _signature(a) != _signature("mine stone: out_of_reach hand mining reach is 3 tiles")


# ------------------------------------------------------------------ pumpjack
class OilWorld:
    def __init__(self, wells, blocked=()):
        self.pos, self.inventory, self.wells, self.blocked_tiles = (0.0, 0.0), {"pumpjack": 1, "small-electric-pole": 1}, wells, set(blocked)
        self.placed = []

    def oil_wells(self, near=None):
        return list(self.wells)

    def footprint(self, name, x, y, direction="north"):
        w = 3 if name == "pumpjack" else 1
        return {(tx, ty) for tx in range(math.floor(x - w / 2 + 0.01), math.floor(x + w / 2 - 0.01) + 1)
                for ty in range(math.floor(y - w / 2 + 0.01), math.floor(y + w / 2 - 0.01) + 1)}

    def go(self, x, y):
        self.pos = (x, y)

    def observe(self, *a, **k):
        return {}

    def blocked(self, t):
        return t in self.blocked_tiles

    def free(self, tiles):
        return not any(t in self.blocked_tiles for t in tiles)

    def place(self, name, x, y, direction="north", tiles=()):
        self.inventory[name] -= 1
        self.blocked_tiles |= set(tiles)
        self.placed.append((name, x, y))
        return {"name": name, "x": x, "y": y}


class PoweredBase:
    centre = None
    sites: list = []

    def powered_poles(self):
        return [(0, 0)]


def test_pumpjack_takes_the_next_well_when_one_is_covered():
    w = OilWorld([(0.5, 0.5), (10.5, 0.5)], blocked={(0, 0)})
    site = MODULES["pumpjack"].build(w, PoweredBase(), {"count": 1})
    assert ("pumpjack", 10.5, 0.5) in w.placed and site.params["count"] == 1
    assert site.poles and any(n == "small-electric-pole" for n, *_ in w.placed)
    assert site.centre == (10.5, 0.5)


def test_pumpjack_needs_power():
    w = OilWorld([(0.5, 0.5)])

    class NoPower(PoweredBase):
        def powered_poles(self):
            return []
    with pytest.raises(BuildError, match="power"):
        MODULES["pumpjack"].build(w, NoPower(), {"count": 1})


# ------------------------------------------------------------------ steel and the oil trigger
needs_catalog = pytest.mark.skipif(catalog.get().empty, reason="needs config/catalog.json (arena catalog)")


@needs_catalog
def test_steel_is_smelted_not_blocked():
    from brain.knowledge import Knowledge
    kn = Knowledge(json.loads(json.dumps(catalog.get().data)))
    kn.learn_research(["steam-power", "electronics", "oil-gathering"])
    p = kn.kit_plan({"pumpjack": 1})
    assert not p.blocked                                   # it used to be "cannot hand-craft steel-plate"
    assert p.raw.get("steel-plate") == 5 and "iron-plate" in p.raw


def test_opening_book_goes_on_to_oil_after_steel():
    from brain.brain import ScriptedStrategist
    rep = {"base": [{"kind": "burner_mine", "resource": r, "count": n} for r, n in
                    (("iron-ore", 9), ("coal", 2), ("stone", 1), ("copper-ore", 4))]
           + [{"kind": "steam_power"}, {"kind": "steam_power"}, {"kind": "labs", "count": 2},
              {"kind": "factory", "item": "automation-science-pack", "per_minute": 6.0},
              {"kind": "factory", "item": "logistic-science-pack", "per_minute": 12.0}],
           "researched": ["automation", "electric-mining-drill", "logistics", "logistic-science-pack",
                          "steel-processing"], "recent_results": [], "research": {}}
    s = ScriptedStrategist()
    assert s.plan(rep)["goals"][0] == {"do": "research", "tech": "oil-processing"}
    rep["researched"] += ["oil-processing"]
    assert s.plan(rep)["goals"][0] == {"do": "research", "tech": "plastics"}


# ------------------------------------------------------------------ fixer
def test_rule_fixer_digs_out_a_wedged_character_then_sets_the_goal_aside():
    from brain.fixer import RuleFixer, apply
    sit = {"error": "place iron-chest at (2.5, -12.5) north: out_of_reach out_of_reach",
           "character": {"at": [1.5, -34.0], "walk_fails": 4},
           "around_character": [{"name": "burner-mining-drill", "x": 1, "y": -35, "site": 2},
                                {"name": "iron-chest", "x": 9.5, "y": -35.5, "site": 2}]}
    d = RuleFixer().diagnose(sit)
    assert d["fixes"][0] == {"fix": "pickup", "x": 1, "y": -35} and d["fixes"][-1]["fix"] == "skip"

    class W:
        walk_fails = 4
        picked = []

        def pickup(self, *e):
            self.picked.append(e)
            return True

        def now_tick(self):
            return 1000

    class S:
        entities = [("burner-mining-drill", 1, -35, "east")]

    class C:
        w = W()
        base = type("B", (), {"sites": [S()]})()

    goals, skipped = [], {}
    done = apply(C(), d["fixes"] + [{"fix": "retry", "goal": {"do": "wait", "seconds": 5}}], goals, skipped, "k")
    assert C.w.picked == [("burner-mining-drill", 1, -35, "east")] and not S.entities   # out of its site too
    assert skipped["k"] > 1000 and goals == [{"do": "wait", "seconds": 5}] and len(done) == 3


def test_think_keeps_the_base_tended_and_says_what_it_works_on():
    import time as _t
    w = _world(FakeRun())
    tended, seen = [], []
    w.tend = lambda: (tended.append(1), seen.append(w.activity))

    def slow(x):
        _t.sleep(4.5)
        return x * 2
    assert w.think("designing a factory", slow, 21) == 42
    assert tended and all(a.startswith("designing a factory (") for a in seen)


def test_lab_row_gets_a_feeder_chest_and_inserter_at_its_head():
    from brain.modules import Labs, Site

    class W:
        def __init__(self):
            self.placed = []

        def free(self, tiles):
            return True

        def place(self, name, x, y, direction="north", tiles=()):
            self.placed.append((name, x, y, direction))

    s = Site("labs", {"count": 2}, (0, 0), labs=[(10.5, 5.5), (14.5, 5.5)])
    w = W()
    assert Labs.add_feeder(w, s)
    assert ("iron-chest", 7.5, 5.5, "north") in w.placed
    assert ("inserter", 8.5, 5.5, "west") in w.placed          # picks from the chest (west), drops into lab 1
    assert (7.5, 5.5, "lab-feed") in s.chests
    assert not Labs.add_feeder(w, s)                            # never twice


def test_an_alert_without_a_fix_does_not_break_the_report():
    from brain.assess import detect
    rows = [{"level": "urgent", "key": "walk", "what": "stuck"}]          # an alert some code made without "fix"
    assert [{k: a.get(k) for k in ("level", "what", "fix")} for a in rows][0]["fix"] is None
    import inspect
    src = inspect.getsource(detect)
    assert '"key": "walk"' in src and '"fix": None' in src.split('"key": "walk"')[1][:400]


# ------------------------------------------------------------------ a pocket bigger than the old probes
# run 20261119-105322: a 7x4 pocket walled in by burner mines and furnaces. 7-tile probes walked from one end
# to the other, so boxed_in said "free" and the dig-out never ran; the 4-tile seal check let it close.
POCKET = {(x, y) for x in range(11, 19) for y in range(-33, -29)}            # where the character could walk


def _pocket_world():
    w = FreeWorld(FakeRun(), None, log=lambda *a: None)
    w.seen = {(x, y) for x in range(-20, 50) for y in range(-60, 0)}
    ring = {(x, y) for x in range(9, 21) for y in range(-35, -27)} - POCKET
    w.used = set(ring)
    w.pos = (18.5, -30.5)
    return w


class PocketMoves:
    """Moves inside the pocket work; anything outside it is no_path until a wall comes down."""
    budgets: dict = {}

    def __init__(self):
        self.open, self.pos = False, (18.5, -30.5)

    def move(self, x, y):
        t = (math.floor(x), math.floor(y))
        if self.open or t in POCKET:
            self.pos = (x, y)
            return {"ok": True, "data": {"position": {"x": x, "y": y}}}
        return {"ok": False, "code": "no_path", "data": {}}


def test_a_pocket_longer_than_the_short_probe_is_still_boxed_in():
    w = _pocket_world()
    w.run = PocketMoves()
    assert w.boxed_in()                               # 7 tiles works inside the pocket, 24 does not
    w.run.open = True
    assert not w.boxed_in()


def test_the_building_that_closes_a_big_pocket_is_seen_before_it_is_placed():
    w = _pocket_world()
    gap = {(15, -29), (16, -29), (15, -28), (16, -28)}  # the last way out (the wall is two tiles thick)
    w.used -= gap
    assert not w.sealed()
    assert w.sealed(gap)                               # the old 4-tile look called this open
    assert not w.sealed(gap, start=(15, -27))          # standing outside, it stays open
