"""Buildings a site never got (gaps.py) and trees that share a tile (automation_bot.Obstacles), offline.

Run 20261122-133537: two trees stood in front of a gear factory's iron port; the obstacle map kept one per tile, so
one was cleared and the other blocked the factory's first two belts. The build only noted "3 failed", the iron belt
ended one tile short of the factory and the factory never made a gear."""

import math
import pickle

import automation_bot as ab
from arena_sdk import Run
from brain import gaps
from brain.assess import RuleAssessor
from brain.modules import Site
from brain.world import FreeWorld

TREE = 0.4                      # half the side of a tree's collision box


class SizeKn:
    def size(self, name):
        return {"assembling-machine-1": (3, 3), "stone-furnace": (2, 2)}.get(name, (1, 1))


class TinyGame(Run):
    """A few tiles of Factorio: trees with real boxes (one can cover the tile next to it), 1x1 buildings,
    belts that know what they point at. Every action arrives; a tile with a tree or a building refuses a place."""

    def __init__(self, trees=(), entities=()):
        self.budgets = {"ticks_left": 10 ** 6}
        self.trees = list(trees)
        self.ents: dict[tuple[int, int], dict] = {}
        for e in entities:
            self.ents[(math.floor(e["x"]), math.floor(e["y"]))] = dict(e)
        self.log: list = []

    def _blocked(self, t) -> bool:
        if t in self.ents:
            return True
        return any(t[0] < x + TREE and x - TREE < t[0] + 1 and t[1] < y + TREE and y - TREE < t[1] + 1
                   for x, y in self.trees)

    def act(self, a):
        self.log.append(a)
        t = a["type"]
        if t == "move":
            return {"ok": True, "data": {"position": {"x": a["x"], "y": a["y"]}}}
        if t == "observe":
            return {"ok": True, "data": {"position": {"x": 0, "y": 0}, "centre": {"x": a.get("x", 0), "y": a.get("y", 0)},
                                         "radius": a["radius"], "obstacles": [[x, y, "tree"] for x, y in self.trees],
                                         "entities": self._described(),
                                         "inventory": {"transport-belt": 50, "inserter": 10}}}
        if t == "pickup":
            near = [p for p in self.trees if math.dist(p, (a["x"], a["y"])) <= 1.5]
            if not near:
                return {"ok": False, "code": "no_entity"}
            self.trees.remove(min(near, key=lambda p: math.dist(p, (a["x"], a["y"]))))
            return {"ok": True, "data": {"picked_up": {"wood": 4}, "cleared": "tree"}}
        if t == "place":
            tile = (math.floor(a["x"]), math.floor(a["y"]))
            if self._blocked(tile):
                return {"ok": False, "code": "cannot_place"}
            e = {"name": a["entity"], "x": a["x"], "y": a["y"], "direction": a.get("direction", "north")}
            self.ents[tile] = e
            return {"ok": True, "data": {"entity": e}}
        if t == "place_line":
            dx, dy = a["x2"] - a["x1"], a["y2"] - a["y1"]
            d = "east" if dx > 0 else "west" if dx < 0 else "south" if dy > 0 else "north"
            n = int(max(abs(dx), abs(dy))) + 1
            sx, sy = ab.DIRS[d]
            tiles = [(math.floor(a["x1"]) + sx * i, math.floor(a["y1"]) + sy * i) for i in range(n)]
            if any(self._blocked(q) for q in tiles):
                return {"ok": False, "code": "cannot_place", "message": "a tile is blocked"}
            for q in tiles:
                self.ents[q] = {"name": a["entity"], "x": q[0] + 0.5, "y": q[1] + 0.5, "direction": d}
            return {"ok": True, "data": {"placed": [[q[0] + 0.5, q[1] + 0.5] for q in tiles], "direction": d}}
        if t in ("set_recipe", "insert", "craft"):
            return {"ok": True, "data": {}}
        raise AssertionError(a)

    def _described(self) -> list[dict]:
        """Belts with what feeds them and what they feed, inserters with their drop tile."""
        out = []
        for tile, e in self.ents.items():
            d = dict(e)
            if e["name"] == "transport-belt":
                vx, vy = ab.DIRS[e["direction"]]
                nxt = self.ents.get((tile[0] + vx, tile[1] + vy))
                d["feeds"] = [[nxt["x"], nxt["y"]]] if nxt and nxt["name"] == "transport-belt" else {}
                d["fed_by"] = [[o["x"], o["y"]] for q, o in self.ents.items() if o["name"] == "transport-belt"
                               and (q[0] + ab.DIRS[o["direction"]][0], q[1] + ab.DIRS[o["direction"]][1]) == tile] or {}
            if e["name"] == "inserter":                     # facing = where it picks up; it drops behind
                vx, vy = ab.DIRS[e["direction"]]
                d["drop"] = [e["x"] - vx, e["y"] - vy]
            out.append(d)
        return out


