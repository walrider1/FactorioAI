"""Replay checker: re-run a logged run on a fresh game and compare every step.

Factorio (and the simulator) are deterministic, so the same seed + the same
actions must give the same per-action results and the same score. Use it to
audit results and settle disputes.
"""

from __future__ import annotations

import json
from pathlib import Path

from .backends.base import Backend
from .config import Settings, Task
from .controller import ArenaError, RunSession
from .models import ActRequest


def _make_backend(settings: Settings, kind: str) -> Backend:
    if kind == "sim":
        from .backends.sim import SimBackend
        return SimBackend()
    from .backends.factorio import FactorioBackend, SlotPool
    pool = SlotPool(1)
    return FactorioBackend(settings.competition.factorio, settings.root, pool, pool.acquire())


def replay_run(run_dir: Path, settings: Settings, backend: Backend | None = None) -> dict:
    run_dir = Path(run_dir)
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    original = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    task = Task.model_validate(meta["task_config"])
    backend = backend or _make_backend(settings, meta["backend"]["backend"])
    session = RunSession("replay-" + meta["run_id"], meta["team"], task, meta["mode"],
                         meta["seed"], meta["map"], run_dir=None)
    # realtime runs: the world stays paused and runs to each action's logged tick before it is applied,
    # which recreates the live game exactly (the factory and the walking character only move on ticks)
    session.replaying = session.realtime
    session.start(backend)
    if session.status != "active":
        return {"run_id": meta["run_id"], "match": False, "error": session.end_reason}
    mismatches: list[dict] = []
    lines = [json.loads(x) for x in (run_dir / "actions.jsonl").read_text(encoding="utf-8").splitlines() if x]
    for entry in lines:
        req = ActRequest(request_id=entry["request_id"], action=entry["action"])
        session.replay_tick = entry.get("tick_after")
        try:
            res = session.act(req)
        except ArenaError as exc:
            mismatches.append({"seq": entry["seq"], "problem": f"replay raised {exc.code}"})
            break
        checks = [("ok", res["ok"]), ("code", res["code"]), ("tick_after", res["tick"])]
        if not session.realtime:     # live realtime scores are read a few ticks after the action; skip those
            checks.append(("score", res["score_so_far"]))
        for key, got in checks:
            want = entry.get(key)
            if key == "tick_after" and want is None:
                continue
            if want != got:
                mismatches.append({"seq": entry["seq"], "field": key, "logged": want, "replayed": got})
        if session.status != "active":
            break
    if session.realtime and session.status == "active" and session.clock_tick is not None:
        used = (original.get("budgets_used") or {}).get("ticks")
        if used is not None:                 # the live run's build time ended at this tick
            backend.run_ticks(session.clock_tick + int(used) - backend.tick())
    session.end(original.get("end_reason") or "replay")
    replayed = session.result or {}
    score_match = replayed.get("score") == original.get("score")
    return {
        "run_id": meta["run_id"],
        "original_score": original.get("score"),
        "replayed_score": replayed.get("score"),
        "actions_replayed": len(lines),
        "match": score_match and not mismatches,
        "mismatches": mismatches[:50],
    }
