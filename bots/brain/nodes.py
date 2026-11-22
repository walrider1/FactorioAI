"""Inputs and outputs one by one: which factory ports get nothing and why, which sources nobody draws from, and
what of each plate really reaches a factory. Phase 0 of docs/logistics-plan.md.

    port_report(crew)        every input port no belt feeds, with its state and why, and every stranded source
                             (output chests nobody links that back their site up)
    plate_ledger(crew)       per plate: made, what the belts carry to factories, what unfed ports want, what sits
                             stranded, so a planner never reads "169 made vs 148 used" as "enough"
    consumer_block(crew, g)  why a build that eats an item must wait (that item's factories already starve), or None

Run 20261120-092648 (minute 334): 16 of 26 iron ports unfed and 6 full iron chests at site 25 nobody drew from,
while every planner saw a surplus and asked for another green-science factory, which starved too.
"""

from __future__ import annotations

import json

PLATES =("iron-plate", "copper-plate", "steel-plate", "stone-brick")
SOURCE_KINDS = ("electric_mine", "smelter_column", "burner_mine")
BACKED_UP = ("waiting_for_space_in_destination", "full_output")
CONSUMERS = ("factory", "smelter_column", "workshop")
GATE_ESCAPE_MIN = 30.0         # a held-back build asked for this long goes ahead anyway (no deadlock for good)
GATE_MIN_PORTS = 1             # unfed ports of an item (blocked or without a source) before its consumers wait


def _minute(crew) -> float:
    try:
        return crew.w.now_tick() / 3600
    except Exception:  # noqa: BLE001 - stub worlds
        return 0.0


def _port_state(crew, site, pt) -> dict:
    """One unfed port: blocked (the link step tried and failed), no_source (nothing with spare to draw on), or
    pending (a source exists and the link step has not tried yet)."""
    key = tuple(pt["end"])
    f = (getattr(crew, "_link_fails", None) or {}).get(key)
    item, need = pt["item"], float(pt.get("per_minute") or 0)
    out = {"site": site.id, "kind": site.kind, "item": item, "per_minute": round(need, 1), "at": list(key)}
    if f:
        return {**out, "state": "blocked", "failed": f.get("n", 1), "why": str(f.get("reason", ""))[:140]}
    try:
        srcs = crew._free_outputs(item, site, need)
        taps = crew._belt_taps(item, site, need)
    except Exception:  # noqa: BLE001 - stub crews
        srcs, taps = [], []
    if not srcs and not taps:
        return {**out, "state": "no_source",
                "why": f"no {item} source has {need:.0f}/min to spare (each mine chest feeds one link)"}
    return {**out, "state": "pending", "why": "a source is free: the next link step tries it"}


def unfed_ports(crew) -> list[dict]:
    """Every item input port (fuel and trunk ports aside) that no belt feeds."""
    try:
        ports = crew.item_ports()
    except Exception:  # noqa: BLE001
        return []
    return [_port_state(crew, s, pt) for s, pt in ports if not pt.get("fed") and not pt.get("fuel")]


