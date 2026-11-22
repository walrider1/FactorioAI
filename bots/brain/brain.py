"""The strategists: who decides what to build and research next.

Both take the same one-page report and return the same plan:

    {"note": "...", "doctrine": "the standing long-term plan", "goals": [goal, ...]}

ScriptedStrategist  a free opening book: from nothing to automated green science. Used for tests, as the
                    baseline, and as the LLM's fallback when it is unavailable or out of budget.
LLMStrategist       an OpenAI-compatible chat model. It is called rarely (on a milestone, when the goal
                    queue runs dry, after failures) and in the background, so the crew never waits for it.
                    It keeps a doctrine between calls: its own plan in a few lines, so a model with no memory
                    still plays one consistent game. Cost-capped.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from .crew import Crew
from .modules import MODULES

GOAL_HELP = """Goals you may give (JSON objects, done in order by the crew):
- {"do": "build", "module": "<module>", ...args}   build a module: the crew unlocks and crafts its kit, builds
                                                    it, wires it to power and checks it has power
- {"do": "research", "tech": "<technology>"}        research it and any missing prerequisites
- {"do": "stock", "item": "<item>", "count": n}      hand-craft items to keep on hand
- {"do": "remove", "site": <id>}                     pick a site's buildings back up into the inventory
                                                    (poles stay). Refused for the only power station, labs while
                                                    researching, the last working source of a plate, the last coal
                                                    mine; add "force": true only if you are sure
- {"do": "replace", "site": <id>, "with": {build goal}}   build the new thing first, then remove the old site
- {"do": "connect", "site": <id>}                    wire a site to the power network (reuses existing poles)
- {"do": "design", "module": "factory", "item": "<item>", "per_minute": n}
                                                    preview a chiplet without building: whether it fits the ground,
                                                    its size, kit, plate cost and what must be researched first
