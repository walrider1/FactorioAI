"""Self-sustaining sites, the close-out before each new tier, and the shield around frontier goals.

How players do it: a block is built, made to run on its own (inputs by belt, fuel by belt or none, power, a
place for its output), checked, and then left alone while they move on to the next tier. They come back on
purpose, a few times a game, to upgrade what the next tier needs (coal belts once logistics is in, electric
drills instead of burners, electric furnaces at blue science, an outpost when a patch runs low), not every
time one furnace hiccups. Hands are fine early and for small amounts of complicated things; what does not
scale is feeding fuel by hand.

    needs(crew, site)     what keeps a site from running on its own, each with the fix (or none: accepted)
    update_seals(crew)    sites that ran on their own for SEAL_MIN minutes are sealed: not removed, replaced
                          or re-routed while they work; their small problems wait for the goal in hand
    closeout(crew)        the planned trip back: every hand dependency the current tier no longer accepts and
                          every broken site, as goals, plus headroom (power and coal) for the next tier
    may_interrupt(...)    a frontier goal (the next tier's build) is paused only for base-wide trouble
"""

from __future__ import annotations

import json
import math

SEAL_MIN = 10.0                 # game minutes a site must run on its own before it is sealed
INTERRUPT_GAP_MIN = 10.0        # at most one pause of a non-frontier goal for site-level trouble this often
CLOSEOUT_MAX_GOALS = 4          # one trip back fixes this much at most; the rest is listed for the planner
POWER_HEADROOM = 1.3            # capacity the close-out keeps over what the network draws
COAL_HEADROOM = 1.25            # coal mined over coal burned
SUPPLY_HEADROOM = 1.25          # any other item: made over used (supply.shortfalls)

# trouble that stops the whole base: worth pausing anything for
BASE_WIDE = ("power-capacity", "coal-deficit", "walk")
# the next tier's builds: long, and what the run is for; site-level alerts wait until they finish
FRONTIER_MODULES = {"pumpjack", "workshop", "silo", "factory", "research_station", "outpost", "smelter_column"}
NEUTRAL_KINDS = {"poles", "storage"}           # nothing to sustain
# frontier aging: the plan never waits longer than this behind fixes and the close-out (see next_goal)
PLAN_STARVE_MIN = 8.0          # progress first: upkeep gets a few minutes, then the plan goes (was 20)
QUICK_FIX_DOS = ("feed", "connect")            # rule-floor fuel/power fixes (seconds each): still before an aged goal
NO_RESET_DOS = ("wait", "research")            # plan goals that take no crew time: they do not end the wait


def coal_belts_ready(kn) -> bool:
    from .modules import coal_belts_ready as ready
    return ready(kn)


def policy(crew) -> dict:
    """What the base may still do by hand at this point of the game (the usual curve):
    fuel by hand until logistics (then coal belts), burner drills until electric drills and logistics (then
    electric rows), a courier-fed workshop always (small amounts of complicated items)."""
    kn = crew.kn
    belts = coal_belts_ready(kn)
    electric = "electric-mining-drill" in (kn.unlocked or set()) or "electric-mining-drill" in (kn.researched or set())
    return {"hand_fuel": not belts, "burner_drills": not (belts and electric), "courier": True}


def _bad(site, *states) -> int:
    return sum(site.health.get(s, 0) for s in states)


