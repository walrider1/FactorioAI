"""The road to the rocket, computed from the game's own data and the base as it stands.

    rm = roadmap(crew)
    rm["stages"]      # the ladder from power to a launched rocket, each done / now / todo with a detail line
    rm["research"]    # technologies left to rocket-silo, packs they cost, lab hours at the labs we have
    rm["science"]     # each pack: made per minute (factories + cells) against the rate the road needs
    rm["power"]       # capacity against demand
    rm["next_step"]   # the one goal that moves the earliest unfinished stage (the rocket book plays these)

Both strategists read it (the LLM in its report, the free book as its plan past green science), and the monitor
shows it, so a person sees what the bot is aiming at and what it waits on.
"""

from __future__ import annotations

import math

SCIENCE_RATE = {"automation-science-pack": 15, "logistic-science-pack": 15, "chemical-science-pack": 10,
                "production-science-pack": 6, "utility-science-pack": 6}
PACK_TECH = {"automation-science-pack": "automation-science-pack", "logistic-science-pack": "logistic-science-pack",
             "chemical-science-pack": "chemical-science-pack", "production-science-pack": "production-science-pack",
             "utility-science-pack": "utility-science-pack"}
LABS_TARGET = 12
RATE_HEADROOM = 1.15     # build 15% over a rate milestone: its window must hold the rate for 5-10 minutes
RATE_DONE = 0.9          # an item counts as built up at 90% of its target (just over the milestone's rate)
RATE_LOOKAHEAD = 3       # the next unreached milestones whose rates count: targets step up with the ladder
FLUID_ITEMS = ("plastic-bar", "sulfur", "chemical-science-pack", "production-science-pack", "utility-science-pack")


def rate_targets(crew) -> dict[str, float]:
    """What each item is built up to: the road's science rates (SCIENCE_RATE), raised to RATE_HEADROOM over the rate
    of any of the next RATE_LOOKAHEAD unreached rate milestones of the task (bot.play gives crew.milestone_ladder).
    Run 20261120-211555: purple and yellow were built for 6/min against milestones of 10/min (5000 points out of
    reach as written), blue stopped adding at 8/min against 10, and the 30/min milestone for red and green and the
    30/min plastic one were never aimed at. Science packs first, in road order; other items (plastic) after."""
    out = dict(SCIENCE_RATE)
    reached = set((getattr(crew.w, "milestones", None) or {}))
    upcoming = [m for m in getattr(crew, "milestone_ladder", None) or [] if m.get("id") not in reached]
    for m in upcoming[:RATE_LOOKAHEAD]:
        if m.get("item") and m.get("per_minute") and not m.get("fluid"):
            for it in [m["item"]] + list(m.get("also") or []):
                out[it] = max(out.get(it, 0.0), round(float(m["per_minute"]) * RATE_HEADROOM, 1))
    return out


def milestone_rate(crew, item: str) -> float | None:
    """The rate of the task's first rate milestone for this item (the "automated" step), None without one."""
    for m in getattr(crew, "milestone_ladder", None) or []:
        if m.get("per_minute") and item in [m.get("item")] + list(m.get("also") or []):
            return float(m["per_minute"])
    return None
SILO_PARTS_PER_MIN = 0.5                         # 100 parts in ~3.5 game hours, gentle on the courier
SILO_KIT = {"steel-plate": 1000, "concrete": 1000, "processing-unit": 200, "electric-engine-unit": 200, "pipe": 100}
CRUDE_PER_PUMPJACK = 300.0                       # a careful guess: fresh wells give 300-600 a minute


PACK_COLOUR = {"automation-science-pack": "Red", "logistic-science-pack": "Green", "chemical-science-pack": "Blue",
               "production-science-pack": "Purple", "utility-science-pack": "Yellow"}


def automated_milestone(crew, item: str) -> str | None:
    """The id of the task's first rate milestone for this item (the "automated" step), None without one."""
    for m in getattr(crew, "milestone_ladder", None) or []:
        if m.get("per_minute") and m.get("automated", True) and item in [m.get("item")] + list(m.get("also") or []):
            return m.get("id")
    return None


def automated(crew, pack: str, default: float) -> bool:
    """Is this pack made by machines at its milestone's rate? The game decides: its milestone check leaves
    hand-crafting out (the scenario counts the crew's crafts apart). The production statistics do not, so a crew
    crafting red science for its first research read as "Red science automated" at minute 34.7 of run
    20261125-154120, before any red science machine stood. Without such a milestone in the task: the measured
    rate less what the crew crafted by hand over the last minutes."""
    mid = automated_milestone(crew, pack)
    if mid is not None:
        return mid in (getattr(crew.w, "milestones", None) or {})
    return pack_rate(crew, pack) - hand_rate(crew, pack) >= default


