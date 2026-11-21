"""Power: cover every electric building with small poles (greedy set cover), then join them into one network.

A small electric pole powers a 5x5 area centred on its tile and links to other poles up to 7.5 tiles away.
A building is powered if any of its tiles is inside some pole's area.
"""

from __future__ import annotations

import math

from .grid import Grid

Tile = tuple[int, int]
SUPPLY = 2                 # tiles each side of the pole: the 5x5 area
WIRE = 7.5


def _dist(a: Tile, b: Tile) -> float:
    return math.dist(a, b)


def plan_poles(grid: Grid, consumers: list[set[Tile]], occupied: set[Tile], box: tuple[int, int, int, int],
               anchors: list[Tile] = ()) -> tuple[list[Tile], list[str]]:
    """consumers: the tiles of each electric building. anchors: existing powered poles to join, if near."""
    x0, y0, x1, y1 = box

    def ok(t: Tile) -> bool:
        return x0 <= t[0] <= x1 and y0 <= t[1] <= y1 and grid.free(t) and t not in occupied

    covers: dict[Tile, set[int]] = {}
    for i, tiles in enumerate(consumers):
        for (tx, ty) in tiles:
            for dx in range(-SUPPLY, SUPPLY + 1):
                for dy in range(-SUPPLY, SUPPLY + 1):
                    p = (tx + dx, ty + dy)
                    if ok(p):
                        covers.setdefault(p, set()).add(i)
    problems = [f"building {i} has no free tile for a pole near it" for i in range(len(consumers))
                if not any(i in s for s in covers.values())]
    left = set(range(len(consumers))) - {int(p.split()[1]) for p in problems}
    poles: list[Tile] = []
    while left:
        def score(p: Tile) -> float:
            gain = len(covers[p] & left)
            near = any(_dist(p, q) <= WIRE for q in poles) or not poles
            return gain + (0.5 if near else 0.0) - (0.3 if p in grid.obstacles else 0.0)
        p = max(covers, key=score)
        if not covers[p] & left:
            break
        poles.append(p)
        left -= covers[p]
        del covers[p]

    # join everything (and the anchors) into one network with relay poles
    taken = set(occupied) | set(poles)
    nodes = list(poles) + [a for a in anchors]
    while True:
        comp = _components(nodes)
        if len(set(comp.values())) <= 1:
            break
        main = comp[nodes[0]]
        a_side = [n for n in nodes if comp[n] == main]
        b_side = [n for n in nodes if comp[n] != main]
        a, b = min(((p, q) for p in a_side for q in b_side), key=lambda pq: _dist(*pq))
        cur = a
        stuck = False
        while _dist(cur, b) > WIRE:
            d = _dist(cur, b)
            ideal = (cur[0] + (b[0] - cur[0]) * 6.5 / d, cur[1] + (b[1] - cur[1]) * 6.5 / d)
            cands = [(round(ideal[0]) + ox, round(ideal[1]) + oy) for ox in range(-2, 3) for oy in range(-2, 3)]
            cands = [c for c in cands if ok(c) and c not in taken and _dist(c, cur) <= WIRE and _dist(c, b) < d - 1]
            if not cands:
                stuck = True
                break
            nxt = min(cands, key=lambda c: (_dist(c, b), _dist(c, ideal)))
            poles.append(nxt)
            nodes.append(nxt)
            taken.add(nxt)
            cur = nxt
        if stuck:
            if b in anchors:              # can't reach an existing pole from here: the builder runs a line later
                nodes = [n for n in nodes if n != b]
                anchors = [x for x in anchors if x != b]
                continue
            problems.append(f"no relay spot between poles {cur} and {b}")
            break
    return poles, problems


def _components(nodes: list[Tile]) -> dict[Tile, int]:
    parent = {n: n for n in nodes}

    def find(n):
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n
    for i, a in enumerate(nodes):
        for b in nodes[i + 1:]:
            if _dist(a, b) <= WIRE:
                parent[find(a)] = find(b)
    return {n: find(n) for n in nodes}
