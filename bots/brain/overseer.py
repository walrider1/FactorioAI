"""The overseer: every few game minutes a deep-thinking model reads everything the bot knows about the game,
finds what is wrong or wasteful, and gives orders that are carried out at once, above everyone else.

The rest of the brain is made of narrow parts (strategist, assessor, rule floor, fixer, foreman), each good at
its own job and blind to the others' mistakes: drills that stand idle for an hour because their furnaces have
no coal, an outpost goal that built 36 unfed smelting columns, belts that stopped carrying, power built for
a load that never comes. A person looking at the map sees these in seconds. The overseer is that look.

    Overseer(client, model, effort="high", period_min=7.0)   one call per period, in the background
    full_report(crew, base_report, ...)                       everything known: sites, every machine's state,
                                                              links, plans, history, previous orders
    apply_orders(crew, orders, ...)                           orders first, in the main thread, at a safe point

It acts only on a problem it can name; "all is well, no orders" is the normal answer. Its first look is at
FIRST_LOOK_MIN, not minute 0.

Its authority (each order is optional):
    orders        up to ORDER_MAX goals, after the rule floor's fuel/power fixes and before everyone else's;
                  orders already running or queued are skipped; a forced remove still meets the crew's guard
    interrupt     pause the goal in hand at the next safe point, frontier builds included
    drop_current  the goal in hand is not resumed after the orders (it was the mistake); a build or replace
                  DROP_TO_PAUSE_MIN in (or a close-out goal) is paused instead, its work kept
    clear_urgent  drop the fixes others queued before adding its orders (never the rule floor's)
    plan          replace the strategist's queued plan, only with a plan_why (a plan the strategist was asked
                  for before is dropped)
    forbid        goals nobody may run for a while (a partial goal matches every goal that has those keys):
                  stops loops and wrong habits, also inside other builds (a build's own nested builds); at most
                  FORBID_MAX, never on a close-out goal; matching goals leave the queues at once
    allow         lift forbids (by index in "forbidden", or all)
    priority      the foreman's order after coal mines and power
    doctrine      the strategist's standing doctrine, overwritten
    guidance      standing instructions every planner reads until the next overseer call (binding for them)
    next_check_minutes   when to look again (3-15 game minutes)
"""

from __future__ import annotations

import json
import time
from collections import Counter
from typing import Any

from .assess import GOAL_DOS, _short, flows
from .upkeep import PLAN_STARVE_MIN, plan_waited_min

ORDER_MAX = 3                  # orders per look: 8 at once locked everyone else out for 25+ minutes while the
                               # rule floor's coal feeds waited behind them (run 20261120-084046)
FORBID_MAX = 3                 # active forbids at most: the oldest go first (20 at once boxed the bot in; 8 still
                               # banned red science's own gear and cable factories and looped the planners)
MIN_GAP_MIN = 4.0              # game minutes after its last answer before an early look (watchdog): its orders
                               # need time to run; two looks 0.1-1 min apart only said "not run yet" (run 20261120-023103)
FIRST_LOOK_MIN = 10.0          # its first look: at minute 0 there is nothing to oversee; it only re-ordered the
                               # strategist's first goal (a duplicate coal mine) and replaced its plan (run 20261120-084046)
GAP_RANGE = (5.0, 20.0)        # what it may ask for as its next look
PLAN_MAX = 10
FORBID_MAX_MIN = 30.0
FORBID_DEFAULT_MIN = 10.0
DROP_TO_PAUSE_MIN = 3.0        # a build or replace this many game minutes in is paused, not dropped: its work stays
                               # (run 20261120-023103: 8 drops threw away 42 game minutes, two of them by mistake)
RULES = "rules:"               # the rule floor's fuel and power fixes: before its orders, never cleared


