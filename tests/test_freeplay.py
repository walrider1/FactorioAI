"""Freeplay server features without a game: catalog names, task config, the new verbs' rules, milestones."""

import pytest
from pydantic import TypeAdapter, ValidationError

from arena import catalog
from arena.backends.base import Backend
from arena.config import Score, Task
from arena.controller import ArenaError, RunSession
from arena.models import Action, ActRequest, Outcome
from arena.scorer import milestone_breakdown

A = TypeAdapter(Action)
pytestmark = pytest.mark.skipif(catalog.get().empty, reason="needs config/catalog.json (arena catalog)")


class FakeBackend(Backend):
    """A realtime game that accepts everything and reports what the test sets."""

    def __init__(self):
        self.t = 0
        self.sent = []
        self.live = {"score": 0}
        self.reached: dict[str, int] = {}

    def setup(self, task, seed):
        return {"spawn": {"x": 0, "y": 0}}

    def tick(self):
        return self.t

    def act(self, action, max_ticks):
        self.sent.append(action)
        self.t += 12
        return Outcome(ok=True, data={}, tick=self.t)

    def run_ticks(self, n):
        self.t += max(0, n)

    def score(self, score):
        return int(self.live["score"])

    def score_breakdown(self, score):
        return dict(self.live)

    def start_clock(self, ticks, run=True):
        return self.t

    def stop_clock(self):
        return self.t

    def milestones(self):
        return dict(self.reached)

    def item_stats(self, items):
        return {i: [0, 0] for i in items}

    def close(self):
        pass


def _task(settings, name="freeplay-green", **changes) -> Task:
    t = settings.tasks[name]
    return t.model_copy(update=changes) if changes else t


def _session(task) -> tuple[RunSession, FakeBackend]:
    s = RunSession("r1", "alpha", task, "practice", 1, "practice-1", run_dir=None)
    b = FakeBackend()
    s.start(b)
    assert s.status == "active"
    return s, b


def _req(n, action):
    return ActRequest(request_id=f"req-{n:08d}", action=action)


# ------------------------------------------------------------------ names
def test_catalog_names_are_accepted_and_free_text_is_not():
    assert A.validate_python({"type": "place", "entity": "rocket-silo", "x": 0, "y": 0}).entity == "rocket-silo"
    assert A.validate_python({"type": "research", "technologies": ["automation"]}).technologies == ["automation"]
    for bad in ({"type": "research", "technologies": ["automation; game.print(1)"]},
                {"type": "research", "technologies": []},
                {"type": "stats", "items": ["not-an-item"]},
                {"type": "batch", "ops": [{"type": "move", "x": 1, "y": 1}]},       # walking is not batchable
                {"type": "batch", "ops": [{"type": "insert", "x": 0, "y": 0, "item": "coal", "count": 1}] * 101},
                {"type": "observe", "radius": 10, "x": 3},                          # x without y
                {"type": "place_line", "entity": "assembling-machine-1", "x1": 0, "y1": 0, "x2": 3, "y2": 0}):
        with pytest.raises(ValidationError):
            A.validate_python(bad)
    assert A.validate_python({"type": "place_line", "entity": "fast-transport-belt",
                              "x1": 0, "y1": 0, "x2": 3, "y2": 0}).entity == "fast-transport-belt"


def test_snap_uses_catalog_sizes():
    from arena import rules
    assert rules.snap("lab", 3.2, 4.9) == (3.5, 4.5)              # 3x3
    assert rules.snap("oil-refinery", 3.2, 4.9) == (3.5, 4.5)     # 5x5
    assert rules.snap("rocket-silo", 0.4, 0.4) == (0.5, 0.5)      # 9x9


# ----------------------------------------------------------------- config
def test_task_config_rules(settings):
    base = settings.tasks["freeplay-green"].model_dump()
    with pytest.raises(ValidationError):                            # research needs the real tech tree
        Task.model_validate({**base, "tech": "unlock"})
    with pytest.raises(ValidationError):                            # batch needs a batch size
        Task.model_validate({**base, "limits": {**base["limits"], "batch_max_ops": 0}})
    with pytest.raises(ValidationError):                            # the simulator has no tech tree
        Task.model_validate({**base, "backends": ["sim"]})
    with pytest.raises(ValidationError):
        Score.model_validate({"type": "milestones", "milestones": [
            {"id": "x", "name": "x", "points": 1, "tech": "automation", "item": "iron-plate", "produced": 1}]})
    with pytest.raises(ValidationError):
        Score.model_validate({"type": "milestones", "milestones": [
            {"id": "x", "name": "x", "points": 1, "tech": "no-such-tech"}]})
    t = settings.tasks["freeplay-green"]
    assert t.recipe_allowed("lab") and not t.recipe_allowed("iron-plate")   # smelting is not hand-craftable
    assert t.machine_recipe_allowed("plastic-bar") and t.entity_allowed("oil-refinery")


