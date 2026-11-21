"""More of anything the base runs short of, sized to the gap: one rule for every item, not one per item.

    supply_goal(crew, item, short_per_min)   the build that makes `short_per_min` more of `item` (with headroom),
                                             in the best module the current technology allows, or None

How it decides, by what the item IS (never by its name):
    an ore, coal or stone      drills on its patch into chests: electric (30/min each) once electric drills and
                               power are in, burner (15/min, coal burners keep ~2.25 of their own) before
    a plate or brick           drills on its ore, each into a furnace (the furnace or the drill, whichever is
                               slower: 18.75 iron/copper plates or 15 bricks a minute in a stone furnace)
    a furnace product of       a smelting column on its source plate (steel: 3.75/min per stone furnace,
    a plate (steel)            7.5 per steel furnace)
    anything an assembler      a belt factory at that rate
    makes from these
    anything else              a workshop (oil products, chemistry), or nothing when no module can make it

The size is ceil(gap x headroom / rate per unit), at least 2, at most what one build of the module takes (12
drills a row, 8 burner drills, 24 column furnaces): a bigger gap gets the biggest build now and the rest the next
time it is measured. A patch with no room left near the base gets an outpost (or a scout); burner drills on a full
patch are swapped for electric ones. A goal the overseer forbids is skipped for the next option.

Run 20261120-023103: iron ran at 150/min against 220/min used for an hour; the only fix on offer was a burner mine
(the overseer had forbidden those) and every iron row was asked for at the habitual 4 drills of 12.
"""

from __future__ import annotations

import math

from .knowledge import PLATES, RAW, SMELTED

HEADROOM = 1.3                  # build for the gap plus this much: demand grows while the build goes up
ELECTRIC_ORE = 30.0             # an electric drill: 0.5 ore a second
BURNER_ORE = 15.0               # a burner drill: 0.25 ore a second
BURNER_SELF = 2.25              # coal a burner drill burns itself
FURNACE = {"stone-furnace": 18.75, "steel-furnace": 37.5}       # plates a minute (3.2 s recipes)
STEEL = {"stone-furnace": 3.75, "steel-furnace": 7.5}           # steel a minute (16 s recipe)
MINABLE = {"iron-ore", "copper-ore", "coal", "stone", "uranium-ore"}
MAX_UNITS = {"electric_mine": 12, "burner_mine": 8, "smelter_column": 24}


def _units(want: float, rate: float, module: str) -> int:
    return max(2, min(MAX_UNITS.get(module, 12), math.ceil(want / max(rate, 0.01))))


def _electric(crew) -> bool:
    """Electric drills researched and a powered network to wire them to."""
    try:
        return "electric-mining-drill" in crew.kn.unlocked and bool(crew.base.powered_poles())
    except Exception:  # noqa: BLE001 - stub crews in tests
        return False


def _patch_full(crew, resource: str) -> bool:
    full = getattr(crew, "patch_full", None)
    try:
        return bool(full(resource)) if callable(full) else False
    except Exception:  # noqa: BLE001
        return False


def _ore_per(item: str, kn) -> float:
    """Ore a plate/brick takes (bricks: 2 stone each), from the recipe when known."""
    rec = (getattr(kn, "recipes", None) or {}).get(item) or {}
    ore = PLATES.get(item)
    for ing in rec.get("ingredients") or []:
        if ing.get("name") == ore:
            return float(ing.get("amount") or 1)
    return 2.0 if item == "stone-brick" else 1.0


def _column_furnace(kn) -> str:
    return "steel-furnace" if "steel-furnace" in (getattr(kn, "unlocked", None) or set()) else "stone-furnace"


