"""Find what is wrong before repairing it.

Run 20261121-140755 spent 100 game minutes on relinks, and most changed nothing: site 56 (a steel column) was
relinked seven times, each one "1/1 fed", while it stayed starved. "Fed" only meant a belt reached its port; the
belt came from a source that already fed more than it got, so the same source was laid again each time. A relink
fixes a missing or broken belt. It does not fix a source that is dry, stopped or shared too thin, or a belt
with the wrong items on it.

    diagnose(crew, site, item)   the facts behind a starving input and the repair that fits them:
        no-link          a port with no belt                     -> relink (the link step lays it)
        polluted         its belt carries other items            -> purge
        source-gone      its belt starts at a source that was removed  -> relink from a living source
        source-dead      its source mine is mined out            -> that mine rebuilt on fresh ore
        source-unfuelled its source's furnaces have no fuel      -> that source's coal input, diagnosed in turn
        source-stopped   its source site stands still itself     -> that site's own input, diagnosed in turn
        source-short     its source feeds more than it really gets (nominal rate, less what its own machines
                         lack: run 20261121-140755 had a row "making" 37.5/min for 36.3/min of ports while its
                         own furnaces starved)                    -> another source fed in, or more made
        no-effect        relinked before and still starved       -> relink from a different source
        output-full      it waits on its output, not its input   -> no relink (nobody takes what it makes)
        no-fluid         it lacks a fluid, not an item           -> no relink (the pipe and oil alerts say why)
        unknown          nothing measurable is wrong             -> relink once; the planners see the facts

Every relink goes through here first (Crew.run_goal). When the cause is not the belt, the repair that fits is
queued in its place, and the planners see the diagnosis in their reports (diagnoses()).
"""

from __future__ import annotations

STILL_SHARE = 0.35          # a site with fewer of its machines working than this stands still
SAFE_SHARE = 0.85           # a belt drawn above this share of its source's real rate starves its last port
LACKING = ("waiting_for_source_items", "no_fuel", "no_power", "no_minable_resources", "no_ingredients",
           "low_power")      # a source's own machines that make nothing for want of something
NO_EFFECT_MIN = 45.0        # a relink of the same input within this long that left it starved had no effect
WAITING = ("waiting_for_source_items", "item_ingredient_shortage", "waiting_for_ingredients")


BLOCKED = ("waiting_for_space_in_destination", "full_output", "output_full")
NO_RELINK = ("output-full", "no-fluid")


def _blocked(site) -> int:
    return sum((site.health or {}).get(k, 0) for k in BLOCKED)


def _working_share(site) -> float | None:
    total = sum((site.health or {}).values())
    return None if not total else (site.health.get("working", 0) / total)


def _starving(site) -> int:
    return sum((site.health or {}).get(k, 0) for k in WAITING)


def _short(goal: dict) -> str:
    return " ".join(f"{k}={v}" for k, v in goal.items() if k not in ("reflex", "with")) + \
        (f" with {goal['with'].get('module')}" if isinstance(goal.get("with"), dict) else "")


def _links_into(crew, site, item: str) -> list[dict]:
    ends = {tuple(pt["end"]) for pt in site.params.get("ports", []) if pt.get("item") == item}
    return [ln for ln in getattr(crew, "links", []) if tuple((ln.get("port") or {}).get("end") or ()) in ends
            and ln.get("kind") not in ("science", "trunk") and ln.get("boost_into") is None]


def _relinked_before(crew, site_id, item: str) -> bool:
    """Was this input relinked (successfully) within NO_EFFECT_MIN, so a relink now repeats one that did not help?"""
    try:
        from .overseer import same_target
        key = repr(same_target({"do": "relink", "site": site_id, "item": item}))
    except Exception:  # noqa: BLE001
        return False
    now = crew._minute() if hasattr(crew, "_minute") else 0.0
    return any(k == key and ok and now - m <= NO_EFFECT_MIN for k, m, ok in getattr(crew, "fix_log", None) or [])


def real_rate(site, nominal: float) -> float:
    """What a source really gives: its nominal rate less the share of its machines that lack something."""
    total = sum((site.health or {}).values())
    if not total:
        return nominal
    lacking = sum(site.health.get(k, 0) for k in LACKING)
    return nominal * max(0.0, 1.0 - lacking / total)


