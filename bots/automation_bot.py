"""Scripted baseline for the Automation level: a small, fully automated red-science factory (tier 1).

Organizer test bot and a reference for teams. It plays only through the public API.

Layout, built next to the iron patch (x grows east, y grows south; X, Y = block anchor tile):

    Y-6   chest                       <- red science ends up here
    Y-5   inserter (pole)
    Y-4.. red-science assembler  <- inserter <- copper belt (from a burner drill + furnace on copper)
    Y-1   inserter        (pole)
    Y..   gear assembler
    Y+3   inserter inserter (pole)
    Y+4.. furnace  furnace
    Y+6.. drill    drill              <- burner drills on iron, dropping straight into the furnaces

Power: offshore pump -> pipes -> boiler -> steam engine at the nearest water, with a chest and a
burner inserter feeding the boiler coal, and a line of small poles to the block. Fuel goes in by
hand (that is allowed); every plate, gear and pack is moved by the factory.

    python bots/automation_bot.py --url http://127.0.0.1:8710 --token fa_...
"""

from __future__ import annotations

import argparse
import heapq
import math
import re
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sdk"))
from arena_sdk import ArenaClient, Run  # noqa: E402

Tile = tuple[int, int]
DIRS = {"north": (0, -1), "east": (1, 0), "south": (0, 1), "west": (-1, 0)}
REACH = 9.0          # stay a little inside the real 10


def tile(x: float, y: float) -> Tile:
    return math.floor(x), math.floor(y)


def centre(t: Tile) -> tuple[float, float]:
    return t[0] + 0.5, t[1] + 0.5


class BuildError(RuntimeError):
    pass


class Obstacles(dict):
    """Trees and rocks by tile. As a dict: tile -> the position of one obstacle on it (enough for "is this tile
    blocked"). `every` keeps all of them: one tree's trunk can cover the tile next to its own, so two trees often
    cover the same tile. Run 20261122-133537: a map that kept one obstacle per tile cleared one of the two trees in
    front of a gear factory's input port; the other one blocked the factory's first two belts and the factory got
    no iron for the rest of the run.

        add(tile, pos)   one more obstacle covers the tile
        on(tiles)        every obstacle covering any of these tiles (each once)
        remove(pos)      that obstacle is gone: the tiles it covered stay blocked if another one covers them too
    """

    def _every(self) -> dict:
        # set lazily: unpickling a dict subclass sets its items before its __dict__ comes back
        return self.__dict__.setdefault("every", {})

    def add(self, t: Tile, pos: tuple[float, float]) -> None:
        on = self._every().setdefault(t, [])
        if pos not in on:
            on.append(pos)
        super().__setitem__(t, on[0])

    def __setitem__(self, t: Tile, pos: tuple[float, float]) -> None:
        self.add(t, pos)

    def __delitem__(self, t: Tile) -> None:
        super().__delitem__(t)
        self._every().pop(t, None)

    def pop(self, t, *default):
        self._every().pop(t, None)
        return super().pop(t, *default)

    def clear(self) -> None:
        super().clear()
        self._every().clear()

    def on(self, tiles) -> list[tuple[float, float]]:
        out: list = []
        every = self._every()
        for t in sorted(tiles):
            for pos in every.get(t) or ([self[t]] if t in self else []):
                if pos not in out:
                    out.append(pos)
        return out

    def remove(self, pos: tuple[float, float]) -> list[Tile]:
        """Forget one obstacle; returns the tiles that are free of obstacles now."""
        freed = []
        every = self._every()
        for t in [t for t, v in self.items() if v == pos or pos in (every.get(t) or ())]:
            left = [q for q in (every.get(t) or [self[t]]) if q != pos]
            if left:
                every[t] = left
                super().__setitem__(t, left[0])
            else:
                del self[t]
                freed.append(t)
        return freed


