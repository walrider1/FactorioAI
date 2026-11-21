"""Factorio AI Arena: Python SDK for teams.

Single file, one dependency (httpx). Copy it into your project.

    from arena_sdk import ArenaClient
    client = ArenaClient("https://arena.example.org", token="fa_...")
    run = client.start_run("level1-iron")          # practice by default
    obs = run.observe(radius=20)
    run.move(10.5, 4.5)
    run.place("burner-mining-drill", 12, 4, "north")
    run.advance(600)
    print(run.end_and_wait())

Every action carries a request_id. If the network drops, the SDK retries with
the SAME request_id, so the server never runs (or charges) the action twice.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx


class ArenaError(Exception):
    def __init__(self, status: int, code: str, message: str, body: dict | None = None):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message, self.body = status, code, message, body or {}


class ArenaClient:
    def __init__(self, base_url: str = "", token: str = "", timeout: float = 120.0,
                 http: httpx.Client | None = None):
        self.http = http or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)
        self.headers = {"Authorization": f"Bearer {token}"}

    def _request(self, method: str, path: str, json: Any = None, retries: int = 3) -> dict:
        delay = 0.5
        for attempt in range(retries + 1):
            try:
                r = self.http.request(method, path, json=json, headers=self.headers)
            except httpx.TransportError:
                if attempt == retries:
                    raise
                time.sleep(delay)
                delay *= 2
                continue
            if r.status_code == 429 and attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            body = r.json() if r.content else {}
            if r.status_code >= 400:
                err = body.get("error", {})
                msg = err.get("message", "")
                if err.get("problems"):                  # which fields failed validation, and why
                    msg += ": " + "; ".join(f"{p.get('field')} {p.get('problem')}" for p in err["problems"][:4])
                    msg += f" (request: {str(json)[:160]})"
                raise ArenaError(r.status_code, err.get("code", "error"), msg, body)
            return body
        raise RuntimeError("unreachable")

    def catalog(self) -> dict:
        """The game's data: items, fluids, recipes, technologies, entities, resources (tech-tree levels)."""
        return self._request("GET", "/v1/catalog")

    def rules(self) -> dict:
        return self._request("GET", "/v1/rules")

    def me(self) -> dict:
        return self._request("GET", "/v1/me")

    def start_run(self, task: str, mode: str = "practice", wait: bool = True, timeout: float = 600) -> "Run":
        state = self._request("POST", "/v1/runs", {"task": task, "mode": mode}, retries=0)
        run = Run(self, state["run_id"], state)
        if wait:
            run.wait_until_active(timeout)
        return run



def _belt_opts(belt: dict) -> dict:
    """The belt options a placement may carry (carries, isolated, allow), None ones left out."""
    bad = set(belt) - {"carries", "isolated", "allow"}
    if bad:
        raise TypeError(f"unknown belt option(s): {sorted(bad)}")
    return {k: v for k, v in belt.items() if v is not None and v is not False}