- {"do": "wait", "seconds": n}                       let the base run (fuel, plates and labs are still tended)
- {"do": "feed", "site": <id>, "item": "coal", "count": n}   put fuel into a site now (a power station's boiler
                                                    chest, or a mine's drills)
- {"do": "link"}                                     belt outputs to the input ports that want them: science to the
                                                    research stations, steel/plastic/circuits... to the factories
                                                    and workshop cells that take them (the book does it on its own)
- {"do": "relink", "site": <id>, "item"?: "<item>"}  take up the belts into one site's input ports and lay them
                                                    again (from a plate line built since, or around what walled
                                                    them in): rework one input, not the whole site
- {"do": "purge", "site": <id>, "item": "<item>"}   a belt into the site carries something else (alert
                                                    belt-foreign): stop what joins it and clean its belts
- {"do": "upgrade", "site": <id>}                    faster machines where the old ones stand (assembling machine
                                                    1->2->3, stone->steel furnace, fast inserters): same layout,
                                                    picked up and placed one by one; no site: the one that gains
                                                    most. Only helps a site that waits on its machines
- {"do": "repair", "site": <id>}                     place the pipes a build could not (the pipe-gap alert names
                                                    them): fluid stops at a gap
- {"do": "scout", "resource": "<ore>"}                walk out in rings until a patch of the ore nobody works yet
                                                    is known (for an outpost; see ore_outlook). For crude-oil it
                                                    reports the known fields (base_status.oil) and walks only
                                                    when none is known
- {"do": "priority", "sites": [<id> | <kind> | <resource> | <item>, ...]}
                                                    what the foreman tends first, right after coal mines and power
                                                    (e.g. the mine feeding the factory you are about to build)
Modules:
""" + "\n".join(f"- {m.name}: {m.describe}" for m in MODULES.values())


def _upkeep_status(crew) -> dict:
    try:
        from .upkeep import status
        out = status(crew)
    except Exception as exc:  # noqa: BLE001 - a report bug never stops planning
        return {"error": str(exc)[:160]}
    try:                                     # the crude oil known: planners never send a scout for what is found
        if hasattr(crew, "oil_fields"):
            out = {**out, "oil": crew.oil_fields()[:4]}
    except Exception:  # noqa: BLE001
        pass
    return out


def _outlook(crew) -> dict:
    try:
        from .outposts import outlook
        return outlook(crew)
    except Exception as exc:  # noqa: BLE001 - a report bug never stops planning
        return {"error": str(exc)[:160]}


def _brief_goal(g: dict) -> dict:
    return {k: v for k, v in g.items() if k != "reflex"}


def _nodes(crew) -> dict:
    try:
        from .nodes import plate_ledger, port_report
        from .nodes import ledger
        return {"plates": [r["text"] for r in plate_ledger(crew)], "unfed_ports": port_report(crew)["summary"],
                "lines": ledger(crew)[:12]}
    except Exception:  # noqa: BLE001 - a report bug never stops the strategist
        return {}


def report(crew: Crew, task: dict, last_results: list[dict]) -> dict:
    """One page for the strategist: where the game stands and what happened."""
    w, kn, base = crew.w, crew.kn, crew.base
    st = w.stats or {}
    items = st.get("items") or {}
    rates = {k: v.get("made_per_min") for k, v in items.items()
             if v.get("made_per_min") and (k.endswith("plate") or k.endswith("science-pack") or k in (
                 "coal", "stone", "iron-gear-wheel", "electronic-circuit", "copper-cable", "inserter", "transport-belt"))}
    milestones = task.get("score", {}).get("milestones", [])
    reached = w.milestones or {}
    inv = {k: v for k, v in sorted(w.inventory.items(), key=lambda kv: -kv[1])[:25]}
    return {
        "game_minute": round(w.now_tick() / 3600, 1),
        "minutes_left": round(w.minutes_left, 1),
        "milestones_reached": [m["name"] for m in milestones if m["id"] in reached],
        "next_milestones": [m["name"] for m in milestones if m["id"] not in reached][:4],
        "research": w.research,
        "researched": sorted(t for t in kn.researched if not kn.techs.get(t, {}).get("researched_at_start")),
        "can_build_now": sorted(kn.entities[e]["type"] + ":" + e for e in kn.entities
                                if kn.recipe_for(e, hand=True))[:60],
        "base": [s.brief() for s in base.sites],
        "blocked_on": crew.blocker(),        # the next tier everything waits on (None: all of it stands)
        "burner_drills": crew.burner_drills(),
        "refactor_hints": crew.hints(),
        "factory_can_make": MODULES["factory"].can_make(kn)[:45],   # building one researches what it needs
        "production_per_min": rates,
        "inventory": inv,
        "recent_results": last_results[-8:],
        "repeated_failures": _repeated(last_results),
        "failure_counts": _failure_counts(last_results),
        "recent_removals": [{k: r[k] for k in ("site", "key", "minute", "built_minute")} for r in crew.removed[-5:]],
        "foreman_problems": crew.foreman.problems[-6:],
        # what is going wrong now, each with the goal that fixes it (urgent ones made you be asked now)
        "alerts": [{k: a.get(k) for k in ("level", "what", "fix")} for a in crew.alerts()],
        # belt links that failed: why, and how often; they retry by themselves once a site is built or removed
        "stuck_links": crew.stuck_links(),
        # per plate: made, what belts carry to factories, what unfed ports want, what sits in full chests; and
        # every input port no belt feeds, with why (nodes.py): routing, not totals, says what is short
        **_nodes(crew),
        "short_coal_belts": crew.coal_chains(),
        # belts of any item whose source makes less than their ports take (the link step feeds spare sources in)
        "short_belts": [c for c in crew.short_chains() if c["item"] != "coal"],
        # which sites run on their own (sealed: left alone), and what still needs hands or repair, with the fix
        "base_status": _upkeep_status(crew),
        "closeout_queue": [_brief_goal(g) for g in getattr(crew, "closeout_queue", [])],
        # per ore: what is left where we mine, how fast it goes, room for more drills, the next patch, the move
        "ore_outlook": _outlook(crew),
        "coal": crew.coal_budget(),
        "foreman_priority": ["coal mines", "power"] + crew.foreman.priority + ["labs and factories", "plate mines",
                                                                              "other"],
        "minutes_since_last_milestone": _since_milestone(w),
        # what the supervisor changed between your plans (urgent fixes, paused goals, foreman priority)
        "supervisor": (crew.supervisor.notes[-5:] if getattr(crew, "supervisor", None) else []),
        # the road to the rocket, computed: stages, research left, science rates, power, the workshop's shortages,
        # and the step the free book would take next
        "roadmap": _compact_roadmap(crew),
        "courier": crew.foreman.last_courier,
        # what the fixer (troubleshooter) found and did about failures that repeat
        "fixer": getattr(crew, "fix_notes", [])[-4:],
        "dig_outs": list(getattr(crew.w, "trap_notes", []))[-3:],      # the character got shut in and dug out
        # the overseer's standing word and what it forbids for now: binding for the strategist
        "overseer": {"guidance": getattr(crew, "overseer_guidance", None),
                     "forbidden": [f["match"] for f in getattr(crew, "overseer_forbid", [])]},
    }


def _compact_roadmap(crew) -> dict:
    try:
        from .roadmap import roadmap
        rm = roadmap(crew)
    except Exception as exc:  # noqa: BLE001 - a report must never fail on its extras
        return {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    crew.last_roadmap = rm
    return {"stages": [f"[{s['status']}] {s['name']}" + (f": {s['detail']}" if s["detail"] else "")
                       for s in rm["stages"]],
            "research": rm["research"], "science": rm["science"], "power": rm["power"], "plates": rm["plates"],
            "workshop": rm["workshop"], "next_step": rm["next_step"]}


def _since_milestone(w) -> float:
    ticks = [t for t in (w.milestones or {}).values() if isinstance(t, (int, float))]
    return round((w.now_tick() - max(ticks, default=0)) / 3600, 1)


def _failure_counts(results: list[dict]) -> dict[str, int]:
    """How often each goal has failed over the whole run (the opening book degrades or drops such steps)."""
    out: dict[str, int] = {}
    for r in results:
        if not r["ok"]:
            k = json.dumps(r["goal"], sort_keys=True)
            out[k] = out.get(k, 0) + 1
    return out


def _smaller(goal: dict) -> dict | None:
    """A smaller variant of a build goal that keeps failing: one engine, fewer drills, half the rate."""
    g = dict(goal)
    if g.get("engines", 1) > 1:
        g["engines"] = 1
    elif int(g.get("count", 1)) > 1:
        g["count"] = max(1, int(g["count"]) // 2)
    elif float(g.get("per_minute", 0)) > 2:
        g["per_minute"] = max(2, round(float(g["per_minute"]) / 2, 1))
    else:
        return None
    return g


def _repeated(results: list[dict]) -> list[dict]:
    """Goals that failed more than once in the last 12, with how often and the latest reason."""
    seen: dict[str, dict] = {}
    for r in results[-12:]:
        if r["ok"]:
            continue
        k = json.dumps(r["goal"], sort_keys=True)
        row = seen.setdefault(k, {"goal": r["goal"], "times": 0})
        row["times"] += 1
        row["last_reason"] = r["result"][:300]
    return [v for v in seen.values() if v["times"] >= 2]


# ------------------------------------------------------------------ scripted
class ScriptedStrategist:
    """From nothing to automated green science, step by step. Each step has a 'done' test on the report."""
    name = "scripted"
    cost_usd = 0.0
    calls = 0
    max_cost = 0.0
    model = "none (free opening book)"
    tokens = {"prompt": 0, "cached": 0, "completion": 0}

    def __init__(self):
        self.doctrine = ("Opening: a coal mine first, then burner mines on iron, stone and copper; steam power; labs; research "
                         "automation and electric mining; an automated red science factory; green science "
                         "research; an automated green science factory; steel; a pumpjack for oil processing. "
                         "Then the rocket book: electric mines for plates, power ahead of demand, 12 labs, workshops for "
                         "every science colour, the rocket-silo research, the silo's kit, the silo, rocket parts.")

    @staticmethod
    def _count(rep: dict, kind: str, **match) -> int:
        n = 0
        for s in rep["base"]:
            if s["kind"] == kind and all(s.get(k) == v for k, v in match.items()):
                n += int(s.get("count", 1))
        return n

    @staticmethod
    def _rate(rep: dict, item: str) -> float:
        """Total designed rate of our factories for an item (smaller factories add up)."""
        return sum(float(s.get("per_minute", 0)) for s in rep["base"] if s["kind"] == "factory" and s.get("item") == item)

    def plan(self, rep: dict) -> dict:
        self.calls += 1
        rs = rep.get("research") or {}
        # research that is queued or running counts as done for the book: asking again only re-queues it
        done = set(rep["researched"]) | set(rs.get("queue") or []) | ({rs["current"]} if rs.get("current") else set())
        failed = {json.dumps(r["goal"], sort_keys=True) for r in rep["recent_results"] if not r["ok"]}
        # alerts first: whatever is going wrong right now (coal, power) comes before the next step of the book
        for a in sorted(rep.get("alerts") or [], key=lambda a: a["level"] != "urgent"):
            g = a.get("fix")
            if g and json.dumps(g, sort_keys=True) not in failed:
                return {"note": f"alert: {a['what'][:120]}", "doctrine": self.doctrine, "goals": [g]}
        # a failure that names its own fix ("build pumpjacks first ({...})"): do that next
        for r in reversed(rep["recent_results"][-3:]):
            if r["ok"]:
                continue
            for m in re.finditer(r'\{"do": "build"[^{}]*\}', str(r.get("result", ""))):
                try:
                    g = json.loads(m.group(0))
                except ValueError:
                    continue
                if json.dumps(g, sort_keys=True) not in failed:
                    return {"note": f"a failure named its fix: {r['result'][:100]}", "doctrine": self.doctrine,
                            "goals": [g]}
        # general-manager rules first: clean up what no longer earns its place, fix what is cut off
        for site in rep["base"]:
            h = site.get("health") or {}
            drills = h.get("no_minable_resources", 0)
            if site["kind"] == "burner_mine" and drills and drills >= max(1, int(site.get("count", 1)) * 0.75):
                g = {"do": "remove", "site": site["id"]}
                if json.dumps(g, sort_keys=True) not in failed:
                    return {"note": f"site {site['id']} is mined out: take it back", "doctrine": self.doctrine,
                            "goals": [g]}
            if site["kind"].startswith("unfinished"):
                g = {"do": "remove", "site": site["id"], "force": True}
                if json.dumps(g, sort_keys=True) not in failed:
                    return {"note": f"clear the unfinished site {site['id']}", "doctrine": self.doctrine,
                            "goals": [g]}
            if site.get("powered") is False:
                g = {"do": "connect", "site": site["id"]}
                if json.dumps(g, sort_keys=True) not in failed:
                    return {"note": f"site {site['id']} has no power", "doctrine": self.doctrine, "goals": [g]}
        steps: list[tuple[bool, dict]] = [
            (self._count(rep, "burner_mine", resource="coal") >= 2,         # fuel from chests from the start
             {"do": "build", "module": "burner_mine", "resource": "coal", "count": 2}),
            (self._count(rep, "burner_mine", resource="iron-ore") >= 2,
             {"do": "build", "module": "burner_mine", "resource": "iron-ore", "count": 2}),
            (self._count(rep, "burner_mine", resource="stone") >= 1,
             {"do": "build", "module": "burner_mine", "resource": "stone", "count": 1}),
            (self._count(rep, "burner_mine", resource="copper-ore") >= 2,
             {"do": "build", "module": "burner_mine", "resource": "copper-ore", "count": 2}),
            (self._count(rep, "burner_mine", resource="iron-ore") >= 5,
             {"do": "build", "module": "burner_mine", "resource": "iron-ore", "count": 3}),
            (self._count(rep, "steam_power") >= 1, {"do": "build", "module": "steam_power", "engines": 2}),
            (self._count(rep, "labs") >= 1, {"do": "build", "module": "labs", "count": 2}),
            ("automation" in done, {"do": "research", "tech": "automation"}),
            ("electric-mining-drill" in done, {"do": "research", "tech": "electric-mining-drill"}),
            (self._count(rep, "burner_mine", resource="copper-ore") >= 4,
             {"do": "build", "module": "burner_mine", "resource": "copper-ore", "count": 2}),
            (self._count(rep, "burner_mine", resource="iron-ore") >= 9,
             {"do": "build", "module": "burner_mine", "resource": "iron-ore", "count": 4}),
            ("logistics" in done, {"do": "research", "tech": "logistics"}),
            (self._rate(rep, "automation-science-pack") >= 6,
             {"do": "build", "module": "factory", "item": "automation-science-pack",
              "per_minute": round(max(2.0, 6 - self._rate(rep, "automation-science-pack")), 1)}),
            ("logistic-science-pack" in done or (rep["research"] or {}).get("current") == "logistic-science-pack",
             {"do": "research", "tech": "logistic-science-pack"}),
            (self._count(rep, "steam_power") >= 2, {"do": "build", "module": "steam_power", "engines": 2}),
            ("logistic-science-pack" in done and self._rate(rep, "logistic-science-pack") >= 12,
             {"do": "build", "module": "factory", "item": "logistic-science-pack",
              "per_minute": round(max(2.0, 12 - self._rate(rep, "logistic-science-pack")), 1)}),
            ("steel-processing" in done, {"do": "research", "tech": "steel-processing"}),
            # oil-processing is a trigger: the crew researches oil-gathering, smelts steel and builds a pumpjack
            ("oil-processing" in done, {"do": "research", "tech": "oil-processing"}),
            ("plastics" in done, {"do": "research", "tech": "plastics"}),
            ("chemical-science-pack" in done, {"do": "research", "tech": "chemical-science-pack"}),
        ]
        # past the opening: the rocket book, one step at a time from the computed roadmap
        nxt = (rep.get("roadmap") or {}).get("next_step")
        if nxt:
            steps.append((False, nxt))
        counts = rep.get("failure_counts") or {}
        for ok, goal in steps:
            if ok:
                continue
            # a step that keeps failing is tried smaller, then dropped, so the book never loops on it
            while goal is not None and counts.get(json.dumps(goal, sort_keys=True), 0) >= 2:
                goal = _smaller(goal) if goal.get("do") == "build" else None
            if goal is None or json.dumps(goal, sort_keys=True) in failed:
                continue
            return {"note": "rocket book" if goal is nxt else "opening book", "doctrine": self.doctrine,
                    "goals": [goal]}
        return {"note": "book: nothing to add, the base runs", "doctrine": self.doctrine,
                "goals": [{"do": "wait", "seconds": 60}]}


# ------------------------------------------------------------------------ LLM
PROMPT = """You are the strategist of a Factorio AI playing the base game in real time, from an empty inventory.
You never act yourself. A scripted crew carries out your goals at full speed: it crafts each module's kit
(gathering plates, ore, stone, wood and coal on the way), unlocks what the kit needs (trigger technologies
and research), and builds the module. A foreman keeps everything fuelled and feeds the labs. The game keeps
running while you think, so plan several steps ahead.

The task: {title}
{summary}
Milestones (points, in order): {milestones}

How the early game works here (Factorio 2.1): steam power unlocks after 50 iron plates are made, electronics
after 10 copper plates, red science after the first lab is crafted; everything else is researched in labs.
Burner drills mine 15 ore a minute; hand-crafting is slow, so plate supply limits everything early on.
A steam engine gives 900 kW; an assembler uses 75 kW, an electric drill 90 kW, a lab 60 kW.

You are the base's general manager. Each report lists every site (id, kind, when built, whether it has power,
and the status of its machines: working, no_power, no_minable_resources = mined out, waiting for ingredients,
output full), plus refactor_hints. Decide each time:
- what to build next toward the next milestone;
- when a site has served its purpose: a mined-out mine, a burner mine on ore that electric mines should use
  once a factory supplies plates, an unfinished site. Remove it (its buildings come back to the inventory and
  save crafting time) or replace it (build the better version first, then remove the old);