def stranded(crew) -> list[dict]:
    """Output chests no link draws from, on sites whose machines wait for room: product made and left there."""
    used = {tuple(ln["source"]) for ln in getattr(crew, "links", []) or []}
    try:                                       # only what some factory wants: ore chests the crew mines into are
        wanted = {pt["item"] for _, pt in crew.item_ports()} | set(PLATES)     # its own stock, not lost supply
    except Exception:  # noqa: BLE001
        wanted = set(PLATES)
    out: dict[tuple, dict] = {}
    for s in crew.base.sites:
        if s.kind not in SOURCE_KINDS or not any(s.health.get(k, 0) for k in BACKED_UP):
            continue
        for x, y, what in s.chests:
            if (x, y) in used or what in getattr(crew, "TAGS", ()) or what not in wanted:
                continue
            try:
                rate = float(crew._source_rate((x, y), what))
            except Exception:  # noqa: BLE001
                rate = 0.0
            row = out.setdefault((s.id, what), {"site": s.id, "item": what, "chests": 0, "per_minute": 0.0})
            row["chests"] += 1
            row["per_minute"] = round(row["per_minute"] + rate, 1)
    lines = getattr(crew, "lines", None)
    for s, ln in (lines() if callable(lines) else []):       # a line whose links draw less than it carries
        if not any(s.health.get(k, 0) for k in BACKED_UP):
            continue
        try:
            load, supply = crew._budget(ln["item"]).get(tuple(ln["source"])) or [0.0, crew.line_rate(ln, s)]
        except Exception:  # noqa: BLE001
            load, supply = 0.0, float(ln.get("rate") or 0)
        if supply - load > 1.0:
            out[(s.id, ln["item"])] = {"site": s.id, "item": ln["item"], "chests": 0, "line": True,
                                       "per_minute": round(supply - load, 1)}
    return sorted(out.values(), key=lambda r: -r["per_minute"])


def port_report(crew) -> dict:
    ports = unfed_ports(crew)
    lost = stranded(crew)
    by_item: dict[str, dict] = {}
    for p in ports:
        row = by_item.setdefault(p["item"], {"blocked": 0, "no_source": 0, "pending": 0, "sites": set()})
        row[p["state"]] += 1
        row["sites"].add(p["site"])
    lines = []
    for item, row in sorted(by_item.items()):
        n = row["blocked"] + row["no_source"] + row["pending"]
        parts = [f"{k.replace('_', ' ')} {row[k]}" for k in ("blocked", "no_source", "pending") if row[k]]
        lines.append(f"{n} {item} port(s) unfed ({', '.join(parts)}) at sites {sorted(row['sites'])}")
    for r in lost[:6]:
        if r.get("line"):
            lines.append(f"the {r['item']} line of site {r['site']} backs up: ~{r['per_minute']:.0f}/min nobody draws "
                         "(a port it can reach takes it)")
        else:
            lines.append(f"{r['chests']} {r['item']} chest(s) at site {r['site']} full with no link "
                         f"(~{r['per_minute']:.0f}/min made and left there)")
    return {"unfed_ports": ports[:24], "unfed_count": len(ports), "stranded": lost[:12], "summary": lines}


def plate_ledger(crew) -> list[dict]:
    """Per plate the base makes or wants: made (game statistics), what links carry to ports, what fed ports ask,
    what unfed ports want, what sits stranded."""
    try:
        from .assess import flows
        made = {f["item"]: f["made"] for f in flows(crew)}
    except Exception:  # noqa: BLE001
        made = {}
    ports = unfed_ports(crew)
    lost = stranded(crew)
    out = []
    for item in PLATES:
        try:
            budget = crew._budget(item)
        except Exception:  # noqa: BLE001
            budget = {}
        carried = sum(min(load, supply) for load, supply in budget.values())
        asked = sum(load for load, _ in budget.values())
        unfed = [p for p in ports if p["item"] == item]
        want = sum(p["per_minute"] for p in unfed)
        left = [r for r in lost if r["item"] == item]
        idle = sum(r["per_minute"] for r in left)
        if not (made.get(item) or carried or want or idle):
            continue
        row = {"item": item, "made_per_min": made.get(item), "belted_to_factories": round(carried, 1),
               "fed_ports_ask": round(asked, 1), "unfed_ports": len(unfed), "unfed_want": round(want, 1),
               "stranded_per_min": round(idle, 1), "stranded_at": sorted({r["site"] for r in left})}
        text = (f"{item}: made {made.get(item, '?')}/min; belts carry ~{carried:.0f}/min to factories"
                + (f" (their ports ask {asked:.0f})" if asked > carried + 1 else ""))
        if unfed:
            text += f"; {len(unfed)} unfed port(s) want {want:.0f}/min more"
        if idle:
            text += f"; ~{idle:.0f}/min sits in full chests nobody links (sites {row['stranded_at']})"
        row["text"] = text
        out.append(row)
    return out