OVERSEE_PROMPT = """You are the overseer of a Factorio AI that is racing to launch a rocket. You have absolute authority
over it. Below you, a strategist plans the build order, a fast assessor amends the plan, a rule floor answers
fuel and power alerts, a fixer troubleshoots repeated failures, a foreman refuels and hauls, and a scripted crew
carries out one goal at a time. Each is narrow and none sees the others' mistakes. You see everything, every few
game minutes, and you think it through properly before you act.

Your job: find what is wrong, wasted or stupid in the base and in what the bot is doing, and fix it now.
Look hard at, among others:
- machines that stand still, and the real cause behind them. "machines" gives every site's buildings by name and
  state: electric drills "waiting_for_space_in_destination" behind stone furnaces with "no_fuel" are a coal
  problem, not a drill problem; factories "waiting_for_source_items" mean an input is not arriving (unfed port,
  stuck link, short belt, empty source); idle drills on a patch hold ore tiles other mines could use.
- the "idle" census: how much of each machine type works. A third of the drills idle is a big waste.
- goals that loop, run for a very long time, or build the same thing again and again (the current goal's
  minutes, recent_results, failure counts, churn hints, sites of one kind piling up unfed).
- repair_diagnoses: for each starving site, what is really wrong and the repair that fits (a dry, stopped or
  overdrawn source, a polluted belt, a missing belt). Order that repair, not a relink: a relink only lays the belt
  again, from the same source. The crew diagnoses every relink and swaps in the fitting repair anyway.
- belts: unfed ports, stuck links and why, short coal belts. A port nobody can belt to needs its consumer moved
  (replace it with the same build, which goes on open ground near its inputs) or a new source next to it.
- power: capacity against what the network really draws (power.demand_kw; demand_source says whether the game
  measured it). Do not build power for full_load_kw. Too little power is urgent; far too much is wasted iron.
- coal: mined against burned, boilers and furnace rows fed by hand that the foreman cannot keep up with.
- supply of anything (plates, coal, stone, steel, gears, circuits, later oil products): made against used in
  flows. Size every supply build to the measured gap, never by habit: the gap is used minus made a minute
  (production flows, alerts "used X/min, made Y/min"), plus about 30% headroom. An electric_mine row takes 2-12
  drills (each ~19 plates, 15 bricks or 30 ore/coal/stone a minute), a burner_mine 2-8 (15 ore a minute), a steel
  smelter_column 2-24 furnaces (3.75 steel a minute each), a factory any per_minute. A gap of 70 iron plates a
  minute is one row of 5 drills, not another row of 4; a gap bigger than one build gets the biggest build now and
  more next time. An alert's "fix" is already sized this way.
- research: labs idle with packs available, research stuck.
- the plan: is the strategist's queue the right next thing? Are the assessor's fixes helping or churning?
- your own previous orders (previous_overseer): did they work? Do not repeat what failed; change approach.

Progress first. The run is judged by how far it gets (the next science, the next research, the rocket), not by how
tidy the base is. The parts on the way to the next stage must work properly: the plan's next build, the inputs and
fluids of the sites that make the next science, the labs and their packs. Fix those fully and first. Everything
else gets the least upkeep that keeps it from blocking progress: idle drills, a slow mine, a site a little short,
mines running low while others have spare (81% of drills idle is a reason NOT to rebuild mines) are left alone.
Choose the cheapest fix that works: a relink or a trunk before a rebuild, feeding a few items by hand before a new
factory, one targeted repair before several. Never order upkeep "while waiting": the plan runs instead.

When to act at all:
- Act only on a problem you can point to in the report: an alert, machines standing still, a measured supply
  gap, a goal that fails, loops or runs far too long, a plan whose next step is wrong. Being asked is not a reason
  to act: you are asked on a timer, not because something broke.
- When the base runs and the bot is already on the right thing (the goal in hand, queues.urgent, the plan),
  reply with no orders, no forbids, plan null, interrupt false, and say so in the assessment. That is the normal
  answer. Never order again what is already running or queued, and never re-order the queue for taste.
- The rule floor's fuel and power fixes ("rules:" in queues.urgent) run before your orders and are never cleared.
- Supply is judged by "plates" and "ports", not by flows: made-vs-used can show a surplus while "ports" lists
  factories no belt reaches and sources whose chests sit full. That is a routing problem: more mines or another
  consumer of that item fix nothing (the crew holds such builds back while the item's ports starve). Fix the
  route: remove what walls a port in, {{"do": "relink", "site": id}} to lay one site's input belts again (from a
  line built since, say), or replace the starving factory with one next to its source. A link (and a relink)
  reaches only sources within 220 tiles: when the spare sources are farther, the fix is {{"do": "trunk", "site":
  id, "item": item}} (one long belt, a goal of its own) or a nearer mine, never another relink. "lines" lists each
  plate line, what it carries, what is drawn and what is left: a new factory belongs where a line has spare.

How to act (only for a problem named above):
- orders: at most 3 concrete goals in the order they must run, done before the planners' work. Undo mistakes
  with remove, replace a badly placed or starved site with the same build elsewhere, feed, link, connect, build
  what is missing. "force": true only passes the sealed-site guard (a sealed site that is useless); it never
  removes the last source of a plate, the only power station, the researching labs, the last coal mine or a
  working site built minutes ago. Prefer automation over hands, but feeding a starved row by hand now is right
  when that is the quick fix.
- interrupt: true to pause the goal in hand at the next safe point (frontier builds too). drop_current: true when
  the goal in hand is itself the mistake (it is then not resumed). A build or replace that has run 3 minutes or
  more is paused instead (its work is kept); forbid it as well if it must not come back. A goal in hand marked
  "protected" is the plan's own goal after the plan waited {starve:.0f} minutes behind fixes ("plan_waited_min"
  counts toward that): it is neither paused nor dropped, your orders run right after it. Keep the urgent queue
  short: everything in it delays the plan.
- Spare capacity on paper is not supply: a line whose mines run dry ("ore_outlook" minutes_left, rebuild alerts)
  has no spare. Never forbid building, mining or replacing a mine of something that is short ("short_supply":
  made under 90% of what its ports want, or ports with no source): such forbids are refused, and a goal that adds
  supply of a short item runs whatever is forbidden.
- forbid: stop a loop that is happening now, e.g. {{"match": {{"do": "build", "module": "smelter_column"}},
  "minutes": 10, "why": "..."}}. Every goal with those keys and values is refused, also when another build would
  start it. At most 3 stand (the oldest drop out), each at most 30 minutes. Never forbid what queues.closeout
  builds: that is the tier's own supply (its gear and cable factories feed the science), and a ban there only
  makes the planners plan it again. A ban on all linking or on every power station also blocks the fix you will
  need next. Lift one with "allow" as soon as it is in the way.
- A fix you ordered last time whose problem is back (the same rows out of coal again, the same port unfed) treated
  the symptom: order the cause away now (a coal belt via link, a rebuild of the row where a belt reaches it,
  feeding by priority), not the same feed again.
- plan: null keeps the strategist's queue, and that is the default. Replace it only when its next steps are
  wrong, and say what is wrong in "plan_why" (a plan without plan_why is ignored). guidance: what the
  strategist and assessor must follow from now on, in plain words; they treat it as binding.
- Pausing or dropping the goal in hand costs the work it did: do it only when that goal is the mistake or
  blocks something urgent, not to reorder the queue (your orders run first anyway when it ends). Ask for your
  next look (5-20 game minutes) by how soon your orders can show results: longer while a big build runs.
- Be decisive but not destructive: never remove a working site that the base needs, the only power station,
  or the last source of a plate. When everything runs well, give no orders and say so.

Goals use this format:
{goals}

Reply with JSON only:
{{"assessment": "what is wrong and why, in a few sentences (or that all is well)", "orders": [],
"interrupt": false, "drop_current": false, "clear_urgent": false, "plan": null, "plan_why": "", "forbid": [],
"allow": [], "priority": null, "doctrine": null, "guidance": "", "next_check_minutes": 10}}"""