- when to leave a working site alone and move on: a site that works and feeds a milestone is not to be
  touched just because it is old.
Never cut your own supply: keep at least one working source of iron and copper plates for the crew's crafting,
and power for everything electric.

Modules are chiplets designed on demand for the ground they get, not fixed blueprints: burner mines pick
spots facing any way on the patch, the power station tries every rotation and a one-engine size around every
shore spot, and a factory is laid out for exactly the item and rate you ask (any item in factory_can_make:
science, or buildings for a mall to stop hand-crafting kits). Need a bigger or different factory? Ask for one
at the rate you need, or design it first to see if it fits and what it costs. A chiplet is never invented
around something that does not exist: no water means no steam power, whatever the layout.

Size every supply build to the measured gap, never by habit: the gap is used minus made a minute (production flows,
alerts "used X/min, made Y/min"), plus about 30% headroom. An electric_mine row takes 2-12 drills (each ~19 plates,
15 bricks or 30 ore/coal/stone a minute), a burner_mine 2-8 (15 ore a minute), a steel smelter_column 2-24 furnaces
(3.75 steel a minute each), a factory any per_minute. A gap of 70 iron plates a minute is one row of 5 drills, not
another row of 4; a gap bigger than one build gets the biggest build now and more next time. An alert's "fix" is
already sized this way.
Energy first: power comes before building anything else. roadmap.power.demand_kw is what the network really
draws (demand_source: measured by the game, or estimated), not every machine at full work: do not build power
for full_load_kw. Keep roadmap.power.headroom_kw above ~1.5 MW (each
steam_power adds 1.8 MW, with a new offshore pump of its own when the first water line is busy); the crew also
adds stations on its own before an electric build, and the supervisor treats low power as urgent.
Burner drills and stone furnaces are a bootstrap: once electric-mining-drill is researched, replace every burner
mine with an electric_mine ({{"do": "replace", "site": id, "with": {{electric_mine goal}}}}): no refuelling every
few minutes, and the burner mine's buildings come back for kits.
Factories are modular blocks: you say what one makes, how fast, and what comes in from outside
({{"do": "build", "module": "factory", "item": "logistic-science-pack", "per_minute": 15, "inputs":
["iron-gear-wheel"]}}); it makes everything else in its chain itself. Ore is for drills only: plates are inputs by
default, so factories go on open ground off the ore and electric_mine rows feed them (the crew builds those first
when short). What assemblers cannot make from plates (steel, plastic, sulfur, processing units, batteries) is
always an input, and a chain too big for one layout is split into smaller factories that feed each other (an
advanced-circuit factory into a blue-science factory). When a factory does not fit, make it smaller: move part of
its chain into `inputs` and build that part as its own factory elsewhere. Each input port shows in the base with
its item; build what makes it if nothing does, then "link" belts outputs to ports. Until a link exists, the
courier keeps the port's chest stocked.
How the base grows to the rocket. Iron and copper chains (red and green science, circuits, gears, belts,
buildings) are real factories ("factory": drills, furnaces and belts into assemblers, nobody carries anything).
The "workshop" is courier-fed, so it is only for what belts cannot make yet: fluids and oil and what needs them.
The workshop makes such an item at a rate: the planner works out every
intermediate from the game's recipes and builds one cell per machine (assemblers, furnaces, chemical plants,
refineries) on one growing floor, pipes every fluid in its own net (water from an offshore pump, crude from the
pumpjacks), and reuses what earlier cells make spare. A courier carries items between cell chests every minute
or so and brings plates, coal, stone and ore from the mines, so plate supply must keep up: "electric_mine" rows
(drill -> furnace -> chest) are the plate supply for workshops; "roadmap.plates" compares supply with what the
cells take. Every machine draws power: keep "roadmap.power.headroom_kw" positive with steam_power before a big
workshop. oil-processing is a trigger technology (pumping crude oil): a pumpjack does it; anything with plastic,
sulfur or acid needs pumpjacks first (about one per 300 crude a minute). The "silo" module builds the rocket
silo once rocket-silo is researched and its kit (1000 steel and concrete, 200 processing units and electric
engines) sits in workshop chests; the courier feeds it, and a finished rocket launches on its own.
"roadmap" is computed every time: the stages to the rocket, research left (techs, packs, lab hours), each
science colour's rate against what the road needs, and "next_step", the goal the free book would take. Use it;
deviate when you see a better order. Workshops at 10-15 packs a minute per colour and 12 labs finish the
research in a few game hours.

