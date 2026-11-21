"""Stream watchdog: every real hour Claude reviews the live run, and only bothers you when the bot is stuck for good.

    uv run python stream/watchdog.py              # keep running next to the stream
    uv run python stream/watchdog.py --token      # print the phone app's access token (made on first run)
    uv run python stream/watchdog.py --packet     # print what the next review would see, no Claude, no alert
    uv run python stream/watchdog.py --check-now  # one review right now, print the verdict, no alert

Loop
  1. Every --interval minutes (60) it builds a review packet from the playing run (runs/watch/reviews/).
  2. `claude -p` reads it (read-only: it may grep the logs and the code) and answers ok / unsure / stuck.
     ok: nothing happens. unsure: it looks again after --unsure-interval minutes (20). stuck and certain: an alert
     with a problem analysis goes to the phone app.
  3. You answer in the app. "Let it run": the alert closes. "Fix it" (+ your text): the stream switches to the
     Self-Upgrading screen, the run is saved and stopped, and `claude -p` fixes the code (short tests only). The
     fix report comes back as a new alert: "Let it run" resumes from the save and returns the stream to Live,
     "Fix it" starts another round with your new text.

Phone API (for the Android app; bearer token from secrets/phone_token), on 0.0.0.0:--port (8740):
  GET /api/status · GET /api/alerts?status=open|all · GET /api/alerts/<id> · POST /api/alerts/<id>/reply
  {"decision": "fix"|"run", "text": "..."}
  GET /api/stream · POST /api/stream/start {"countdown_min": 2} · POST /api/stream/stop ·
  POST /api/stream/schedule {"enabled": bool, "start": "HH:MM", "end": "HH:MM"} (daily, local time; may cross midnight)
  POST /api/stream/start {"reset": true} starts over (run #1, 0 upgrades, new game); POST /api/stream/reset
  {"armed": true|false} makes the next start do that (GET /api/stream shows "reset_next")

Stream: start = Starting Soon scene, relay up, OBS streaming, Live after the countdown. Stop = Ending scene, then OBS
stops and the relay closes. With the schedule on, that happens when the daily window opens and closes. Reviews only
run while OBS is streaming (--always overrides). Counters for the overlay: runs/watch/stats.json (--reset-counters).
Game: the bot plays only while the stream is live. Starting the stream resumes the run from its newest save (or starts
a fresh run if the last one really ended); ending it makes the bot save and stop (--no-game turns this off).
Game client: while live, the Factorio client on this PC is (re)started with --mp-connect whenever the run's game server
restarts and nobody is connected (--keep-client: also off-stream; --no-client: never).
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import random
import re
import secrets as pysecrets
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "stream"))
from arena.monitor import Runs, _json, _running, _tail_lines, request_control, request_resume, saves  # noqa: E402

RUNS = ROOT / "runs"
WATCH = RUNS / "watch"                      # runs/ is git-ignored
ALERTS = WATCH / "alerts"
REVIEWS = WATCH / "reviews"
STATUS = WATCH / "status.json"
TOKEN_FILE = ROOT / "secrets" / "phone_token"
LOCK = threading.Lock()

SCENE_LIVE, SCENE_UPGRADE, SCENE_BETWEEN = "Live", "Self-Upgrading", "Between Runs"
OVERLAY_SOURCE = "Factorio Overlay"   # the browser source with /overlay in the Live scene

REVIEW_TOOLS = ["Read", "Grep", "Glob", "Bash(tail:*)", "Bash(wc:*)", "Bash(head:*)"]
# the fix session changes the bot (and stream tooling), never the game's rules, and only what was approved: the
# prompt makes it stop and ask (a new alert) for anything beyond that. Never a real game run.
FIX_TOOLS = ["Read", "Edit", "Write", "Grep", "Glob", "Bash(tail:*)", "Bash(wc:*)",
             "Bash(uv run python -m pytest:*)", "Bash(uv run --no-sync python -m pytest:*)", "Bash(uv run pytest:*)",
             "Bash(uv run --no-sync --with obsws-python python stream/obs_setup.py:*)",
             "Bash(git diff:*)", "Bash(git status:*)", "Bash(git log:*)", "Bash(git add:*)", "Bash(git commit:*)",
             "Bash(docker ps:*)", "Bash(docker logs:*)"]


def secrets_cfg() -> dict:
    """secrets/stream.json; "git_push": true makes an approved resume also push the fix commit."""
    return _json(ROOT / "secrets" / "stream.json") or {}


def git_push() -> str:
    """Push the fix commits, after the organizer approved resuming. Only if this folder is a git repo with a remote."""
    if not (ROOT / ".git").exists():
        return " (git push skipped: not a git repository)"
    r = subprocess.run(["git", "push"], cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    return " Pushed to git." if r.returncode == 0 else f" (git push failed: {(r.stderr or r.stdout)[-150:]})"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(*a) -> None:
    """To the console and to runs/watch/watchdog.log, so what happened can be read back later."""
    line = " ".join([time.strftime("%Y-%m-%d %H:%M:%S"), *map(str, a)])
    print(line[11:], flush=True)
    try:
        WATCH.mkdir(parents=True, exist_ok=True)
        with open(WATCH / "watchdog.log", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def clip(v, n: int = 300):
    if isinstance(v, str):
        return v if len(v) <= n else v[:n] + "..."
    if isinstance(v, list):
        return [clip(x, n) for x in v]
    if isinstance(v, dict):
        return {k: clip(x, n) for k, x in v.items()}
    return v


# ------------------------------------------------------------------ OBS

def obs_scene(name: str | None = None) -> str | None:
    """Switch the live scene (when name is given) and return the current one; None if OBS is unreachable."""
    try:
        from obs_setup import connect
        with connect() as c:                 # close every connection: open ones keep OBS from shutting down
            if name:
                c.set_current_program_scene(name)
            return c.get_current_program_scene().scene_name
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - OBS closed, websocket off, obsws-python missing
        if name:
            log(f"OBS: could not switch to {name!r} ({str(exc)[:120]})")
        return None


def obs_call(fn: str, *args):
    """One OBS request by name (start_stream, stop_stream, get_stream_status...); None if OBS is unreachable."""
    try:
        from obs_setup import connect
        with connect() as c:
            return getattr(c, fn)(*args)
    except (Exception, SystemExit) as exc:  # noqa: BLE001
        log(f"OBS: {fn} failed ({str(exc)[:120]})")
        return None


def obs_streaming() -> bool | None:
    r = obs_call("get_stream_status")
    return None if r is None else bool(r.output_active)


# ------------------------------------------------------------------ stream on/off and the daily window

STREAM_CFG = WATCH / "stream.json"   # {"schedule": {"enabled", "start": "HH:MM", "end": "HH:MM"}, "since": iso}
SCENE_START, SCENE_END = "Starting Soon", "Ending"
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def stream_cfg(**changes) -> dict:
    with LOCK:
        c = _json(STREAM_CFG) or {}
        c.setdefault("schedule", {"enabled": False, "start": "18:00", "end": "23:00"})
        c.update(changes)
        WATCH.mkdir(parents=True, exist_ok=True)
        STREAM_CFG.write_text(json.dumps(c, indent=2), encoding="utf-8")
        return c


def in_window(sched: dict, now: datetime | None = None) -> bool:
    """Local-time daily window; an end before the start runs past midnight (22:00-02:00)."""
    if not sched.get("enabled"):
        return False
    t = (now or datetime.now()).strftime("%H:%M")
    s, e = sched["start"], sched["end"]
    return s <= t < e if s <= e else (t >= s or t < e)


# ------------------------------------------------------------------ child processes, all without windows

# no console window for anything the watchdog starts (DETACHED_PROCESS made every console child, like docker,
# open its own window); a new process group so a child survives the watchdog's own Ctrl+C
HIDDEN = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000} if sys.platform == "win32"
          else {"start_new_session": True})


def kill_tree(pid: int) -> None:
    """The process and everything it started (the relay's ffmpeg, a monitor's server)."""
    if sys.platform == "win32":
        _run(["taskkill", "/PID", str(pid), "/T", "/F"])
    else:
        try:
            os.killpg(pid, 15)
        except OSError:
            pass


def stray_pids(pattern: str) -> list[int]:
    """Processes whose command line matches the regex (left over from an earlier watchdog)."""
    if sys.platform != "win32":
        return []
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match '%s' -and $_.ProcessId -ne %d } "
          "| ForEach-Object { $_.ProcessId }" % (pattern, os.getpid()))
    return [int(x) for x in _run(["powershell", "-NoProfile", "-Command", ps]).split() if x.isdigit()]


def port_open(port: int) -> bool:
    import socket
    with socket.socket() as sk:
        sk.settimeout(0.5)
        return sk.connect_ex(("127.0.0.1", port)) == 0


# ------------------------------------------------------------------ map seeds for fresh runs

def pick_seed() -> tuple[int, bool]:
    """A new map for every fresh run: an unused seed from the checked pool (stream.json "seed_pool", filled by
    --vet-seeds), else a random one. Returns (seed, checked)."""
    c = stream_cfg()
    used = set(c.get("seeds_used") or [])
    pool = [x for x in (c.get("seed_pool") or []) if x not in used]
    seed, vetted = (random.choice(pool), True) if pool else (random.randrange(1, 2**31 - 1), False)
    stream_cfg(seeds_used=sorted(used | {seed}))
    return seed, vetted


def vet_seed(seed: int, task: str = "freeplay-open") -> tuple[bool, str]:
    """Boot the map once (about a minute) and keep it only if iron, copper, coal and stone are near spawn
    (within 64 tiles) and water within 60."""
    out = _run([sys.executable, "-c", "import sys; from arena.cli import main; main(sys.argv[1:])",
                "vet-seed", "--task", task, "--seed", str(seed)], timeout=300)
    try:
        rep = json.loads(out[out.index("{"): out.rindex("}") + 1])
    except ValueError:
        return False, "vet-seed failed: " + " ".join(out[-200:].split())
    near = rep.get("resources_within_64") or {}
    missing = [r for r in ("iron-ore", "copper-ore", "coal", "stone") if r not in near]
    water = rep.get("nearest_water") or {}
    wd = water.get("distance") if isinstance(water, dict) else None
    if missing:
        return False, "no " + ", ".join(missing) + " near spawn"
    if wd is None or wd > 60:
        return False, f"water too far ({wd})"
    return True, ", ".join(f"{k} {v.get('distance')}" for k, v in near.items()) + f", water {wd}"


def vet_seeds(want: int, max_tries: int = 60) -> None:
    """Off-stream job: try random seeds until `want` good maps are in the pool. Never while a run plays."""
    good = 0
    for i in range(max_tries):
        seed = random.randrange(1, 2**31 - 1)
        ok, why = vet_seed(seed)
        log(f"seed {seed}: {'GOOD' if ok else 'skip'} ({why})")
        if ok:
            c = stream_cfg()
            stream_cfg(seed_pool=sorted(set(c.get("seed_pool") or []) | {seed}))
            good += 1
            if good >= want:
                break
    log(f"vetted {i + 1} seeds, {good} added; pool now {len(stream_cfg().get('seed_pool') or [])}")


# ------------------------------------------------------------------ the stream PC's Factorio client

STEAM_FACTORIO = 427520          # Factorio's Steam app id
FACTORIO_GUESSES = [r"E:\SteamLibrary\steamapps\common\Factorio\bin\x64\factorio.exe",
                    r"C:\Program Files (x86)\Steam\steamapps\common\Factorio\bin\x64\factorio.exe",
                    r"C:\Program Files\Factorio\bin\x64\factorio.exe"]


def _run(cmd: list[str], timeout: int = 20) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.TimeoutExpired):
        return ""


def factorio_running() -> bool:
    return "factorio.exe" in _run(["tasklist", "/FI", "IMAGENAME eq factorio.exe", "/NH"]).lower()


def factorio_exe() -> str | None:
    """Where the game client is: remembered in runs/watch/stream.json, else a running client's path, else guesses."""
    known = stream_cfg().get("factorio_exe")
    if known and Path(known).exists():
        return known
    running = _run(["powershell", "-NoProfile", "-Command",
                    "(Get-Process factorio -ErrorAction SilentlyContinue | Select-Object -First 1).Path"]).strip()
    for cand in [running, *FACTORIO_GUESSES]:
        if cand and Path(cand).exists():
            stream_cfg(factorio_exe=cand)
            return cand
    return None


