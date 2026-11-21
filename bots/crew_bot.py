"""Crew bot: a director plans, a scripted crew builds, a foreman keeps it running. Made for the realtime clock.

In real time the game never waits, so an LLM that thinks for 10 seconds per action loses. This bot splits
the job by speed:

    Director  (LLM, slow, rare)   reads a one-page report and picks the next build steps from a menu of
                                  skills. Called a handful of times per run, in the background, so the crew
                                  never stands still while it thinks. Costs cents.
    Crew      (scripted, fast)    turns each step into exact actions: finds a site, routes belts with A*,
                                  places, fuels. Runs at the game's action rate limit.
    Foreman   (rules, instant)    reads every building's status the game reports and either fixes it on the
                                  spot (low fuel -> insert coal) or escalates it to the director
                                  (no power, starved assembler, depleted ore).

The director never sees coordinates or single actions; the crew never decides what to build. Teams can
swap any layer: a better skill library raises the ceiling, a better director uses it more wisely.

    python bots/crew_bot.py --url http://127.0.0.1:8710 --token fa_... --director scripted
    arena local-run --backend factorio --task automation-realtime --bot crew --director openai --model gpt-6.1-terra
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sdk"))
sys.path.insert(0, os.path.dirname(__file__))
from arena_sdk import ArenaClient, RealtimeRun, Run  # noqa: E402

import automation_bot as ab  # noqa: E402  (the skill library: layouts, routing, building)
from fab.build import build as fab_build  # noqa: E402  (the chiplet layout engine)
from fab.design import design_factory  # noqa: E402
from fab.grid import Grid  # noqa: E402

TICKS_PER_MINUTE = 3600
BLOCK_COAL = 400          # coal a new red block needs to keep running to game minute 30


# ------------------------------------------------------------------- skills
SKILLS = {
    "iron_block": "Build a red-science block on the iron patch: 2 burner drills, 2 furnaces, a gear assembler, "
                  "a red-science assembler, inserters, an output chest. Makes ~6 red/min once it has copper and "
                  "power. args: {}",
    "copper_feed": "Feed copper plates to block N: a burner drill + furnace on copper and a belt to the block. "
                   "args: {\"block\": N}",
    "power": "Build the steam power station at the nearest water (pump, boiler, steam engine, coal chest) and "
             "a pole line to block N. Needed once. args: {\"block\": N}",
    "connect_power": "Run a pole line from an already powered block to block N. args: {\"block\": N}",
    "coal_mine": "Put 2 burner drills on the nearest coal, each into a chest. The foreman then restocks your coal "
                 "from them (~25 coal/min). The kit's 1,000 coal runs one block and the power station; a second "
                 "block needs about 400 more. args: {}",
    "factory": "Design and build a whole factory for an item at a rate with the layout engine: electric drills "
               "and furnaces on iron and copper, an assembler for every intermediate, belts, undergrounds, poles. "
               "Green science at 10/min (tier 2) is ~8 assemblers, 5 drills, ~100 belts, 3-5 game minutes of "
               "crew time; 30/min (tier 3) needs nearly the whole kit and may not fit. Builds the power station "
               "itself if there is none. args: {\"item\": \"logistic-science-pack\", \"per_minute\": 10}",
    "wait": "Do nothing new for a while (the foreman keeps patrolling and restocking coal). "
            "args: {\"seconds\": 10..300}",
    "finish": "End the build phase now. The scored window still starts at game minute 20, so finish only when "
              "there is nothing useful left to build. args: {}",
}


class CrewWorld(ab.World):
    """The scripted world model, plus a memory of the nearest ore and water seen from every observe."""

    def __init__(self, run: Run, log=print):
        super().__init__(run, log)
        self.hints: dict[str, list[tuple[float, float]]] = {}

    def observe(self, radius: int = 20) -> dict:
        d = super().observe(radius)
        for name, s in self.summary.items():
            n = s.get("nearest")
            if n:
                self.hints.setdefault(name, []).append((n["x"], n["y"]))
        return d

    def nearest(self, name: str) -> tuple[float, float] | None:
        """Nearest known tile of a resource from here, from anything seen so far."""
        spots = list(self.hints.get(name, [])) + [ab.centre(t) for t in self.resources.get(name, ())]
        return min(spots, key=lambda s: ab.math.dist(s, self.pos)) if spots else None


@dataclass
class Block:
    id: int
    plan: dict
    copper: bool = False
    powered: bool = False
    problems: list[str] = field(default_factory=list)


class Crew:
    """Executes skills with the scripted building code. Every skill returns a short line for the report."""

    def __init__(self, run: Run, log: Callable[..., None]):
        self.world = CrewWorld(run, log)
        self.log = log
        self.blocks: list[Block] = []
        self.power_pole: tuple[int, int] | None = None
        self.mine_drills: list[tuple[float, float]] = []
        self.mine_chests: list[tuple[float, float]] = []
        self.factories: list[dict] = []
        self.placer, self.designer = "search", None       # factory layouts: search, llm or best

    def start(self) -> dict:
        d = self.world.observe(20)
        return d

    def _block(self, args: dict) -> Block:
        n = int(args.get("block", len(self.blocks) - 1))
        if not 0 <= n < len(self.blocks):
            raise ab.BuildError(f"there is no block {n} (blocks: {len(self.blocks)})")
        return self.blocks[n]

    def run_skill(self, skill: str, args: dict) -> str:
        w = self.world
        if skill == "iron_block":
            coal = w.inventory.get("coal", 0)
            if self.blocks and coal < BLOCK_COAL:            # a block that runs dry by minute 20 scores nothing
                raise ab.BuildError(f"not enough coal for another block: {coal} of {BLOCK_COAL}. Build coal_mine "
                                    "and wait for the foreman to restock")
            iron = w.summary.get("iron-ore", {}).get("nearest")
            copper = w.summary.get("copper-ore", {}).get("nearest")
            if not iron:
                raise ab.BuildError("no iron within 64 tiles")
            if not self.blocks:
                w.go(iron["x"], iron["y"])
            w.observe(20)
            toward = (copper["x"], copper["y"]) if copper else w.pos
            site = ab.find_block(w, toward)
            if site is None and w.resources.get("iron-ore"):
                tiles = w.resources["iron-ore"]
                w.go(sum(t[0] for t in tiles) / len(tiles), sum(t[1] for t in tiles) / len(tiles))
                w.observe(20)
                site = ab.find_block(w, toward)
            if site is None:
                raise ab.BuildError("no room left on the iron patch for a block")
            plan = ab.build_block(w, *site)
            self.blocks.append(Block(len(self.blocks), plan))
            return f"block {len(self.blocks) - 1} built at {site}"
        if skill == "copper_feed":
            b = self._block(args)
            if b.copper:
                return f"block {b.id} already has copper"
            w.go(*ab.centre(b.plan["belt_goal"]))
            w.observe(20)
            start = ab.build_copper(w, b.plan["belt_goal"])
            ab.build_belt(w, start, b.plan["belt_goal"])
            b.copper = True
            return f"copper belt to block {b.id} built"
        if skill == "power":
            b = self._block(args)
            if self.power_pole is not None:
                return self.run_skill("connect_power", args)
            w.go(*ab.centre(b.plan["belt_goal"]))
            w.observe(20)
            pole = ab.build_power(w, b.plan["belt_goal"], engines=2, coal=500)
            ab.build_poles(w, pole, b.plan["poles"][1])
            self.power_pole, b.powered = pole, True
            return f"power station built, block {b.id} powered"
        if skill == "connect_power":
            b = self._block(args)
            if b.powered:
                return f"block {b.id} already powered"
            # Any powered pole is a source: the station (a factory may have built it before any block had power),
            # a powered block, a factory. Looking only at blocks bounced power <-> connect_power forever.
            src = self.powered_poles()
            if not src:                                  # no station yet, so "power" builds one: no loop
                return self.run_skill("power", args)
            goal = tuple(b.plan["poles"][1])
            near = min(src, key=lambda p: math.dist(p, goal))
            ab.build_poles(w, near, goal)
            b.powered = True
            return f"block {b.id} connected to the power network at {near}"
        if skill == "factory":
            return self._factory(str(args.get("item", "logistic-science-pack")),
                                 min(60.0, max(1.0, float(args.get("per_minute", 10)))))
        if skill == "coal_mine":
            if self.mine_drills:
                return "the coal mine is already built"
            coal = w.nearest("coal")
            if not coal:
                raise ab.BuildError("no coal seen anywhere yet")
            w.near(*coal, dist=4)
            w.observe(20)
            ore = w.resources.get("coal", set())
            for _ in range(2):
                site = None
                for tx, ty in sorted(ore, key=lambda t: ab.math.dist(t, w.pos)):
                    cx, cy = tx + 1, ty + 1
                    drill = {(cx - 1, cy - 1), (cx, cy - 1), (cx - 1, cy), (cx, cy)}
                    above = {(cx - 1, cy - 2), (cx, cy - 2)}
                    if drill <= ore and w.free(drill | above):
                        site = (cx, cy, drill)
                        break
                if site is None:
                    break
                cx, cy, drill = site
                ent = w.place("burner-mining-drill", cx, cy, "north", drill)
                dx, dy = ent.get("drop", [cx - 0.5, cy - 1.5])
                chest = ab.tile(dx, dy)
                w.place("iron-chest", *ab.centre(chest), "north", {chest})
                w.used |= {(cx - 1, cy - 2), (cx, cy - 2)}
                w.fuel(cx, cy, 10)
                self.mine_drills.append((cx, cy))
                self.mine_chests.append(ab.centre(chest))
            if not self.mine_drills:
                raise ab.BuildError("no room for a drill on the coal patch")
            return f"coal mine: {len(self.mine_drills)} drills into chests"
        raise ab.BuildError(f"unknown skill {skill}")

    def sites(self) -> list[tuple[str, float, float]]:
        """Where the foreman looks: each block (by its middle pole), the coal mine, each factory's groups."""
        out = [(f"block {b.id}", b.plan["poles"][1][0] + 0.5, b.plan["poles"][1][1] + 0.5) for b in self.blocks]
        if self.mine_drills:
            out.append(("coal mine", *self.mine_drills[0]))
        for f in self.factories:
            out += [(f"factory {f['id']} {name}", x, y) for name, x, y in f["sites"]]
        return out

    def powered_poles(self) -> list[tuple[int, int]]:
        out = [tuple(self.power_pole)] if self.power_pole else []
        out += [tuple(b.plan["poles"][1]) for b in self.blocks if b.powered]
        for f in self.factories:
            out += [tuple(p) for p in f["poles"][:3]]
        return out

    def survey(self, focus: tuple[float, float], radius: int = 34, step: int = 26) -> None:
        """Observe a grid of points around the factory site, so the planner knows every tile it may use."""
        w = self.world
        pts = [(focus[0] + dx, focus[1] + dy) for dx in range(-radius, radius + 1, step)
               for dy in range(-radius, radius + 1, step)]
        for x, y in sorted(pts, key=lambda p: math.dist(p, w.pos)):
            around = [(int(x) + ox, int(y) + oy) for ox in (-10, 0, 10) for oy in (-10, 0, 10)]
            if sum(t in w.seen for t in around) >= 8:
                continue
            w.go(x, y)
            w.observe(20)

    def _factory(self, item: str, per_minute: float) -> str:
        w = self.world
        iron, copper = w.nearest("iron-ore"), w.nearest("copper-ore")
        if not iron or not copper:
            raise ab.BuildError("need both iron and copper seen nearby for a factory")
        focus = ((iron[0] + copper[0]) / 2, (iron[1] + copper[1]) / 2)
        self.survey(focus)
        w.observe(8)                                     # fresh inventory for the kit check
        grid = Grid.from_world(w)
        t0 = time.monotonic()
        design = design_factory(grid, item, per_minute, near=focus, placer=self.placer, llm=self.designer,
                                log=self.log, seed=len(self.factories) + 1, anchors=self.powered_poles())
        self.log(f"[design] {design.summary()} ({time.monotonic() - t0:.1f} s)")
        if not design.ok:
            raise ab.BuildError("no buildable layout: " + "; ".join(design.problems)[:180])
        kit = design.kit()
        short = {k: v for k, v in kit.items() if w.inventory.get(k, 0) < v}
        if short:
            raise ab.BuildError(f"not enough in the kit for this layout: {short}")
        if self.power_pole is None:                      # power first, toward the factory
            self.power_pole = ab.build_power(w, (int(focus[0]), int(focus[1])), engines=2, coal=500)
        report = fab_build(w, design, self.log, anchors=self.powered_poles())
        groups = {"iron": [c for c in design.cells if c.ore == "iron-ore"],
                  "copper": [c for c in design.cells if c.ore == "copper-ore"],
                  "assemblers": [c for c in design.cells if c.kind == "asm"]}
        sites = []
        for name, cells in groups.items():
            if cells:
                xs, ys = [c.centre()[0] for c in cells], [c.centre()[1] for c in cells]
                sites.append((name, sum(xs) / len(xs), sum(ys) / len(ys)))
        f = {"id": len(self.factories), "item": item, "per_minute": per_minute, "placer": design.placer,
             "built": report["placed"], "failed": report["failed"], "sites": sites,
             "poles": [list(p) for p in design.poles], "summary": design.summary(),
             "layout": design.render(grid), "notes": design.notes}
        self.factories.append(f)
        msg = f"factory {f['id']}: {per_minute:g}/min {item}, {report['placed']} things built"
        if report["failed"]:
            msg += f", {len(report['failed'])} failed (first: {report['failed'][0][:80]})"
        return msg


