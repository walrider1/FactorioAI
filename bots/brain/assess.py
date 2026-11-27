"""The supervisor: watches the base all the time and amends the plan the moment something goes wrong.

The strategist plans the next steps toward the rocket; goals can run for many minutes (a factory build took 19).
Without a supervisor a problem that starts mid-goal (coal running short, a power station out of fuel, research
stuck, a mine starved) waits until the goal ends, and a run is a race. So, at every checkpoint the crew passes
(each foreman patrol, every few seconds of mining, crafting or waiting):

    detect(crew)      cheap rules over the whole base: any item running short (game statistics: made vs used
                      per minute, and the stock), power, buildings out of fuel or input, output stuck, research
                      that has not moved, a goal running long. Each alert says what is wrong and the goal that
                      fixes it, when there is an obvious one.
    RuleAssessor      the floor that always runs: fuel and power fixes go to the front of the queue and pause the
                      current goal (keeping everything running is never left to a model's mood or latency).
    LLMAssessor       a fast model sees a one-page situation every ~1.5 game minutes or as soon as a new alert
                      appears, and decides: amend now (urgent goals, interrupt, foreman priority), ask the
                      strategist to replan, or carry on. It runs in the background; the crew never waits for it.
    Supervisor        runs the two, applies their decisions to the crew (crew.urgent, crew.preempt,
                      foreman.priority) and tells the main loop when the strategist should replan.

A paused goal resumes after the urgent ones. Goals are only paused at safe points: never while buildings are
being placed, removed or wired.
"""

from __future__ import annotations

import json
import math
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from .belts import LINK_REACH

FUELS = ("coal", "wood")
CRITICAL = ("coal", "boiler", "power", "fuel", "pipe", "belt-foreign", "gap-", "labs-feed", "labs-topup")   # keys the rule floor fixes itself
SOON = ("rebuild-", "swap-", "ore-soon-", "trunk-", "modules-", "retire-", "headroom-", "scout-")  # keys it queues by itself without pausing the goal in hand (planned)
FAR_LINK = "a link reaches"     # in a link failure: only the distance stopped it (Crew._link_port), a trunk fixes it
TRUNK_AUTO_MAX = 600            # Manhattan tiles: a farther source is better made next to the site than belted over
GOAL_DOS = ("build", "research", "stock", "remove", "replace", "connect", "feed", "priority", "wait",
            "link", "link_science", "scout", "upgrade", "repair", "repipe", "relink", "purge", "trunk", "modules",
            "act")
LONG_GOAL_MIN = 8.0


# ------------------------------------------------------------------ detection
def flows(crew) -> list[dict]:
    """Every item the base makes or uses: per minute (game statistics) and the stock on hand and in chests."""
    items = (crew.w.stats or {}).get("items") or {}
    out = []
    for name, st in items.items():
        made, used = float(st.get("made_per_min") or 0), float(st.get("used_per_min") or 0)
        if made <= 0 and used <= 0:
            continue
        stock = crew.w.inventory.get(name, 0) + (sum(crew.foreman.boiler_coal.values()) if name == "coal" else 0)
        net = made - used
        out.append({"item": name, "made": round(made, 1), "used": round(used, 1), "net": round(net, 1),
                    "stock": stock, "minutes_left": round(stock / -net, 1) if net < 0 else None})
    return sorted(out, key=lambda f: f["net"])


def electric_coal(crew) -> bool:
    """Can coal come from electric drills (no fuel to carry to them) yet?"""
    try:
        return bool(crew.base.powered_poles()) and "electric-mining-drill" in crew.kn.unlocked
    except Exception:  # noqa: BLE001 - stub crews in tests
        return False


def coal_fix(crew, short: float) -> dict | None:
    """More coal, sized to the gap: the same rule as any item (supply.supply_goal): electric drills into chests
    once there is power, burner drills before; on a full patch burners become electric drills, else an outpost.
    A gap of 0 (the coal patch is just full) still asks for a minimal build."""
    from .supply import supply_goal
    return supply_goal(crew, "coal", max(short, 1.0))


def expand_fix(crew, resource: str) -> dict | None:
    """The patch near the base is full: an outpost on the next patch known, else a scout for one (None when
    outposts are not possible yet: no power or no electric drills)."""
    if not electric_coal(crew) or not hasattr(crew, "w"):
        return None
    try:
        from .outposts import spare_patches
        spare = spare_patches(crew.w, crew.base, resource)
    except Exception:  # noqa: BLE001 - stub crews in tests
        return None
    if spare:
        return {"do": "build", "module": "outpost", "resource": resource, "count": 16 if resource in ("iron-ore", "copper-ore") else 8}
    return {"do": "scout", "resource": resource}


def ore_alerts(crew) -> list[dict]:
    """Thinking ahead: an ore whose worked patches run low or have no room left, with the next move. A warning:
    nothing pauses for it, but the planner sees it before the drills stop."""
    try:
        from .outposts import outlook
        look = outlook(crew)
    except Exception:  # noqa: BLE001 - stub crews in tests
        return []
    out = []
    for r, o in look.items():
        if not o.get("fix"):
            continue
        why = ("no room for more drills on the patches we work" if not o["room_for_more"] else
               f"about {o['minutes_left']} game minutes of ore left where we mine it")
        nxt = o.get("next_patch")
        what = f"{r}: {why}; " + (f"the next patch is {nxt['distance']} tiles out: an outpost mines it and belts it home"
                                  if nxt else "no other patch known: scout for one before the drills stop")
        soon = _ore_soon(crew, r, o)
        out.append({"level": "urgent" if soon else "warn", "key": f"ore-soon-{r}" if soon else f"ore-{r}",
                    "what": what + (f" ({soon}: queued now, the base must not wait for it)" if soon else ""),
                    "fix": o["fix"]})
    return out


ORE_SOON_MIN = 150.0            # game minutes of iron or copper left where we mine it: the next patch is built now


def _ore_soon(crew, resource: str, o: dict) -> str | None:
    """Why the next patch of a plate ore is built now by the rule floor, not left to the planners: its worked
    patches run out within ORE_SOON_MIN, or they are full while the plate is short. Run 20261124-015025: iron near
    the base ran dry from minute 330, the outpost came only after burner mines and hand feeding, 20 and 33 minutes
    late, and iron stood at 38/min against 207 wanted."""
    if resource not in ("iron-ore", "copper-ore") or not electric_coal(crew):
        return None
    left = o.get("minutes_left")
    if left is not None and left < ORE_SOON_MIN:
        return f"about {left} game minutes of ore left"
    if not o.get("room_for_more"):
        if _stranded(crew, resource):
            return None                               # mined but not delivered: links, not a far patch
        from .overseer import short_of
        why = short_of(crew, "iron-plate" if resource == "iron-ore" else "copper-plate")
        if why:
            return f"the patches near the base are full and {why}"
    return None


STRANDED_SHARE = 0.3


def _stranded(crew, resource: str) -> bool:
    """Do the mines of this ore stand with nowhere to put a good share of what they mine? Then the plate is short
    for want of belts, not of ore. Run 20261124-120854: three copper mines stood full while the green science
    factory got 18.8 of its 24.8 copper a minute; a far outpost would not have fed it."""
    n = full = 0
    for s in crew.base.sites:
        if s.kind in ("electric_mine", "burner_mine") and s.params.get("resource") == resource:
            h = getattr(s, "health", None) or {}
            n += sum(h.values())
            full += h.get("waiting_for_space_in_destination", 0) + h.get("full_output", 0)
    return n > 0 and full >= STRANDED_SHARE * n


def supply_fix(crew, item: str, short: float) -> dict | None:
    """The build that covers a shortage of any item, sized to the gap with headroom, in the best module the
    technology allows (supply.supply_goal): one rule for iron, coal, steel, gears or anything later."""
    from .supply import supply_goal
    return supply_goal(crew, item, short)


FURNACES = ("stone-furnace", "steel-furnace")


def coal_belts_ready(kn) -> bool:
    from .modules import coal_belts_ready as ready
    return ready(kn)


def coal_chain_alerts(crew) -> list[dict]:
    """Coal belts shared by more ports than their chest feeds: the cause behind boilers and rows running dry."""
    out = []
    for c in (crew.coal_chains() if hasattr(crew, "coal_chains") else []):
        what = (f"the coal belt from the chest at {c['chest']} feeds sites {c['feeds_sites']}, which burn "
                f"{c['burns_per_min']} coal/min, but its drills give {c['gets_per_min']}/min: they run dry in turn")
        if c["spare_coal_chests"] and c["boost_may_retry"]:
            fix = {"do": "link"}
            what += f"; {c['spare_coal_chests']} coal chest(s) feed nothing yet: the link step belts one into it"
        elif c["spare_coal_chests"]:
            fix = None
            what += "; a spare coal chest could not be belted into it (it retries once a site is built or removed)"
        else:
            fix = coal_fix(crew, c["burns_per_min"] - c["gets_per_min"])
            what += ("; more coal drills, then the link step belts them into it" if fix else
                     "; the coal patches near the base are full and no burner coal mine is left to swap")
        out.append({"level": "urgent", "key": f"coal-chain-{c['chest'][0]},{c['chest'][1]}", "what": what,
                    "fix": fix})
    return out


def input_chain_alerts(crew) -> list[dict]:
    """Belts that bring a factory less than its ports take (plates, circuits...): one source feeds more than it
    makes. The fix is another source fed into that belt (the link step), or more of the item made."""
    out = []
    for c in (crew.short_chains() if hasattr(crew, "short_chains") else []):
        if c["item"] == "coal":
            continue                                  # coal_chain_alerts
        what = (f"the {c['item']} belt from {c['chest']} feeds sites {c['feeds_sites']}, which take "
                f"{c['burns_per_min']}/min, but its source gives {c['gets_per_min']}/min: their machines wait on it")
        if c["spare_sources"] and c["boost_may_retry"]:
            fix = {"do": "link"}
            what += f"; {c['spare_sources']} unused {c['item']} source(s): the link step feeds one into that belt"
        elif c["spare_sources"]:
            fix = None
            what += "; a spare source could not be belted in (retried once a site is built or removed)"
        else:
            fix = supply_fix(crew, c["item"], c["burns_per_min"] - c["gets_per_min"])
            what += "; more of it made, then the link step feeds the new source in"
        out.append({"level": "warn", "key": f"input-chain-{c['chest'][0]},{c['chest'][1]}", "what": what,
                    "fix": fix})
    return out