class ClientKeeper:
    """Keeps this PC's Factorio client connected to the run's game server, so the stream shows the game again by
    itself after a resume or a fresh run (each one is a new container on the same spectator port).
    Reads the container's log: "to(InGame)" means the server is up, [JOIN]/[LEAVE] lines say whether anyone is
    watching. When the server is up and nobody is connected, it restarts the client with --mp-connect (at most
    3 tries per server, 2 minutes apart). The scenario turns the follow camera on for whoever joins."""

    def __init__(self):
        self.container: str | None = None
        self.launched_at = 0.0
        self.tries = 0

    @staticmethod
    def port(runs: Runs) -> int:
        d = runs.latest()
        meta = (_json(d / "meta.json") or {}) if d else {}
        return int((meta.get("backend") or {}).get("spectator_port_udp") or 34300)

    @staticmethod
    def server_state(cid: str) -> tuple[bool, bool]:
        out = _run(["docker", "logs", "--tail", "20000", cid], timeout=30)
        watching = 0
        for line in out.splitlines():
            if "[JOIN]" in line:
                watching += 1
            elif "[LEAVE]" in line:
                watching = max(0, watching - 1)
        return "to(InGame)" in out, watching > 0

    def tick(self, runs: Runs) -> None:
        port = self.port(runs)
        cid = _run(["docker", "ps", "--filter", f"publish={port}/udp", "--format", "{{.ID}}"]).strip().split("\n")[0]
        if not cid or " " in cid:                  # no game server right now (between runs), or docker is down
            self.container = None
            return
        if cid != self.container:
            self.container, self.tries, self.launched_at = cid, 0, 0.0
            log(f"client: game server on port {port} is container {cid[:12]}")
        ready, watching = self.server_state(cid)
        if watching:
            self.tries = 0
            return
        if not ready or time.time() - self.launched_at < 120 or self.tries >= 3:
            if self.tries == 3:
                log("client: 3 tries without a join; check the Factorio window (login, version 2.1.24, whitelist)")
                self.tries += 1
            return
        self.launch(port)

    def launch(self, port: int) -> None:
        exe = factorio_exe()
        if not exe:
            log("client: factorio.exe not found; set factorio_exe in runs/watch/stream.json")
            self.tries = 3
            return
        if factorio_running():
            _run(["taskkill", "/IM", "factorio.exe", "/F"])
            time.sleep(4)
        if "steamapps" in exe.lower() and sys.platform == "win32":
            # Steam asks before it runs a game with arguments it didn't set itself, so the Steam copy starts through
            # Steam with no arguments: put "--mp-connect 127.0.0.1:34300" in Factorio's Launch Options in Steam.
            os.startfile(f"steam://rungameid/{STEAM_FACTORIO}")
            how = "through Steam (Launch Options: --mp-connect 127.0.0.1:34300)"
        else:
            flags = HIDDEN
            subprocess.Popen([exe, "--mp-connect", f"127.0.0.1:{port}"], cwd=str(Path(exe).parent),
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **flags)
            how = f"connecting to 127.0.0.1:{port}"
        self.tries += 1
        self.launched_at = time.time()
        log(f"client: started Factorio {how} (try {self.tries})")


# ------------------------------------------------------------------ alerts

def alert_path(aid: str) -> Path:
    return ALERTS / f"{aid}.json"


def load_alerts() -> list[dict]:
    out = []
    for p in sorted(ALERTS.glob("*.json")):
        a = _json(p)
        if a:
            out.append(a)
    return out


def save_alert(a: dict) -> dict:
    ALERTS.mkdir(parents=True, exist_ok=True)
    tmp = alert_path(a["id"]).with_suffix(".tmp")
    tmp.write_text(json.dumps(a, indent=2), encoding="utf-8")
    tmp.replace(alert_path(a["id"]))
    return a


def new_alert(run_id: str, kind: str, title: str, summary: str, analysis: str) -> dict:
    aid = time.strftime("%Y%m%d-%H%M%S-") + pysecrets.token_hex(2)
    a = {"id": aid, "created": now_iso(), "run_id": run_id, "kind": kind, "title": title[:80],
         "summary": summary[:300], "analysis": analysis, "status": "open", "reply": None, "result": None}
    with LOCK:
        save_alert(a)
    log(f"ALERT {aid} [{kind}] {title} -- {summary}")
    return a


def update_alert(aid: str, **changes) -> dict | None:
    with LOCK:
        a = _json(alert_path(aid))
        if not a:
            return None
        a.update(changes)
        return save_alert(a)


STATS = WATCH / "stats.json"        # the show's counters, read by arena/monitor.py for the overlay and screens


def stats(**changes) -> dict:
    """runs/watch/stats.json: {"since": run-id-style UTC stamp, "upgrades": n, "upgrading": bool}."""
    with LOCK:
        s = _json(STATS) or {}
        s.setdefault("since", datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))
        s.setdefault("upgrades", 0)
        s.update(changes)
        WATCH.mkdir(parents=True, exist_ok=True)
        STATS.write_text(json.dumps(s, indent=2), encoding="utf-8")
        return s