def hand_rate(crew, item: str, window_min: float = 5.0) -> float:
    """What the crew crafted of an item a minute lately, from the game's running count of hand crafts (kept per
    reading on the crew, so no extra game call)."""
    row = ((getattr(crew.w, "stats", None) or {}).get("items") or {}).get(item) or {}
    try:
        now = crew.w.now_tick() / 3600
    except Exception:  # noqa: BLE001 - stub worlds in tests
        return 0.0
    seen = crew.__dict__.setdefault("_hand_seen", {}).setdefault(item, [])
    total = float(row.get("hand_made") or 0)
    if not seen or seen[-1][0] < now:
        seen.append((now, total))
    while len(seen) > 2 and now - seen[1][0] >= window_min:
        seen.pop(0)
    t0, n0 = seen[0]
    return (total - n0) / (now - t0) if now - t0 >= 0.5 else 0.0


def planned_rate(crew, pack: str) -> float:
    """What the factories and cells built for an item were sized for (nominal: they may starve)."""
    fac = sum(float(s.params.get("per_minute", 0)) for s in crew.base.of("factory") if s.params.get("item") == pack)
    return round(fac + crew.cell_rate(pack), 1)


def pack_rate(crew, pack: str) -> float:
    """What the base really makes of an item a minute: the game's production statistics (the same window as the
    report's rates). Stages and the close-out go by this, so a starving factory is no "done" stage (run
    20261122-133537: the roadmap said red and green "done, 30/min" at a measured 6.6 and 0, and moved on to oil).
    Without statistics at all (no game read yet) the planned rate stands in."""
    items = (getattr(crew.w, "stats", None) or {}).get("items")
    if not items:
        return planned_rate(crew, pack)
    return round(float((items.get(pack) or {}).get("made_per_min") or 0), 1)


