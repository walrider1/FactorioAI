"""Build a design in the game through the scripted World (walking, clearing trees, reach)."""

from __future__ import annotations

import math

import automation_bot as ab

from .cells import DIRS
from .design import Design

Tile = tuple[int, int]


def _centre(t: Tile) -> tuple[float, float]:
    return t[0] + 0.5, t[1] + 0.5


def _tour(items: list, pos_of, start: tuple[float, float]) -> list:
    """Nearest-neighbour order, so the character walks as little as possible."""
    left = list(items)
    out = []
    here = start
    while left:
        nxt = min(left, key=lambda it: math.dist(pos_of(it), here))
        left.remove(nxt)
        out.append(nxt)
        here = pos_of(nxt)
    return out


def _runs(route) -> list[tuple[str, list[Tile], str]]:
    """Belt work in flow order: ('line', tiles, facing) for plain belts, ('ug', [tile], facing/type) for undergrounds."""
    out: list = []
    for t in route.path:
        kind, face = route.kinds[t], route.facing[t]
        if kind == "belt":
            if out and out[-1][0] == "line" and out[-1][2] == face and len(out[-1][1]) < 10:
                last = out[-1][1][-1]
                if (last[0] + DIRS[face][0], last[1] + DIRS[face][1]) == t:
                    out[-1][1].append(t)
                    continue
            out.append(("line", [t], face))
        else:
            out.append(("ug", [t], face + ("/input" if kind == "ug_in" else "/output")))
    return out


def build(world: "ab.World", design: Design, log=print, anchors: list[Tile] = ()) -> dict:
    """Place the whole design. Never stops at the first failure: each failure is recorded and the rest goes on."""
    failed: list[str] = []
    placed = 0

    def attempt(what: str, fn) -> bool:
        nonlocal placed
        try:
            fn()
            placed += 1
            return True
        except ab.BuildError as exc:
            failed.append(f"{what}: {str(exc)[:140]}")
            log(f"[build] {what} failed: {str(exc)[:140]}")
            return False

    def run_ok(r: dict, what: str) -> None:
        if not r.get("ok"):
            raise ab.BuildError(f"{what}: {r.get('code')} {r.get('message', '')}")

    # 1. cells: drills, furnaces (fuelled), assemblers (recipe set)
    for c in _tour(design.cells, lambda c: c.centre(), world.pos):
        for e in c.entities():
            ok = attempt(f"{e.name} of {c.id}", lambda e=e: world.place(e.name, e.x, e.y, e.direction, set(e.tiles)))
            if ok and e.name == "stone-furnace":
                world.fuel(e.x, e.y, 50)                # 50 coal runs a stone furnace ~37 minutes
            if ok and e.name == "assembling-machine-1":
                attempt(f"recipe of {c.id}",
                        lambda e=e, c=c: run_ok(world.run.set_recipe(e.x, e.y, c.recipe), "set_recipe"))
    # 2. inserters and output chests
    feeds = design.feed_ids()                  # a feed's pin tile is where the link's belt ends: no inserter
    ins = [(p.site.inserter, p.site.inserter_dir(p.into_cell), r.item, p.cell) for r in design.routes for p in r.pins
           if p.cell not in feeds]
    for t, face, item, cid in _tour(ins, lambda it: _centre(it[0]), world.pos):
        attempt(f"inserter {item} at {cid}", lambda t=t, face=face: world.place("inserter", *_centre(t), face, {t}))
    chests = [t for r in design.routes for t in r.chests]
    for t in _tour(chests, _centre, world.pos):
        attempt(f"chest {t}", lambda t=t: world.place("iron-chest", *_centre(t), "north", {t}))
    # 3. belts, net by net, runs of up to 10 tiles laid with one place_line (all of it within reach)
    for r in _tour(design.routes, lambda r: _centre(r.path[0]) if r.path else world.pos, world.pos):
        for kind, tiles, face in _runs(r):
            if kind == "line":
                def lay(tiles=tiles, face=face):
                    world.clear(tiles)
                    mid = tiles[len(tiles) // 2]
                    world.near(*_centre(mid), dist=3)
                    if len(tiles) == 1:
                        res = world.run.place("transport-belt", *_centre(tiles[0]), face)
                    else:
                        res = world.run.place_line("transport-belt", *_centre(tiles[0]), *_centre(tiles[-1]))
                    run_ok(res, f"belt {tiles[0]}->{tiles[-1]}")
                    world.used.update(tiles)
                attempt(f"{r.item} belt {tiles[0]}", lay)
            else:
                d, bt = face.split("/")

                def ug(t=tiles[0], d=d, bt=bt):
                    world.clear([t])
                    world.near(*_centre(t))
                    if abs(world.pos[0] - t[0] - 0.5) < 1.2 and abs(world.pos[1] - t[1] - 0.5) < 1.2:
                        world.step_out({t})
                    run_ok(world.run.place("underground-belt", *_centre(t), d, bt), f"underground {t}")
                    world.used.add(t)
                attempt(f"{r.item} underground {tiles[0]}", ug)
    # 4. poles, then a line to the nearest powered pole if the design is not already in reach of one
    for t in _tour(design.poles, _centre, world.pos):
        attempt(f"pole {t}", lambda t=t: world.place("small-electric-pole", *_centre(t), "north", {t}))
    if anchors and design.poles:
        a, p = min(((a, p) for a in anchors for p in design.poles), key=lambda ap: math.dist(*ap))
        if math.dist(a, p) > 7.5:
            attempt(f"pole line to {a}", lambda: ab.build_poles(world, p, a))
    return {"placed": placed, "failed": failed}
