"""Research follows the floorplan era (docs/run-4f7178-review.md, section 5 point 5 and plan step 6).

Run 4f7178: while blue science was not automated the labs worked on modules, battery and concrete (a "research
rocket-silo" queues that whole path), and the overseer had to drop advanced-material-processing-2 as premature.
Science packs were the binding limit all game, so every pack spent off the era's road held the era back.

While the floorplan's focus era is not automated, the research the bot picks is held to:
- the techs that unlock that era's and the next era's recipes (zones.OPEN_RECIPES, the packs zones.SEGMENTS adds,
  the head inputs zones.SEGMENT_INPUTS), with their prerequisites;
- the tier's essentials (ESSENTIAL_TECHS; FURNACE_TECH only while a plate is short).
The focus era is the live era while its packs are not all made by a factory, and the next era once they are: in
run 4f7178 segment 1 stayed live from minute 251 to 654, green ran from 254, and blue was what everything waited on.
A pick outside the set is dropped here by rule, logged once, never left for the overseer. Zones off or no era open:
nothing is held. Research the crew needs to unlock a kit it is building (crew.unlock_items) is never held.
"""

from __future__ import annotations

ESSENTIAL_TECHS = ("logistics-2", "electric-mining-drill", "steel-processing")
FURNACE_TECH = "advanced-material-processing"      # steel furnaces: twice the plates a column, when plates are short
SHORT_PLATES = ("iron-plate", "copper-plate", "steel-plate", "stone-brick")


def era_packs(era: int) -> set[str]:
    """The science packs (era 5: rocket parts) era n adds to the floorplan: era 1 red and green, 2 blue, ..."""
    from . import zones
    return set(zones.SEGMENTS.get(era, {})) - set(zones.SEGMENTS.get(era - 1, {}))


def focus_era(crew) -> int | None:
    """The era research serves: the live one while a factory does not make all its packs yet, else the next one
    while its packs are not all made. None: zones off, no era open, or both made (nothing held)."""
    from . import zones
    z = getattr(crew, "zones", None)
    if not zones.ENABLED or z is None or not getattr(z, "opened", None):
        return None
    live = z.era_of(z.current)
    for era in (live, live + 1):
        packs = era_packs(era)
        if packs and not all(crew.makes(p) for p in packs):
            return era
    return None


def era_techs(kn, era: int) -> set[str]:
    """Technologies (prerequisites included, researched ones left out) that unlock the recipes of every era up to
    the one after `era`, plus the tier's essentials."""
    from . import zones
    recipes: set[str] = set()
    for e in range(1, era + 2):
        recipes |= set(zones.OPEN_RECIPES.get(e, ()))
        for item in set(zones.SEGMENTS.get(e, {})) | set(zones.SEGMENT_INPUTS.get(e, ())):
            r = kn.recipe_for(item, unlocked_only=False)
            if r:
                recipes.add(r)
    out: set[str] = set()
    for t in [kn.unlocking_tech(r) for r in recipes] + list(ESSENTIAL_TECHS):
        if t:
            out.update(kn.tech_path(t))
    return out


def furnaces_short(crew) -> bool:
    """A plate is used faster than it is made (supply.shortfalls, the game's statistics)."""
    from .supply import shortfalls
    return any(item in SHORT_PLATES for item, _ in shortfalls(crew))


def hold(crew):
    """None when nothing is held; else a check: tech -> why it waits (None: it may be researched now)."""
    try:
        era = focus_era(crew)
        if era is None:
            return None
        allowed = era_techs(crew.kn, era)
    except Exception:  # noqa: BLE001 - a research decision never fails on the floorplan
        return None
    packs = ", ".join(sorted(era_packs(era)))

    def why(tech: str) -> str | None:
        if tech in allowed or tech in crew.kn.researched:
            return None
        if tech == FURNACE_TECH and furnaces_short(crew):
            return None
        return (f"{tech} is off the floorplan's road: until a factory makes {packs} (era {era}), research holds to "
                f"the techs for era {era} and {era + 1} and the tier's essentials")
    return why


def drop_off_era(crew, tech: str) -> str | None:
    """A research goal (the strategists' or the LLM's pick) for a tech off the era's road: dropped, logged once per
    era and tech, and the labs given an allowed tech if they sit idle. None: research it as asked."""
    check = hold(crew)
    why = check(tech) if check is not None else None
    if why is None:
        return None
    seen = crew.__dict__.setdefault("_era_dropped", set())
    key = (focus_era(crew), tech)
    if key not in seen:
        seen.add(key)
        crew.log(f"[crew] research rule: dropped {tech}: {why}")
    msg = f"dropped research {tech}: {why}"
    if crew.base.of("labs") and not (crew.w.research or {}).get("current"):
        pick = crew.auto_research()
        if pick:
            msg += f"; the labs were idle, so queued {pick}"
    return msg
