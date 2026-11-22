"""Belt links between factories: an A* over (tile, heading) that can go under things with underground belts.

A link carries one item from a source chest (an inserter lifts it onto the first belt tile) to a port: the belt's
last tile sits in front of the port's inserter and is reached head-on from the approach tile, so the belt never
points into a neighbouring port's belt. Other factories' belts, pipes and buildings are crossed underground
(underground belts reach 5 tiles in 2.1: up to 4 tiles in between).

    steps = route_belt(free, start, approach, end, avoid=..., underground=True)
    lay_belt(world, steps)

steps: [("belt", tile, facing), ("ug", tile, facing, "input" | "output"), ...] in the order items travel.
"""

from __future__ import annotations

import heapq
import itertools
import math

Tile = tuple[int, int]
VEC = {"north": (0, -1), "east": (1, 0), "south": (0, 1), "west": (-1, 0)}
LEFT = {"north": "west", "west": "south", "south": "east", "east": "north"}
RIGHT = {v: k for k, v in LEFT.items()}
UG_REACH = 5                        # underground-belt max_underground_distance (catalog)
AXIS = {"north": "ns", "south": "ns", "east": "ew", "west": "ew"}
# A link (inserter, belt, port) is a repair-sized job: its source within LINK_REACH tiles (Manhattan) of the port
# and its route at most LINK_ROUTE_MAX belt tiles. Farther is a trunk, a goal of its own. Run 20261121-140755: a
# copper link inside a pump build walked ~460 tiles each way three times for a belt from x=391 to x=-88.
LINK_REACH = 150
LINK_ROUTE_MAX = 200


def route_len(steps: list[tuple]) -> int:
    """Tiles a route spans, the ground an underground pair goes under included."""
    return sum(abs(a[1][0] - b[1][0]) + abs(a[1][1] - b[1][1]) for a, b in zip(steps, steps[1:])) + (1 if steps else 0)


def ug_cover(ugs) -> dict[str, set[Tile]]:
    """Tiles a NEW underground pair on the same axis must not span or touch: every existing underground belt
    and the tiles between neighbours on one line within reach (a pair). The game pairs an underground with the
    nearest one on its line, so a new entrance inside another pair's span takes that pair over and flips one of
    its ends (run 20261119-202811: a coal link's entrance placed inside an iron link's pair came out as an exit
    facing the other way; both links stopped). Crossing at right angles is fine. ugs: [(tile, facing)]."""
    lines: dict[tuple, set[int]] = {}
    for t, d in ugs:
        ax = AXIS.get(d)
        if ax is None:
            continue
        lines.setdefault((ax, t[0] if ax == "ns" else t[1]), set()).add(t[1] if ax == "ns" else t[0])
    out: dict[str, set[Tile]] = {"ns": set(), "ew": set()}
    for (ax, line), ps in lines.items():
        ps_sorted = sorted(ps)
        cov = set(ps_sorted)
        for a, b in zip(ps_sorted, ps_sorted[1:]):
            if b - a <= UG_REACH:
                cov.update(range(a, b + 1))
        out[ax] |= {(line, q) if ax == "ns" else (q, line) for q in cov}
    return out


def _add(t: Tile, d: str, k: int = 1) -> Tile:
    return t[0] + VEC[d][0] * k, t[1] + VEC[d][1] * k


def route_belt(free, start, approach: Tile, end: Tile, avoid: set[Tile] = frozenset(),
               underground: bool = True, margin: int = 30, limit: int = 300_000,
               soft: dict | None = None, ug_block: dict | None = None) -> list[tuple] | None:
    """Belts from `start` to `approach`, arriving headed for `end`, then the end tile. free(t): may a belt go on t.
    None when no route exists inside the search box.

    start: one tile, or {tile: cost} for several possible starts (the route begins at whichever is cheapest
    overall; its first step's tile says which). soft: {tile: extra cost} for tiles that are taken but may be
    cleared for this belt (another link's belts the caller is willing to rip up and lay again elsewhere).
    ug_block: ug_cover() of the undergrounds already standing: no new pair spans or touches one on its axis."""
    goal_dir = next((d for d, v in VEC.items() if v == (end[0] - approach[0], end[1] - approach[1])), None)
    soft = soft or {}
    starts = {start: 0.0} if isinstance(start, tuple) else dict(start)
    ok = lambda t: t not in avoid and (free(t) or t in soft)
    starts = {s: c for s, c in starts.items() if ok(s)}
    if goal_dir is None or not starts:
        return None
    xs, ys = [s[0] for s in starts] + [approach[0]], [s[1] for s in starts] + [approach[1]]
    x0, x1 = min(xs) - margin, max(xs) + margin
    y0, y1 = min(ys) - margin, max(ys) + margin
    inside = lambda t: x0 <= t[0] <= x1 and y0 <= t[1] <= y1
    extra = lambda t: soft.get(t, 0.0)

    def h(t: Tile) -> float:
        return abs(t[0] - approach[0]) + abs(t[1] - approach[1])

    tie = itertools.count()
    heap: list = []
    best: dict[tuple, float] = {}
    parent: dict[tuple, tuple] = {}
    for s, c0 in starts.items():
        for d in VEC:                                   # the first belt may face any way
            st = (s, d)
            g0 = c0 + extra(s)
            if g0 >= best.get(st, 1e18):
                continue
            best[st] = g0
            parent[st] = (None, [("belt", s, d)])
            heapq.heappush(heap, (g0 + h(s), next(tie), g0, st))
    n = 0
    while heap:
        _, _, g, st = heapq.heappop(heap)
        if g > best.get(st, 1e18):
            continue
        t, d = st
        if t == approach and d == goal_dir:
            return _unwind(parent, st) + [("belt", end, goal_dir)]
        n += 1
        if n > limit:
            return None
        nxt = _add(t, d)
        moves = []
        if inside(nxt) and ok(nxt):
            for nd, turn in ((d, 0.0), (LEFT[d], 0.3), (RIGHT[d], 0.3)):
                moves.append(((nxt, nd), 1.0 + turn + extra(nxt), [("belt", nxt, nd)]))
        if underground and inside(nxt) and ok(nxt):          # go under whatever is ahead
            cov = (ug_block or {}).get(AXIS[d]) or ()
            for k in range(2, UG_REACH + 1):
                ex = _add(nxt, d, k)
                if not inside(ex):
                    break
                if cov and any(_add(nxt, d, i) in cov for i in range(k + 1)):
                    break                                    # it would pair with another link's underground
                if ok(ex):
                    moves.append(((ex, d), 4.0 + 0.2 * k + extra(nxt) + extra(ex),
                                  [("ug", nxt, d, "input"), ("ug", ex, d, "output")]))
        for ns, cost, steps in moves:
            ng = g + cost
            if ng < best.get(ns, 1e18):
                best[ns] = ng
                parent[ns] = (st, steps)
                heapq.heappush(heap, (ng + h(ns[0]), next(tie), ng, ns))
    return None