# ------------------------------------------------------------------ foreman
BURNERS = {"burner-mining-drill", "stone-furnace", "burner-inserter", "boiler"}
POWER_PROBLEMS = {"no_power", "low_power"}


def classify(e: dict) -> tuple[str, str] | None:
    """One building's status -> (problem, remedy). remedy 'fuel' the foreman fixes; 'director' it escalates."""
    status = e.get("status", "")
    fuel = (e.get("inventories") or {}).get("fuel")
    if e["name"] in BURNERS and fuel is not None and sum(fuel.values()) < 5 and status != "working":
        return ("low fuel", "fuel")
    if status == "no_fuel":
        return ("no fuel", "fuel")
    if status in POWER_PROBLEMS:
        return ("no power", "director")
    if status == "no_minable_resources":
        return ("ore depleted", "director")
    if e["name"] == "assembling-machine-1" and e.get("missing") and status != "working":
        return (f"starved of {', '.join(e['missing'])}", "director")
    return None


class Foreman:
    """Walks the blocks, fixes fuel on the spot, remembers what it can't fix."""

    def __init__(self, crew: Crew, log: Callable[..., None]):
        self.crew, self.log = crew, log
        self.escalations: list[str] = []
        self.fixes = 0
        self.restocked = 0
        self._seen: dict[tuple, int] = {}

    def restock(self) -> None:
        """Empty the coal mine's chests on every patrol: coal left in a chest can't fuel a block or count toward
        the next one (waiting for 'low' kept coal at 307 for 7 minutes while the chests filled)."""
        w, crew = self.crew.world, self.crew
        if not crew.mine_chests:
            return
        for x, y in crew.mine_chests:
            w.near(x, y)
            r = w.run.extract(x, y, "coal", 500)
            if r["ok"]:
                w.inventory["coal"] = w.inventory.get("coal", 0) + r["data"].get("extracted", 0)
                self.restocked += r["data"].get("extracted", 0)

    def patrol(self) -> None:
        w = self.crew.world
        self.restock()
        blocks = {f"block {b.id}": b for b in self.crew.blocks}
        for name, sx, sy in self.crew.sites():
            b = blocks.get(name)
            w.near(sx, sy, dist=4)
            d = w.observe(12)
            problems: list[str] = []
            for e in d.get("entities") or []:
                verdict = classify(e)
                if not verdict:
                    continue
                problem, remedy = verdict
                if remedy == "fuel" and w.inventory.get("coal", 0) >= 20:
                    w.fuel(e["x"], e["y"], 20)
                    self.fixes += 1
                    continue
                key = (e["name"], e["x"], e["y"], problem)
                self._seen[key] = self._seen.get(key, 0) + 1
                if self._seen[key] >= 2:                  # seen twice in a row: not just warming up
                    problems.append(f"{name}: {e['name']} at ({e['x']}, {e['y']}): {problem}")
            if b is not None:
                b.problems = problems
            for p in problems:
                if p not in self.escalations:
                    self.escalations.append(p)