The crew hand-crafts only what is cheap by hand (under 2 game minutes of crafting, like the 10 red packs of
the first research) or what nothing can make yet. A research goal whose packs no factory makes builds that
factory first (red and green with the factory module, the rest with a workshop), then lets the labs run on its
output: so asking for research is fine, but a science factory you plan anyway is best built before it.
Automation is the point of the game: prefer building something that makes an item over the crew gathering it
by hand. A coal mine comes first (2 burner drills into chests): every drill, furnace and boiler is then fuelled
from its chests instead of from trees (which run out near the base and cost a long walk) or hand-mining. The crew
builds one on its own before anything that burns fuel, and waits for its own drills before mining by hand.
Burner mines are a bootstrap. About 8 to 12 burner drills give enough plates to build power, labs and the first
factory; past that, more burner mines unblock nothing. "blocked_on" names the next tier everything waits on
(power, labs, the research for automation, the first automated science): make it your next goal. When a build of
it fails on terrain, ask for it again: chiplets remember the shore tiles and spots the game refused and try other
places, other water and other patches, and when it blocks progress they keep shrinking until something fits.
Building something elsewhere to "move the crew" changes nothing: chiplets search the ground themselves.

Speed counts: milestones score more the earlier they come, so keep the crew busy on what moves toward the
next one and avoid goals that only wait. The base must also keep running while you push: "alerts" says what is
going wrong right now (coal running short, a coal mine or the boiler out of fuel, stalled research), each with
the goal that fixes it. Act on them first; the crew already answers urgent coal and power alerts on its own.
"coal" is the coal budget from the game's statistics: mined vs burned per minute and how long the stock lasts.
A supervisor watches the base between your plans: it fixes urgent problems at once (it may pause the goal in
hand and resume it after), and asks you to replan when your plan no longer fits; "supervisor" lists what it did.
Plan for the rocket, not only the next milestone: each tier must keep running while you build the next.
Every burner drill, furnace and boiler you add burns coal; grow coal mining with them (a burner drill on coal
nets ~12.75 coal/min, an electric one 30/min; a boiler at full load burns 27/min). Coal follows the path
players take: the opening feeds burners, furnaces and boilers by hand (the foreman tops each up with nearly a
stack, which lasts a stone furnace ~30 minutes). Logistics (underground belts) is researched right after the
first automated red science, because belts must cross each other; from then on coal moves by belt: new
electric_mine furnace rows and power stations get a coal input port ("fuel": "belt" in the base) linked to an
electric coal mine's chest (built first when none is free), and the link step fits the rows and stations built
before logistics with coal belts where they stand. Never tear down working sites or belts to make room for
coal belts; "fuel": "hand" sites that cannot take one (burner mines) need the foreman's visits, so replace
them with electric_mine rows as the base grows. A site whose furnaces have no fuel
also shows its drills "waiting_for_space": that is a fuel problem, not a belt problem, and linking does not
fix it. The foreman tends coal mines and power first, then "foreman_priority" (set it with a priority goal),
then the rest.
"stuck_links" lists belt links that failed, why (what walls the port in), and how often. The crew already
retries each by itself, with wider detours, taps on belts that carry the item and moving other links out of
the way; it tries again once a site is built or removed. Asking for "link" again before that changes nothing:
remove what walls the port in, rebuild the consumer nearer its source, or build a new source next to it.
"plates" and "unfed_ports" say what really reaches the factories: an item can be made in surplus and still
starve every factory whose ports no belt reaches. While an item has unfed ports the crew holds back new consumers
of it (they would starve too): plan the routing fix instead, not more mines or another factory.
"lines" lists every plate line (a mine row's or a smelting column's plates on one belt): what it carries, what
its links draw, what is left. Plate rows are lines once logistics is in; a factory is built beside the lines its
plates come from, with one belt per plate. Grow a short line with more drills on its patch; relink a site whose
inputs should come from a newer line.
"short_coal_belts" lists coal belts that feed more boilers and rows than their chest's drill mines: those run
dry in turn. The link step belts a spare coal chest into them; new electric coal mines are what makes spares.
When the coal patch has no room left, replace burner coal mines on it with electric ones, and keep new
boilers and furnace rows within the coal there is: the patch is the ceiling until electric furnaces.