def _half(site, n: int) -> bool:
    total = sum(site.health.values())
    return total > 0 and n >= max(1, total // 2)


def needs(crew, site) -> list[dict]:
    """What keeps a site from running on its own: [{kind, why, fix}]. kind: hands (a person must keep doing
    it), broken (it should run but does not), courier (hands on purpose: the workshop)."""
    out: list[dict] = []
    if site.kind in NEUTRAL_KINDS:
        return out
    if site.kind.startswith("unfinished"):
        return [{"kind": "broken", "why": "half-built", "fix": {"do": "remove", "site": site.id, "force": True}}]
    depleted = _bad(site, "no_minable_resources")
    if depleted and _half(site, depleted):
        return [{"kind": "broken", "why": "its ore is used up", "fix": depleted_fix(crew, site)}]
    if site.kind == "burner_mine":
        fix = None
        if "electric-mining-drill" in crew.kn.researched and crew.base.powered_poles():
            r = site.params.get("resource")
            into = "chest" if r in ("coal", "stone") or site.params.get("into") == "chest" else "furnace"
            n = max(2, math.ceil(int(site.params.get("count", 1)) * 15 / 18))
            fix = {"do": "replace", "site": site.id,
                   "with": {"do": "build", "module": "electric_mine", "resource": r, "count": min(10, n), "into": into}}
        out.append({"kind": "hands", "why": "burner drills are fuelled by hand", "fix": fix})
    if site.params.get("fuel") == "hand":
        due = site in (crew.retrofit_due() if hasattr(crew, "retrofit_due") else [])
        out.append({"kind": "hands", "why": "fuelled by hand",
                    "fix": {"do": "link"} if due else None})
    for pt in site.params.get("ports", []):
        if pt.get("item") and not pt.get("fed"):
            out.append({"kind": "broken", "why": f"its {pt['item']} input is not belted", "fix": {"do": "link"}})
    if site.electric and _half(site, _bad(site, "no_power")):
        out.append({"kind": "broken", "why": "no power", "fix": {"do": "connect", "site": site.id}})
    if site.kind == "labs" and not site.params.get("station"):
        out.append({"kind": "hands", "why": "labs fed with packs by hand",
                    "fix": None})                     # a research station replaces them in the roadmap
    if site.params.get("pipe_gaps"):
        out.append({"kind": "broken", "why": f"{len(site.params['pipe_gaps'])} pipe(s) never placed",
                    "fix": {"do": "repair", "site": site.id}})
    if site.params.get("gaps"):                       # gaps.py: refused buildings, holes in belt lines
        from .gaps import pending
        todo = pending(site)
        out.append({"kind": "broken", "why": f"{len(site.params['gaps'])} building(s) never placed"
                    + ("" if todo else " (repairs failed: rebuild it elsewhere)"),
                    "fix": {"do": "repair", "site": site.id} if todo else None})
    if site.kind == "workshop":
        out.append({"kind": "courier", "why": "courier-fed workshop (by design)", "fix": None})
    dry = _bad(site, "no_fuel")
    if dry and not site.params.get("fuel") == "hand":
        out.append({"kind": "broken", "why": f"{dry} burner(s) out of fuel", "fix": None})   # coal alerts say why
    return out


MINES = ("electric_mine", "burner_mine")


def mine_depleted(site) -> bool:
    """Half or more of a mine's drills stand on mined-out ground."""
    drills = sum(1 for e in site.entities if e[0] in ("burner-mining-drill", "electric-mining-drill"))
    n = site.health.get("no_minable_resources", 0)
    return drills > 0 and n >= max(1, math.ceil(drills / 2))


# A mine is rebuilt before it runs dry, not after. Run 20261121-140755: 13 mines were replaced only once half
# their drills stood on bare ground, so the factories they fed starved while the crew gathered the new mine's
# parts (2-13 game minutes each, 21 for one), then relinked every consumer. Now the game's own ore amounts under
# the drills say how long a mine has left; when that is less than a rebuild takes plus margin, the same mine goes
# on fresh ore while the old one still runs, and the old one comes down once the new one stands.
REBUILD_AHEAD_MIN = 45.0         # game minutes of ore left under a mine when its replacement is started
ORE_LOOK_MIN = 20.0              # the foreman looks at a mine's ground again after this long
MINING_REACH = {"electric-mining-drill": 2, "burner-mining-drill": 0}    # tiles around the centre it mines
DRILL_RATE = {"electric-mining-drill": 30.0, "burner-mining-drill": 15.0}  # ore a minute, flat out


def mine_rate(site) -> float:
    """Ore a minute the mine takes out of the ground: its drills, slowed to its furnaces' pace when each drill
    drops into a furnace."""
    furnace = 37.5 if any(e[0] == "steel-furnace" for e in site.entities) else 18.75
    into_furnace = site.kind == "electric_mine" and site.params.get("into") in ("furnace", "line")
    rate = 0.0
    for e in site.entities:
        r = DRILL_RATE.get(e[0])
        if r:
            rate += min(r, furnace) if into_furnace else r
    return rate


def mine_ore_left(w, site) -> int | None:
    """Ore left under the mine's drills (each tile counted once), from the game's amounts at the last look;
    None when the ground under it was never measured."""
    amounts = getattr(w, "ore_amount", None)
    r = site.params.get("resource")
    if not amounts or not r:
        return None
    ore = (getattr(w, "resources", {}) or {}).get(r, set())
    tiles: set = set()
    for e in site.entities:
        reach = MINING_REACH.get(e[0])
        if reach is None:
            continue
        cx, cy = math.floor(e[1]), math.floor(e[2])
        if e[0] == "burner-mining-drill":          # 2x2: its centre is a tile corner
            tiles |= {(cx - 1 + dx, cy - 1 + dy) for dx in (0, 1) for dy in (0, 1)}
        else:
            tiles |= {(cx + dx, cy + dy) for dx in range(-reach, reach + 1) for dy in range(-reach, reach + 1)}
    known = [amounts[t] for t in tiles if t in amounts and t in ore]
    if not known and not any(t in ore for t in tiles):
        return 0 if tiles else None
    return sum(known) if known else None


def mine_runway(crew, site) -> float | None:
    """Game minutes of ore the mine has left (None: unknown). The amounts are as of the foreman's last look at
    it, so what the drills took since then comes off."""
    left = mine_ore_left(crew.w, site)
    rate = mine_rate(site)
    if left is None or rate <= 0:
        return None
    looked = site.params.get("ore_look")
    since = max(0.0, crew._minute() - looked) if looked is not None and hasattr(crew, "_minute") else 0.0
    return max(0.0, left / rate - since)


def rebuild_due(crew, site) -> str | None:
    """Why this mine should be rebuilt now while it still runs (None: not yet). One drill already on bare
    ground counts too: drills of one row run dry one by one, and the rest follow within the hour."""
    if site.kind not in MINES or mine_depleted(site) or not site.params.get("resource"):
        return None
    drills = sum(1 for e in site.entities if e[0] in DRILL_RATE)
    dry = site.health.get("no_minable_resources", 0)
    runway = mine_runway(crew, site)
    if runway is not None and runway < REBUILD_AHEAD_MIN:
        return f"about {runway:.0f} game minutes of ore left under its drills"
    if dry and drills >= 2 and dry >= max(1, math.ceil(drills / 3)):
        # a third of a big row on bare ground, not its first drill: the end drills of a row on a patch edge run
        # dry early while the rest mine for hours (the ore runway above catches the row as a whole)
        return f"{dry} of its {drills} drills already on mined-out ground"
    return None


SMELTS_TO = {"iron-ore": "iron-plate", "copper-ore": "copper-plate", "stone": "stone-brick"}
NEED_HEADROOM = {"coal": 1.5}    # the other mines must give this many times what the base uses (default 1.25)


def mine_product(site) -> str | None:
    r = site.params.get("resource")
    if site.kind == "electric_mine" and site.params.get("into") in ("furnace", "line") or \
            site.kind == "burner_mine" and site.params.get("into", "furnace" if r not in ("coal", "stone") else "chest") == "furnace":
        return SMELTS_TO.get(r, r)
    return r


def still_needed(crew, site) -> bool:
    """Would the base run short of what this mine makes without it? The other mines of the same product (not
    running dry themselves) against what the base really uses (game statistics). A mine nobody needs is not
    rebuilt when its ore runs out: it comes down and its belts are laid again from the mines that stand (a relink
    takes a minute or two, a rebuild 10-30). Run 20261121-140755: half the game time from minute 600 went into
    rebuilding 1-2 drill mines while 81% of all drills stood idle. Unknown use (no statistics): needed."""
    items = (getattr(crew.w, "stats", None) or {}).get("items") or {}
    product = mine_product(site)
    if not items or not product:
        return True
    used = float((items.get(product) or {}).get("used_per_min") or 0)
    others = 0.0
    for s in crew.base.sites:
        if s is site or s.kind not in MINES or mine_product(s) != product or mine_depleted(s):
            continue
        runway = mine_runway(crew, s)
        if runway is not None and runway < REBUILD_AHEAD_MIN:
            continue
        others += mine_rate(s) * (0.5 if product == "stone-brick" else 1.0)
    return others < used * NEED_HEADROOM.get(product, SUPPLY_HEADROOM)


def depleted_fix(crew, site) -> dict:
    """A mined-out mine: the same mine again on fresh tiles of its ore first, then the old one comes down; its
    belts are taken up with it and the link step belts the new chests to whatever the old ones fed (run
    20261119-202811: coal mines ran dry under boilers' coal belts and the only fix on offer was to remove them).
    Just removed when it is no mine, when the other mines give enough without it (still_needed), or when its
    patches near the base have no room left (an outpost is the move)."""
    gone = {"do": "remove", "site": site.id, "force": True}
    r = site.params.get("resource")
    if site.kind not in MINES or not r:
        return gone
    try:
        if not still_needed(crew, site):
            return gone
    except Exception:  # noqa: BLE001 - stub crews and worlds in tests: rebuild as before
        pass
    full = (getattr(crew, "_patch_full", None) or {}).get(r)
    if full is not None and hasattr(crew, "_base_sig") and full[1] == crew._base_sig():
        return gone
    kn = getattr(crew, "kn", None)
    poles = getattr(crew.base, "powered_poles", lambda: [])()
    n = max(2, int(site.params.get("count", 2)))
    if kn is not None and "electric-mining-drill" in (kn.researched or set()) and poles:
        into = site.params.get("into") or ("chest" if r in ("coal", "stone") else "furnace")
        from .supply import least_units
        least = least_units("electric_mine", r, into)
        # its rebuild is a bigger row: it lasts longer, and its plates feed the old belts (spliced in)
        new = {"do": "build", "module": "electric_mine", "resource": r, "count": min(12, max(n, least)),
               "into": "furnace" if into == "line" else into}
    else:
        new = {"do": "build", "module": "burner_mine", "resource": r, "count": min(10, n)}
    return {"do": "replace", "site": site.id, "with": new, "force": True}


def accepted(crew, need: dict, pol: dict | None = None) -> bool:
    """Is this need fine at this point of the game?"""
    pol = pol or policy(crew)
    if need["kind"] == "courier":
        return pol["courier"]
    if need["kind"] == "hands":
        if "burner" in need["why"]:
            return pol["burner_drills"]
        if "fuelled by hand" in need["why"]:
            return pol["hand_fuel"]
        return True                                   # labs by hand: the roadmap brings research stations
    return False


def update_seals(crew) -> None:
    """Seal sites that ran on their own for SEAL_MIN minutes; unseal one that needs hands again, or that nobody
    needs any more (idle: its output sat untouched for a long time)."""
    from .upgrade import is_idle, retire_promoted, update_idle
    update_idle(crew)
    for note in retire_promoted(crew):                # a belt factory took over a workshop cell's item
        if hasattr(crew, "foreman"):
            crew.foreman.note(note)
    now = crew._minute()
    healthy = crew.__dict__.setdefault("healthy_since", {})
    sealed = crew.__dict__.setdefault("sealed", {})
    pol = policy(crew)
    for s in crew.base.sites:
        ns = needs(crew, s)
        hands = [n for n in ns if n["kind"] == "hands" or (n["kind"] == "courier" and not pol["courier"])]
        broken = [n for n in ns if n["kind"] == "broken"]
        if s.kind in NEUTRAL_KINDS or s.kind.startswith("unfinished") or s.kind == "workshop":
            continue
        if hands:
            healthy.pop(s.id, None)
            sealed.pop(s.id, None)
            continue
        if broken:
            healthy.pop(s.id, None)                   # a sealed site stays sealed while it is repaired
            continue
        if is_idle(crew, s.id):
            sealed.pop(s.id, None)
            continue
        healthy.setdefault(s.id, now)
        if s.id not in sealed and now - healthy[s.id] >= SEAL_MIN:
            sealed[s.id] = now
    for sid in list(sealed):
        if crew.base.get(sid) is None:
            sealed.pop(sid, None)
            healthy.pop(sid, None)


def is_sealed(crew, site_id) -> bool:
    return site_id in crew.__dict__.get("sealed", {})


def status(crew) -> dict:
    """For the planner and the monitor: what is sealed, and what still needs hands or repair (and its fix)."""
    pol = policy(crew)
    out = {"sealed": sorted(crew.__dict__.get("sealed", {})), "needs": [], "accepted": [], "policy": pol}
    for s in crew.base.sites:
        for n in needs(crew, s):
            row = {"site": s.id, "kind_of_site": s.kind, "need": n["kind"], "why": n["why"], "fix": n["fix"]}
            (out["accepted"] if accepted(crew, n, pol) else out["needs"]).append(row)
    out["needs"] = out["needs"][:16]
    out["accepted"] = out["accepted"][:10]
    from .upgrade import idle_sites, mall_goal, promote_goal, upgradable_sites
    out["idle"] = idle_sites(crew)[:8]
    out["upgradable"] = [s.id for s in upgradable_sites(crew)][:8]
    out["next_growth"] = [g for g in (promote_goal(crew), mall_goal(crew)) if g]
    return out


def headroom_goals(crew) -> list[dict]:
    """Capacity for the next tier: power over what the network draws (power_status), coal mined over burned,
    and every other item made over used, each with headroom."""
    goals = []
    p = crew.power_status()
    if max(p["demand_kw"], p.get("expected_kw", 0)) * POWER_HEADROOM > p["capacity_kw"]:
        goals.append({"do": "build", "module": "steam_power", "engines": 2})
    try:
        b = crew.coal_budget()
        if crew.coal_mines() and b["burned_per_min"] > 0 and b["mined_per_min"] < b["burned_per_min"] * COAL_HEADROOM:
            from .assess import coal_fix
            fix = coal_fix(crew, b["burned_per_min"] * COAL_HEADROOM - b["mined_per_min"])
            if fix:
                goals.append(fix)
    except Exception:  # noqa: BLE001 - stub crews in tests
        pass
    # every other item the base uses faster than it makes (with headroom): plates, stone, steel, gears,
    # circuits, later oil products... one rule, each build sized to its own measured gap (run 20261120-023103:
    # iron at 150/min against 220/min used for an hour, and nothing grew it)
    try:
        from .supply import shortfalls, supply_goal
        for item, gap in shortfalls(crew, headroom=SUPPLY_HEADROOM):
            if item == "coal":
                continue                              # the coal budget above (it counts the chests too)
            g = supply_goal(crew, item, gap, headroom=SUPPLY_HEADROOM)
            if g and g not in goals:
                goals.append(g)
    except Exception:  # noqa: BLE001 - stub crews in tests
        pass
    return goals


def upgrade_goals(crew, n: int = 2) -> list[dict]:
    """Faster machines in place for the sites that wait on their machines: plate rows first (plates are what
    everything waits on), then the rest."""
    from .upgrade import upgradable_sites
    sites = upgradable_sites(crew)
    sites.sort(key=lambda s: s.kind not in ("electric_mine", "smelter_column"))
    return [{"do": "upgrade", "site": s.id} for s in sites[:n]]


def growth_goals(crew) -> list[dict]:
    """The base outgrowing hands: a busy workshop item moved to a belt factory, and the building the crew keeps
    hand-crafting made by a small factory of its own (one of each per close-out)."""
    from .upgrade import mall_goal, promote_goal
    return [g for g in (promote_goal(crew), mall_goal(crew)) if g]


def better_machines(kn) -> set:
    from .upgrade import SUCCESSOR
    unlocked = getattr(kn, "unlocked", set()) or set()
    return {m for m in SUCCESSOR.values() if m in unlocked}


def ahead_goals(crew) -> list[dict]:
    """Think ahead: the one ore that runs out first (or has no room) gets its next patch prepared now."""
    try:
        from .outposts import outlook
        look = outlook(crew)
    except Exception:  # noqa: BLE001 - stub crews in tests
        return []
    due = [(o["minutes_left"] if o["minutes_left"] is not None else 0, o["fix"]) for o in look.values() if o.get("fix")]
    return [min(due, key=lambda d: d[0])[1]] if due else []


def prerequisite_goals(crew) -> list[dict]:
    """What later stages will wait on, started as soon as it can be made. Engine units: every pump needs one (so oil
    and water reach a floor from any distance) and blue science eats them. Run 20261121-140755: nothing made
    engine units, so a 441-tile crude line stood without pumps and the plastics workshop never got crude."""
    kn = crew.kn
    if "engine" not in (getattr(kn, "researched", None) or set()):
        return []
    made = any(s.params.get("item") == "engine-unit" for s in crew.base.sites)
    try:
        made = made or any("engine-unit" in (c.get("item_out") or {}) for _, c in crew.cells())
    except Exception:  # noqa: BLE001 - stub crews in tests
        pass
    out = [] if made else [{"do": "build", "module": "factory", "item": "engine-unit", "per_minute": 2}]
    return out + field_goals(crew)


FIELD_ORES = ("iron-ore", "copper-ore")


def field_goals(crew) -> list[dict]:
    """Plates for purple and yellow science before their factories: once blue science is researched, a clean
    iron patch and a clean copper patch are set aside and mined densely, smelted beside the ore, their plates
    belted out (outposts.plan_field). The factories that come next are placed where those lines arrive, so they
    start fed instead of starving and being relinked (run 20261121-140755: green science and the steel column
    were built first and fed from whatever mine rows there were, then repaired for hours)."""
    kn = crew.kn
    done = getattr(kn, "researched", None) or set()
    if "chemical-science-pack" not in done or "electric-mining-drill" not in done:
        return []
    if not getattr(crew.base, "powered_poles", lambda: [])():
        return []
    out = []
    try:
        from .outposts import plan_field
    except Exception:  # noqa: BLE001
        return []
    for r in FIELD_ORES:
        if any(s.kind == "outpost" and s.params.get("field") and not s.params.get("near_base")
               and s.params.get("resource") == r for s in crew.base.sites):
            continue
        try:
            g = plan_field(crew, r)
        except Exception:  # noqa: BLE001 - stub crews in tests, a world without patches
            g = None
        out.append(g or {"do": "scout", "resource": r})
    return out


def closeout(crew, why: str) -> list[dict]:
    """The planned trip back before the next tier: the fixes for what this tier no longer accepts and what is
    broken, then headroom. Goals carry a reflex tag (stripped before they run) so the log says why."""
    pol = policy(crew)
    goals, seen = [], set()
    rows = []
    for s in crew.base.sites:
        for n in needs(crew, s):
            if not accepted(crew, n, pol) and n["fix"]:
                rows.append((0 if n["kind"] == "broken" else 1, n["fix"]))
    for _, fix in sorted(rows, key=lambda r: r[0]):
        key = json.dumps(fix, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        goals.append(fix)
    # progress first: what the next tier needs (prerequisites) before headroom; faster machines and growth are the
    # planners' call, not upkeep's (run 20261121-140755: a close-out upgrade spent 10 minutes on bricks and failed)
    for g in prerequisite_goals(crew) + headroom_goals(crew) + ahead_goals(crew):
        key = json.dumps(g, sort_keys=True)
        if key not in seen:
            seen.add(key)
            goals.append(g)
    return [{**g, "reflex": f"close-out: {why}"[:160]} for g in goals[:CLOSEOUT_MAX_GOALS]]


def stage_index(crew) -> int:
    """How far along the road the base is: the first stage not done (roadmap)."""
    from .roadmap import roadmap
    try:
        stages = roadmap(crew)["stages"]
    except Exception:  # noqa: BLE001 - a roadmap bug never blocks the crew
        return crew.__dict__.get("closeout_stage", 0)
    return next((i for i, s in enumerate(stages) if not s["done"]), len(stages))


def closeout_due(crew) -> str | None:
    """A tier was just finished, or logistics just came in: time for the planned trip back."""
    st = crew.__dict__
    stage, belts, better = stage_index(crew), coal_belts_ready(crew.kn), better_machines(crew.kn)
    if "closeout_stage" not in st:                    # the first look only takes note of where the run is
        st["closeout_stage"], st["closeout_belts"], st["closeout_machines"] = stage, belts, sorted(better)
        return None
    why = None                                        # a faster machine unlocked is no reason to stop: upgrades
    if stage > st["closeout_stage"]:                  # are the planners' call (progress first)
        why = f"tier {stage} reached: make what stands run on its own before the next one"
    elif belts and not st.get("closeout_belts"):
        why = "logistics researched: coal belts and electric drills replace hand fuelling"
    st["closeout_stage"], st["closeout_belts"] = max(stage, st["closeout_stage"]), belts
    st["closeout_machines"] = sorted(better)
    return why


def is_frontier(goal: dict | None) -> bool:
    return bool(goal) and goal.get("do") == "build" and goal.get("module") in FRONTIER_MODULES \
        and not goal.get("near_base") \
        and not str(goal.get("reflex", "")).startswith(("rules:", "assessor:", "close-out:"))


def may_interrupt(crew, alert_keys) -> bool:
    """May the goal in hand be paused for these alerts? Base-wide trouble: yes. A frontier goal: no (site
    problems wait for it; their fixes run right after it). Anything else: at most once every
    INTERRUPT_GAP_MIN minutes, so upkeep never eats the whole hour."""
    cur = crew.current or {}
    if not cur or cur.get("urgent"):
        return True
    if any(str(k).startswith(BASE_WIDE) for k in alert_keys):
        return True
    if cur.get("aged") or is_frontier(cur.get("goal")):
        return False
    last = crew.__dict__.get("last_interrupt", -1e9)
    return crew._minute() - last >= INTERRUPT_GAP_MIN


def plan_waited_min(crew, now: float) -> float:
    """Game minutes the plan's next goal has waited behind fixes and the close-out (0: not waiting)."""
    since = getattr(crew, "plan_starved_since", None)
    return 0.0 if since is None else max(0.0, (now - since) / 3600)


def next_goal(crew, goals: list[dict], now: float) -> tuple[dict, str, float]:
    """Which goal runs next, as (goal, source, minutes the plan had waited). Sources: "urgent" (the rule floor's,
    the assessor's and the overseer's fixes), "closeout", "plan", "aged", "idle".

    Urgent fixes go first, then the close-out, then the plan. Frontier aging: the plan never waits more than
    PLAN_STARVE_MIN game minutes. Its clock starts when something else is picked while the plan has real work
    queued, and stops when a plan goal that takes crew time starts. Past the limit the plan's next goal goes
    first ("aged"). Only the rule floor's quick fuel and power fixes still go ahead of it. Leading waits are
    skipped, since a wait is pointless with work queued. Run 20261120-180703: the green-science build waited
    170 minutes at queue position 5. New fixes from the rules, the assessor and the overseer kept arriving
    ahead of it. It never ran."""
    real = any(g.get("do") != "wait" for g in goals)
    if not real:
        crew.plan_starved_since = None
    waited = plan_waited_min(crew, now)
    if crew.urgent or crew.closeout_queue:
        if real and getattr(crew, "plan_starved_since", None) is None:
            crew.plan_starved_since = now
        if real and waited >= PLAN_STARVE_MIN:
            quick = next((i for i, g in enumerate(crew.urgent) if str(g.get("reflex", "")).startswith("rules:")
                          and g.get("do") in QUICK_FIX_DOS), None)
            if quick is not None:
                return crew.urgent.pop(quick), "urgent", waited
            while goals[0].get("do") == "wait":
                goals.pop(0)
            goal = goals.pop(0)
            if goal.get("do") not in NO_RESET_DOS:
                crew.plan_starved_since = None
            return goal, "aged", waited
        if crew.urgent:
            return crew.urgent.pop(0), "urgent", waited
        return crew.closeout_queue.pop(0), "closeout", waited
    if goals:
        goal = goals.pop(0)
        if goal.get("do") not in NO_RESET_DOS:
            crew.plan_starved_since = None
        return goal, "plan", waited
    return {"do": "wait", "seconds": 15}, "idle", waited
