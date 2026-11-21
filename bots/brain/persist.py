"""Stop a run and carry it on later: the bot's memory next to the game's save.

A save is two files in the run's saves/ folder, written at a safe point between goals:

    <name>.zip            the game (the arena session saves it; the scenario script and its storage ride along)
    <name>.session.json   the arena session's clock and counters
    <name>.bot.pkl        this: what the bot knows that the game does not tell it again (sites and what they
                          are for, belt links and their ports, the foreman's book, what the planners decided,
                          the goal queue and results, the map knowledge it gathered)

Everything is pickled in ONE call, so objects shared between parts stay shared after loading (a link's port is
the same dict as the one in its site's params; a tap link points at the link it taps). Attributes that cannot
be pickled (the game connection, threads, callbacks, LLM clients) are left out and come from the new process,
as are clocks of the old process (time.monotonic() values mean nothing in another one).
"""

from __future__ import annotations

import json
import pickle
import time
from pathlib import Path

VERSION = 1

# never carried over: the new process's own connection, callbacks, threads, clocks and per-process budgets
SKIP = {
    "crew": {"w", "kn", "log", "supervisor", "watchdog", "foreman", "designs", "current", "preempt", "kit_hold",
             "_claim", "_leaving", "fix_notes", "_finishing", "drop_current", "strategist", "preempt_force", "reserved", "preempt_for"},
    "foreman": {"crew", "last", "busy"},
    "world": {"run", "kn", "log", "speed", "_tick_wall", "recorder", "blocking", "checkpoint", "tend", "make_way",
              "on_dug", "on_trapped", "_escalating", "_unsticking", "stage", "steps", "foreman_step", "trace",
              "activity"},
    "strategist": {"client", "_prices", "log", "task", "max_cost", "cost_usd", "calls", "tokens", "timeout_s",
                   "last_latency", "name", "model"},
    "supervisor": {"rules", "assessor", "pool", "pending", "log", "event", "doctrine", "last_ask_wall",
                   "last_detect", "period", "min_gap", "overseer", "report_fn", "overseer_pool", "overseer_pending",
                   "overseer_next_tick"},
    "watchdog": set(),
}


def _picklable(value) -> bool:
    try:
        pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
        return True
    except Exception:  # noqa: BLE001 - anything that will not pickle stays behind
        return False


def _state(obj, part: str, dropped: list[str]) -> dict:
    if obj is None:
        return {}
    out = {}
    for k, v in vars(obj).items():
        if k in SKIP.get(part, set()) or callable(v) and not isinstance(v, (dict, list, set, tuple)):
            continue
        if _picklable(v):
            out[k] = v
        else:
            dropped.append(f"{part}.{k}")
    return out


def capture(crew, world, strategist=None, supervisor=None, watchdog=None, loop: dict | None = None) -> tuple[bytes, list[str]]:
    """The bot's state as bytes, and the attributes left out (for the log)."""
    dropped: list[str] = []
    snap = {
        "version": VERSION, "saved_at": time.time(), "tick": getattr(world, "tick", 0),
        "crew": _state(crew, "crew", dropped),
        "foreman": _state(getattr(crew, "foreman", None), "foreman", dropped),
        "world": _state(world, "world", dropped),
        "strategist": _state(strategist, "strategist", dropped),
        "supervisor": _state(supervisor, "supervisor", dropped),
        "watchdog": _state(watchdog, "watchdog", dropped),
        "researched": sorted(getattr(getattr(world, "kn", None), "researched", []) or []),
        "loop": {k: v for k, v in (loop or {}).items() if _picklable(v)},
        "modules": _modules_state(dropped),
    }
    return pickle.dumps(snap, protocol=pickle.HIGHEST_PROTOCOL), dropped


def _modules_state(dropped: list[str]) -> dict:
    """Module state the game does not tell again: the workshop floor (its slots, fluid nets, joined pumpjacks).
    Without it every resume started a new floor, which needed its own water pump and crude line."""
    try:
        from .modules import MODULES
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    ws = MODULES.get("workshop")
    if ws is not None and (getattr(ws, "district", None) is not None or getattr(ws, "floors", None)):
        state = {"district": ws.district, "floors": list(getattr(ws, "floors", []) or [])}   # one pickle: the
        if _picklable(state):                                                            # current floor stays
            out["workshop"] = state                                                      # one of the floors
        else:
            dropped.append("modules.workshop")
    return out