class World:
    """What the bot has seen: resources, water, obstacles, and the tiles it has built on."""

    def __init__(self, run: Run, log=print):
        self.run, self.log = run, log
        self.resources: dict[str, set[Tile]] = {}
        self.water: set[Tile] = set()
        self.cliffs: set[Tile] = set()
        self.obstacles = Obstacles()          # tile -> position to pick up (all of them: Obstacles.on)
        self.seen: set[Tile] = set()
        self.used: set[Tile] = set()
        self.keepout: set[Tile] = set()       # stays free for belts and pipes: no buildings here
        self.summary: dict = {}
        self.water_summary: dict | None = None
        self.water_hints: list[tuple[float, float]] = []   # nearest water from every observe so far
        self.pos = (0.0, 0.0)

    @property
    def obstacles(self) -> Obstacles:
        o = self.__dict__.get("_obstacles")
        if o is None:                         # a world pickled before Obstacles: its plain dict carries over
            o = self.__dict__["_obstacles"] = Obstacles()
            for t, pos in (self.__dict__.pop("obstacles", None) or {}).items():
                o.add(t, pos)
        return o

    @obstacles.setter
    def obstacles(self, value) -> None:
        if not isinstance(value, Obstacles):
            o = Obstacles()
            for t, pos in (value or {}).items():
                o.add(t, pos)
            value = o
        self.__dict__["_obstacles"] = value

    # ------------------------------------------------------------ sensing
    def observe(self, radius: int = 20) -> dict:
        d = self.run.observe(radius)["data"]
        self.pos = (d["position"]["x"], d["position"]["y"])
        px, py = self.pos
        for tx in range(math.floor(px - radius), math.ceil(px + radius) + 1):
            for ty in range(math.floor(py - radius), math.ceil(py + radius) + 1):
                if math.hypot(tx + 0.5 - px, ty + 0.5 - py) <= radius - 1:
                    self.seen.add((tx, ty))
                    self.obstacles.pop((tx, ty), None)      # refreshed below
        for name, tiles in (d.get("resources") or {}).items():
            self.resources.setdefault(name, set()).update(tile(x, y) for x, y, *_ in tiles)
        self.water.update(tile(x, y) for x, y in d.get("water") or [])
        for x, y, kind in d.get("obstacles") or []:
            if kind == "cliff":
                self.cliffs.update(self._box(x, y, 1.5, 1.5))
                continue
            half_w, half_h = (0.45, 0.45) if kind == "tree" else (1.6, 1.2)
            for t in self._box(x, y, half_w, half_h):
                self.obstacles.add(t, (x, y))
        for e in d.get("entities") or []:
            self.used.add(tile(e["x"], e["y"]))
        self.summary = d.get("resource_summary") or {}
        self.water_summary = d.get("water_summary")
        if self.water_summary:
            n = self.water_summary["nearest"]
            self.water_hints.append((n["x"], n["y"]))
        self.inventory = d.get("inventory") or {}
        return d

    @staticmethod
    def _box(x: float, y: float, hw: float, hh: float) -> set[Tile]:
        return {(tx, ty) for tx in range(math.floor(x - hw), math.floor(x + hw) + 1)
                for ty in range(math.floor(y - hh), math.floor(y + hh) + 1)}

    def blocked(self, t: Tile) -> bool:
        return t in self.water or t in self.cliffs or t in self.used

    def free(self, tiles, need_seen: bool = True) -> bool:
        return all(not self.blocked(t) and t not in self.keepout and (t in self.seen or not need_seen)
                   for t in tiles)

    # ------------------------------------------------------------- acting
    def go(self, x: float, y: float) -> None:
        if math.hypot(x - self.pos[0], y - self.pos[1]) < 0.5:
            return
        r = self.run.move(x, y)
        if r["ok"]:
            p = r["data"]["position"]
            self.pos = (p["x"], p["y"])

    def near(self, x: float, y: float, dist: float = REACH - 2) -> None:
        """Get within `dist` of (x, y), stopping short on the side we come from (never on the spot itself)."""
        d = math.hypot(x - self.pos[0], y - self.pos[1])
        if d > dist:
            stop = min(4.5, dist - 0.5)
            ux, uy = (self.pos[0] - x) / d, (self.pos[1] - y) / d
            self.go(x + ux * stop, y + uy * stop)

    def clear(self, tiles) -> None:
        """Pick up every tree or rock on these tiles (all of them: two trees can cover one tile)."""
        for pos in self.obstacles.on(tiles):
            self.near(*pos, dist=REACH - 1)
            r = self.run.pickup(*pos)
            self.obstacles.remove(pos)
            if not r["ok"]:
                self.log(f"could not clear {pos}: {r['code']}")

    def relook(self, tiles) -> None:
        """Look again at what stands on these tiles (a tree or rock the last look missed)."""
        xs, ys = [t[0] for t in tiles], [t[1] for t in tiles]
        self.near((min(xs) + max(xs)) / 2 + 0.5, (min(ys) + max(ys)) / 2 + 0.5)
        self.observe(int(max(max(xs) - min(xs), max(ys) - min(ys)) / 2) + 6)

    def retry_blocked(self, tiles, act):
        """act() is one game action (a place or place_line result). When the game refuses the tiles
        (cannot_place), the usual cause is an obstacle the map does not know about: look again, clear what is
        there and try once more. Returns the last result."""
        r = act()
        if r.get("ok") or r.get("code") != "cannot_place" or not tiles:
            return r
        try:
            self.relook(tiles)
        except BuildError:
            return r
        self.clear(tiles)
        return act()

    def step_out(self, tiles) -> None:
        """Walk off a footprint we are about to build on (buildings collide with the character)."""
        xs, ys = [t[0] for t in tiles], [t[1] for t in tiles]
        x0, x1, y0, y1 = min(xs) - 2, max(xs) + 2, min(ys) - 2, max(ys) + 2
        ring = [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)
                if x in (x0, x1) or y in (y0, y1)]
        ok = [r for r in ring if not self.blocked(r)] or ring
        self.go(*centre(min(ok, key=lambda r: math.dist(centre(r), self.pos))))

    def place(self, entity: str, x: float, y: float, direction: str = "north", tiles=(), **belt) -> dict:
        """belt: carries / isolated / allow for a belt (see Run.place); left out when empty."""
        self.clear(tiles)
        self.near(x, y)
        if tiles and any(abs(self.pos[0] - (tx + 0.5)) < 1.2 and abs(self.pos[1] - (ty + 0.5)) < 1.2
                         for tx, ty in tiles):
            self.step_out(tiles)
        r = self.run.place(entity, x, y, direction, **belt) if belt else self.run.place(entity, x, y, direction)
        if not r["ok"]:
            raise BuildError(f"place {entity} at ({x}, {y}) {direction}: {r['code']} {r.get('message', '')}")
        self.used.update(tiles)
        return r["data"]["entity"]

    def fuel(self, x: float, y: float, count: int) -> None:
        self.near(x, y)
        r = self.run.insert(x, y, "coal", count)
        if r["ok"]:                                    # keep the coal count right between observes
            self.inventory["coal"] = max(0, self.inventory.get("coal", 0) - count)
        else:
            self.log(f"fuel at ({x}, {y}) failed: {r['code']}")
            if r["code"] == "not_in_inventory":
                self.inventory["coal"] = 0