# ----------------------------------------------------------------- directors
class ScriptedDirector:
    """Free stand-in for the LLM: two red blocks, then finish. Used by tests and as a baseline."""
    name = "scripted"
    cost_usd = 0.0
    calls = 0

    def __init__(self, blocks: int = 2, green: bool = True):
        self.blocks, self.green = blocks, green

    def plan(self, report: dict) -> dict:
        self.calls += 1
        if report["queued_steps"]:
            return {"note": "the crew still has work queued", "steps": []}
        n = len(report["blocks"])
        built = report["blocks"]
        for b in built:
            if not b["copper"]:
                return {"note": f"block {b['id']} needs copper", "steps": [{"skill": "copper_feed", "args": {"block": b["id"]}}]}
            if not b["powered"]:
                skill = "power" if not any(x["powered"] for x in built) else "connect_power"
                return {"note": f"block {b['id']} needs power", "steps": [{"skill": skill, "args": {"block": b["id"]}}]}
        factories = report.get("factories", [])
        if (self.green and n >= 1 and not factories and report["game_minutes_left"] > 8
                and "factory" not in report["failed_skills"]):
            return {"note": "tier 1 is up: now a green science factory for tier 2",
                    "steps": [{"skill": "factory", "args": {"item": "logistic-science-pack", "per_minute": 10}}]}
        if 1 <= n < self.blocks and report["inventory"]["coal"] < BLOCK_COAL:   # a block needs ~400 coal to run to minute 30
            if not report["coal_mine"] and "coal_mine" not in report["failed_skills"]:
                return {"note": "coal for a second block", "steps": [{"skill": "coal_mine", "args": {}}]}
            if report["coal_mine"] and report["game_minutes_left"] > 8:
                return {"note": "waiting for the coal mine to fill", "steps": [{"skill": "wait", "args": {"seconds": 60}}]}
            return {"note": "not enough coal for another block", "steps": [{"skill": "finish", "args": {}}]}
        if n < self.blocks and report["game_minutes_left"] > 6 and not report["failed_skills"].get("iron_block"):
            return {"note": "another red block", "steps": [
                {"skill": "iron_block", "args": {}}, {"skill": "copper_feed", "args": {"block": n}},
                {"skill": "power" if n == 0 else "connect_power", "args": {"block": n}}]}
        return {"note": "nothing left worth building", "steps": [{"skill": "finish", "args": {}}]}