RELOCATE_SETTLE_MIN = 30.0      # game minutes a factory stands before it may be moved next to its plates


def failed_before_logistics(crew, q: dict) -> bool:
    """Did this stuck link fail only for want of underground belts, which are researched now?"""
    unlocked = getattr(getattr(crew, "kn", None), "unlocked", None) or set()
    return "research logistics" in str(q.get("why") or "") and "underground-belt" in unlocked


def _strands(crew, q: dict) -> bool:
    """Is at least what this port wants of its item made and left in chests nobody links (nodes.stranded)? Only
    for a factory or research station, which the relocate builds next to its sources."""
    site = crew.base.get(q.get("into_site")) if q.get("into_site") is not None else None
    if site is None or site.kind not in ("factory", "labs"):
        return False
    try:
        if crew._minute() - float(site.built_minute or 0) < RELOCATE_SETTLE_MIN:
            return False                              # just built (or moved): its links get their own tries
    except Exception:  # noqa: BLE001 - stub crews in tests
        pass
    try:
        from .nodes import stranded
        # only plates on a line (one belt a link runs on from): boxed-in chests stay out of reach wherever the
        # factory stands. Run 20261125-164938: red science moved 33 -> 39 -> 43 -> again next to such chests
        lined = {s.id for s in crew.base.sites if s.params.get("line")}
        lost = sum(r["per_minute"] for r in stranded(crew) if r["item"] == q.get("item") and r["site"] in lined)
    except Exception:  # noqa: BLE001 - stub crews in tests
        return False
    want = sum(float(pt.get("per_minute") or 0) for pt in site.params.get("ports", [])
               if pt.get("item") == q.get("item") and not pt.get("fed"))
    return lost >= max(5.0, want)


def relocate_fix(crew, q: dict) -> dict | None:
    """A consumer whose inputs cannot reach it (walled in): the same build again, which goes on open ground
    near its inputs (research stations and steel columns look for their sources), then the old one comes down.
    Sealed sites are not moved this way."""
    site = crew.base.get(q.get("into_site")) if q.get("into_site") is not None else None
    if site is None or (site.id in getattr(crew, "sealed", {}) and q.get("failed", 0) < 3):
        return None                                   # a sealed site moves only once its inputs failed 3x
    if q.get("at") and q["at"][0] == "boost":
        # extra supply into a chain that has no side to join: lay the site's input again from a spare source
        return {"do": "relink", "site": site.id, "item": q.get("item")}
    if site.kind == "labs" and site.params.get("station"):
        new = {"do": "build", "module": "research_station", "count": int(site.params.get("count") or 2)}
    elif site.kind == "factory" and site.params.get("item"):
        new = {"do": "build", "module": "factory", "item": site.params["item"],
               "per_minute": float(site.params.get("per_minute") or 5)}
    elif site.kind == "smelter_column" and not site.params.get("outpost"):
        new = {"do": "build", "module": "smelter_column", "resource": site.params.get("resource"),
               "count": int(site.params.get("count") or 2)}
    else:
        return None
    return {"do": "replace", "site": site.id, "with": new}