def supplying(site) -> bool:
    """Can this site make plates at all: its ore input belted (a column's trunk, a port) and its machines on power?
    Run 20261122-133537: column 33 (no trunk, 30 inserters without power) counted 281/min of iron the base never
    saw; plates.supply_per_min said 622 iron against 194 measured."""
    for pt in site.params.get("ports", []) or []:
        if pt.get("item") and not pt.get("fuel") and not pt.get("fed"):
            return False                              # (coal: the foreman feeds a row by hand until its belt)
    n = sum((site.health or {}).values())
    return not (n and (site.health or {}).get("no_power", 0) > n // 10)


def plate_supply(crew) -> dict[str, float]:
    """Plates a minute our mines can make (burner furnaces 15, electric units 18; workshop cells excluded)."""
    out: dict[str, float] = {}
    for s in crew.base.sites:
        if not supplying(s):
            continue
        steel = any(e[0] in ("steel-furnace", "electric-furnace") for e in s.entities)   # speed 2: twice as fast
        if s.kind == "burner_mine":
            per = 15.0
        elif s.kind == "electric_mine":
            per = 30.0 if steel else 18.75                              # one drill's 30 ore caps a steel furnace
        elif s.kind == "smelter_column":
            per = 37.5 if steel else 18.75
        else:
            per = 0.0
        for *_, p in s.plate_furnaces:
            rate = per / 5 if p == "steel-plate" else per               # five plates a steel
            out[p] = out.get(p, 0.0) + rate
        if s.kind == "electric_mine" and s.params.get("into") == "chest":
            r = s.params.get("resource")
            out[r] = out.get(r, 0.0) + 30.0 * int(s.params.get("count", 0))
        if s.kind == "burner_mine" and s.params.get("into") == "chest":
            r = s.params.get("resource")
            out[r] = out.get(r, 0.0) + 12.75 * int(s.params.get("count", 0))
    return {k: round(v, 1) for k, v in out.items()}


def workshop_demand(crew) -> dict[str, float]:
    """What the cells take a minute of the items the courier brings (plates, coal, stone, ore)."""
    from .production import SUPPLIED
    out: dict[str, float] = {}
    for _, c in crew.cells():
        for it, r in c.get("item_in", {}).items():
            if it in SUPPLIED or it in ("stone-brick",):
                out[it] = out.get(it, 0.0) + r
    return {k: round(v, 1) for k, v in out.items()}


def roadmap(crew) -> dict:
    kn, base = crew.kn, crew.base
    researched = kn.researched
    path = kn.tech_path("rocket-silo")
    packs: dict[str, int] = {}
    secs = 0.0
    labs = base.count("labs")
    for t in path:
        for k, v in kn.research_cost(t).items():
            packs[k] = packs.get(k, 0) + v
        secs += kn.research_seconds(t, 1)
    targets = rate_targets(crew)
    science = {p: {"made_per_min": pack_rate(crew, p), "planned_per_min": planned_rate(crew, p),
                   "target": targets[p], "unlocked": PACK_TECH[p] in researched}
               for p in SCIENCE_RATE}

    def made(p: str) -> str:                                # measured, and what was built for it beside it
        s = science[p]
        return f"{s['made_per_min']}/min" + (f" of {s['planned_per_min']} planned"
                                             if abs(s["planned_per_min"] - s["made_per_min"]) >= 0.5 else "")
    slowest = None
    for p, left in packs.items():
        rate = science.get(p, {}).get("made_per_min") or 0
        hours = left / rate / 60 if rate else None
        if hours is not None and (slowest is None or hours > slowest[1]):
            slowest = (p, hours)
    power = crew.power_status()
    silo = base.of("silo")
    rockets = (crew.w.stats or {}).get("rockets_launched") or 0
    oil = bool(base.of("pumpjack"))

    def st(name: str, done: bool, detail: str = "") -> dict:
        return {"name": name, "done": bool(done), "detail": detail}

    def auto(pack: str, default: float) -> float:          # a stage is done at its milestone's rate
        return milestone_rate(crew, pack) or default

    stages = [
        st("Power and labs", base.of("steam_power") and base.of("labs"),
           f"{power['stations']} steam stations, {labs} labs"),
        st("Red science automated", automated(crew, "automation-science-pack", auto("automation-science-pack", 5)),
           made("automation-science-pack")),
        st("Green science automated", automated(crew, "logistic-science-pack", auto("logistic-science-pack", 5)),
           made("logistic-science-pack")),
        st("Workshop floor and electric mines", base.of("workshop") and base.of("electric_mine"),
           f"{len(crew.cells())} cells, {len(base.of('electric_mine'))} electric mines"),
        st("Oil: pumpjacks and a refinery", oil and any(c["machine"] == "oil-refinery" for _, c in crew.cells()),
           f"{base.count('pumpjack')} pumpjacks"),
        st("Blue science automated", automated(crew, "chemical-science-pack", auto("chemical-science-pack", 5)),
           made("chemical-science-pack")),
        st("Purple science automated", automated(crew, "production-science-pack", auto("production-science-pack", 3)),
           made("production-science-pack")),
        st("Yellow science automated", automated(crew, "utility-science-pack", auto("utility-science-pack", 3)),
           made("utility-science-pack")),
        st("Rocket silo researched", "rocket-silo" in researched,
           f"{len(path)} technologies left" if path else "researched"),
        st("Silo built", bool(silo), ""),
        st("Rocket parts", rockets > 0, ""),
        st("Rocket launched", rockets > 0, f"{rockets} launched"),
    ]
    # science stages go by the game's automated milestones (automated()): each carries its milestone id, so a stamp
    # a resumed run kept from before this rule is dropped while the milestone is not in (telemetry.stamp_stages)
    for s in stages:
        if s["name"].endswith("science automated"):
            pack = next((p for p, c in PACK_COLOUR.items() if s["name"].startswith(c)), None)
            if pack and automated_milestone(crew, pack):
                s["milestone"] = automated_milestone(crew, pack)
    now = next((i for i, s in enumerate(stages) if not s["done"]), None)
    for i, s in enumerate(stages):
        s["status"] = "done" if s["done"] else "now" if i == now else "todo"
    return {
        "goal": "launch a rocket",
        "stages": stages,
        "research": {"techs_left": len(path), "next": path[:6], "packs_left": packs,
                     "lab_hours_left": round(secs / 3600 / max(1, labs), 1) if labs else None, "labs": labs,
                     "slowest_pack": ({"pack": slowest[0], "hours": round(slowest[1], 1)} if slowest else None)},
        "science": science,
        "power": power,
        "plates": {"supply_per_min": plate_supply(crew), "workshop_demand_per_min": workshop_demand(crew)},
        "workshop": {"cells": len(crew.cells()), "short": dict(list(crew.cell_short.items())[:8]),
                     "stock": dict(sorted(crew.cell_stock.items(), key=lambda kv: -kv[1])[:12])},
        "next_step": next_step(crew, science, power, path),
    }


def _labs_busy(crew) -> float | None:
    """The share of our labs at work (None: no lab seen yet). More labs only help when this is high: idle labs
    wait for science, and another station would wait too (run 20261119-154451 built four that did nothing)."""
    total = busy = 0
    for s in crew.base.of("labs"):
        n = sum(s.health.values())
        total += n
        busy += s.health.get("working", 0)
    return busy / total if total else None


POWER_RESERVE_KW = 1500                          # spare power the book keeps before building anything else


def burner_to_replace(crew) -> dict | None:
    """The oldest burner mine on ore that electric drills can take over (built first, then the old one removed)."""
    for s in sorted(crew.base.of("burner_mine"), key=lambda s: s.built_minute):
        r = s.params.get("resource")
        if r not in ("iron-ore", "copper-ore", "stone", "coal"):
            continue
        n = max(2, math.ceil(int(s.params.get("count", 1)) * 15 / 18))
        into = "chest" if r in ("coal", "stone") or s.params.get("into") == "chest" else "furnace"
        return {"do": "replace", "site": s.id,
                "with": {"do": "build", "module": "electric_mine", "resource": r, "count": min(10, n), "into": into}}
    return None


def supplier_needed(crew) -> dict | None:
    """An item some input port wants that nothing we built makes: the goal that makes it, at the ports' rate."""
    from .modules import MODULES
    want: dict[str, float] = {}
    for s, pt in crew.item_ports():
        if pt.get("fed"):
            continue
        # plates and coal: a chest feeds one or two ports (and burner coal mines keep their coal), so a port with
        # no free chest left needs more mines
        if (crew._free_outputs(pt["item"], s) if pt["item"] in ("iron-plate", "copper-plate", "coal")
                else crew.outputs(pt["item"])):
            continue
        want[pt["item"]] = want.get(pt["item"], 0.0) + float(pt.get("per_minute") or 5)
    for item, rate in sorted(want.items(), key=lambda kv: -kv[1]):
        if crew._minute() - crew.failed_suppliers.get(item, -1e9) < 15:    # it just failed: not in a loop
            continue
        from .supply import supply_goal              # the same sizing rule as every shortage
        g = supply_goal(crew, item, max(2.0, rate), headroom=1.1)
        if g is not None:
            return g
    return None


ELECTRIC_TECH = "advanced-material-processing-2"


def electric_smelting_step(crew, science: dict | None = None) -> dict | None:
    """Research electric furnaces once blue science is researched and made, unless it is done or on its way."""
    kn = crew.kn
    researched = getattr(kn, "researched", None) or set()
    if ELECTRIC_TECH not in (getattr(kn, "techs", None) or {}) or ELECTRIC_TECH in researched \
            or "chemical-science-pack" not in researched:
        return None
    rs = getattr(crew.w, "research", None) or {}
    if ELECTRIC_TECH in set(rs.get("queue") or []) | {rs.get("current")}:
        return None
    blue = ((science or {}).get("chemical-science-pack") or {}).get("made_per_min")
    if blue is None:
        blue = pack_rate(crew, "chemical-science-pack")
    return {"do": "research", "tech": ELECTRIC_TECH} if blue and blue > 0 else None


def next_step(crew, science: dict, power: dict, path: list[str]) -> dict | None:
    """The rocket book: the one goal that moves the earliest unfinished part of the road (None: let it run)."""
    kn, base = crew.kn, crew.base
    researched = kn.researched
    if not base.of("steam_power") or not base.of("labs"):
        return None                                   # the opening book's job
    # power before anything else: every machine stalls on a short network, so it is fixed ahead of building
    if power["headroom_kw"] < POWER_RESERVE_KW:
        return {"do": "build", "module": "steam_power", "engines": 2}
    # electric furnaces right after blue science (docs/hybrid-plan.md, section 5): the coal-fed smelters are then
    # swapped one by one (assess.electric_swap_alerts) and the coal hauling ends
    amp = electric_smelting_step(crew, science)
    if amp is not None:
        return amp
    # burner drills and stone furnaces out, electric mines in, as soon as the drills are researched: no more
    # refuelling every few minutes, and the burner mine's buildings come back for kits
    if "electric-mining-drill" in researched:
        old = burner_to_replace(crew)
        if old is not None:
            return old
    # plates for the workshop: electric mines once the drills are researched
    if "electric-mining-drill" in researched:
        supply, demand = plate_supply(crew), workshop_demand(crew)
        for item, ore in (("iron-plate", "iron-ore"), ("copper-plate", "copper-ore"), ("coal", "coal"),
                          ("stone", "stone")):
            need = max(demand.get(item, 0.0) * 1.2, {"iron-plate": 60, "copper-plate": 40}.get(item, 0) if base.of("workshop") else 0)
            if need and supply.get(item, 0.0) < need:
                from .supply import supply_goal      # sized to the gap; a full patch gets an outpost
                g = supply_goal(crew, item, need - supply.get(item, 0.0), headroom=1.0)
                if g is not None:
                    return g
    # steel the workshop takes (and the silo's 1000) from a belt-fed column, not furnaces fed by hand
    if "steel-processing" in researched:
        want = sum(c.get("item_in", {}).get("steel-plate", 0.0) for _, c in crew.cells())
        have = plate_supply(crew).get("steel-plate", 0.0) + crew.cell_rate("steel-plate")
        if want > have * 1.1 + 0.5 and crew._minute() - crew.failed_suppliers.get("steel-plate", -1e9) > 15:
            from .supply import supply_goal
            g = supply_goal(crew, "steel-plate", want - have, headroom=1.1)
            if g is not None:
                return g
    # factories feed factories: belt every output to the input ports that want it (science to the stations)
    if crew.linkable():
        return {"do": "link"}
    # the courier is one man: a workshop item made in bulk moves to a belt factory once belts can make it
    from .upgrade import mall_goal, promote_goal
    promote = promote_goal(crew)
    if promote is not None:
        return promote
    # an input port nothing makes yet: build what makes it (a belt factory if belts can, else a workshop)
    supplier = supplier_needed(crew)
    if supplier is not None:
        return supplier
    # the mall: what the crew keeps hand-crafting for its builds gets a small factory of its own
    mall = mall_goal(crew)
    if mall is not None:
        return mall
    # labs for the long research: a research station (its own ground, belt ports), not more hand-fed rows
    labs_busy = _labs_busy(crew)
    if path and base.count("labs") < LABS_TARGET and (labs_busy is None or labs_busy >= 0.5):
        return {"do": "build", "module": "research_station", "count": max(2, min(10, LABS_TARGET - base.count("labs")))}
    # science, then other items with a rate milestone ahead (plastic): each once it is unlocked, at its target
    targets = rate_targets(crew)
    # what to BUILD goes by what was built (planned): a factory that starves needs its inputs, not a twin (the
    # stages and the close-out go by the measured rate)
    rated = {**science, **{it: {"planned_per_min": planned_rate(crew, it), "target": r, "unlocked": it in kn.unlocked}
                           for it, r in targets.items() if it not in science}}
    for pack, info in rated.items():
        built = info.get("planned_per_min", info.get("made_per_min", 0.0))
        if not info["unlocked"] or built >= info["target"] * RATE_DONE:
            continue
        if pack in FLUID_ITEMS and not base.of("pumpjack"):
            if getattr(crew.w, "oil_rings", 0) >= getattr(crew.w, "OIL_MAX_RINGS", 99):
                continue                              # searched as far as it goes: no oil on this map in reach
            return {"do": "build", "module": "pumpjack", "count": 4}
        # red and green come from belt factories (drills, furnaces, belts: real automation); a workshop is
        # courier-fed, so it is only for what belts cannot make yet (fluids: blue, purple, yellow)
        from .modules import MODULES
        module = "factory" if pack in MODULES["factory"].can_make(crew.kn) else "workshop"
        return {"do": "build", "module": module, "item": pack,
                "per_minute": round(info["target"] - built, 1)}
    if "rocket-silo" not in researched:
        nxt = next((t for t in path if t not in researched), None)
        return {"do": "research", "tech": "rocket-silo"} if nxt and not (crew.w.research or {}).get("current") else None
    # the silo: its kit, then the parts' ingredients
    if not base.of("silo"):
        for item, n in SILO_KIT.items():
            if item in ("steel-plate", "pipe"):
                continue
            rate = crew.cell_rate(item)
            if rate < n / 60:                         # the kit within about an hour
                return {"do": "build", "module": "workshop", "item": item, "per_minute": round(n / 60 - rate + 1, 1)}
        stock_ok = all(crew.cell_stock.get(i, 0) + crew.w.inventory.get(i, 0) >= n
                       for i, n in SILO_KIT.items() if i not in ("steel-plate", "pipe"))
        return {"do": "build", "module": "silo"} if stock_ok else {"do": "wait", "seconds": 120}
    for item in ("processing-unit", "low-density-structure", "rocket-fuel"):
        want = 10 * SILO_PARTS_PER_MIN
        if crew.cell_rate(item) < want:
            return {"do": "build", "module": "workshop", "item": item, "per_minute": round(want - crew.cell_rate(item) + 0.5, 1)}
    return {"do": "wait", "seconds": 120}