# ------------------------------------------------------------------ what it sees
def machine_census(crew) -> tuple[list[dict], dict]:
    """Every site's buildings by name and state (the foreman's last look), and per machine type how many work."""
    per_site, total = [], Counter()
    for s in crew.base.sites:
        cen = getattr(s, "census", None) or {}
        if cen:
            per_site.append({"site": s.id, "kind": s.kind, "machines": cen})
        for name, states in cen.items():
            for st, n in states.items():
                total[(name, "working" if st == "working" else "idle")] += n
    idle = {}
    for name in sorted({k[0] for k in total}):
        w, i = total[(name, "working")], total[(name, "idle")]
        idle[name] = {"working": w, "not_working": i, "share_idle": round(i / (w + i), 2) if w + i else 0}
    return per_site, idle


def _sites(crew) -> list[dict]:
    out = []
    sealed = getattr(crew, "sealed", {})
    for s in crew.base.sites:
        names = Counter(e[0] for e in s.entities)
        out.append({
            "id": s.id, "kind": s.kind, "at": [round(s.centre[0]), round(s.centre[1])],
            "params": {k: v for k, v in s.params.items() if k not in ("ports",) and not isinstance(v, (list, dict))
                       or k in ("ports_fed", "ports_waiting")},
            "ports": [{"item": p.get("item") or p.get("pack"), "fed": p.get("fed") is not None or bool(p.get("pack")),
                       "fuel": bool(p.get("fuel"))} for p in s.params.get("ports", [])],
            "chests": [what for *_, what in s.chests][:12],
            "health": s.health, "powered": s.powered, "built_minute": s.built_minute,
            "sealed": s.id in sealed, "buildings": dict(names.most_common(8)),
            **({"note": s.note[:200]} if s.kind.startswith("unfinished") and s.note else {}),   # why it stopped
        })
    return out


def _links(crew) -> list[dict]:
    return [{"item": ln["item"], "into_site": ln.get("to_site"), "from": ln.get("from"), "belts": ln.get("belts"),
             "kind": ln.get("kind"), "laid_minute": ln.get("minute")} for ln in getattr(crew, "links", [])]


def _diagnoses(crew) -> list[dict]:
    try:
        from .triage import diagnoses
        return diagnoses(crew)
    except Exception:  # noqa: BLE001 - a report line is never worth a crash
        return []


def _short_supply(crew) -> list[str]:
    """Plates, coal and steel the base makes too little of now (short_of): builds of these are never forbidden."""
    out = []
    for item in ("iron-plate", "copper-plate", "coal", "stone-brick", "steel-plate"):
        why = short_of(crew, item)
        if why:
            out.append(why)
    return out