# ---------------------------------------------------------------- planning
def block_tiles(X: int, Y: int) -> set[Tile]:
    return {(x, y) for x in range(X, X + 5) for y in range(Y - 6, Y + 10)}


def find_block(world: World, toward: tuple[float, float]) -> tuple[int, int] | None:
    iron = world.resources.get("iron-ore", set())
    best = None
    for tx, ty in iron:
        X, Y = tx, ty - 6
        if not all((X + dx, Y + 6 + dy) in iron for dx in range(4) for dy in range(2)):
            continue
        foot = block_tiles(X, Y)
        if not world.free(foot):
            continue
        cost = math.dist((X + 4, Y - 3), toward) + 8 * sum(t in world.obstacles for t in foot)
        if best is None or cost < best[0]:
            best = (cost, X, Y)
    return (best[1], best[2]) if best else None


def pair_tiles(cx: int, cy: int) -> tuple[set[Tile], set[Tile]]:
    """Copper pair: drill tiles (on ore), and everything the pair occupies (drill, furnace, inserter, belt start,
    and the coal chest + burner inserter that keep the drill fuelled under it)."""
    drill = {(cx - 1, cy - 1), (cx, cy - 1), (cx - 1, cy), (cx, cy)}
    rest = {(cx - 1, cy - 3), (cx, cy - 3), (cx - 1, cy - 2), (cx, cy - 2), (cx - 1, cy - 4), (cx - 1, cy - 5),
            (cx - 1, cy + 1), (cx - 1, cy + 2)}
    return drill, drill | rest


