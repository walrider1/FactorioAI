"""The live monitor: a read-only page of what every run on this machine is doing.

    arena monitor                 -> http://127.0.0.1:8730   (every run under runs/)
    arena local-run ... --monitor -> the same page, started next to the run (on by default)
    http://127.0.0.1:8730/overlay -> the stream overlay for OBS (follows the run that is playing; ?run=ID pins one)
    http://127.0.0.1:8730/screen  -> full-screen stream cards (?mode=upgrading|bugs|starting|between|brb|ending)

It only reads files the server and the bot already write into each run's folder:

    meta.json, result.json    the server: task, map, final score
    actions.jsonl             the server: every API call with its result, budgets and live score
    bot_live.json             the brain bot: current goal, activity, strategist, base (bots/brain/telemetry.py)
    bot_events.jsonl          the brain bot: strategist calls, goals, milestones

Organizer-only: it binds to 127.0.0.1 and serves nothing a team sends. Other bots show their API calls,
budgets and score; the brain bot also shows its thinking.

Saves (brain bot on the factorio backend): the Saves tab lists runs/<id>/saves and can ask a running bot to
save, or to save and stop (it writes control.json; the bot takes it between goals), and can carry a stopped run
on from any save (it starts `arena local-run --resume <id>:<save>` with the flags in the run's launch.json).
Those requests need the X-Arena-Monitor header and a localhost Host, so another web page cannot send them.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PAGE = Path(__file__).with_name("monitor.html")
OVERLAY = Path(__file__).with_name("overlay.html")      # the stream overlay (an OBS browser source)
SCREENS = Path(__file__).with_name("screens.html")      # full-screen stream cards: /screen?mode=upgrading|bugs|...
RUN_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
TAIL_BYTES = 400_000


def _json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _tail_lines(path: Path, nbytes: int = TAIL_BYTES) -> list[dict]:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - nbytes))
            chunk = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = chunk.splitlines()
    if size > nbytes and lines:
        lines = lines[1:]                     # the first line was cut in half
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def _head_scan(path: Path, max_lines: int = 40) -> dict | None:
    """The run's first scan (ore patches and water), from the start of the action log."""
    try:
        with open(path, encoding="utf-8") as fh:
            for _ in range(max_lines):
                line = fh.readline()
                if not line:
                    break
                row = json.loads(line)
                if row.get("action", {}).get("type") == "scan" and row.get("ok"):
                    return row.get("data")
    except (OSError, ValueError):
        pass
    return None


def _short_action(a: dict) -> str:
    t = a.get("type", "?")
    if t == "batch":
        ops = a.get("ops") or []
        kinds = sorted({o.get("type") for o in ops})
        return f"{len(ops)} ops: {', '.join(kinds)}"
    keep = {k: v for k, v in a.items() if k != "type" and v not in (None, False, [], "full", 2000)}
    for k in ("x", "y", "x1", "y1", "x2", "y2"):
        if isinstance(keep.get(k), float):
            keep[k] = round(keep[k], 1)
    return " ".join(f"{k}={v}" for k, v in keep.items())[:120]


# The road to the rocket (bots/brain/roadmap.py), in order, and the task milestone that means the same thing: a run
# from before the bot stamped its stages borrows that milestone's time. "oil" is left out on purpose: it fires when
# crude is first pumped, long before the stage (pumpjacks and a refinery) is done.
STAGE_ORDER = ["Power and labs", "Red science automated", "Green science automated", "Workshop floor and electric mines",
               "Oil: pumpjacks and a refinery", "Blue science automated", "Purple science automated",
               "Yellow science automated", "Rocket silo researched", "Silo built", "Rocket parts", "Rocket launched"]
STAGE_MILESTONE = {"Power and labs": "power-on", "Red science automated": "red-auto",
                   "Green science automated": "green-auto", "Blue science automated": "blue-auto",
                   "Purple science automated": "purple-auto", "Yellow science automated": "yellow-auto",
                   "Rocket silo researched": "silo", "Rocket launched": "rocket"}