Build like a player who finishes a block before moving on. "base_status" lists the sealed sites (they ran on
their own for 10 minutes: inputs by belt, fuel by belt or none, power) and what still needs hands or repair,
each with its fix; "policy" says what hands are still fine at this point of the game: feeding fuel by hand
until logistics, burner drills until electric drills and logistics, a courier-fed workshop always. When a tier
is done (a roadmap stage) the crew makes a planned trip back on its own ("closeout_queue": the fixes plus
power and coal headroom) before your next goals run: do not repeat those goals, and do not keep sending upkeep
goals between tiers. Sealed sites are not removed, replaced or re-routed while they work (such goals are
refused); build the next tier beside them. While a next-tier build runs, the supervisor holds site-level fixes
until it finishes and only pauses it for base-wide trouble (power, coal running out), so plan the next tier
in full rather than in small steps.
Changing what stands: make a site faster with "upgrade" (same layout, better machines: the close-out does it
for the sites that gain most whenever a better machine is unlocked; base_status.upgradable lists them). Make
more of the same thing by replacing a site with a bigger build of the same product (allowed even when sealed:
the new one is built first). To make something else, build it new; the old site goes idle once nobody takes
its output (base_status.idle, after 45 minutes) and may then be removed for its machines.
Inputs are matched by rate: a source chest gives what one drill, furnace or factory share makes, and a belt
that feeds more than that is listed in short_belts; the link step feeds an unused source of the item into it
from the side. When no source is spare, build more of the item, not another link.
Grow out of hands as players do: a workshop item made in bulk (8+/min) gets a belt factory once belts can make
it (the courier is one man and cannot carry rocket-scale amounts), and the building the crew keeps
hand-crafting gets a small factory of its own (a mall); base_status.next_growth shows both and the close-out
does them. Steel comes from a smelter_column on iron plates, not furnaces fed by hand. A consumer whose links
keep failing because it is walled in is rebuilt where its inputs are (the alert says so); every site keeps a
one-tile margin for belts. A refinery with a full output stops the whole oil chain: the oil-balance alert says
what takes the surplus.
Think ahead about ore. "ore_outlook" says per ore how much is left on the patches we mine, how fast it goes,
whether there is room for more drills, and the next patch known. There are no trains: when a patch is full or
has less than about four game hours left, build an outpost on the next patch (drills on both sides of one
belt, a pole line home, the ore smelted beside the field when it is near the base or at the base end of a trunk
belt when far; one goal does it all), or scout for one first. An outpost is a long build: plan it as a frontier
goal, not as an urgent fix. Big consumers (purple and yellow science, the silo) get their plates from such dense
fields set aside for them, built BEFORE the factory: the close-out plans an iron and a copper field once blue
science is researched; build the factories after they stand, so they are placed where the plate lines arrive.
Mines running low are rebuilt on fresh ore by the crew itself before they run dry: do not order that.

