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
CLOSEOUT_MAX_GOALS = 8          # one trip back fixes this much at most; the rest is listed for the planner
POWER_HEADROOM = 1.3            # capacity the close-out keeps over what the network draws
COAL_HEADROOM = 1.25            # coal mined over coal burned
SUPPLY_HEADROOM = 1.25          # any other item: made over used (supply.shortfalls)

# trouble that stops the whole base: worth pausing anything for
BASE_WIDE = ("power-capacity", "coal-deficit", "walk")
# the next tier's builds: long, and what the run is for; site-level alerts wait until they finish
FRONTIER_MODULES = {"pumpjack", "workshop", "silo", "factory", "research_station", "outpost", "smelter_column"}
NEUTRAL_KINDS = {"poles", "storage"}           # nothing to sustain
# frontier aging: the plan never waits longer than this behind fixes and the close-out (see next_goal)
PLAN_STARVE_MIN = 20.0
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


def depleted_fix(crew, site) -> dict:
    """A mined-out mine: the same mine again on fresh tiles of its ore first, then the old one comes down; its
    belts are taken up with it and the link step belts the new chests to whatever the old ones fed (run
    20261119-202811: coal mines ran dry under boilers' coal belts and the only fix on offer was to remove them).
    Just removed when it is no mine, or its patches near the base have no room left (an outpost is the move)."""
    gone = {"do": "remove", "site": site.id, "force": True}
    r = site.params.get("resource")
    if site.kind not in MINES or not r:
        return gone
    full = (getattr(crew, "_patch_full", None) or {}).get(r)
    if full is not None and hasattr(crew, "_base_sig") and full[1] == crew._base_sig():
        return gone
    kn = getattr(crew, "kn", None)
    poles = getattr(crew.base, "powered_poles", lambda: [])()
    n = max(2, int(site.params.get("count", 2)))
    if kn is not None and "electric-mining-drill" in (kn.researched or set()) and poles:
        into = site.params.get("into") or ("chest" if r in ("coal", "stone") else "furnace")
        new = {"do": "build", "module": "electric_mine", "resource": r, "count": min(10, n), "into": into}
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
    if p["demand_kw"] * POWER_HEADROOM > p["capacity_kw"]:
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
    for g in headroom_goals(crew) + upgrade_goals(crew) + growth_goals(crew) + ahead_goals(crew):
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
    why = None
    new = better - set(st.get("closeout_machines", []))
    if stage > st["closeout_stage"]:
        why = f"tier {stage} reached: make what stands run on its own before the next one"
    elif belts and not st.get("closeout_belts"):
        why = "logistics researched: coal belts and electric drills replace hand fuelling"
    elif new:
        why = f"{', '.join(sorted(new))} unlocked: faster machines where the old ones stand"
    st["closeout_stage"], st["closeout_belts"] = max(stage, st["closeout_stage"]), belts
    st["closeout_machines"] = sorted(better)
    return why


def is_frontier(goal: dict | None) -> bool:
    return bool(goal) and goal.get("do") == "build" and goal.get("module") in FRONTIER_MODULES \
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