def test_original_levels_still_whitelist(settings):
    s, _ = _session(settings.tasks["automation-realtime"])
    with pytest.raises(ArenaError) as e:
        s.act(_req(1, A.validate_python({"type": "place", "entity": "rocket-silo", "x": 0, "y": 0})))
    assert e.value.code == "entity_not_allowed"
    with pytest.raises(ArenaError) as e:
        s.act(_req(2, A.validate_python({"type": "research", "technologies": ["automation"]})))
    assert e.value.code == "verb_not_allowed"
    with pytest.raises(ArenaError) as e:                            # observe radius: the task's own limit
        s.act(_req(3, A.validate_python({"type": "observe", "radius": 30})))
    assert e.value.code == "radius_too_large"


# ------------------------------------------------------------- controller
def test_read_only_verbs_use_the_observe_budget_and_batches_count_each_op(settings):
    s, b = _session(_task(settings))
    s.act(_req(1, A.validate_python({"type": "stats"})))
    s.act(_req(2, A.validate_python({"type": "scan", "radius": 100})))
    s.act(_req(3, A.validate_python({"type": "observe", "radius": 32, "x": 10, "y": 10, "detail": "brief"})))
    assert (s.observes_used, s.actions_used) == (3, 0)
    ops = [{"type": "place", "entity": "stone-furnace", "x": i * 2, "y": 0} for i in range(5)]
    s.act(_req(4, A.validate_python({"type": "batch", "ops": ops})))
    assert s.actions_used == 5
    with pytest.raises(ArenaError) as e:
        s.act(_req(5, A.validate_python({"type": "batch", "ops": ops * 11})))     # 55 > the task's 50
    assert e.value.code == "batch_too_large"


def test_batch_ops_follow_the_task_rules(settings):
    t = _task(settings, entities=["stone-furnace"])
    s, _ = _session(t)
    with pytest.raises(ArenaError) as e:
        s.act(_req(1, A.validate_python({"type": "batch", "ops": [
            {"type": "place", "entity": "stone-furnace", "x": 0, "y": 0},
            {"type": "place", "entity": "lab", "x": 5, "y": 0}]})))
    assert e.value.code == "entity_not_allowed"


def test_run_ends_when_every_milestone_is_reached(settings):
    s, b = _session(_task(settings))
    s.act(_req(1, A.validate_python({"type": "stats"})))
    assert s.status == "active"
    ids = [m.id for m in s.task.score.milestones]
    b.reached = {m: 600 * (k + 1) for k, m in enumerate(ids)}
    b.live = {"score": 1, "complete": True}
    s.act(_req(2, A.validate_python({"type": "stats"})))
    assert s.status == "finished" and s.end_reason == "completed"
    bd = s.result["breakdown"]
    assert bd["reached"] == len(ids) and bd["complete"]
    assert s.result["score"] > sum(m.points for m in s.task.score.milestones)   # time bonus on top


def test_clock_speed_scales_realtime_budgets(settings):
    t = _task(settings, clock_speed=4.0)
    s, _ = _session(t)
    s.act(_req(1, A.validate_python({"type": "stats"})))
    s.clock_wall -= 10                                   # ten real seconds ago
    assert s.budgets()["ticks_left"] == pytest.approx(t.budgets.ticks - 2400, abs=30)


# ----------------------------------------------------------------- scorer
def test_milestone_breakdown_points_and_time_bonus():
    sc = Score.model_validate({"type": "milestones", "time_bonus": 1.0, "milestones": [
        {"id": "a", "name": "A", "points": 100, "tech": "automation"},
        {"id": "b", "name": "B", "points": 300, "item": "logistic-science-pack", "per_minute": 10}]})
    bd = milestone_breakdown(sc, {"a": 1000 + 36000}, start_tick=1000, budget_ticks=72000)
    assert bd["score"] == 150 and bd["reached"] == 1 and not bd["complete"]   # half the time left: +50%
    assert bd["furthest"] == "A" and bd["milestones"][0]["game_minute"] == 10.0
    assert milestone_breakdown(sc, {}, 0, 72000)["score"] == 0
