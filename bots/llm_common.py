"""Provider-neutral pieces shared by the LLM test bots (Claude, OpenAI, Ollama).

Tool specs, the system prompt and the code that runs one tool call against the
arena live here, so every bot plays by exactly the same rules and the scores are
comparable. Each bot only wraps these specs in its provider's tool format.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sdk"))
from arena_sdk import ArenaError, Run  # noqa: E402

ENTITIES = ["burner-mining-drill", "stone-furnace", "wooden-chest", "iron-chest",
            "burner-inserter", "transport-belt"]
ITEMS = ["iron-ore", "copper-ore", "coal", "stone", "wood", "iron-plate", "copper-plate",
         "stone-brick", "iron-gear-wheel", "copper-cable", "electronic-circuit",
         "automation-science-pack", "logistic-science-pack"] + ENTITIES
RECIPES = ["iron-gear-wheel", "stone-furnace", "burner-mining-drill", "wooden-chest",
           "iron-chest", "burner-inserter", "transport-belt"]
LINE_ENTITIES = ["transport-belt", "pipe"]
DIRS = ["north", "east", "south", "west"]

NUM = {"type": "number"}
XY = {"x": NUM, "y": NUM}

# name -> (description, properties). Every property is required.
SPECS: dict[str, tuple[str, dict]] = {
    "observe": ("See inventory, nearby entities, resource tiles and obstacles. Uses 1 observe.",
                {"radius": {"type": "integer", "description": "1-20"}}),
    "move": ("Walk to a position. 1 action, ceil(distance/0.15) ticks.", XY),
    "place": ("Place an entity from your inventory within reach 10. 1 action.",
              {"entity": {"type": "string", "enum": ENTITIES}, **XY,
               "direction": {"type": "string", "enum": DIRS}}),
    "place_line": ("Place a straight belt line (max 32 tiles). 1 action.",
                   {"entity": {"type": "string", "enum": ["transport-belt"]},
                    "x1": NUM, "y1": NUM, "x2": NUM, "y2": NUM}),
    "insert": ("Put items from your inventory into the entity at x,y. 1 action.",
               {**XY, "item": {"type": "string", "enum": ITEMS}, "count": {"type": "integer"}}),
    "extract": ("Take items out of the entity at x,y. 1 action.",
                {**XY, "item": {"type": "string", "enum": ITEMS}, "count": {"type": "integer"}}),
    "advance": ("Let game time run. Costs only ticks, no action.", {"ticks": {"type": "integer"}}),
    "craft": ("Hand-craft a recipe. 1 action, recipe time in ticks.",
              {"recipe": {"type": "string", "enum": RECIPES}, "count": {"type": "integer"}}),
    "mine": ("Hand-mine a resource tile within 3 tiles. 1 action, 120 ticks per ore.",
             {**XY, "count": {"type": "integer"}}),
    "rotate": ("Rotate the entity at x,y. 1 action.", {**XY, "direction": {"type": "string", "enum": DIRS}}),
    "pickup": ("Pick up the entity at x,y with its contents. 1 action.", XY),
    "set_recipe": ("Set the recipe of the assembling machine at x,y (any point on it). 1 action.",
                   {**XY, "recipe": {"type": "string", "enum": []}}),
}
END_SPEC = ("End the run now and hand over to the scorer.", {})


def _enum(prop: dict, values: list[str]) -> dict:
    return {**prop, "enum": list(values)}


def _task_props(task: dict, name: str, props: dict) -> dict:
    """Narrow the enums to what this task allows, so the model can't pick a forbidden entity or item."""
    entities = task.get("entities") or ENTITIES
    items = sorted(set(ITEMS) | set(entities) | set(task.get("start_inventory") or {}))
    props = dict(props)
    if name == "place":
        props["entity"] = _enum(props["entity"], entities)
    elif name == "place_line":
        props["entity"] = _enum(props["entity"], [e for e in LINE_ENTITIES if e in entities] or ["transport-belt"])
    elif name == "insert":
        props["item"] = _enum(props["item"], task.get("insert_items") or items)
    elif name == "extract":
        props["item"] = _enum(props["item"], items)
    elif name == "craft":
        props["recipe"] = _enum(props["recipe"], task.get("recipes") or RECIPES)
    elif name == "set_recipe":
        props["recipe"] = _enum(props["recipe"], task.get("assembler_recipes") or [])
    return props