def restore(data: bytes, crew, world, strategist=None, supervisor=None, watchdog=None) -> dict:
    """Put a captured state back onto freshly built objects. Returns the main loop's part."""
    snap = pickle.loads(data)
    if snap.get("version") != VERSION:
        raise ValueError(f"bot save version {snap.get('version')} (this bot reads {VERSION})")
    for obj, part in ((crew, "crew"), (getattr(crew, "foreman", None), "foreman"), (world, "world"),
                      (strategist, "strategist"), (supervisor, "supervisor"), (watchdog, "watchdog")):
        if obj is None:
            continue
        for k, v in (snap.get(part) or {}).items():
            setattr(obj, k, v)
    kn = getattr(world, "kn", None)
    if kn is not None and snap.get("researched") and hasattr(kn, "learn_research"):
        kn.learn_research(snap["researched"])
    ws_state = (snap.get("modules") or {}).get("workshop")
    if ws_state:
        from .modules import MODULES
        if MODULES.get("workshop") is not None:
            MODULES["workshop"].district = ws_state.get("district")
            MODULES["workshop"].floors = list(ws_state.get("floors") or [])
            MODULES["workshop"].designs.clear()
    return snap.get("loop") or {}


def save_point(saver, saves_dir: Path, name: str, state: dict, summary: dict, tel=None, log=print) -> bool:
    """Everything a save does, and none of it may end the run (run 20261119-200940 saved fine, then died on
    the line that logged it): capture the bot, save the game, write the bot's files, tell the monitor. Any
    failure is logged and the game plays on. Returns whether the save is complete."""
    try:
        data, dropped = capture(**state)
        saver(name)
        write(saves_dir, name, data, {**summary, "left_out": dropped})
    except Exception as exc:  # noqa: BLE001 - a failed save never ends the run
        _quietly(log, f"[bot] save failed: {type(exc).__name__}: {str(exc)[:200]}")
        if tel is not None:
            _quietly(tel.event, "save", name=name, ok=False, error=str(exc)[:200], minute=summary.get("minute"))
        return False
    _quietly(log, f"[bot] saved {name}" + (f" (left out: {', '.join(dropped)})" if dropped else ""))
    if tel is not None:
        _quietly(tel.event, "save", name=name, ok=True, save_kind=summary.get("kind"), minute=summary.get("minute"))
        _quietly(lambda: tel.set(saves=index(saves_dir)[-12:]))
    return True


def _quietly(fn, *a, **kw) -> None:
    """Bookkeeping after a save: a bug here is logged nowhere, rather than ending the run."""
    try:
        fn(*a, **kw)
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ the saves folder
def index(saves_dir: Path) -> list[dict]:
    """Every complete save in a run's saves/ folder (game, session and bot files), newest last."""
    saves_dir = Path(saves_dir)
    out = []
    for st in sorted(saves_dir.glob("*.session.json"), key=lambda p: p.stat().st_mtime) if saves_dir.exists() else []:
        name = st.name[:-len(".session.json")]
        try:
            info = json.loads(st.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        meta = saves_dir / f"{name}.bot.json"
        extra = {}
        if meta.exists():
            try:
                extra = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        out.append({"name": name, "minute": round((info.get("tick") or 0) / 3600, 1),
                    "saved_at": info.get("saved_at"), "game": (saves_dir / f"{name}.zip").exists(),
                    "bot": (saves_dir / f"{name}.bot.pkl").exists(), **extra})
    return out


def write(saves_dir: Path, name: str, data: bytes, summary: dict) -> None:
    saves_dir = Path(saves_dir)
    saves_dir.mkdir(parents=True, exist_ok=True)
    tmp = saves_dir / f"{name}.bot.pkl.tmp"
    tmp.write_bytes(data)
    tmp.replace(saves_dir / f"{name}.bot.pkl")
    (saves_dir / f"{name}.bot.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


def read(saves_dir: Path, name: str | None = None) -> tuple[str, bytes] | None:
    """A save's bot state (the newest complete one when no name is given)."""
    rows = [r for r in index(saves_dir) if r["bot"] and r["game"] and (name is None or r["name"] == name)]
    if not rows:
        return None
    name = rows[-1]["name"]
    return name, (Path(saves_dir) / f"{name}.bot.pkl").read_bytes()


# ------------------------------------------------------------------ requests from the monitor page
def take_request(run_dir: Path) -> str | None:
    """A save or stop the monitor page asked for (control.json), taken once."""
    p = Path(run_dir) / "control.json"
    if not p.exists():
        return None
    try:
        cmd = json.loads(p.read_text(encoding="utf-8")).get("cmd")
    except (OSError, ValueError):
        cmd = None
    try:
        p.unlink()
    except OSError:
        pass
    return cmd if cmd in ("save", "stop") else None
