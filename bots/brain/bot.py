"""The brain bot's main loop: the strategist plans in the background, the crew works through its goals.

    python -m brain.bot --url http://127.0.0.1:8720 --token fa_... --task freeplay-green
    arena local-run --backend factorio --task freeplay-green --bot brain [--strategist openai --model ...]

The strategist is asked again when its goals run out, when a milestone is reached, after two failures in a
row, and at least every `replan_minutes` of game time. A goal that raises anything (even a bug) is recorded
as a failure for the report and the run goes on: one bad step never voids a run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "..", "sdk"))

from arena_sdk import ArenaClient, ArenaError, RealtimeRun, Run  # noqa: E402

from .brain import ScriptedStrategist, report  # noqa: E402
from .assess import Supervisor  # noqa: E402
from .crew import DEAD_RUN, Crew  # noqa: E402
from .fixer import RuleFixer, apply as apply_fixes, situation as fix_situation  # noqa: E402
from .knowledge import Knowledge  # noqa: E402
from .modules import MODULES  # noqa: E402
from .nodes import consumer_block  # noqa: E402
from .overseer import drop_forbidden, forbidden, same_target  # noqa: E402
from . import persist, upkeep  # noqa: E402
from .telemetry import Telemetry  # noqa: E402
from .watchdog import Watchdog  # noqa: E402
from .world import BuildError, FreeWorld, Preempted  # noqa: E402


def play(run: Run, client: ArenaClient, task: dict, strategist=None, log=print, replan_minutes: float = 8.0,
         stop_minutes_left: float = 0.3, telemetry_dir=None, designer=None, placer: str = "fallback",
         assessor=None, fixer=None, chiplets=None, saver=None, resume_from: str | None = None,
         autosave_minutes: float = 20.0, overseer=None) -> dict:
    """telemetry_dir: where to write bot_live.json and bot_events.jsonl for the monitor page (usually the
    run's own log folder); None writes nothing. Every factory design's terrain is saved in its chiplets/
    folder, as cases for the chiplet bench.
    designer: a fab.llm_place.LLMDesigner that draws factory floor plans; placer: search | fallback (search,
    then the designer where search finds nothing) | llm (the designer first, search when it fails).
    assessor: an assess.LLMAssessor that watches the base and amends the plan as problems appear (None: the
    supervisor's rule floor only, which still answers fuel and power at once).
    chiplets: a designer.ChipletDesigner (--designs ai | ai-only): the AI draws the steam station fresh this run,
    in the background from the start, checked before use (None: the hand-made library).
    saver: saver(name) saves the game and the arena session into telemetry_dir/saves (local-run passes it; None:
    no saves). The bot then saves every `autosave_minutes` of game time and when the monitor page asks
    (control.json: save, or stop = save and end the run), always between goals. resume_from: a save name in
    telemetry_dir/saves ("" = the newest) whose bot state to load: the game itself was already loaded by the
    arena (local-run --resume).
    overseer: an overseer.Overseer: every few game minutes a deep-thinking model reads everything and gives
    orders that run before anything else (None: no overseer)."""
    speed = float(task.get("clock_speed", 1.0))
    run = run if isinstance(run, RealtimeRun) else RealtimeRun.wrap(run, speed=speed)
    kn = Knowledge(client.catalog())
    world = FreeWorld(run, kn, log=log, speed=speed)
    world.scan_radius = int((task.get("limits") or {}).get("scan_radius") or 192)
    rng = (task.get("limits") or {}).get("observe_range")
    world.observe_range = int(rng) if rng is not None else 60       # how far it may look without walking
    crew = Crew(world, log=log)
    factory = MODULES["factory"]
    factory.designer, factory.placer = designer, placer
    factory.case_dir = Path(telemetry_dir) / "chiplets" if telemetry_dir else None
    MODULES["workshop"].case_dir = Path(telemetry_dir) / "chiplets" if telemetry_dir else None
    MODULES["steam_power"].designer = chiplets          # set every run: a class attribute outlives the run
    crew.designs = chiplets.records if chiplets is not None else []
    if chiplets is not None:
        chiplets.prefetch(kn, MODULES["steam_power"].library_template)
    strategist = strategist or ScriptedStrategist()
    pool = ThreadPoolExecutor(max_workers=1)
    pending: Future | None = None
    goals: list[dict] = []
    results: list[dict] = []
    seen_milestones: set[str] = set()
    last_plan_tick = -1e9
    fails_in_row = 0
    notes: list[dict] = []
    alerted: set[str] = set()                   # urgent alert keys the strategist was already asked about
    resume: dict | None = None                  # the goal the supervisor paused, to finish after its fixes
    jams: dict[str, int] = {}                   # crew faults (one error on many goals) -> times seen
    fixer = fixer or RuleFixer()
    skipped: dict[str, float] = {}              # goal key -> game tick until which the fixer set it aside
    consulted: set[str] = set()                 # goals the fixer already looked at for repeated failures
    crew.fix_notes = []

    def consult(error: str, goal: dict, why: str) -> None:
        """Ask the fixer (model or rules) and carry out its fixes now."""
        try:
            sit = fix_situation(crew, error, goal, [r for r in results if not r["ok"]])
            d = fixer.diagnose(sit)
            key = json.dumps({k: v for k, v in goal.items() if k != "reflex"}, sort_keys=True)
            done = apply_fixes(crew, d.get("fixes") or [], goals, skipped, key)
        except ArenaError as exc:
            if exc.code in DEAD_RUN:
                raise
            done, d = [f"refused: {exc.code}"], {"note": ""}
        except Exception as exc:  # noqa: BLE001 - troubleshooting must never end the run
            done, d = [f"crashed: {type(exc).__name__}: {str(exc)[:100]}"], {"note": ""}
        note = {"minute": round(world.now_tick() / 3600, 1), "why": why, "error": error[:160],
                "note": d.get("note", ""), "done": done, "who": fixer.name}
        crew.fix_notes = (crew.fix_notes + [note])[-8:]
        log(f"[fixer] {why}: {d.get('note', '')} -> {done}")
        tel.event("fix", **note)
    # the trap watch (FreeWorld._dig_out) calls the fixer itself when digging out fails or the same trap comes
    # back: a trapped character fails no goal, so the circuit breaker below never sees it
    world.on_trapped = lambda why: consult(why, (crew.current or {}).get("goal") or {"do": "unstuck"}, "trapped")
    tel = Telemetry(telemetry_dir)
    tel.bind(world, crew, strategist, fixer=fixer, assessor=assessor, overseer=overseer)
    crew.strategist = strategist
    dog = Watchdog()
    crew.watchdog = dog
    crew.target_techs = [m["tech"] for m in (task.get("score") or {}).get("milestones", []) if m.get("tech")]
    crew.milestone_ladder = list((task.get("score") or {}).get("milestones", []))   # rate targets (roadmap.py)
    book = ScriptedStrategist()                 # the other planner, for the watchdog's "switch" rung
    tel.set(task={"id": task.get("id"), "title": task.get("title")}, status="playing",
            can_save=saver is not None and telemetry_dir is not None)
    sup = Supervisor(assessor, log=log, event=tel.event, doctrine=lambda: getattr(strategist, "doctrine", ""),
                     overseer=overseer, report_fn=lambda: (safe_report(), results))
    crew.supervisor = sup

    saves_dir = Path(telemetry_dir) / "saves" if telemetry_dir else None
    stop_requested = False
    last_save_tick = world.now_tick()

    def save_point(kind: str) -> str | None:
        """Game, session and bot state under one name, between goals (nothing half-built in hand)."""
        nonlocal last_save_tick
        if saver is None or saves_dir is None:
            return None
        try:
            minute = world.now_tick() / 3600
            name = f"m{int(minute):05d}-{kind}-{time.strftime('%H%M%S')}"
            world.doing(f"saving the game ({kind})")
            state = {"crew": crew, "world": world, "strategist": strategist, "supervisor": sup, "watchdog": dog,
                     "loop": {"goals": goals, "results": results, "notes": notes,
                              "seen_milestones": seen_milestones, "fails_in_row": fails_in_row,
                              "alerted": alerted, "resume": resume, "jams": jams, "skipped": skipped,
                              "consulted": consulted}}
            summary = {"kind": kind, "minute": round(minute, 1), "milestones": sorted(world.milestones),
                       "sites": len(crew.base.sites), "researched": len(kn.researched),
                       "goal_queue": [_short(g) for g in goals[:6]]}
        except Exception as exc:  # noqa: BLE001 - a failed save never ends the run
            log(f"[bot] save skipped: {type(exc).__name__}: {str(exc)[:200]}")
            last_save_tick = world.now_tick()
            return None
        last_save_tick = world.now_tick()          # not again at once, even when this one fails
        return name if persist.save_point(saver, saves_dir, name, state, summary, tel=tel, log=log) else None

    if resume_from is not None and saves_dir is not None:     # carry on where a saved run stopped
        got = persist.read(saves_dir, resume_from or None)
        if got is None:
            raise RuntimeError(f"no bot save {resume_from!r} in {saves_dir}")
        name, data = got
        loop = persist.restore(data, crew, world, strategist, sup, dog)
        goals, results, notes = loop.get("goals", []), loop.get("results", []), loop.get("notes", [])
        seen_milestones = set(loop.get("seen_milestones", set()))
        alerted, jams = set(loop.get("alerted", set())), dict(loop.get("jams", {}))
        skipped, consulted = dict(loop.get("skipped", {})), set(loop.get("consulted", set()))
        resume = loop.get("resume")
        crew.target_techs = crew.target_techs or [m["tech"] for m in (task.get("score") or {}).get("milestones", [])
                                                   if m.get("tech")]
        crew.milestone_ladder = list((task.get("score") or {}).get("milestones", []))   # the task as it is now
        log(f"[bot] resumed from save {name}: {len(crew.base.sites)} sites, {len(crew.links)} links, "
            f"{len(goals)} goals queued")
        tel.event("resume", name=name, sites=len(crew.base.sites), links=len(crew.links))

    def safe_report() -> dict:
        try:
            return report(crew, task, results)
        except Exception as exc:  # noqa: BLE001 - a bug in the report must never end the run: a short one
            log(f"[bot] report failed ({type(exc).__name__}: {exc}); planning from a short report")
            return {"game_minute": round(world.now_tick() / 3600, 1), "base": [s.brief() for s in crew.base.sites],
                    "recent_results": results[-8:], "researched": sorted(kn.researched), "research": world.research,
                    "inventory": dict(list(world.inventory.items())[:25]), "report_error": str(exc)[:200]}

    def ask(reason: str) -> Future:
        nonlocal last_plan_tick
        last_plan_tick = world.now_tick()
        try:
            world.fresh_stats()                     # the plan is made from this minute's research and rates
        except ArenaError as exc:
            if exc.code in DEAD_RUN:
                raise
        except Exception:  # noqa: BLE001 - stale numbers are better than no plan
            pass
        rep = safe_report()
        rep["stuck"] = dog.status(world.now_tick())
        tel.state["strategist"]["thinking_since"] = time.time()
        tel.state["strategist"]["reason"] = reason
        tel.state["strategist"]["cost_at_ask"] = strategist.cost_usd
        tel.state["strategist"]["ready_since"] = None
        tel.event("ask", reason=reason)
        fut = pool.submit(strategist.plan, rep)

        def answered(f: Future) -> None:          # the model is done: stop the "thinking" clock right here
            st = tel.state["strategist"]
            st["answer_s"] = round(time.time() - (st.get("thinking_since") or time.time()), 1)
            st["thinking_since"], st["ready_since"] = None, time.time()
        fut.add_done_callback(answered)
        return fut

    def recover(action: str, queued: list[dict]) -> list[dict]:
        """One rung of the watchdog's ladder. Returns the goal queue to carry on with."""
        nonlocal pending
        st = dog.status(world.now_tick())
        log(f"[watchdog] no progress for {st['minutes_without_progress']} min (last: {st['last_progress']}): {action}")
        tel.event("watchdog", action=action, **st)
        if overseer is not None and action in ("refresh", "switch", "expand"):
            # the overseer decides what replaces a stalled plan: its plan is not swapped for the opening book's
            # or buried under supply goals (run 20261119-202811: switch and expand overrode its rescue plan)
            if action == "refresh":
                crew.refresh_world()
            sup.overseer_soon(world.now_tick())
            return queued
        if action == "refresh":
            crew.refresh_world()
            return []                                   # drop the stale plan; the replan below sees the stall
        if action == "switch":
            other = book if strategist is not book and not isinstance(strategist, ScriptedStrategist) else None
            if other is not None:
                plan = other.plan(safe_report())
                log(f"[watchdog] the opening book takes over for one plan: {plan.get('note')}")
                return list(plan.get("goals") or [])
            return crew.supply_goals()                  # the book was planning: try supply instead
        if action == "expand":
            return crew.supply_goals() + queued
        if action == "restart":
            crew.foreman.note(f"stalled for {st['minutes_without_progress']} min; the recovery ladder starts over")
        return queued

    plan_seen = crew.overseer_plan_tick                 # the overseer's last plan the loop has taken up
    try:
        world.observe(32)
        world.scan()
        world.read_stats()
        try:                                    # pipe lines laid before the segment limit was known (pipes.py)
            for note in crew.split_pipelines():
                log(f"[bot] {note}")
                tel.event("pumps", note=note)
        except Exception as exc:  # noqa: BLE001 - a repair never stops the run
            log(f"[bot] pipe line repair failed: {type(exc).__name__}: {str(exc)[:160]}")
        log(f"[bot] patches: {[(p['name'], p['tiles'], p['distance']) for p in world.patches[:8]]}")
        pending = ask("first plan" if resume_from is None else "resumed from a save: check the plan still fits")
        if saves_dir is not None:
            tel.set(saves=persist.index(saves_dir)[-12:])
        while world.minutes_left > stop_minutes_left:
            world.enter("take-goal")
            asked = persist.take_request(telemetry_dir) if telemetry_dir else None
            due = saver is not None and autosave_minutes and \
                world.now_tick() - last_save_tick >= autosave_minutes * 3600
            if asked or due:
                saved = save_point("stop" if asked == "stop" else "manual" if asked else "auto")
                if asked == "stop":
                    if saved:
                        stop_requested = True
                        log(f"[bot] stopping as asked: carry on later from save {saved}")
                        break
                    crew.foreman.note("stop asked, but the save failed: playing on")
            spent = spent_budgets({"strategist": strategist, "overseer": overseer, "assessor": assessor,
                                   "fixer": fixer, "designer": designer})
            if spent and saver is not None:
                # a model's cost cap ran out: save and stop between goals, so the run carries on with new caps
                # (arena local-run --resume <id> --max-cost ... ; costs count from zero again after a resume)
                saved = save_point("budget")
                if saved:
                    stop_requested = True
                    log(f"[bot] cost cap reached ({', '.join(spent)}): saved as {saved} and stopping; resume "
                        "with higher caps to carry on")
                    tel.event("budget_stop", spent=spent, save=saved)
                    break
                crew.foreman.note(f"cost cap reached ({', '.join(spent)}) but the save failed: playing on")
            if pending is not None and (pending.done() or (not goals and not crew.urgent)):
                while not pending.done() and not crew.urgent:   # nothing queued: tend the base while it thinks
                    crew.foreman.patrol()
                    crew.checkpoint()
                    time.sleep(0.5)
            if pending is not None and pending.done():
                try:
                    plan = pending.result()
                except Exception as exc:  # noqa: BLE001
                    log(f"[brain] strategist failed: {exc}")
                    plan = ScriptedStrategist().plan(safe_report())
                pending = None
                log(f"[brain] {plan.get('note', '')} -> {[_short(g) for g in plan.get('goals', [])]}")
                st = tel.state["strategist"]
                held = round(time.time() - (st.get("ready_since") or time.time()), 1)
                last = {"note": plan.get("note"), "doctrine": plan.get("doctrine"), "goals": plan.get("goals"),
                        "minute": round(world.now_tick() / 3600, 1),
                        "seconds": plan.get("latency_s", st.get("answer_s", 0)), "waited_for_crew_s": held,
                        "reason": tel.state["strategist"].get("reason"),
                        "cost_usd": round(strategist.cost_usd - tel.state["strategist"].get("cost_at_ask", 0.0), 5)}
                tel.state["strategist"].update(thinking_since=None, ready_since=None, last=last)
                tel.event("plan", **last)
                notes.append({"minute": round(world.now_tick() / 3600, 1), "note": plan.get("note"),
                              "goals": plan.get("goals")})
                if crew.overseer_plan_tick > last_plan_tick:    # asked before the overseer replaced the plan
                    log("[brain] this plan was asked for before the overseer's plan: keeping the overseer's")
                else:
                    goals = list(plan.get("goals") or [])
                    banned_now = drop_forbidden(crew, goals)    # never left waiting in the queue: the assessor
                    if banned_now:                              # asked for replan after replan to clear them
                        log(f"[brain] forbidden by the overseer, left out of the plan: {banned_now}")
                    goals = goals or [{"do": "wait", "seconds": 30}]
                if resume is not None and forbidden(crew, resume) is not None:
                    resume = None                       # forbidden since it was paused: it does not come back
                if resume is not None and resume not in goals:   # a paused goal survives a new plan
                    goals.insert(0, resume)
            if crew.overseer_plan_tick != plan_seen:    # the overseer replaced the plan: carry on with its
                plan_seen = crew.overseer_plan_tick
                goals = crew.plan_queue
            # the supervisor's fixes, then the close-out (the planned trip back before the next tier), then the
            # plan; but the plan never waits more than upkeep.PLAN_STARVE_MIN behind the other two (aging)
            goal, source, waited = upkeep.next_goal(crew, goals, world.now_tick())
            urgent, aged = source == "urgent", source == "aged"
            if aged:
                log(f"[bot] the plan waited {waited:.0f} min behind fixes and the close-out: {_short(goal)} goes "
                    "first, and only base-wide trouble may pause it")
                tel.event("aged", goal=_short(goal), waited_min=round(waited, 1),
                          passed=len(crew.urgent) + len(crew.closeout_queue))
            gkey = json.dumps({k: v for k, v in goal.items() if k != "reflex"}, sort_keys=True)
            if not urgent and skipped.get(gkey, -1) > world.now_tick():    # the fixer set it aside
                results.append({"goal": goal, "ok": False, "game_minutes": 0,
                                "result": "set aside by the fixer for now (see fixer notes)"})
                continue
            banned = forbidden(crew, goal)
            if banned is not None:                      # the overseer refused it: not a failure of the crew
                results.append({"goal": goal, "ok": False, "game_minutes": 0,
                                "result": f"forbidden by the overseer: {banned.get('why', '')}"[:200]})
                continue
            try:
                held = consumer_block(crew, goal)
            except Exception as exc:  # noqa: BLE001 - a bug in the gate never stops the run
                held, _ = None, crew.log(f"[gate] check failed: {str(exc)[:120]}")
            if held is not None:                        # its item's factories already starve: not a failure either
                results.append({"goal": goal, "ok": False, "game_minutes": 0, "result": held[:400]})
                tel.event("gate", goal=_short(goal), why=held[:200])
                continue
            crew.plan_queue = goals
            if goal == resume:
                resume = None
            t0 = world.now_tick()
            crew.current = {"goal": goal, "tick": t0, "urgent": urgent, **({"aged": True} if aged else {})}
            world.last_progress_tick = t0               # the stall guard counts from the start of this goal
            paused = False
            tel.set(current_goal={"goal": goal, "text": _short(goal), "since_minute": round(t0 / 3600, 1),
                                  "budget_min": crew.goal_budget({k: v for k, v in goal.items() if k != "reflex"}),
                                  "who": str(goal.get("reflex", "plan")).split(":")[0] or "plan"},
                    queue=[_short(g) for g in goals])
            tel.event("goal_start", goal=_short(goal))
            world.enter("run-goal")
            try:
                msg, ok = crew.run_goal(goal), True
                fails_in_row = 0
            except Preempted as exc:                    # something more urgent: do it, then carry on with this
                msg, ok, paused = f"paused for: {exc}", True, True
                if crew.drop_current:                   # the overseer or the stall guard: not again
                    crew.drop_current = False
                    stalled = str(exc).startswith(("stalled", "over budget"))
                    msg = f"dropped: {exc}" if stalled else f"dropped by the {exc}"
                    if stalled:                         # a failure: the fixer and the planners see it
                        ok, paused = False, False
                        fails_in_row += 1
                    if resume == goal:
                        resume = None
                elif goal.get("do") != "wait" and forbidden(crew, goal) is None:   # a pause is pointless to
                    goals.insert(0, goal)                                         # resume, a forbidden goal too
                    resume = goal
            except BuildError as exc:
                msg, ok = str(exc)[:240], False
                fails_in_row += 1
            except ArenaError as exc:
                if exc.code in DEAD_RUN:
                    log(f"[bot] the run is over: {exc.code} {exc.message}")
                    break
                msg, ok = f"refused: {exc.code}: {exc.message[:400]}", False
                fails_in_row += 1
            except Exception as exc:  # noqa: BLE001 - a bug in one goal must never void the run
                msg, ok = f"crashed: {type(exc).__name__}: {str(exc)[:200]}", False
                fails_in_row += 1
                log(traceback.format_exc())
            finally:
                crew.current = None
                if crew.preempt_for == t0:              # a pause asked for this goal never came: it ended first
                    crew.clear_pause()
            world.enter("record")
            minutes = round((world.now_tick() - t0) / 3600, 1)
            results.append({"goal": goal, "ok": ok, "result": msg, "game_minutes": minutes,
                            **({"paused": True} if paused else {}), **({"urgent": True} if urgent else {})})
            from .overseer import same_target       # repairs that keep coming back are held (nodes.repeat_block)
            crew.fix_log = (list(getattr(crew, "fix_log", None) or [])
                            + [(repr(same_target(goal)), round(world.now_tick() / 3600, 1), bool(ok and not paused))])[-80:]
            tel.set(current_goal=None, results=[{**r, "goal": _short(r["goal"])} for r in results[-40:]])
            tel.event("goal_end", goal=_short(goal), ok=ok, result=msg, game_minutes=minutes)
            log(f"[crew] {_short(goal)}: {'PAUSED' if paused else 'ok' if ok else 'FAILED'} - {msg} ({minutes} min)")
            # circuit breaker: the same error on different goals is a fault in the crew (walled in, a stuck
            # chest, a full inventory), not in the plan. Replanning cannot fix it (a run once asked the model
            # ~100 times about one unreachable chest): dig out, refresh, and back off before asking again.
            if not ok:
                sig = _signature(msg)
                same = [r for r in results[-4:] if not r["ok"] and _signature(r["result"]) == sig]
                if len(same) >= 3 and len({json.dumps(r["goal"], sort_keys=True) for r in same}) >= 2:
                    jams[sig] = jams.get(sig, 0) + 1
                    hold = min(300, 60 * 2 ** (jams[sig] - 1))
                    log(f"[bot] the same failure hit {len(same)} different goals ({msg[:100]}): a crew fault, "
                        f"not the plan; unjamming, then {hold} s before more of the plan")
                    tel.event("jam", error=msg[:200], times=jams[sig], hold_s=hold)
                    crew.foreman.note(f"crew fault seen on {len(same)} different goals: {msg[:100]}")
                    consult(msg, goal, "the same error on different goals")
                    try:
                        log(f"[bot] unjam: {crew.unjam(msg)}")
                    except (BuildError, ArenaError) as exc:
                        if isinstance(exc, ArenaError) and exc.code in DEAD_RUN:
                            raise
                        crew.foreman.note(f"unjam failed: {str(exc)[:120]}")
                    fails_in_row = 0
                    goals.insert(0, {"do": "wait", "seconds": hold})
                else:
                    # one goal failing again and again: the engineer looks at why before the planner asks again
                    key = json.dumps({k: v for k, v in goal.items() if k != "reflex"}, sort_keys=True)
                    n = sum(1 for r in results if not r["ok"] and json.dumps(
                        {k: v for k, v in r["goal"].items() if k != "reflex"}, sort_keys=True) == key)
                    if n >= 3 and key not in consulted:
                        consulted.add(key)
                        consult(msg, goal, f"this goal failed {n} times")
            try:
                world.read_stats()
            except Exception:  # noqa: BLE001
                pass
            if any(s.params.get("needs_pumps") for s in crew.base.sites) and \
                    world.now_tick() - getattr(crew, "_pumps_tried", -1e9) > 10 * 3600:
                crew._pumps_tried = world.now_tick()       # a long line waiting for pumps: can they be made now?
                try:
                    for note in crew.split_pipelines():
                        log(f"[bot] {note}")
                        tel.event("pumps", note=note)
                except (BuildError, ArenaError) as exc:
                    if isinstance(exc, ArenaError) and exc.code in DEAD_RUN:
                        raise
                    log(f"[bot] pipe line repair failed: {str(exc)[:160]}")
            new = set(world.milestones) - seen_milestones
            if new:
                seen_milestones |= new
                log(f"[bot] milestone(s) reached: {sorted(new)} at minute {world.now_tick() / 3600:.1f}")
                tel.event("milestone", ids=sorted(new))
            dog.observe(world.now_tick(), None if paused else results[-1], new, len(kn.researched))
            action = dog.check(world.now_tick())
            if action:
                goals = recover(action, goals)
            tel.set(watchdog=dog.status(world.now_tick()))
            # the supervisor watched all along (and may have queued fixes or paused the goal); a new urgent
            # alert, or the assessor's word, asks the strategist now instead of at the next check-in
            sup.tick(crew, force=True)
            try:                                        # sites that run on their own get sealed; a finished
                upkeep.update_seals(crew)               # tier brings the planned trip back (close-out)
                why_close = upkeep.closeout_due(crew)
                if why_close:
                    fixes = upkeep.closeout(crew, why_close)
                    queued = {same_target(g) for g in crew.urgent}     # a job someone already queued is not
                    fixes = [g for g in fixes if same_target(g) not in queued and forbidden(crew, g) is None]
                    crew.closeout_queue = fixes                        # queued twice (run 20261120-092648)
                    log(f"[bot] close-out ({why_close}): {[_short(g) for g in fixes]}")
                    tel.event("closeout", why=why_close, goals=[_short(g) for g in fixes])
            except Exception as exc:  # noqa: BLE001 - upkeep bookkeeping never ends the run
                log(f"[bot] upkeep failed: {type(exc).__name__}: {str(exc)[:160]}")
            alerts = sup.alerts
            why = sup.take_replan()
            fresh = [a for a in alerts if a["level"] == "urgent" and a["key"] not in alerted]
            alerted.intersection_update({a["key"] for a in alerts})
            alerted.update(a["key"] for a in fresh)
            tel.set(alerts=alerts)
            world.enter("replan")
            # anti-spin: goals that finish instantly again and again (whoever planned them) get a pause, so a
            # plan that changes nothing cannot flood the server and the log
            quick = results[-6:]
            if len(quick) == 6 and all(r["game_minutes"] == 0 for r in quick) and not goals and not new:
                log("[bot] the last 6 goals changed nothing: letting the base run for 45 s before asking again")
                results.append({"goal": {"do": "wait", "seconds": 45}, "ok": True,
                                "result": crew.wait(45), "game_minutes": 0.75})
            replan = (not goals or bool(new) or fails_in_row >= 2 or bool(fresh) or bool(why)
                      or world.now_tick() - last_plan_tick > replan_minutes * 3600)
            if replan and pending is None:
                if fails_in_row >= 2:
                    goals = []                          # the plan went wrong: ask before doing more of it
                reason = ("two failures in a row" if fails_in_row >= 2 else "milestone reached" if new
                          else f"alert: {fresh[0]['what'][:100]}" if fresh else f"supervisor: {why}" if why
                          else "goals ran out" if not goals else "regular check-in")
                pending = ask(reason)
    except ArenaError as exc:
        if exc.code not in DEAD_RUN:
            raise
        log(f"[bot] the run is over: {exc.code} {exc.message}")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        sup.close()
        world.enter("end")
        tel.set(status="ending", current_goal=None)
        world.doing("run ending: waiting for the score")
    result = run.end_and_wait()
    tel.close(status="finished", score=result.get("score"), end_reason=result.get("end_reason"))
    result["bot"] = {"kind": "brain", "strategist": strategist.name, "strategist_calls": strategist.calls,
                     "cost_usd": round(strategist.cost_usd, 4), "doctrine": getattr(strategist, "doctrine", ""),
                     "results": results, "plans": notes, "base": [s.brief() for s in crew.base.sites],
                     "fuel_fixes": crew.foreman.fuel_fixes, "hand_packs": crew.foreman.hand_packs,
                     "placer": placer if designer is not None else "search",
                     "designer": getattr(designer, "model", None),
                     "designer_cost_usd": round(getattr(designer, "cost_usd", 0.0), 4),
                     "designer_calls": getattr(designer, "calls", 0),
                     "assessor": getattr(assessor, "name", "rules"),
                     "assessor_calls": getattr(assessor, "calls", 0),
                     "assessor_cost_usd": round(getattr(assessor, "cost_usd", 0.0), 4),
                     "supervisor_notes": sup.notes,
                     "overseer": getattr(overseer, "name", None),
                     "overseer_calls": getattr(overseer, "calls", 0),
                     "overseer_cost_usd": round(getattr(overseer, "cost_usd", 0.0), 4),
                     "overseer_notes": sup.overseer_notes,
                     "fixer": fixer.name, "fixer_calls": fixer.calls, "fixer_cost_usd": round(fixer.cost_usd, 4),
                     "fixer_notes": crew.fix_notes,
                     "paused_goals": sum(1 for r in results if r.get("paused")),
                     "stopped_for_resume": stop_requested}
    return result


def spent_budgets(parts: dict) -> list[str]:
    """Which model-driven parts have used up their cost cap (name: cost/cap), e.g. ["overseer $5.01/$5.00"]."""
    out = []
    for name, obj in parts.items():
        cap = getattr(obj, "max_cost", None)
        cost = getattr(obj, "cost_usd", 0.0) or 0.0
        if obj is not None and cap and cost >= cap:
            out.append(f"{name} ${cost:.2f}/${cap:.2f}")
    return out


def _signature(msg: str) -> str:
    """An error with its numbers taken out: the same fault at the same place reads the same."""
    return re.sub(r"-?\d+(\.\d+)?", "#", msg or "")[:90]


def _short(goal: dict) -> str:
    rest = {k: v for k, v in goal.items() if k not in ("do", "module")}
    return f"{goal.get('do')} {goal.get('module', '')} {rest}".replace("  ", " ")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("ARENA_URL", "http://127.0.0.1:8720"))
    ap.add_argument("--token", default=os.environ.get("ARENA_TOKEN", ""))
    ap.add_argument("--task", default="freeplay-green")
    ap.add_argument("--strategist", choices=["scripted", "openai"], default="scripted")
    ap.add_argument("--model", default="gpt-6.1-terra")
    ap.add_argument("--max-cost", type=float, default=2.0)
    args = ap.parse_args()
    client = ArenaClient(args.url, args.token)
    task = next(t for t in client.rules()["tasks"] if t["id"] == args.task)
    run = client.start_run(args.task)
    strategist = None
    if args.strategist == "openai":
        import openai_bot
        from dotenv_lite import load_env
        from .brain import LLMStrategist
        load_env()
        strategist = LLMStrategist(openai_bot.make_client("openai", None, None), args.model, task, args.max_cost)
    print(json.dumps(play(run, client, task, strategist), indent=2))


if __name__ == "__main__":
    main()
