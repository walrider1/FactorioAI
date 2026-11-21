"""Automation level: tier scoring, the hands-off window, and the level's rules (no Factorio needed)."""

import pytest
from pydantic import ValidationError

from arena.backends.base import Backend
from arena.config import Score, Task
from arena.controller import ArenaError, RunSession
from arena.models import ActRequest, Outcome
from arena.scorer import TIER_SPAN, automation_breakdown, automation_items, score_run

_n = 0


def req(**action):
    global _n
    _n += 1
    return ActRequest(request_id=f"auto-{_n:08d}", action=action)


class FakeFactory(Backend):
    """Pretends to be a factory that makes and uses items at fixed rates per minute."""

    def __init__(self, per_minute: dict[str, tuple[int, int]], stock: dict[str, tuple[int, int]] | None = None):
        self.per_minute, self.t = per_minute, 0
        self.base = stock or {}
        self.acts: list = []

    def setup(self, task, seed):
        return {}

    def tick(self):
        return self.t

    def act(self, action, max_ticks):
        self.acts.append(action)
        if action.type == "advance":
            self.t += min(action.ticks, max_ticks)
        return Outcome(ok=True)

    def run_ticks(self, n):
        self.t += n

    def score(self, score):
        return 0

    def item_stats(self, items):
        m = self.t / 3600
        out = {}
        for i in items:
            made, used = self.per_minute.get(i, (0, 0))
            b_made, b_used = self.base.get(i, (0, 0))
            out[i] = [b_made + int(made * m), b_used + int(used * m)]
        return out

    def close(self):
        pass


def red_factory(gears_made=12, gears_used=6, red=6):
    return {"iron-plate": (30, 24), "copper-plate": (12, 6), "iron-gear-wheel": (gears_made, gears_used),
            "automation-science-pack": (red, 0)}


def test_tier1_needs_rate_and_a_self_supplied_chain(settings):
    task = settings.tasks["automation"]
    res = score_run(FakeFactory(red_factory()), task)
    b = res["breakdown"]
    assert b["tier"] == 1 and res["score"] == TIER_SPAN + b["points"]
    assert b["tiers"][0]["per_minute"] == 6 and b["tiers"][0]["chain_short"] == []
    assert b["per_minute"]["automation-science-pack"] == [6] * 10
    assert res["idle_tail"]["tick_before"] == 0 and res["idle_tail"]["tick_after"] == 36_000
    # points: 10 min of 30 iron, 12 copper, 12 gears, 6 red
    assert b["points"] == 10 * (30 * 1 + 12 * 1 + 12 * 3 + 6 * 10)


def test_stockpiled_gears_do_not_reach_a_tier(settings):
    task = settings.tasks["automation"]
    # 6 red/min, but the gears come out of a chest: the factory makes none during the window
    res = score_run(FakeFactory(red_factory(gears_made=0, gears_used=6)), task)
    b = res["breakdown"]
    assert b["tier"] == 0 and b["tiers"][0]["rate_ok"] and b["tiers"][0]["chain_short"] == ["iron-gear-wheel"]
    assert res["score"] == b["points"] < TIER_SPAN


def test_too_slow_misses_the_tier_but_still_earns_points(settings):
    res = score_run(FakeFactory(red_factory(red=2)), settings.tasks["automation"])
    assert res["breakdown"]["tier"] == 0 and res["score"] > 0


def test_tier2_ranks_above_any_tier1(settings):
    task = settings.tasks["automation"]
    green = {"iron-plate": (170, 165), "copper-plate": (45, 45), "iron-gear-wheel": (45, 45),
             "copper-cable": (90, 90), "electronic-circuit": (30, 30), "inserter": (30, 30),
             "transport-belt": (30, 30), "logistic-science-pack": (31, 0)}
    t2 = score_run(FakeFactory(green), task)
    huge_t1 = score_run(FakeFactory({**red_factory(), "iron-plate": (5000, 24)}), task)
    assert t2["breakdown"]["tier"] == 2 and huge_t1["breakdown"]["tier"] == 1
    assert huge_t1["breakdown"]["points"] > t2["breakdown"]["points"]   # lots of plates...
    assert t2["score"] > huge_t1["score"]                                  # ...still ranks below tier 2
    assert huge_t1["score"] < 2 * TIER_SPAN


def test_points_are_capped_below_the_next_tier(settings):
    sc = settings.tasks["automation"].score
    snaps = [{i: [0, 0] for i in automation_items(sc)}, {i: [10**6, 0] for i in automation_items(sc)}]
    res = automation_breakdown(sc, snaps, 3600)
    assert res["breakdown"]["tier"] == 2 and res["score"] == 2 * TIER_SPAN + TIER_SPAN - 1


