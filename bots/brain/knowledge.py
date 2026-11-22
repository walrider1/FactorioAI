"""What the game is: recipes, technologies, what unlocks what, hand-crafting plans. Built from GET /v1/catalog.

Everything here is pure (no game calls), so it is tested offline and shared by every layer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

RAW = {"iron-ore", "copper-ore", "coal", "stone", "wood", "crude-oil", "water", "uranium-ore"}
PLATES = {"iron-plate": "iron-ore", "copper-plate": "copper-ore", "stone-brick": "stone"}
# made in a furnace from plates, not from ore: the crew smelts these itself (crew.smelted), so kits that need
# them (a pumpjack's 5 steel) are planned like plates instead of blocked as "cannot hand-craft"
SMELTED = {"steel-plate": ("iron-plate", 5)}


@dataclass
class CraftPlan:
    """Hand crafts in order (recipe, times), and the raw items they need that crafting cannot make."""
    steps: list[tuple[str, int]] = field(default_factory=list)
    raw: dict[str, int] = field(default_factory=dict)
    blocked: list[str] = field(default_factory=list)      # items with no usable hand recipe (locked or machine-only)

    @property
    def seconds(self) -> float:
        return getattr(self, "_seconds", 0.0)


class Knowledge:
    def __init__(self, catalog: dict):
        self.cat = catalog
        self.recipes: dict[str, dict] = catalog.get("recipes", {})
        self.techs: dict[str, dict] = catalog.get("technologies", {})
        self.items: dict[str, dict] = catalog.get("items", {})
        self.entities: dict[str, dict] = catalog.get("entities", {})
        self.hand_cats = set((catalog.get("character") or {}).get("crafting_categories", ["crafting"]))
        self.unlocked: set[str] = {n for n, r in self.recipes.items() if r.get("enabled_at_start")}
        self.researched: set[str] = {n for n, t in self.techs.items() if t.get("researched_at_start")}
        self._unlocker: dict[str, str] = {}
        for name, t in self.techs.items():
            for r in t.get("unlocks", []):
                self._unlocker.setdefault(r, name)

    # --------------------------------------------------------- resources
    @property
    def resource_names(self) -> list[str]:
        return sorted(self.cat.get("resources") or ("coal", "copper-ore", "iron-ore", "stone"))

    def resource_name(self, name: str | None) -> str | None:
        """The game's name for a resource as a planner might write it ("iron", "Iron ore", "copper_ore", "oil"),
        or None when no resource is meant (a made-up name must never send the crew exploring for it)."""
        if not name:
            return None
        n = str(name).strip().lower().replace("_", "-").replace(" ", "-")
        names = self.resource_names
        if n in names:
            return n
        for cand in (f"{n}-ore", n.removesuffix("s"), {"oil": "crude-oil", "rock": "stone", "rocks": "stone"}.get(n)):
            if cand in names:
                return cand
        return None

    # ------------------------------------------------------------- state
    def learn_research(self, researched: list[str]) -> None:
        """Update from stats: researched technologies unlock their recipes."""
        for name in researched:
            if name not in self.researched:
                self.researched.add(name)
            for r in self.techs.get(name, {}).get("unlocks", []):
                self.unlocked.add(r)

    # ----------------------------------------------------------- recipes
    @staticmethod
    def categories(r: dict) -> set[str]:
        return set(r.get("categories") or ([r["category"]] if r.get("category") else []))

    def products(self, recipe: str) -> dict[str, float]:
        out = {}
        for p in self.recipes[recipe]["products"]:
            amt = p.get("amount")
            if amt is None:
                amt = (p.get("amount_min", 0) + p.get("amount_max", 0)) / 2
            out[p["name"]] = amt * (p.get("probability") or 1)
        return out

    def recipe_for(self, item: str, hand: bool = False, unlocked_only: bool = True) -> str | None:
        """The usual recipe that makes `item` (the one named after it first)."""
        cands = []
        for name, r in self.recipes.items():
            if item not in self.products(name):
                continue
            if hand and not self.hand_craftable(name):
                continue
            if unlocked_only and name not in self.unlocked:
                continue
            cands.append(name)
        if not cands:
            return None
        cands.sort(key=lambda n: (n != item, self.recipes[n].get("main_product") != item, len(n)))
        return cands[0]

    def hand_craftable(self, recipe: str) -> bool:
        r = self.recipes.get(recipe)
        if not r or not self.categories(r) & self.hand_cats:
            return False
        return all(i["type"] == "item" for i in r["ingredients"]) and all(p["type"] == "item" for p in r["products"])

    def unlocking_tech(self, recipe: str) -> str | None:
        return self._unlocker.get(recipe)

    def hand_plan(self, item: str, count: int, have: dict[str, int] | None = None) -> CraftPlan:
        """What to hand-craft, in order, to end up with `count` more of `item`, using what is in `have` first.
        Plates, ore, stone, wood and coal are raw here (they come from mining and furnaces)."""
        return self.kit_plan({item: count}, have, extra=True)

    def kit_plan(self, kit: dict[str, int], have: dict[str, int] | None = None, extra: bool = False,
                 supplied: set[str] | frozenset = frozenset()) -> CraftPlan:
        """One plan for a list of items (a module's kit), sharing stock between them.
        extra=False: `kit` is what you want to hold in total; extra=True: on top of what you hold.
        supplied: items our workshop cells make; those that cannot be hand-crafted are taken from their chests."""
        stock = dict(have or {})
        plan = CraftPlan()
        seconds = 0.0

        def need(it: str, n: float, depth: int = 0) -> None:
            nonlocal seconds
            use = min(max(stock.get(it, 0), 0), n)
            stock[it] = stock.get(it, 0) - use
            n -= use
            if n <= 0:
                return
            if it in RAW or it in PLATES or it in SMELTED or depth > 12:
                plan.raw[it] = plan.raw.get(it, 0) + math.ceil(n)
                return
            rec = self.recipe_for(it, hand=True)
            if rec is None and it in supplied:
                plan.raw[it] = plan.raw.get(it, 0) + math.ceil(n)
                return
            if rec is None:
                if it not in plan.blocked:
                    plan.blocked.append(it)
                plan.raw[it] = plan.raw.get(it, 0) + math.ceil(n)
                return
            per = self.products(rec)[it]
            times = math.ceil(n / per)
            for ing in self.recipes[rec]["ingredients"]:
                need(ing["name"], ing["amount"] * times, depth + 1)
            seconds += self.recipes[rec]["energy"] * times
            plan.steps.append((rec, times))          # after its ingredients, so the order is craftable
            stock[it] = stock.get(it, 0) + per * times - n

        for item, n in kit.items():
            if extra:                                 # n more on top of what we hold: holdings do not count
                held = stock.pop(item, 0)
                need(item, n)
                stock[item] = stock.get(item, 0) + held
            else:                                     # n in total: holdings count first
                need(item, n)
        plan._seconds = seconds
        return plan

    # -------------------------------------------------------- technology
    def tech_path(self, target: str) -> list[str]:
        """Technologies to research, prerequisites first, to get `target` (already researched ones left out)."""
        out: list[str] = []
        seen: set[str] = set()

        def visit(t: str) -> None:
            if t in seen or t in self.researched or t not in self.techs:
                return
            seen.add(t)
            for p in self.techs[t].get("prerequisites", []):
                visit(p)
            out.append(t)

        visit(target)
        return out

    def is_trigger(self, tech: str) -> bool:
        return bool(self.techs.get(tech, {}).get("trigger"))

    def research_cost(self, tech: str) -> dict[str, int]:
        t = self.techs[tech]
        n = t.get("unit_count") or 0
        return {i["name"]: i["amount"] * n for i in t.get("unit_ingredients", [])}

    def research_seconds(self, tech: str, labs: int = 1) -> float:
        t = self.techs[tech]
        return (t.get("unit_count") or 0) * (t.get("unit_time") or 0) / max(1, labs)

    def tech_for_recipe_path(self, recipe: str) -> list[str]:
        t = self.unlocking_tech(recipe)
        return self.tech_path(t) if t else []

    def size(self, entity: str) -> tuple[int, int]:
        e = self.entities.get(entity) or {}
        return int(e.get("width", 1)), int(e.get("height", 1))

    # ------------------------------------------------------------- fluids
    DIRS = ("north", "east", "south", "west")
    VEC = {0: (0, -1), 4: (1, 0), 8: (0, 1), 12: (-1, 0)}

    def ports(self, entity: str, x: float, y: float, direction: str = "north") -> list[dict]:
        """Every pipe connection of an entity placed at (x, y): its fluid box, input/output, the tile inside the
        entity it sits on, and the tile outside a pipe must take to join it (from the catalog's fluid_ports)."""
        out = []
        r = self.DIRS.index(direction)
        for box in (self.entities.get(entity) or {}).get("fluid_ports") or []:
            for c in box.get("connections") or []:
                if c.get("type") != "normal":
                    continue
                px, py = c["positions"][r]
                vx, vy = self.VEC[(int(c.get("direction", 0)) + 4 * r) % 16]
                inside = (math.floor(x + px), math.floor(y + py))
                out.append({"box": box["index"], "production": box.get("production"), "flow": c.get("flow"),
                            "filter": box.get("filter"), "inside": inside, "target": (inside[0] + vx, inside[1] + vy)})
        return out

    # A recipe fluid's fluidbox_index (the n-th input or output box of its machine), which the catalog leaves out.
    # Read from the stock game's prototypes (2.1.24): basic oil processing takes crude in the refinery's SECOND
    # input and gives petroleum from its THIRD output, where advanced processing has them. Run 20261121-140755
    # piped crude into the first input (water's): the refinery never ran and both pumpjacks backed up for hours.
    FLUIDBOX_INDEX = {"basic-oil-processing": {"crude-oil": 2, "petroleum-gas": 3}}

    def fluid_slots(self, entity: str, recipe: str | None) -> dict[int, str]:
        """Which fluid each fluid box carries: a fluid with a fluidbox_index takes that input/output box (counted
        among the machine's inputs or outputs from 1), the rest take the remaining boxes in order (the game's
        rule); fixed filters (boilers, pumps) win."""
        boxes = (self.entities.get(entity) or {}).get("fluid_ports") or []
        out = {b["index"]: b["filter"] for b in boxes if b.get("filter")}
        if recipe and recipe in self.recipes:
            r = self.recipes[recipe]
            fixed = self.FLUIDBOX_INDEX.get(recipe, {})
            for kind, prod in (("ingredients", "input"), ("products", "output")):
                fluids = [(x["name"], x.get("fluidbox_index") or fixed.get(x["name"]))
                          for x in r[kind] if x["type"] == "fluid"]
                free = [b["index"] for b in sorted(boxes, key=lambda b: b["index"])
                        if b.get("production") == prod and b["index"] not in out]
                mine = [b["index"] for b in sorted(boxes, key=lambda b: b["index"]) if b.get("production") == prod]
                for name, n in fluids:
                    if n and 0 < n <= len(mine) and mine[n - 1] in free:
                        out[mine[n - 1]] = name
                        free.remove(mine[n - 1])
                for name, n in fluids:
                    if not (n and name in out.values()) and free:
                        out[free.pop(0)] = name
        return out

    def is_fluid(self, name: str) -> bool:
        return name in (self.cat.get("fluids") or {})
