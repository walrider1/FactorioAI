"""Run controller and run manager.

RunSession enforces one run's rules: budgets, tick accounting, idempotent
requests, time limits, logging, and the hand-off to the scorer.
RunManager owns all sessions: quotas, seed choice, one active run per team,
game slots, and timeouts.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import __version__
from .backends.base import Backend, BackendError
from .config import Settings, Task
from .models import READ_ONLY_VERBS, ActRequest, Outcome
from .scorer import score_run

log = logging.getLogger("arena.controller")

QUEUED, STARTING, ACTIVE, SCORING, FINISHED, VOID = (
    "queued", "starting", "active", "scoring", "finished", "void")
OPEN_STATES = {QUEUED, STARTING, ACTIVE, SCORING}
# realtime: refusals that change nothing in the game and cost nothing (the call came too early)
FREE_CODES = {"too_fast", "busy", "time_up", "clock_not_started"}
TICKS_PER_SECOND = 60


class ArenaError(Exception):
    """An error the gateway turns into an HTTP error response."""

    def __init__(self, code: str, message: str, http_status: int):
        super().__init__(message)
        self.code, self.message, self.http_status = code, message, http_status


def _now() -> float:
    return time.time()


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


def _fingerprint(action) -> str:
    return hashlib.sha256(json.dumps(action.model_dump(), sort_keys=True).encode()).hexdigest()


def _file_hash(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for p in sorted(paths):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


class RunSession:
    def __init__(self, run_id: str, team: str, task: Task, mode: str, seed: int, map_label: str,
                 run_dir: Path | None, on_done: Callable[["RunSession"], None] | None = None,
                 scenario_hash: str | None = None):
        self.id, self.team, self.task, self.mode = run_id, team, task, mode
        self.seed, self.map_label = seed, map_label
        self.dir = run_dir
        self.on_done = on_done
        self.scenario_hash = scenario_hash
        self.status = QUEUED
        self.end_reason: str | None = None
        self.created_at = _now()
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.deadline: float | None = None
        self.last_activity = self.created_at
        self.actions_used = self.observes_used = self.ticks_used = 0
        self.seq = 0
        self.backend: Backend | None = None
        self.result: dict | None = None
        self.lock = threading.RLock()
        self._cache: OrderedDict[str, tuple[str, dict]] = OrderedDict()
        self._log_fh = None
        self.anomalies: list[str] = []
        # realtime clock
        self.realtime = task.clock == "realtime"
        self.clock_tick: int | None = None       # game tick the build time started at (first call)
        self.clock_wall: float | None = None
        self.replaying = False                   # replay: keep the world paused, run it to each logged tick
        self.replay_tick: int | None = None

    # ------------------------------------------------------------ lifecycle
    def start(self, backend: Backend) -> None:
        with self.lock:
            self.status = STARTING
            self.backend = backend
            try:
                setup = backend.setup(self.task, self.seed)
            except Exception as exc:  # noqa: BLE001 - anything here voids the run
                log.exception("run %s failed to start", self.id)
                self._void(f"backend_failed_to_start: {exc}")
                return
            self.started_at = self.last_activity = _now()
            self.deadline = self.started_at + self.task.budgets.wall_clock_seconds
            self.start_tick = backend.tick()
            if self.dir:
                self.dir.mkdir(parents=True, exist_ok=True)
                self._log_fh = open(self.dir / "actions.jsonl", "a", encoding="utf-8")
                self._write_json("meta.json", {
                    "run_id": self.id, "team": self.team, "task": self.task.id, "mode": self.mode,
                    "map": self.map_label, "seed": self.seed, "created_at": _iso(self.created_at),
                    "started_at": _iso(self.started_at), "arena_version": __version__,
                    "scenario_hash": self.scenario_hash, "task_config": self.task.model_dump(),
                    "backend": backend.info(), "setup": setup,
                })
            self.status = ACTIVE

    # ------------------------------------------------------------ save / resume (organizer only)
    def save(self, name: str) -> dict:
        """Save the game and this session's clock and counters into the run folder (saves/<name>.zip and
        saves/<name>.session.json), so a later process can carry the run on from here."""
        with self.lock:
            if self.status != ACTIVE or self.backend is None or self.dir is None:
                raise ArenaError("run_not_active", f"run is {self.status}", 409)
            folder = self.dir / "saves"
            path = self.backend.save(name, folder)
            tick = self.backend.tick()
            state = {"name": name, "file": path.name, "tick": tick, "saved_at": _iso(_now()),
                     "clock_tick": self.clock_tick, "start_tick": getattr(self, "start_tick", 0),
                     "actions_used": self.actions_used, "observes_used": self.observes_used,
                     "ticks_used": self.ticks_used, "seq": self.seq,
                     "wall_used": round(_now() - (self.started_at or _now()), 1) + getattr(self, "wall_before", 0.0),
                     "seed": self.seed, "map": self.map_label, "task": self.task.id}
            (folder / f"{name}.session.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
            return state

    def start_resumed(self, backend: Backend, save_file: Path, state: dict) -> None:
        """Like start(), from a save: the game comes back as it was, and the clock, budgets and counters
        carry on from the saved session (the score's time bonus counts game time from the first start)."""
        with self.lock:
            self.status = STARTING
            self.backend = backend
            try:
                setup = backend.resume(self.task, save_file)
            except Exception as exc:  # noqa: BLE001 - anything here voids the run
                log.exception("run %s failed to resume", self.id)
                self._void(f"backend_failed_to_resume: {exc}")
                return
            self.started_at = self.last_activity = _now()
            self.wall_before = float(state.get("wall_used") or 0.0)
            self.deadline = self.started_at + max(60.0, self.task.budgets.wall_clock_seconds - self.wall_before)
            self.start_tick = int(state.get("start_tick") or 0)
            self.actions_used = int(state.get("actions_used") or 0)
            self.observes_used = int(state.get("observes_used") or 0)
            self.ticks_used = int(state.get("ticks_used") or 0)
            self.seq = int(state.get("seq") or 0)
            if self.realtime and state.get("clock_tick") is not None:
                self.clock_tick = int(state["clock_tick"])
                now_tick = backend.tick()
                left = max(1, self.task.budgets.ticks - (now_tick - self.clock_tick))
                backend.start_clock(left, run=True)          # the world runs again from here
                self.clock_wall = _now() - (now_tick - self.clock_tick) / self.game_ticks_per_second
            if self.dir:
                self.dir.mkdir(parents=True, exist_ok=True)
                self._log_fh = open(self.dir / "actions.jsonl", "a", encoding="utf-8")
                meta_path = self.dir / "meta.json"
                meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
                meta.setdefault("resumes", []).append({"at": _iso(self.started_at), "save": state.get("name"),
                                                       "tick": setup.get("tick"), "backend": backend.info()})
                meta["backend"] = backend.info()
                self._write_json("meta.json", meta)
                (self.dir / "result.json").unlink(missing_ok=True)   # running again: the old end no longer holds
            self.status = ACTIVE

    @property
    def game_ticks_per_second(self) -> float:
        """Realtime clock: game ticks per real second (the task's clock_speed x 60)."""
        return TICKS_PER_SECOND * self.task.clock_speed

    def budgets(self) -> dict:
        b = self.task.budgets
        used = self.ticks_used
        if self.realtime and self.clock_wall is not None and self.status == ACTIVE and not self.replaying:
            used = min(b.ticks, max(used, int((_now() - self.clock_wall) * self.game_ticks_per_second)))
        out = {
            "actions_left": None if b.actions is None else b.actions - self.actions_used,
            "observes_left": b.observes - self.observes_used,
            "ticks_left": b.ticks - used,
            "actions_used": self.actions_used,
            "observes_used": self.observes_used,
            "ticks_used": self.ticks_used,
            "wall_seconds_left": max(0, int(self.deadline - _now())) if self.deadline else None,
        }
        if self.realtime:
            out["clock"] = "realtime" if self.clock_tick is not None else "realtime (starts on your first call)"
        return out

    def expired_reason(self, now: float | None = None) -> str | None:
        now = now or _now()
        if self.status != ACTIVE:
            return None
        if self.deadline and now > self.deadline:
            return "wall_clock_limit"
        if (self.realtime and self.clock_wall is not None and not self.replaying
                and now - self.clock_wall > self.task.budgets.ticks / self.game_ticks_per_second + 30):
            return "time_up"                    # the world paused itself; nobody asked since
        if now - self.last_activity > self.task.budgets.idle_timeout_seconds:
            return "idle_timeout"
        return None

    # --------------------------------------------------------------- acting
    def act(self, req: ActRequest) -> dict:
        with self.lock:
            reason = self.expired_reason()
            if reason:
                self.end(reason)
            if self.status != ACTIVE:
                raise ArenaError("run_not_active", f"run is {self.status}", 409)

            fp = _fingerprint(req.action)
            cached = self._cache.get(req.request_id)
            if cached:
                if cached[0] != fp:
                    raise ArenaError("request_id_reused",
                                     "this request_id was already used for a different action", 409)
                return {**cached[1], "replayed": True}

            a = req.action
            self._check_task_rules(a)
            kind = "observe" if a.type in READ_ONLY_VERBS else "advance" if a.type == "advance" else "action"
            cost = len(a.ops) if a.type == "batch" else 1      # a batch counts each op as an action
            b = self.budgets()
            assert self.backend is not None
            tick_before = tick_after = None
            charged = None
            if kind == "observe" and b["observes_left"] <= 0:
                outcome = Outcome.fail("budget_exhausted", "no observes left")
            elif kind == "action" and b["actions_left"] is not None and b["actions_left"] <= 0:
                outcome = Outcome.fail("budget_exhausted", "no actions left")
            elif kind == "action" and self.task.budgets.action_ticks and b["ticks_left"] <= 0:
                outcome = Outcome.fail("budget_exhausted", "no ticks left, and every action takes game time")
            elif kind == "advance" and b["ticks_left"] <= 0:
                outcome = Outcome.fail("budget_exhausted", "no ticks left")
            elif self.realtime:
                try:
                    if self.clock_tick is None:     # the build time starts with the team's first call
                        self.clock_tick = self.backend.start_clock(self.task.budgets.ticks, run=not self.replaying)
                        self.clock_wall = _now()
                    if self.replaying and self.replay_tick is not None:
                        self.backend.run_ticks(self.replay_tick - self.backend.tick())
                    outcome = self.backend.act(a, 0)
                except BackendError as exc:
                    log.error("run %s backend error: %s", self.id, exc)
                    self._void(f"backend_error: {exc}")
                    raise ArenaError("run_voided",
                                     "the game server failed; this run is void and does not count", 503)
                tick_after = outcome.tick
                if tick_after is not None:
                    self.ticks_used = min(self.task.budgets.ticks, max(self.ticks_used, tick_after - self.clock_tick))
                if outcome.code not in FREE_CODES:
                    charged = kind
                    if kind == "observe":
                        self.observes_used += 1
                    elif kind == "action":
                        self.actions_used += cost
            else:
                if kind == "advance":
                    a = a.model_copy(update={"ticks": min(a.ticks, b["ticks_left"])})
                try:
                    tick_before = self.backend.tick()
                    outcome = self.backend.act(a, b["ticks_left"])
                    tick_after = self.backend.tick()
                    min_ticks = self.task.budgets.action_ticks
                    if kind == "action" and min_ticks:
                        # every action takes at least min_ticks of game time; the factory keeps running
                        spent = tick_after - tick_before
                        extra = min(min_ticks - spent, b["ticks_left"] - spent)
                        if extra > 0:
                            self.backend.run_ticks(extra)
                            tick_after = self.backend.tick()
                except BackendError as exc:
                    log.error("run %s backend error: %s", self.id, exc)
                    self._void(f"backend_error: {exc}")
                    raise ArenaError("run_voided",
                                     "the game server failed; this run is void and does not count", 503)
                spent = tick_after - tick_before
                if spent > b["ticks_left"]:
                    self.anomalies.append(f"seq {self.seq + 1}: spent {spent} > budget {b['ticks_left']}")
                self.ticks_used += spent
                charged = kind
                if kind == "observe":
                    self.observes_used += 1
                elif kind == "action":
                    self.actions_used += cost

            complete = False
            try:
                live = self.backend.score_breakdown(self.task.score)
                score_now = int(live["score"])
                complete = bool(live.get("complete"))
            except BackendError:
                score_now = None
            self.seq += 1
            self.last_activity = _now()
            response = {
                "seq": self.seq,
                "ok": outcome.ok,
                "code": outcome.code,
                "message": outcome.message,
                "data": outcome.data,
                "tick": tick_after if tick_after is not None else self.backend.tick(),
                "ticks_spent": (tick_after - tick_before) if tick_before is not None and tick_after is not None else 0,
                "charged": charged,
                "budgets": self.budgets(),
                "score_so_far": score_now,
                "run_status": self.status,
            }
            self._cache[req.request_id] = (fp, response)
            while len(self._cache) > 5000:
                self._cache.popitem(last=False)
            self._log({
                "seq": self.seq, "t": round(self.last_activity, 3), "request_id": req.request_id,
                "action": req.action.model_dump(), "ok": outcome.ok, "code": outcome.code,
                "data": _trim_for_log(a.type, outcome.data), "tick_before": tick_before,
                "tick_after": tick_after, "charged": charged, "budgets": response["budgets"],
                "score": score_now,
            })
            nb = response["budgets"]
            no_actions = ((nb["actions_left"] is not None and nb["actions_left"] <= 0)
                          or (self.task.budgets.action_ticks > 0 and nb["ticks_left"] <= 0))
            if complete and self.task.score.end_when_complete:
                self.end("completed")                  # every milestone reached: nothing left to score
                response = {**response, "run_status": self.status}
            elif self.realtime:
                if outcome.code == "time_up" or self.ticks_used >= self.task.budgets.ticks:
                    self.end("time_up")
                    response = {**response, "run_status": self.status}
            elif no_actions and nb["ticks_left"] <= 0:
                self.end("budgets_exhausted")
                response = {**response, "run_status": self.status}
            return response

    def _check_task_rules(self, a, in_batch: bool = False) -> None:
        t = self.task
        if not in_batch and a.type not in t.verbs:
            raise ArenaError("verb_not_allowed", f"'{a.type}' is not allowed in {t.id}", 403)
        if a.type == "batch":
            if len(a.ops) > t.limits.batch_max_ops:
                raise ArenaError("batch_too_large", f"{t.id} allows at most {t.limits.batch_max_ops} ops a batch", 403)
            for op in a.ops:
                if op.type not in t.verbs:
                    raise ArenaError("verb_not_allowed", f"'{op.type}' is not allowed in {t.id}", 403)
                self._check_task_rules(op, in_batch=True)
            return
        if a.type == "observe":
            if a.radius > t.limits.observe_radius:
                raise ArenaError("radius_too_large", f"{t.id} allows observe radius up to {t.limits.observe_radius}", 403)
            if a.x is not None and not t.limits.observe_range:
                raise ArenaError("observe_centre_not_allowed", f"in {t.id} you observe around your character", 403)
        if a.type == "scan" and a.radius > t.limits.scan_radius:
            raise ArenaError("radius_too_large", f"{t.id} allows scan radius up to {t.limits.scan_radius}", 403)
        if a.type in ("place", "place_line") and not t.entity_allowed(a.entity):
            raise ArenaError("entity_not_allowed", f"'{a.entity}' is not allowed in {t.id}", 403)
        if a.type == "craft" and not t.recipe_allowed(a.recipe):
            raise ArenaError("recipe_not_allowed", f"'{a.recipe}' is not allowed in {t.id}", 403)
        if a.type == "set_recipe" and not t.machine_recipe_allowed(a.recipe):
            raise ArenaError("recipe_not_allowed", f"'{a.recipe}' is not allowed in {t.id}", 403)
        if a.type == "insert" and t.insert_items is not None and a.item not in t.insert_items:
            raise ArenaError("item_not_allowed", f"in {t.id} you may only insert {', '.join(t.insert_items)} "
                             "by hand; everything else must be moved by the factory", 403)

    # ---------------------------------------------------------------- ending
    def end(self, reason: str) -> None:
        with self.lock:
            if self.status != ACTIVE:
                return
            self.status = SCORING
            self.end_reason = reason
            assert self.backend is not None
            if self.realtime and self.clock_tick is not None:
                try:                                    # pause the world and see how much build time ran
                    t = self.backend.stop_clock()
                except BackendError as exc:
                    log.error("run %s failed to stop the clock: %s", self.id, exc)
                    self._void(f"scoring_failed: {exc}")
                    return
                self.ticks_used = min(self.task.budgets.ticks, max(0, t - self.clock_tick))
            fill = 0
            if self.task.score.type == "automation" and self.task.score.window_at_build_end:
                fill = max(0, self.task.budgets.ticks - self.ticks_used)
            try:
                start = self.clock_tick if self.clock_tick is not None else getattr(self, "start_tick", 0)
                scored = score_run(self.backend, self.task, fill_ticks=fill, start_tick=start or 0)
            except BackendError as exc:
                log.error("run %s failed while scoring: %s", self.id, exc)
                self._void(f"scoring_failed: {exc}")
                return
            self.finished_at = _now()
            self.result = {
                "run_id": self.id, "team": self.team, "task": self.task.id, "mode": self.mode,
                "map": self.map_label, "status": FINISHED, "end_reason": reason,
                "score": scored["score"], "metric": scored["metric"],
                "breakdown": scored["breakdown"], "idle_tail": scored["idle_tail"],
                "budgets_used": {"actions": self.actions_used, "observes": self.observes_used,
                                 "ticks": self.ticks_used},
                "limits": self.task.budgets.model_dump(),
                "wall_seconds": round(self.finished_at - (self.started_at or self.finished_at), 1),
                "anomalies": self.anomalies,
                "finished_at": _iso(self.finished_at),
            }
            self.status = FINISHED
            self._finish()

    def _void(self, reason: str) -> None:
        self.status = VOID
        self.end_reason = reason
        self.finished_at = _now()
        self.result = {
            "run_id": self.id, "team": self.team, "task": self.task.id, "mode": self.mode,
            "map": self.map_label, "status": VOID, "end_reason": reason, "score": None,
            "budgets_used": {"actions": self.actions_used, "observes": self.observes_used,
                             "ticks": self.ticks_used},
            "finished_at": _iso(self.finished_at),
        }
        self._finish()

    def void(self, reason: str) -> None:
        with self.lock:
            if self.status in (FINISHED, VOID):
                return
            self._void(reason)

    def _finish(self) -> None:
        if self.dir and self.result:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._write_json("result.json", self.result)
        if self._log_fh:
            self._log_fh.close()
            self._log_fh = None
        if self.backend:
            try:
                self.backend.close()
            except Exception:  # noqa: BLE001
                log.exception("run %s: closing backend failed", self.id)
        if self.on_done:
            self.on_done(self)

    # --------------------------------------------------------------- output
    def public_state(self, reveal_seed: bool) -> dict:
        state = {
            "run_id": self.id, "task": self.task.id, "mode": self.mode, "map": self.map_label,
            "status": self.status, "end_reason": self.end_reason,
            "created_at": _iso(self.created_at), "started_at": _iso(self.started_at),
            "budgets": self.budgets(),
        }
        if reveal_seed:
            state["seed"] = self.seed
        return state

    def _log(self, entry: dict) -> None:
        if self._log_fh:
            self._log_fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
            self._log_fh.flush()

    def _write_json(self, name: str, data: dict) -> None:
        assert self.dir is not None
        (self.dir / name).write_text(json.dumps(data, indent=2), encoding="utf-8")


def _trim_for_log(action_type: str, data: dict) -> dict:
    if action_type != "observe":
        return data
    d = dict(data)
    res = d.pop("resources", {}) or {}
    d["resource_tiles"] = {k: len(v) for k, v in res.items()}
    # tile positions only (no amounts), so a replay page can draw the ore patches
    d["resource_xy"] = {k: [[int(t[0] // 1), int(t[1] // 1)] for t in v] for k, v in res.items()}
    return d


# ======================================================================
class RunManager:
    def __init__(self, settings: Settings, backend_factory: Callable[[Task], Backend] | None = None):
        self.settings = settings
        self.sessions: dict[str, RunSession] = {}
        self.lock = threading.RLock()
        self.index_file = settings.runs_dir / "index.jsonl"
        self.history: list[dict] = self._load_history()
        self.pool = None
        if backend_factory is None:
            backend_factory = self._default_factory()
        self.backend_factory = backend_factory
        scenario_dir = settings.root / settings.competition.factorio.scenario_dir
        files = list(scenario_dir.glob("*")) if scenario_dir.exists() else []
        self.scenario_hash = _file_hash(files) if files else None
        self._stop = threading.Event()
        self._reaper: threading.Thread | None = None

    def _default_factory(self) -> Callable[[Task], Backend]:
        if self.settings.competition.backend == "sim":
            from .backends.sim import SimBackend
            return lambda task: SimBackend()
        from .backends.factorio import FactorioBackend, SlotPool
        fcfg = self.settings.competition.factorio
        self.pool = SlotPool(fcfg.slots)

        def make(task: Task) -> Backend:
            slot = self.pool.acquire()
            return FactorioBackend(fcfg, self.settings.root, self.pool, slot)
        return make

    def _load_history(self) -> list[dict]:
        if not self.index_file.exists():
            return []
        out = []
        for line in self.index_file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out

    # ---------------------------------------------------------- reaper
    def start_reaper(self, interval: float = 5.0) -> None:
        def loop():
            while not self._stop.wait(interval):
                self.reap()
        self._reaper = threading.Thread(target=loop, name="arena-reaper", daemon=True)
        self._reaper.start()

    def stop(self) -> None:
        self._stop.set()
        for s in list(self.sessions.values()):
            if s.status == ACTIVE:
                s.end("server_shutdown")

    def reap(self) -> None:
        for s in list(self.sessions.values()):
            reason = s.expired_reason()
            if reason:
                try:
                    s.end(reason)
                except Exception:  # noqa: BLE001
                    log.exception("reaper failed to end run %s", s.id)

    # ---------------------------------------------------------- runs
    def active_run(self, team: str) -> RunSession | None:
        for s in self.sessions.values():
            if s.team == team and s.status in OPEN_STATES:
                return s
        return None

    def create_run(self, team: str, task_id: str, mode: str, wait: bool = False,
                   seed_override: int | None = None) -> RunSession:
        """seed_override is organizer-only (local testing, seed vetting); the API never sets it."""
        cfg = self.settings.competition
        with self.lock:
            task = self.settings.tasks.get(task_id)
            if task is None:
                raise ArenaError("unknown_task", f"no task named {task_id}", 404)
            if cfg.backend not in task.backends:
                raise ArenaError("task_unavailable", f"{task_id} is not available on this server", 409)
            if self.active_run(team):
                raise ArenaError("run_already_active", "finish or end your current run first", 409)
            if mode == "practice":
                day_ago = _now() - 86_400
                n = sum(1 for h in self._all_runs() if h["team"] == team and h["mode"] == "practice"
                        and h["created_ts"] > day_ago)
                if n >= cfg.practice.runs_per_day:
                    raise ArenaError("practice_quota", "practice run limit reached for today", 429)
                total = sum(1 for h in self._all_runs() if h["team"] == team and h["task"] == task_id
                            and h["mode"] == "practice")
                seed = task.practice_seeds[total % len(task.practice_seeds)]
                if seed_override is not None:
                    seed = seed_override
                label = f"practice-{seed}"
            else:
                if not cfg.eval.open:
                    raise ArenaError("eval_closed", "evaluation is not open yet", 403)
                seeds = self.settings.load_eval_seeds().get(task_id, [])
                done = {h["map"] for h in self._all_runs() if h["team"] == team and h["task"] == task_id
                        and h["mode"] == "eval" and h["status"] in (FINISHED, *OPEN_STATES)}
                choice = next(((i, s) for i, s in enumerate(seeds) if f"eval-{i + 1}" not in done), None)
                if choice is None:
                    raise ArenaError("no_eval_maps_left", "you have played every evaluation map", 409)
                seed, label = choice[1], f"eval-{choice[0] + 1}"
            run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
            session = RunSession(run_id, team, task, mode, seed, label,
                                 self.settings.runs_dir / run_id, on_done=self._on_done,
                                 scenario_hash=self.scenario_hash)
            self.sessions[run_id] = session
        if wait:
            self._launch(session)
        else:
            threading.Thread(target=self._launch, args=(session,), name=f"launch-{run_id}",
                             daemon=True).start()
        return session

    def resume_run(self, team: str, run_id: str, save: str | None = None) -> RunSession:
        """Organizer only (local-run --resume): carry a saved run on in a new game, under the same run id and
        folder. save: a save name in runs/<id>/saves (default: the newest)."""
        folder = self.settings.runs_dir / run_id / "saves"
        states = sorted(folder.glob("*.session.json"), key=lambda p: p.stat().st_mtime) if folder.exists() else []
        if save:
            states = [p for p in states if p.name == f"{save}.session.json"]
        if not states:
            raise ArenaError("no_save", f"no save {'named ' + save + ' ' if save else ''}in {folder}", 404)
        state = json.loads(states[-1].read_text(encoding="utf-8"))
        task = self.settings.tasks.get(state["task"])
        if task is None:
            raise ArenaError("unknown_task", f"no task named {state['task']}", 404)
        with self.lock:
            if self.active_run(team):
                raise ArenaError("run_already_active", "finish or end your current run first", 409)
            session = RunSession(run_id, team, task, "practice", int(state["seed"]), state.get("map") or "resumed",
                                 self.settings.runs_dir / run_id, on_done=self._on_done,
                                 scenario_hash=self.scenario_hash)
            self.sessions[run_id] = session
        try:
            backend = self.backend_factory(task)
        except Exception as exc:  # noqa: BLE001
            session.void(f"no_backend: {exc}")
            return session
        session.start_resumed(backend, folder / state["file"], state)
        return session

    def _launch(self, session: RunSession) -> None:
        try:
            backend = self.backend_factory(session.task)
        except Exception as exc:  # noqa: BLE001
            log.exception("could not create backend for %s", session.id)
            session.void(f"no_backend: {exc}")
            return
        session.start(backend)

    def _all_runs(self) -> list[dict]:
        live_ids = set(self.sessions)
        rows = [h for h in self.history if h["run_id"] not in live_ids]
        for s in self.sessions.values():
            rows.append({"run_id": s.id, "team": s.team, "task": s.task.id, "mode": s.mode,
                         "map": s.map_label, "status": s.status, "created_ts": s.created_at})
        return rows

    def _on_done(self, s: RunSession) -> None:
        row = {"run_id": s.id, "team": s.team, "task": s.task.id, "mode": s.mode, "map": s.map_label,
               "status": s.status, "created_ts": s.created_at,
               "score": s.result.get("score") if s.result else None}
        with self.lock:
            self.history.append(row)
            self.index_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.index_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")

    def get(self, run_id: str, team: str) -> RunSession:
        s = self.sessions.get(run_id)
        if s is None or s.team != team:
            raise ArenaError("not_found", "no such run", 404)
        return s

    def team_summary(self, team: str) -> dict:
        rows = [h for h in self._all_runs() if h["team"] == team]
        day_ago = _now() - 86_400
        active = self.active_run(team)
        return {
            "team": team,
            "active_run": active.id if active else None,
            "practice_runs_today": sum(1 for h in rows if h["mode"] == "practice" and h["created_ts"] > day_ago),
            "practice_runs_per_day": self.settings.competition.practice.runs_per_day,
            "eval_open": self.settings.competition.eval.open,
            "eval_maps_played": sorted({f'{h["task"]}:{h["map"]}' for h in rows
                                        if h["mode"] == "eval" and h["status"] == FINISHED}),
        }
