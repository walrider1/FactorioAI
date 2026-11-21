"""Compact memory for the LLM bots: the model sees its goal, a live state sheet and the last few turns.

Instead of resending the whole conversation every turn (cost grows with turns squared), the bot
keeps what matters itself, for free, from the results the game already returns:

    Ledger   budgets, tick, score, position, inventory, every entity the model has touched,
             recent rejections, and the model's own notes ("memo" on each tool call).
    Map      the latest observe, compressed to run-length rows. It changes only when the model
             observes again, so it sits early in the prompt and stays in the provider's cache.

Each request is then: system + tools (fixed), map (rarely changes), last K turns, state sheet.
Its size stays flat however long the run is. No extra observes are spent to build any of it.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Any

MEMO_PROPERTY = {
    "type": "string",
    "description": "Notes for your future self: plan, progress, what to do next (max ~400 characters). "
                   "You will NOT see older turns, only these notes and the state sheet, so carry "
                   "forward anything you still need. Repeat the previous notes if nothing changed.",
}

MEMORY_PROMPT = """
Memory: you only see the last few turns. Every turn ends with a STATE sheet (budgets, position,
inventory, your entities, rejections) kept by the game client, and the MAP from your latest observe.
Put your plan and progress in the `memo` argument of your tool calls; it is shown back to you every turn.
Coordinates: a tile (x,y) spans x..x+1, y..y+1. A 2x2 entity at integer (cx,cy) covers tiles
cx-1..cx by cy-1..cy; a 1x1 entity sits at a tile centre (x+0.5, y+0.5)."""

BATCH_PROMPT = """
Batch: put several tool calls in one turn; they run in order, and if one fails the rest are skipped
(skipped calls cost nothing). When building, send a whole step per turn, typically 5-15 calls: a row of
drills, furnaces, inserters and poles, then the fuel. Every turn costs money and time; one call per turn
wastes both and leaves you short of turns for the bigger tiers."""


def _key(x: float, y: float) -> tuple[float, float]:
    return (round(x * 2) / 2, round(y * 2) / 2)


def _fmt_counts(d: dict | None) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted((d or {}).items()) if v) or "-"


def _rle_rows(tiles: list[tuple[int, int]]) -> list[str]:
    """[(x, y)] tile corners -> ["y=3: 4-12 15-18", ...]."""
    rows: dict[int, list[int]] = {}
    for x, y in tiles:
        rows.setdefault(y, []).append(x)
    out = []
    for y in sorted(rows):
        xs = sorted(set(rows[y]))
        runs, start, prev = [], xs[0], xs[0]
        for x in xs[1:] + [None]:
            if x is not None and x == prev + 1:
                prev = x
                continue
            runs.append(f"{start}" if start == prev else f"{start}-{prev}")
            if x is not None:
                start = prev = x
        out.append(f"y={y}: {' '.join(runs)}")
    return out


def compress_observe(data: dict) -> str:
    """Observe result -> compact text map (tiles as run-length rows)."""
    lines = [f"MAP from your latest observe (tick {data.get('tick')}, you were at "
             f"({data['position']['x']}, {data['position']['y']})). Tile rows list x-ranges per y."]
    for name, tiles in sorted((data.get("resources") or {}).items()):
        cells = [(math.floor(t[0]), math.floor(t[1])) for t in tiles]
        total = sum(t[2] for t in tiles if len(t) > 2)
        lines.append(f"{name} ({len(cells)} tiles in view, {total} ore):")
        lines += ["  " + r for r in _rle_rows(cells)]
    for label in ("obstacles", "water"):
        pts = data.get(label) or []
        if pts:
            lines.append(f"{label}:")
            lines += ["  " + r for r in _rle_rows([(math.floor(p[0]), math.floor(p[1])) for p in pts])]
    summ = data.get("resource_summary") or {}
    if summ:
        lines.append("Within 64 tiles: " + "; ".join(
            f"{k} {v['tiles']} tiles, nearest ({v['nearest']['x']}, {v['nearest']['y']}) at {v['distance']}"
            for k, v in sorted(summ.items()) if v.get("nearest")))
    ws = data.get("water_summary")
    if ws:
        lines.append(f"Nearest water ({ws['tiles']} tiles within 64): ({ws['nearest']['x']}, {ws['nearest']['y']}) "
                     f"at {ws['distance']}")
    return "\n".join(lines)


class Ledger:
    """What the model would otherwise have to remember, rebuilt from tool results at no cost."""

    def __init__(self, task: dict, budgets: dict):
        self.task = task
        self.budgets = {k: budgets.get(k) for k in ("actions_left", "observes_left", "ticks_left")}
        self.inventory: dict[str, int] = dict(task.get("start_inventory") or {})
        self.inventory_exact = bool(task.get("start_inventory") is not None)
        self.entities: dict[tuple[float, float], dict] = {}
        self.belts: list[str] = []
        self.position: dict | None = None
        self.tick: int | None = None
        self.score: Any = None
        self.notes = ""
        self.map: str | None = None
        self.rejections: deque[str] = deque(maxlen=5)

    # ------------------------------------------------------------ updates
    def _add(self, item: str, n: int) -> None:
        self.inventory[item] = self.inventory.get(item, 0) + n
        if self.inventory[item] <= 0:
            self.inventory.pop(item)

    def _upsert(self, e: dict | None) -> None:
        if e and "x" in e:
            self.entities[_key(e["x"], e["y"])] = e

    def _drop_near(self, x: float, y: float) -> None:
        if not self.entities:
            return
        k = min(self.entities, key=lambda p: math.hypot(p[0] - x, p[1] - y))
        if math.hypot(k[0] - x, k[1] - y) <= 1.5:
            self.entities.pop(k)

    def record(self, name: str, args: dict, out: dict) -> None:
        brief = f"{name} " + " ".join(f"{k}={v}" for k, v in args.items())
        if "error" in out:
            self.rejections.append(f"{brief} -> {out['error']}: {out.get('message', '')}"[:200])
            return
        self.budgets = out.get("budgets", self.budgets)
        if out.get("score_so_far") is not None:
            self.score = out["score_so_far"]
        if not out.get("ok"):
            self.rejections.append(f"{brief} -> {out.get('code')}"[:200])
            return
        d = out.get("data") or {}
        if "tick" in d:
            self.tick = d["tick"]
        if name == "observe":
            self.position = d.get("position", self.position)
            self.inventory, self.inventory_exact = dict(d.get("inventory") or {}), True
            for e in d.get("entities") or []:
                self._upsert(e)
            self.map = compress_observe(d)
        elif name == "move":
            self.position = d.get("position", self.position)
        elif name == "place":
            self._add(args["entity"], -1)
            self._upsert(d.get("entity"))
        elif name == "place_line":
            placed = d.get("placed") or []                 # the game returns the placed tiles
            self._add(args["entity"], -(len(placed) if isinstance(placed, list) else int(placed)))
            links = d.get("links") or {}
            line = f"{args['entity']} ({args['x1']},{args['y1']})->({args['x2']},{args['y2']}) {d.get('direction', '')}".strip()
            if links:
                line += f" [start: {links.get('start')}; end: {links.get('end')}]"
            self.belts.append(line)
        elif name == "insert":
            self._add(args["item"], -int(d.get("inserted", 0)))
            self._upsert(d.get("entity"))
        elif name == "extract":
            self._add(args["item"], int(d.get("extracted", 0)))
            self._upsert(d.get("entity"))
        elif name == "craft":
            for k, v in (d.get("crafted") or {}).items():
                self._add(k, int(v))
            self.inventory_exact = False      # ingredients not known client-side
        elif name == "mine":
            for k, v in (d.get("mined") or {}).items():
                self._add(k, int(v))
        elif name in ("rotate", "set_recipe"):
            self._upsert(d.get("entity"))
        elif name == "pickup":
            if not d.get("cleared"):                 # a cleared tree or rock was never in the ledger
                self._drop_near(args["x"], args["y"])
            for k, v in (d.get("picked_up") or {}).items():
                self._add(k, int(v))

    # ------------------------------------------------------------- render
    def _entity_line(self, e: dict) -> str:
        inv = e.get("inventories") or {}
        parts = [f"{e['name']} ({e['x']},{e['y']})"]
        if e.get("direction"):
            parts.append(e["direction"])
        if e.get("recipe"):
            parts.append(f"recipe {e['recipe']}")
        if e.get("missing"):
            parts.append("missing " + ", ".join(e["missing"]))
        if e.get("pickup") and e.get("drop"):
            parts.append(f"picks ({e['pickup'][0]},{e['pickup'][1]}) drops ({e['drop'][0]},{e['drop'][1]})")
        elif e.get("drop"):
            parts.append(f"drops ({e['drop'][0]},{e['drop'][1]})")
        if e.get("status"):
            parts.append(e["status"])
        parts += [f"{slot}: {_fmt_counts(c)}" for slot, c in inv.items() if c]
        if e.get("protected"):
            parts.append("(scoring chest)")
        return "  " + " | ".join(parts)

    def render(self) -> str:
        b = self.budgets
        lines = [
            "STATE (kept by the game client; older turns are not shown)",
            f"Tick {self.tick if self.tick is not None else '?'}. Left: "
            f"{'no limit on' if b.get('actions_left') is None else b.get('actions_left')} actions, "
            f"{b.get('observes_left')} observes, {b.get('ticks_left')} ticks. Score so far: {self.score}.",
        ]
        if self.position:
            lines.append(f"Position: ({self.position['x']}, {self.position['y']})")
        tag = "" if self.inventory_exact else " (approximate since you crafted; observe to refresh)"
        lines.append(f"Inventory{tag}: {_fmt_counts(self.inventory)}")
        if self.entities:
            lines.append(f"Entities you placed or saw ({len(self.entities)}; statuses as of your last action on each):")
            lines += [self._entity_line(e) for _, e in sorted(self.entities.items(), key=lambda kv: (kv[0][1], kv[0][0]))]
        if self.belts:
            lines.append("Belt lines: " + "; ".join(self.belts))
        if self.rejections:
            lines.append("Recent rejections (don't repeat them):")
            lines += ["  " + r for r in self.rejections]
        lines.append(f"Your notes: {self.notes or '(none yet)'}")
        return "\n".join(lines)