def pipe_alerts(crew) -> list[dict]:
    """A pipe the game refused leaves the fluid stuck behind it (a crude line with one gap feeds nothing), and
    pumpjacks with nowhere to send their crude stop: both say where and what fixes it."""
    out = []
    for s in crew.base.sites:
        gaps = s.params.get("pipe_gaps") or []
        if gaps:
            fluids = sorted({g[0] for g in gaps})
            out.append({"level": "urgent", "key": f"pipe-gap-{s.id}",
                        "what": f"site {s.id} ({s.kind}): {len(gaps)} pipe(s) of its {', '.join(fluids)} line were "
                                f"never placed (first at {tuple(gaps[0][2])}): nothing flows past the gap",
                        "fix": {"do": "repair", "site": s.id}})
    from .modules import MODULES
    ws = MODULES.get("workshop")
    for s in crew.base.of("workshop"):
        try:
            bad = [b for b in ws.misports(crew.w, s) if b["fluid"]]
        except Exception:  # noqa: BLE001 - stub crews and worlds in tests
            bad = []
        if bad:
            b = bad[0]
            out.append({"level": "urgent", "key": f"pipe-ports-{s.id}",
                        "what": f"site {s.id} (workshop): {len(bad)} pipe(s) join the wrong connection of their machine "
                                f"(its {b['machine']} takes {b['fluid']} at {b['target']}, its pipe is on another one): "
                                f"no {b['fluid']} flows there until it is piped again",
                        "fix": {"do": "repipe", "site": s.id}})
    for s in crew.base.of("pumpjack"):
        n = sum(s.health.values())
        full = s.health.get("waiting_for_space_in_destination", 0) + s.health.get("full_output", 0)
        if n and full >= max(1, n // 2) and not any(o.params.get("pipe_gaps") for o in crew.base.sites):
            out.append({"level": "warn", "key": f"crude-stuck-{s.id}",
                        "what": f"pumpjack site {s.id}: {full}/{n} pumpjacks full: their crude reaches no refinery "
                                "(no refinery yet, or its line is broken somewhere)", "fix": None})
    return out


def station_alerts(crew) -> list[dict]:
    """A research station that gets some colours but not every one the current research needs does nothing."""
    out = []
    rs = getattr(getattr(crew, "w", None), "research", None) or {}
    cur = rs.get("current")
    if not cur or not hasattr(crew.kn, "research_cost"):
        return out
    need = set(crew.kn.research_cost(cur))
    for s in crew.base.of("labs"):
        if not s.params.get("station"):
            continue
        has = {pt.get("pack") for pt in s.params.get("ports", []) if pt.get("pack")}
        if has and need - has:
            out.append({"level": "warn", "key": f"station-short-{s.id}",
                        "what": f"research station site {s.id} gets {sorted(has)} but {cur} needs {sorted(need)}: "
                                f"its labs wait for {sorted(need - has)}", "fix": {"do": "link"}})
    return out


def retire_alerts(crew) -> list[dict]:
    """Once a research station gets every pack the research in hand needs by belt, the first hand-fed labs come
    down, and so does a second station that still misses a colour after RETIRE_SETTLE_MIN. Hand-fed labs only
    take packs the belts would have brought (run 20261125-214645: the crew carried red packs out of the factory's
    chests to the 2 labs at spawn every patrol while the 10-lab station waited on its belt); a station that never
    gets its last colour researches nothing. Their labs go back to the inventory for the next station."""
    rs = getattr(getattr(crew, "w", None), "research", None) or {}
    cur = rs.get("current")
    if not cur or not hasattr(crew.kn, "research_cost"):
        return []
    need = set(crew.kn.research_cost(cur))
    labs = crew.base.of("labs")

    def has(s) -> set:
        return {pt.get("pack") for pt in s.params.get("ports", []) if pt.get("pack")}

    full = [s for s in labs if s.params.get("station") and s.powered and need <= has(s)]
    if not need or not full:
        return []
    best = max(full, key=lambda s: int(s.params.get("count") or 0))
    out = []
    for s in labs:
        if s in full:
            continue
        if not s.params.get("station"):
            out.append({"level": "urgent", "key": f"retire-labs-{s.id}",
                        "what": f"labs site {s.id} is fed by hand while research station site {best.id} gets every "
                                f"pack {cur} needs by belt: the crew only carries the station's packs there, so "
                                "it comes down (its labs back to the inventory)",
                        "fix": {"do": "remove", "site": s.id}})
            continue
        try:
            age = crew._minute() - float(s.built_minute or 0)
        except Exception:  # noqa: BLE001 - stub crews in tests
            age = RETIRE_SETTLE_MIN
        if age >= RETIRE_SETTLE_MIN and int(s.params.get("count") or 0) <= int(best.params.get("count") or 0):
            out.append({"level": "urgent", "key": f"retire-station-{s.id}",
                        "what": f"research station site {s.id} still misses {sorted(need - has(s))} after "
                                f"{age:.0f} min while station site {best.id} gets every pack {cur} needs: its labs "
                                "research nothing, so it comes down (its labs back to the inventory)",
                        "fix": {"do": "remove", "site": s.id}})
    return out


RETIRE_SETTLE_MIN = 30.0       # game minutes a second station gets to link its last colour before it comes down


def oil_alerts(crew) -> list[dict]:
    """A refinery whose output is full stops, and with it plastic, sulfur and everything after: one of heavy
    oil, light oil and petroleum backs up. Solid fuel takes any of them (and rocket fuel needs it anyway)."""
    ents = getattr(getattr(crew, "w", None), "entities", {}) or {}
    stuck = [e for e in ents.values() if isinstance(e, dict) and e.get("name") == "oil-refinery"
             and "full_output" in str(e.get("status") or "")]
    if not stuck:
        return []
    makes = getattr(crew, "ready_items", lambda: set())()
    fix = None if "solid-fuel" in makes else {"do": "build", "module": "workshop", "item": "solid-fuel",
                                                "per_minute": 10}
    return [{"level": "urgent", "key": "oil-balance",
             "what": f"{len(stuck)} oil refiner(ies) stopped with a full output: one oil product backs up and "
                     "blocks the others" + ("; a solid-fuel cell takes the surplus (rocket fuel needs it anyway)"
                                            if fix else "; add cracking or more of what uses the full product"),
             "fix": fix}]


def fuel_alerts(crew) -> list[dict]:
    """Sites (other than coal mines and boilers, which have their own alerts) whose burners are out of fuel,
    with the fix that matters: link the coal belt, more coal into it, or (no belt) a delivery for now."""
    out: list[dict] = []
    coal_mines = {s.id for s in crew.coal_mines()}
    for s in crew.base.sites:
        dry = s.health.get("no_fuel", 0)
        if not dry or s.kind == "steam_power" or s.id in coal_mines:
            continue
        if s.kind == "burner_mine" and s.params.get("resource") != "coal":
            continue                                   # the fuel-starved-mines alert covers these
        furnaces = sum(1 for e in s.entities if e[0] in FURNACES)
        what = f"site {s.id} ({s.kind}): {dry} furnace(s)/burner(s) out of fuel" + (
            "; the drills behind them back up (waiting_for_space): a fuel problem, not a belt one" if furnaces else "")
        port = next((pt for pt in s.params.get("ports", []) if pt.get("fuel")), None)
        if port is not None and not port.get("fed"):
            stuck = next((q for q in (crew.stuck_links() if hasattr(crew, "stuck_links") else [])
                          if tuple(q["at"]) == tuple(port["end"])), None)
            if stuck is not None:
                # its coal belt found no route: coal by hand meanwhile, so a smelting column never stands cold
                # while the link waits for its retry (a cold column backs its whole field up)
                out.append({"level": "urgent", "key": f"fuel-site-{s.id}",
                            "what": what + f"; its coal belt failed {stuck['failed']}x ({stuck['why'][:80]}): fed "
                                           "by hand until it is linked",
                            "fix": {"do": "feed", "site": s.id, "item": "coal", "count": min(200, 40 * dry)}})
                continue
            out.append({"level": "urgent", "key": f"fuel-belt-{s.id}", "what": what + "; its coal belt is not linked",
                        "fix": {"do": "link"}})
            continue
        if port is not None:
            ln = next((q for q in getattr(crew, "links", []) if q.get("port") is port), None)
            if ln is not None and crew._minute() - ln.get("minute", 0) < 3:
                continue                               # linked just now: the coal is still on its way
            root = crew._coal_root(ln) if ln is not None and hasattr(crew, "_coal_root") else None
            chain = next((c for c in (crew.coal_chains() if hasattr(crew, "coal_chains") else [])
                          if tuple(c["chest"]) == root), None)
            if chain is not None:                      # the coal-chain alert names the cause and its fix
                continue
            out.append({"level": "urgent", "key": f"fuel-dry-{s.id}",
                        "what": what + "; its coal belt is linked but runs empty: the coal chest it starts from needs "
                                       "more drills", "fix": coal_fix(crew, 1.4 * max(dry, furnaces))})
            continue
        due = crew.retrofit_due() if hasattr(crew, "retrofit_due") else []
        if s in due:                                   # logistics is in: it gets its coal belt where it stands
            out.append({"level": "urgent", "key": f"fuel-site-{s.id}",
                        "what": what + "; it is still fed by hand: the link step fits it a coal belt",
                        "fix": {"do": "link"}})
            continue
        ready = coal_belts_ready(getattr(crew, "kn", None))
        out.append({"level": "urgent", "key": f"fuel-site-{s.id}",
                    "what": what + ("; fed by hand (no room for a coal belt here)" if ready else
                                    "; fed by hand until logistics is researched (then it gets a coal belt)"),
                    "fix": {"do": "feed", "site": s.id, "item": "coal", "count": min(200, 40 * dry)}})
    return out


def rebuild_alerts(crew) -> list[dict]:
    """Hand-fed furnace rows whose coal strip is taken for good: the same row again where its coal belt fits
    (built with the belt from the start), then the old one comes down and its plate links follow. Feeding them by
    hand forever is what kept drills idle (run 20261119-202811: five rows, refuelled at every overseer look)."""
    out: list[dict] = []
    if not coal_belts_ready(getattr(crew, "kn", None)) or not hasattr(crew, "blocked_rows"):
        return out
    full = getattr(crew, "_patch_full", None) or {}
    for s in crew.blocked_rows():
        r = s.params.get("resource")
        if r in full and full[r][1] == crew._base_sig():
            continue                                  # no room on the patch for a new row: feeding it stays
        n = max(1, int(s.params.get("count", 2)))
        out.append({"level": "urgent", "key": f"fuel-rebuild-{s.id}",
                    "what": f"site {s.id} (electric_mine {r}, {n} drills) is fed by hand and its coal belt strip is "
                            "taken: the same row goes where a coal belt reaches it, then the old one comes down",
                    "fix": {"do": "replace", "site": s.id, "force": True,
                            "with": {"do": "build", "module": "electric_mine", "resource": r, "count": n,
                                     "into": "furnace"}}})
    return out


SWAP_PLATES = ("iron-plate", "copper-plate", "stone-brick")


def burner_swap_alerts(crew) -> list[dict]:
    """A plate the base makes less of than its ports want (or whose ports starve with no source), while burner
    mines of it stand full: their plates sit in stone furnaces no belt can take from, so the fix is electric
    drills in their place (nodes.swap_goal), one site per plate at a time, oldest first. Never held by the gate
    (a replace). Run 20261122-133537: copper at 38/min for red science and circuits, two burner copper mines full
    for an hour, and the gate held every new copper mine back because of them."""
    if not electric_coal(crew):
        return []
    try:
        from .nodes import starving, swap_goal, unlinkable_full
        ports = crew.item_ports()
        hungry = starving(crew)
    except Exception:  # noqa: BLE001 - stub crews in tests
        return []
    items = (getattr(crew.w, "stats", None) or {}).get("items") or {}
    out = []
    for plate in SWAP_PLATES:
        want = sum(float(pt.get("per_minute") or 0) for _, pt in ports if pt.get("item") == plate)
        made = float((items.get(plate) or {}).get("made_per_min") or 0)
        if want <= 0 or (made >= want and plate not in hungry):
            continue
        dead = [s for s in unlinkable_full(crew, plate) if swap_goal(s) is not None]
        # a swap whose electric drills found no room near the base lately is not asked for again (run
        # 20261122-133537: "replace site 8" queued twice, failed twice on patches 321+ tiles out, forbidden twice)
        full = getattr(crew, "patch_full", None)
        dead = [s for s in dead if not (callable(full) and full(s.params.get("resource")))]
        if not dead:
            continue
        s = dead[0]
        out.append({"level": "urgent", "key": f"swap-{s.id}",
                    "what": f"{plate}: made {made:.0f}/min, its ports want {want:.0f}/min"
                            + (f" ({len(hungry[plate])} port(s) with no source)" if plate in hungry else "")
                            + f"; burner mine site {s.id} stands full with its plates in stone furnaces no belt can "
                              "take from: electric drills go in its place (built first), then it comes down",
                    "fix": swap_goal(s)})
    return out


POWER_HEADROOM = 1.3            # capacity kept over what the network draws (and what is booked for swaps)


def power_short_kw(crew, add_kw: float = 0.0) -> float:
    """How much capacity is missing to carry what the network draws (or will once its fed sites run), plus what
    is booked for electric swaps and `add_kw`, with POWER_HEADROOM (0: enough)."""
    ps = crew.power_status()
    load = max(ps["demand_kw"], ps.get("expected_kw", 0)) + float(ps.get("booked_kw", 0) or 0) + add_kw
    return max(0.0, load * POWER_HEADROOM - ps["capacity_kw"])


SWAP_CIRCUITS_SPARE = 2.0      # advanced circuits a minute made over what is used before a furnace swap may start


def _circuits_to_spare(crew) -> bool:
    """Is blue science automated, with advanced circuits made faster than they are used? An electric furnace takes 5
    of them, a 13-furnace column 65, and the swap's kit takes them from the advanced-circuit factory's chests: the
    same circuits blue science runs on (3 per 2 packs, at about 5 a minute in run 20261125-214645)."""
    w = getattr(crew, "w", None)
    if "blue-auto" not in (getattr(w, "milestones", None) or {}):
        return False
    st = ((getattr(w, "stats", None) or {}).get("items") or {}).get("advanced-circuit") or {}
    return float(st.get("made_per_min") or 0) - float(st.get("used_per_min") or 0) >= SWAP_CIRCUITS_SPARE


def electric_swap_alerts(crew) -> list[dict]:
    """Electric smelting (docs/hybrid-plan.md, section 5): once electric furnaces are unlocked, the coal-fed smelter
    columns and furnace rows are rebuilt with them, one at a time and the least risky first (upgrade.swap_candidates),
    each a replace that builds the new one first. Its furnaces' power (180 kW each) is booked before it may start:
    with too little headroom the alert asks for a steam station first (its key carries today's capacity, so each new
    station lets the rule floor ask again). Queued by the rule floor without pausing anything ("swap-")."""
    from .modules import ELECTRIC_FURNACE
    kn = getattr(crew, "kn", None)
    if ELECTRIC_FURNACE not in (getattr(kn, "unlocked", None) or set()):
        return []
    try:
        from .upgrade import swap_candidates, swap_goal, swap_in_flight, swap_tries
        if swap_in_flight(crew) is not None:
            return []                                 # never more than one swap in flight
        cands = swap_candidates(crew)
    except Exception:  # noqa: BLE001 - stub crews in tests
        return []
    can = getattr(crew, "can_obtain", None)
    if not _circuits_to_spare(crew):
        return []                                     # blue science first: it runs on the same advanced circuits
    for s in cands[:3]:
        g = swap_goal(crew, s)
        n = max(1, int(g["power_kw"] // 180))
        if callable(can) and not can(ELECTRIC_FURNACE, n):
            continue                                  # its furnaces cannot be made yet (advanced circuits)
        try:
            short = power_short_kw(crew, g["power_kw"])
            cap = crew.power_status()["capacity_kw"]
            base_short = power_short_kw(crew)
        except Exception:  # noqa: BLE001
            short, cap, base_short = 0.0, 0, 0.0
        if short > 0 and base_short > 0:
            return []                                 # short already: the headroom alert asks for that station
        if short > 0:
            return [{"level": "urgent", "key": f"swap-power-electric-{cap}",
                     "what": f"site {s.id} ({s.kind} {s.params.get('resource')}) is next for electric furnaces: "
                             f"{g['power_kw']} kW more would leave the network {short:.0f} kW short of its "
                             f"{int(round((POWER_HEADROOM - 1) * 100))}% headroom: a steam station first",
                     "fix": {"do": "build", "module": "steam_power", "engines": 2}}]
        tries, _ = swap_tries(crew, s.id)
        return [{"level": "urgent", "key": f"swap-electric-{s.id}-{tries}",
                 "what": f"site {s.id} ({s.kind} {s.params.get('resource')}, {n} coal-fed furnaces): rebuilt with "
                         f"electric furnaces beside it ({g['power_kw']} kW booked), then it comes down with its coal "
                         "belt; no more coal or refuelling for it",
                 "fix": g}]
    return []


def power_headroom_alerts(crew) -> list[dict]:
    """The power planner: keep POWER_HEADROOM over measured demand (and booked swaps), with a steam station queued
    early ("headroom-": no pause) rather than at the limit (the urgent "power-capacity" alert at 95%). The key
    carries today's capacity, so the next station is asked for once this one stands, if still short.
    TODO(solar): once solar-energy is researched a solar field (panels + accumulators) would add capacity with no
    fuel at all; there is no solar module yet, so steam it is (and power_status counts steam engines only)."""
    try:
        ps = crew.power_status()
        if not crew.base.of("steam_power") or not ps["capacity_kw"]:
            return []
        short = power_short_kw(crew)
    except Exception:  # noqa: BLE001 - stub crews in tests
        return []
    if short <= 0 or max(ps["demand_kw"], ps.get("expected_kw", 0)) > 0.95 * ps["capacity_kw"]:
        return []                                     # enough, or the urgent power-capacity alert has it
    return [{"level": "urgent", "key": f"headroom-power-{ps['capacity_kw']}",
             "what": f"power: {ps['capacity_kw']} kW against {max(ps['demand_kw'], ps.get('expected_kw', 0))} kW drawn"
                     + (f" and {float(ps.get('booked_kw')):.0f} kW booked" if ps.get("booked_kw") else "")
                     + f": under {int(round((POWER_HEADROOM - 1) * 100))}% headroom, another steam station now",
             "fix": {"do": "build", "module": "steam_power", "engines": 2}}]


OIL_WELLS_MIN = 3               # crude wells known before the oil chain is built on them
OIL_TECHS = ("oil-processing", "plastics", "sulfur-processing", "advanced-oil-processing", "chemical-science-pack")


def oil_scout_alerts(crew) -> list[dict]:
    """Oil scouting ahead: as soon as oil processing (or the plastic and sulfur chain) is queued, researched or
    asked for, at least OIL_WELLS_MIN crude wells should be known. Run 20261125-214645 knew one well, 188 tiles out,
    for the whole oil chain. A scout goal walks the ring search on (Crew.scout_oil), one ring a goal."""
    w = getattr(crew, "w", None)
    if w is None:
        return []
    rs = getattr(w, "research", None) or {}
    kn = getattr(crew, "kn", None)
    asked = set(rs.get("queue") or []) | ({rs["current"]} if rs.get("current") else set())
    asked |= set(getattr(kn, "researched", None) or set())
    goals = [g for q in ("urgent", "closeout_queue", "plan_queue") for g in (getattr(crew, q, None) or [])
             if isinstance(g, dict)]
    wanted = bool(asked & set(OIL_TECHS)) or any(
        g.get("module") == "pumpjack" or (g.get("do") == "research" and g.get("tech") in OIL_TECHS)
        or g.get("item") in ("plastic-bar", "sulfur", "sulfuric-acid", "petroleum-gas") for g in goals)
    if not wanted:
        return []
    wells = len((getattr(w, "resources", None) or {}).get("crude-oil", ()) or ())
    if wells >= OIL_WELLS_MIN or getattr(w, "oil_rings", 0) >= min(OIL_SCOUT_MAX_RINGS, getattr(w, "OIL_MAX_RINGS", 5)):
        return []                                     # enough, or searched as far as scouting ahead is worth
    try:
        now = crew._minute()
    except Exception:  # noqa: BLE001
        return []
    if now - float(crew.__dict__.get("_oil_scouted", -1e9)) < OIL_SCOUT_GAP_MIN:
        return []
    return [{"level": "urgent", "key": f"scout-oil-{wells}-{getattr(w, 'oil_rings', 0)}",
             "what": f"oil processing is on the way and only {wells} crude-oil well(s) are known: scouting for "
                     f"{OIL_WELLS_MIN - wells} more before the oil chain is built on them",
             "fix": {"do": "scout", "resource": "crude-oil"}}]


OIL_SCOUT_GAP_MIN = 40.0        # game minutes between oil scouts (each walks at most one or two new rings)
OIL_SCOUT_MAX_RINGS = 3         # scouting ahead stops ~1,400 tiles out (a pumpjack goal still searches farther)


def _diagnoses(crew) -> list[dict]:
    try:
        from .triage import diagnoses
        return diagnoses(crew)
    except Exception:  # noqa: BLE001 - stub crews in tests, a report line is never worth a crash
        return []


def rebuild_soon_alerts(crew) -> list[dict]:
    """Mines running out: the same mine on fresh ore while the old one still runs (upkeep.rebuild_due). The rule
    floor queues it at once (no LLM needed, nothing paused), so the factories it feeds never see a gap."""
    out: list[dict] = []
    from .upkeep import MINES, depleted_fix, rebuild_due
    feeding = {(round(ln["source"][0], 2), round(ln["source"][1], 2)) for ln in getattr(crew, "links", [])}
    for s in crew.base.sites:
        if s.kind not in MINES:
            continue
        srcs = [(x, y) for x, y, _ in s.chests] + ([tuple(s.params["line"]["source"])] if s.params.get("line") else [])
        if s.params.get("resource") != "coal" and not any((round(x, 2), round(y, 2)) in feeding for x, y in srcs):
            continue                                  # nobody draws on it: rebuilding it early only spends parts
        why = rebuild_due(crew, s)
        if not why:
            continue
        fix = depleted_fix(crew, s)
        if fix.get("do") != "replace":
            continue                                  # no room for it near the base: the ore outlook says what next
        out.append({"level": "urgent", "key": f"rebuild-{s.id}",
                    "what": f"site {s.id} ({s.kind} {s.params.get('resource')}): {why}; the same mine goes on fresh "
                            "ore now while this one still runs, then this one comes down and its belts follow",
                    "fix": {**fix, "early": True}})
    return out


def depleted_alerts(crew) -> list[dict]:
    """Mines on mined-out ground. One that feeds a belt, or any coal mine, goes onto fresh ore now and its belts
    follow (depleted_fix); a coal one is a coal-* alert, which the rule floor acts on by itself."""
    out: list[dict] = []
    from .upkeep import MINES, depleted_fix, mine_depleted
    feeding = {(round(ln["source"][0], 2), round(ln["source"][1], 2)) for ln in getattr(crew, "links", [])}
    for s in crew.base.sites:
        if s.kind not in MINES or not mine_depleted(s):
            continue
        feeds = any((round(x, 2), round(y, 2)) in feeding for x, y, _ in s.chests)
        fix = depleted_fix(crew, s)
        moved = fix.get("do") == "replace"
        out.append({"level": "urgent" if feeds or s.params.get("resource") == "coal" else "warn",
                    # coal-*: the rule floor acts on it at once (boilers and furnaces burn what it mines)
                    "key": f"{'coal-' if s.params.get('resource') == 'coal' else ''}depleted-{s.id}",
                    "what": f"site {s.id} ({s.kind} {s.params.get('resource')}): {s.health.get('no_minable_resources')} "
                            "drill(s) on mined-out ground" + ("; its chests feed belts" if feeds else "")
                            + ("; the same mine goes on fresh tiles, then the old one comes down and its belts are "
                               "laid from the new chests" if moved else "; no room for it on the patch: removed"),
                    "fix": fix})
    return out


def unfinished_key(mod: str, params: dict) -> str:
    """What a half-built site was meant to be: its rebuild is tried once per such key."""
    return mod + json.dumps({k: v for k, v in params.items() if k not in ("reflex",)}, sort_keys=True, default=str)


UNFINISHED_GRACE_MIN = 2.0      # game minutes a half-built site stands before it is taken up (its own goal may retry)


def unfinished_alerts(crew) -> list[dict]:
    """A build that stopped half-way leaves an "unfinished" site: buildings that do nothing (often unpowered) and
    hold ground and parts. The rule floor takes it up and builds the same thing again once, from the parts it
    gets back (a replace with clear_first); a second failure only takes it up. An outpost's smelting column is
    only taken up: the outpost's own finishing step builds its column (and trunk) again. Run 20261125-154120:
    a 13-furnace column stopped at 125 buildings (no iron chest), stood unpowered for 25 minutes, and a second
    column went up elsewhere."""
    out: list[dict] = []
    try:
        minute = crew._minute()
        from .modules import MODULES
    except Exception:  # noqa: BLE001 - stub crews in tests
        return out
    for s in crew.base.sites:
        if not s.kind.startswith("unfinished ") or minute - float(s.built_minute or 0) < UNFINISHED_GRACE_MIN:
            continue
        mod = s.kind[len("unfinished "):]
        p = {k: v for k, v in s.params.items() if k not in ("do", "module", "reflex")}
        column = mod == "smelter_column" and p.get("resource") in ("iron-ore", "copper-ore", "coal", "stone",
                                                                   "uranium-ore")
        tried = getattr(crew, "__dict__", {}).get("_unfinished_retried") or set()
        again = mod in MODULES and not column and unfinished_key(mod, p) not in tried
        fix = ({"do": "replace", "site": s.id, "clear_first": True,
                "with": {"do": "build", "module": mod, **p}} if again else
               {"do": "remove", "site": s.id, "force": True})
        out.append({"level": "urgent", "key": f"rebuild-unfinished-{s.id}",
                    "what": f"site {s.id} is a half-built {mod} ({len(s.entities)} buildings; {s.note[:100]}): "
                            + ("taken up and built again from its parts" if again else "taken up (parts back)"),
                    "fix": fix})
    return out


def orphan_alerts(crew) -> list[dict]:
    """Factories whose only consumer was removed (Crew._note_orphans): link them to another consumer of their
    item, or remove them, so their machines and the belt they tap serve something."""
    try:
        orphans = crew.orphan_sites()
        ports = crew.item_ports()
    except Exception:  # noqa: BLE001 - stub crews in tests
        return []
    out = []
    for s, o in orphans:
        item = o.get("item")
        want = sorted({q.id for q, pt in ports if pt.get("item") == item and not pt.get("fed") and q is not s})
        out.append({"level": "warn", "key": f"orphan-{s.id}",
                    "what": f"site {s.id} ({s.kind} {s.params.get('item') or item}) lost its only consumer, site "
                            f"{o.get('lost')} (removed at minute {o.get('minute', 0):.0f}): nothing takes its {item}"
                            + (f"; sites {want[:6]} have unfed {item} ports: link it there" if want else
                               "; no other site wants it: remove it (its machines and its input belt are spare)"),
                    "fix": {"do": "link"} if want else {"do": "remove", "site": s.id}})
    return out


def detect(crew) -> list[dict]:
    """Alerts: {level: urgent|warn, key, what, fix}. 'urgent' means the base stops working soon."""
    out: list[dict] = []
    base, w = crew.base, crew.w
    burning = any(e[0] in ("burner-mining-drill", "stone-furnace", "boiler") for s in base.sites for e in s.entities)

    # the character itself: if it cannot walk, every goal fails as "out of reach"
    if getattr(w, "walk_fails", 0) >= 3:
        out.append({"level": "urgent", "key": "walk",
                    "what": f"the character failed {w.walk_fails} moves in a row ({w.walk_fail}); goals that need "
                            "a walk fail as out_of_reach until it can move again", "fix": None})

    # fuel: the coal budget, and coal mines and boilers that are running dry
    b = crew.coal_budget()
    if burning and crew.coal_mines():
        short = b["burned_per_min"] - b["mined_per_min"]
        if short > 0 or (b["minutes_left"] is not None and b["minutes_left"] < 10):
            fix = coal_fix(crew, max(short, 0))
            urgent = b["minutes_left"] is not None and b["minutes_left"] < 8
            out.append({"level": "urgent" if urgent else "warn", "key": "coal-deficit",
                        "what": f"coal: mined {b['mined_per_min']}/min, burned {b['burned_per_min']}/min, "
                                f"stock {b['stock']}" + (f" (runs out in ~{b['minutes_left']} min)"
                                                         if b["minutes_left"] else "")
                                + (f"; {fix['count']} more {fix['module'].split('_')[0]} coal drills cover it"
                                   if fix and fix.get("do") == "build" else
                                   f"; the coal patches near the base are full: swap burner coal mine site "
                                   f"{fix['site']} for electric drills" if fix else
                                   "; the coal patches near the base are full and no burner coal mine is left to "
                                   "swap: use less (no new boilers or furnace rows) until electric furnaces"),
                        "fix": fix})
    out += depleted_alerts(crew)
    out += unfinished_alerts(crew)
    out += orphan_alerts(crew)
    out += rebuild_soon_alerts(crew)
    out += rebuild_alerts(crew)
    out += burner_swap_alerts(crew)
    out += electric_swap_alerts(crew)
    out += oil_scout_alerts(crew)
    for s in crew.coal_mines():
        dry = s.health.get("no_fuel", 0)
        if dry:
            out.append({"level": "urgent", "key": f"coal-mine-{s.id}",
                        "what": f"coal mine site {s.id}: {dry} drill(s) out of fuel; coal production is stopping",
                        "fix": {"do": "feed", "site": s.id, "item": "coal", "count": 10 * dry}})
    for (x, y), n in list(crew.foreman.boiler_coal.items()):
        if n < 10:
            site = next((s for s in base.of("steam_power") if any(
                abs(cx - x) < 1 and abs(cy - y) < 1 for cx, cy, _ in s.chests)), None)
            if site is None:                          # the station came down: a stale chest, nothing to feed (run
                crew.foreman.boiler_coal.pop((x, y), None)   # 20261124-015025: "feed site None" failed 6 times)
                continue
            out.append({"level": "urgent", "key": f"boiler-{x},{y}",
                        "what": f"power station site {site.id}: only {n} coal left in the "
                                "boiler's chest; the power (labs, assemblers) stops when it is empty",
                        "fix": {"do": "feed", "site": site.id, "item": "coal", "count": 60}})

    # power: electric sites cut off, or the network at its limit
    for s in base.sites:
        n = sum(s.health.values())
        if s.electric and n and s.health.get("no_power", 0) >= max(1, n // 2):
            out.append({"level": "urgent", "key": f"power-site-{s.id}",
                        "what": f"site {s.id} ({s.kind}): {s.health['no_power']}/{n} machines have no power",
                        "fix": {"do": "connect", "site": s.id}})
    # Energy comes before building: machines on low power slow everything at once, so this is urgent (the rule
    # floor pauses the goal in hand and builds a station), seen either in the machines' status or in our own
    # count of what the network must carry
    low = sum(s.health.get("low_power", 0) for s in base.sites)
    elec = sum(sum(s.health.values()) for s in base.sites if s.electric)
    try:
        ps = crew.power_status()
    except Exception:  # noqa: BLE001 - stub crews in tests
        ps = {"capacity_kw": 0, "demand_kw": 0}
    # what the network draws, or what its fed sites will draw once they run (expected_kw): whichever is more
    load = max(ps["demand_kw"], ps.get("expected_kw", 0))
    over = ps["capacity_kw"] and load > 0.95 * ps["capacity_kw"]
    if (low and low >= max(2, elec // 4)) or over:
        out.append({"level": "urgent", "key": "power-capacity",
                    "what": (f"{low}/{elec} electric machines run on low power; " if low else "")
                            + f"the network carries {ps['demand_kw']} kW of {ps['capacity_kw']} kW"
                            + (f" and its fed sites will draw ~{ps['expected_kw']} kW once they run"
                               if ps.get("expected_kw", 0) > ps["demand_kw"] else "") + ": more steam power",
                    "fix": {"do": "build", "module": "steam_power", "engines": 2}})
    else:
        out += power_headroom_alerts(crew)            # 30% over demand, queued early without a pause

    # smelters out of fuel. A furnace with no fuel fills up with ore and its drill stops behind it
    # ("waiting_for_space"), which reads like a belt problem; the alert says what it really is
    out += coal_chain_alerts(crew)
    out += fuel_alerts(crew)
    out += ore_alerts(crew)
    out += input_chain_alerts(crew)
    out += oil_alerts(crew)
    try:
        from .modfit import alerts as module_alerts
        out += module_alerts(crew)
    except Exception:  # noqa: BLE001 - a look that failed: no verdict this time
        pass
    out += pipe_alerts(crew)
    out += station_alerts(crew)
    out += retire_alerts(crew)

    # buildings that stopped: out of fuel, out of input, output stuck
    starved = [s for s in base.sites if s.kind == "burner_mine" and s.params.get("resource") != "coal"
               and s.health.get("no_fuel", 0) >= max(1, int(s.params.get("count", 1)) // 2)]
    if starved:
        # coal piling up somewhere: the burners lack deliveries, not coal; else there is too little coal
        enough = b["net_per_min"] > 0 or b["stock"] >= 40 * len(starved)
        out.append({"level": "warn", "key": "fuel-starved-mines",
                    "what": f"{len(starved)} burner mine(s) out of fuel (sites {', '.join(str(s.id) for s in starved[:6])})"
                            + (": hand-fed burners outgrew the foreman; replace them with electric_mine rows (coal "
                               "by belt)" if enough else ": more burners than the coal supply feeds"),
                    "fix": ({"do": "feed", "site": starved[0].id, "item": "coal", "count": 20 * int(
                        starved[0].params.get("count", 2))} if enough else coal_fix(crew, max(
                        -b["net_per_min"], 2.25 * sum(int(s.params.get("count", 1)) for s in starved))))})

    # belt links that keep failing: say why once, so nobody keeps asking for the same link
    for q in (crew.stuck_links() if hasattr(crew, "stuck_links") else []):
        far = far_link_fix(q)
        if far is not None:
            # only the distance stopped it: no retry ever helps, the trunk goal does (queued by the rules)
            out.append({"level": "urgent", "key": f"trunk-{q['at'][0]},{q['at'][1]}",
                        "what": f"the {q['item']} link into site {q['into_site']} is beyond a link's reach "
                                f"({q['why'][:120]}): a trunk belt is laid as a goal of its own",
                        "fix": far})
        elif q["failed"] >= 2 and failed_before_logistics(crew, q):
            # it failed before belts could cross; they can now: lay the belt again (with undergrounds) before any
            # move. Run 20261125-184025: red science was rebuilt for two pre-logistics failures, the overseer
            # stopped the move half way, and a second red science factory stood unfed
            out.append({"level": "urgent", "key": f"rebuild-relink-{q['at'][0]},{q['at'][1]}",
                        "what": f"the {q['item']} link into site {q['into_site']} failed before underground belts "
                                "were researched: laid again now, with undergrounds",
                        "fix": {"do": "relink", "site": q["into_site"], "item": q["item"]}})
        elif q["failed"] >= 2 and _strands(crew, q):
            # the plates are there, only out of a belt's reach: the factory goes next to them (built first), not
            # a third try a half hour on. Run 20261125-154120: the red factory stood 80 tiles from a 13-furnace
            # column whose 244 plates a minute filled its chests, two links failed, and research stalled on packs
            fix = relocate_fix(crew, q)
            out.append({"level": "urgent" if fix else "warn", "key": f"rebuild-near-{q['at'][0]},{q['at'][1]}",
                        "what": f"the {q['item']} link into site {q['into_site']} failed {q['failed']}x while "
                                f"{q['item']} sits unused in chests: "
                                + ("the site is built again next to them, then the old one comes down" if fix else
                                   "it cannot be moved (sealed or no module for it)"),
                        "fix": fix})
        elif q["failed"] >= 2:
            fix = relocate_fix(crew, q) if q["failed"] >= 3 else None
            out.append({"level": "warn", "key": f"link-stuck-{q['at'][0]},{q['at'][1]}",
                        "what": f"the {q['item']} link into site {q['into_site']} failed {q['failed']}x: {q['why'][:200]}"
                                + f"; it retries by itself within {15 * min(4, q['failed'])} min (a relink goal: now)"
                                + ("; it is walled in: rebuild it where its inputs are (the new one first)"
                                   if fix and fix.get("do") == "replace" else
                                   "; lay its input again from a spare source" if fix else ""),
                        "fix": fix})
    # plates made but not delivered: the totals say "surplus" while factories starve (nodes.py)
    try:
        from .nodes import plate_ledger
        ledger = plate_ledger(crew)
    except Exception:  # noqa: BLE001 - stub crews in tests
        ledger = []
    for row in ledger:
        if row["unfed_ports"]:
            out.append({"level": "warn", "key": f"plate-routing-{row['item']}",
                        "what": row["text"] + ": the shortage is routing, not supply. More mines or another "
                                f"{row['item']} consumer change nothing until those ports get a belt. A relink "
                                f"reaches only sources within {LINK_REACH} tiles: a "
                                "farther spare source needs a trunk goal, or a new mine or smelter next to the "
                                "starving site; not another relink", "fix": None})
    for s in base.sites:
        n = sum(s.health.values())
        idle = sum(v for k, v in s.health.items() if k in ("no_ingredients", "item_ingredient_shortage",
                                                            "waiting_for_source_items"))
        full = sum(v for k, v in s.health.items() if k in ("full_output", "waiting_for_space_in_destination"))
        if s.kind == "factory" and n > 3 and idle >= 0.6 * n:
            out.append({"level": "warn", "key": f"input-{s.id}",
                        "what": f"factory site {s.id} ({s.params.get('item')}): {idle}/{n} machines wait for input; "
                                "its ore or plate supply is the bottleneck", "fix": None})
        if n > 2 and full >= 0.6 * n and s.kind not in ("burner_mine",):
            out.append({"level": "warn", "key": f"output-{s.id}",
                        "what": f"site {s.id} ({s.kind}): {full}/{n} machines have nowhere to put their output",
                        "fix": None})

    # items running short (anything, not only coal): what the base uses faster than it makes
    for f in flows(crew):
        if f["item"] in FUELS or f["net"] >= 0 or f["minutes_left"] is None or f["minutes_left"] > 6:
            continue
        if not (f["item"].endswith(("plate", "science-pack")) or f["item"] in ("iron-gear-wheel",
                                                                                 "electronic-circuit")):
            continue
        out.append({"level": "warn", "key": f"short-{f['item']}",
                    "what": f"{f['item']}: used {f['used']}/min, made {f['made']}/min, {f['stock']} on hand "
                            f"(~{f['minutes_left']} min)", "fix": supply_fix(crew, f["item"], -f["net"])})

    # belts that carry something they were not laid for (purity.py): the rule floor purges them
    try:
        from .purity import check as belt_check
        out.extend(belt_check(crew))
    except Exception:  # noqa: BLE001 - a stub crew in a test, or a failed look: no verdict this time
        pass
    # buildings a site never got (refused tiles, holes in belt lines; gaps.py): the rule floor repairs them
    try:
        from . import gaps
        for note in gaps.audit(crew):
            crew.w.log(f"[gaps] {note}")
        out.extend(gaps.alerts(crew))
    except Exception:  # noqa: BLE001 - a stub crew in a test, or a failed look: no verdict this time
        pass

    # research, and the goal in hand
    stall = crew.research_stall()
    small = False
    if stall:
        try:
            small = bool(base.of("labs")) and crew._top_up_stalled(w.research or {}, dry=True)
        except Exception:  # noqa: BLE001 - stub crews in tests
            small = False
    if stall and small:
        # a handful of hand-craftable packs is all it lacks (the zones test, seed 737373: automation at 80% for
        # 25 game minutes with nothing feeding the labs): the lab round crafts and carries them, now
        lab = base.of("labs")[0]
        out.append({"level": "urgent", "key": f"labs-topup-{lab.id}", "what": stall + "; its last packs are crafted "
                    "by hand and taken to the labs now",
                    "fix": {"do": "feed", "site": lab.id, "item": "automation-science-pack"}})
    elif stall:
        out.append({"level": "warn", "key": "research-stalled", "what": stall, "fix": None})
    try:
        starving = crew.foreman._labs_starving()
    except Exception:  # noqa: BLE001 - stub crews in tests
        starving = False
    if starving:
        # labs out of packs while packs are on hand or in chests: research is what everything waits on, so the
        # rule floor feeds them at once (the crew's lab round), ahead of the goal in hand
        lab = next((s for s in base.of("labs") if s.health.get("missing_science_packs", 0)), None)
        out.append({"level": "urgent", "key": f"labs-feed-{lab.id if lab else 0}",
                    "what": "labs wait for science packs that are on hand or in chests: taken to them now",
                    "fix": {"do": "feed", "site": lab.id if lab else None, "item": "automation-science-pack"}})
    if base.of("labs") and not (w.research or {}).get("current"):
        out.append({"level": "warn", "key": "labs-idle", "what": "the labs have nothing to research", "fix": None})
    cur = crew.current
    if cur:
        mins = (w.now_tick() - cur["tick"]) / 3600
        if mins >= LONG_GOAL_MIN:
            out.append({"level": "warn", "key": "long-goal",
                        "what": f"the goal {_short(cur['goal'])} has run {mins:.0f} game min "
                                f"(now: {w.activity[:80]})", "fix": None})
    return out


def _nodes_view(crew) -> dict:
    """Ports and plates one by one (nodes.py): unfed inputs and why, stranded sources, made vs delivered."""
    try:
        from .nodes import ledger, plate_ledger, port_report
        return {"unfed_ports": port_report(crew)["summary"], "plates": [r["text"] for r in plate_ledger(crew)],
                "lines": ledger(crew)[:12]}
    except Exception:  # noqa: BLE001 - stub crews in tests
        return {}


def _agenda(crew):
    try:
        from .agenda import agenda_of, now_of
        return agenda_of(crew).summary(crew, n=10)
    except Exception:  # noqa: BLE001 - stub crews in tests
        return None


def situation(crew, alerts: list[dict], doctrine: str = "") -> dict:
    """One page for the assessor: what is happening now and what is going wrong."""
    w, base = crew.w, crew.base
    cur = crew.current
    rs = w.research or {}
    from .zones import report_of
    return {
        "zones": report_of(crew),
        "game_minute": round(w.now_tick() / 3600, 1),
        "doing_now": w.activity[:100],
        "current_goal": ({"goal": cur["goal"], "minutes": round((w.now_tick() - cur["tick"]) / 3600, 1),
                          "step": list(w.steps), "can_pause": crew.can_pause(),
                          "protected": bool(cur.get("aged"))} if cur else None),
        "urgent_queue": [_short(g) for g in crew.urgent],
        "agenda": _agenda(crew),
        "plan_waited_min": round(_plan_waited(crew), 1),
        "plan_queue": [_short(g) for g in crew.plan_queue[:6]],
        "closeout_queue": [_short(g) for g in getattr(crew, "closeout_queue", [])[:6]],
        "alerts": [{k: a.get(k) for k in ("level", "what", "fix")} for a in alerts],
        "flows": flows(crew)[:14],
        "coal": crew.coal_budget(),
        "research": {"current": rs.get("current"), "progress": rs.get("progress"), "queue": rs.get("queue")},
        "sites": [{"id": s.id, "kind": s.kind, **{k: v for k, v in s.params.items() if k in ("resource", "item",
                                                                                              "count", "engines", "fuel")},
                   "health": s.health} for s in base.sites][:30],
        "stuck_links": crew.stuck_links() if hasattr(crew, "stuck_links") else [],
        "repair_diagnoses": _diagnoses(crew),
        **_nodes_view(crew),
        "short_coal_belts": crew.coal_chains() if hasattr(crew, "coal_chains") else [],
        "short_belts": [c for c in (crew.short_chains() if hasattr(crew, "short_chains") else [])
                        if c["item"] != "coal"],
        "sealed_sites": sorted(getattr(crew, "sealed", {})),
        "current_goal_is_frontier": _frontier(crew),
        "foreman_priority": crew.foreman.priority,
        "inventory": dict(sorted(w.inventory.items(), key=lambda kv: -kv[1])[:16]),
        "doctrine": doctrine[:400],
        "overseer": {"guidance": getattr(crew, "overseer_guidance", None),
                     "forbidden": [f["match"] for f in getattr(crew, "overseer_forbid", [])]},
    }


def far_link_fix(q: dict) -> dict | None:
    """The trunk goal for a stuck link that failed on distance alone, or None. A failed trunk records its own
    reason (no FAR_LINK in it), so a trunk that cannot be laid is not queued again and again."""
    why = str(q.get("why") or "")
    if FAR_LINK not in why or q.get("into_site") is None or not q.get("item"):
        return None
    import re
    m = re.search(r"is (\d+) tiles from", why)
    if m and int(m.group(1)) > TRUNK_AUTO_MAX:
        return None
    return {"do": "trunk", "site": q["into_site"], "item": q["item"]}


def _plan_waited(crew) -> float:
    from .upkeep import plan_waited_min
    return plan_waited_min(crew, crew.w.now_tick())


def _frontier(crew) -> bool:
    from .upkeep import is_frontier
    return is_frontier((getattr(crew, "current", None) or {}).get("goal"))


def _short(goal: dict) -> str:
    rest = {k: v for k, v in goal.items() if k not in ("do", "module", "reflex")}
    return f"{goal.get('do')} {goal.get('module', '')} {rest}".replace("  ", " ")


# ------------------------------------------------------------------ deciding
class RuleAssessor:
    """The floor: fuel and power fixes go first and pause the goal in hand. Other alerts are left to the AI
    assessor and the strategist (they see them in their reports)."""
    name = "rules"
    cost_usd = 0.0

    def decide(self, sit: dict, alerts: list[dict], answered: set[str]) -> dict:
        goals, keys, stop = [], [], False
        for a in alerts:
            if a["level"] == "urgent" and a.get("fix") and a["key"].startswith(CRITICAL + SOON)                     and a["key"] not in answered:
                goals.append(a["fix"])
                keys.append(a["key"])
                stop = stop or a["key"].startswith(CRITICAL)
        return {"note": "; ".join(a["what"][:80] for a in alerts if a["key"] in keys)[:240],
                "urgent_goals": goals, "interrupt": stop, "replan": False, "priority": None, "keys": keys}


ASSESS_PROMPT = """You are the shift supervisor of a Factorio AI racing to launch a rocket. A strategist plans the
build order; a scripted crew carries out one goal at a time, and goals can take many minutes. You see the base
every minute or two, and at once when something new goes wrong. Your job is to keep the base running and moving
at full speed: amend the plan as soon as a problem appears, not when the current goal happens to end.

The floorplan (when the report has "zones"): the base grows along a main bus. A bus site carries plates from its
head past reserved slots, and slot factories (sites with a "slot") take their inputs from the bus. When the bus
runs short, the fix is plates into its head ("trunk" on the bus site, or a bigger smelter feeding it); never
forbid the bus's trunks and never replace or remove a slot factory to route around the bus (both are refused).

One agenda, shared by every decider ("agenda" in the report): every queued goal with who asked for it, the
overseer's vetoes, the goals the overseer dropped recently and why, and the latest decisions with their authors.
Read it before you act. Do not ask again for something already queued or running (it is merged, not done twice),
never re-queue what the overseer vetoed or dropped (it is refused), and build on the others' decisions instead of
undoing them. The overseer has the final word.

Progress first: the run is judged by how far it gets, not by how tidy the base is. Fix fully what the next stage
needs (the plan's next build, the inputs and fluids of the sites making the next science, the labs); give the rest
only what keeps it from blocking progress. Leave idle or slightly short sites alone, never rebuild a mine while other
mines of the same item have spare, and pick the cheapest fix that works (relink or trunk before a rebuild, a hand
feed before a new factory). Every urgent goal delays the plan.

Decide now:
- urgent_goals: small fixes to do before anything else (0-3), e.g. feed a site, more coal or ore drills, connect
  a site, a power station, a factory for an item that is running out. Only what fixes a real problem shown in
  the situation (alerts, flows going negative, sites stopped, research stuck). The crew already handles urgent
  fuel and power alerts itself (they appear in urgent_queue): do not repeat those.
- interrupt: true to pause the current goal now (it resumes after your urgent goals). Only when waiting for it to
  finish costs more than pausing it (current_goal.can_pause must be true; it never is while buildings are placed).
- priority: what the foreman tends first after coal mines and power (site ids, kinds, resources or items), or
  null to leave it.
- replan: true when the strategist's plan no longer fits (its next goals are wrong now, a goal keeps failing, a
  bottleneck needs a different build order). It is asked in the background.
Read site health for the cause, not the symptom: furnaces with no_fuel make their drills show
waiting_for_space and the factories they feed show waiting_for_source_items. That is a fuel problem (the fuel
alerts say which coal belt to link or feed), and a link goal does not fix it. stuck_links lists links that
already failed and why: do not ask for "link" again for them; it only helps after something changed (a site
built or removed). repair_diagnoses says, for each starving site, what is really wrong (a dry or stopped source,
a source shared too thin, a polluted belt, a missing belt) and the repair that fits: order that repair. A relink
only fixes a missing or broken belt; the crew diagnoses every relink and swaps in the fitting repair anyway.
Before logistics (underground belts) burners and furnaces are fed by hand, nearly a stack at
a time; once logistics is researched, coal moves by belt and the link step fits the hand-fed rows and boilers
with coal belts where they stand: then prefer electric coal mines and coal-belt fixes over feeding by hand.
Never remove a working site or belt just to make room for another coal belt.
Size every supply build to the measured gap, never by habit: the gap is used minus made a minute (production flows,
alerts "used X/min, made Y/min"), plus about 30% headroom. An electric_mine row takes 2-12 drills (each ~19 plates,
15 bricks or 30 ore/coal/stone a minute), a burner_mine 2-8 (15 ore a minute), a steel smelter_column 2-24 furnaces
(3.75 steel a minute each), a factory any per_minute. A gap of 70 iron plates a minute is one row of 5 drills, not
another row of 4; a gap bigger than one build gets the biggest build now and more next time. An alert's "fix" is
already sized this way.
A coal belt carries what its chest gets (one electric drill: 30 coal/min, a boiler alone burns 27/min).
"short_coal_belts" lists belts shared by more boilers and rows than their chest feeds: they run dry in turn.
The fix is a spare coal chest belted into that belt (the link step does it), so more coal drills help only
when no coal chest is spare. When the coal patch near the base is full, swap burner coal mines on it for
electric ones (replace), and stop adding boilers and furnace rows that coal cannot feed.
A workshop is courier-fed: never ask for one for red or green science or other iron and copper chains (the
factory module automates those with belts). Automation beats hands: never use urgent goals to make the crew hand-craft what a factory could make. A research
goal builds the factories for packs nothing makes before it starts, which can take many minutes; put research
in urgent_goals only when the labs are idle and nothing else is more pressing.
Sealed sites (sealed_sites) ran on their own for a while: never remove them while they work; replacing one
is only for a bigger build of the same thing, and {{"do": "upgrade", "site": id}} makes one faster in place. When
current_goal_is_frontier is true the crew is building the next tier: interrupt only for base-wide trouble
(power short, coal running out); other fixes go in urgent_goals and run right after it.
The overseer (situation.overseer) has the last word: follow its guidance, never undo its orders, and never ask
for a goal it forbids.
Work with the run, not against it. current_goal, urgent_queue, plan_queue and closeout_queue will all run:
- never ask for a job that is already there (the same build, research or feed); asking for one only moves it up.
- never ask for a replan to remove a duplicate or a forbidden goal: duplicates are merged and forbidden goals
  are taken out by themselves.
- the close-out (closeout_queue, at each finished tier) builds the gear, cable and plate factories that make
  the tier run on its own: do not build those early for a few a minute of the crew's own hand-crafting.
- research order is the plan's: add research only when the labs are idle and the plan has none coming.
- feeding by hand is the rule floor's and the foreman's job: ask for a feed only when no fuel alert covers it.
- interrupt only for an urgent alert; anything else runs after the goal in hand.
- read "plates" and "unfed_ports", not the made/used totals: a plate can be made in surplus and still never reach
  a factory. While an item has unfed ports, a new consumer of it (factory, gear factory, steel column) starves the
  same way and is held back by the crew: fix the routing (remove what walls a port in, or rebuild the starving
  factory next to its source); a mine adds nothing while its output sits in full chests nobody links.
Most of the time nothing needs to change: then send no goals, interrupt false, replan false.

Goals use the strategist's format:
{goals}

Reply with JSON only:
{{"note": "one short sentence", "urgent_goals": [], "interrupt": false, "priority": null, "replan": false}}"""


class LLMAssessor:
    """A fast model that reads the situation and amends the plan. Cost-capped; falls back to rules only."""

    def __init__(self, client: Any, model: str, max_cost_usd: float = 1.0, log=print):
        import openai_bot
        from .brain import GOAL_HELP
        self.client, self.model, self.max_cost, self.log = client, model, max_cost_usd, log
        self._prices = openai_bot
        self.name = f"llm:{model}"
        self.cost_usd = 0.0
        self.calls = 0
        self.system = ASSESS_PROMPT.format(goals=GOAL_HELP)
        self.timeout_s = 45.0

    def decide(self, sit: dict, alerts: list[dict], answered: set[str]) -> dict:
        if self.cost_usd >= self.max_cost:
            return {"note": "assessor budget spent", "urgent_goals": [], "interrupt": False, "replan": False,
                    "priority": None}
        client = self.client.with_options(timeout=self.timeout_s, max_retries=0) \
            if hasattr(self.client, "with_options") else self.client
        resp = client.chat.completions.create(
            model=self.model, response_format={"type": "json_object"},
            messages=[{"role": "system", "content": self.system},
                      {"role": "user", "content": json.dumps(sit, separators=(",", ":"))}])
        self.calls += 1
        u = resp.usage
        if u is not None:
            cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
            self.cost_usd += self._prices.cost_usd(self.model, u.prompt_tokens, cached, u.completion_tokens) or 0.0
        try:
            out = json.loads(resp.choices[0].message.content or "{}")
        except json.JSONDecodeError:
            out = {}
        goals = [g for g in out.get("urgent_goals") or [] if isinstance(g, dict) and g.get("do") in GOAL_DOS][:3]
        pr = out.get("priority")
        return {"note": str(out.get("note", ""))[:240], "urgent_goals": goals, "interrupt": bool(out.get("interrupt")),
                "replan": bool(out.get("replan")), "priority": [str(x) for x in pr][:8] if isinstance(pr, list) else None}


class Supervisor:
    """Runs the rule floor at every checkpoint and the AI assessor in the background, and applies both."""

    REPLAN_GAP_MIN = 5.0          # game minutes between the assessor's replan asks (run 20261120-092648: three
                                  # in 6 minutes, each about a duplicate or a goal a forbid would have taken out)

    def __init__(self, assessor=None, period_game_s: float = 90.0, min_gap_s: float = 15.0, log=print,
                 event=lambda kind, **data: None, doctrine=lambda: "", overseer=None, report_fn=None):
        self.rules = RuleAssessor()
        self.assessor = assessor                      # LLMAssessor or None (rules only)
        # the overseer (overseer.Overseer or None): every few game minutes, everything known, orders above all
        self.overseer, self.report_fn = overseer, report_fn
        self.overseer_pool = ThreadPoolExecutor(max_workers=1) if overseer else None
        self.overseer_pending: Future | None = None
        from .overseer import FIRST_LOOK_MIN
        self.overseer_next_tick = FIRST_LOOK_MIN * 3600   # game tick of its next look (its first: once the
                                                          # opening has run; on a resume, at once)
        self.overseer_last_tick = -1e9                # when its last answer was carried out
        self.overseer_notes: list[dict] = []
        self.period = period_game_s * 60
        self.min_gap = min_gap_s
        self.log, self.event, self.doctrine = log, event, doctrine
        self.pool = ThreadPoolExecutor(max_workers=1) if assessor else None
        self.pending: Future | None = None
        self.last_ask_tick = -1e9
        self.last_ask_wall = 0.0
        self.last_detect = 0.0
        self.alerts: list[dict] = []
        self.seen: set[str] = set()                  # alert keys the AI assessor has already been shown
        self.answered: set[str] = set()              # alert keys the rule floor already answered
        self.replan_reason: str | None = None
        self.notes: list[dict] = []

    def tick(self, crew, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self.last_detect < 3.0:
            return
        self.last_detect = now
        try:                                          # everyone below plans from this game minute's numbers
            fresh = getattr(crew.w, "fresh_stats", None)
            if fresh is not None:
                fresh()
        except Exception as exc:  # noqa: BLE001 - stale numbers are better than a stopped crew
            self.log(f"[supervisor] stats refresh failed: {type(exc).__name__}: {str(exc)[:120]}")
        try:
            self.alerts = detect(crew)
        except Exception as exc:  # noqa: BLE001 - watching must never stop the crew
            self.log(f"[supervisor] detect failed: {type(exc).__name__}: {exc}")
            return
        live = {a["key"] for a in self.alerts}
        self.answered &= live
        self.seen &= live
        # the floor: fuel and power, at once
        d = self.rules.decide({}, self.alerts, self.answered)
        if d["urgent_goals"]:
            self.answered.update(d["keys"])
            self.apply(crew, d, who="rules")
        # the AI assessor: its answer from last time, then a new look when something changed or it is time
        if self.pending is not None and self.pending.done():
            try:
                self.apply(crew, self.pending.result(), who="assessor")
            except Exception as exc:  # noqa: BLE001 - a failed call changes nothing
                self.log(f"[supervisor] assessor failed: {type(exc).__name__}: {str(exc)[:120]}")
            self.pending = None
        self._oversee(crew)
        new = live - self.seen
        tick = crew.w.now_tick()
        due = tick - self.last_ask_tick > self.period or bool(new)     # on schedule, or something new is wrong
        if self.assessor and self.pending is None and due and now - self.last_ask_wall > self.min_gap:
            self.seen |= live
            self.last_ask_tick, self.last_ask_wall = tick, now
            sit = situation(crew, self.alerts, self.doctrine())
            self.pending = self.pool.submit(self.assessor.decide, sit, self.alerts, set(self.answered))

    def apply(self, crew, d: dict, who: str) -> None:
        changed = []
        if d.get("priority") is not None and d["priority"] != crew.foreman.priority:
            crew.foreman.priority = d["priority"]
            changed.append(f"priority {d['priority']}")
        from .overseer import forbidden, rules_end, same_target

        def bare(g):
            return json.dumps({k: v for k, v in g.items() if k != "reflex"}, sort_keys=True, default=str)

        urgent_now = any(a.get("level") == "urgent" for a in self.alerts)
        queued = {bare(g) for g in crew.urgent}
        cur = bare(crew.current["goal"]) if crew.current else None
        ordered = [g for g in crew.urgent if str(g.get("reflex", "")).startswith("overseer")]
        if crew.current and str((crew.current.get("goal") or {}).get("reflex", "")).startswith("overseer"):
            ordered.append(crew.current["goal"])
        goals = list(d.get("urgent_goals") or [])
        front = who == "rules"                         # fuel and power: before the overseer's orders
        if who == "assessor" and ordered and goals:
            # the overseer's orders are running: the assessor does not pile more on top (it re-queued cleanups
            # and research the overseer had already ordered, 15 at a time). But a fix for an urgent alert goes
            # through, one at a time, ahead of the orders (run 20261120-084046: "replace the depleted iron mine"
            # was held back five times over 8 minutes while iron ran out)
            targets = {same_target(g) for g in ordered}
            goals = [g for g in goals if isinstance(g, dict) and same_target(g) not in targets][:1] \
                if urgent_now else []
            d = {**d, "interrupt": False}
            front = bool(goals)
            changed.append("its goals held back: the overseer's orders run first" if not goals
                           else "one urgent fix let through ahead of the overseer's orders")
        mainline = [getattr(crew, "plan_queue", []), getattr(crew, "closeout_queue", [])]   # the run's own work
        for g in goals[:3]:
            if not isinstance(g, dict) or g.get("do") not in GOAL_DOS:
                continue                                 # whoever decided, only goals the crew knows
            g = {k: v for k, v in g.items() if k != "reflex"}
            key = bare(g)
            if key in queued or key == cur:
                continue
            if who != "rules" and forbidden(crew, g) is not None:
                continue                                 # refused when it ran anyway: not queued at all
            if who != "rules":
                from .overseer import floorplan_guard
                plan_why = floorplan_guard(crew, g)
                if plan_why is not None:                 # the zones floorplan: the bus is fixed, not bypassed
                    changed.append(f"{_short(g)}: {plan_why}"[:200])
                    continue
            try:
                from .nodes import consumer_block
                held = consumer_block(crew, g)
            except Exception:  # noqa: BLE001 - stub crews in tests
                held = None
            if held is not None:                         # its item's factories already starve (nodes.py)
                changed.append(f"{_short(g)}: {held[:160]}")
                continue
            goal = {**g, "reflex": f"{who}: {d.get('note', '')}"[:160]}
            from .agenda import agenda_of, now_of
            ag = agenda_of(crew)
            hold = ag.holds(crew, goal, who)
            if hold is not None:                         # vetoed, or the overseer dropped it a moment ago
                ag.record(now_of(crew), who, "refused", goal, hold)
                changed.append(f"{_short(g)}: {hold[:120]}")
                continue
            if who == "assessor":
                # the assessor amends the run, it does not fight it (run 20261120-092648): a job the plan or the
                # close-out already has is moved up, not queued twice (then "replan to drop the duplicate"); a
                # job running or queued already is left alone; research keeps the plan's order
                t = same_target(g)
                if (crew.current and same_target(crew.current.get("goal") or {}) == t) or \
                        any(same_target(q) == t for q in crew.urgent):
                    changed.append(f"already under way: {_short(g)}")
                    continue
                hit = next(((q, i) for q in mainline for i, x in enumerate(q) if same_target(x) == t), None)
                if hit is not None:
                    goal = hit[0].pop(hit[1])            # its own sizing and tag: moved up, done once
                    changed.append(f"moved up: {_short(goal)}")
                    if front:
                        crew.urgent.insert(rules_end(crew.urgent), goal)
                    else:
                        crew.urgent.append(goal)
                    queued.add(bare(goal))
                    ag.record(now_of(crew), who, "moved up", goal)
                    continue
                if g.get("do") == "research" and ((getattr(crew.w, "research", None) or {}).get("current")
                                                  or any(x.get("do") == "research" for q in mainline for x in q[:3])):
                    changed.append(f"research left to the plan: {_short(g)}")
                    continue
            if front:
                at = rules_end(crew.urgent)
                crew.urgent.insert(at, goal)
            else:
                crew.urgent.append(goal)
            queued.add(key)
            changed.append(_short(g))
            ag.record(now_of(crew), who, "propose", goal, d.get("note", ""))
        if who == "assessor" and d.get("interrupt") and not urgent_now:
            d = {**d, "interrupt": False}               # its fixes run after the goal in hand: a pause is for an
            changed.append("no pause without an urgent alert")   # urgent alert (it paused the iron build for
                                                                 # a stone mine, the labs for coal feeds)
        if d.get("interrupt") and crew.urgent and crew.current and not crew.current.get("urgent"):
            from .upkeep import may_interrupt
            keys = d.get("keys") or [a["key"] for a in self.alerts if a["level"] == "urgent"]
            if may_interrupt(crew, keys):
                crew.request_pause(f"{who}: {d.get('note', '')}"[:160])
                crew.last_interrupt = crew._minute()
                changed.append("pause the current goal")
            else:                                     # the fixes wait in the urgent queue: they run right after it
                changed.append("fixes queued for after the goal in hand (frontier or recent pause)")
        tick = crew.w.now_tick()
        if d.get("replan") and not self.replan_reason:
            if who == "assessor" and tick - getattr(self, "last_replan_tick", -1e9) < self.REPLAN_GAP_MIN * 3600:
                changed.append("no replan: the strategist was asked for one minutes ago")
            else:
                self.replan_reason = f"{who}: {d.get('note', '')}"[:160]
                self.last_replan_tick = tick
                changed.append("ask the strategist")
        if changed:
            self.log(f"[supervisor] {who}: {d.get('note', '')[:120]} -> {changed}")
            self.event("assess", who=who, note=d.get("note"), changes=changed)
            self.notes = (self.notes + [{"minute": round(crew.w.now_tick() / 3600, 1), "who": who,
                                         "note": d.get("note"), "changes": changed}])[-10:]

    def _oversee(self, crew) -> None:
        """The overseer: its answer is carried out here (the main thread, a safe point), first of all; its next
        look starts in the background when due."""
        if not self.overseer:
            return
        from .overseer import apply_orders, full_report
        if not self.overseer.history and self.overseer_notes:     # resumed: its earlier word comes back
            self.overseer.history = list(self.overseer_notes[-6:])
        if self.overseer_pending is not None and self.overseer_pending.done():
            fut, self.overseer_pending = self.overseer_pending, None
            try:
                d = fut.result()
                changed = apply_orders(crew, d, log=self.log, event=self.event)
                self.overseer_last_tick = crew.w.now_tick()
                gap = self.overseer.next_gap_min(d)
                self.overseer_next_tick = crew.w.now_tick() + gap * 3600
                entry = {"minute": round(crew.w.now_tick() / 3600, 1), "assessment": str(d.get("assessment", ""))[:300],
                         "changes": changed, "guidance": str(d.get("guidance") or "")[:300],
                         "seconds": d.get("latency_s")}
                self.overseer.history = (self.overseer.history + [entry])[-6:]
                self.overseer_notes = (self.overseer_notes + [entry])[-10:]
            except Exception as exc:  # noqa: BLE001 - a failed look changes nothing; try again next period
                self.log(f"[overseer] failed: {type(exc).__name__}: {str(exc)[:160]}")
                self.overseer_next_tick = crew.w.now_tick() + self.overseer.period_min * 3600
        now = crew.w.now_tick()
        waiting = any(str(g.get("reflex", "")).startswith("overseer") for g in crew.urgent)
        if self.overseer_pending is None and now >= self.overseer_next_tick and waiting                 and now - self.overseer_last_tick < 2 * self.overseer.period_min * 3600:
            self.overseer_next_tick = now + 2 * 3600       # its orders have not started yet: look once they have
        if self.overseer_pending is None and crew.w.now_tick() >= self.overseer_next_tick:
            try:
                base, results = self.report_fn() if self.report_fn else ({}, [])
                done = [{"goal": _short(r["goal"]), "ok": r["ok"], "result": str(r.get("result", ""))[:160]}
                        for r in results[-40:] if str(r["goal"].get("reflex", "")).startswith("overseer")]
                rep = full_report(crew, base, results, self.overseer.history,
                                  extra={"results_of_your_orders": done[-12:], "alerts_now": [
                                      {k: a.get(k) for k in ("level", "what", "fix")} for a in self.alerts],
                                      "assessor_notes": self.notes[-6:]})
            except Exception as exc:  # noqa: BLE001 - a report bug: skip this look
                self.log(f"[overseer] report failed: {type(exc).__name__}: {str(exc)[:160]}")
                self.overseer_next_tick = crew.w.now_tick() + 3600
                return
            self.overseer_next_tick = float("inf")     # set again when the answer comes
            self.overseer_pending = self.overseer_pool.submit(self.overseer.decide, rep)
            self.event("oversee_ask", minute=round(crew.w.now_tick() / 3600, 1))

    def overseer_soon(self, now: float) -> None:
        """Bring its next look forward (the watchdog saw a stall), but not sooner than MIN_GAP_MIN after its last
        answer: its orders need time to show."""
        from .overseer import MIN_GAP_MIN
        if self.overseer and self.overseer_pending is None:
            self.overseer_next_tick = min(self.overseer_next_tick,
                                          max(now, self.overseer_last_tick + MIN_GAP_MIN * 3600))

    def take_replan(self) -> str | None:
        r, self.replan_reason = self.replan_reason, None
        return r

    def close(self) -> None:
        if self.pool:
            self.pool.shutdown(wait=False, cancel_futures=True)
        if self.overseer_pool:
            self.overseer_pool.shutdown(wait=False, cancel_futures=True)