def ledger(crew) -> list[dict]:
    """Every line (docs/logistics-plan.md, phase 4): what it carries, what its links take, what is left, and
    which sites draw on it, so a planner reasons in lines and ports instead of totals."""
    lines = getattr(crew, "lines", None)
    if not callable(lines):
        return []
    out = []
    for s, ln in lines():
        try:
            load, supply = crew._budget(ln["item"]).get(tuple(ln["source"])) or [0.0, crew.line_rate(ln, s)]
        except Exception:  # noqa: BLE001
            load, supply = 0.0, float(ln.get("rate") or 0)
        users = sorted({q.get("to_site") for q in getattr(crew, "links", []) or []
                        if tuple(q.get("source") or ()) == tuple(ln["source"]) or q.get("tap_of") is ln})
        out.append({"site": s.id, "item": ln["item"], "per_minute": round(supply, 1), "drawn": round(load, 1),
                    "spare": round(supply - load, 1), "feeds_sites": users, "taps": ln.get("taps", 0),
                    "start": list(ln["start"])})
    return sorted(out, key=lambda r: (r["item"], -r["spare"]))


# ------------------------------------------------------------------ the gate on new consumers
def goal_inputs(crew, goal: dict) -> set[str]:
    """Items a build would draw through input ports (the plates a plate-fed factory takes, a steel column's
    iron); empty for anything that is not a consumer."""
    g = goal.get("with") if goal.get("do") == "replace" else goal
    if not isinstance(g, dict) or g.get("do", "build") != "build" or g.get("module") not in CONSUMERS:
        return set()
    if g["module"] == "smelter_column":
        return {g["resource"]} if g.get("resource") in PLATES else set()
    if g["module"] == "factory":
        try:
            from .modules import MODULES
            supplied, _ = MODULES["factory"].supplied_for(crew.kn, {k: v for k, v in g.items() if k not in ("do", "module")})
            return {i for i in supplied if i != g.get("item")}
        except Exception:  # noqa: BLE001 - unknown item: the build fails on its own with the real choices
            return set()
    raw = g.get("inputs") or []
    out = {str(x) for x in (raw if isinstance(raw, (list, tuple, set)) else [raw])}
    rec = (getattr(crew.kn, "recipes", None) or {}).get(g.get("item")) or {}   # a workshop: its recipe's plates
    out |= {i.get("name") for i in rec.get("ingredients") or [] if i.get("name") in PLATES}
    return out


def starving(crew) -> dict[str, list[dict]]:
    """Items whose factories already wait on a belt that will not come by itself: blocked ports, or ports with no
    source to spare (pending ones get their link at the next link step)."""
    out: dict[str, list[dict]] = {}
    for p in unfed_ports(crew):
        if p["state"] in ("blocked", "no_source"):
            out.setdefault(p["item"], []).append(p)
    return {k: v for k, v in out.items() if len(v) >= GATE_MIN_PORTS}


PRODUCER_KINDS = ("electric_mine", "burner_mine", "factory", "workshop", "smelter_column")
FULL_SHARE = 0.6               # a producer whose machines are mostly output-blocked: nothing takes what it makes
FULL_MIN_SITES = 2             # full producers of an item before more of them wait


def _product(kind: str, params: dict) -> str | None:
    """What a site (or a build goal) of this kind puts out: an ore into chests, plates from furnaces or a line,
    or a factory's item."""
    if kind in ("electric_mine", "burner_mine"):
        res = params.get("resource")
        if res == "coal" or params.get("into") == "chest":
            return res
        return {"iron-ore": "iron-plate", "copper-ore": "copper-plate", "stone": "stone-brick"}.get(res, res)
    return params.get("item")