def _source_input(site) -> str | None:
    """The input a site waits on: an unfed port's item first, else any port's (its machines show waiting)."""
    ports = [pt for pt in site.params.get("ports", []) if pt.get("item") and not pt.get("trunk")]
    unfed = [pt for pt in ports if not pt.get("fed")]
    if unfed:
        return unfed[0]["item"]
    return ports[0]["item"] if ports and _starving(site) else None


def diagnose(crew, site_id, item: str | None = None, depth: int = 0) -> dict:
    """{cause, why, facts, fix, avoid}: fix is the goal that repairs it (None: the relink itself is the repair);
    avoid lists sources a relink must not take again."""
    site = crew.base.get(site_id)
    out = {"cause": "unknown", "why": "", "facts": [], "fix": None, "avoid": []}
    if site is None:
        out.update(cause="no-site", why=f"there is no site {site_id}")
        return out
    ports = [pt for pt in site.params.get("ports", []) if pt.get("item") and not pt.get("trunk")
             and (item is None or pt["item"] == item)]
    if not ports:
        out.update(cause="no-port", why=f"site {site.id} has no input port" + (f" for {item}" if item else ""))
        return out
    item = item or ports[0]["item"]
    facts = out["facts"]
    if any(not pt.get("fed") for pt in ports):
        out.update(cause="no-link", why=f"its {item} port has no belt: the link step lays one")
        return out
    # a belt with the wrong items on it: cleaning it (and stopping what feeds them) is the repair
    try:
        from . import purity
        bad = [a for a in purity.check(crew) if a["key"] == f"belt-foreign-{site.id}-{item}"]
    except Exception:  # noqa: BLE001 - a stub crew, out of range: no verdict on the belt
        bad = []
    if bad:
        out.update(cause="polluted", why=bad[0]["what"][:200], fix={"do": "purge", "site": site.id, "item": item})
        return out
    links = _links_into(crew, site, item)
    roots = []
    for ln in links:
        root = crew._coal_root(ln) if hasattr(crew, "_coal_root") else tuple(ln.get("source") or ())
        if root:
            roots.append(tuple(root))
    for root in dict.fromkeys(roots):
        src = crew._source_site(root, item) if hasattr(crew, "_source_site") else None
        gone = crew.gone_roots() if hasattr(crew, "gone_roots") else {}
        if src is None and root in gone:
            # its source came down but its belt was kept: a new source fed into its side (spliced) is judged by
            # what it gets (below); before that, the link step feeds one in. No relink: that unplugs its taps.
            if crew.spliced(root):
                continue
            out.update(cause="source-gone",
                       why=f"its {item} belt's source at {list(root)} came down; the belt stays and the link step "
                           "feeds a new source into its side", fix={"do": "link"})
            return out
        if src is None:
            out.update(cause="source-gone", avoid=[root],
                       why=f"its {item} belt starts at {list(root)}, where no source stands any more (rebuilt or "
                           "removed): relinked from a living source")
            return out
        from .upkeep import MINES, depleted_fix, mine_depleted
        if src.kind in MINES and mine_depleted(src):
            out.update(cause="source-dead",
                       why=f"its {item} comes from site {src.id} ({src.kind}), which is mined out",
                       fix=depleted_fix(crew, src))
            return out
        if src.health.get("no_fuel") and src.params.get("fuel") != "hand" and depth < 2:
            coal = [pt for pt in src.params.get("ports", []) if pt.get("item") == "coal"]
            if coal:
                inner = diagnose(crew, src.id, "coal", depth + 1)
                out.update(cause="source-unfuelled",
                           why=f"its source site {src.id} ({src.kind}) has {src.health['no_fuel']} machine(s) "
                               "without fuel" + (f" ({inner['why'][:120]})" if inner.get("why") else ""),
                           fix=inner["fix"] or {"do": "relink", "site": src.id, "item": "coal"})
                return out
        share = _working_share(src)
        if share is not None and share < STILL_SHARE and src.kind not in MINES:
            facts.append(f"source site {src.id} ({src.kind}) works at {share:.0%}")
            if src.health.get("no_power"):
                out.update(cause="source-stopped", why=f"its source site {src.id} has no power",
                           fix={"do": "connect", "site": src.id})
                return out
            need = _source_input(src)
            if need and depth < 2:
                inner = diagnose(crew, src.id, need, depth + 1)
                fix = inner["fix"] or {"do": "relink", "site": src.id, "item": need}
                out.update(cause="source-stopped",
                           why=f"its source site {src.id} ({src.kind}) stands still itself, waiting on {need}"
                               + (f" ({inner['why'][:120]})" if inner.get("why") else ""),
                           fix=fix)
                return out
    budget = crew._budget(item) if hasattr(crew, "_budget") else {}
    for root in dict.fromkeys(roots):
        load, nominal = budget.get(root) or [0.0, 0.0]
        src = crew._source_site(root, item) if hasattr(crew, "_source_site") else None
        supply = real_rate(src, nominal) if src is not None else nominal
        if load > supply * SAFE_SHARE + 0.5:
            facts.append(f"source {list(root)} really gives ~{supply:.0f}/min (nominal {nominal:.0f}) and its "
                         f"belts feed {load:.0f}/min")
            spare = crew._spare(item) if hasattr(crew, "_spare") else []
            may = crew._may_retry(("boost",) + root) if hasattr(crew, "_may_retry") else True
            if spare and may:
                fix = {"do": "link"}
                how = f"{len(spare)} unused {item} source(s) are fed into that belt"
            else:
                from .supply import supply_goal
                fix = supply_goal(crew, item, max(load - supply * SAFE_SHARE, 0.3 * load))
                how = "more of it is made, then fed into that belt" if fix else "nothing can make more yet"
            out.update(cause="source-short",
                       why=f"its {item} source gets {supply:.0f}/min but feeds {load:.0f}/min: {how}", fix=fix)
            return out
    if depth == 0 and _blocked(site) > max(1, _starving(site)) and True:
        out.update(cause="output-full",
                   why=f"{_blocked(site)} of its machines wait for room for their output and {_starving(site)} for "
                       "input: nobody takes what it makes, and a relink of its input changes nothing")
        return out
    if depth == 0 and site.health.get("fluid_ingredient_shortage", 0) >= max(1, _starving(site)):
        out.update(cause="no-fluid", why="its machines lack a fluid (crude oil, water, a product), not an item: "
                                         "the pipe, pump and oil alerts say where it stops")
        return out
    if _relinked_before(crew, site.id, item):
        out.update(cause="no-effect",
                   why=f"its {item} input was relinked within {NO_EFFECT_MIN:.0f} min and it still starves: "
                       "this time from a different source",
                   avoid=list(dict.fromkeys(roots)))
        return out
    out["why"] = "no measurable cause (belt there, source running and not overdrawn): relinked once"
    return out