def full_report(crew, base_report: dict, results: list[dict], previous: list[dict], extra: dict | None = None) -> dict:
    """Everything the bot knows, for the overseer. base_report: the strategist's own report."""
    w = crew.w
    cur = crew.current
    per_site, idle = machine_census(crew)
    rep = dict(base_report)
    rep.pop("base", None)                              # replaced by the fuller "sites" below
    try:
        power = crew.power_status()
    except Exception as exc:  # noqa: BLE001 - a report bug never stops the overseer
        power = {"error": str(exc)[:120]}
    try:
        fl = flows(crew)
    except Exception:  # noqa: BLE001
        fl = []
    try:
        from .nodes import ledger, plate_ledger, port_report
        ports, plates, lines = port_report(crew), [r["text"] for r in plate_ledger(crew)], ledger(crew)
    except Exception as exc:  # noqa: BLE001
        ports, plates, lines = {"error": str(exc)[:120]}, [], []
    rep.update({
        "now": {"game_minute": round(w.now_tick() / 3600, 1), "doing": w.activity[:120],
                "current_goal": ({"goal": _short(cur["goal"]), "minutes": round((w.now_tick() - cur["tick"]) / 3600, 1),
                                  "urgent": bool(cur.get("urgent")), "protected": bool(cur.get("aged")),
                                  "steps": list(w.steps)} if cur else None)},
        "queues": {"urgent": [_short(g) for g in crew.urgent], "plan": [_short(g) for g in crew.plan_queue[:PLAN_MAX]],
                   "plan_waited_min": round(plan_waited_min(crew, w.now_tick()), 1),
                   "closeout": [_short(g) for g in getattr(crew, "closeout_queue", [])]},
        "sites": _sites(crew),
        "machines": per_site,
        "idle": idle,
        "links": _links(crew),
        "repair_diagnoses": _diagnoses(crew),
        "ports": ports,
        "plates": plates,
        "lines": lines,
        "power": power,
        "flows": fl[:20],
        "inventory_full": dict(sorted(w.inventory.items(), key=lambda kv: -kv[1])),
        "results_history": [{"goal": _short(r["goal"]), "ok": r["ok"], "minutes": r.get("game_minutes"),
                             "result": str(r.get("result", ""))[:220]} for r in results[-25:]],
        "short_supply": _short_supply(crew),
        "forbidden": [{"match": f["match"], "minutes_left": round((f["until"] - w.now_tick()) / 3600, 1),
                       "why": f.get("why", "")} for f in active_forbids(crew)],
        "previous_overseer": previous[-3:],
        **(extra or {}),
    })
    return rep


# ------------------------------------------------------------------ forbids (checked by the crew)
def active_forbids(crew) -> list[dict]:
    now = crew.w.now_tick()
    keep = [f for f in getattr(crew, "overseer_forbid", []) if f["until"] > now]
    crew.overseer_forbid = keep
    return keep


def forbidden(crew, goal: dict) -> dict | None:
    """The forbid a goal falls under (None: allowed). A match is a partial goal: every key must be equal. A goal
    that adds supply of something measurably short (relieves) is never held back by a forbid."""
    g = {k: v for k, v in goal.items() if k != "reflex"}
    for f in active_forbids(crew):
        m = f["match"]
        if m and all(g.get(k) == v for k, v in m.items()):
            if relieves(crew, g):
                return None
            return f
    return None


# ------------------------------------------------------------------ what a forbid may never hold back
# Run 20261124-015025: the overseer forbade every iron build for 30 minutes, three times, because "existing iron
# lines have substantial spare capacity": the lines were big on paper while their mines ran dry. Iron fell to
# 38 plates a minute against 207 wanted, and the rule floor's replacements of dry mines were refused as forbidden.
SHORT_SHARE = 0.9              # made under this share of what the ports want: short, whatever the lines say
ORE_ITEM = {"iron-ore": "iron-plate", "copper-ore": "copper-plate", "stone": "stone-brick", "coal": "coal"}


def goal_item(goal: dict) -> str | None:
    """The item a build (or a replace's build) adds supply of: a mine's or outpost's plate, a steel column's
    steel, pumpjacks' crude oil, a factory's or workshop's item."""
    g = goal.get("with") if goal.get("do") == "replace" and isinstance(goal.get("with"), dict) else goal
    r = g.get("resource")
    if g.get("module") == "smelter_column" and r == "iron-plate":
        return "steel-plate"
    if r in ORE_ITEM:
        return ORE_ITEM[r]
    if g.get("module") == "pumpjack" or r == "crude-oil":
        return "crude-oil"
    return g.get("item")


