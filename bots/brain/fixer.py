"""The fixer: the crew's troubleshooter and engineer, for the failures the plan cannot see into.

The strategist sees goals and their results; it cannot see that the character is wedged between two drills, or
that a workshop failed because one corner of the floor touches a lake. When the same error fails several goals
(the circuit breaker), or one goal keeps failing, the fixer is asked. It gets the error, the goal, the last
actions the game refused, and the buildings and ground around the character, and answers with fixes from a
small menu, which the crew carries out at once:

    {"fix": "escape"}                                 dig the character out by the map: the fewest buildings
                                                      between it and open ground, the way out kept open
    {"fix": "pickup", "x": 3.5, "y": -12.5}         pick up one of our buildings (the character is walled in)
    {"fix": "walk", "x": 10, "y": 4}                  walk there first
    {"fix": "remove_site", "site": 7, "force": false} take a site down (it is in the way)
    {"fix": "retry", "goal": {...}}                   the goal again, changed (smaller, elsewhere, another item)
    {"fix": "skip", "minutes": 20}                    drop that goal for a while; the strategist hears why
    {"fix": "wait", "seconds": 60}                    let the base run (a shortage that fills itself)

RuleFixer is the floor (always there, free): dig out, rescan, then skip. LLMFixer is a model with a cost cap; when
it fails or its budget is spent, the rules answer.
"""

from __future__ import annotations

import json
import math
import time
from typing import Any

FIXES = ("escape", "pickup", "walk", "remove_site", "retry", "skip", "wait")


def situation(crew, error: str, goal: dict | None, failures: list[dict]) -> dict:
    """One page: the error, the goal, what the game refused lately, and what stands around the character."""
    w = crew.w
    scene = []
    try:
        d = w.observe(10, include=["entities", "obstacles", "water"], detail="brief")
        for e in (d.get("entities") or [])[:40]:
            scene.append({k: e.get(k) for k in ("name", "x", "y", "status") if e.get(k) is not None})
        water = len(d.get("water") or [])
    except Exception:  # noqa: BLE001 - the page is best effort
        water = None
    owner = {}
    for s in crew.base.sites:
        for name, x, y, _ in s.entities:
            owner[(x, y)] = s.id
    for e in scene:
        if (e.get("x"), e.get("y")) in owner:
            e["site"] = owner[(e["x"], e["y"])]
    return {
        "error": error[:400],
        "goal": goal,
        "failures": [{"goal": f["goal"], "result": f["result"][:200]} for f in failures[-6:]],
        "character": {"at": [round(w.pos[0], 1), round(w.pos[1], 1)], "walk_fails": getattr(w, "walk_fails", 0),
                      "last_walk_fail": getattr(w, "walk_fail", None),
                      "dig_outs": list(getattr(w, "trap_notes", []))[-4:]},
        "map_around_character": _local_map(w),
        "refused_actions": list(getattr(w, "refused", []))[-12:],
        "around_character": scene,
        "water_tiles_near": water,
        "inventory_slots_free": 80 - crew.slots_used(),
        "sites": [{"id": s.id, "kind": s.kind, "at": [round(s.centre[0]), round(s.centre[1])],
                   **{k: v for k, v in s.params.items() if k in ("resource", "item", "count", "per_minute")}}
                  for s in crew.base.sites][:40],
    }


def _local_map(w) -> str | None:
    try:
        return w.local_map(14)
    except Exception:  # noqa: BLE001 - the page is best effort
        return None


class RuleFixer:
    name = "rules"
    cost_usd = 0.0
    calls = 0

    def diagnose(self, sit: dict) -> dict:
        err = sit["error"]
        fixes: list[dict] = []
        if "dig out" in err or "trapped" in err:
            fixes.append({"fix": "escape"})
        if "out_of_reach" in err or "no_path" in err or sit["character"]["walk_fails"]:
            near = sorted((e for e in sit["around_character"] if e.get("site") is not None),
                          key=lambda e: math.dist((e["x"], e["y"]), sit["character"]["at"]))
            if near and math.dist((near[0]["x"], near[0]["y"]), sit["character"]["at"]) < 2.5:
                fixes.append({"fix": "pickup", "x": near[0]["x"], "y": near[0]["y"]})
        fixes.append({"fix": "skip", "minutes": 15})
        return {"note": "rules: dig out if wedged, then set the goal aside", "fixes": fixes}