def tool_specs(task: dict) -> list[tuple[str, str, dict]]:
    """(name, description, JSON schema) for the verbs this task allows, plus end_run."""
    out = []
    for name in [v for v in task["verbs"] if v in SPECS] + ["end_run"]:
        desc, props = END_SPEC if name == "end_run" else SPECS[name]
        if name == "pickup" and task.get("clear_obstacles"):
            desc += " Also clears a tree or rock at x,y (you get its wood or stone)."
        if name == "place_line" and "pipe" in (task.get("entities") or []):
            desc = "Place a straight line of belts (facing the direction of travel) or pipes, max 32 tiles. 1 action."
        if name != "end_run":
            props = _task_props(task, name, props)
        schema = {"type": "object", "properties": props, "required": list(props),
                  "additionalProperties": False}
        out.append((name, desc, schema))
    return out


def budget_rules(b: dict) -> str:
    """How this task limits the player: an action budget, a time-only budget, or both."""
    step = b.get("action_ticks") or 0
    if b.get("actions") is None:
        return (f"Budgets: no action limit. Game time is the budget: {b['ticks']} ticks (60 ticks = 1 s), and every "
                f"action takes at least {step} ticks while the world keeps running, so at most {b['ticks'] // step} "
                f"actions fit. {b['observes']} observes (separate, instant).")
    extra = f" Every action takes at least {step} ticks." if step else ""
    return (f"Budgets: {b['actions']} actions, {b['observes']} observes (separate), {b['ticks']} game ticks "
            f"(60 ticks = 1 s).{extra}")


AUTOMATION_FACTS = """Facts (Factorio 2.1, research already done):
- Power: an offshore pump goes on a land tile next to water, facing the water; it needs no power and
  outputs water on its opposite side. A boiler (3x2) facing north takes water on its west and east ends
  and puts steam out of its north side; a steam engine (3x5, vertical) sits directly north of it. A boiler
  burns 1.8 MW of fuel (coal is 4 MJ) and feeds two steam engines (900 kW each). Pipes connect to every
  neighbour. A small electric pole powers a 5x5 area and links to poles within 7.5 tiles; every electric
  building, and the steam engine, must touch a pole's area.
- Electric mining drill: 3x3, 0.5 ore/s, 90 kW, drops ore 1 tile past its front edge. Burner mining drill:
  2x2, 0.25 ore/s, burns fuel. Stone furnace: 2x2, 0.3125 plates/s, burns fuel, takes ore from drills or
  inserters.
- An inserter faces the side it picks up from: facing south it picks from the tile south of it and drops
  on the tile north. Long-handed inserters reach 2 tiles. Burner inserters need fuel, the others power.
- Assembling machine 1: 3x3, speed 0.5, 75 kW; set its recipe with set_recipe. Recipe times at speed 1:
  gear 0.5 s (2 iron plates), copper cable 0.5 s (1 copper plate -> 2), electronic circuit 0.5 s (1 iron
  plate + 3 cable), inserter 0.5 s (circuit + gear + iron plate), transport belt 0.5 s (gear + iron plate
  -> 2), red science 5 s (copper plate + gear), green science 6 s (inserter + transport belt).
- One assembling machine 1 makes 6 red science or 5 green science per minute. 30 green per minute needs
  6 green machines plus one machine each for inserters, belts, gears, circuits and copper cable: 11 in all.
- The kit is enough for tier 2 with plenty to spare. Tier 2 needs about 11 assemblers, 6 electric drills and
  9 furnaces on iron (165 plates/min), 2 drills and 3 furnaces on copper (45/min), and about 2 MW of power
  (2 boilers, 4 steam engines). You start with 20 assemblers, 20 electric drills, 30 furnaces, 4 boilers and
  8 steam engines. Build tier 1 first, then go for tier 2.
- You cannot craft. You may insert only fuel by hand; everything else must be moved by the factory.
- Hand-inserted fuel runs out. A boiler at full load burns a 50-coal fuel slot in under 2 minutes; a
  burner drill lasts about 22 minutes on 50 coal, a stone furnace about 37. The scored window runs from game
  minute 20 to 30, so everything must stay fuelled until minute 30: feed boilers and burner drills from a
  coal drill by belt and inserters, or from a chest of coal through an inserter. Electric drills need no fuel.
- Game time only passes when you advance or move, and your build time is limited. To test a change,
  advance 300-600 ticks and observe; long advances burn your build time.
- observe shows each inserter's pickup and drop positions, where each drill drops its ore, and what an
  assembling machine is missing ("missing"). Use them to find the broken link instead of guessing.
- Ending early gains nothing: only the window after the run is scored. Keep building toward the next
  tier while you have build time; end only when the factory runs well or time is nearly up.
- 2x2 and other even-sized sides sit on integer coordinates, odd-sized ones on .5."""