def find_pair(world: World, goal: Tile) -> tuple[int, int] | None:
    copper = world.resources.get("copper-ore", set())
    best = None
    for tx, ty in copper:
        cx, cy = tx + 1, ty + 1
        drill, foot = pair_tiles(cx, cy)
        if not drill <= copper or not world.free(foot):
            continue
        cost = math.dist((cx - 1, cy - 5), goal) + 8 * sum(t in world.obstacles for t in foot)
        if best is None or cost < best[0]:
            best = (cost, cx, cy)
    return (best[1], best[2]) if best else None


def route(world: World, start: Tile, goal: Tile, avoid: set[Tile] = frozenset(), margin: int = 30) -> list[Tile] | None:
    """A* over tiles: water, cliffs and built tiles are walls; trees and rocks cost extra; turns cost a little."""
    x0, x1 = min(start[0], goal[0]) - margin, max(start[0], goal[0]) + margin
    y0, y1 = min(start[1], goal[1]) - margin, max(start[1], goal[1]) + margin

    def cost(t: Tile) -> float:
        return 1.0 + (6.0 if t in world.obstacles else 0.0) + (0.3 if t not in world.seen else 0.0)

    frontier = [(math.dist(start, goal), 0.0, start, None)]
    best: dict[tuple[Tile, str | None], float] = {(start, None): 0.0}
    came: dict[tuple[Tile, str | None], tuple[Tile, str | None] | None] = {(start, None): None}
    while frontier:
        _, g, t, d = heapq.heappop(frontier)
        if t == goal:
            path, key = [], (t, d)
            while key is not None:
                path.append(key[0])
                key = came[key]
            return path[::-1]
        if g > best.get((t, d), math.inf):
            continue
        for name, (dx, dy) in DIRS.items():
            n = (t[0] + dx, t[1] + dy)
            if not (x0 <= n[0] <= x1 and y0 <= n[1] <= y1):
                continue
            if n != goal and (world.blocked(n) or n in avoid):
                continue
            ng = g + cost(n) + (0.5 if d is not None and d != name else 0.0)
            if ng < best.get((n, name), math.inf):
                best[(n, name)] = ng
                came[(n, name)] = (t, d)
                heapq.heappush(frontier, (ng + math.dist(n, goal), ng, n, name))
    return None


def runs_of(path: list[Tile]) -> list[tuple[list[Tile], str]]:
    """Split a path into straight runs; each tile faces the next one (the last faces like its predecessor)."""
    dirs = []
    for a, b in zip(path, path[1:]):
        dirs.append(next(k for k, v in DIRS.items() if v == (b[0] - a[0], b[1] - a[1])))
    dirs.append(dirs[-1] if dirs else "north")
    out: list[tuple[list[Tile], str]] = []
    for t, d in zip(path, dirs):
        if out and out[-1][1] == d:
            out[-1][0].append(t)
        else:
            out.append(([t], d))
    return out


LINE_REACH = 9.0          # the scenario's build reach is 10 tiles to the building's box; keep a tile spare


