"""Run controller rules: budgets, tick accounting, idempotency, limits, voiding."""

import time

import pytest

from arena.backends.base import BackendError
from arena.backends.sim import SimBackend
from arena.controller import ArenaError, RunSession
from arena.models import ActRequest

_n = 0


def req(**action):
    global _n
    _n += 1
    return ActRequest(request_id=f"req-{_n:08d}", action=action)


def session(settings, task="level1-iron", backend=None, run_dir=None):
    s = RunSession("t1", "alpha", settings.tasks[task], "practice", 101, "practice-101", run_dir)
    s.start(backend or SimBackend())
    assert s.status == "active"
    return s


def test_action_budget_and_rejections_are_charged(settings):
    s = session(settings)
    r = s.act(req(type="place", entity="stone-furnace", x=50, y=50))   # out of reach
    assert not r["ok"] and r["code"] == "out_of_reach" and r["charged"] == "action"
    assert r["budgets"]["actions_left"] == 99
    s.actions_used = 100
    r = s.act(req(type="place", entity="stone-furnace", x=3, y=3))
    assert r["code"] == "budget_exhausted" and r["charged"] is None


def test_observe_has_its_own_budget(settings):
    s = session(settings)
    r = s.act(req(type="observe"))
    assert r["ok"] and r["budgets"]["observes_left"] == 299 and r["budgets"]["actions_left"] == 100
    s.observes_used = 300
    assert s.act(req(type="observe"))["code"] == "budget_exhausted"


def test_advance_costs_only_ticks_and_is_clamped(settings):
    s = session(settings)
    r = s.act(req(type="advance", ticks=1000))
    assert r["ticks_spent"] == 1000 and r["budgets"]["actions_left"] == 100
    r = s.act(req(type="advance", ticks=100_000))
    assert r["ticks_spent"] == 35_000 and r["budgets"]["ticks_left"] == 0
    assert s.act(req(type="advance", ticks=1))["code"] == "budget_exhausted"


def test_tick_accounting_uses_real_tick_delta(settings):
    s = session(settings)
    r = s.act(req(type="move", x=15.5, y=0.5))
    assert r["ticks_spent"] == 100 and s.ticks_used == 100


def test_idempotent_retry_is_not_charged_twice(settings):
    s = session(settings)
    q = req(type="place", entity="stone-furnace", x=3, y=3)
    first = s.act(q)
    again = s.act(q)
    assert again["replayed"] is True and again["seq"] == first["seq"]
    assert s.actions_used == 1
    with pytest.raises(ArenaError) as e:
        s.act(ActRequest(request_id=q.request_id, action={"type": "observe"}))
    assert e.value.code == "request_id_reused"


def test_task_rules_block_verbs_entities_recipes(settings):
    s = session(settings)
    for bad, code in [
        (dict(type="craft", recipe="iron-gear-wheel"), "verb_not_allowed"),
        (dict(type="mine", x=1, y=1), "verb_not_allowed"),
        (dict(type="place", entity="iron-chest", x=3, y=3), "entity_not_allowed"),
    ]:
        with pytest.raises(ArenaError) as e:
            s.act(req(**bad))
        assert e.value.code == code
    assert s.actions_used == 0


def test_wall_clock_limit_ends_and_scores(settings):
    s = session(settings)
    s.deadline = time.time() - 1
    with pytest.raises(ArenaError) as e:
        s.act(req(type="observe"))
    assert e.value.code == "run_not_active"
    assert s.status == "finished" and s.end_reason == "wall_clock_limit"
    assert s.result["idle_tail"]["tick_after"] - s.result["idle_tail"]["tick_before"] == 3600


def test_idle_timeout_detected(settings):
    s = session(settings)
    s.last_activity = time.time() - 10_000
    assert s.expired_reason() == "idle_timeout"


def test_auto_end_when_all_budgets_spent(settings):
    s = session(settings)
    s.actions_used = 100
    r = s.act(req(type="advance", ticks=36_000))
    assert r["run_status"] == "finished" and s.end_reason == "budgets_exhausted"


class ExplodingBackend(SimBackend):
    def act(self, a, max_ticks):
        raise BackendError("simulated crash")


def test_backend_crash_voids_run(settings, tmp_path):
    s = session(settings, backend=ExplodingBackend(), run_dir=tmp_path / "run")
    with pytest.raises(ArenaError) as e:
        s.act(req(type="observe"))
    assert e.value.code == "run_voided" and e.value.http_status == 503
    assert s.status == "void" and s.result["score"] is None
    assert "simulated crash" not in e.value.message        # internals stay organizer-side
    assert (tmp_path / "run" / "result.json").exists()


def test_scorer_counts_production_after_idle_tail(settings):
    s = session(settings)
    for a in [dict(type="move", x=-10.5, y=-5.5)]:
        s.act(req(**a))
    s.end("ended_by_team")
    assert s.result["status"] == "finished" and s.result["score"] == 0
    assert s.result["metric"] == "production:iron-plate"


# ------------------------------------------------------------------ time-only levels

def test_time_only_level_charges_game_time_per_action(settings):
    s = session(settings, "level2-bootstrap-timed")
    assert s.budgets()["actions_left"] is None
    r = s.act(req(type="rotate", x=40, y=40, direction="east"))        # rejected, still takes 60 ticks
    assert not r["ok"] and r["ticks_spent"] == 60 and r["budgets"]["actions_left"] is None
    r = s.act(req(type="move", x=15.5, y=0.5))                          # 100-tick walk: no top-up
    assert r["ticks_spent"] == 100
    r = s.act(req(type="move", x=16.5, y=0.5))                          # 7-tick step: topped up to 60
    assert r["ticks_spent"] == 60
    r = s.act(req(type="observe"))                                      # observes stay instant
    assert r["ticks_spent"] == 0
    assert s.ticks_used == 220 and s.actions_used == 3


def test_time_only_run_ends_when_time_runs_out(settings):
    s = session(settings, "level2-bootstrap-timed")
    s.act(req(type="advance", ticks=72_000 - 30))
    r = s.act(req(type="rotate", x=40, y=40, direction="east"))        # only 30 ticks left: clamped
    assert r["ticks_spent"] == 30 and r["budgets"]["ticks_left"] == 0
    assert s.status == "finished" and s.end_reason == "budgets_exhausted"


def test_action_budget_level_unchanged_by_the_option(settings):
    s = session(settings)
    r = s.act(req(type="rotate", x=40, y=40, direction="east"))
    assert r["ticks_spent"] == 0 and r["budgets"]["actions_left"] == 99


def test_unlimited_actions_need_a_time_cost():
    from arena.config import Budgets
    common = dict(observes=10, ticks=100, idle_tail_ticks=0, wall_clock_seconds=60, idle_timeout_seconds=60)
    with pytest.raises(ValueError):
        Budgets(actions=None, action_ticks=0, **common)
    assert Budgets(actions=None, action_ticks=30, **common).actions is None