def _world(game):
    w = FreeWorld(game, SizeKn(), log=lambda *a: None)
    w.pos = (45.5, -58.5)
    w.inventory = {"transport-belt": 50, "inserter": 10}
    return w


# ------------------------------------------------------------------ fix 1: every obstacle on a tile
def test_two_trees_on_one_tile_are_both_kept_and_both_cleared():
    o = ab.Obstacles()
    for pos in ((44.81, -55.94), (46.31, -55.81)):        # the two trees of run 20261122-133537
        for t in ab.World._box(*pos, 0.45, 0.45):
            o.add(t, pos)
    assert o.on([(45, -56)]) == [(44.81, -55.94), (46.31, -55.81)]
    assert sorted(o.remove((46.31, -55.81))) == [(46, -57), (46, -56)]   # its own tiles are free now
    assert (45, -56) in o and o[(45, -56)] == (44.81, -55.94)    # the shared tile is still blocked
    back = pickle.loads(pickle.dumps(o))
    assert back.on([(45, -56)]) == [(44.81, -55.94)]


def test_a_world_pickled_with_a_plain_obstacle_dict_carries_over():
    w = _world(TinyGame())
    w.__dict__.pop("_obstacles")
    w.__dict__["obstacles"] = {(3, 4): (3.5, 4.5)}       # how a save made before the fix holds it
    assert isinstance(w.obstacles, ab.Obstacles) and w.obstacles.on([(3, 4)]) == [(3.5, 4.5)]


def test_place_clears_both_trees_in_front_of_a_port():
    trees = [(44.81, -55.94), (46.31, -55.81)]
    game = TinyGame(trees)
    w = _world(game)
    w.observe(10, x=45.5, y=-55.5, include=["entities", "obstacles"])
    w.place("transport-belt", 45.5, -55.5, "north", {(45, -56)})
    assert not game.trees and (45, -56) in game.ents


def test_place_without_tiles_looks_again_and_clears_its_own_footprint():
    game = TinyGame([(44.81, -55.94)])                   # a tree the bot's map never saw
    w = _world(game)
    w.place("transport-belt", 45.5, -55.5, "north")
    assert not game.trees and (45, -56) in game.ents


# ------------------------------------------------------------------ fix 2: refused buildings become gaps
def test_fab_build_retries_and_keeps_what_stays_refused_as_gaps():
    from fab.build import build

    class Route:
        item = "iron-plate"
        path = [(45, -56), (45, -57), (44, -57)]
        kinds = {t: "belt" for t in path}
        facing = {(45, -56): "north", (45, -57): "west", (44, -57): "south"}
        pins, chests = [], []

    class Design:
        cells, poles = [], []
        routes = [Route()]

        def feed_ids(self):
            return set()

    game = TinyGame([(44.81, -55.94)])                   # unknown to the map: the retry finds it
    game.ents[(44, -57)] = {"name": "stone-furnace", "x": 44.5, "y": -56.5, "direction": "north"}  # stays in the way
    w = _world(game)
    report = build(w, Design(), log=lambda *a: None)
    assert (45, -56) in game.ents and (45, -57) in game.ents and not game.trees
    assert [(g["name"], g["x"], g["y"], g["dir"]) for g in report["gaps"]] == [("transport-belt", 44.5, -56.5, "south")]