DIRECTOR_PROMPT = """You direct a real-time Factorio build. You never act yourself: a scripted crew carries out your
steps at full speed, and a foreman refuels machines on its own. The game keeps running while you think.

Goal (scored only in the hands-off window, game minutes 20 to 30, however early the build ends):
- Tier 1 = red science at 3 or more per minute, made from plates and gears the factory makes itself. Worth 100,000.
- Tier 2 = green science at 10 per minute (200,000). Tier 3 = green at 30 per minute (300,000).
  In every tier each intermediate must be made in the window at least 0.8x as fast as it is used.
- Points on top for every item made in the window: plate 1, cable 1, gear 3, belt 4, circuit 8, inserter 15,
  red 10, green 40 (capped at 99,999).
A sound plan: one red block with copper and power first (tier 1 in a few minutes), then "factory" for green
science at 10/min. A factory that fails for lack of room or kit is not worth retrying at the same rate.

Skills (one block makes about 6 red per minute once it has copper and power):
{skills}

A block needs copper_feed and power; the first block gets "power", later blocks "connect_power".
A skill takes the crew roughly 1 to 3 game minutes. Failed skills and foreman problems are in the report;
don't repeat a step that failed for lack of room.

Reply with JSON only: {{"note": "one short sentence", "steps": [{{"skill": "...", "args": {{...}}}}, ...]}}
with 1 to 4 steps."""