def stage_splits(live: dict) -> dict[str, float]:
    """The game minute each roadmap stage was first cleared: the bot's own stamp, else the matching milestone."""
    out: dict[str, float] = {}
    for name, m in (live.get("stage_minutes") or {}).items():
        if isinstance(m, (int, float)):
            out[name] = round(float(m), 2)
    for s in (live.get("roadmap") or {}).get("stages") or []:
        if s.get("done") and isinstance(s.get("done_minute"), (int, float)):
            out.setdefault(s.get("name"), round(float(s["done_minute"]), 2))
    ms = live.get("milestones") or {}
    for name, mid in STAGE_MILESTONE.items():
        if name not in out and isinstance(ms.get(mid), (int, float)):
            out[name] = round(ms[mid] / 3600, 2)
    return out


def personal_best(runs: list[tuple[str, dict]]) -> dict | None:
    """The run to race (it got furthest down the road; the earlier one there on a tie) and the best time any run
    ever cleared each stage in (a "gold" split beats it). `runs` is [(run id, its splits)], the live run left out."""
    best: dict[str, float] = {}
    pb = None
    for rid, sp in runs:
        sp = {k: v for k, v in sp.items() if k in STAGE_ORDER}
        if not sp:
            continue
        for k, v in sp.items():
            best[k] = min(best.get(k, v), v)
        far = max(STAGE_ORDER.index(k) for k in sp)
        key = (far, -sp[STAGE_ORDER[far]])
        if pb is None or key > pb[0]:
            pb = (key, rid, sp)
    if pb is None:
        return None
    return {"run": pb[1], "splits": pb[2], "best": best, "runs": sum(1 for _, sp in runs if sp)}


SAVE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ROOT = Path(__file__).resolve().parent.parent


def saves(d: Path) -> list[dict]:
    """A run's saves, newest first: the game, the session and the bot's state under one name."""
    folder = d / "saves"
    out = []
    for st in sorted(folder.glob("*.session.json"), key=lambda p: p.stat().st_mtime, reverse=True) \
        if folder.exists() else []:
        name = st.name[:-len(".session.json")]
        info, bot = _json(st) or {}, _json(folder / f"{name}.bot.json") or {}
        out.append({"name": name, "minute": round((info.get("tick") or 0) / 3600, 1), "saved_at": info.get("saved_at"),
                    "kind": bot.get("kind"), "milestones": len(bot.get("milestones") or []),
                    "sites": bot.get("sites"), "complete": (folder / f"{name}.zip").exists()
                    and (folder / f"{name}.bot.pkl").exists()})
    return out


def _running(d: Path) -> bool:
    live = _json(d / "bot_live.json") or {}
    return not (d / "result.json").exists() and live.get("status") in ("playing", "starting") \
        and time.time() - live.get("updated", 0) < 60


def request_control(d: Path, cmd: str) -> dict:
    if cmd not in ("save", "stop"):
        return {"ok": False, "error": "cmd is save or stop"}
    if not _running(d):
        return {"ok": False, "error": "the run is not playing"}
    (d / "control.json").write_text(json.dumps({"cmd": cmd, "at": time.time()}), encoding="utf-8")
    return {"ok": True, "note": "the bot saves at its next safe point, between goals"}


def request_resume(d: Path, save: str) -> dict:
    """Start `arena local-run --resume` for a stopped run, in its own process (it outlives this page)."""
    if _running(d):
        return {"ok": False, "error": "the run is still playing: stop it first"}
    if not SAVE_NAME.match(save or "") or not any(s["name"] == save and s["complete"] for s in saves(d)):
        return {"ok": False, "error": f"no complete save {save!r}"}
    launch = _json(d / "launch.json") or {}
    argv = [a for a in launch.get("argv") or [] if isinstance(a, str)]
    if "local-run" not in argv:
        return {"ok": False, "error": "this run has no launch.json (start it with arena local-run)"}
    log_path = d / f"resume-{time.strftime('%Y%m%d-%H%M%S')}.log"
    cmd = [sys.executable, "-c", "import sys; from arena.cli import main; main(sys.argv[1:])",
           *argv, "--resume", f"{d.name}:{save}", "--monitor", "0"]
    # no console window (DETACHED_PROCESS made every console child, like docker, open its own window)
    flags = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000} \
        if os.name == "nt" else {"start_new_session": True}
    with open(log_path, "ab") as fh:
        subprocess.Popen(cmd, cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **flags)
    return {"ok": True, "log": log_path.name, "note": "the game loads in about a minute; this run's page picks it up"}


