"""The production planner: for any item at a rate, every recipe, machine kind and machine count down to what the
crew brings in by hand (plates, ore, coal, stone) and what comes out of the ground (water, crude oil).

Pure and catalog-driven, so it covers the whole game, not a hand-kept list:

    pl = Planner(kn)
    plan = pl.plan("chemical-science-pack", 10, spare={"electronic-circuit": 20})
    plan.cells     # [Need(recipe, machine, count, item_in, item_out, fluid_in, fluid_out), ...] producers first
    plan.raw       # items per minute the courier must bring: {"iron-plate": 140, "coal": 3, ...}
    plan.fluids    # fluid sources per minute: {"water": 1200, "crude-oil": 800}

A machine is one cell (an input chest and inserter, the machine, an output inserter and chest), so the count
also respects what one inserter can carry.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# brought by the courier from mines and storage, never made in a cell
SUPPLIED = {"iron-plate", "copper-plate", "coal", "stone", "iron-ore", "copper-ore", "wood", "uranium-ore"}
SOURCES = {"water", "crude-oil"}                 # fluids from offshore pumps and pumpjacks
MACHINE_FOR = {                                  # crafting category -> machines in order of preference
    "crafting": ["assembling-machine-2", "assembling-machine-1"],
    "advanced-crafting": ["assembling-machine-2", "assembling-machine-1"],
    "crafting-with-fluid": ["assembling-machine-2"],
    "smelting": ["electric-furnace", "steel-furnace", "stone-furnace"],
    "chemistry": ["chemical-plant"],
    "oil-processing": ["oil-refinery"],
    "rocket-building": ["rocket-silo"],
}
# the one recipe to use for an item that several recipes make
PREFERRED = {"petroleum-gas": "advanced-oil-processing", "light-oil": "advanced-oil-processing",
             "heavy-oil": "advanced-oil-processing", "solid-fuel": "solid-fuel-from-petroleum-gas"}
INSERTER_PER_MIN = {"inserter": 50.0, "fast-inserter": 130.0, "burner-inserter": 35.0}
FUEL_BURN = {"stone-furnace": 1.35, "steel-furnace": 1.35}     # coal a minute for a burner furnace at full work


@dataclass
class Need:
    recipe: str
    machine: str
    count: int
    rate: float                                  # crafts a minute wanted (all machines)
    item_in: dict[str, float] = field(default_factory=dict)      # per minute, all machines
    item_out: dict[str, float] = field(default_factory=dict)
    fluid_in: dict[str, float] = field(default_factory=dict)
    fluid_out: dict[str, float] = field(default_factory=dict)
    sink: bool = False                           # makes something only so a refinery never stalls (emptied to storage)

    def brief(self) -> str:
        outs = ", ".join(f"{k} {v:g}/min" for k, v in {**self.item_out, **self.fluid_out}.items())
        return f"{self.count} x {self.machine} [{self.recipe}] -> {outs}"


@dataclass
class Plan:
    item: str
    per_minute: float
    cells: list[Need] = field(default_factory=list)
    raw: dict[str, float] = field(default_factory=dict)
    fluids: dict[str, float] = field(default_factory=dict)
    blocked: list[str] = field(default_factory=list)      # items with no recipe the planner can use
    locked: set[str] = field(default_factory=set)         # recipes that need research first

    @property
    def machines(self) -> int:
        return sum(c.count for c in self.cells)

    def kit(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for c in self.cells:
            out[c.machine] = out.get(c.machine, 0) + c.count
        return out

    def summary(self) -> str:
        return f"{self.per_minute:g}/min {self.item}: " + "; ".join(c.brief() for c in self.cells)


class Planner:
    def __init__(self, kn, inserter: str = "inserter"):
        self.kn = kn
        self.inserter = inserter

    # ------------------------------------------------------------ choices
    def recipe(self, item: str) -> str | None:
        """The recipe a cell uses for `item` (never barrels, never recycling)."""
        pref = PREFERRED.get(item)
        if pref and pref in self.kn.recipes:
            if item == "petroleum-gas" and pref not in self.kn.unlocked and "basic-oil-processing" in self.kn.recipes:
                return "basic-oil-processing" if "basic-oil-processing" in self.kn.unlocked else pref
            return pref
        cands = []
        for name, r in self.kn.recipes.items():
            if "barrel" in name or "recycling" in name:
                continue
            prods = {p["name"] for p in r["products"]}
            if item in prods:
                cands.append(name)
        if not cands:
            return None
        cands.sort(key=lambda n: (n != item, self.kn.recipes[n].get("main_product") != item,
                                  n not in self.kn.unlocked, len(n)))
        return cands[0]

    def machine(self, recipe: str) -> str | None:
        cats = self.kn.categories(self.kn.recipes[recipe])
        for cat in cats:
            for m in MACHINE_FOR.get(cat, []):
                if m not in self.kn.entities:
                    continue
                rec = self.kn.recipe_for(m, unlocked_only=False)
                if m == MACHINE_FOR[cat][-1] or rec is None or rec in self.kn.unlocked:
                    return m
        return None

    def speed(self, machine: str) -> float:
        return float((self.kn.entities.get(machine) or {}).get("crafting_speed") or 1.0)

    def crafts_per_min(self, recipe: str, machine: str) -> float:
        return 60.0 * self.speed(machine) / max(0.01, float(self.kn.recipes[recipe].get("energy") or 0.5))

    # ------------------------------------------------------------ planning
    def plan(self, item: str, per_minute: float, spare: dict[str, float] | None = None,
             margin: float = 1.1) -> Plan:
        """Walk the recipe tree from `item` down to supplied items and fluid sources. spare: what cells already
        built make beyond what they use (per minute); it is used before new cells are planned."""
        spare = dict(spare or {})
        p = Plan(item, per_minute)
        want: dict[str, float] = {}                 # recipe -> crafts a minute
        order: list[str] = []                       # recipes, consumers first
        byprod: dict[str, float] = {}               # by-products of this plan nobody uses (yet)

        def need(it: str, rate: float, depth: int = 0) -> None:
            if rate <= 1e-9:
                return
            use = min(spare.get(it, 0.0), rate)
            if use > 0:
                spare[it] -= use
                rate -= use
                if byprod.get(it):
                    byprod[it] = max(0.0, byprod[it] - use)
            if rate <= 1e-9:
                return
            if it in SUPPLIED:
                p.raw[it] = p.raw.get(it, 0.0) + rate
                return
            if it in SOURCES:
                p.fluids[it] = p.fluids.get(it, 0.0) + rate
                return
            rec = self.recipe(it)
            if rec is None or depth > 14:
                if it not in p.blocked:
                    p.blocked.append(it)
                return
            mach = self.machine(rec)
            if mach is None:
                if it not in p.blocked:
                    p.blocked.append(it)
                return
            if rec not in self.kn.unlocked:
                p.locked.add(rec)
            out = {pr["name"]: _amount(pr) for pr in self.kn.recipes[rec]["products"]}
            crafts = rate / out[it]
            if rec in want:                          # a multi-product recipe (the refinery): one cell set covers
                extra = max(0.0, crafts - want[rec])   # whichever product needs the most
                want[rec] += extra
                crafts = extra
            else:
                want[rec] = crafts
                order.append(rec)
            for pname, amt in out.items():           # by-products count as spare for later needs
                if pname != it:
                    spare[pname] = spare.get(pname, 0.0) + crafts * amt
                    byprod[pname] = byprod.get(pname, 0.0) + crafts * amt
            for ing in self.kn.recipes[rec]["ingredients"]:
                need(ing["name"], crafts * ing["amount"], depth + 1)

        need(item, per_minute)
        # A refinery stops the moment any of its outputs backs up. Oil it makes that nothing here uses is cracked
        # down (heavy -> light -> petroleum), and petroleum left over goes into solid fuel in a chest (a sink the
        # courier empties), so the refinery never stalls on a by-product.
        sinks: set[str] = set()
        for fl, rec, sink in (("heavy-oil", "heavy-oil-cracking", False), ("light-oil", "light-oil-cracking", False),
                              ("petroleum-gas", "solid-fuel-from-petroleum-gas", True)):
            left = byprod.get(fl, 0.0)
            if left <= 1 or rec not in self.kn.recipes or (rec not in self.kn.unlocked and not sink):
                continue
            r = self.kn.recipes[rec]
            amt_in = next(i["amount"] for i in r["ingredients"] if i["name"] == fl)
            crafts = left / amt_in
            spare[fl] = max(0.0, spare.get(fl, 0.0) - left)
            byprod[fl] = 0.0
            want[rec] = want.get(rec, 0.0) + crafts
            if rec not in order:
                order.insert(0, rec)
            for x in r["products"]:
                if x["type"] == "fluid":
                    byprod[x["name"]] = byprod.get(x["name"], 0.0) + crafts * _amount(x)
                    spare[x["name"]] = spare.get(x["name"], 0.0) + crafts * _amount(x)
            for i in r["ingredients"]:
                if i["name"] != fl:
                    need(i["name"], crafts * i["amount"])
            if sink:
                sinks.add(rec)
        ins_cap = INSERTER_PER_MIN.get(self.inserter, 50.0)
        for rec in order:
            mach = self.machine(rec)
            r = self.kn.recipes[rec]
            crafts = want[rec] * margin
            per_machine = self.crafts_per_min(rec, mach)
            items_in = sum(i["amount"] for i in r["ingredients"] if i["type"] == "item")
            items_out = sum(_amount(x) for x in r["products"] if x["type"] == "item")
            count = crafts / per_machine
            if mach != "rocket-silo":                # one input and one output inserter per cell
                if items_in:
                    count = max(count, crafts * items_in / ins_cap)
                if items_out:
                    count = max(count, crafts * items_out / ins_cap)
            n = max(1, math.ceil(count - 1e-6))
            need_ = Need(rec, mach, n, round(crafts, 2))
            for i in r["ingredients"]:
                (need_.fluid_in if i["type"] == "fluid" else need_.item_in)[i["name"]] = round(crafts * i["amount"], 2)
            for x in r["products"]:
                (need_.fluid_out if x["type"] == "fluid" else need_.item_out)[x["name"]] = round(crafts * _amount(x), 2)
            if FUEL_BURN.get(mach):
                need_.item_in["coal"] = round(FUEL_BURN[mach] * n, 2)
                p.raw["coal"] = p.raw.get("coal", 0.0) + FUEL_BURN[mach] * n
            need_.sink = rec in sinks
            p.cells.append(need_)
        p.cells.reverse()                            # producers before consumers: build order
        p.raw = {k: round(v, 2) for k, v in p.raw.items()}
        p.fluids = {k: round(v, 2) for k, v in p.fluids.items()}
        return p

    def power_kw(self, plan: Plan) -> float:
        """What the plan's machines draw at full work (kW)."""
        kw = 0.0
        for c in plan.cells:
            e = self.kn.entities.get(c.machine) or {}
            if e.get("electric"):
                kw += float(e.get("max_energy_usage") or e.get("energy_usage") or 0) * 60 / 1000 * c.count
            kw += 13.0 * 2 * c.count                 # two inserters
        return round(kw, 1)


def _amount(p: dict) -> float:
    a = p.get("amount")
    if a is None:
        a = (p.get("amount_min", 0) + p.get("amount_max", 0)) / 2
    return float(a) * float(p.get("probability") or 1)