def mining_options(crew, resource: str, want: float, into: str) -> list[dict]:
    """Ways to mine `want` more a minute of a resource (into chests for the ore itself, furnaces for its plate):
    best first. Plate rates are per drill: the slower of the drill and its stone furnace."""
    item_per_unit = ELECTRIC_ORE
    burner_per_unit = BURNER_ORE - (BURNER_SELF if resource == "coal" else 0.0)
    if into == "furnace":
        plate = next((p for p, o in PLATES.items() if o == resource), None)
        per = _ore_per(plate, crew.kn) if plate else 1.0
        item_per_unit = min(ELECTRIC_ORE / per, FURNACE["stone-furnace"])
        burner_per_unit = min(BURNER_ORE / per, FURNACE["stone-furnace"])
    out: list[dict] = []
    electric = _electric(crew)
    if _patch_full(crew, resource):
        # no room on the patches near the base: burners there become electric drills (same ground, twice the
        # ore), else the next patch out
        burners = [s for s in crew.base.of("burner_mine") if s.params.get("resource") == resource]
        if electric and burners:
            n = int(burners[0].params.get("count", 2))
            out.append({"do": "replace", "site": burners[0].id,
                        "with": {"do": "build", "module": "electric_mine", "resource": resource,
                                 "count": max(2, min(12, n)), "into": into}})
        from .assess import expand_fix
        fix = expand_fix(crew, resource)
        if fix:
            out.append(fix)
        return out
    if electric:
        out.append({"do": "build", "module": "electric_mine", "resource": resource,
                    "count": _units(want, item_per_unit, "electric_mine"), "into": into})
    out.append({"do": "build", "module": "burner_mine", "resource": resource,
                "count": _units(want, burner_per_unit, "burner_mine"), "into": into})
    return out


def supply_options(crew, item: str, short_per_min: float, headroom: float = HEADROOM) -> list[dict]:
    """Every way to make `short_per_min` more of `item` (with headroom), best first."""
    if short_per_min <= 0:
        return []
    want = short_per_min * headroom
    kn = crew.kn
    if item in MINABLE:
        return mining_options(crew, item, want, "chest")
    if item in PLATES:
        return mining_options(crew, PLATES[item], want, "furnace")
    if item in SMELTED:
        src, _ = SMELTED[item]
        rate = STEEL.get(_column_furnace(kn), 3.75)
        return [{"do": "build", "module": "smelter_column", "resource": src,
                 "count": _units(want, rate, "smelter_column")}]
    if item in RAW:
        return []                                    # wood, crude oil, water: no module makes more of these here
    from .modules import MODULES
    out: list[dict] = []
    try:
        if item in MODULES["factory"].can_make(kn):
            out.append({"do": "build", "module": "factory", "item": item, "per_minute": round(max(2.0, want), 1)})
    except Exception:  # noqa: BLE001 - no catalog in some tests
        pass
    if "workshop" in MODULES and (getattr(kn, "recipes", None) or {}).get(item):
        out.append({"do": "build", "module": "workshop", "item": item, "per_minute": round(max(2.0, want), 1)})
    return out


def supply_goal(crew, item: str, short_per_min: float, headroom: float = HEADROOM) -> dict | None:
    """The best build for the gap that nobody has forbidden and whose inputs do not already starve (see
    supply_options and nodes.consumer_block), or None."""
    from .nodes import goal_inputs, starving
    from .overseer import forbidden
    try:
        hungry = starving(crew)
    except Exception:  # noqa: BLE001 - stub crews
        hungry = {}
    for g in supply_options(crew, item, short_per_min, headroom):
        try:
            if hungry and goal_inputs(crew, g) & set(hungry):
                continue                             # a gear factory while iron ports starve: connect first
            if forbidden(crew, g) is None:
                return g
        except Exception:  # noqa: BLE001 - stub crews without the overseer's state
            return g
    return None


def shortfalls(crew, headroom: float = HEADROOM, top: int = 3) -> list[tuple[str, float]]:
    """Items the base uses faster than it makes them with `headroom` (game statistics), biggest gap first:
    [(item, per minute short of made = used x headroom)]. Only items some module can make more of."""
    try:
        from .assess import flows
        rows = flows(crew)
    except Exception:  # noqa: BLE001 - stub crews in tests
        return []
    try:                                         # plates already made but sitting where no belt takes them
        from .nodes import stranded
        lost: dict[str, float] = {}
        for r in stranded(crew):
            lost[r["item"]] = lost.get(r["item"], 0.0) + r["per_minute"]
    except Exception:  # noqa: BLE001 - stub crews
        lost = {}
    out = []
    for f in rows:
        if f["used"] <= 0:
            continue
        gap = f["used"] * headroom - f["made"] - lost.get(f["item"], 0.0)   # connect those first (phase 3)
        if gap > 1.0 and supply_options(crew, f["item"], gap / headroom, headroom):
            out.append((f["item"], round(gap / headroom, 1)))
    return sorted(out, key=lambda q: -q[1])[:top]