def stream_schedule(path: Path) -> dict | None:
    """The stream's daily hours (set from the phone app, kept by stream/watchdog.py) for viewers, with this PC's
    time zone: {"start": "10:00", "end": "19:00", "tz": "CET"}. None when there is no schedule."""
    sched = ((_json(path) or {}).get("schedule") or {})
    if not sched.get("enabled") or not sched.get("start") or not sched.get("end"):
        return None
    off = datetime.now().astimezone().utcoffset()
    hours = int(off.total_seconds() // 3600) if off is not None else 0
    tz = {1: "CET", 2: "CEST", 0: "UTC"}.get(hours, f"UTC{hours:+d}")
    return {"start": sched["start"], "end": sched["end"], "tz": tz}


class Runs:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._cache: dict[tuple[Path, str], tuple[float, object]] = {}

    def _cached(self, path: Path, kind: str, read):
        """A file's parsed form, read again only when the file changes (the overlay asks every second)."""
        try:
            mt = path.stat().st_mtime
        except OSError:
            return None
        hit = self._cache.get((path, kind))
        if hit and hit[0] == mt:
            return hit[1]
        val = read(path)
        self._cache[(path, kind)] = (mt, val)
        return val

    def personal_best(self, d: Path, same_map: bool = False) -> dict | None:
        """The best earlier run of the same task (on the same map, if asked) to race, as splits."""
        meta = _json(d / "meta.json") or {}
        others, maps = [], {}
        for o in self.dirs():
            if o == d:
                continue
            om = self._cached(o / "meta.json", "meta", lambda p: {k: (_json(p) or {}).get(k) for k in ("task", "map")}) or {}
            if om.get("task") != meta.get("task") or (same_map and om.get("map") != meta.get("map")):
                continue
            sp = self._cached(o / "bot_live.json", "splits", lambda p: stage_splits(_json(p) or {}))
            if sp:
                others.append((o.name, sp))
                maps[o.name] = om.get("map")
        pb = personal_best(others)
        if pb:
            pb["map"], pb["same_map"] = maps.get(pb["run"]), same_map
        return pb

    def dirs(self) -> list[Path]:
        found = []
        for base in (self.root, *[p for p in self.root.glob("*") if p.is_dir() and not RUN_ID.match(p.name)]):
            for d in base.glob("*"):
                if d.is_dir() and RUN_ID.match(d.name) and (d / "meta.json").exists():
                    found.append(d)
        return sorted(found, key=lambda d: d.name, reverse=True)

    def find(self, run_id: str) -> Path | None:
        if not RUN_ID.match(run_id):
            return None
        return next((d for d in self.dirs() if d.name == run_id), None)

    def listing(self, limit: int = 40) -> list[dict]:
        out = []
        for d in self.dirs()[:limit]:
            meta = _json(d / "meta.json") or {}
            result = _json(d / "result.json")
            live = d / "bot_live.json"
            out.append({"id": d.name, "task": meta.get("task"), "map": meta.get("map"),
                        "started": meta.get("started_at"),
                        "status": (result or {}).get("status") or "active",
                        "score": (result or {}).get("score"), "brain": live.exists()})
        return out

    def latest(self) -> Path | None:
        """The run that is playing now, else the newest one: the stream overlay follows it on its own."""
        dirs = self.dirs()
        return next((d for d in dirs if _running(d)), dirs[0] if dirs else None)

    def overlay(self, d: Path, same_map: bool = False) -> dict:
        """What the stream overlay shows, light enough to ask for every second: the bot's live state, the task's
        milestones, the score (from the last few lines of the action log only), this run's stage splits and the
        personal best to race."""
        meta = _json(d / "meta.json") or {}
        task = meta.get("task_config") or {}
        tail = _tail_lines(d / "actions.jsonl", 6000)
        last = {"score": next((r.get("score") for r in reversed(tail) if r.get("score") is not None), None)}
        live = _json(d / "bot_live.json") or {}
        result = _json(d / "result.json")
        stale = live.get("status") in ("playing", "starting") and time.time() - live.get("updated", 0) > 30
        keep = ("status", "game_minute", "activity", "current_goal", "queue", "stage", "research", "milestones",
                "rates", "watchdog", "overseer", "strategist", "strategist_name", "doctrine", "assessor", "fixer",
                "events", "results", "alerts", "updated", "minutes_left")
        out = {k: live.get(k) for k in keep}
        rm = live.get("roadmap") or {}
        out["roadmap"] = {k: rm.get(k) for k in ("stages", "power", "science", "plates", "research")} if rm else None
        out["sites"] = len(live.get("base") or [])
        sup = live.get("supervisor") or {}
        out["supervisor_notes"] = (sup.get("notes") or [])[-4:]
        return {"id": d.name, "title": task.get("title"), "map": meta.get("map"),
                "milestones": [{"id": m["id"], "name": m["name"], "points": m["points"]}
                               for m in (task.get("score") or {}).get("milestones", [])],
                "score": (result or {}).get("score", last.get("score")), "finished": bool(result),
                "stale": stale, "live": out, "now": time.time(),
                "splits": stage_splits(live), "pb": self.personal_best(d, same_map),
                "stream": self.stream_counters(d)}

    def stream_counters(self, d: Path | None = None) -> dict:
        """The show's counters: which run of the stream this is, and how many self-upgrades so far.
        runs/watch/stats.json (kept by stream/watchdog.py): {"since": "YYYYMMDD-HHMMSS" (UTC, like run ids),
        "upgrades": n, "upgrading": bool}. Without it, every run of the task counts."""
        st = _json(self.root / "watch" / "stats.json") or {}
        d = d or self.latest()
        out = {"upgrades": int(st.get("upgrades") or 0), "upgrading": bool(st.get("upgrading")), "run_no": None,
               "schedule": stream_schedule(self.root / "watch" / "stream.json")}
        if d is None:
            return out
        task = (_json(d / "meta.json") or {}).get("task")
        since = st.get("since") or ""
        names = sorted(o.name for o in self.dirs() if o.name >= since and self._cached(
            o / "meta.json", "task", lambda p: (_json(p) or {}).get("task")) == task)
        if d.name not in names:                 # the run that was playing when the counters were reset
            names.insert(0, d.name)
        out["run_no"] = names.index(d.name) + 1
        return out

    def detail(self, d: Path) -> dict:
        meta = _json(d / "meta.json") or {}
        task = meta.get("task_config") or {}
        rows = _tail_lines(d / "actions.jsonl")
        actions = [{"seq": r.get("seq"), "t": r.get("t"), "tick": r.get("tick_after"),
                    "type": r.get("action", {}).get("type"), "args": _short_action(r.get("action", {})),
                    "ok": r.get("ok"), "code": r.get("code"), "charged": r.get("charged")} for r in rows[-80:]]
        last = rows[-1] if rows else {}
        stats = next((r.get("data") for r in reversed(rows) if r.get("action", {}).get("type") == "stats"
                      and r.get("ok")), None)
        pos = None
        for r in reversed(rows):
            data = r.get("data") or {}
            if isinstance(data.get("position"), dict):
                pos = [data["position"].get("x"), data["position"].get("y")]
                break
        live = _json(d / "bot_live.json")
        if live and live.get("status") in ("playing", "starting") and time.time() - live.get("updated", 0) > 30:
            live["stale"] = True                  # the bot stopped writing (crashed or killed)
        return {
            "id": d.name, "task": {"id": task.get("id"), "title": task.get("title"), "clock": task.get("clock"),
                                   "ticks": (task.get("budgets") or {}).get("ticks"),
                                   "milestones": [{"id": m["id"], "name": m["name"], "points": m["points"]}
                                                  for m in (task.get("score") or {}).get("milestones", [])]},
            "map": meta.get("map"), "seed": meta.get("seed"), "started": meta.get("started_at"),
            "spectator_port": (meta.get("backend") or {}).get("spectator_port_udp"),
            "result": _json(d / "result.json"),
            "budgets": last.get("budgets"), "score": last.get("score"), "last_tick": last.get("tick_after"),
            "last_call": last.get("t"), "calls": last.get("seq"),
            "actions": actions[::-1], "stats": stats, "position": pos,
            "scan": _head_scan(d / "actions.jsonl"), "live": live,
            "bot": _json(d / "bot.json"),          # written by local-run when the run ends (cost, calls...)
            "saves": saves(d), "control": (_json(d / "control.json") or {}).get("cmd"),
            "resumable": (d / "launch.json").exists(),
        }


def _handler(runs: Runs):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):            # quiet: the run's own output matters more
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            path = self.path.split("?")[0]
            try:
                if path in ("/", "/index.html"):
                    return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
                if path in ("/overlay", "/overlay.html"):
                    return self._send(200, OVERLAY.read_bytes(), "text/html; charset=utf-8")
                if path in ("/screen", "/screens.html"):
                    return self._send(200, SCREENS.read_bytes(), "text/html; charset=utf-8")
                if path.startswith("/api/overlay/"):
                    rid = path.rsplit("/", 1)[-1]
                    d = runs.latest() if rid == "latest" else runs.find(rid)
                    if d is None:
                        return self._send(404, b'{"error":"no such run"}', "application/json")
                    same_map = "pb=map" in (self.path.split("?", 1)[1:] or [""])[0].split("&")
                    return self._send(200, json.dumps(runs.overlay(d, same_map), default=str).encode(), "application/json")
                if path == "/api/stream":
                    return self._send(200, json.dumps(runs.stream_counters()).encode(), "application/json")
                if path == "/api/runs":
                    return self._send(200, json.dumps(runs.listing()).encode(), "application/json")
                if path.startswith("/api/runs/"):
                    d = runs.find(path.rsplit("/", 1)[-1])
                    if d is None:
                        return self._send(404, b'{"error":"no such run"}', "application/json")
                    return self._send(200, json.dumps(runs.detail(d), default=str).encode(), "application/json")
            except Exception as exc:  # noqa: BLE001 - a bad file never takes the page down
                return self._send(500, json.dumps({"error": str(exc)[:200]}).encode(), "application/json")
            self._send(404, b"not found", "text/plain")

        def do_POST(self):  # noqa: N802
            # only this page may ask: a custom header (other sites cannot send it without a CORS preflight we
            # never answer) and a localhost Host (no DNS rebinding)
            host = (self.headers.get("Host") or "").split(":")[0]
            if self.headers.get("X-Arena-Monitor") != "1" or host not in ("127.0.0.1", "localhost"):
                return self._send(403, b'{"error":"forbidden"}', "application/json")
            m = re.match(r"^/api/runs/([^/]+)/(control|resume)$", self.path.split("?")[0])
            d = runs.find(m.group(1)) if m else None
            if d is None:
                return self._send(404, b'{"error":"no such run"}', "application/json")
            try:
                n = min(int(self.headers.get("Content-Length") or 0), 4096)
                body = json.loads(self.rfile.read(n) or b"{}")
                out = request_control(d, body.get("cmd")) if m.group(2) == "control" \
                    else request_resume(d, body.get("save"))
            except Exception as exc:  # noqa: BLE001
                out = {"ok": False, "error": str(exc)[:200]}
            self._send(200 if out.get("ok") else 409, json.dumps(out).encode(), "application/json")
    return H


def serve_monitor(runs_dir: Path, port: int = 8730) -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", port), _handler(Runs(runs_dir)))
    print(f"monitor: http://127.0.0.1:{port}  (runs in {runs_dir}; Ctrl+C to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def start_monitor(runs_dir: Path, port: int = 8730) -> str | None:
    """In a background thread, for local-run. Returns the URL, or None if the port is taken."""
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), _handler(Runs(runs_dir)))
    except OSError:
        print(f"monitor: port {port} is busy (is `arena monitor` already running? it shows this run too)",
              flush=True)
        return None
    threading.Thread(target=srv.serve_forever, name="monitor", daemon=True).start()
    return f"http://127.0.0.1:{port}/"
