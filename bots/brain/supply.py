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
# Fewer, bigger mines: a 2-drill row runs dry within hours and every mine is one more source with its own belts
# (run 20261121-140755: 27 of 37 electric mines had 2 drills, 15 were rebuilt, and each rebuild moved belts). An
# electric row of a bulk resource is built at least this big (the row finder takes the longest run that fits, so
# a crowded patch still gets what fits); the spare it makes backs up in its furnaces or chests, where the crew
# collects it. Stone and anything else used in small amounts keeps the plain sizing.
BULK = ("iron-ore", "copper-ore", "coal")
MIN_UNITS = {"electric_mine": 6}
MIN_UNITS_CHEST = {"electric_mine": 4}          # ore or coal straight into chests: 30/min a drill already


def least_units(module: str, resource: str | None, into: str = "furnace") -> int:
    if resource not in BULK:
        return 2
    return (MIN_UNITS_CHEST if into == "chest" else MIN_UNITS).get(module, 2)


def _units(want: float, rate: float, module: str, into: str = "furnace", resource: str | None = None) -> int:
    return max(least_units(module, resource, into), min(MAX_UNITS.get(module, 12), math.ceil(want / max(rate, 0.01))))


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


# Every resource is mined densely once electric drills run. Not uranium yet: its drills need sulfuric acid piped
# to each of them, which the dense layout does not lay (a uranium field would stand idle).
FIELD_ORES = ("iron-ore", "copper-ore", "coal", "stone")
NEAR_FIELD_LEAST = 4            # drills of a near-base dense field at least (~120 ore a minute). Run 20261125-154120:
                                # 8 drills and a 13-furnace column for 33 iron/min at minute 49 took 19 minutes
NEAR_FIELD_MOST = 16            # and at most: one 24-furnace column smelts what 15 drills mine (stone furnaces)
SMALL_MINE = 6                  # an electric mine of this many drills or fewer is replaced by a field on a full patch
                                # (rows built before fields were possible are 6 drills: supply.MIN_UNITS)
FIELD_RETRY_MIN = 30.0          # game minutes of mine rows after a near field failed to build
CLEAR_RETRY_MIN = 60.0         # game minutes before another small mine is cleared for a field after one failed


def near_field(crew, resource: str, want: float, into: str, cleared: bool = False) -> dict | None:
    """A dense field on the patch near the base (outposts.Outpost with near_base): drills on both sides of one
    belt, smelted in a column beside it (plates, bricks), or into depot chests at the base (coal, and any ore or
    stone wanted raw: into chest)."""
    if resource not in FIELD_ORES or (into == "chest" and resource != "coal"):
        # raw ore or stone into chests: a row of drills into chests is already compact, and a field would add a
        # column and a trunk for nothing (run 20261125-184025: an iron field with a chest column for raw ore)
        return None
    unlocked = getattr(getattr(crew, "kn", None), "unlocked", None)
    if unlocked is not None and not {"underground-belt", "long-handed-inserter"} <= set(unlocked):
        # a field's trunk and its column's coal and plate belts are walls until belts can cross (logistics), and
        # its column lifts coal with long-handed inserters (automation): until both, rows, swapped for fields later
        # (small_mines). Run 20261125-184025: two fields and a coal field before logistics walled red science off
        # its iron (no belt route), so its packs starved logistics itself
        return None
    failed = (getattr(crew, "__dict__", {}).get("_field_failed") or {}).get(resource)
    if not cleared and failed is not None and getattr(crew, "_minute", lambda: 0.0)() - failed < FIELD_RETRY_MIN:
        return None                                  # one failed lately: rows meanwhile
    n = max(NEAR_FIELD_LEAST, min(NEAR_FIELD_MOST, math.ceil(want / ELECTRIC_ORE)))
    return {"do": "build", "module": "outpost", "resource": resource, "count": n, "near_base": True,
            **({"into": "chest"} if into == "chest" and resource != "coal" else {})}


def small_mines(crew, resource: str) -> list:
    """Electric mines of a few drills on this ore, smallest and oldest first: what a dense field replaces once the
    patch near the base has no room left (the old drills and their furnaces come down first, see Crew replace
    with clear_first)."""
    return sorted((s for s in crew.base.of("electric_mine") if s.params.get("resource") == resource
                   and int(s.params.get("count") or 0) <= SMALL_MINE),
                  key=lambda s: (int(s.params.get("count") or 0), s.id))


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
                                 "count": max(_units(want, item_per_unit, "electric_mine", into, resource), min(12, n)),
                                 "into": into}})
        # (a field that found no room before may fit once a small mine's ground is cleared)
        field = near_field(crew, resource, want, into, cleared=True) if electric else None
        smalls = small_mines(crew, resource) if field else []
        failed = (getattr(crew, "__dict__", {}).get("_clear_failed") or {}).get(resource)
        minute = getattr(crew, "_minute", lambda: 0.0)()
        if smalls and (failed is None or minute - failed > CLEAR_RETRY_MIN):
            # a full patch of small rows: the smallest comes down and a dense field takes its ground
            out.append({"do": "replace", "site": smalls[0].id, "clear_first": True, "with": field})
        from .assess import expand_fix
        fix = expand_fix(crew, resource)
        if fix:
            out.append(fix)
        return out
    dense = near_field(crew, resource, want, into) if electric else None
    if dense:                     # once electric drills run, bulk ore and coal come from dense fields near the base
        out.append(dense)
    if electric:                  # a row only where the overseer forbids the field (or no field fits: patch full)
        out.append({"do": "build", "module": "electric_mine", "resource": resource,
                    "count": _units(want, item_per_unit, "electric_mine", into, resource), "into": into})
    if not electric:              # once electric drills run, never back to hand-fed burners: run 20261124-015025
        out.append({"do": "build", "module": "burner_mine", "resource": resource,   # fell back to them when the
                    "count": _units(want, burner_per_unit, "burner_mine"), "into": into})   # electric row was banned
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
