"""Public game API (the gateway).

The only thing teams can reach. It authenticates teams, rate-limits them,
validates every request against strict schemas, and forwards to the run
controller. Errors are always {"error": {"code", "message"}} and never carry
stack traces, file paths or raw game output.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
import uuid

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__, catalog, rules
from .config import Settings, hash_token
from .controller import FINISHED, VOID, ArenaError, RunManager
from .models import ActRequest, StartRunRequest

log = logging.getLogger("arena.gateway")


def _err(status: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status, headers=headers)


class TokenBucket:
    def __init__(self, rate: float, burst: int):
        self.rate, self.burst = rate, burst
        self._state: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            tokens, last = self._state.get(key, (float(self.burst), now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            if tokens < 1:
                self._state[key] = (tokens, now)
                return False
            self._state[key] = (tokens - 1, now)
            return True


class _BodyTooLarge(Exception):
    pass


class BodyLimit:
    """ASGI middleware: reject request bodies over the limit, even without Content-Length."""

    def __init__(self, app, limit: int):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        cl = headers.get(b"content-length")
        if cl is not None and (not cl.isdigit() or int(cl) > self.limit):
            return await _err(413, "body_too_large", f"request body over {self.limit} bytes")(scope, receive, send)
        seen = 0
        started = False

        async def limited_receive():
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > self.limit:
                    raise _BodyTooLarge()
            return msg

        async def tracking_send(msg):
            nonlocal started
            if msg["type"] == "http.response.start":
                started = True
            await send(msg)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            if not started:
                await _err(413, "body_too_large", f"request body over {self.limit} bytes")(scope, receive, send)


class TeamAuth:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._teams: dict[str, str] = {}
        self._mtime: float | None = None
        self._lock = threading.Lock()

    def lookup(self, token: str) -> str | None:
        path = self.settings.teams_file
        mtime = path.stat().st_mtime if path.exists() else None
        with self._lock:
            if mtime != self._mtime:
                self._teams, self._mtime = self.settings.load_teams(), mtime
            digest = hash_token(token)
            for known, team in self._teams.items():
                if secrets.compare_digest(known, digest):
                    return team
        return None


def create_app(settings: Settings, manager: RunManager | None = None, start_reaper: bool = True) -> FastAPI:
    cfg = settings.competition
    manager = manager or RunManager(settings)
    auth = TeamAuth(settings)
    team_bucket = TokenBucket(cfg.gateway.rate_limit_rps, cfg.gateway.rate_limit_burst)
    fail_bucket = TokenBucket(0.2, 10)   # failed logins per client address

    app = FastAPI(title=f"{cfg.name} · public game API", version=__version__,
                  description="The only interface team AIs get. See docs/RULES.md.")
    app.state.manager = manager

    if start_reaper:
        @app.on_event("startup")
        def _startup() -> None:
            manager.start_reaper()

        @app.on_event("shutdown")
        def _shutdown() -> None:
            manager.stop()

    # ------------------------------------------------------------ errors
    @app.exception_handler(ArenaError)
    async def _arena_error(request: Request, exc: ArenaError):
        return _err(exc.http_status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        problems = [{"field": ".".join(str(p) for p in e.get("loc", [])[1:]), "problem": e.get("msg", "")}
                    for e in exc.errors()][:10]
        return JSONResponse({"error": {"code": "invalid_request", "message": "request failed validation",
                                       "problems": problems}}, status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return _err(exc.status_code, code, code.replace("_", " "))

    @app.exception_handler(Exception)
    async def _crash(request: Request, exc: Exception):
        incident = uuid.uuid4().hex[:12]
        log.exception("internal error %s on %s %s", incident, request.method, request.url.path)
        return _err(500, "internal_error", f"internal error (incident {incident})")

    # -------------------------------------------------------------- auth
    def team_of(request: Request, authorization: str | None = Header(default=None)) -> str:
        client = request.client.host if request.client else "unknown"
        token = ""
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        team = auth.lookup(token) if token else None
        if team is None:
            if not fail_bucket.allow(client):
                raise ArenaError("rate_limited", "too many failed logins", 429)
            raise ArenaError("unauthorized", "missing or invalid team token", 401)
        if not team_bucket.allow(team):
            raise ArenaError("rate_limited", "slow down", 429)
        return team

    # ------------------------------------------------------------ routes
    @app.get("/v1/health")
    def health():
        return {"ok": True, "version": __version__, "backend": cfg.backend}

    @app.get("/v1/rules")
    def public_rules():
        return {
            "tasks": [t.public() for t in settings.tasks.values() if cfg.backend in t.backends],
            "constants": {
                "ticks_per_second": rules.TICKS_PER_SECOND, "reach": rules.REACH,
                "resource_reach": rules.RESOURCE_REACH, "character_speed_tiles_per_tick": rules.CHARACTER_SPEED,
                "max_move_distance": rules.MAX_MOVE_DISTANCE, "max_observe_radius": rules.MAX_OBSERVE_RADIUS,
                "max_line_length": rules.MAX_LINE_LENGTH,
                "max_scan_radius": rules.MAX_SCAN_RADIUS, "max_batch_ops": rules.MAX_BATCH_OPS,
                "note": "each task's own limits (observe radius, scan radius, batch size) are in its 'limits'",
            },
            "costs": {
                "observe": "1 observe (separate budget), 0 ticks",
                "advance": "0 actions, the ticks you ask for",
                "move": "1 action, ceil(distance / 0.15) ticks",
                "mine": "1 action, 120 ticks per ore",
                "craft": "1 action, recipe time x count",
                "pickup": "1 action, entity mining time (tasks with clear_obstacles: also trees and rocks)",
                "set_recipe": "1 action, 0 ticks",
                "stats, scan": "1 observe each (read-only, never rate-limited)",
                "research": "1 action, 0 ticks",
                "batch": "1 action per op; on realtime levels each op costs limits.batch_op_gap_ticks of the rate limit",
                "other": "1 action, 0 ticks",
                "action_ticks": "if a task sets budgets.action_ticks, every action takes at least that many "
                                "ticks (the world keeps running); tasks with budgets.actions = null have no "
                                "action limit, only the tick budget",
                "rejected": "game-level rejections still cost what the verb costs; schema errors cost nothing",
            },
        }

    @app.get("/v1/catalog")
    def game_catalog():
        """The game's data (items, fluids, recipes, technologies, entities, resources): public, the same for all."""
        cat = catalog.get()
        if cat.empty:
            raise ArenaError("no_catalog", "this server has no game catalog", 404)
        return cat.data

    @app.get("/v1/me")
    def me(team: str = Depends(team_of)):
        return manager.team_summary(team)

    @app.post("/v1/runs", status_code=201)
    def start_run(body: StartRunRequest, team: str = Depends(team_of)):
        s = manager.create_run(team, body.task, body.mode)
        return s.public_state(reveal_seed=s.mode == "practice")

    @app.get("/v1/runs/{run_id}")
    def run_state(run_id: str, team: str = Depends(team_of)):
        s = manager.get(run_id, team)
        return s.public_state(reveal_seed=s.mode == "practice")

    @app.post("/v1/runs/{run_id}/act")
    def act(run_id: str, body: ActRequest, team: str = Depends(team_of)):
        s = manager.get(run_id, team)
        return s.act(body)

    @app.post("/v1/runs/{run_id}/end")
    def end(run_id: str, team: str = Depends(team_of)):
        s = manager.get(run_id, team)
        if s.status == "active":
            s.end("ended_by_team")
        return s.public_state(reveal_seed=s.mode == "practice")

    @app.get("/v1/runs/{run_id}/result")
    def result(run_id: str, team: str = Depends(team_of)):
        s = manager.get(run_id, team)
        if s.status not in (FINISHED, VOID) or s.result is None:
            raise ArenaError("not_ready", f"run is {s.status}", 409)
        r = dict(s.result)
        r.pop("team", None)
        if s.mode == "eval" and not cfg.eval.reveal_scores and r.get("status") == FINISHED:
            for k in ("score", "breakdown"):
                r.pop(k, None)
            r["note"] = "evaluation scores are published with the final report"
        return json.loads(json.dumps(r))

    return app


def build_app() -> FastAPI:
    """uvicorn factory: ``uvicorn arena.gateway:build_app --factory``."""
    import os
    from .config import load_settings
    settings = load_settings(os.environ.get("ARENA_CONFIG", "config/competition.yaml"))
    app = create_app(settings)
    return BodyLimit(app, settings.competition.gateway.max_body_bytes)  # type: ignore[return-value]