RELINK_CAUSES = ("no-link", "no-effect", "unknown", "source-gone")


def diagnoses(crew, limit: int = 6) -> list[dict]:
    """For the planners: each starving consumer's diagnosis, so they order the repair that fits, not a relink.
    Every input of a site is looked at; the first with a measurable cause is the one reported."""
    out = []
    for s in crew.base.sites:
        if s.kind in ("electric_mine", "burner_mine", "outpost") or not (_starving(s) or s.health.get(
                "fluid_ingredient_shortage")):
            continue
        share = _working_share(s)
        if share is None or share >= 0.6:
            continue
        items = list(dict.fromkeys(pt["item"] for pt in s.params.get("ports", []) if pt.get("item")
                                   and not pt.get("trunk"))) or [None]
        best = None
        for need in items:
            try:
                d = diagnose(crew, s.id, need)
            except Exception:  # noqa: BLE001 - a report line is never worth a crash
                continue
            if best is None or (best[1]["cause"] == "unknown" and d["cause"] != "unknown"):
                best = (need, d)
            if d["cause"] in NO_RELINK:
                break
        if best is None:
            continue
        need, d = best
        if d["cause"] == "no-port":
            continue
        out.append({"site": s.id, "kind": s.kind, "input": need or "fluid", "cause": d["cause"], "why": d["why"][:200],
                    "repair": d["fix"] or ({"do": "relink", "site": s.id, "item": need}
                                           if d["cause"] in RELINK_CAUSES else None)})
        if len(out) >= limit:
            break
    return out


def route(crew, goal: dict) -> tuple[dict | None, str, list]:
    """For a relink goal: (the goal to queue instead or None, a note, sources to avoid)."""
    d = diagnose(crew, goal.get("site"), goal.get("item"))
    if d["cause"] in NO_RELINK:
        return {"skip": True}, f"diagnosed ({d['cause']}): {d['why']}", []
    fix = d["fix"]
    if fix is not None and not (fix.get("do") == "relink" and fix.get("site") == goal.get("site")):
        return fix, f"diagnosed ({d['cause']}): {d['why']}", d["avoid"]
    return None, f"diagnosed ({d['cause']}): {d['why']}", d["avoid"]