class LLMDirector:
    """Asks a chat model for the next steps. OpenAI-compatible, JSON output, cost-capped."""

    def __init__(self, client: Any, model: str, max_cost_usd: float = 1.0, log=print):
        import openai_bot  # prices
        self.client, self.model, self.max_cost, self.log = client, model, max_cost_usd, log
        self._prices = openai_bot
        self.name = f"llm:{model}"
        self.cost_usd = 0.0
        self.calls = 0
        self.notes: list[dict] = []

    def plan(self, report: dict) -> dict:
        if self.cost_usd >= self.max_cost:
            return {"note": "director cost cap reached", "steps": [{"skill": "finish", "args": {}}]}
        skills = "\n".join(f"- {k}: {v}" for k, v in SKILLS.items())
        msgs = [{"role": "system", "content": DIRECTOR_PROMPT.format(skills=skills)},
                {"role": "user", "content": json.dumps(report, separators=(",", ":"))}]
        resp = self.client.chat.completions.create(model=self.model, messages=msgs,
                                                   response_format={"type": "json_object"})
        self.calls += 1
        u = resp.usage
        cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
        self.cost_usd += self._prices.cost_usd(self.model, u.prompt_tokens, cached, u.completion_tokens) or 0.0
        text = resp.choices[0].message.content or "{}"
        try:
            out = json.loads(text)
        except json.JSONDecodeError:
            out = {"note": "unreadable reply", "steps": []}
        steps = [s for s in out.get("steps") or [] if isinstance(s, dict) and s.get("skill") in SKILLS][:4]
        out = {"note": str(out.get("note", ""))[:200], "steps": steps}
        self.notes.append({"minute": report["game_minute"], **out})
        return out


