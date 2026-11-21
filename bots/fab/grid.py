"""The planner's view of the map: a snapshot of what the bot has seen, independent of the game."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

Tile = tuple[int, int]


@dataclass
class Grid:
    seen: set[Tile] = field(default_factory=set)
    water: set[Tile] = field(default_factory=set)
    cliffs: set[Tile] = field(default_factory=set)
    used: set[Tile] = field(default_factory=set)            # already built on
    keepout: set[Tile] = field(default_factory=set)         # reserved by earlier builds (e.g. a belt's way in)
    obstacles: set[Tile] = field(default_factory=set)       # trees and rocks: buildable after clearing
    ore: dict[Tile, str] = field(default_factory=dict)      # tile -> resource name
    origin: tuple[float, float] = (0.0, 0.0)                # where the bot stood when the snapshot was taken

    @classmethod
    def from_world(cls, world) -> "Grid":
        ore: dict[Tile, str] = {}
        for name, tiles in world.resources.items():
            for t in tiles:
                ore[t] = name
        # lanes (margins kept free for the bot's belts between sites) are off limits to a factory floor too
        keep = set(world.keepout) | set(getattr(world, "lanes", ()) or ())
        return cls(set(world.seen), set(world.water), set(world.cliffs), set(world.used), keep,
                   set(world.obstacles), ore, tuple(world.pos))

    def free(self, t: Tile) -> bool:
        return (t in self.seen and t not in self.water and t not in self.cliffs and t not in self.used
                and t not in self.keepout)

    def ore_tiles(self, name: str) -> set[Tile]:
        return {t for t, n in self.ore.items() if n == name}

    @classmethod
    def from_log(cls, actions_jsonl: Path, upto_seq: int | None = None) -> "Grid":
        """Rebuild what a bot knew from a run's action log: every observe, plus every building it placed.
        For debugging a design offline, exactly as the bot saw the map."""
        import math
        sizes = {"electric-mining-drill": (3, 3), "assembling-machine-1": (3, 3), "burner-mining-drill": (2, 2),
                 "stone-furnace": (2, 2), "boiler": (3, 2), "steam-engine": (3, 5), "splitter": (2, 1)}
        g = cls()
        for line in Path(actions_jsonl).read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if upto_seq is not None and row["seq"] > upto_seq:
                break
            a, d = row["action"], row.get("data") or {}
            if not row.get("ok"):
                continue
            if a["type"] == "observe":
                px, py = d["position"]["x"], d["position"]["y"]
                r = a.get("radius", 16)
                g.origin = (px, py)
                for tx in range(math.floor(px - r), math.ceil(px + r) + 1):
                    for ty in range(math.floor(py - r), math.ceil(py + r) + 1):
                        if math.hypot(tx + 0.5 - px, ty + 0.5 - py) <= r - 1:
                            g.seen.add((tx, ty))
                            g.obstacles.discard((tx, ty))
                for name, tiles in (d.get("resources") or d.get("resource_xy") or {}).items():
                    for x, y, *_ in tiles:                     # the run log keeps tiles as resource_xy
                        g.ore[(math.floor(x), math.floor(y))] = name
                g.water.update((math.floor(x), math.floor(y)) for x, y in d.get("water") or [])
                for x, y, kind in d.get("obstacles") or []:
                    t = (math.floor(x), math.floor(y))
                    (g.cliffs if kind == "cliff" else g.obstacles).add(t)
                for e in d.get("entities") or []:
                    w, h = sizes.get(e["name"], (1, 1))
                    if e.get("direction") in ("east", "west"):
                        w, h = h, w
                    x0, y0 = math.floor(e["x"] - w / 2 + 0.01), math.floor(e["y"] - h / 2 + 0.01)
                    g.used.update((x0 + i, y0 + j) for i in range(w) for j in range(h))
            elif a["type"] == "place_line":
                g.used.update((math.floor(x), math.floor(y)) for x, y in d.get("placed") or [])
            elif a["type"] == "place":
                e = d.get("entity") or {}
                if e:
                    w, h = sizes.get(e["name"], (1, 1))
                    if e.get("direction") in ("east", "west"):
                        w, h = h, w
                    x0, y0 = math.floor(e["x"] - w / 2 + 0.01), math.floor(e["y"] - h / 2 + 0.01)
                    g.used.update((x0 + i, y0 + j) for i in range(w) for j in range(h))
        for t in g.used:
            g.obstacles.discard(t)
        return g

    # ---- fixtures (offline tests and the LLM designer's map)
    def save(self, path: Path) -> None:
        def ts(s):
            return sorted([list(t) for t in s])
        data = {"seen": ts(self.seen), "water": ts(self.water), "cliffs": ts(self.cliffs), "used": ts(self.used),
                "keepout": ts(self.keepout), "obstacles": ts(self.obstacles),
                "ore": sorted([[t[0], t[1], n] for t, n in self.ore.items()]), "origin": list(self.origin)}
        Path(path).write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "Grid":
        d = json.loads(Path(path).read_text(encoding="utf-8"))

        def st(k):
            return {(a, b) for a, b in d[k]}
        return cls(st("seen"), st("water"), st("cliffs"), st("used"), st("keepout"), st("obstacles"),
                   {(a, b): n for a, b, n in d["ore"]}, tuple(d["origin"]))