The report's "stuck" block says how long the base has gone without progress (no milestone, research or
successful build). When it grows, your current approach is not working: change it (another place, a smaller
version, more supply, research, exploring) rather than repeating it. A watchdog will step in after a while.

When a goal fails, read the reason: it names the cause. Remove a site only when a failure names that site as
in the way. A failure caused by terrain, unexplored ground or a missing unlock is not fixed by tearing down
working sites. repeated_failures lists goals that failed the same way more than once: do not send them again
unchanged; change what the reason points at (another place, a smaller size, research, exploring) or move on.
recent_removals lists what you took down; building the same kind of thing again minutes later means the
removal was wasted work (it shows up in refactor_hints as churn). Working sites younger than 10 game minutes
are refused for removal unless you pass "force": true.

{goals}

Keep a doctrine: your standing plan for the rest of the game in at most 6 short lines. You get it back next
time; rewrite it when the situation changes. Don't repeat a goal that failed for the same reason; work
around it (another module, research first, more plate supply).
An overseer looks at the whole base every few minutes and has the last word: "overseer.guidance" is binding
(plan along it, never against it), and never plan a goal that "overseer.forbidden" matches.

Reply with JSON only:
{{"note": "one sentence about this decision", "doctrine": "...", "goals": [goal, ...]}}   (1 to 6 goals)"""


class LLMStrategist:
    def __init__(self, client: Any, model: str, task: dict, max_cost_usd: float = 2.0, log=print,
                 fallback: ScriptedStrategist | None = None):
        import openai_bot  # prices
        self.client, self.model, self.task, self.max_cost, self.log = client, model, task, max_cost_usd, log
        self._prices = openai_bot
        self.name = f"llm:{model}"
        self.cost_usd = 0.0
        self.calls = 0
        self.tokens = {"prompt": 0, "cached": 0, "completion": 0}
        self.doctrine = ""
        self.notes: list[dict] = []
        self.timeout_s = 120.0          # the OpenAI client's own default is 10 minutes plus retries
        self.last_latency = 0.0
        self.fallback = fallback or ScriptedStrategist()

    def plan(self, rep: dict) -> dict:
        if self.cost_usd >= self.max_cost:
            out = self.fallback.plan(rep)
            out["note"] = "strategist budget spent: opening book"
            return out
        ms = ", ".join(f"{m['name']} ({m['points']})" for m in self.task.get("score", {}).get("milestones", []))
        system = PROMPT.format(title=self.task.get("title", ""), summary=self.task.get("summary", "").strip(),
                               milestones=ms, goals=GOAL_HELP)
        user = json.dumps({"doctrine": self.doctrine, **rep}, separators=(",", ":"))
        t0 = time.monotonic()
        try:
            client = self.client.with_options(timeout=self.timeout_s, max_retries=1) \
                if hasattr(self.client, "with_options") else self.client
            resp = client.chat.completions.create(
                model=self.model, messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                response_format={"type": "json_object"})
        except Exception as exc:  # noqa: BLE001 - a failed or slow call never stops the crew
            self.last_latency = round(time.monotonic() - t0, 1)
            self.log(f"[brain] model call failed after {self.last_latency} s ({str(exc)[:120]}); "
                     "using the opening book this time")
            out = self.fallback.plan(rep)
            out["note"] = f"model call failed ({type(exc).__name__}): opening book"
            return out
        self.last_latency = round(time.monotonic() - t0, 1)
        self.calls += 1
        u = resp.usage
        cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
        self.cost_usd += self._prices.cost_usd(self.model, u.prompt_tokens, cached, u.completion_tokens) or 0.0
        self.tokens["prompt"] += u.prompt_tokens or 0
        self.tokens["cached"] += cached
        self.tokens["completion"] += u.completion_tokens or 0
        try:
            out = json.loads(resp.choices[0].message.content or "{}")
        except json.JSONDecodeError:
            out = {}
        goals = [g for g in out.get("goals") or [] if isinstance(g, dict) and g.get("do") in
                 ("build", "research", "stock", "remove", "replace", "connect", "design", "wait", "feed",
                  "priority", "link", "link_science", "relink", "upgrade", "scout", "repair", "purge")][:6]
        if out.get("doctrine"):
            self.doctrine = str(out["doctrine"])[:800]
        plan = {"note": str(out.get("note", ""))[:300], "doctrine": self.doctrine, "goals": goals,
                "latency_s": self.last_latency}
        self.notes.append({"minute": rep["game_minute"], **plan})
        return plan