# ---------------------------------------------------------------------- play
def report(crew: Crew, foreman: Foreman, run: Run, history: list[dict], queued: list[dict]) -> dict:
    b = run.budgets
    left = b.get("ticks_left", 0) / TICKS_PER_MINUTE
    inv = crew.world.inventory
    keep = ("burner-mining-drill", "stone-furnace", "assembling-machine-1", "inserter", "transport-belt",
            "small-electric-pole", "boiler", "steam-engine", "iron-chest", "coal")
    failed: dict[str, str] = {}
    for h in history:
        if not h["ok"]:
            failed[h["skill"]] = h["result"]
    return {
        "game_minute": round(20 - left, 1), "game_minutes_left": round(left, 1),
        "score_so_far": (run.last or {}).get("score_so_far"),
        "blocks": [{"id": x.id, "copper": x.copper, "powered": x.powered, "problems": x.problems[:5]}
                   for x in crew.blocks],
        "power_station": crew.power_pole is not None,
        "coal_mine": bool(crew.mine_drills),
        "factories": [{"item": f["item"], "per_minute": f["per_minute"], "built": f["built"],
                       "failed": f["failed"][:3]} for f in crew.factories],
        "coal_restocked_so_far": foreman.restocked,
        "inventory": {k: inv.get(k, 0) for k in keep},
        "nearest": {k: v.get("distance") for k, v in crew.world.summary.items()},
        "recent_steps": history[-6:],
        "failed_skills": failed,
        "queued_steps": queued,
        "foreman_open_problems": foreman.escalations[-6:],
        "foreman_fuel_fixes": foreman.fixes,
    }


def fresh_report(crew: Crew, foreman: Foreman, run: Run, history: list[dict], queued: list[dict]) -> dict:
    crew.world.observe(8)                 # current inventory and clock for the director
    return report(crew, foreman, run, history, queued)