def short_of(crew, item: str | None) -> str | None:
    """Why an item is short now (made under SHORT_SHARE of what its ports want, ports with no source at all,
    coal burned faster than mined), or None."""
    if not item:
        return None
    try:
        if item == "coal" and hasattr(crew, "coal_budget"):
            b = crew.coal_budget()
            if b.get("burned_per_min", 0) > b.get("mined_per_min", 0):
                return f"coal: mined {b['mined_per_min']}/min, burned {b['burned_per_min']}/min"
        want = sum(float(pt.get("per_minute") or 0) for _, pt in crew.item_ports() if pt.get("item") == item)
        items = (getattr(crew.w, "stats", None) or {}).get("items") or {}
        made = float((items.get(item) or {}).get("made_per_min") or 0)
        if want > 0 and made < SHORT_SHARE * want:
            return f"{item}: made {made:.0f}/min, its ports want {want:.0f}/min"
        from .nodes import starving
        hungry = starving(crew)
        if item in hungry:
            return f"{item}: {len(hungry[item])} port(s) with no source"
    except Exception:  # noqa: BLE001 - stub crews: nothing measured, nothing exempt
        return None
    return None


def _dry_mine(crew, site) -> bool:
    try:
        from .upkeep import MINES, mine_depleted, rebuild_due
        return site is not None and site.kind in MINES and (mine_depleted(site) or bool(rebuild_due(crew, site)))
    except Exception:  # noqa: BLE001
        return False


def relieves(crew, goal: dict) -> str | None:
    """Why a goal must run whatever is forbidden: it adds supply of an item that is short now, or it puts a mine
    that runs dry back on fresh ore (the same mine again: no new capacity, only none lost)."""
    item = goal_item(goal)
    why = short_of(crew, item)
    if why:
        return why
    base = getattr(crew, "base", None)
    if goal.get("do") == "replace" and base is not None and _dry_mine(crew, base.get(goal.get("site"))):
        return f"site {goal.get('site')} runs dry"
    old = getattr(crew, "_replacing", None)            # the build inside such a replace
    if goal.get("do") == "build" and old is not None and goal.get("resource") == old.params.get("resource")             and _dry_mine(crew, old):
        return f"site {old.id} runs dry"
    return None


# ------------------------------------------------------------------ carrying out its word
def _bare(goal: dict) -> str:
    return json.dumps({k: v for k, v in goal.items() if k != "reflex"}, sort_keys=True, default=str)


def same_target(g: dict) -> tuple:
    """What a goal acts on, loosely (do, site, module, resource, item, tech; not its size): two goals with the
    same target are one job queued twice (the assessor's 2.1/min gear factory and the close-out's 6/min one)."""
    w = g.get("with") or {}
    return (g.get("do"), g.get("site"), g.get("module") or w.get("module"), g.get("resource") or w.get("resource"),
            g.get("item") or w.get("item"), g.get("tech"))


def is_rules(goal: dict) -> bool:
    return str(goal.get("reflex", "")).startswith(RULES)


def rules_end(urgent: list[dict]) -> int:
    """Where the rule floor's fixes end at the front of the urgent queue: new rule fixes and orders go here."""
    i = 0
    while i < len(urgent) and is_rules(urgent[i]):
        i += 1
    return i


def drop_forbidden(crew, queue: list[dict]) -> list[str]:
    """Take the goals a forbid matches out of a queue (in place): the planners then never see them waiting and
    ask for replan after replan to get rid of them (run 20261120-084046: four strategist calls for one banned
    cable factory). Returns what was taken out."""
    out = [g for g in queue if forbidden(crew, g) is not None]
    if out:
        queue[:] = [g for g in queue if forbidden(crew, g) is None]
    return [_short(g) for g in out]


CARRY_TAGS = ("overseer", "fixer", "close-out")    # goals someone asked for on purpose: a new plan keeps them


def trigger_cleared(crew, g: dict) -> str | None:
    """Why a queued goal has nothing left to do, or None: its site is gone, a connect whose site has power, a
    stock already on hand, a replace whose old site is fine where it stands (Crew.stale_replace)."""
    do, site_id = g.get("do"), g.get("site")
    base = getattr(crew, "base", None)
    site = base.get(site_id) if base is not None and site_id is not None else None
    if site_id is not None and base is not None and site is None and do in ("remove", "replace", "connect", "relink",
                                                                            "purge", "upgrade", "repair", "feed"):
        return f"site {site_id} is gone"
    if do == "connect" and site is not None and site.powered and not site.health.get("no_power"):
        return f"site {site_id} has power"
    if do == "stock" and g.get("item"):
        try:
            if crew.w.inventory.get(g["item"], 0) >= int(g.get("count", 1)):
                return f"{g['item']} already on hand"
        except Exception:  # noqa: BLE001 - stub crews
            pass
    if do == "replace" and hasattr(crew, "stale_replace"):
        try:
            return crew.stale_replace(g)
        except Exception:  # noqa: BLE001
            return None
    return None


REPAIR_DOS = ("relink", "link", "link_science", "repair", "connect", "trunk")