def _score_text(task: dict) -> str:
    b, sc = task["budgets"], task["score"]
    if sc["type"] != "automation":
        return (f"Score: {sc['type']} of {sc['item']}. After you finish, the scorer lets the world run\n"
                f"{b['idle_tail_ticks']} more ticks with no actions, then counts.")
    tiers = "; ".join(f"tier {i}: {t['item']} at {t['per_minute']}/min" for i, t in enumerate(sc["tiers"], 1))
    pts = ", ".join(f"{k} {v}" for k, v in sc["points"].items())
    start = (f" The window always starts at game minute {b['ticks'] // 3600}, when your build time is up: ending "
             "early only skips ahead, it saves and earns nothing.") if sc.get("window_at_build_end") else ""
    return (f"Score: after you finish, the factory runs {b['idle_tail_ticks'] // 3600} minutes with nobody touching "
            f"it, and only that window counts.{start} Tiers ({tiers}) need the factory to make the item at that rate "
            f"and to make at least {sc['balance']:.0%} of the intermediates it uses (no living off stockpiles). Score = "
            f"tier x 100,000 + points for items made in the window ({pts}). The next tier is worth 100,000: more than "
            "any number of points can ever add, so a working tier 1 plus a real attempt at tier 2 beats a bigger "
            "tier 1. score_so_far shows points for everything made so far, not the final score.")


def system_prompt(task: dict, constants: dict) -> str:
    b = task["budgets"]
    automation = task["score"]["type"] == "automation"
    return f"""You are playing a Factorio AI competition task through a game API.

Task: {task['title']}
{task['summary']}
{_score_text(task)}

{budget_rules(b)}
The world is paused between your calls; only the ticks your calls spend move it. Rejected actions still
cost what they cost, so check reach ({constants['reach']} tiles) and inventory before acting.

{AUTOMATION_FACTS if automation else LEVEL_FACTS}{chr(10) + BELT_FACTS if "transport-belt" in (task.get("entities") or []) else ""}

Work efficiently: observe, plan the whole layout, build it, fuel it, then spend ticks with few large
advances. Act only through the tools. Call end_run when there is nothing useful left to do."""


BELT_FACTS = """Belts: items move the way each belt faces. To turn a corner, the corner tile itself must face the
new direction: end the first line one tile short and start the second line on the corner tile (or rotate
the corner belt). A belt never puts items into a machine: an inserter must take them off. place_line and
rotate report "links": what the line's end feeds and what feeds its start. Read them."""


LEVEL_FACTS = """Facts: a burner mining drill (2x2, 0.25 ore/s, 150 kW) drops ore 1 tile in front of it; facing north, a
stone furnace centred 2 tiles above the drill's centre catches the ore. A stone furnace makes 0.3125 plates/s
at 90 kW and stops when its output holds 100 plates. Coal is 4 MJ. 2x2 entities sit on integer coordinates."""


def run_tool(run: Run, name: str, args: dict, max_resources: int = 120) -> dict:
    """Run one tool call against the arena and return a compact, JSON-safe result for the model."""
    if name == "end_run":
        return {"ended": True}
    try:
        res = run.act({"type": name, **args})
    except ArenaError as e:
        # 4xx: malformed or not allowed. Costs nothing; tell the model so it can fix the call.
        return {"error": e.code, "message": e.message}
    data = res.get("data") or {}
    if name == "observe" and "resources" in data:
        data = {**data, "resources": {k: v[:max_resources] for k, v in data["resources"].items()}}
    return {"ok": res["ok"], "code": res.get("code"), "data": data,
            "budgets": {k: res["budgets"][k] for k in ("actions_left", "observes_left", "ticks_left")},
            "score_so_far": res.get("score_so_far")}


def budgets_spent(run: Run) -> bool:
    b = run.budgets
    ticks_out = (b.get("ticks_left") or 0) <= 0
    actions = b.get("actions_left")
    return ticks_out and (actions is None or actions <= 0)   # time-only: out of ticks means out of actions


def finish(run: Run, task: dict | None = None) -> dict:
    """Spend any ticks the model left (factories keep producing), then end and fetch the result.

    Not on the automation level: only the hands-off window after the run counts, and spending
    leftover time there just burns fuel before the window starts."""
    automation = bool(task) and task["score"]["type"] == "automation"
    if not automation and run.budgets.get("ticks_left", 0) > 0:
        run.advance(run.budgets["ticks_left"])
    return run.end_and_wait()


def dumps(obj: dict) -> str:
    return json.dumps(obj, separators=(",", ":"))