def play(run: Run, director=None, log=print, patrol_seconds: float = 20.0, placer: str = "search",
         designer=None) -> dict:
    """placer: how factory layouts are drawn: 'search' (free), 'llm' (designer draws), 'best' (both)."""
    run = run if isinstance(run, RealtimeRun) else RealtimeRun.wrap(run)
    director = director or ScriptedDirector()
    crew = Crew(run, log)
    crew.placer, crew.designer = placer, designer
    foreman = Foreman(crew, log)
    history: list[dict] = []
    queue: list[dict] = []
    pool = ThreadPoolExecutor(max_workers=1)
    pending: Future | None = None
    finished = False
    last_patrol = time.monotonic()
    try:
        crew.start()
        pending = pool.submit(director.plan, fresh_report(crew, foreman, run, history, queue))
        while not finished and run.budgets.get("ticks_left", 1) > 600:
            if pending is not None and (pending.done() or not queue):
                if not queue:                         # nothing to do: patrol while the director thinks
                    while not pending.done():
                        if crew.blocks and time.monotonic() - last_patrol > patrol_seconds:
                            foreman.patrol()
                            last_patrol = time.monotonic()
                        else:
                            time.sleep(0.5)
                plan = pending.result()
                pending = None
                log(f"[director] {plan['note']} -> {[s['skill'] for s in plan['steps']]}")
                queue.extend(plan["steps"])
                if not queue:                         # the director has nothing more to add
                    queue.append({"skill": "finish", "args": {}})
            if not queue:
                if pending is None:
                    pending = pool.submit(director.plan, fresh_report(crew, foreman, run, history, queue))
                continue
            step = queue.pop(0)
            if step["skill"] == "finish":
                finished = True
                break
            if step["skill"] == "wait":
                secs = min(300.0, max(10.0, float((step.get("args") or {}).get("seconds", 60))))
                until = time.monotonic() + secs
                while time.monotonic() < until and run.budgets.get("ticks_left", 1) > 600:
                    foreman.patrol()
                    last_patrol = time.monotonic()
                    time.sleep(min(10.0, max(0.0, until - time.monotonic())))
                history.append({"skill": "wait", "args": {"seconds": secs}, "ok": True,
                                "result": f"waited, coal now {crew.world.inventory.get('coal', 0)}",
                                "game_minutes": round(secs / 60, 1)})
                if pending is None:
                    pending = pool.submit(director.plan, fresh_report(crew, foreman, run, history, queue))
                continue
            t0 = run.budgets.get("ticks_left", 0)
            try:
                result = crew.run_skill(step["skill"], step.get("args") or {})
                ok = True
            except ab.BuildError as exc:
                result, ok = str(exc)[:200], False
            minutes = round((t0 - run.budgets.get("ticks_left", 0)) / TICKS_PER_MINUTE, 1)
            history.append({"skill": step["skill"], "args": step.get("args") or {}, "ok": ok,
                            "result": result, "game_minutes": minutes})
            log(f"[crew] {step['skill']} {step.get('args') or ''}: {'ok' if ok else 'FAILED'} - {result} "
                f"({minutes} min)")
            if crew.blocks and time.monotonic() - last_patrol > patrol_seconds:
                foreman.patrol()
                last_patrol = time.monotonic()
            if pending is None and (len(queue) <= 1 or not ok or foreman.escalations):
                pending = pool.submit(director.plan, fresh_report(crew, foreman, run, history, queue))
                foreman.escalations.clear()
        if crew.blocks:
            foreman.patrol()                          # a last fuel round before the window
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    result = run.end_and_wait()
    result["bot"] = {"kind": "crew", "director": director.name, "director_calls": director.calls,
                     "cost_usd": round(director.cost_usd, 4), "steps": history,
                     "foreman_fuel_fixes": foreman.fixes, "coal_restocked": foreman.restocked,
                     "director_notes": getattr(director, "notes", []),
                     "factories": crew.factories,
                     "designer_cost_usd": round(getattr(crew.designer, "cost_usd", 0.0), 4)}
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("ARENA_URL", "http://127.0.0.1:8710"))
    ap.add_argument("--token", default=os.environ.get("ARENA_TOKEN", ""))
    ap.add_argument("--task", default="automation-realtime")
    ap.add_argument("--director", choices=["scripted", "openai"], default="scripted")
    ap.add_argument("--model", default="gpt-6.1-terra")
    ap.add_argument("--max-cost", type=float, default=1.0)
    args = ap.parse_args()
    client = ArenaClient(args.url, args.token)
    run = client.start_run(args.task)
    director = None
    if args.director == "openai":
        import openai_bot
        from dotenv_lite import load_env
        load_env()
        director = LLMDirector(openai_bot.make_client("openai", None, None), args.model, args.max_cost)
    print(json.dumps(play(run, director), indent=2))


if __name__ == "__main__":
    main()
