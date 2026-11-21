"""Real Factorio backend: one fresh headless 2.1.24 container per run.

Security model
- RCON is published on 127.0.0.1 only, with a random password per container.
- Only the organizer process talks RCON. Team input arrives here already
  validated (numbers + whitelisted names) and is passed to Lua as JSON *data*
  inside a long-bracket string; ``_lua_string`` refuses anything that could
  close that string.
- Lua errors and raw server output never reach teams: they become BackendError,
  which voids the run and is logged on the organizer side only.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import subprocess
import threading
import time
from pathlib import Path

from .. import rules
from ..config import FactorioCfg, Score, Task
from ..scorer import live_points, milestone_breakdown
from ..models import Outcome
from .base import Backend, BackendError
from .rcon import RconClient, RconError

log = logging.getLogger("arena.factorio")

REMOTE_FUNCS = {"ping", "setup", "tick", "act", "run_ticks", "score", "stats", "start_clock", "stop_clock",
                "milestones", "catalog"}
_SAFE_JSON = re.compile(r"^[\x20-\x7e]*$")
_DICT_FIELDS = {"inventory", "fuel", "source", "input", "output", "contents", "resources",
                "resource_summary", "picked_up", "mined", "crafted", "inventories", "data",
                "items", "fluids", "milestones", "reached", "made", "used"}


def _lua_string(text: str) -> str:
    if not _SAFE_JSON.match(text) or "]==]" in text:
        raise BackendError("refusing to send unsafe payload to Lua")
    return "[==[" + text + "]==]"


def _fix_empty(obj, key: str | None = None):
    """Factorio serialises empty tables as []; turn the ones that are maps back into {}."""
    if isinstance(obj, list):
        if not obj and key in _DICT_FIELDS:
            return {}
        return [_fix_empty(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _fix_empty(v, k) for k, v in obj.items()}
    return obj


_CATALOG_LISTS = {"prerequisites", "unlocks", "unit_ingredients", "ingredients", "products", "categories",
                  "crafting_categories", "resource_categories", "lab_inputs", "entities"}


def _normalise_catalog(data: dict) -> dict:
    """Lua writes an empty table as {}: make the empty lists lists again, and every section a dict."""
    def fix(obj, key=None):
        if isinstance(obj, dict):
            if not obj and key in _CATALOG_LISTS:
                return []
            return {k: fix(v, k) for k, v in obj.items()}
        if isinstance(obj, list):
            return [fix(v) for v in obj]
        return obj
    out = fix(data)
    for section in ("items", "fluids", "recipes", "technologies", "entities", "resources"):
        if isinstance(out.get(section), list):
            out[section] = {}
    return out


class SlotPool:
    """Fixed number of game slots (ports) on this machine."""

    def __init__(self, n: int):
        self._free = list(range(n))
        self._cond = threading.Condition()

    def acquire(self, timeout: float | None = None) -> int:
        with self._cond:
            if not self._cond.wait_for(lambda: self._free, timeout=timeout):
                raise TimeoutError("no free game slot")
            return self._free.pop(0)

    def release(self, slot: int) -> None:
        with self._cond:
            if slot not in self._free:
                self._free.append(slot)
                self._free.sort()
            self._cond.notify()

    @property
    def free(self) -> int:
        with self._cond:
            return len(self._free)


def _item_stats(caller: "_RemoteCaller", items: list[str]) -> dict[str, list[int]]:
    res = caller.call("stats", {"items": list(items)})
    return {k: [int(v[0]), int(v[1])] for k, v in (res.get("items") or {}).items()}


def _milestones(caller: "_RemoteCaller") -> dict[str, int]:
    res = caller.call("milestones")
    return {str(k): int(v) for k, v in (res.get("reached") or {}).items()}


def _score_breakdown(caller: "_RemoteCaller", score: Score) -> dict:
    if score.type == "automation":
        return {"score": live_points(score, _item_stats(caller, sorted(score.points)))}
    if score.type == "milestones":                  # live: points only (the final score adds time bonuses)
        bd = milestone_breakdown(score, _milestones(caller), 0, 0)
        return {"score": bd["score"], "reached": bd["reached"], "complete": bd["complete"]}
    res = caller.call("score", {"type": score.type, "item": score.item})
    return {k: int(v) for k, v in res.items()}


class _RemoteCaller:
    def __init__(self, rcon: RconClient):
        self.rcon = rcon

    def call(self, fn: str, payload: dict | None = None):
        if fn not in REMOTE_FUNCS:
            raise BackendError(f"unknown remote function {fn}")
        if payload is None:
            cmd = f'/sc rcon.print(remote.call("arena","{fn}"))'
        else:
            text = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
            cmd = f'/sc rcon.print(remote.call("arena","{fn}",{_lua_string(text)}))'
        try:
            out = self.rcon.command(cmd)
        except (RconError, OSError) as exc:
            raise BackendError(f"rcon failure: {exc}") from exc
        try:
            res = json.loads(out)
        except json.JSONDecodeError as exc:
            raise BackendError(f"bad reply from game for {fn}: {out[:300]!r}") from exc
        if isinstance(res, dict) and "lua_error" in res:
            raise BackendError(f"lua error in {fn}: {res['lua_error']}")
        return _fix_empty(res)

    def tick(self) -> int:
        return int(self.call("tick")["tick"])

    def wait_tick(self, target: int, speed: float) -> None:
        start = time.monotonic()
        last = -1
        stall_since = time.monotonic()
        while True:
            t = self.tick()
            if t >= target:
                return
            if t != last:
                last, stall_since = t, time.monotonic()
            elif time.monotonic() - stall_since > 30:
                raise BackendError(f"game stopped ticking at {t} (target {target})")
            time.sleep(0.02 if target - t < 600 else 0.1)
            if time.monotonic() - start > 3600:
                raise BackendError("tick wait exceeded one hour")



def _refresh_script(save_zip: Path, scenario: Path) -> list[str]:
    """Put the scenario's current scripts (control.lua and any other .lua) into a save, in place of the ones it was
    saved with. A save carries the script it ran, so without this a resumed run keeps an old server (no belt
    merge guard, no lane contents). Storage stays as saved; the scenario creates new storage fields lazily.
    Returns the files replaced."""
    import zipfile
    fresh = {f.name: f.read_bytes() for f in Path(scenario).glob("*.lua")}
    if not fresh:
        return []
    tmp = Path(str(save_zip) + ".tmp")
    done: list[str] = []
    with zipfile.ZipFile(save_zip) as src, zipfile.ZipFile(tmp, "w") as dst:
        for info in src.infolist():
            name = info.filename.rsplit("/", 1)[-1]
            if "/" in info.filename and info.filename.count("/") == 1 and name in fresh:
                dst.writestr(info, fresh[name])
                done.append(name)
            else:
                dst.writestr(info, src.read(info))
    tmp.replace(save_zip)
    return done

class FactorioBackend(Backend):
    def __init__(self, cfg: FactorioCfg, root: Path, pool: SlotPool, slot: int):
        self.cfg, self.root, self.pool, self.slot = cfg, root, pool, slot
        self.rcon_port = cfg.rcon_base_port + slot
        self.game_port = cfg.game_base_port + slot
        self.password = secrets.token_hex(16)
        self.container = f"{cfg.container_prefix}-slot{slot}-{secrets.token_hex(3)}"
        self._rcon: RconClient | None = None
        self._caller: _RemoteCaller | None = None
        self._closed = False
        self._scorer: _ScorerView | None = None

    # -------------------------------------------------------------- docker
    # The server writes saves into <write data>/saves, and that folder does not exist in the image: asked to save,
    # the server quit with "cannot make canonical path ... /opt/factorio/saves" and took the run with it (run
    # 20261119-193819 died at its first autosave). Every container gets a writable folder of its own there.
    SAVES_IN_CONTAINER = "/opt/factorio/saves"

    def _saves_dir(self) -> Path:
        d = (self.root / "runs" / ".server-saves" / self.container).resolve()
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _docker_run(self, save_file: Path | None = None) -> None:
        """A fresh game from the scenario, or (save_file) the game a run saved earlier."""
        import shutil
        scenario = (self.root / self.cfg.scenario_dir).resolve()
        config = (self.root / self.cfg.server_config_dir).resolve()
        saves = self._saves_dir()
        start = ["--start-server-load-scenario", "arena"]
        mounts = ["--mount", f"type=bind,source={saves},target={self.SAVES_IN_CONTAINER}"]
        if save_file is not None:
            # the server loads a copy in its own saves folder: nothing it writes can touch the run's saves
            copy = saves / Path(save_file).name
            shutil.copy2(Path(save_file).resolve(), copy)
            _refresh_script(copy, scenario)
            start = ["--start-server", f"{self.SAVES_IN_CONTAINER}/{Path(save_file).name}"]
        cmd = [
            "docker", "run", "-d", "--rm", "--name", self.container,
            "--label", f"arena={self.cfg.container_prefix}",
            "-p", f"127.0.0.1:{self.rcon_port}:27015/tcp",
            "-p", f"{self.game_port}:34197/udp",
            "--mount", f"type=bind,source={scenario},target=/factorio/scenarios/arena,readonly",
            "--mount", f"type=bind,source={config},target=/arena-config,readonly",
            *mounts,
            "--entrypoint", "/opt/factorio/bin/x64/factorio",
            self.cfg.image,
            *start,
            "--port", "34197",
            "--rcon-port", "27015",
            "--rcon-password", self.password,
            "--server-settings", "/arena-config/server-settings.json",
            "--server-whitelist", "/arena-config/server-whitelist.json",
            "--use-server-whitelist",
            # base game only: the image enables the Space Age DLC mods unless a mod list says otherwise
            "--mod-directory", "/arena-config/mods",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if res.returncode != 0:
            raise BackendError(f"docker run failed: {res.stderr.strip()[:500]}")

    def _connect(self) -> None:
        deadline = time.monotonic() + self.cfg.boot_timeout_seconds
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            try:
                rcon = RconClient("127.0.0.1", self.rcon_port, self.password, timeout=30)
                rcon.connect()
                # First Lua command on a fresh map may only print an achievements warning.
                for _ in range(3):
                    out = rcon.command('/sc rcon.print(remote.call("arena","ping"))')
                    if '"pong"' in out:
                        self._rcon, self._caller = rcon, _RemoteCaller(rcon)
                        return
                    time.sleep(0.5)
                rcon.close()
            except (OSError, RconError) as exc:
                last_err = exc
            time.sleep(1.0)
        raise BackendError(f"factorio did not come up in time: {last_err}; logs: {self.logs(40)}")

    def logs(self, lines: int = 100) -> str:
        res = subprocess.run(["docker", "logs", "--tail", str(lines), self.container],
                             capture_output=True, text=True, timeout=30)
        return (res.stdout + res.stderr)[-4000:]

    # ------------------------------------------------------------ backend
    def setup(self, task: Task, seed: int) -> dict:
        self.task = task
        self._docker_run()
        self._connect()
        payload = {
            "seed": int(seed),
            "inventory": task.start_inventory,
            "score_item": task.score.item,
            "speed": self.cfg.advance_speed,
        }
        if task.score.type == "delivered":
            payload["delivery"] = list(task.score.delivery_offset)
        if task.assembler_recipes:
            payload["recipes"] = list(task.assembler_recipes)      # unlocked without research
        if task.clear_obstacles:
            payload["clear_obstacles"] = True
        if task.clock == "realtime":
            payload["realtime"] = True
            payload["min_action_gap"] = task.budgets.min_action_gap_ticks
            payload["clock_speed"] = task.clock_speed
        lim = task.limits
        payload.update({"tech": task.tech, "max_observe_radius": lim.observe_radius,
                        "observe_range": lim.observe_range, "scan_radius": lim.scan_radius,
                        "batch_op_gap": lim.batch_op_gap_ticks or task.budgets.min_action_gap_ticks,
                        "chart_radius": lim.chart_radius, "walking": task.walking})
        if task.water_summary:
            payload["water_summary"] = True
        if task.score.type == "milestones":
            payload["milestones"] = [m.lua() for m in task.score.milestones]
        return self.caller.call("setup", payload)

    SAVE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

    def save(self, name: str, dest: Path) -> Path:
        """/server-save, wait until the file stops growing, then copy it out of the container (which is
        removed with the run)."""
        if not self.SAVE_NAME.match(name):
            raise BackendError(f"bad save name {name!r}")
        if self._rcon is None:
            raise BackendError("not connected")
        try:
            self._rcon.command(f"/server-save {name}")
        except (RconError, OSError) as exc:
            raise BackendError(f"rcon failure while saving: {exc}") from exc
        src = self._saves_dir() / f"{name}.zip"
        size, steady = -1, 0
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            now = src.stat().st_size if src.exists() else -1
            steady = steady + 1 if now == size and now > 0 else 0
            size = now
            if steady >= 2:                    # the same size three looks in a row: the save is written
                break
            time.sleep(1.0)
        else:
            raise BackendError(f"the game did not write {name}.zip within 5 minutes")
        import shutil
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / f"{name}.zip"
        shutil.copy2(src, out)
        return out

    def resume(self, task: Task, save_file: Path) -> dict:
        """Load a saved run: its storage comes back with the save, so no setup. The script is the scenario's as it
        is now (_refresh_script), so server fixes reach a resumed run.
        The world is paused at once (it was running when saved); the clock is started again by the session."""
        self.task = task
        self._docker_run(save_file=save_file)
        self._connect()
        self.stop_clock()
        return {"resumed_from": Path(save_file).name, "tick": self.tick()}

    def dump_catalog(self, out: Path) -> dict:
        """Organizer tool: ask the scenario for every prototype and copy catalog.json out of the container."""
        self.caller.call("catalog")
        for src in ("/factorio/script-output/catalog.json", "/opt/factorio/script-output/catalog.json"):
            res = subprocess.run(["docker", "cp", f"{self.container}:{src}", str(out)],
                                 capture_output=True, text=True, timeout=120)
            if res.returncode == 0:
                data = _normalise_catalog(json.loads(out.read_text(encoding="utf-8")))
                out.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
                return data
        raise BackendError(f"could not copy catalog.json out of the container: {res.stderr.strip()[:300]}")

    def start_clock(self, ticks: int, run: bool = True) -> int:
        return int(self.caller.call("start_clock", {"ticks": int(ticks), "run": bool(run)})["tick"])

    def stop_clock(self) -> int:
        return int(self.caller.call("stop_clock")["tick"])

    @property
    def caller(self) -> _RemoteCaller:
        if self._caller is None:
            raise BackendError("not connected")
        return self._caller

    def tick(self) -> int:
        return self.caller.tick()

    def act(self, action, max_ticks: int) -> Outcome:
        payload = action.model_dump(exclude_none=True)
        if action.type == "place":
            payload["x"], payload["y"] = rules.snap(action.entity, action.x, action.y, action.direction)
        if action.type == "batch":
            for op, model in zip(payload["ops"], action.ops):
                if model.type == "place":
                    op["x"], op["y"] = rules.snap(model.entity, model.x, model.y, model.direction)
        payload["max_ticks"] = int(max_ticks)
        res = self.caller.call("act", payload)
        if not isinstance(res, dict) or "ok" not in res:
            raise BackendError(f"bad act reply: {str(res)[:300]}")
        tick = int(res["tick"]) if res.get("tick") is not None else None
        if res["ok"]:
            target = int(res.get("target_tick", 0) or 0)
            if target:
                self.caller.wait_tick(target, self.cfg.advance_speed)
            data = res.get("data") or {}
            return Outcome(ok=True, data=data if isinstance(data, dict) else {}, tick=tick)
        return Outcome(ok=False, code=str(res.get("code", "rejected")),
                       message=str(res.get("message", ""))[:200], tick=tick)

    def run_ticks(self, n: int) -> None:
        if n <= 0:
            return
        res = self.caller.call("run_ticks", {"ticks": int(n)})
        self.caller.wait_tick(int(res["target_tick"]), self.cfg.advance_speed)

    def score(self, score: Score) -> int:
        return int(self.score_breakdown(score)["score"])

    def score_breakdown(self, score: Score) -> dict:
        return _score_breakdown(self.caller, score)

    def item_stats(self, items: list[str]) -> dict[str, list[int]]:
        return _item_stats(self.caller, items)

    def milestones(self) -> dict[str, int]:
        return _milestones(self.caller)

    def scorer(self) -> Backend:
        if self._scorer is None:
            self._scorer = _ScorerView(self)
        return self._scorer

    def info(self) -> dict:
        return {"backend": "factorio", "image": self.cfg.image, "container": self.container,
                "slot": self.slot, "spectator_port_udp": self.game_port}

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._scorer:
                self._scorer.close()
            if self._rcon:
                self._rcon.close()
        finally:
            subprocess.run(["docker", "rm", "-f", self.container], capture_output=True, timeout=60)
            self.pool.release(self.slot)
            import shutil
            shutil.rmtree(self.root / "runs" / ".server-saves" / self.container, ignore_errors=True)


class _ScorerView(Backend):
    """Separate RCON connection used only by the scorer, independent of the team's session."""

    def __init__(self, parent: FactorioBackend):
        self.parent = parent
        self.rcon = RconClient("127.0.0.1", parent.rcon_port, parent.password)
        self.rcon.connect()
        self.caller = _RemoteCaller(self.rcon)

    def setup(self, task, seed):  # pragma: no cover - never used
        raise NotImplementedError

    def tick(self) -> int:
        return self.caller.tick()

    def act(self, action, max_ticks):  # pragma: no cover - scorer never acts
        raise BackendError("scorer cannot act")

    def run_ticks(self, n: int) -> None:
        if n > 0:
            res = self.caller.call("run_ticks", {"ticks": int(n)})
            self.caller.wait_tick(int(res["target_tick"]), self.parent.cfg.advance_speed)

    def score(self, score: Score) -> int:
        return int(self.score_breakdown(score)["score"])

    def score_breakdown(self, score: Score) -> dict:
        return _score_breakdown(self.caller, score)

    def item_stats(self, items: list[str]) -> dict[str, list[int]]:
        return _item_stats(self.caller, items)

    def milestones(self) -> dict[str, int]:
        return _milestones(self.caller)

    def close(self) -> None:
        self.rcon.close()