def linkable(crew, site, item: str) -> bool:
    """Can a link start at this site for `item` (an output chest of it, or its line's tail: Crew.outputs)? A
    burner mine smelting into stone furnaces has neither: its plates wait in the furnaces for the crew's hands."""
    try:
        return any(s is site for _, _, s in crew.outputs(item))
    except Exception:  # noqa: BLE001 - stub crews: its chests and line decide
        return any(what == item for *_, what in getattr(site, "chests", [])) or \
            ((site.params.get("line") or {}).get("item") == item)


def _full(site) -> bool:
    h = getattr(site, "health", None) or {}
    total = sum(h.values())
    blocked = h.get("waiting_for_space_in_destination", 0) + h.get("full_output", 0)
    return bool(total) and blocked / total >= FULL_SHARE


def full_producers(crew, item: str) -> list:
    """Sites that make `item`, stand mostly output-blocked (their last patrol's machine states) and could be
    linked (linkable): only those mean "connect what you have before building more". A full producer nothing
    can draw from (unlinkable_full) says nothing about demand. Run 20261122-133537: two burner copper mines into
    stone furnaces stood full, so six electric copper mines were held back over 10 minutes while red science ran
    at a fifth of its rate for copper ("connect them", which the link step cannot)."""
    return [s for s in crew.base.sites if s.kind in PRODUCER_KINDS and _product(s.kind, s.params) == item
            and _full(s) and linkable(crew, s, item)]


def unlinkable_full(crew, item: str) -> list:
    """Full producers of `item` no link can start from (burner mines into furnaces), oldest first: their output
    reaches nothing, so they are replaced by electric mines, not connected."""
    out = [s for s in crew.base.sites if s.kind in PRODUCER_KINDS and _product(s.kind, s.params) == item
           and _full(s) and not linkable(crew, s, item)]
    return sorted(out, key=lambda s: getattr(s, "built_minute", 0) or 0)


def swap_goal(site) -> dict | None:
    """The replace that puts electric drills (into furnaces whose plates leave by chest or line) where a burner
    mine stands full; None for anything but a burner mine on iron, copper or stone."""
    r = site.params.get("resource")
    if site.kind != "burner_mine" or r not in ("iron-ore", "copper-ore", "stone"):
        return None
    n = max(2, min(10, int(site.params.get("count", 2))))
    return {"do": "replace", "site": site.id, "force": True,
            "with": {"do": "build", "module": "electric_mine", "resource": r, "count": n, "into": "furnace"}}


REPAIRS = ("relink", "purge", "replace", "upgrade")   # repairs of one site (a bare "link" lays new links)
REPEAT_MAX = 2                 # a repair that finished this often on one target lately...
REPEAT_WINDOW_MIN = 45.0       # ...within these game minutes, and is asked for again, did not work


def repeat_block(crew, goal: dict) -> str | None:
    """Why a repair must not run again: the same repair on the same target already finished REPEAT_MAX times in
    the last REPEAT_WINDOW_MIN game minutes, and here it is asked for again, so what it fixed came back. Run
    20261121-140755: site 56's iron input was relinked six times ("1/1 fed" each time) while the column stayed
    starved, and site 83's coal belt was purged six times while its inserter kept feeding copper onto it."""
    if goal.get("do") not in REPAIRS:
        return None
    from .overseer import same_target
    key = repr(same_target(goal))
    now = _minute(crew)
    done = [m for k, m, ok in getattr(crew, "fix_log", None) or [] if k == key and ok and now - m <= REPEAT_WINDOW_MIN]
    if len(done) < REPEAT_MAX:
        return None
    return (f"held back: this repair already ran {len(done)}x in the last {REPEAT_WINDOW_MIN:.0f} game min and "
            "what it fixed came back, so it does not work. Find the cause and fix that instead (more supply for a "
            "short belt, rebuild the site where its inputs are, take up what feeds a belt the wrong items)")