PROMPT = """You are the troubleshooter of a Factorio AI that plays through an API, one character, in real time.
A planner decides what to build; a scripted crew builds it. Something keeps failing and the planner cannot see
why. Read the situation (the error, the goal, the actions the game refused, the buildings around the character
with the site each belongs to, the sites, and map_around_character: rows north to south, @ the character,
letters our buildings, ~ water, # cliff, T tree or rock, . open ground, ? unseen) and fix it with up to 3 fixes,
done in order:

- {"fix": "escape"}                     dig the character out by the map (the fewest buildings between it and
                                        open ground; that way stays open). For a character shut in a pocket of
                                        its own buildings; character.dig_outs says what digging did before. When
                                        the same trap keeps coming back, also take down or move the site that
                                        forms it (remove_site) or set aside the goal that keeps walking in
- {"fix": "pickup", "x": .., "y": ..}   pick up one of OUR buildings (only ones listed around the character with a
                                        site); for a character wedged between buildings (out_of_reach, no_path
                                        everywhere) pick up the one it stands against
- {"fix": "walk", "x": .., "y": ..}     walk somewhere open first
- {"fix": "remove_site", "site": id, "force": false}   take a site down that a failure names as in the way
- {"fix": "retry", "goal": {goal}}      the failed goal changed so it can work: a smaller count or rate, another
                                        resource patch, an ingredient first (e.g. a workshop for what is short),
                                        a power station first when power is the cause
- {"fix": "skip", "minutes": n}         set the goal aside (it cannot work now); say why in the note
- {"fix": "wait", "seconds": n}         a shortage that fills itself (drills, cells still producing)

Goals use the planner's format:
{goals}

Reply with JSON only: {"note": "what is wrong, in one sentence", "fixes": [...]}"""


class LLMFixer:
    def __init__(self, client: Any, model: str, max_cost_usd: float = 1.0, log=print):
        import openai_bot
        from .brain import GOAL_HELP
        self.client, self.model, self.max_cost, self.log = client, model, max_cost_usd, log
        self._prices = openai_bot
        self.name = f"llm:{model}"
        self.cost_usd = 0.0
        self.calls = 0
        self.system = PROMPT.replace("{goals}", GOAL_HELP)   # the menu's JSON braces are literal
        self.rules = RuleFixer()
        self.timeout_s = 60.0

    def diagnose(self, sit: dict) -> dict:
        if self.cost_usd >= self.max_cost:
            out = self.rules.diagnose(sit)
            out["note"] = "fixer budget spent: " + out["note"]
            return out
        try:
            client = self.client.with_options(timeout=self.timeout_s, max_retries=0) \
                if hasattr(self.client, "with_options") else self.client
            resp = client.chat.completions.create(
                model=self.model, response_format={"type": "json_object"},
                messages=[{"role": "system", "content": self.system},
                          {"role": "user", "content": json.dumps(sit, separators=(",", ":"), default=str)}])
        except Exception as exc:  # noqa: BLE001 - a failed call falls back to the rules
            self.log(f"[fixer] model call failed ({str(exc)[:100]}): rules")
            return self.rules.diagnose(sit)
        self.calls += 1
        u = resp.usage
        if u is not None:
            cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
            self.cost_usd += self._prices.cost_usd(self.model, u.prompt_tokens, cached, u.completion_tokens) or 0.0
        try:
            out = json.loads(resp.choices[0].message.content or "{}")
        except json.JSONDecodeError:
            out = {}
        fixes = [f for f in out.get("fixes") or [] if isinstance(f, dict) and f.get("fix") in FIXES][:3]
        return {"note": str(out.get("note", ""))[:240], "fixes": fixes or self.rules.diagnose(sit)["fixes"]}


def apply(crew, fixes: list[dict], goals: list[dict], skipped: dict[str, float], key: str) -> list[str]:
    """Carry the fixes out. Retries go to the front of the goal queue; skips are remembered by goal key until
    their minute passes. Returns what was done, for the log and the strategist."""
    from .world import BuildError
    w, done = crew.w, []
    for f in fixes:
        kind = f.get("fix")
        try:
            if kind == "escape":
                taken = w.escape()
                done.append(f"dug out: took {[t[0] for t in taken]}")
                w.walk_fails = 0
            elif kind == "pickup":
                x, y = float(f["x"]), float(f["y"])
                e = next(((n, ex, ey, d) for s in crew.base.sites for n, ex, ey, d in s.entities
                          if abs(ex - x) < 0.6 and abs(ey - y) < 0.6), None)
                if e is None:
                    done.append(f"pickup ({x}, {y}): not one of ours")
                    continue
                ok = w.pickup(*e)
                for s in crew.base.sites:                       # forget it in its site (it is in the inventory)
                    if e in s.entities:
                        s.entities.remove(e)
                done.append(f"picked up {e[0]} at ({x}, {y})" if ok else f"pickup ({x}, {y}) failed")
                w.walk_fails = 0
            elif kind == "walk":
                w.go(float(f["x"]), float(f["y"]))
                done.append(f"walked to ({f['x']}, {f['y']})")
            elif kind == "remove_site":
                done.append(crew.remove(f.get("site"), force=bool(f.get("force"))))
            elif kind == "retry" and isinstance(f.get("goal"), dict) and f["goal"].get("do"):
                goals.insert(0, f["goal"])
                done.append(f"retry queued: {json.dumps(f['goal'])[:120]}")
            elif kind == "skip":
                skipped[key] = w.now_tick() + float(f.get("minutes", 15)) * 3600
                done.append(f"set aside for {f.get('minutes', 15)} min")
            elif kind == "wait":
                goals.insert(0, {"do": "wait", "seconds": min(300, float(f.get("seconds", 60)))})
                done.append(f"wait {f.get('seconds', 60)} s")
        except (BuildError, KeyError, ValueError, TypeError) as exc:
            done.append(f"{kind} failed: {str(exc)[:100]}")
    return done
