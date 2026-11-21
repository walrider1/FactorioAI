"""Power routing: connect a site to the network with as few new poles as possible, reusing every pole that
already stands (ours or anyone's) instead of laying a second line next to the first.

The same idea as the belt router: a shortest-path search over the map. Nodes are pole positions; two poles
connect when they are within wire reach. Stepping onto a pole that already exists costs nothing, stepping onto
a free tile costs one new pole. The search starts from every pole of the site and ends at any pole known to be
powered, so it finds the cheapest bridge, which is often zero or one pole.
"""

from __future__ import annotations

import heapq
import math

Tile = tuple[int, int]
REACH = 7.0          # small pole wire reach is 7.5 tiles; keep a margin for tile-centre rounding
MEDIUM_REACH = 8.5   # medium pole: 9 tiles


def route_poles(starts: list[Tile], anchors: list[Tile], existing: set[Tile], free, margin: int = 12,
                reach: float = REACH, max_nodes: int = 60000, old_reach: float | None = None) -> list[Tile] | None:
    """New pole tiles (in order from the site) that join `starts` to any of `anchors`; [] if already joined.

    existing: tiles with a pole on them already (free hops). free(t): may a new pole go on tile t.
    reach: wire reach between two new poles; old_reach: between a new pole and one that stands already (a wire
    spans the shorter reach of its two poles: medium poles 9 tiles apart, but 7.5 to a small one).
    Returns None if there is no way within the search box."""
    starts, anchors = [tuple(s) for s in starts], {tuple(a) for a in anchors}
    if not starts or not anchors:
        return None
    if anchors & set(starts):
        return []
    pts = starts + list(anchors)
    x0, x1 = min(p[0] for p in pts) - margin, max(p[0] for p in pts) + margin
    y0, y1 = min(p[1] for p in pts) - margin, max(p[1] for p in pts) + margin
    existing = {tuple(e) for e in existing if x0 <= e[0] <= x1 and y0 <= e[1] <= y1} | anchors
    r = int(math.floor(reach))
    offsets = [(dx, dy) for dx in range(-r, r + 1) for dy in range(-r, r + 1)
               if 0 < dx * dx + dy * dy <= reach * reach]
    old_r2 = (old_reach if old_reach is not None else reach) ** 2
    old = set(starts) | existing

    def h(t: Tile) -> float:
        # Existing poles are free hops, so no positive estimate is safe: this is Dijkstra (A* with h = 0),
        # kept inside a box around the site and the network so it stays fast.
        return 0.0

    best: dict[Tile, float] = {}
    prev: dict[Tile, Tile | None] = {}
    heap: list[tuple[float, float, Tile]] = []
    for s in starts:
        best[s], prev[s] = 0.0, None
        heapq.heappush(heap, (h(s), 0.0, s))
    seen = 0
    while heap:
        f, g, t = heapq.heappop(heap)
        if g > best.get(t, math.inf):
            continue
        if t in anchors:
            path = []
            while t is not None and t not in starts:
                if t not in existing:
                    path.append(t)
                t = prev[t]
            return path[::-1]
        seen += 1
        if seen > max_nodes:
            return None
        for dx, dy in offsets:
            n = (t[0] + dx, t[1] + dy)
            if not (x0 <= n[0] <= x1 and y0 <= n[1] <= y1):
                continue
            if dx * dx + dy * dy > old_r2 and (t in old or n in old):
                continue                              # a standing (small) pole: its shorter reach
            if n in existing:
                cost = 0.0
            elif free(n):
                cost = 1.0 + 0.001 * (dx * dx + dy * dy) ** 0.5    # prefer fewer poles, then shorter wires
            else:
                continue
            ng = g + cost
            if ng < best.get(n, math.inf):
                best[n], prev[n] = ng, t
                heapq.heappush(heap, (ng + h(n), ng, n))
    return None