class Run:
    def __init__(self, client: ArenaClient, run_id: str, state: dict):
        self.client, self.id, self.state = client, run_id, state
        self.budgets: dict = state.get("budgets", {})
        self.last: dict | None = None

    def refresh(self) -> dict:
        self.state = self.client._request("GET", f"/v1/runs/{self.id}")
        self.budgets = self.state.get("budgets", self.budgets)
        return self.state

    def wait_until_active(self, timeout: float = 600) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            st = self.refresh()["status"]
            if st == "active":
                return
            if st in ("void", "finished"):
                raise ArenaError(409, "run_not_active", f"run is {st}: {self.state.get('end_reason')}")
            time.sleep(0.5)
        raise TimeoutError("run did not start in time")

    # ------------------------------------------------------------- core
    def act(self, action: dict) -> dict:
        """Send one action. Returns the server response; check response['ok']."""
        body = {"request_id": uuid.uuid4().hex, "action": action}
        res = self.client._request("POST", f"/v1/runs/{self.id}/act", body)
        self.budgets = res.get("budgets", self.budgets)
        self.last = res
        return res

    def end(self) -> dict:
        return self.client._request("POST", f"/v1/runs/{self.id}/end")

    def result(self) -> dict:
        return self.client._request("GET", f"/v1/runs/{self.id}/result")

    def end_and_wait(self, timeout: float = 600) -> dict:
        self.end()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                return self.result()
            except ArenaError as e:
                if e.code != "not_ready":
                    raise
            time.sleep(0.5)
        raise TimeoutError("no result in time")

    # ---------------------------------------------------------- helpers
    def observe(self, radius: int = 16, **options) -> dict:
        """options (levels that allow them): x, y (centre within limits.observe_range), include (subset of
        entities, resources, summary, obstacles, water), detail ("brief"), only_problems, names, limit."""
        if options:
            return self.act({"type": "observe", "radius": radius, **options})
        return self.act({"type": "observe", "radius": radius})

    def move(self, x: float, y: float) -> dict:
        return self.act({"type": "move", "x": x, "y": y})

    def place(self, entity: str, x: float, y: float, direction: str = "north", belt_type: str | None = None,
              **belt) -> dict:
        """belt_type: underground-belt only, "input" (goes down) or "output" (comes up); both face the way items go.
        belt (belts only): carries=[items] tags what the belt is for (observe flags anything else on it as
        foreign_items); isolated=True refuses any join with another belt except the tiles in allow=[[x, y], ...].
        Every belt placement reports the joins it made as data["merges"]."""
        a = {"type": "place", "entity": entity, "x": x, "y": y, "direction": direction}
        if belt_type:
            a["belt_type"] = belt_type
        return self.act({**a, **_belt_opts(belt)})

    def place_line(self, entity: str, x1: float, y1: float, x2: float, y2: float, **belt) -> dict:
        return self.act({"type": "place_line", "entity": entity, "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                         **_belt_opts(belt)})

    def insert(self, x: float, y: float, item: str, count: int) -> dict:
        return self.act({"type": "insert", "x": x, "y": y, "item": item, "count": count})

    def extract(self, x: float, y: float, item: str, count: int) -> dict:
        return self.act({"type": "extract", "x": x, "y": y, "item": item, "count": count})

    def advance(self, ticks: int) -> dict:
        return self.act({"type": "advance", "ticks": ticks})

    def craft(self, recipe: str, count: int = 1) -> dict:
        return self.act({"type": "craft", "recipe": recipe, "count": count})

    def mine(self, x: float, y: float, count: int = 1) -> dict:
        return self.act({"type": "mine", "x": x, "y": y, "count": count})

    def rotate(self, x: float, y: float, direction: str) -> dict:
        return self.act({"type": "rotate", "x": x, "y": y, "direction": direction})

    def pickup(self, x: float, y: float) -> dict:
        return self.act({"type": "pickup", "x": x, "y": y})

    def set_recipe(self, x: float, y: float, recipe: str) -> dict:
        return self.act({"type": "set_recipe", "x": x, "y": y, "recipe": recipe})

    def research(self, technologies: list[str], mode: str = "replace") -> dict:
        return self.act({"type": "research", "technologies": list(technologies), "mode": mode})

    def stats(self, items: list[str] | None = None, minutes: float = 5) -> dict:
        a: dict = {"type": "stats", "minutes": minutes}
        if items:
            a["items"] = list(items)
        return self.act(a)

    def scan(self, radius: int = 128, resources: list[str] | None = None, min_tiles: int = 4) -> dict:
        a: dict = {"type": "scan", "radius": radius, "min_tiles": min_tiles}
        if resources:
            a["resources"] = list(resources)
        return self.act(a)

    def batch(self, ops: list[dict], stop_on_error: bool = True) -> dict:
        """ops: place, place_line, insert, extract, rotate or set_recipe actions (dicts with a "type")."""
        return self.act({"type": "batch", "ops": list(ops), "stop_on_error": stop_on_error})


class RealtimeRun(Run):
    """For levels with the realtime clock: the game never waits, so this paces you.

    * keeps game actions at most `max_rate` a second, and retries an action refused as
      `too_fast` or `busy` (those cost nothing) until it goes through;
    * `move(..., wait=True)` returns once the character has arrived (walking takes game time).

        run = RealtimeRun.wrap(client.start_run("automation-realtime"))
    """

    def __init__(self, client: ArenaClient, run_id: str, state: dict, max_rate: float = 4.8, speed: float = 1.0):
        super().__init__(client, run_id, state)
        self.speed = speed                    # the task's clock_speed: game seconds per real second
        self.gap = 1.0 / (max_rate * speed)
        self._last = 0.0

    @classmethod
    def wrap(cls, run: Run, max_rate: float = 4.8, speed: float = 1.0) -> "RealtimeRun":
        rt = cls(run.client, run.id, run.state, max_rate, speed)
        rt.budgets, rt.last = run.budgets, run.last
        return rt

    READ_ONLY = ("observe", "stats", "scan")

    def act(self, action: dict) -> dict:
        game_action = action.get("type") not in self.READ_ONLY
        for _ in range(200):
            if game_action:
                wait = self._last + self.gap - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
            res = super().act(action)
            if game_action:
                self._last = time.monotonic()
                nxt = (res.get("data") or {}).get("next_action_tick")   # a batch holds the rate limit longer
                if nxt and res.get("tick") is not None and nxt > res["tick"]:
                    self._last += (nxt - res["tick"]) / 60 / self.speed - self.gap
            if res.get("code") not in ("too_fast", "busy"):
                return res
            time.sleep(0.1)
        return res

    def move(self, x: float, y: float, wait: bool = True) -> dict:
        res = super().move(x, y)
        if wait and res.get("ok"):
            ticks = res["data"].get("arrives_tick", res["tick"]) - res["tick"]
            if ticks > 0:
                time.sleep(ticks / 60 / self.speed + 0.05)
        return res
