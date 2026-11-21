import os
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "sdk"))
sys.path.insert(0, str(ROOT / "bots"))

from arena.config import load_settings  # noqa: E402
from arena.controller import RunManager  # noqa: E402
from arena.gateway import BodyLimit, create_app  # noqa: E402


def make_settings(tmp_path: Path, backend: str = "sim"):
    s = load_settings(ROOT / "config" / "competition.yaml")
    s.competition.backend = backend
    # tests use their own ports, so they never clash with a real server or match on this PC
    s.competition.factorio.rcon_base_port = 27150
    s.competition.factorio.game_base_port = 34250
    s.runs_dir = tmp_path / "runs"
    s.secrets_dir = tmp_path / "secrets"
    s.reports_dir = tmp_path / "reports"
    return s


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def token(settings):
    return settings.add_team("alpha")


@pytest.fixture
def manager(settings):
    return RunManager(settings)


@pytest.fixture
def http(settings, manager):
    app = BodyLimit(create_app(settings, manager, start_reaper=False), settings.competition.gateway.max_body_bytes)
    with TestClient(app) as c:
        yield c


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def start_active(http, token, task="level1-iron", mode="practice"):
    r = http.post("/v1/runs", json={"task": task, "mode": mode}, headers=auth(token))
    assert r.status_code == 201, r.text
    run_id = r.json()["run_id"]
    for _ in range(200):
        st = http.get(f"/v1/runs/{run_id}", headers=auth(token)).json()
        if st["status"] == "active":
            return run_id
        assert st["status"] in ("queued", "starting"), st
        time.sleep(0.02)
    raise AssertionError("run never became active")


factorio = pytest.mark.skipif(os.environ.get("ARENA_FACTORIO") != "1",
                              reason="set ARENA_FACTORIO=1 to run against real Factorio in Docker")