def still_wanted(crew, g: dict) -> bool:
    """A repair whatever planner queued it (assessor, triage, the strategist's own): its target still has what it
    is for, an unfed input port (of its item), a site without power, gaps to fill. A new plan keeps it (when
    trigger_cleared says nothing is left to do it goes anyway). Run 20261122-133537: "relink site 25
    electronic-circuit", the one fix green science waited on, was dropped as "not in the new plan" at minutes
    206 and 229."""
    do = g.get("do")
    if do not in REPAIR_DOS:
        return False
    try:
        if do in ("link", "link_science"):
            return bool(crew.unfed_ports()) or bool(crew.unlinked_sources())
        site = crew.base.get(g.get("site"))
        if site is None:
            return False
        if do == "connect":
            return site.powered is False or bool(site.health.get("no_power"))
        if do == "repair":
            from . import gaps
            return bool(site.params.get("pipe_gaps") or gaps.pending(site))
        item = g.get("item")
        return any(pt.get("item") and not pt.get("fed") and (item is None or pt["item"] == item)
                   for pt in site.params.get("ports", []))
    except Exception:  # noqa: BLE001 - stub crews: tags decide
        return False


def carry_over(crew, old: list[dict], new: list[dict], where: str,
               event=lambda kind, **data: None) -> list[str]:
    """A new plan replaces the plan queue: goals of the old one that the overseer, the fixer or the close-out
    queued, and that the new plan does not cover (same goal, or the same target), go on at its end (in place,
    up to PLAN_MAX), unless forbidden now or their trigger cleared. Every other goal of the old queue that is
    not in the new one gets a "dropped" event with the reason, so a review sees drops directly. Run
    20261122-133537: "stock small-electric-pole" was asked four times and never ran (two strategist plans
    replaced the queue under it); the overseer's own plan was gone eight minutes later. Returns what was kept."""
    covered = {_bare(g) for g in new} | {same_target(g) for g in new}
    busy = {same_target(g) for g in getattr(crew, "urgent", [])} | (
        {same_target(crew.current["goal"])} if getattr(crew, "current", None) else set())
    keep, gone = [], []
    seen = set()
    for g in old:
        if not isinstance(g, dict) or g.get("do") == "wait":
            continue
        if _bare(g) in covered or same_target(g) in covered or same_target(g) in busy or _bare(g) in seen:
            continue                                    # the new plan (or a queued fix) does it: nothing lost
        seen.add(_bare(g))
        tagged = str(g.get("reflex", "")).startswith(CARRY_TAGS) or \
            _bare(g) in (getattr(crew, "carry_keys", None) or set()) or still_wanted(crew, g)
        f = forbidden(crew, g)
        cleared = trigger_cleared(crew, g)
        if f is not None:
            gone.append((g, f"forbidden by the overseer: {f.get('why', '')}"[:120]))
        elif cleared:
            gone.append((g, f"nothing left to do: {cleared}"[:160]))
        elif tagged:
            keep.append(g)
        else:
            gone.append((g, "not in the new plan"))
    room = max(0, PLAN_MAX - len(new))
    gone += [(g, "the new plan is full") for g in keep[room:]]
    keep = keep[:room]
    new.extend(keep)
    if getattr(crew, "carry_keys", None):              # remembered only while still queued
        live = {_bare(g) for g in new}
        crew.carry_keys = {k for k in crew.carry_keys if k in live}
    for g, why in gone:
        event("dropped", goal=_short(g), why=why, where=where)
    return [_short(g) for g in keep]


def note_dropped(goals: list[dict], why: str, where: str, event=lambda kind, **data: None) -> None:
    """A "dropped" event for every real goal taken out of a queue (waits aside)."""
    for g in goals:
        if isinstance(g, dict) and g.get("do") != "wait":
            event("dropped", goal=_short(g), why=why[:160], where=where)


def _closeout_match(crew, match: dict) -> dict | None:
    """The close-out goal a forbid would block (None: none). The close-out is the tier's own supply."""
    for g in getattr(crew, "closeout_queue", []) + ([crew.current["goal"]] if crew.current and str(
            crew.current["goal"].get("reflex", "")).startswith("close-out") else []):
        if all(g.get(k) == v for k, v in match.items()):
            return g
    return None


def _guard_force(crew, g: dict) -> tuple[dict, str | None]:
    """A forced removal passes the sealed-site guard only: the crew's own removal guard (last source of a plate,
    only power station, researching labs, last coal mine, a working site built minutes ago) still holds (run
    20261120-084046: it force-removed the starter iron smelter, then iron ran 11 plates a minute short)."""
    if g.get("do") != "remove" or not g.get("force"):
        return g, None
    site = crew.base.get(g.get("site")) if hasattr(crew, "base") else None
    why = crew.removal_blocker(site) if site is not None and hasattr(crew, "removal_blocker") else None
    if not why:
        return g, None
    return {k: v for k, v in g.items() if k != "force"}, f"force refused on site {g.get('site')}: {why}"[:160]


