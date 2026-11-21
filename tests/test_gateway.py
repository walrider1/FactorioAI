"""Gateway: auth, isolation, rate limits, error hygiene, quotas, hidden eval maps."""

import uuid

from conftest import auth, start_active

from arena.gateway import create_app


def act(http, token, run_id, action, rid=None):
    return http.post(f"/v1/runs/{run_id}/act", headers=auth(token),
                     json={"request_id": rid or uuid.uuid4().hex, "action": action})


def test_health_and_rules_are_public(http):
    assert http.get("/v1/health").json()["ok"] is True
    rules = http.get("/v1/rules").json()
    ids = [t["id"] for t in rules["tasks"]]
    assert "level1-iron" in ids and "level3-delivery" not in ids   # sim backend can't run level 3


def test_auth_required(http, token):
    assert http.get("/v1/me").status_code == 401
    assert http.get("/v1/me", headers=auth("fa_wrong")).status_code == 401
    r = http.get("/v1/me", headers=auth(token))
    assert r.status_code == 200 and r.json()["team"] == "alpha"


def test_teams_cannot_see_each_others_runs(http, settings, token):
    other = settings.add_team("beta")
    run_id = start_active(http, token)
    assert http.get(f"/v1/runs/{run_id}", headers=auth(other)).status_code == 404
    assert act(http, other, run_id, {"type": "observe"}).status_code == 404


def test_one_active_run_per_team(http, token):
    start_active(http, token)
    r = http.post("/v1/runs", json={"task": "level1-iron"}, headers=auth(token))
    assert r.status_code == 409 and r.json()["error"]["code"] == "run_already_active"


def test_full_run_over_http(http, token):
    run_id = start_active(http, token)
    r = act(http, token, run_id, {"type": "observe", "radius": 10})
    assert r.status_code == 200 and r.json()["ok"]
    assert http.get(f"/v1/runs/{run_id}/result", headers=auth(token)).status_code == 409
    st = http.post(f"/v1/runs/{run_id}/end", headers=auth(token)).json()
    assert st["status"] == "finished"
    res = http.get(f"/v1/runs/{run_id}/result", headers=auth(token)).json()
    assert res["score"] == 0 and "team" not in res


def test_validation_errors_are_clean_and_free(http, token):
    run_id = start_active(http, token)
    r = act(http, token, run_id, {"type": "place", "entity": "x\"); game.speed=9 --", "x": 0, "y": 0})
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "invalid_request"
    assert "game.speed" not in r.text                     # input is never echoed back
    st = http.get(f"/v1/runs/{run_id}", headers=auth(token)).json()
    assert st["budgets"]["actions_used"] == 0


def test_body_size_limit(http, token, settings):
    run_id = start_active(http, token)
    big = settings.competition.gateway.max_body_bytes + 1000
    r = http.post(f"/v1/runs/{run_id}/act", headers={**auth(token), "content-type": "application/json"},
                  content=b'{"request_id":"' + b"a" * big + b'"}')
    assert r.status_code == 413


def test_rate_limit(settings, manager, token):
    from fastapi.testclient import TestClient
    settings.competition.gateway.rate_limit_rps = 0.001
    settings.competition.gateway.rate_limit_burst = 3
    with TestClient(create_app(settings, manager, start_reaper=False)) as c:
        codes = [c.get("/v1/me", headers=auth(token)).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3:] == [429, 429]


def test_internal_errors_hide_details(settings, manager, token, monkeypatch):
    from fastapi.testclient import TestClient

    def boom(team):
        raise RuntimeError("secret path /home/organizer/secrets.yaml")
    monkeypatch.setattr(manager, "team_summary", boom)
    with TestClient(create_app(settings, manager, start_reaper=False), raise_server_exceptions=False) as c:
        r = c.get("/v1/me", headers=auth(token))
    assert r.status_code == 500 and r.json()["error"]["code"] == "internal_error"
    assert "secret" not in r.text


def test_practice_reveals_seed_eval_does_not(http, settings, token):
    run_id = start_active(http, token)
    assert "seed" in http.get(f"/v1/runs/{run_id}", headers=auth(token)).json()
    http.post(f"/v1/runs/{run_id}/end", headers=auth(token))

    r = http.post("/v1/runs", json={"task": "level1-iron", "mode": "eval"}, headers=auth(token))
    assert r.status_code == 403 and r.json()["error"]["code"] == "eval_closed"

    settings.competition.eval.open = True
    seeds = settings.generate_eval_seeds("level1-iron", 2)
    run_id = start_active(http, token, mode="eval")
    st = http.get(f"/v1/runs/{run_id}", headers=auth(token)).json()
    assert "seed" not in st and st["map"] == "eval-1"
    assert not any(str(s) in http.get(f"/v1/runs/{run_id}", headers=auth(token)).text for s in seeds)
    http.post(f"/v1/runs/{run_id}/end", headers=auth(token))
    res = http.get(f"/v1/runs/{run_id}/result", headers=auth(token)).json()
    assert "score" not in res                                 # hidden until the report
    run2 = start_active(http, token, mode="eval")
    assert http.get(f"/v1/runs/{run2}", headers=auth(token)).json()["map"] == "eval-2"
    http.post(f"/v1/runs/{run2}/end", headers=auth(token))
    r = http.post("/v1/runs", json={"task": "level1-iron", "mode": "eval"}, headers=auth(token))
    assert r.json()["error"]["code"] == "no_eval_maps_left"


def test_practice_quota(http, settings, token):
    settings.competition.practice.runs_per_day = 2
    for _ in range(2):
        rid = start_active(http, token)
        http.post(f"/v1/runs/{rid}/end", headers=auth(token))
    r = http.post("/v1/runs", json={"task": "level1-iron"}, headers=auth(token))
    assert r.status_code == 429 and r.json()["error"]["code"] == "practice_quota"


def test_void_runs_do_not_use_up_eval_maps(http, settings, manager, token):
    settings.competition.eval.open = True
    settings.generate_eval_seeds("level1-iron", 1)
    run_id = start_active(http, token, mode="eval")
    manager.sessions[run_id].void("simulated crash")
    run2 = start_active(http, token, mode="eval")
    assert http.get(f"/v1/runs/{run2}", headers=auth(token)).json()["map"] == "eval-1"