def stand_beside(world: World, chunk: list[Tile], d: str) -> Tile | None:
    """A tile to stand on while placing a straight run: never on the run (the game refuses a pipe or belt under
    the character), free, and within reach of all of it. The spots 3-4 tiles beside its middle first, then the
    nearest free tile within reach (run 20261119-011655: with those four spots taken by storage chests and a
    boiler, the character stood on the line itself, and the same pipe failed 41 times)."""
    mid, on = chunk[len(chunk) // 2], set(chunk)
    px, py = -DIRS[d][1], DIRS[d][0]
    beside = [(mid[0] + px * k, mid[1] + py * k) for k in (3, -3, 4, -4, 2, -2, 5, -5)]
    around = sorted(((mid[0] + dx, mid[1] + dy) for dx in range(-8, 9) for dy in range(-8, 9)),
                    key=lambda t: math.dist(centre(t), world.pos))
    for t in beside + around:
        if t in on or world.blocked(t):
            continue
        if max(math.dist(centre(t), centre(c)) for c in chunk) <= LINE_REACH:
            return t
    return None


def lay(world: World, entity: str, path: list[Tile]) -> None:
    """Lay belts (facing along the path) or pipes, in chunks the character can reach."""
    for tiles, d in runs_of(path):
        for i in range(0, len(tiles), 14):
            chunk = tiles[i:i + 14]
            world.clear(chunk)
            spot = stand_beside(world, chunk, d)
            if spot is None:
                raise BuildError(f"{entity} line {chunk[0]}->{chunk[-1]}: no free tile to stand on beside it "
                                 "(a line is never laid under the character)")
            here = (math.floor(world.pos[0]), math.floor(world.pos[1]))
            if here in chunk or math.dist(centre(spot), world.pos) > 1.5:
                world.go(*centre(spot))
            if len(chunk) == 1:
                r = world.run.place(entity, *centre(chunk[0]), d)
            else:
                r = world.run.place_line(entity, *centre(chunk[0]), *centre(chunk[-1]))
            if not r["ok"]:
                msg = r.get("message", "") or ""
                m = re.search(r"tile (\d+) \(", msg)
                if m and r["code"] == "cannot_place" and int(m.group(1)) < len(chunk):
                    bad = chunk[int(m.group(1))]
                    here = (math.floor(world.pos[0]), math.floor(world.pos[1]))
                    if bad != here:            # something the map does not show stands there: route around it
                        world.used.add(bad)
                        msg += f" (tile {bad} remembered as blocked; the next layout avoids it)"
                raise BuildError(f"{entity} line {chunk[0]}->{chunk[-1]}: {r['code']} {msg}")
            world.used.update(chunk)


def survey(world: World, a: tuple[float, float], b: tuple[float, float], step: float = 28) -> None:
    """Walk from a to b observing every `step` tiles, so routing knows about water and trees on the way."""
    n = max(1, math.ceil(math.dist(a, b) / step))
    for k in range(1, n + 1):
        p = (a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n)
        around = [(math.floor(p[0]) + ox, math.floor(p[1]) + oy) for ox in (-8, 0, 8) for oy in (-8, 0, 8)]
        if not all(t in world.seen for t in around):
            world.go(*p)
            world.observe(20)


# -------------------------------------------------------------- building
def feed_from_chest(world: World, tx: int, ty: int, coal: int = 30) -> None:
    """Keep a burner drill fuelled for the whole run: a burner inserter on tile (tx, ty) under the drill takes
    coal from a chest one tile further south (it fuels itself from the coal it carries)."""
    world.place("burner-inserter", *centre((tx, ty)), "south", {(tx, ty)})
    world.place("iron-chest", *centre((tx, ty + 1)), "north", {(tx, ty + 1)})
    world.fuel(*centre((tx, ty)), 2)
    world.fuel(*centre((tx, ty + 1)), coal)   # + the drill's own 50: 80 coal, a burner drill burns ~68 in 30 min


# -------------------------------------------------------------- building
def build_block(world: World, X: int, Y: int) -> dict:
    world.go(X + 6.5, Y + 0.5)
    world.clear(block_tiles(X, Y))
    P = {}
    P["drills"] = [(X + 1, Y + 7), (X + 3, Y + 7)]
    P["furnaces"] = [(X + 1, Y + 5), (X + 3, Y + 5)]
    for (dx, dy), (fx, fy) in zip(P["drills"], P["furnaces"]):
        world.place("burner-mining-drill", dx, dy, "north", {(dx - 1, dy - 1), (dx, dy - 1), (dx - 1, dy), (dx, dy)})
        world.place("stone-furnace", fx, fy, "north", {(fx - 1, fy - 1), (fx, fy - 1), (fx - 1, fy), (fx, fy)})
    P["gear"] = (X + 1.5, Y + 1.5)
    P["red"] = (X + 1.5, Y - 2.5)
    world.place("assembling-machine-1", *P["gear"], "north", {(x, y) for x in range(X, X + 3) for y in range(Y, Y + 3)})
    world.place("assembling-machine-1", *P["red"], "north", {(x, y) for x in range(X, X + 3) for y in range(Y - 4, Y - 1)})
    for xy, recipe in ((P["gear"], "iron-gear-wheel"), (P["red"], "automation-science-pack")):
        r = world.run.set_recipe(*xy, recipe)
        if not r["ok"]:
            raise BuildError(f"set_recipe {recipe}: {r['code']}")
    # inserters face the side they pick up from
    for t, d in (((X + 1, Y + 3), "south"), ((X + 2, Y + 3), "south"), ((X + 1, Y - 1), "south"),
                 ((X + 3, Y - 3), "east"), ((X + 1, Y - 5), "south")):
        world.place("inserter", *centre(t), d, {t})
    world.place("iron-chest", *centre((X + 1, Y - 6)), "north", {(X + 1, Y - 6)})
    P["poles"] = [(X + 3, Y + 3), (X + 3, Y - 1), (X + 2, Y - 5)]
    for t in P["poles"]:
        world.place("small-electric-pole", *centre(t), "north", {t})
    for xy in P["drills"] + P["furnaces"]:
        world.fuel(*xy, 50)
    for dx, dy in P["drills"]:              # 50 coal runs a burner drill ~22 min; the window ends at minute 30
        feed_from_chest(world, dx - 1, dy + 1)
    P["belt_goal"] = (X + 4, Y - 3)
    world.used |= block_tiles(X, Y) | {(x, y) for x in range(X - 1, X + 6) for y in (Y - 7, Y + 10)}
    world.used.discard(P["belt_goal"])
    world.keepout |= {(X + 5 + k, Y - 3 + o) for k in range(3) for o in (-1, 0, 1)}   # the belt's way in
    return P


def build_copper(world: World, goal: Tile) -> Tile:
    near = world.summary.get("copper-ore", {}).get("nearest")
    if not near:
        raise BuildError("no copper within 64 tiles of the block")
    survey(world, world.pos, (near["x"], near["y"]))
    site = find_pair(world, goal)
    if site is None:
        world.observe(20)
        site = find_pair(world, goal)
    if site is None:
        raise BuildError("no room for a copper drill and furnace")
    cx, cy = site
    drill, foot = pair_tiles(cx, cy)
    world.go(cx + 3.5, cy - 1.5)
    world.place("burner-mining-drill", cx, cy, "north", drill)
    world.place("stone-furnace", cx, cy - 2, "north", {(cx - 1, cy - 3), (cx, cy - 3), (cx - 1, cy - 2), (cx, cy - 2)})
    world.place("burner-inserter", *centre((cx - 1, cy - 4)), "south", {(cx - 1, cy - 4)})
    world.fuel(cx, cy, 50)
    world.fuel(cx, cy - 2, 50)
    world.fuel(*centre((cx - 1, cy - 4)), 10)
    feed_from_chest(world, cx - 1, cy + 1)
    return (cx - 1, cy - 5)


def build_belt(world: World, start: Tile, goal: Tile) -> None:
    survey(world, world.pos, centre(goal))
    path = route(world, start, goal)
    if path is None:
        raise BuildError("no belt route from copper to the block")
    world.log(f"copper belt: {len(path)} tiles")
    world.go(*centre(start))
    lay(world, "transport-belt", path)


def shore_spots(world: World) -> list[tuple[Tile, str]]:
    out = []
    for w in world.water:
        for d, (dx, dy) in DIRS.items():
            land = (w[0] - dx, w[1] - dy)                 # pump faces the water
            if land not in world.water and not world.blocked(land) and land in world.seen:
                out.append((land, d))
    return out


def power_tiles(i: int, j: int, engines: int = 1) -> set[Tile]:
    """Boiler (facing north, tiles i-1..i+1, j-1..j) and the steam engines stacked above it (5 tiles each)."""
    return {(x, y) for x in range(i - 1, i + 2) for y in range(j - 1 - 5 * engines, j + 1)}


def build_power(world: World, toward: Tile, engines: int = 1, coal: int = 250) -> Tile:
    """engines: 1 (900 kW) or 2 (1.8 MW, all one boiler can feed). coal: what the boiler's chest starts with."""
    if not world.water_hints:
        raise BuildError("no water seen within 64 tiles of anywhere the bot looked")
    water = min(world.water_hints, key=lambda w: math.dist(w, toward))
    survey(world, world.pos, water)
    world.observe(20)
    spots = sorted(shore_spots(world), key=lambda s: math.dist(s[0], toward))
    tries = 0
    for (land, d) in spots[:80]:
        dx, dy = DIRS[d]
        out = (land[0] - dx, land[1] - dy)               # the pump outputs on its back side
        if world.blocked(out) or out in world.water:
            continue
        site = None
        for r in range(2, 12):
            cands = [(out[0] + ox, out[1] + oy) for ox in range(-r, r + 1) for oy in range(-r, r + 1)
                     if max(abs(ox), abs(oy)) == r]
            for i, j in sorted(cands, key=lambda c: math.dist(c, toward)):
                foot = power_tiles(i, j, engines)
                extra = {(i + 2, j - 1), (i + 3, j - 1), (i + 2, j - 4)}   # inserter, chest, pole
                inlet = (i - 2, j)
                if land in foot | extra or out in foot | extra or inlet in foot:
                    continue
                if not world.free(foot | extra | {inlet}):
                    continue
                avoid = foot | extra | {land, (i + 2, j), (i, j - 2 - 5 * engines)}   # keep pipes off fluid ports
                path = route(world, out, inlet, avoid=avoid, margin=6)
                if path is not None and all(t not in world.water for t in path):
                    site = (i, j, path)
                    break
            if site:
                break
        if not site:
            continue
        i, j, path = site
        world.near(*centre(land))
        r = world.run.place("offshore-pump", *centre(land), d)
        if not r["ok"]:                      # not every shore tile takes a pump; try the next one
            tries += 1
            if tries >= 12:
                break
            continue
        world.used.add(land)
        world.log(f"pump at {land} facing {d}, boiler at ({i + 0.5}, {j}), {len(path)} pipes")
        lay(world, "pipe", path)
        world.place("boiler", i + 0.5, j, "north", {(x, y) for x in range(i - 1, i + 2) for y in (j - 1, j)})
        for k in range(engines):            # engines chain end to end, steam flowing north
            world.place("steam-engine", i + 0.5, j - 3.5 - 5 * k, "north",
                        {(x, y) for x in range(i - 1, i + 2) for y in range(j - 6 - 5 * k, j - 1 - 5 * k)})
        world.place("iron-chest", *centre((i + 3, j - 1)), "north", {(i + 3, j - 1)})
        world.place("burner-inserter", *centre((i + 2, j - 1)), "east", {(i + 2, j - 1)})
        pole = (i + 2, j - 4)
        world.place("small-electric-pole", *centre(pole), "north", {pole})
        world.fuel(i + 0.5, j, 50)
        world.fuel(*centre((i + 2, j - 1)), 5)
        world.fuel(*centre((i + 3, j - 1)), coal)
        return pole
    raise BuildError("no place for the pump, boiler and steam engine")


def build_poles(world: World, start: Tile, goal: Tile) -> None:
    """Small poles every <= 7 tiles from start to goal, sidestepping water and buildings."""
    cur = start
    while math.dist(cur, goal) > 7:
        d = math.dist(cur, goal)
        ideal = (cur[0] + (goal[0] - cur[0]) * 6.5 / d, cur[1] + (goal[1] - cur[1]) * 6.5 / d)
        cands = sorted({(math.floor(ideal[0]) + ox, math.floor(ideal[1]) + oy)
                        for ox in range(-3, 4) for oy in range(-3, 4)},
                       key=lambda t: (math.dist(t, goal), math.dist(t, ideal)))
        nxt = next((t for t in cands if math.dist(t, cur) <= 7.2 and math.dist(t, goal) < d - 2
                    and not world.blocked(t)), None)
        if nxt is None:
            raise BuildError(f"no pole spot after {cur}")
        world.near(*centre(nxt), dist=6)
        if nxt not in world.seen:
            world.observe(20)
            if world.blocked(nxt):
                continue
        world.place("small-electric-pole", *centre(nxt), "north", {nxt})
        cur = nxt


# ------------------------------------------------------------------ play
def play(run: Run, log=print, warmup_ticks: int = 3 * 3600) -> dict:
    world = World(run, log)
    try:
        world.observe(20)
        iron = world.summary.get("iron-ore", {}).get("nearest")
        copper = world.summary.get("copper-ore", {}).get("nearest")
        if not iron or not copper:
            raise BuildError("need iron and copper within 64 tiles")
        world.go(iron["x"], iron["y"])
        world.observe(20)
        toward = (copper["x"], copper["y"])
        site = find_block(world, toward)
        if site is None:                                  # look deeper into the patch
            cx = sum(t[0] for t in world.resources["iron-ore"]) / len(world.resources["iron-ore"])
            cy = sum(t[1] for t in world.resources["iron-ore"]) / len(world.resources["iron-ore"])
            world.go(cx, cy)
            world.observe(20)
            site = find_block(world, toward)
        if site is None:
            raise BuildError("no room for the iron block")
        X, Y = site
        log(f"iron block at ({X}, {Y})")
        P = build_block(world, X, Y)
        world.observe(20)                                  # nearest copper and water from the block
        copper_belt_start = build_copper(world, P["belt_goal"])
        build_belt(world, copper_belt_start, P["belt_goal"])
        world.go(*centre(P["belt_goal"]))
        world.observe(20)
        pole = build_power(world, P["belt_goal"])
        build_poles(world, pole, P["poles"][1])
        # let it warm up, then check on it
        if warmup_ticks:
            run.advance(min(warmup_ticks, run.budgets["ticks_left"]))
        world.go(X + 6.5, Y + 0.5)
        d = world.observe(12)
        for e in d["entities"]:
            if e["name"] in ("assembling-machine-1", "inserter", "stone-furnace"):
                log(f"  {e['name']} ({e['x']}, {e['y']}) {e.get('recipe', '')} {e['status']} "
                    f"{e.get('inventories', {})}")
    except BuildError as exc:
        log(f"build stopped: {exc}")
    return run.end_and_wait()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("ARENA_URL", "http://127.0.0.1:8710"))
    ap.add_argument("--token", default=os.environ.get("ARENA_TOKEN", ""))
    ap.add_argument("--mode", default="practice")
    args = ap.parse_args()
    client = ArenaClient(args.url, args.token)
    run = client.start_run("automation", args.mode)
    print("run", run.id, run.state.get("map"))
    print(play(run))


if __name__ == "__main__":
    main()