def test_window_counts_only_what_happens_after_the_run(settings):
    # 1,000 red packs made before the window started do not count
    res = score_run(FakeFactory({}, stock={"automation-science-pack": (1000, 0)}), settings.tasks["automation"])
    assert res["score"] == 0 and res["breakdown"]["tier"] == 0


def _session(settings, backend):
    s = RunSession("a1", "alpha", settings.tasks["automation"], "practice", 1008, "practice-1008", None)
    s.start(backend)
    assert s.status == "active"
    return s


def test_only_fuel_may_be_inserted_by_hand(settings):
    backend = FakeFactory({})
    s = _session(settings, backend)
    assert s.act(req(type="insert", x=0, y=0, item="coal", count=5))["ok"]
    with pytest.raises(ArenaError) as e:
        s.act(req(type="insert", x=0, y=0, item="iron-plate", count=5))
    assert e.value.code == "item_not_allowed" and e.value.http_status == 403
    with pytest.raises(ArenaError) as e:
        s.act(req(type="craft", recipe="iron-gear-wheel"))
    assert e.value.code == "verb_not_allowed"
    assert len(backend.acts) == 1 and s.actions_used == 1                 # refused calls cost nothing


def test_set_recipe_follows_the_task_whitelist(settings):
    s = _session(settings, FakeFactory({}))
    assert s.act(req(type="set_recipe", x=1.5, y=1.5, recipe="automation-science-pack"))["charged"] == "action"
    lvl1 = RunSession("b1", "alpha", settings.tasks["level1-iron"], "practice", 1, "p", None)
    lvl1.start(FakeFactory({}))
    with pytest.raises(ArenaError) as e:
        lvl1.act(req(type="set_recipe", x=1.5, y=1.5, recipe="iron-gear-wheel"))
    assert e.value.code == "verb_not_allowed"


def test_score_so_far_is_live_points(settings):
    from arena.scorer import live_points
    sc = settings.tasks["automation"].score
    assert live_points(sc, {i: [2, 0] for i in sc.points}) == 2 * sum(sc.points.values())


def test_config_validation(settings):
    base = settings.tasks["automation"].model_dump()
    with pytest.raises(ValidationError):
        Score(type="automation", item="logistic-science-pack")                 # no tiers or points
    with pytest.raises(ValidationError):
        Task.model_validate({**base, "backends": ["sim", "factorio"]})        # real game only
    with pytest.raises(ValidationError):
        Task.model_validate({**base, "assembler_recipes": []})               # set_recipe needs recipes
    with pytest.raises(ValidationError):
        Task.model_validate({**base, "insert_items": ["plutonium"]})
    bad = {**base, "score": {**base["score"], "points": {"unobtainium": 5}}}
    with pytest.raises(ValidationError):
        Task.model_validate(bad)


def test_llm_tools_only_offer_what_the_level_allows(settings):
    import llm_common
    specs = {n: s for n, _, s in llm_common.tool_specs(settings.tasks["automation"].public())}
    assert "craft" not in specs and "set_recipe" in specs
    assert specs["insert"]["properties"]["item"]["enum"] == ["coal", "wood"]
    assert "assembling-machine-1" in specs["place"]["properties"]["entity"]["enum"]
    assert specs["place_line"]["properties"]["entity"]["enum"] == ["transport-belt", "pipe"]
    l1 = {n: s for n, _, s in llm_common.tool_specs(settings.tasks["level1-iron"].public())}
    assert l1["place"]["properties"]["entity"]["enum"] == ["burner-mining-drill", "stone-furnace", "wooden-chest"]
    prompt = llm_common.system_prompt(settings.tasks["automation"].public(), {"reach": 10})
    assert "hands-off" not in prompt or "nobody touching" in prompt
    assert "offshore pump" in prompt and "tier 2" in prompt


def test_window_starts_when_the_build_time_is_up_however_early_the_run_ends(settings):
    """window_at_build_end: a team that ends at minute 3 is scored over the same minutes 20-30 as one that builds
    to the end, so ending early changes nothing (and leftover build time is no tie-break)."""
    task = settings.tasks["automation"]
    assert task.score.window_at_build_end
    early, late = FakeFactory(red_factory()), FakeFactory(red_factory())
    a = _session(settings, early)
    a.act(req(type="advance", ticks=600))
    a.end("ended_by_team")
    b = RunSession("a2", "alpha", task, "practice", 1008, "practice-1008", None)
    b.start(late)
    b.act(req(type="advance", ticks=task.budgets.ticks))
    b.end("budgets_exhausted")
    for s in (a, b):
        tail = s.result["idle_tail"]
        assert tail["tick_before"] == task.budgets.ticks
        assert tail["tick_after"] == task.budgets.ticks + task.budgets.idle_tail_ticks
    assert a.result["idle_tail"]["build_time_filled"]["ticks"] == task.budgets.ticks - 600
    assert b.result["idle_tail"]["build_time_filled"]["ticks"] == 0
    assert a.result["score"] == b.result["score"]


