"""Recipe maths: how many machines of each kind a target rate needs (vanilla Factorio 2.x numbers)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# recipe -> (ingredients, products per craft, seconds at crafting speed 1)
RECIPES: dict[str, tuple[dict[str, int], int, float]] = {
    "iron-gear-wheel": ({"iron-plate": 2}, 1, 0.5),
    "copper-cable": ({"copper-plate": 1}, 2, 0.5),
    "electronic-circuit": ({"iron-plate": 1, "copper-cable": 3}, 1, 0.5),
    "inserter": ({"electronic-circuit": 1, "iron-gear-wheel": 1, "iron-plate": 1}, 1, 0.5),
    "transport-belt": ({"iron-plate": 1, "iron-gear-wheel": 1}, 2, 0.5),
    "automation-science-pack": ({"copper-plate": 1, "iron-gear-wheel": 1}, 1, 5.0),
    "logistic-science-pack": ({"inserter": 1, "transport-belt": 1}, 1, 6.0),
}
ORE_FOR = {"iron-plate": "iron-ore", "copper-plate": "copper-ore"}   # plates come from mine cells

ASSEMBLER_SPEED = 0.5            # assembling-machine-1
FURNACE_PER_MIN = 60 / 3.2       # stone furnace, speed 1: 18.75 plates a minute
DRILL_PER_MIN = 30.0             # electric mining drill: 0.5 ore a second (feeds one furnace with room to spare)
INSERTER_PER_MIN = 45.0          # a basic inserter, belt to machine, kept conservative (~0.75 items a second)


@dataclass
class Plan:
    """Machines per recipe and the flows between them, for one target item at one rate."""
    target: str
    per_minute: float
    machines: dict[str, int] = field(default_factory=dict)       # recipe, plate or supplied item -> count
    supplied: dict[str, float] = field(default_factory=dict)     # inputs from other factories -> per minute
    rate: dict[str, float] = field(default_factory=dict)         # item -> needed per minute

    def summary(self) -> str:
        parts = [f"{n} x {k}" for k, n in self.machines.items()]
        return f"{self.per_minute:g}/min {self.target}: " + ", ".join(parts)


ASSEMBLER_CATEGORIES = {"crafting", "advanced-crafting"}     # what assembling-machine-1 can make


def load_catalog(catalog: dict) -> list[str]:
    """Learn every recipe an assembler can make from items, from the game's catalog (GET /v1/catalog).

    Only single-product item recipes are added, so any item whose chain bottoms out in iron and copper
    plates becomes designable (buildings for a mall, ammo, more science...). Returns what can be made."""
    for name, r in (catalog.get("recipes") or {}).items():
        cats = set(r.get("categories") or [r.get("category")])
        prods = r.get("products") or []
        if not cats & ASSEMBLER_CATEGORIES or len(prods) != 1 or prods[0].get("type") != "item":
            continue
        if any(i.get("type") != "item" for i in r.get("ingredients") or []):
            continue
        item = prods[0]["name"]
        if item != name or item in ORE_FOR or name in RECIPES:
            continue
        RECIPES[name] = ({i["name"]: i["amount"] for i in r["ingredients"]}, int(prods[0].get("amount") or 1),
                         float(r.get("energy") or 0.5))
    return sorted(i for i in RECIPES if feasible(i))


def feasible(item: str, depth: int = 0, supplied: frozenset | set = frozenset()) -> bool:
    """Can a factory make this from iron and copper ore, plus `supplied` items (inputs that arrive from other
    factories: steel, plastic, sulfur, processing units...)?"""
    if item in ORE_FOR or item in supplied:
        return True
    if item not in RECIPES or depth > 10:
        return False
    return all(feasible(i, depth + 1, supplied) for i in RECIPES[item][0])


def inputs_needed(item: str, depth: int = 0) -> set[str]:
    """The items a belt factory for `item` cannot make itself (no assembler recipe from plates): what other
    factories must supply through input cells. Empty for red and green science."""
    if item in ORE_FOR:
        return set()
    if item not in RECIPES or depth > 10:
        return {item}
    out: set[str] = set()
    for i in RECIPES[item][0]:
        out |= inputs_needed(i, depth + 1)
    return out


def machine_rate(recipe: str) -> float:
    """Products a minute one assembler makes, capped by how fast one inserter can feed each ingredient."""
    ings, out, secs = RECIPES[recipe]
    by_speed = 60 * ASSEMBLER_SPEED * out / secs
    by_feed = min(INSERTER_PER_MIN * out / n for n in ings.values())
    return min(by_speed, by_feed, INSERTER_PER_MIN)          # one output inserter, too


FEED_PER_MIN = 450.0            # one lane of a yellow belt: what one feed (a belt straight in) carries


def plan(target: str, per_minute: float, margin: float = 1.1, supplied: frozenset | set = frozenset(),
         feeds: frozenset | set = frozenset()) -> Plan:
    """Walk the recipe tree from the target down to plates and supplied items. margin: build a little over the
    target. A supplied item gets input cells (one per inserter's worth of flow) instead of assemblers."""
    need: dict[str, float] = {}

    def add(item: str, rate: float) -> None:
        need[item] = need.get(item, 0.0) + rate
        if item in RECIPES and item not in supplied:
            ings, out, _ = RECIPES[item]
            for ing, n in ings.items():
                add(ing, rate * n / out)

    add(target, per_minute)
    p = Plan(target, per_minute, rate={k: round(v, 2) for k, v in need.items()})
    order = ([k for k in RECIPES if k in need and k not in supplied] + [k for k in ORE_FOR if k in need and k not in supplied]
             + [k for k in need if k in supplied])
    for item in order:
        # a supplied plate gets one input cell per furnace's worth: each is belted from one mine chest (one drill
        # into one furnace), so cells and the mine chests that feed them match one for one
        # a fed item comes in on one belt (a feed): one per lane's worth, not one per furnace
        cap = (FEED_PER_MIN if item in supplied and item in feeds
               else (FURNACE_PER_MIN if item in ORE_FOR else INSERTER_PER_MIN) if item in supplied
               else machine_rate(item) if item in RECIPES else FURNACE_PER_MIN)
        p.machines[item] = max(1, math.ceil(need[item] * margin / cap))
    p.supplied = {k: round(need[k] * margin, 2) for k in need if k in supplied}
    return p