def write_status(**changes) -> None:
    with LOCK:
        s = _json(STATUS) or {}
        s.update(changes)
        WATCH.mkdir(parents=True, exist_ok=True)
        STATUS.write_text(json.dumps(s, indent=2), encoding="utf-8")


# ------------------------------------------------------------------ review packet

def goal_kind(goal: str) -> str:
    return re.split(r"[\s{(]", goal or "?", maxsplit=1)[0]


def packet(d: Path, window_s: float, prev: dict | None) -> dict:
    """Everything a reviewer needs to judge the last window, small enough for one prompt (~10k tokens)."""
    now = time.time()
    live = _json(d / "bot_live.json") or {}
    meta = _json(d / "meta.json") or {}
    result = _json(d / "result.json")
    task = meta.get("task_config") or {}
    all_ms = [m["id"] for m in (task.get("score") or {}).get("milestones", [])]
    ev = [e for e in _tail_lines(d / "bot_events.jsonl", 800_000) if e.get("t", 0) >= now - window_s]
    goals = [e for e in ev if e.get("kind") == "goal_end"]
    failed = [e for e in goals if not e.get("ok")]
    repeats = Counter(clip(e.get("goal", ""), 90) for e in goals)
    sup, ov = live.get("supervisor") or {}, live.get("overseer") or {}
    p = {
        "run_id": d.name, "task": meta.get("task"), "map": meta.get("map"),
        "playing": _running(d), "status": live.get("status"), "finished": bool(result),
        "end_reason": (result or {}).get("end_reason") or live.get("end_reason"),
        "seconds_since_bot_update": round(now - live.get("updated", 0)),
        "game_minute": live.get("game_minute"), "activity": live.get("activity"),
        "current_goal": live.get("current_goal"), "queue": (live.get("queue") or [])[:8],
        "stage": live.get("stage"), "score": live.get("score"),
        "roadmap": [{k: s.get(k) for k in ("name", "status", "detail")}
                    for s in ((live.get("roadmap") or {}).get("stages") or [])],
        "milestones_reached": sorted((live.get("milestones") or {}).keys()),
        "milestones_total": len(all_ms), "research": live.get("research"), "rates": live.get("rates"),
        "bot_watchdog": live.get("watchdog"),
        "alerts": [a.get("what") for a in (live.get("alerts") or [])][:10],
        "foreman_problems": ((live.get("foreman") or {}).get("problems") or [])[:10],
        "hints": (live.get("hints") or [])[:10],
        "upkeep_needs": ((live.get("upkeep") or {}).get("needs") or [])[:10],
        "doctrine": live.get("doctrine"),
        "overseer_notes": (ov.get("notes") or [])[-3:], "supervisor_notes": (sup.get("notes") or [])[-3:],
        "llm_cost_usd": {k: (live.get(k) or {}).get("cost_usd") for k in ("assessor", "fixer")},
        "window": {
            "real_minutes": round(window_s / 60), "events": dict(Counter(e.get("kind") for e in ev)),
            "goals_ok": len(goals) - len(failed), "goals_failed": len(failed),
            "failed_by_kind": dict(Counter(goal_kind(e.get("goal", "")) for e in failed)),
            "goals_repeated_3x_or_more": {g: n for g, n in repeats.items() if n >= 3},
            "milestones_gained": [e.get("name") or e.get("id") for e in ev if e.get("kind") == "milestone"],
            "recent_failures": [{"minute": e.get("minute"), "goal": e.get("goal"), "result": e.get("result")}
                                for e in failed[-8:]],
            "bot_watchdog_events": [e for e in ev if e.get("kind") in ("watchdog", "jam", "gate", "aged")][-8:],
        },
    }
    if prev:
        p["since_last_review"] = {
            "game_minutes": round((p["game_minute"] or 0) - (prev.get("game_minute") or 0), 1),
            "new_milestones": sorted(set(p["milestones_reached"]) - set(prev.get("milestones_reached") or [])),
            "score_change": (p["score"] or 0) - (prev.get("score") or 0),
            "research_then": prev.get("research"),
        }
    logs = sorted(d.glob("*.log"), key=lambda f: f.stat().st_mtime)
    if logs and (not p["playing"] or p["seconds_since_bot_update"] > 300):
        p["process_log_tail"] = logs[-1].read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
    p["window"]["rebuilt_spots"] = churn(d, now - window_s)
    p["findings"] = findings(d, p, ev, live, prev)
    p["flags"] = flags(p)
    return clip(p, 400)


# The checks a person makes when asked "go through the run and find what went wrong" (run 20261121-140755: the
# review said ok five times while the bot looped on one belt for two hours, failed to gather plates, built a dead
# plastics station, re-polluted one belt six times, left the labs idle, and 22 of 62 sites stood mostly still).
# Each finding is a fact from the run, not a judgement; the reviewer must explain every one before saying ok.
DEAD_SHARE = 0.35          # a site with fewer of its machines working than this is "mostly still"
NOT_SITES = {"labs", "poles", "pole", "storage", "pumpjack", "steam_power"}
DROUGHT_MIN = 90.0         # game minutes without a milestone


