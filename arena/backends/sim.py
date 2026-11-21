"""Deterministic pure-Python simulator for levels 1 and 2.

It is not Factorio, but it uses Factorio 2.0 numbers (drill 240 ticks per ore,
furnace 192 ticks per plate, burner fuel use, 100-plate output cap, drop
positions) so strategies and scores carry over closely. It exists so the whole
stack can be tested in seconds without Docker, and so replays are exact.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from dataclasses import dataclass, field

from .. import rules
from ..config import Score, Task
from ..models import Outcome
from .base import Backend

Tile = tuple[int, int]

DRILL_TICKS_PER_ORE = round(rules.TICKS_PER_SECOND / 0.25)     # 240


@dataclass
class Ent:
    uid: int
    name: str
    x: float
    y: float
    direction: str
    spec: rules.EntitySpec
    fuel: Counter = field(default_factory=Counter)
    source: Counter = field(default_factory=Counter)
    output: Counter = field(default_factory=Counter)    # furnace output / chest contents
    energy: float = 0.0
    work: int = 0
    crafting: rules.Smelt | None = None
    pending: str | None = None
    status: str = "idle"
    protected: bool = False


def _box(name: str, x: float, y: float) -> tuple[float, float, float, float]:
    h = rules.ENTITIES[name].size / 2
    return x - h, y - h, x + h, y + h


def _tiles(box: tuple[float, float, float, float]) -> list[Tile]:
    x0, y0, x1, y1 = box
    return [(tx, ty)
            for ty in range(math.floor(y0 + 1e-6), math.floor(y1 - 1e-6) + 1)
            for tx in range(math.floor(x0 + 1e-6), math.floor(x1 - 1e-6) + 1)]


def _dist_to_box(px: float, py: float, box) -> float:
    x0, y0, x1, y1 = box
    dx = max(x0 - px, 0.0, px - x1)
    dy = max(y0 - py, 0.0, py - y1)
    return math.hypot(dx, dy)


def _clean(c: Counter) -> dict:
    return {k: v for k, v in sorted(c.items()) if v > 0}


class SimBackend(Backend):
    def __init__(self) -> None:
        self._tick = 0
        self.closed = False

    # ------------------------------------------------------------------ setup
    def setup(self, task: Task, seed: int) -> dict:
        self.task = task
        self.seed = seed
        self.resources: dict[Tile, list] = {}
        self._generate_map(random.Random(seed))
        self.entities: dict[int, Ent] = {}
        self.occupied: dict[Tile, int] = {}
        self.ground: dict[Tile, list] = {}
        self.char = [0.5, 0.5]
        self.inv: Counter = Counter(task.start_inventory)
        self.produced: Counter = Counter()
        self.consumed: Counter = Counter()
        self._next_uid = 1
        self._tick = 0
        return {"spawn": {"x": self.char[0], "y": self.char[1]}}

    def _generate_map(self, rng: random.Random) -> None:
        # name, distance range, radius range, amount range
        plan = [
            ("iron-ore", (9, 20), (5.0, 6.5), (700, 1500)),
            ("coal", (9, 22), (3.5, 4.5), (600, 1200)),
            ("stone", (11, 26), (3.0, 4.0), (500, 900)),
            ("copper-ore", (11, 26), (3.5, 4.5), (700, 1300)),
        ]
        centers: list[tuple[float, float, float]] = []
        for name, (dmin, dmax), (rmin, rmax), (amin, amax) in plan:
            for _ in range(200):
                ang = rng.uniform(0, 2 * math.pi)
                dist = rng.uniform(dmin, dmax)
                r = rng.uniform(rmin, rmax)
                cx, cy = math.cos(ang) * dist, math.sin(ang) * dist
                if dist - r < 4:
                    continue
                if all(math.hypot(cx - ox, cy - oy) > r + orr + 3 for ox, oy, orr in centers):
                    break
            centers.append((cx, cy, r))
            for ty in range(math.floor(cy - r - 2), math.ceil(cy + r + 2)):
                for tx in range(math.floor(cx - r - 2), math.ceil(cx + r + 2)):
                    if math.hypot(tx + 0.5 - cx, ty + 0.5 - cy) <= r + rng.uniform(-0.8, 0.8):
                        if (tx, ty) not in self.resources:
                            self.resources[(tx, ty)] = [name, rng.randint(amin, amax)]

    # ------------------------------------------------------------------- core
    def tick(self) -> int:
        return self._tick

    def run_ticks(self, n: int) -> None:
        ents = [self.entities[u] for u in sorted(self.entities)
                if self.entities[u].spec.kind in ("drill", "furnace")]
        if not ents:
            self._tick += n
            return
        for _ in range(n):
            for e in ents:
                if e.spec.kind == "drill":
                    self._update_drill(e)
                else:
                    self._update_furnace(e)
            self._tick += 1

    def _burn(self, e: Ent, joules: float) -> bool:
        if e.energy < joules:
            for f in ("coal", "wood"):
                if e.fuel[f] > 0:
                    e.fuel[f] -= 1
                    e.energy += rules.FUEL_VALUE[f]
                    self.consumed[f] += 1
                    break
            else:
                e.status = "no_fuel"
                return False
        e.energy -= joules
        return True

    def _update_drill(self, e: Ent) -> None:
        if e.pending:
            if not self._drop(e, e.pending):
                e.status = "waiting_for_space_in_destination"
                return
            e.pending = None
        tiles = [t for t in _tiles(_box(e.name, e.x, e.y)) if t in self.resources]
        if not tiles:
            e.status = "no_minable_resources"
            return
        if not self._burn(e, e.spec.energy_watts / rules.TICKS_PER_SECOND):
            return
        e.status = "working"
        e.work += 1
        if e.work >= DRILL_TICKS_PER_ORE:
            e.work = 0
            tile = tiles[0]
            res = self.resources[tile]
            name = res[0]
            res[1] -= 1
            if res[1] <= 0:
                del self.resources[tile]
            self.produced[name] += 1
            if not self._drop(e, name):
                e.pending = name

    def _drop(self, e: Ent, item: str) -> bool:
        dx, dy = rules.rotate_vector(e.spec.drop_vector_north, e.direction)
        tile = (math.floor(e.x + dx), math.floor(e.y + dy))
        uid = self.occupied.get(tile)
        if uid is not None:
            return self._machine_accepts(self.entities[uid], item)
        if tile not in self.ground:
            self.ground[tile] = [item, 1]
            return True
        return False

    def _machine_accepts(self, t: Ent, item: str) -> bool:
        kind = t.spec.kind
        if kind == "furnace" and item in rules.SMELTING:
            if (not t.source or item in t.source) and t.source[item] < rules.FURNACE_SOURCE_CAP:
                t.source[item] += 1
                return True
            return False
        if kind in ("furnace", "drill") and item in rules.FUEL_VALUE:
            if (not +t.fuel or item in +t.fuel) and t.fuel[item] < rules.FUEL_SLOT_CAP:
                t.fuel[item] += 1
                return True
            return False
        if kind == "chest":
            if self._room(t.output, item, t.spec.chest_slots) >= 1:
                t.output[item] += 1
                return True
        return False

    def _update_furnace(self, e: Ent) -> None:
        if e.crafting is None:
            src = +e.source
            if not src:
                e.status = "no_ingredients"
                return
            item = next(iter(src))
            sm = rules.SMELTING[item]
            if src[item] < sm.ingredient_count:
                e.status = "no_ingredients"
                return
            out = +e.output
            if out and (sm.product not in out or out[sm.product] >= rules.FURNACE_OUTPUT_CAP):
                e.status = "full_output"
                return
            e.source[item] -= sm.ingredient_count
            self.consumed[item] += sm.ingredient_count
            e.crafting = sm
            e.work = 0
        if not self._burn(e, e.spec.energy_watts / rules.TICKS_PER_SECOND):
            return
        e.status = "working"
        e.work += 1
        if e.work >= round(e.crafting.seconds * rules.TICKS_PER_SECOND):
            e.output[e.crafting.product] += 1
            self.produced[e.crafting.product] += 1
            e.crafting = None

    # ---------------------------------------------------------- inventories
    @staticmethod
    def _room(inv: Counter, item: str, slots: int) -> int:
        stack = rules.STACK_SIZE[item]
        used_by_others = sum(math.ceil(c / rules.STACK_SIZE[i]) for i, c in inv.items() if i != item and c > 0)
        return max(0, (slots - used_by_others) * stack - inv[item])

    def _char_room(self, item: str) -> int:
        return self._room(self.inv, item, rules.CHARACTER_SLOTS)

    def _entity_at(self, x: float, y: float) -> Ent | None:
        uid = self.occupied.get((math.floor(x), math.floor(y)))
        return self.entities.get(uid) if uid is not None else None

    def _in_reach(self, e: Ent) -> bool:
        return _dist_to_box(self.char[0], self.char[1], _box(e.name, e.x, e.y)) <= rules.REACH

    def describe(self, e: Ent) -> dict:
        d = {"name": e.name, "x": e.x, "y": e.y, "direction": e.direction, "status": e.status}
        if e.spec.kind == "drill":
            d["inventories"] = {"fuel": _clean(e.fuel)}
        elif e.spec.kind == "furnace":
            d["inventories"] = {"fuel": _clean(e.fuel), "source": _clean(e.source), "output": _clean(e.output)}
        elif e.spec.kind == "chest":
            d["inventories"] = {"contents": _clean(e.output)}
        if e.protected:
            d["protected"] = True
        return d

    # --------------------------------------------------------------- actions
    def act(self, a, max_ticks: int) -> Outcome:
        return getattr(self, "_act_" + a.type)(a, max_ticks)

    def _act_observe(self, a, max_ticks: int) -> Outcome:
        cx, cy = self.char
        ents = [self.describe(self.entities[u]) for u in sorted(self.entities)
                if _dist_to_box(cx, cy, _box(self.entities[u].name, self.entities[u].x, self.entities[u].y)) <= a.radius]
        resources: dict[str, list] = {}
        summary: dict[str, dict] = {}
        for (tx, ty), (name, amount) in sorted(self.resources.items()):
            px, py = tx + 0.5, ty + 0.5
            d = math.hypot(px - cx, py - cy)
            if d <= a.radius:
                resources.setdefault(name, []).append([px, py, amount])
            if d <= 64:
                s = summary.setdefault(name, {"nearest": None, "distance": 1e9, "tiles": 0, "amount": 0})
                s["tiles"] += 1
                s["amount"] += amount
                if d < s["distance"]:
                    s["distance"] = round(d, 2)
                    s["nearest"] = {"x": px, "y": py}
        return Outcome.success(
            tick=self._tick, position={"x": cx, "y": cy}, inventory=_clean(self.inv),
            entities=ents, resources=resources, resource_summary=summary, obstacles=[])

    def _act_move(self, a, max_ticks: int) -> Outcome:
        d = math.hypot(a.x - self.char[0], a.y - self.char[1])
        if d > rules.MAX_MOVE_DISTANCE:
            return Outcome.fail("too_far", f"max move distance is {rules.MAX_MOVE_DISTANCE:g} tiles")
        ticks = rules.move_ticks(d)
        if ticks > max_ticks:
            return Outcome.fail("insufficient_ticks", f"move needs {ticks} ticks")
        pos = self._free_spot(a.x, a.y)
        if pos is None:
            return Outcome.fail("blocked")
        self.char = list(pos)
        self.run_ticks(ticks)
        return Outcome.success(position={"x": pos[0], "y": pos[1]}, ticks=ticks)

    def _free_spot(self, x: float, y: float) -> tuple[float, float] | None:
        if (math.floor(x), math.floor(y)) not in self.occupied:
            return (x, y)
        bx, by = math.floor(x), math.floor(y)
        for r in range(1, 11):
            ring = [(bx + dx, by + dy) for dy in range(-r, r + 1) for dx in range(-r, r + 1)
                    if max(abs(dx), abs(dy)) == r]
            ring.sort(key=lambda t: (math.hypot(t[0] + 0.5 - x, t[1] + 0.5 - y), t))
            for t in ring:
                if t not in self.occupied:
                    return (t[0] + 0.5, t[1] + 0.5)
        return None

    def _act_place(self, a, max_ticks: int) -> Outcome:
        spec = rules.ENTITIES[a.entity]
        if not spec.sim:
            return Outcome.fail("cannot_place", "entity not supported by the simulator")
        if self.inv[a.entity] < 1:
            return Outcome.fail("not_in_inventory")
        x, y = rules.snap(a.entity, a.x, a.y)
        box = _box(a.entity, x, y)
        if _dist_to_box(self.char[0], self.char[1], box) > rules.REACH:
            return Outcome.fail("out_of_reach")
        tiles = _tiles(box)
        if any(t in self.occupied for t in tiles):
            return Outcome.fail("cannot_place", "something is in the way")
        cx, cy = self.char
        if box[0] < cx < box[2] and box[1] < cy < box[3]:
            return Outcome.fail("cannot_place", "the character is standing there")
        e = Ent(self._next_uid, a.entity, x, y, a.direction, spec)
        self._next_uid += 1
        self.entities[e.uid] = e
        for t in tiles:
            self.occupied[t] = e.uid
            self.ground.pop(t, None)
        self.inv[a.entity] -= 1
        return Outcome.success(entity=self.describe(e))

    def _act_place_line(self, a, max_ticks: int) -> Outcome:
        return Outcome.fail("cannot_place", "belts are not supported by the simulator")

    def _act_insert(self, a, max_ticks: int) -> Outcome:
        e = self._entity_at(a.x, a.y)
        if e is None:
            return Outcome.fail("no_entity")
        if not self._in_reach(e):
            return Outcome.fail("out_of_reach")
        if e.protected and a.item == self.task.score.item:
            return Outcome.fail("forbidden_target", "you may not insert the score item here")
        have = self.inv[a.item]
        if have < 1:
            return Outcome.fail("not_in_inventory")
        want = min(have, a.count)
        kind = e.spec.kind
        if kind == "chest":
            n = min(want, self._room(e.output, a.item, e.spec.chest_slots))
            e.output[a.item] += n
        elif kind == "furnace" and a.item in rules.SMELTING:
            ok = not +e.source or a.item in +e.source
            n = min(want, rules.FURNACE_SOURCE_CAP - e.source[a.item]) if ok else 0
            e.source[a.item] += n
        elif kind in ("furnace", "drill") and a.item in rules.FUEL_VALUE:
            ok = not +e.fuel or a.item in +e.fuel
            n = min(want, rules.FUEL_SLOT_CAP - e.fuel[a.item]) if ok else 0
            e.fuel[a.item] += n
        else:
            n = 0
        if n <= 0:
            return Outcome.fail("cannot_insert")
        self.inv[a.item] -= n
        return Outcome.success(inserted=n, entity=self.describe(e))

    def _act_extract(self, a, max_ticks: int) -> Outcome:
        e = self._entity_at(a.x, a.y)
        if e is None:
            return Outcome.fail("no_entity")
        if not self._in_reach(e):
            return Outcome.fail("out_of_reach")
        total = 0
        for inv in (e.output, e.source, e.fuel):
            take = min(a.count - total, inv[a.item], self._char_room(a.item))
            if take > 0:
                inv[a.item] -= take
                self.inv[a.item] += take
                total += take
        if total == 0:
            if e.output[a.item] + e.source[a.item] + e.fuel[a.item] > 0:
                return Outcome.fail("inventory_full")
            return Outcome.fail("nothing_to_extract")
        return Outcome.success(extracted=total, entity=self.describe(e))

    def _act_advance(self, a, max_ticks: int) -> Outcome:
        n = min(a.ticks, max_ticks)
        self.run_ticks(n)
        return Outcome.success(ticks=n)

    def _act_craft(self, a, max_ticks: int) -> Outcome:
        r = rules.HAND_RECIPES[a.recipe]
        need = {i: c * a.count for i, c in r.ingredients.items()}
        if any(self.inv[i] < c for i, c in need.items()):
            return Outcome.fail("not_enough_ingredients")
        ticks = rules.craft_ticks(a.recipe, a.count)
        if ticks > max_ticks:
            return Outcome.fail("insufficient_ticks", f"crafting needs {ticks} ticks")
        after = self.inv.copy()
        for i, c in need.items():
            after[i] -= c
        made = r.product_count * a.count
        if self._room(after, r.product, rules.CHARACTER_SLOTS) < made:
            return Outcome.fail("inventory_full")
        self.inv = after
        for i, c in need.items():
            self.consumed[i] += c
        self.run_ticks(ticks)
        self.inv[r.product] += made
        self.produced[r.product] += made
        return Outcome.success(crafted={r.product: made}, ticks=ticks)

    def _act_mine(self, a, max_ticks: int) -> Outcome:
        tile = (math.floor(a.x), math.floor(a.y))
        res = self.resources.get(tile)
        if res is None:
            return Outcome.fail("no_resource")
        if _dist_to_box(self.char[0], self.char[1], (tile[0], tile[1], tile[0] + 1, tile[1] + 1)) > rules.RESOURCE_REACH:
            return Outcome.fail("out_of_reach", f"hand mining reach is {rules.RESOURCE_REACH:g} tiles")
        name, amount = res
        n = min(a.count, amount)
        ticks = rules.hand_mine_ticks(name, n)
        if ticks > max_ticks:
            return Outcome.fail("insufficient_ticks", f"mining needs {ticks} ticks")
        if self._char_room(name) < n:
            return Outcome.fail("inventory_full")
        res[1] -= n
        if res[1] <= 0:
            del self.resources[tile]
        self.run_ticks(ticks)
        self.inv[name] += n
        self.produced[name] += n
        return Outcome.success(mined={name: n}, ticks=ticks)

    def _act_rotate(self, a, max_ticks: int) -> Outcome:
        e = self._entity_at(a.x, a.y)
        if e is None:
            return Outcome.fail("no_entity")
        if not self._in_reach(e):
            return Outcome.fail("out_of_reach")
        if e.spec.kind not in ("drill", "inserter", "belt"):
            return Outcome.fail("cannot_rotate")
        e.direction = a.direction
        return Outcome.success(entity=self.describe(e))

    def _act_pickup(self, a, max_ticks: int) -> Outcome:
        e = self._entity_at(a.x, a.y)
        if e is None:
            return Outcome.fail("no_entity")
        if not self._in_reach(e):
            return Outcome.fail("out_of_reach")
        if e.protected:
            return Outcome.fail("forbidden_target")
        ticks = rules.pickup_ticks(e.name)
        if ticks > max_ticks:
            return Outcome.fail("insufficient_ticks")
        gained = Counter({e.name: 1}) + e.fuel + e.source + e.output
        if e.crafting:
            gained[e.crafting.ingredient] += e.crafting.ingredient_count
        if e.pending:
            gained[e.pending] += 1
        trial = self.inv.copy()
        for item, c in gained.items():
            if self._room(trial, item, rules.CHARACTER_SLOTS) < c:
                return Outcome.fail("inventory_full")
            trial[item] += c
        self.inv = trial
        for t in _tiles(_box(e.name, e.x, e.y)):
            self.occupied.pop(t, None)
        del self.entities[e.uid]
        self.run_ticks(ticks)
        return Outcome.success(picked_up=_clean(gained), ticks=ticks)

    # ----------------------------------------------------------------- score
    def score(self, score: Score) -> int:
        if score.type == "production":
            return int(self.produced[score.item])
        return 0

    def score_breakdown(self, score: Score) -> dict:
        in_entities = sum(e.output[score.item] + e.source[score.item] for e in self.entities.values())
        return {
            "score": self.score(score),
            "produced": int(self.produced[score.item]),
            "in_character_inventory": int(self.inv[score.item]),
            "in_entities": int(in_entities),
            "coal_burned": int(self.consumed["coal"]),
        }

    def info(self) -> dict:
        return {"backend": "sim"}

    def close(self) -> None:
        self.closed = True