def apply_orders(crew, d: dict, log=print, event=lambda kind, **data: None) -> list[str]:
    """Carry out the overseer's orders. Returns what changed. Runs in the main thread (a checkpoint)."""
    w = crew.w
    now = w.now_tick()
    note = str(d.get("assessment", ""))[:200]
    changed: list[str] = []
    tag = f"overseer: {note}"[:160]
    for a in d.get("allow") or []:
        fs = getattr(crew, "overseer_forbid", [])
        if a == "all":
            crew.overseer_forbid = []
            changed.append("all forbids lifted")
        elif isinstance(a, int) and 0 <= a < len(fs):
            changed.append(f"allowed again: {fs.pop(a)['match']}")
    for f in (d.get("forbid") or [])[:FORBID_MAX]:
        if isinstance(f, dict) and isinstance(f.get("match"), dict) and f["match"]:
            hit = _closeout_match(crew, f["match"])
            if hit is not None:
                changed.append(f"forbid {f['match']} refused: the close-out needs {_short(hit)}")
                continue
            short = short_of(crew, goal_item(f["match"])) if f["match"].get("do") in ("build", "replace", None)                 else None
            if short:
                changed.append(f"forbid {f['match']} refused: {short}")
                continue
            try:
                mins = max(1.0, min(FORBID_MAX_MIN, float(f.get("minutes") or FORBID_DEFAULT_MIN)))
            except (TypeError, ValueError):
                mins = FORBID_DEFAULT_MIN
            keep = [q for q in getattr(crew, "overseer_forbid", []) if q["match"] != f["match"]]
            crew.overseer_forbid = (keep + [{"match": f["match"], "until": now + mins * 3600,
                                             "why": str(f.get("why", ""))[:160]}])[-FORBID_MAX:]
            changed.append(f"forbid {f['match']} for {mins:.0f} min")
    if d.get("clear_urgent") and crew.urgent:
        kept = [g for g in crew.urgent if is_rules(g)]       # fuel and power fixes stay (run 20261120-084046:
        n = len(crew.urgent) - len(kept)                     # clearing them left two iron rows without coal and
        if n:                                                # the next goal waited 20 minutes for plates)
            changed.append(f"dropped {n} queued fix(es)" + (f", kept {len(kept)} fuel/power fix(es)" if kept else ""))
        note_dropped([g for g in crew.urgent if not is_rules(g)], f"the overseer cleared the fixes: {note}",
                     "overseer clear_urgent", event)
        crew.urgent[:] = kept
    taken = drop_forbidden(crew, crew.urgent) + drop_forbidden(crew, crew.plan_queue)
    if taken:
        changed.append(f"forbidden, taken out of the queues: {taken}")
        for t in taken:
            event("dropped", goal=t, why="forbidden by the overseer", where="overseer forbid")
    busy = {_bare(g) for g in crew.urgent} | ({_bare(crew.current["goal"])} if crew.current else set())
    orders = []
    for g in (d.get("orders") or []):
        if not (isinstance(g, dict) and g.get("do") in GOAL_DOS) or len(orders) >= ORDER_MAX:
            continue
        g, refused = _guard_force(crew, {k: v for k, v in g.items() if k != "reflex"})
        if refused:
            changed.append(refused)
        if _bare(g) in busy:                                 # already running or queued: not twice (run
            changed.append(f"already under way: {_short(g)}")    # 20261120-084046: a second coal mine at minute 1)
            continue
        busy.add(_bare(g))
        orders.append(g)
    if orders:
        keys = {_bare(g) for g in orders}
        crew.plan_queue[:] = [g for g in crew.plan_queue if _bare(g) not in keys]    # moved up, not done twice
        at = rules_end(crew.urgent)
        crew.urgent[at:at] = [{**g, "reflex": tag} for g in orders]
        changed += [f"order: {_short(g)}" for g in orders]
    plan = d.get("plan")
    if isinstance(plan, list) and not str(d.get("plan_why") or "").strip():
        changed.append("plan kept: no plan_why given")
    elif isinstance(plan, list):
        goals = [g for g in plan if isinstance(g, dict) and g.get("do") in GOAL_DOS][:PLAN_MAX]
        drop_forbidden(crew, goals)
        # remembered by key (not tagged: an untagged goal is the plan's own, which pauses and ages as such), so
        # a strategist plan after this one keeps what it does not cover
        crew.carry_keys = set(getattr(crew, "carry_keys", None) or set()) | {_bare(g) for g in goals}
        kept = carry_over(crew, list(crew.plan_queue), goals, "overseer plan", event)
        crew.plan_queue[:] = goals                      # the main loop's own queue (the same list)
        crew.overseer_plan_tick = now                   # a strategist plan asked for before this is stale
        changed.append(f"new plan ({str(d['plan_why'])[:80]}): {[_short(g) for g in goals]}"
                       + (f"; kept from the old queue: {kept}" if kept else ""))
    if d.get("priority") is not None and isinstance(d["priority"], list):
        crew.foreman.priority = [str(x) for x in d["priority"]][:8]
        changed.append(f"priority {crew.foreman.priority}")
    if d.get("doctrine") and getattr(crew, "strategist", None) is not None:
        crew.strategist.doctrine = str(d["doctrine"])[:800]
        changed.append("doctrine rewritten")
    if d.get("guidance") is not None:
        crew.overseer_guidance = {"minute": round(now / 3600, 1), "text": str(d["guidance"])[:1200]}
    if crew.current and crew.current.get("aged") and (d.get("interrupt") or d.get("drop_current")):
        # frontier aging (upkeep.next_goal): the plan waited PLAN_STARVE_MIN for this goal; a pause would send it
        # back to the end of the line (run 20261120-180703: paused at minute 83, never run again). Stall and
        # over-budget guards still end it; a forbid keeps it from coming back
        changed.append("goal in hand kept: the plan waited too long for it (protected); the orders run right after it")
    elif crew.current and (d.get("interrupt") or d.get("drop_current")):
        # at the next safe point of THIS goal (frontier and urgent ones too); dropped if it ends first
        drop = bool(d.get("drop_current"))
        cur = crew.current
        g = cur.get("goal") or {}
        ran = (now - cur.get("tick", now)) / 3600
        reflex = str(g.get("reflex", ""))
        mainline = not reflex or reflex.startswith("close-out")    # the plan's or the close-out's own goal: a
        # fix someone queued (assessor, overseer, rules) is dropped as asked (run 20261120-092648: the assessor's
        # 2.1/min gear factory, paused instead, came back in front of the overseer's own new plan and of two
        # strategist plans, and it took a second look and a forbid 7 minutes later to get rid of it)
        if drop and mainline and g.get("do") in ("build", "replace") and (
                ran >= DROP_TO_PAUSE_MIN or reflex.startswith("close-out")):
            drop = False                                # its work is kept: paused and resumed after the orders
            changed.append(f"not dropped ({ran:.1f} min of work in it): paused instead")
        crew.request_pause(tag, force=True, drop=drop)
        changed.append("drop the goal in hand" if crew.drop_current else "pause the goal in hand")
    log(f"[overseer] {note} -> {changed or ['no change']}")
    event("oversee", note=note, changes=changed)
    return changed