def test_prompt_states_the_stakes_the_kit_and_batching(settings):
    import llm_common
    from memory import BATCH_PROMPT
    prompt = llm_common.system_prompt(settings.tasks["automation"].public(), {"reach": 10})
    assert "game minute 20" in prompt and "worth 100,000" in prompt
    assert "The kit is enough for tier 2" in prompt and "20 assemblers" in prompt
    assert "5-15 calls" in BATCH_PROMPT


def test_realtime_level_config(settings):
    rt = settings.tasks["automation-realtime"]
    assert rt.clock == "realtime" and "advance" not in rt.verbs
    assert rt.budgets.actions is None and rt.budgets.min_action_gap_ticks == 12
    assert settings.tasks["automation"].clock == "paused"          # the paused level stays as it was
    base = rt.model_dump()
    with pytest.raises(ValidationError):                             # realtime has no advance
        Task.model_validate({**base, "verbs": [*base["verbs"], "advance"]})
    with pytest.raises(ValidationError):                             # the rate limit is realtime only
        Task.model_validate({**base, "clock": "paused"})
    with pytest.raises(ValidationError):                             # no action limit and no rate limit
        Task.model_validate({**base, "budgets": {**base["budgets"], "min_action_gap_ticks": 0}})


def test_crew_director_reply_is_filtered_to_known_skills():
    import crew_bot

    class Usage:
        prompt_tokens, completion_tokens, prompt_tokens_details = 2000, 100, None

    class Reply:
        def __init__(self, text):
            self.usage = Usage()
            self.choices = [type("C", (), {"message": type("M", (), {"content": text})()})()]

    class Fake:
        def __init__(self, text):
            self.chat = type("Chat", (), {"completions": self})()
            self.text, self.sent = text, None

        def create(self, **kw):
            self.sent = kw
            return Reply(self.text)

    fake = Fake('{"note": "go", "steps": [{"skill": "iron_block", "args": {}}, {"skill": "teleport", "args": {}}]}')
    d = crew_bot.LLMDirector(fake, "gpt-6.1-terra", max_cost_usd=1.0)
    rep = {"game_minute": 1.0, "queued_steps": []}
    out = d.plan(rep)
    assert [s["skill"] for s in out["steps"]] == ["iron_block"]
    assert fake.sent["response_format"] == {"type": "json_object"}
    assert d.cost_usd == pytest.approx((2000 * 1.0 + 100 * 5.0) / 1e6)
    d.cost_usd = 1.0
    assert d.plan(rep)["steps"] == [{"skill": "finish", "args": {}}]   # cost cap: stop planning


def test_connect_power_uses_a_station_a_factory_built(monkeypatch):
    """A factory built the station before any block had power: power <-> connect_power used to recurse forever
    (RecursionError voided run 20261118-114634). The block must wire to the station instead."""
    import crew_bot
    crew = object.__new__(crew_bot.Crew)               # no game needed: only the bookkeeping is under test
    crew.world, crew.factories, crew.power_pole = None, [], (40, 8)
    crew.blocks = [crew_bot.Block(0, {"poles": [(9, 34), (9, 30), (8, 26)]})]
    wired = []
    monkeypatch.setattr(crew_bot.ab, "build_poles", lambda w, start, goal: wired.append((start, goal)))
    for skill in ("power", "connect_power"):
        crew.blocks[0].powered = False
        wired.clear()
        assert "connected" in crew.run_skill(skill, {"block": 0})
        assert wired == [((40, 8), (9, 30))] and crew.blocks[0].powered


def test_foreman_classifies_statuses():
    import crew_bot
    assert crew_bot.classify({"name": "boiler", "status": "no_fuel", "inventories": {"fuel": {}}})[1] == "fuel"
    assert crew_bot.classify({"name": "inserter", "status": "no_power", "inventories": {}})[1] == "director"
    starved = {"name": "assembling-machine-1", "status": "item_ingredient_shortage", "missing": ["copper-plate"]}
    assert crew_bot.classify(starved) == ("starved of copper-plate", "director")
    assert crew_bot.classify({"name": "stone-furnace", "status": "working", "inventories": {"fuel": {"coal": 2}}}) is None