def findings(d: Path, p: dict, ev: list, live: dict, prev: dict | None) -> list[dict]:
    out = []

    def add(key, what, **evidence):
        out.append({"key": key, "what": what, **({"evidence": evidence} if evidence else {})})
    goals = [e for e in ev if e.get("kind") == "goal_end"]
    soft = [e for e in goals if str(e.get("result", "")).startswith(("paused", "dropped", "held back"))]
    if len(soft) >= 3:
        add("soft-failures", f"{len(soft)} goals were paused, dropped or held back this window (counted as ok)",
            examples=[clip(e.get("goal", ""), 80) for e in soft[:4]])
    starved = [e for e in goals if "in time" in str(e.get("result", ""))]
    if starved:
        add("gathering", f"{len(starved)} goal(s) failed waiting for materials",
            examples=[clip(e.get("result", ""), 100) for e in starved[:3]])
    long_ = [e for e in goals if (e.get("game_minutes") or 0) >= 15]
    if long_:
        add("slow-goals", f"{len(long_)} goal(s) took 15+ game minutes",
            examples=[f"{clip(e.get('goal', ''), 60)}: {e.get('game_minutes')} min" for e in long_[:4]])
    repairs = Counter(clip(e.get("goal", ""), 90) for e in goals
                      if str(e.get("goal", "")).startswith(("relink", "purge", "replace", "upgrade")))
    again = {g: n for g, n in repairs.items() if n >= 2}
    if again:
        add("repeated-repairs", "the same repair ran more than once: what it fixed came back", repairs=again)
    aged = [e for e in ev if e.get("kind") == "aged"]
    if aged:
        add("plan-starved", f"{len(aged)} planned step(s) waited 20+ min behind fixes and the close-out",
            examples=[f"{clip(e.get('goal', ''), 60)}: {e.get('waited_min')} min" for e in aged[:4]])
    research = live.get("research") or {}
    idle = [q for q in ((live.get("foreman") or {}).get("problems") or []) if "labs were idle" in q or "labs idle" in q]
    if (not research.get("current") and not research.get("queue")) or idle:
        add("research-idle", "the labs ran out of research" + (f" ({len(idle)} times noted)" if idle else ""),
            research=research)
    sites = live.get("base") or []
    dead = []
    for s in sites:
        h = s.get("health") or {}
        total = sum(h.values())
        if s.get("kind") in NOT_SITES or not total:
            continue
        if h.get("working", 0) / total < DEAD_SHARE:
            dead.append(f"{s.get('id')} {s.get('kind')} {s.get('item') or s.get('resource') or ''}: "
                        + ", ".join(f"{k} {v}" for k, v in sorted(h.items(), key=lambda kv: -kv[1])[:3]))
    if dead and len(dead) >= max(3, len(sites) // 6):
        add("idle-sites", f"{len(dead)} of {len(sites)} sites stand mostly still", sites=dead[:12])
    fluid = [s for s in sites if (s.get("health") or {}).get("no_input_fluid")]
    pumps = [e for e in ev if e.get("kind") == "pumps"]
    if fluid or pumps:
        add("fluid", "sites without their fluid", sites=[f"{s.get('id')} {s.get('kind')}" for s in fluid[:6]],
            notes=[e.get("note") for e in pumps][:3])
    purges = Counter(str(e.get("goal", ""))[:60] for e in goals if str(e.get("goal", "")).startswith("purge"))
    if any(n >= 2 for n in purges.values()):
        add("pollution", "a belt was cleaned more than once: something keeps feeding it the wrong items",
            purges=dict(purges))
    churn_ = (p.get("window") or {}).get("rebuilt_spots") or {}
    if churn_.get("spots", 0) >= 5:
        add("build-loop", f"{churn_['spots']} spots placed and taken up 4+ times (a loop, or the same belts "
            "cleaned or relaid over and over)", worst=churn_.get("worst"))
    ms = [e for e in _tail_lines(d / "bot_events.jsonl", 2_000_000) if e.get("kind") == "milestone"]
    last = max([e.get("minute") or 0 for e in ms], default=0)
    gm = p.get("game_minute") or 0
    if gm - last >= DROUGHT_MIN:
        add("no-milestone", f"no milestone for {gm - last:.0f} game minutes (last at minute {last:.0f})")
    if prev:                                   # findings that were there last time too: not resolving on their own
        before = {f["key"] for f in prev.get("findings") or []}
        for f in out:
            if f["key"] in before:
                f["again"] = True
    return out


def churn(d: Path, since: float) -> dict:
    """Spots where the bot placed something and took it up again over and over: a build loop that goal results
    never show. Run 20261121-140755: one link's 45 belts were laid and taken up 56 times in two hours while
    every goal read "collecting plates" and the review said ok."""
    placed: Counter = Counter()
    picked: Counter = Counter()
    for a in _tail_lines(d / "actions.jsonl", 12_000_000):
        if a.get("t", 0) < since or not a.get("ok"):
            continue
        act = a.get("action") or {}
        if act.get("type") == "place" and act.get("entity"):
            placed[(act["entity"], act.get("x"), act.get("y"))] += 1
        elif act.get("type") == "pickup" and not (a.get("data") or {}).get("cleared"):   # not trees or rocks
            picked[(act.get("x"), act.get("y"))] += 1
    again = {k: n for k, n in placed.items() if n >= 4 and picked.get(k[1:], 0) >= 3}
    top = sorted(again.items(), key=lambda kv: -kv[1])[:5]
    return {"spots": len(again), "placements": sum(again.values()),
            "worst": [f"{e} at ({x}, {y}) placed {n}x" for (e, x, y), n in top]}


def flags(p: dict) -> list[str]:
    """Cheap signals, handed to the reviewer as leads (it decides; these never alert on their own)."""
    f, w = [], p["window"]
    if not p["playing"] and not p["finished"]:
        f.append(f"bot process looks dead: no update for {p['seconds_since_bot_update']} s and no result")
    if w["goals_failed"] and w["goals_failed"] >= max(3, w["goals_ok"]):
        f.append(f"more goals failed than succeeded this window ({w['goals_failed']} vs {w['goals_ok']})")
    if w["goals_repeated_3x_or_more"]:
        f.append(f"{len(w['goals_repeated_3x_or_more'])} goal(s) repeated 3+ times: possible loop")
    c = w.get("rebuilt_spots") or {}
    if c.get("spots", 0) >= 5:
        f.append(f"build loop: {c['spots']} spots placed and taken up again 4+ times ({c['placements']} placements, "
                 f"e.g. {'; '.join(c['worst'][:2])}): look for a build that keeps rolling back")
    s = p.get("since_last_review") or {}
    if s and not s.get("new_milestones") and s.get("research_then") == p.get("research"):
        f.append("no new milestone and research unchanged since the last review")
    for x in p.get("findings") or []:
        f.append(f"finding {x['key']}{' (again)' if x.get('again') else ''}: {x['what']}")
    if (p.get("bot_watchdog") or {}).get("minutes_without_progress", 0) > 20:
        f.append(f"bot's own watchdog: {p['bot_watchdog']['minutes_without_progress']} game min without progress")
    return f


# ------------------------------------------------------------------ Claude

def run_claude(prompt: str, tools: list[str], model: str | None, timeout_s: int, edits: bool) -> dict:
    exe = shutil.which("claude")
    if not exe:
        return {"error": "claude CLI not found on PATH"}
    cmd = [exe, "-p", "--output-format", "json", "--no-session-persistence",
           "--permission-mode", "acceptEdits" if edits else "default", "--allowedTools", *tools]
    if not edits:
        cmd += ["--disallowedTools", "Edit", "Write", "NotebookEdit"]
    if model:
        cmd += ["--model", model]
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                           cwd=str(ROOT), timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return {"error": f"claude timed out after {timeout_s} s"}
    try:
        outer = json.loads(r.stdout)
    except ValueError:
        return {"error": f"claude exit {r.returncode}: {(r.stderr or r.stdout)[-400:]}"}
    text = outer.get("result") or ""
    for cand in (text, text[text.find("{"): text.rfind("}") + 1]):
        try:
            out = json.loads(cand)
            out["_cost_usd"] = outer.get("total_cost_usd")
            return out
        except ValueError:
            continue
    return {"error": "reply was not JSON", "raw": text[-1500:]}


REVIEW_PROMPT = """You are the hourly reviewer for a live stream where an AI bot plays stock Factorio entirely on its \
own (task freeplay: from nothing to launching a rocket). Review the run the way the organizer's engineer does when \
asked "go through the run and find everything that went wrong, don't overlook anything": judge how the whole bot \
performs, not whether it is moving at all.

Read the review packet first: {packet}
Its "findings" are facts the watchdog measured (repeated repairs, build loops, goals failing for materials, planned \
steps starved behind fixes, idle labs, sites standing still, missing fluids, re-polluted belts, milestone droughts). \
Dig into the run folder {run} as far as you need (bot_live.json = live state with every site's machine states; \
bot_events.jsonl = goals, plans, overseer notes; actions.jsonl is ~100 MB: only tail or grep it) and the code (bots/).

For EVERY finding: confirm it in the data, find its cause, and decide whether it is noise or a real problem. Also \
look for what the findings do not cover: time going into rework instead of growth, the same problem in another \
form, decisions that will block a later stage (oil, blue science, the silo), resources running out, waste.

Verdicts:
- "ok": progressing well AND every finding is explained as noise (say why for each in "analysis").
- "problems": anything real, even while it progresses: a systemic issue, a loop, wasted hours, a stage that will \
block. This alerts the organizer's phone with your analysis and plan, exactly as the engineer would report it.
- "stuck": it cannot recover by itself (no progress, its own recovery failing, the process died). "certain": true \
only if you would bet on it.
- "unsure": you could not tell from the data; you are asked again in {unsure} minutes.

For "problems" and "stuck", "analysis" (markdown) holds, per problem: 1. Symptom. 2. Evidence (packet fields, \
events, log lines, numbers). 3. Root cause in the code (file, function). 4. Why it is systemic: where else this class \
of failure bites, and why a narrow patch would not be a real fix. 5. Plan: the exact bot-side changes (files, \
functions, what changes); the organizer approves this plan and nothing outside it changes without asking again. \
6. Risk. Order problems by how much they cost the run. Fixes belong in the bot (bots/): the point is to beat the \
stock game, so never propose changing the game, the server's rules or the scenario (arena/, lua/, config/) to make \
it easier. Do not change any files.

Answer with ONLY one JSON object, nothing around it:
{{"verdict": "ok"|"problems"|"stuck"|"unsure", "certain": true|false, "title": "<= 60 chars", \
"summary": "<= 200 chars, read on a phone lock screen", "analysis": "markdown"}}"""

FIX_PROMPT = """The live stream shows the "Self-Upgrading" screen. The bot's run {run_id} is saved and stopped. \
The organizer read the watchdog's analysis and approved a fix.

## Watchdog analysis
{title}

{analysis}

## Organizer's instructions
{text}

## What to do
You may change what the fix needs in the bot (bots/), its tests (tests/), the stream tooling (stream/, and OBS \
through `uv run --no-sync --with obsws-python python stream/obs_setup.py`) and docs. Never change the game, the \
server's rules or the scenario (arena/, lua/, config/) to help the bot: the point is to beat the stock game. \
And ONLY what the approved plan above and the organizer's instructions cover. If the real fix needs a change outside \
that (another subsystem, a bigger redesign, deleting things, anything the organizer did not approve), do not make it: \
stop and answer with "needs_approval" set, describing exactly what you want to change and why. The organizer is asked \
and you get another session with their answer.
1. Confirm the root cause in the code before changing anything (the run folder is {run}; actions.jsonl is huge: tail/grep only).
2. Fix the systemic cause, not only this instance: the fix must also hold for the other cases the analysis lists. \
Keep everything else as it is.
3. The run resumes from a save with the new bot code, so a bot fix applies right away.
4. Run only short tests: `uv run python -m pytest -q -x <relevant test files>`. Never start a real Factorio game \
(no `arena local-run`, no ARENA_FACTORIO tests). Do not touch secrets/ or runs/.
5. If this folder is a git repository, commit your change with a message that starts "Self-upgrade #{n}: " and a \
one-line summary, then a short body (root cause, what changed). Never push: that happens only after the organizer \
approves the next step. Never commit secrets/, runs/ or .env.
6. Decide whether the run should restart from scratch or continue from its save. This is a live stream: the goal is \
steady, watchable progress, not the fastest finish. Viewers should not watch hours of the bot untangling its own mess. \
Restart when the base is badly tangled (lots of broken, walled-in or churned sites, routing failures piling up), when \
untangling would take longer than rebuilding to the same point, or when the fix only helps early-game decisions the \
run is already past. Continue when the base is basically \
sound and the fix lets it move forward right away. Look at the run's state (bot_live.json: base, upkeep, hints, \
milestones and their times) to judge this, and weigh how long it took to reach this point.

Answer with ONLY one JSON object, nothing around it:
{{"fixed": true|false, "restart": true|false, "restart_reason": "one or two sentences: why restart or continue", \
"needs_approval": null or "what you want to change beyond the plan, and why", \
"summary": "<= 250 chars for the phone", "report": "markdown: root cause, what changed (files and functions), why it \
generalizes, tests run and their results, what to watch after resuming"}}"""


# ------------------------------------------------------------------ the loop

class Watchdog:
    def __init__(self, a):
        self.a = a
        self.runs = Runs(RUNS)
        self.prev: dict[str, dict] = {}          # run id -> the packet of its previous review

    def playing_run(self) -> Path | None:
        d = self.runs.latest()
        return d if d and not (d / "result.json").exists() else None

    def review(self, alert: bool = True) -> str:
        d = self.playing_run()
        if d is None:
            write_status(last_check=now_iso(), last_verdict="ok", note="no run is playing")
            log("review: no run is playing")
            return "idle"
        open_ = [x for x in load_alerts() if x["status"] in ("open", "answered", "fixing")]
        if open_ and alert:
            log(f"review skipped: alert {open_[-1]['id']} is still waiting for you")
            return "waiting"
        p = packet(d, self.a.interval * 60, self.prev.get(d.name))
        REVIEWS.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        pp = REVIEWS / f"{stamp}-{d.name}-packet.json"
        pp.write_text(json.dumps(p, indent=2, default=str), encoding="utf-8")
        log(f"review: {d.name} at game minute {p['game_minute']}, flags: {p['flags'] or 'none'}")
        out = run_claude(REVIEW_PROMPT.format(packet=pp, run=d, unsure=self.a.unsure_interval),
                         REVIEW_TOOLS, self.a.model, 20 * 60, edits=False)
        (REVIEWS / f"{stamp}-{d.name}-verdict.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
        self.prev[d.name] = p
        if "error" in out:
            log("review failed:", out["error"])
            write_status(last_check=now_iso(), last_verdict="unsure", note="review failed: " + out["error"][:150])
            return "unsure"
        verdict = out.get("verdict", "unsure")
        log(f"verdict: {verdict} (certain={out.get('certain')}) {out.get('title', '')} -- {out.get('summary', '')}")
        write_status(last_check=now_iso(), last_verdict=verdict, note=out.get("summary", "")[:200])
        if alert and (verdict == "problems" or (verdict == "stuck" and out.get("certain"))):
            kind = "stuck" if verdict == "stuck" else "problems"
            title = out.get("title") or ("Bot is stuck" if kind == "stuck" else "Problems in the run")
            open_ = [a for a in load_alerts() if a.get("run_id") == d.name and a.get("kind") == "problems"
                     and a.get("status") == "open"]
            if kind == "problems" and open_:          # still unanswered: bring it up to date, no second alert
                update_alert(open_[-1]["id"], title=title[:80], summary=(out.get("summary") or "")[:300],
                             analysis=out.get("analysis") or "", updated=now_iso())
            else:
                new_alert(d.name, kind, title, out.get("summary") or "", out.get("analysis") or "")
        if verdict == "problems":
            return "ok"                               # the organizer decides; the next review comes on schedule
        return verdict if verdict != "stuck" or out.get("certain") else "unsure"

    def handle_replies(self) -> None:
        for al in load_alerts():
            if al["status"] != "answered":
                continue
            decision, text = al["reply"]["decision"], al["reply"].get("text") or ""
            d = self.runs.find(al["run_id"])
            if decision == "run":
                if al.get("kind") in ("resume", "failed", "approval") and d is not None:
                    restart = bool(al.get("restart"))
                    if re.search(r"\brestart\b", text, re.I):
                        restart = True
                    elif re.search(r"\bcontinue\b", text, re.I):
                        restart = False
                    self._answering = al["id"]
                    try:
                        update_alert(al["id"], status="closed",
                                     result=self.start_fresh(d) if restart else self.resume(d))
                    finally:
                        self._answering = None
                else:
                    update_alert(al["id"], status="closed", result="Left running. Next review in an hour.")
                    log(f"{al['id']}: left running")
            elif decision == "fix" and d is not None:
                self.fix(al, d, text)
            else:
                update_alert(al["id"], status="closed", result="run folder not found; nothing done")

    def fix(self, al: dict, d: Path, text: str) -> None:
        update_alert(al["id"], status="fixing")
        st = stats()
        stats(upgrades=st["upgrades"] + 1, upgrading=True)   # the screen shows "Self-upgrade #n" while it works
        try:
            self._fix(al, d, text)
        finally:
            stats(upgrading=False)

    def _fix(self, al: dict, d: Path, text: str) -> None:
        obs_scene(SCENE_UPGRADE)
        if _running(d):
            log(f"{al['id']}: stopping the run (it saves at its next safe point)")
            request_control(d, "stop")
            t0 = time.time()
            while _running(d) and time.time() - t0 < 15 * 60:
                time.sleep(5)
        log(f"{al['id']}: Claude is fixing ({'run stopped' if not _running(d) else 'run did not stop in 15 min'})")
        out = run_claude(FIX_PROMPT.format(run_id=d.name, run=d, title=al["title"], analysis=al["analysis"],
                                           n=stats()["upgrades"],
                                           text=text or "(no extra instructions)"),
                         FIX_TOOLS, self.a.model, 60 * 60, edits=True)
        (REVIEWS / f"{time.strftime('%Y%m%d-%H%M%S')}-{d.name}-fix.json").write_text(json.dumps(out, indent=2),
                                                                                     encoding="utf-8")
        if "error" in out:
            update_alert(al["id"], status="closed", result="Fix session failed: " + out["error"][:300])
            new_alert(d.name, "failed", "Fix session failed", out["error"][:200],
                      f"The fix session did not finish.\n\n```\n{out.get('raw') or out['error']}\n```\n\n"
                      "Fix it = try again with your text. Let it run = resume the run unchanged.")
            return
        update_alert(al["id"], status="closed", result=out.get("summary", ""))
        if out.get("needs_approval"):
            new_alert(d.name, "approval", "Needs your OK: " + (out.get("summary") or "a bigger change")[:60],
                      str(out["needs_approval"])[:280],
                      f"## Asks to change, beyond what you approved\n{out['needs_approval']}\n\n"
                      f"## What it found so far\n{out.get('report') or ''}\n\n---\n"
                      "Fix it = approve this (add limits in the text). Let it run = no; resume the run as it is.")
            return
        push = ", and push the commit to git" if secrets_cfg().get("git_push") else ""
        restart = bool(out.get("restart"))
        how = "start a fresh run (new run number)" if restart else "continue this run from its save"
        title = (f"Fix ready: {'restart' if restart else 'continue'} the run?" if out.get("fixed")
                 else "Could not fix it: needs you")
        new_alert(d.name, "resume" if out.get("fixed") else "failed", title, out.get("summary", ""),
                  (out.get("report") or "")
                  + f"\n\n## Restart or continue\n**Recommendation: {how}.** {out.get('restart_reason') or ''}\n\n---\n"
                  f"Let it run = {how}{push}, then back to Live. Write \"restart\" or \"continue\" in the text to "
                  "override. Fix it = another round with your text.")
        update_alert(al["id"], restart=restart)

    def start_fresh(self, d: Path, quiet: bool = False) -> str:
        """A new run with the same settings (task, bot, map seed) as run d: a new run number on the overlay."""
        argv = [a for a in ((_json(d / "launch.json") or {}).get("argv") or []) if isinstance(a, str)]
        if "--seed" in argv:                       # every fresh run gets a new map
            i = argv.index("--seed")
            del argv[i:i + 2]
        seed, vetted = pick_seed()
        argv += ["--seed", str(seed)]
        if "local-run" not in argv:
            return "No launch.json for this run: start the new run by hand."
        if _running(d):
            request_control(d, "stop")
        WATCH.mkdir(parents=True, exist_ok=True)
        log_path = WATCH / f"fresh-{time.strftime('%Y%m%d-%H%M%S')}.log"
        flags = HIDDEN
        with open(log_path, "ab") as fh:
            subprocess.Popen([sys.executable, "-c", "import sys; from arena.cli import main; main(sys.argv[1:])",
                              *argv, "--monitor", "0"], cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, **flags)
        if not quiet:
            obs_scene(SCENE_BETWEEN)
            threading.Timer(120, obs_scene, args=(SCENE_LIVE,)).start()   # a fresh world takes a bit longer to boot
        self.check_comeback("starting a fresh run", d.name, fresh=True, alert_id=self._answering)
        log(f"started a fresh run like {d.name} on seed {seed} ({'checked' if vetted else 'unchecked'} map, "
            f"log {log_path.name})")
        pushed = git_push() if secrets_cfg().get("git_push") and not quiet else ""
        live = "" if quiet else " Stream goes back to Live in 2 min."
        return f"Started a fresh run on a new map (seed {seed}).{live}{pushed}"

    def resume(self, d: Path, quiet: bool = False) -> str:
        done = [s for s in saves(d) if s["complete"]]
        if not done:
            return "No complete save to resume from: start it by hand."
        last = max(done, key=lambda s: s["minute"] or 0)
        r = request_resume(d, last["name"])
        if not r.get("ok"):
            return "Resume failed: " + r.get("error", "?")
        if not quiet:
            obs_scene(SCENE_BETWEEN)
            threading.Timer(90, obs_scene, args=(SCENE_LIVE,)).start()   # the game takes about a minute to load
        log(f"resumed {d.name} from {last['name']}")
        self.check_comeback(f"resuming {d.name}", d.name, fresh=False, alert_id=self._answering)
        pushed = git_push() if secrets_cfg().get("git_push") and not quiet else ""
        live = "" if quiet else " Stream goes back to Live in 90 s."
        return f"Resumed from {last['name']} (game minute {last['minute']}).{live}{pushed}"

    # ---- stream on/off (phone buttons and the daily window)

    relay: subprocess.Popen | None = None
    monitor: subprocess.Popen | None = None
    _answering: str | None = None             # the alert a restart answers, for its check result
    was_in_window: bool | None = None        # None until the first pass

    def clean_strays(self) -> None:
        """Relays (and their ffmpeg) left by an earlier watchdog would fight over the port: end them."""
        for pid in stray_pids(r"stream.relay\.py") + stray_pids(r"ffmpeg.*127\.0\.0\.1:1935"):
            if self.relay is not None and pid == self.relay.pid:
                continue
            kill_tree(pid)
            log(f"ended a leftover relay process ({pid})")

    def ensure_relay(self) -> None:
        if self.relay is None or self.relay.poll() is not None:
            self.relay = None
            self.clean_strays()
            WATCH.mkdir(parents=True, exist_ok=True)
            fh = open(WATCH / "relay.log", "ab")
            self.relay = subprocess.Popen([sys.executable, str(ROOT / "stream" / "relay.py")], cwd=str(ROOT),
                                          stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **HIDDEN)
            log("relay started")
            time.sleep(2)                       # let ffmpeg open its port before OBS connects

    def ensure_monitor(self) -> None:
        """The overlay and the screens come from `arena monitor` (port 8730); only needed while live."""
        if port_open(8730):
            return
        WATCH.mkdir(parents=True, exist_ok=True)
        fh = open(WATCH / "monitor.log", "ab")
        self.monitor = subprocess.Popen(
            [sys.executable, "-c", "import sys; from arena.cli import main; main(['monitor'])"], cwd=str(ROOT),
            stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **HIDDEN)
        for _ in range(30):
            if port_open(8730):
                break
            time.sleep(0.5)
        log("monitor started" if port_open(8730) else "monitor did not come up (see runs/watch/monitor.log)")
        if port_open(8730):                          # pages that loaded while it was down are blank: reload them
            self.reset_cards()

    def shut_down_helpers(self) -> None:
        """After the stream: the relay and the monitor go and the game client closes. The bot's run ends by itself
        once its save is done (a background thread waits for that and logs it)."""
        for name in ("relay", "monitor"):
            proc = getattr(self, name)
            if proc is not None and proc.poll() is None:
                kill_tree(proc.pid)
                log(f"{name} closed")
            setattr(self, name, None)
        self.clean_strays()
        for pid in stray_pids(r"main\(\[.monitor.\]\)"):    # a monitor started by an earlier watchdog
            kill_tree(pid)
            log(f"ended a leftover monitor ({pid})")
        if factorio_running():
            _run(["taskkill", "/IM", "factorio.exe"])      # ask nicely first
            for _ in range(10):
                if not factorio_running():
                    break
                time.sleep(1)
            if factorio_running():
                _run(["taskkill", "/IM", "factorio.exe", "/F"])
            log("game client closed")
        d = self.runs.latest()
        if d is not None and _running(d):
            threading.Thread(target=self._wait_run_end, args=(d,), daemon=True).start()

    # ---- after a restart: check that everything really came back

    def check_comeback(self, label: str, old: str, fresh: bool, alert_id: str | None = None,
                       deadline_s: float = 6 * 60) -> None:
        """Runs in the background after a resume or a fresh run. Within the deadline, every part must be back:
        the bot playing, the game server up, the game client watching and, when the stream is live, OBS on the Live
        scene with the overlay following this run. All fine: a quiet line in the log (and on the alert it answers).
        Anything missing: an alert to the phone that says what didn't come back."""
        threading.Thread(target=self._check_comeback, args=(label, old, fresh, alert_id, deadline_s),
                         daemon=True).start()

    def comeback_state(self, old: str, fresh: bool) -> dict:
        d = self.runs.latest()
        ok = {"run": bool(d) and (d.name != old if fresh else d.name == old)}
        ok["bot playing"] = bool(ok["run"] and _running(d))
        cid = _run(["docker", "ps", "--filter", f"publish={ClientKeeper.port(self.runs)}/udp",
                    "--format", "{{.ID}}"]).strip().split("\n")[0]
        ready, watching = ClientKeeper.server_state(cid) if cid and " " not in cid else (False, False)
        ok["game server up"] = ready
        live = bool(obs_streaming())
        if live:                                     # the client and the picture only matter while on air
            ok["game client watching"] = watching
            ok["stream on the Live scene"] = obs_scene() == SCENE_LIVE
            ov = _run(["curl", "-s", "-m", "3", "http://127.0.0.1:8730/api/overlay/latest"])
            ok["overlay following this run"] = bool(d) and f'"id": "{d.name}"' in ov.replace('":"', '": "')
        ok["_run_id"] = d.name if d else None
        return ok

    def _check_comeback(self, label: str, old: str, fresh: bool, alert_id: str | None, deadline_s: float) -> None:
        t0, state = time.time(), {}
        while time.time() - t0 < deadline_s:
            time.sleep(15)
            state = self.comeback_state(old, fresh)
            if all(v for k, v in state.items() if not k.startswith("_")):
                parts = ", ".join(k for k in state if not k.startswith("_"))
                log(f"check: {label} came back fine after {int(time.time() - t0)} s ({parts})")
                write_status(note=f"{label}: everything came back fine")
                if alert_id:
                    a = _json(alert_path(alert_id)) or {}
                    update_alert(alert_id, result=(a.get("result") or "") + f" Checked: {parts}, all fine.")
                return
        missing = [k for k, v in state.items() if not k.startswith("_") and not v]
        fine = [k for k, v in state.items() if not k.startswith("_") and v]
        log(f"check: {label} did NOT fully come back: missing {missing}")
        run_id = state.get("_run_id") or old
        new_alert(run_id, "check", f"After {label}: {', '.join(missing)[:50]} missing",
                  f"{int(deadline_s // 60)} min after {label}, not back: {', '.join(missing)}.",
                  f"## What didn't come back\n" + "\n".join(f"- {m}" for m in missing)
                  + ("\n\n## What did\n" + "\n".join(f"- {f}" for f in fine) if fine else "")
                  + "\n\nLogs: `runs/watch/watchdog.log`, the run's newest `*.log`, `runs/watch/fresh-*.log`."
                  "\n\n---\nFix it = Claude looks into it (with your text). Let it run = leave it as it is.")

    def _wait_run_end(self, d: Path) -> None:
        t0 = time.time()
        while _running(d) and time.time() - t0 < 15 * 60:
            time.sleep(10)
        log(f"bot run {d.name} " + ("saved and stopped" if not _running(d) else "still runs after 15 min: check it"))

    def start_game(self, fresh: bool = False) -> str:
        """The bot plays only while the stream is live. Carry on the stream's run from its newest save when it was
        stopped at the end of the last stream; start a fresh run with the same settings when it really ended."""
        d = self.runs.latest()
        if d is None:
            return "no earlier run to copy the settings from: start the first run by hand"
        if _running(d):
            ctl = _json(d / "control.json") or {}
            if ctl.get("cmd") == "stop" and time.time() - ctl.get("at", 0) < 15 * 60:
                # still saving from the last stream's end: carry on as soon as that save is done
                threading.Thread(target=self._start_after_stop, args=(d,), daemon=True).start()
                return "the bot is still saving from the last stream; it carries on as soon as that is done"
            return "the bot is already playing"
        if fresh:                                   # a new game was asked for: new map, from scratch
            return self.start_fresh(d, quiet=True)
        result = _json(d / "result.json") or {}
        done = [sv for sv in saves(d) if sv["complete"]]
        newest = max(done, key=lambda sv: sv["minute"] or 0) if done else None
        # a run the stream stopped ends with a "stop" save; a finished run (rocket, give-up) has none to carry on
        if newest and newest.get("kind") == "stop" and result.get("end_reason") in (None, "ended_by_team"):
            return self.resume(d, quiet=True)
        return self.start_fresh(d, quiet=True)

    def _fresh_after_stop(self, d: Path) -> None:
        t0 = time.time()
        while _running(d) and time.time() - t0 < 10 * 60:
            time.sleep(5)
        log("game: " + self.start_fresh(d, quiet=True))

    def _start_after_stop(self, d: Path) -> None:
        t0 = time.time()
        while _running(d) and time.time() - t0 < 10 * 60:
            time.sleep(5)
        if obs_streaming():
            log("game: " + self.start_game())

    def stop_game(self) -> str:
        d = self.runs.latest()
        if d is None or not _running(d):
            return "the bot was not playing"
        r = request_control(d, "stop")
        log(f"stream ending: the bot saves and stops ({r.get('note') or r.get('error')})")
        return "the bot saves and stops at its next safe point; it carries on from there next stream"

    def reset_cards(self, countdown_min: float = 2) -> None:
        """Reload every screen so none shows a stale clock or finished countdown; Starting Soon gets the real wait.
        The game overlay reloads too: a browser source that loaded while the monitor was down stays blank for good."""
        from obs_setup import SCREENS
        obs_call("press_input_properties_button", OVERLAY_SOURCE, "refreshnocache")
        for scene, src, path in SCREENS:
            if scene == SCENE_START:
                obs_call("set_input_settings", src, {"url": f"http://127.0.0.1:8730/screen?mode=starting"
                                                            f"&t={max(0, countdown_min):g}&v={int(time.time())}"}, True)
            obs_call("press_input_properties_button", src, "refreshnocache")

    end_timer: threading.Timer | None = None

    def start_stream(self, countdown_min: float = 2, reset: bool = False) -> dict:
        ending = self.end_timer is not None and self.end_timer.is_alive()
        if ending:                                  # Start during the outro: stay on air and begin again
            self.end_timer.cancel()
            self.end_timer = None
            log("start pressed during the outro: staying live")
        was_live = bool(obs_streaming())
        if was_live and not ending:
            return self.stream_state(note="already live")
        self.ensure_monitor()
        if not was_live:
            self.ensure_relay()
        self.reset_cards(countdown_min)
        obs_scene(SCENE_START)
        if not was_live:
            obs_call("start_stream")                # its reply can be slow or time out: check the real state instead
            for _ in range(20):
                if obs_streaming():
                    break
                time.sleep(1)
            else:
                log("OBS did not go live within 20 s")
                return self.stream_state(note="OBS did not start streaming (is it open? check its Stream settings)")
            stream_cfg(since=now_iso())
        game = ""
        reset = reset or bool(stream_cfg().get("reset_next"))
        if reset:                                   # the show starts over: run #1, 0 upgrades, a new map
            stats(since=datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"), upgrades=0, upgrading=False)
            stream_cfg(reset_next=False)
            log("reset: counters back to run #1 and 0 upgrades; a new game starts")
        if stream_cfg().get("game_with_stream", True) and not self.a.no_game:
            try:
                d = self.runs.latest()
                if reset and d is not None and _running(d):
                    request_control(d, "stop")      # the old game saves and ends; the new one waits for its slot
                    threading.Thread(target=self._fresh_after_stop, args=(d,), daemon=True).start()
                    game = "the old game saves and stops, then a new game starts"
                else:
                    game = self.start_game(fresh=reset)
            except Exception as exc:  # noqa: BLE001 - the stream goes on even if the game won't start
                game = f"could not start the game: {exc!r}"[:200]
            log("game: " + game)
        threading.Timer(max(0, countdown_min) * 60, obs_scene, args=(SCENE_LIVE,)).start()
        self.next_check = time.time() + self.a.interval * 60           # first review an hour into the stream
        log(f"stream started; Live in {countdown_min} min")
        return self.stream_state(note=f"live; switching to the game in {countdown_min:g} min. {game}".strip())

    def stop_stream(self, outro_s: float = 30) -> dict:
        if not obs_streaming():
            return self.stream_state(note="not live")
        if self.end_timer is not None and self.end_timer.is_alive():
            return self.stream_state(note="already ending")
        obs_call("press_input_properties_button", "Screen: Ending", "refreshnocache")
        obs_scene(SCENE_END)
        game = ""
        if stream_cfg().get("game_with_stream", True) and not self.a.no_game:
            game = self.stop_game()

        def end():
            obs_call("stop_stream")
            for _ in range(20):
                if not obs_streaming():
                    break
                time.sleep(1)
            stream_cfg(since=None)
            self.reset_cards()                      # fresh cards, and OBS waits on Starting Soon for the next stream
            obs_scene(SCENE_START)
            time.sleep(3)                           # let the cards load before their server goes
            self.shut_down_helpers()
            self.end_timer = None
            log("stream ended")
        self.end_timer = threading.Timer(outro_s, end)
        self.end_timer.start()
        log(f"stream ending in {outro_s:g} s")
        return self.stream_state(note=f"ending: the stream stops in {outro_s:g} s. {game}".strip())

    def stream_state(self, note: str = "") -> dict:
        c = stream_cfg()
        live = obs_streaming()
        return {"streaming": bool(live), "obs": live is not None, "since": c.get("since") if live else None,
                "scene": obs_scene() or "unknown", "schedule": c["schedule"], "reset_next": bool(c.get("reset_next")),
                "note": note}

    def follow_schedule(self) -> None:
        if self.was_in_window is None:              # first pass after start-up: remember, don't act
            self.was_in_window = in_window(stream_cfg()["schedule"])
            return
        """Start when the daily window opens and stop when it closes (only on those edges, so a button pressed in
        between wins until the next edge)."""
        now_in = in_window(stream_cfg()["schedule"])
        if now_in and not self.was_in_window:
            log("schedule: window open")
            self.start_stream()
        elif self.was_in_window and not now_in:
            log("schedule: window closed")
            self.stop_stream()
        self.was_in_window = now_in

    def loop(self) -> None:
        self.next_check = time.time() + (0 if self.a.now else self.a.interval * 60)
        log(f"watchdog: reviews every {self.a.interval} min while the stream is live")
        stats()                                     # first start: the stream's counters begin now (run #1)
        if not obs_streaming():                     # off air: nothing from an earlier session should still run
            self.clean_strays()
            for pid in stray_pids(r"main\(\[.monitor.\]\)"):
                kill_tree(pid)
                log(f"ended a leftover monitor ({pid})")
        live, live_at, keeper, kept_at = False, 0.0, ClientKeeper(), 0.0
        while True:
            try:
                self.follow_schedule()
                self.handle_replies()                       # your answers are handled even off-stream
                if time.time() - live_at > 30:
                    live, live_at = bool(obs_streaming()) or self.a.always, time.time()
                    if live and not port_open(8730):
                        self.ensure_monitor()               # live but the overlay's server is gone: bring it back
                if (live or self.a.keep_client) and not self.a.no_client and time.time() - kept_at > 15:
                    kept_at = time.time()
                    # "live" can be 30 s old: ask OBS now, or the client comes back right after the stream ends
                    # (20261121: the stream ended at 20:00:45 and Factorio was started again at 20:00:46)
                    if self.a.keep_client or obs_streaming():
                        keeper.tick(self.runs)              # reconnect the game client after a server restart
                    else:
                        live = False
                if not live:                                # automation only reviews while the stream is live:
                    self.next_check = max(self.next_check, time.time() + self.a.interval * 60)  # first one an hour in
                elif time.time() >= self.next_check:
                    v = self.review()
                    mins = self.a.unsure_interval if v == "unsure" else self.a.interval
                    self.next_check = time.time() + mins * 60
                    write_status(next_check=datetime.fromtimestamp(self.next_check, timezone.utc)
                                 .isoformat(timespec="seconds"))
            except Exception as exc:  # noqa: BLE001 - one bad pass must not end the watch
                log("watchdog error:", repr(exc)[:300])
            time.sleep(5)


# ------------------------------------------------------------------ phone API

def token() -> str:
    if not TOKEN_FILE.exists():
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(pysecrets.token_urlsafe(32), encoding="utf-8")
    return TOKEN_FILE.read_text(encoding="utf-8").strip()


def status_body(runs: Runs) -> dict:
    s = _json(STATUS) or {}
    d = runs.latest()
    live = (_json(d / "bot_live.json") or {}) if d else {}
    meta = (_json(d / "meta.json") or {}) if d else {}
    total = len(((meta.get("task_config") or {}).get("score") or {}).get("milestones", [])) or 1
    gm = live.get("game_minute") or 0
    return {"run_id": d.name if d else None, "scene": obs_scene() or "unknown",
            "game_time": f"{int(gm // 60)}:{int(gm % 60):02d}:{int(gm * 60 % 60):02d}",
            "rocket_pct": round(100 * len(live.get("milestones") or {}) / total, 1),
            "playing": bool(d and _running(d)), "streaming": bool(obs_streaming()), "last_check": s.get("last_check"),
            "last_verdict": s.get("last_verdict", "ok"), "next_check": s.get("next_check"), "note": s.get("note", "")}


def make_handler(runs: Runs, tok: str, wd: "Watchdog | None" = None):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, obj) -> None:
            body = json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authed(self) -> bool:
            got = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
            if hmac.compare_digest(got.encode(), tok.encode()):
                return True
            self._send(401, {"error": "bad or missing token"})
            return False

        def do_GET(self):  # noqa: N802
            if not self._authed():
                return
            path, _, q = self.path.partition("?")
            if path == "/api/stream" and wd:
                return self._send(200, wd.stream_state())
            if path == "/api/status":
                try:
                    return self._send(200, status_body(runs))
                except Exception as exc:  # noqa: BLE001
                    return self._send(500, {"error": str(exc)[:200]})
            if path == "/api/alerts":
                which = "all" if "status=all" in q else "open"
                al = load_alerts()
                return self._send(200, {"alerts": al if which == "all" else [a for a in al if a["status"] == "open"]})
            m = re.fullmatch(r"/api/alerts/([0-9a-f-]{10,40})", path)
            if m and (a := _json(alert_path(m.group(1)))):
                return self._send(200, a)
            self._send(404, {"error": "not found"})

        def _stream(self, what: str, body: dict):
            if what == "start":
                return self._send(200, wd.start_stream(float(body.get("countdown_min", 2)), bool(body.get("reset"))))
            if what == "reset":                     # the next start begins a new game with fresh counters
                stream_cfg(reset_next=bool(body.get("armed", True)))
                return self._send(200, wd.stream_state(note="the next start resets the counters and starts a new game"
                                                       if body.get("armed", True) else "reset cancelled"))
            if what == "stop":
                return self._send(200, wd.stop_stream())
            if what == "schedule":
                sched = dict(stream_cfg()["schedule"])
                for k in ("start", "end"):
                    if k in body:
                        if not HHMM.match(str(body[k])):
                            return self._send(400, {"error": f"{k} is HH:MM (24 h)"})
                        sched[k] = body[k]
                if "enabled" in body:
                    sched["enabled"] = bool(body["enabled"])
                if sched["start"] == sched["end"]:
                    return self._send(400, {"error": "start and end must differ"})
                stream_cfg(schedule=sched)
                wd.was_in_window = in_window(sched)    # a new window applies from its next edge, not right now
                log(f"schedule: {sched}")
                return self._send(200, wd.stream_state(note="schedule saved"))
            return self._send(404, {"error": "stream/start, stream/stop or stream/schedule"})

        def do_POST(self):  # noqa: N802
            if not self._authed():
                return
            path = self.path.partition("?")[0]
            try:
                n = min(int(self.headers.get("Content-Length") or 0), 8192)
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._send(400, {"error": "body must be JSON"})
            if path.startswith("/api/stream/") and wd:
                try:
                    return self._stream(path.rsplit("/", 1)[-1], body)
                except Exception as exc:  # noqa: BLE001 - answer with the error instead of dropping the connection
                    log(f"phone request {path} failed: {exc!r}")
                    return self._send(500, {"error": f"the PC hit an error: {exc!r}"[:300]})
            m = re.fullmatch(r"/api/alerts/([0-9a-f-]{10,40})/reply", path)
            if not m:
                return self._send(404, {"error": "not found"})
            decision, text = body.get("decision"), str(body.get("text") or "")[:4000]
            if decision not in ("fix", "run"):
                return self._send(400, {"error": "decision is fix or run"})
            with LOCK:
                a = _json(alert_path(m.group(1)))
                if not a:
                    return self._send(404, {"error": "no such alert"})
                if a["status"] != "open":
                    return self._send(409, {"error": f"already {a['status']}"})
                a.update(status="answered", reply={"decision": decision, "text": text, "at": now_iso()})
                save_alert(a)
            log(f"reply to {a['id']}: {decision} {text[:80]!r}")
            self._send(200, a)
    return H


def serve_api(port: int, wd: "Watchdog | None" = None) -> None:
    srv = ThreadingHTTPServer(("0.0.0.0", port), make_handler(Runs(RUNS), token(), wd))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"phone API on port {port} (LAN and Tailscale; token in {TOKEN_FILE})")


def main() -> None:
    p = argparse.ArgumentParser(description="Hourly Claude review of the live run, with phone alerts.")
    p.add_argument("--interval", type=float, default=60, help="minutes between reviews (60)")
    p.add_argument("--unsure-interval", type=float, default=20, help="minutes to the next review after 'unsure'")
    p.add_argument("--model", help="model for claude -p (default: the CLI's default)")
    p.add_argument("--port", type=int, default=8740)
    p.add_argument("--now", action="store_true", help="first review right away instead of after one interval")
    p.add_argument("--token", action="store_true", help="print the phone token and exit")
    p.add_argument("--packet", action="store_true", help="print the next review packet and exit")
    p.add_argument("--check-now", action="store_true", help="one review now, print the verdict, never alert")
    p.add_argument("--always", action="store_true", help="review even while the stream is off")
    p.add_argument("--keep-client", action="store_true",
                   help="keep the Factorio client connected even while the stream is off (default: only while live)")
    p.add_argument("--no-client", action="store_true", help="never start or restart the Factorio client")
    p.add_argument("--no-game", action="store_true",
                   help="don't start/stop the bot's run with the stream (also: \"game_with_stream\": false in "
                        "runs/watch/stream.json)")
    p.add_argument("--reset-counters", action="store_true", help="the stream starts over: run #1, 0 upgrades")
    p.add_argument("--reset-next", action="store_true",
                   help="the next stream start resets the counters and starts a new game (new random map)")
    p.add_argument("--cancel-reset", action="store_true", help="undo --reset-next")
    p.add_argument("--vet-seeds", type=int, metavar="N",
                   help="off-stream: boot random maps until N good ones are in the seed pool (about a minute each)")
    a = p.parse_args()
    if a.reset_next or a.cancel_reset:
        stream_cfg(reset_next=bool(a.reset_next))
        print("the next stream start resets the counters (run #1, 0 upgrades) and starts a new game"
              if a.reset_next else "reset cancelled: the next start carries on the current game")
        return
    if a.vet_seeds:
        if Runs(RUNS).latest() is not None and _running(Runs(RUNS).latest()):
            sys.exit("a run is playing: vet seeds off-stream (it uses the same game slot)")
        vet_seeds(a.vet_seeds)
        return
    if a.reset_counters:
        print(stats(since=datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"), upgrades=0, upgrading=False))
        return
    if a.token:
        print(token())
        return
    w = Watchdog(a)
    if a.packet or a.check_now:
        d = w.runs.latest()
        if d is None:
            sys.exit("no runs yet")
        if a.packet:
            print(json.dumps(packet(d, a.interval * 60, None), indent=2, default=str))
            return
        w.playing_run = lambda: d          # review the newest run even if it has finished
        print("verdict:", w.review(alert=False))
        return
    serve_api(a.port, w)
    w.loop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