def _unwind(parent: dict, st: tuple) -> list[tuple]:
    out: list[tuple] = []
    while st is not None:
        prev, steps = parent[st]
        out = steps + out
        st = prev
    return out


def tiles_of(steps: list[tuple]) -> list[Tile]:
    return [s[1] for s in steps]


def link_joins(steps: list[tuple], from_belt: bool, tail: Tile | None = None) -> list[Tile]:
    """Where a link may join other belts: the tile ahead of its last belt (the port's belt it feeds, the chain it
    side-loads, the strip it continues), and, when it runs on from a line's tail (no inserter lifts its items),
    that tail. Any other join is an accident (world.belt_job refuses it).

    `tail`: the belt the items come from. The tile behind the first belt is that tail only when the link runs
    straight on; a link that turns on its first tile is fed from the side. Run 20261122-133537: every link that
    turned there was refused (would_merge with the line's own tail), its start tile was banned for good, and 7 of
    11 plate lines could never be belted anywhere while their furnaces stood full beside starving factories."""
    if not steps:
        return []
    (lx, ly), (dx, dy) = steps[-1][1], VEC[steps[-1][2]]
    out = [(lx + dx, ly + dy)]
    if from_belt:
        if tail is not None:
            out.append((int(tail[0]), int(tail[1])))
        else:
            (fx, fy), (dx, dy) = steps[0][1], VEC[steps[0][2]]
            out.append((fx - dx, fy - dy))
    return out


def strip_ends(tiles) -> list[Tile]:
    """The tile in front of each fuel strip's dead end: strips face east, one row each."""
    rows: dict[int, int] = {}
    for x, y in tiles:
        rows[y] = max(rows.get(y, x), x)
    return [(x + 1, y) for y, x in rows.items()]


def strip_blocked(world, tiles) -> Tile | None:
    """A belt in front of a fuel strip's dead end: the strip's spare coal would run onto it (None: clear)."""
    at = getattr(world, "belt_at", None)
    return next((t for t in strip_ends(tiles) if at is not None and at(t)), None)


def job_for(world, items, route=(), joins=()):
    """world.belt_job when the world has one (a stub world in a test has not): nothing changes otherwise."""
    from contextlib import nullcontext
    job = getattr(world, "belt_job", None)
    return job([i for i in items if i], list(route), list(joins)) if job is not None else nullcontext()


def count(steps: list[tuple]) -> dict[str, int]:
    return {"transport-belt": sum(1 for s in steps if s[0] == "belt"),
            "underground-belt": sum(1 for s in steps if s[0] == "ug") // 2 * 2}


def lay_belt(world, steps: list[tuple]) -> None:
    """Place the link in travel order: plain belts facing along it, undergrounds as entrance/exit pairs."""
    from .world import BuildError
    for s in steps:
        t = s[1]
        x, y = t[0] + 0.5, t[1] + 0.5
        if s[0] == "belt":
            try:
                world.place("transport-belt", x, y, s[2], {t})
            except BuildError as exc:
                exc.tile = t                      # the caller rolls back and routes around this tile
                raise
            continue
        world.clear([t])
        world.near(x, y)
        if math.dist((x, y), world.pos) < 1.2:
            world.step_out({t})
        opts = world.belt_opts("underground-belt") if hasattr(world, "belt_opts") else {}
        r = world.run.place("underground-belt", x, y, s[2], s[3], **opts)
        if not r.get("ok"):
            exc = BuildError(f"underground belt at {t}: {r.get('code')} {r.get('message', '')}")
            exc.tile = t
            raise exc
        world.used.add(t)
        world.inventory["underground-belt"] = max(0, world.inventory.get("underground-belt", 0) - 1)
        got = ((r.get("data") or {}).get("entity") or {})
        if got and (got.get("belt_type", s[3]) != s[3] or got.get("direction", s[2]) != s[2]):
            # the game paired it with another underground and turned it round: the belt would carry nothing.
            # The caller takes the link up again (this piece too) and routes around this tile
            exc = BuildError(f"underground belt at {t} came out as {got.get('belt_type')} facing "
                             f"{got.get('direction')} (wanted {s[3]} facing {s[2]}): it paired with another "
                             f"underground{' at ' + str(got['pairs_with']) if got.get('pairs_with') else ''}")
            exc.tile = t
            raise exc