def _site28(game_ents):
    """Site 28 of run 20261122-133537 around its iron port, as built: belts, undergrounds, inserters."""
    ents = [("transport-belt", 44.5, -56.5, "south"), ("transport-belt", 44.5, -55.5, "south"),
            ("underground-belt", 44.5, -54.5, "south"), ("underground-belt", 44.5, -51.5, "south"),
            ("transport-belt", 44.5, -50.5, "east"), ("transport-belt", 45.5, -50.5, "east"),
            ("transport-belt", 46.5, -50.5, "east"), ("transport-belt", 47.5, -51.5, "east"),
            ("transport-belt", 48.5, -51.5, "east"), ("transport-belt", 49.5, -51.5, "north"),
            ("transport-belt", 49.5, -52.5, "north"), ("inserter", 43.5, -56.5, "east"),
            ("inserter", 43.5, -50.5, "east"), ("inserter", 48.5, -50.5, "west")]
    site = Site("factory", {"item": "iron-gear-wheel", "per_minute": 65.0,
                            "ports": [{"item": "iron-plate", "end": (45, -55), "approach": (45, -54), "feed": True,
                                       "fed": True}]}, (46, -54), entities=ents, id=28)
    site.health = {"waiting_for_source_items": 8, "item_ingredient_shortage": 4}
    game_ents += [{"name": n, "x": x, "y": y, "direction": d} for n, x, y, d in ents]
    game_ents += [{"name": "transport-belt", "x": 45.5, "y": -54.5, "direction": "north"},   # the link's last belts
                  {"name": "transport-belt", "x": 45.5, "y": -53.5, "direction": "north"}]
    return site


def test_the_audit_finds_the_three_holes_of_site_28():
    ents: list = []
    site = _site28(ents)
    game = TinyGame(entities=ents)
    w = _world(game)
    found = gaps.holes(w, site, game._described())
    assert sorted((g["x"], g["y"], g["dir"]) for g in found) == [
        (45.5, -56.5, "west"), (45.5, -55.5, "north"), (47.5, -50.5, "north")]


def test_a_whole_site_has_no_holes():
    ents: list = []
    site = _site28(ents)
    ents += [{"name": "transport-belt", "x": 45.5, "y": -55.5, "direction": "north"},
             {"name": "transport-belt", "x": 45.5, "y": -56.5, "direction": "west"},
             {"name": "transport-belt", "x": 47.5, "y": -50.5, "direction": "north"}]
    game = TinyGame(entities=ents)
    assert gaps.holes(_world(game), site, game._described()) == []


class StubCrew:
    def __init__(self, w, sites):
        self.w = w
        self.base = type("B", (), {"sites": sites, "get": lambda self, i: next((s for s in sites if s.id == i), None)})()
        self.t = 0.0

    def _minute(self):
        return self.t

    def unlock_items(self, items):
        pass

    def make(self, need, extra=False):
        pass


def test_audit_alert_rule_floor_and_repair_close_the_holes():
    ents: list = []
    site = _site28(ents)
    game = TinyGame(entities=ents)
    w = _world(game)
    crew = StubCrew(w, [site])
    notes = gaps.audit(crew)
    assert notes and len(gaps.pending(site)) == 3
    al = gaps.alerts(crew)
    assert al[0]["key"] == "gap-28" and al[0]["level"] == "urgent"
    assert RuleAssessor().decide({}, al, set())["urgent_goals"] == [{"do": "repair", "site": 28}]
    placed, left = gaps.repair(crew, site)
    assert placed == 3 and not left and "gaps" not in site.params
    assert gaps.holes(w, site, game._described()) == []
    assert gaps.audit(crew) == []                         # throttled, and nothing to find any more


def test_a_gap_that_stays_blocked_gives_up_and_asks_for_a_rebuild():
    ents: list = []
    site = _site28(ents)
    game = TinyGame(entities=ents)
    w = _world(game)
    crew = StubCrew(w, [site])
    gaps.record(site, [{"name": "transport-belt", "x": 60.5, "y": -60.5, "dir": "north"}])
    game.ents[(60, -61)] = {"name": "stone-furnace", "x": 60.5, "y": -60.5, "direction": "north"}
    for _ in range(gaps.MAX_TRIES):
        gaps.repair(crew, site)
    assert not gaps.pending(site) and gaps.stuck(site)
    al = gaps.alerts(crew)
    assert al[0]["key"] == "gap-stuck-28" and al[0]["fix"]["do"] == "replace"
    assert "missing_buildings" in site.brief() and "gaps" not in site.brief_params()
