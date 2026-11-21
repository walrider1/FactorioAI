"""Scripted baseline for Level 1 (organizer test bot, also a reference for teams).

Strategy: walk to the iron patch, build drill -> furnace pairs (drill facing
north into a furnace directly above it), split the coal so every pair runs to
the end, advance, empty furnaces once before they hit the 100-plate cap, then
spend the rest of the ticks. No coal mining, so it leaves points on the table
on purpose: a good team should beat it.

    python bots/scripted_bot.py --url http://127.0.0.1:8710 --token fa_...
"""

from __future__ import annotations

import argparse
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sdk"))
from arena_sdk import ArenaClient, Run  # noqa: E402

DRILL_COAL_PER_SEC = 150_000 / 4_000_000
FURNACE_COAL_PER_SEC = 90_000 * 0.8 / 4_000_000


def tiles_of(cx: int, cy: int) -> list[tuple[int, int]]:
    return [(cx - 1, cy - 1), (cx, cy - 1), (cx - 1, cy), (cx, cy)]


def plan_pairs(iron: set[tuple[int, int]], station: tuple[float, float], blocked: set[tuple[int, int]],
               max_pairs: int, reach: float = 9.5) -> list[tuple[int, int]]:
    """Drill centers (cx, cy) on iron, with a free furnace slot at (cx, cy-2), all within reach."""
    used: set[tuple[int, int]] = set()
    cands = []
    xs = [t[0] for t in iron]
    ys = [t[1] for t in iron]
    for cy in range(min(ys) - 1, max(ys) + 3):
        for cx in range(min(xs) - 1, max(xs) + 3):
            ore = sum(t in iron for t in tiles_of(cx, cy))
            if ore >= 3:
                for px, py in ((cx, cy), (cx, cy - 2)):
                    if math.hypot(px - station[0], py - station[1]) > reach:
                        break
                else:
                    cands.append((-ore, math.hypot(cx - station[0], cy - station[1]), cx, cy))
    cands.sort()
    out = []
    for _, _, cx, cy in cands:
        need = set(tiles_of(cx, cy)) | set(tiles_of(cx, cy - 2))
        if need & used or need & blocked:
            continue
        if any(abs(station[0] - tx - 0.5) < 0.6 and abs(station[1] - ty - 0.5) < 0.6 for tx, ty in need):
            continue
        used |= need
        out.append((cx, cy))
        if len(out) == max_pairs:
            break
    return out


def play(run: Run, log=print) -> dict:
    obs = run.observe(radius=20)["data"]
    summary = obs["resource_summary"].get("iron-ore")
    if not summary:
        log("no iron in sight")
        return run.end_and_wait()
    near = summary["nearest"]
    if obs["resources"].get("iron-ore") is None or summary["distance"] > 6:
        run.move(near["x"], near["y"])
        obs = run.observe(radius=20)["data"]
    iron = {(math.floor(x), math.floor(y)) for x, y, _ in obs["resources"].get("iron-ore", [])}
    cx = sum(t[0] for t in iron) / len(iron)
    cy = sum(t[1] for t in iron) / len(iron)
    # stand on a tile near the patch centre that we will not build on
    station = (math.floor(cx) + 0.5, math.floor(cy) + 0.5)
    run.move(*station)
    obs = run.observe(radius=20)["data"]
    station = (obs["position"]["x"], obs["position"]["y"])
    blocked = {(math.floor(o[0]), math.floor(o[1])) for o in obs.get("obstacles", [])}
    blocked |= {(math.floor(w[0]), math.floor(w[1])) for w in obs.get("water", [])}
    inv = obs["inventory"]
    ticks_left = run.budgets["ticks_left"]
    seconds = (ticks_left + 3600) / 60
    per_pair = math.ceil(DRILL_COAL_PER_SEC * seconds) + math.ceil(FURNACE_COAL_PER_SEC * seconds) + 2
    coal = inv.get("coal", 0)
    n = min(inv.get("burner-mining-drill", 0), inv.get("stone-furnace", 0), max(1, coal // per_pair))
    pairs = plan_pairs(iron, station, blocked, n)
    log(f"station {station}, building {len(pairs)} pairs, {per_pair} coal each")
    built = []
    for dx, dy in pairs:
        if not run.place("burner-mining-drill", dx, dy, "north")["ok"]:
            continue
        if not run.place("stone-furnace", dx, dy - 2)["ok"]:
            run.pickup(dx, dy)
            continue
        built.append((dx, dy))
    if not built:
        return run.end_and_wait()
    coal = run.observe(radius=4)["data"]["inventory"].get("coal", 0)
    share = coal // len(built)
    d_share = round(share * DRILL_COAL_PER_SEC / (DRILL_COAL_PER_SEC + FURNACE_COAL_PER_SEC))
    for dx, dy in built:
        run.insert(dx, dy, "coal", min(50, d_share))
        run.insert(dx, dy - 2, "coal", min(50, share - d_share))
    # first stretch: furnaces fill at 0.25 plates/s, extract before 100
    ticks_left = run.budgets["ticks_left"]
    first = min(ticks_left, 22_000)
    run.advance(first)
    for dx, dy in built:
        if (run.budgets["actions_left"] or 99) <= 1:
            break
        run.extract(dx, dy - 2, "iron-plate", 100)
    if run.budgets["ticks_left"] > 0:
        run.advance(run.budgets["ticks_left"])
    return run.end_and_wait()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("ARENA_URL", "http://127.0.0.1:8710"))
    ap.add_argument("--token", default=os.environ.get("ARENA_TOKEN", ""))
    ap.add_argument("--task", default="level1-iron")
    ap.add_argument("--mode", default="practice")
    args = ap.parse_args()
    client = ArenaClient(args.url, args.token)
    run = client.start_run(args.task, args.mode)
    print("run", run.id, run.state.get("map"))
    print(play(run))


if __name__ == "__main__":
    main()
