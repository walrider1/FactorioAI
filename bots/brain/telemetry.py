"""Live telemetry for the monitor page: what the bot is doing, written next to the run's logs.

    bot_live.json      the current state, rewritten every second by a background thread
    bot_events.jsonl   one line per event: strategist asked / answered, goal started / finished, milestone

The monitor (`arena monitor`, or `arena local-run --monitor`) reads these files; the bot never talks to it.
Writing is best-effort: a failed write never disturbs the game.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path


def stamp_stages(stages: list[dict], seen: dict, minute: float | None, primed: bool) -> None:
    """Give each cleared roadmap stage `done_minute`: the game minute it was first seen done (the overlay's split
    times). A stage already done the first time the roadmap is read (a resume without the old file) gets None."""
    for s in stages:
        if s.get("milestone") and not s.get("done"):
            seen.pop(s.get("name"), None)            # stamped before the game's milestone: not cleared after all
            continue
        if s.get("done"):
            if s.get("name") not in seen:
                seen[s.get("name")] = minute if primed else None
            s["done_minute"] = seen[s.get("name")]


class Telemetry:
    def __init__(self, folder: str | Path | None):
        self.dir = Path(folder) if folder else None
        self.state: dict = {"status": "starting", "started": time.time(), "current_goal": None, "queue": [],
                            "strategist": {"thinking_since": None, "last": None, "calls": 0, "cost_usd": 0.0},
                            "results": [], "events": []}
        self._lock = threading.Lock()
        self._world = self._crew = self._strategist = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if self.dir:
            self.dir.mkdir(parents=True, exist_ok=True)
            try:                                     # a resumed run keeps the game minute each stage was cleared
                old = json.loads((self.dir / "bot_live.json").read_text(encoding="utf-8"))
                if isinstance(old.get("stage_minutes"), dict):
                    self.state["stage_minutes"] = dict(old["stage_minutes"])
            except (OSError, ValueError):
                pass
            self._thread = threading.Thread(target=self._loop, name="telemetry", daemon=True)
            self._thread.start()

    def bind(self, world, crew, strategist, fixer=None, assessor=None, overseer=None) -> None:
        self._world, self._crew, self._strategist = world, crew, strategist
        self._fixer, self._assessor, self._overseer = fixer, assessor, overseer

    @staticmethod
    def _overseer_clock(crew) -> dict:
        """Is the overseer thinking right now, and when does it look next (game minute)?"""
        sup = getattr(crew, "supervisor", None)
        ov = getattr(sup, "overseer", None)
        if ov is None:
            return {}
        fut = getattr(sup, "overseer_pending", None)
        nxt = getattr(sup, "overseer_next_tick", None)
        return {"thinking": fut is not None and not fut.done(),
                "next_minute": round(nxt / 3600, 1) if isinstance(nxt, (int, float)) and nxt != float("inf") else None,
                "period_min": getattr(ov, "period_min", None), "max_cost": getattr(ov, "max_cost", None),
                "model": getattr(ov, "model", None)}

    @staticmethod
    def _outlook(crew) -> dict:
        try:
            from .outposts import outlook
            return outlook(crew)
        except Exception as exc:  # noqa: BLE001 - the monitor never breaks the bot
            return {"error": str(exc)[:200]}

    @staticmethod
    def _upkeep(crew) -> dict:
        try:
            from .upkeep import status
            return status(crew)
        except Exception as exc:  # noqa: BLE001 - the monitor never breaks the bot
            return {"error": str(exc)[:200]}

    def _roadmap(self, crew) -> dict | None:
        """The road to the rocket, recomputed every few seconds (it reads only what the crew knows: no game calls)."""
        now = time.time()
        if now - getattr(self, "_rm_t", 0) > 4:
            try:
                from .roadmap import roadmap
                self._rm, self._rm_t = roadmap(crew), now
                minute = round(self._world.now_tick() / 3600, 1) if self._world is not None else None
                with self._lock:
                    seen = self.state.setdefault("stage_minutes", {})
                    stamp_stages(self._rm.get("stages") or [], seen, minute, getattr(self, "_rm_primed", False))
                self._rm_primed = True
            except Exception as exc:  # noqa: BLE001
                self._rm, self._rm_t = {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}, now
        return getattr(self, "_rm", None)

    def set(self, **kw) -> None:
        with self._lock:
            self.state.update(kw)

    def event(self, kind: str, **data) -> None:
        row = {"t": round(time.time(), 2), "kind": kind, **data}
        if self._world is not None:
            row["minute"] = round(self._world.now_tick() / 3600, 2)
        with self._lock:
            self.state["events"] = (self.state["events"] + [row])[-60:]
        if self.dir:
            try:
                with open(self.dir / "bot_events.jsonl", "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, default=str) + "\n")
            except OSError:
                pass

    def close(self, **kw) -> None:
        self.set(**kw)
        self._stop.set()
        self.flush()

    # ---------------------------------------------------------------- internals
    def _live(self) -> dict:
        w, crew, st = self._world, self._crew, self._strategist
        out = {}
        if w is not None:
            out.update({
                "game_minute": round(w.now_tick() / 3600, 2), "minutes_left": round(w.minutes_left, 2),
                "activity": getattr(w, "activity", ""), "position": [round(w.pos[0], 1), round(w.pos[1], 1)],
                "inventory": dict(sorted(w.inventory.items(), key=lambda kv: -kv[1])[:24]),
                "research": w.research, "milestones": w.milestones,
                "rates": {k: v.get("made_per_min") for k, v in ((w.stats or {}).get("items") or {}).items()
                          if v.get("made_per_min")},
                "patches": [{"name": p["name"], "box": p["box"], "tiles": p["tiles"]} for p in w.patches[:40]],
                # the stream's world map: crude-oil wells it knows (single tiles, not patches)
                "oil_wells": [list(t) for t in sorted((getattr(w, "resources", None) or {}).get("crude-oil", ()))][:60],
                "stage": {"main": w.stage, "steps": list(w.steps), "foreman": w.foreman_step,
                          "thinking": bool(self.state["strategist"].get("thinking_since"))},
                "trace": list(w.trace),
            })
        if crew is not None:
            sealed = getattr(crew, "sealed", {})
            out.update({"base": [{**s.brief(), **({"sealed": True} if s.id in sealed else {})} for s in crew.base.sites],
                        "upkeep": self._upkeep(crew),
                        "ore_outlook": self._outlook(crew),
                        "hints": crew.hints(),
                        "foreman": {"problems": crew.foreman.problems[-8:], "fuel_fixes": crew.foreman.fuel_fixes,
                                    "hand_packs": crew.foreman.hand_packs, "plates_collected": crew.foreman.collected,
                                    "courier": crew.foreman.last_courier,
                                    "fed_feeders": getattr(crew.foreman, "fed_feeders", 0)},
                        "roadmap": self._roadmap(crew),
                        "fixer": {"notes": getattr(crew, "fix_notes", [])[-8:],
                                  "dig_outs": list(getattr(crew.w, "trap_notes", []))[-8:]},
                        "designs": getattr(crew, "designs", [])[-12:],
                        "design": self._design(crew),
                        "zones": self._zones(crew),
                        "supervisor": {"notes": (crew.supervisor.notes[-8:] if getattr(crew, "supervisor", None)
                                                 else []),
                                       "alerts": [{k: a.get(k) for k in ("level", "what")}
                                                  for a in getattr(crew.supervisor, "alerts", [])[:8]]
                                       if getattr(crew, "supervisor", None) else []},
                        "cells": [{"machine": c["machine"], "recipe": c.get("recipe"), "x": c["x"], "y": c["y"]}
                                  for _, c in crew.cells()][:400],
                        "overseer": {"notes": list(getattr(getattr(crew, "supervisor", None), "overseer_notes", []))[-6:],
                                     **self._overseer_clock(crew),
                                     "guidance": getattr(crew, "overseer_guidance", None),
                                     "forbidden": [{"match": f["match"], "why": f.get("why", "")}
                                                   for f in getattr(crew, "overseer_forbid", [])]}})
        for key, obj in (("fixer", getattr(self, "_fixer", None)), ("assessor", getattr(self, "_assessor", None)),
                         ("overseer", getattr(self, "_overseer", None))):
            if obj is not None and key in out:
                out[key].update({"name": getattr(obj, "name", ""), "calls": getattr(obj, "calls", 0),
                                 "cost_usd": round(getattr(obj, "cost_usd", 0.0), 4)})
            elif obj is not None:
                out[key] = {"name": getattr(obj, "name", ""), "calls": getattr(obj, "calls", 0),
                            "cost_usd": round(getattr(obj, "cost_usd", 0.0), 4)}
        if st is not None:
            out["strategist_name"] = st.name
            out["doctrine"] = getattr(st, "doctrine", "")
        return out

    def _design(self, crew):
        try:
            from .viz import design_feed
            return design_feed(crew)
        except Exception:  # noqa: BLE001 - the overlay's feed never disturbs the game
            return None

    ZONES_PLAN_EVERY = 3.0         # seconds between rewrites of zones_plan.json while it changes

    def _zones(self, crew):
        """The floorplan's live part for bot_live.json; the picture itself goes to zones_plan.json on a change."""
        try:
            from .viz import zones_key, zones_plan
            key = zones_key(crew)
            if key is None:
                return None
            now = time.time()
            if self.dir and (key != getattr(self, "_zkey", None) or now - getattr(self, "_zat", 0) > 60) \
                    and now - getattr(self, "_zat", 0) >= self.ZONES_PLAN_EVERY:
                plan = zones_plan(crew)
                if plan is not None:
                    tmp = self.dir / "zones_plan.json.tmp"
                    tmp.write_text(json.dumps(plan, default=str), encoding="utf-8")
                    os.replace(tmp, self.dir / "zones_plan.json")
                    self._zkey, self._zat = key, now
                    self._zrev = int(now)
            return {"plan_rev": getattr(self, "_zrev", None)}
        except Exception:  # noqa: BLE001
            return None

    def flush(self) -> None:
        if not self.dir:
            return
        try:
            live = self._live()
            with self._lock:
                data = {**self.state, **live, "updated": time.time()}
                data["strategist"] = {**self.state["strategist"]}
                if self._strategist is not None:
                    st = self._strategist
                    data["strategist"]["calls"] = st.calls
                    data["strategist"]["cost_usd"] = round(st.cost_usd, 4)
                    data["strategist"]["max_cost"] = getattr(st, "max_cost", 0.0)
                    data["strategist"]["model"] = getattr(st, "model", "")
                    data["strategist"]["tokens"] = dict(getattr(st, "tokens", {}) or {})
            tmp = self.dir / "bot_live.json.tmp"
            tmp.write_text(json.dumps(data, default=str), encoding="utf-8")
            os.replace(tmp, self.dir / "bot_live.json")
        except Exception:  # noqa: BLE001 - telemetry must never disturb the game
            pass

    def _loop(self) -> None:
        while not self._stop.wait(0.5):              # twice a second: the live graph should feel live
            self.flush()