class Overseer:
    """A deep-thinking model, asked every `period_min` game minutes (sooner or later if it says so)."""

    def __init__(self, client: Any, model: str, effort: str | None = "high", period_min: float = 10.0,
                 max_cost_usd: float = 5.0, log=print):
        import openai_bot
        from .brain import GOAL_HELP
        self.client, self.model, self.effort, self.max_cost, self.log = client, model, effort, max_cost_usd, log
        self._prices = openai_bot
        self.period_min = period_min
        self.name = f"llm:{model}"
        self.cost_usd = 0.0
        self.calls = 0
        self.system = OVERSEE_PROMPT.format(goals=GOAL_HELP, starve=PLAN_STARVE_MIN)
        self.timeout_s = 300.0
        self.history: list[dict] = []                   # its last orders and what came of them

    def next_gap_min(self, d: dict) -> float:
        try:
            return max(GAP_RANGE[0], min(GAP_RANGE[1], float(d.get("next_check_minutes") or self.period_min)))
        except (TypeError, ValueError):
            return self.period_min

    def decide(self, rep: dict) -> dict:
        if self.cost_usd >= self.max_cost:
            return {"assessment": "overseer budget spent", "orders": []}
        client = self.client.with_options(timeout=self.timeout_s, max_retries=1) \
            if hasattr(self.client, "with_options") else self.client
        kw: dict[str, Any] = {"reasoning_effort": self.effort} if self.effort else {}
        msgs = [{"role": "system", "content": self.system},
                {"role": "user", "content": json.dumps(rep, separators=(",", ":"), default=str)}]
        t0 = time.monotonic()
        try:
            resp = client.chat.completions.create(model=self.model, messages=msgs,
                                                  response_format={"type": "json_object"}, **kw)
        except Exception as exc:  # noqa: BLE001 - a model that refuses reasoning_effort: once without it
            if not kw or "reasoning" not in str(exc):
                raise
            self.log(f"[overseer] {self.model} refused reasoning_effort={self.effort}; asking without it")
            self.effort = None
            resp = client.chat.completions.create(model=self.model, messages=msgs,
                                                  response_format={"type": "json_object"})
        self.calls += 1
        u = resp.usage
        if u is not None:
            cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
            self.cost_usd += self._prices.cost_usd(self.model, u.prompt_tokens, cached, u.completion_tokens) or 0.0
        try:
            out = json.loads(resp.choices[0].message.content or "{}")
        except json.JSONDecodeError:
            out = {"assessment": "the overseer's answer was not JSON", "orders": []}
        out["latency_s"] = round(time.monotonic() - t0, 1)
        return out if isinstance(out, dict) else {"assessment": "no orders", "orders": []}