def surplus_block(crew, goal: dict) -> str | None:
    """Why a build that adds more of an item must wait: FULL_MIN_SITES producers of it already stand full, with
    nothing taking what they make. Run 20261121-140755: six ore-into-chest mines (12 drills) sat with full chests
    while more were ordered ("build electric_mine iron-ore into chest" four times). A replace is never held, and a
    build held GATE_ESCAPE_MIN game minutes goes ahead. Full producers no link can start from (burner furnace
    mines) never hold a build: when they are all there is, the build goes ahead and the swap alert
    (assess.burner_swap_alerts) replaces them; when they stand beside linkable ones, the hint names their swap."""
    if goal.get("do") != "build" or goal.get("module") not in PRODUCER_KINDS:
        return None
    item = _product(goal["module"], goal)
    if not item:
        return None
    full = full_producers(crew, item)
    held = getattr(crew, "_surplus_held", None)
    if held is None:
        held = crew._surplus_held = {}
    key = f"{goal['module']}:{item}"
    if len(full) < FULL_MIN_SITES:
        held.pop(key, None)
        return None
    now = _minute(crew)
    first = held.setdefault(key, now)
    if now - first >= GATE_ESCAPE_MIN:
        held.pop(key, None)
        return None
    dead = [s for s in unlinkable_full(crew, item) if swap_goal(s) is not None]
    swap = (f". Sites {[s.id for s in dead][:6]} stand full too but no belt can take from them (burner furnaces): "
            f"replace them, e.g. {json.dumps(swap_goal(dead[0]))}" if dead else "")
    return (f"held back: {len(full)} sites that make {item} already stand full (sites "
            f"{sorted(s.id for s in full)[:8]}): nothing takes what they make, so another one would stand full too. "
            f"Connect them to what needs {item} first (link, or a factory beside them)" + swap
            + f"; asked again after {GATE_ESCAPE_MIN - (now - first):.0f} more game min it goes ahead")


def consumer_block(crew, goal: dict) -> str | None:
    """Why this build must wait, or None: its output already piles up unused (surplus_block), or what it eats
    already starves elsewhere. A replace that moves a starving consumer (its old site has unfed ports of that
    item) is the fix, never held. A build held for GATE_ESCAPE_MIN game minutes goes ahead."""
    for check in (repeat_block, surplus_block):
        why = check(crew, goal)
        if why is not None:
            return why
    wants = goal_inputs(crew, goal)
    if not wants:
        return None
    from .overseer import same_target
    key = repr(same_target(goal))
    held = getattr(crew, "_gate_held", None)
    if held is None:
        held = crew._gate_held = {}
    hungry = starving(crew)
    hit = sorted(i for i in wants if i in hungry)
    if goal.get("do") == "replace" and hit:
        old = goal.get("site")
        if any(p["site"] == old for i in hit for p in hungry[i]):
            hit = []
    if not hit:
        held.pop(key, None)                    # the starving ended: a later hold starts its clock afresh
        return None
    now = _minute(crew)
    first = held.setdefault(key, now)
    if now - first >= GATE_ESCAPE_MIN:
        held.pop(key, None)
        return None
    item = max(hit, key=lambda i: len(hungry[i]))      # the worst-starved input names the reason
    ports = hungry[item]
    sites = sorted({p["site"] for p in ports})
    lost = [r for r in stranded(crew) if r["item"] == item]
    idle = sum(r["per_minute"] for r in lost)
    return (f"held back: {len(ports)} {item} port(s) at sites {sites} already get no belt"
            + (f" while ~{idle:.0f}/min of it sits in full chests at sites {sorted({r['site'] for r in lost})}"
               if idle else "")
            + f". A new {item} consumer would starve the same way: connect the supply first (remove what walls "
            "those ports in, or rebuild a starving factory next to its source)"
            + f"; asked again after {GATE_ESCAPE_MIN - (now - first):.0f} more game min it goes ahead")
