"""The brain bot's pure parts: game knowledge from the catalog, and the opening book's order."""

import json

import pytest

from arena import catalog

pytestmark = pytest.mark.skipif(catalog.get().empty, reason="needs config/catalog.json (arena catalog)")


@pytest.fixture
def kn():
    from brain.knowledge import Knowledge
    return Knowledge(json.loads(json.dumps(catalog.get().data)))


def test_hand_plan_from_nothing(kn):
    p = kn.hand_plan("burner-mining-drill", 2)
    assert p.steps[-1] == ("burner-mining-drill", 2) and not p.blocked
    assert p.raw == {"iron-plate": 18, "stone": 10}             # 3 gears + 3 plates + a furnace, twice
    p = kn.hand_plan("burner-mining-drill", 1, {"iron-gear-wheel": 3, "stone-furnace": 1, "iron-plate": 3})
    assert p.raw == {} and p.steps == [("burner-mining-drill", 1)]


def test_locked_recipes_and_trigger_techs(kn):
    assert kn.recipe_for("lab", hand=True) is None               # locked until electronics
    assert kn.unlocking_tech("lab") == "electronics" and kn.is_trigger("electronics")
    assert kn.kit_plan({"lab": 1}).blocked == ["lab"]
    kn.learn_research(["electronics", "steam-power"])
    p = kn.kit_plan({"lab": 1, "steam-engine": 1})
    assert not p.blocked and set(p.raw) == {"iron-plate", "copper-plate"}
    assert kn.tech_path("logistic-science-pack") == ["automation-science-pack", "logistic-science-pack"]


def test_kit_plan_counts_holdings_once(kn):
    p = kn.kit_plan({"stone-furnace": 3}, {"stone-furnace": 1, "stone": 5})
    assert p.steps == [("stone-furnace", 2)] and p.raw == {"stone": 5}
    p = kn.kit_plan({"stone-furnace": 1}, {"stone-furnace": 1}, extra=True)   # one more on top
    assert p.steps == [("stone-furnace", 1)] and p.raw == {"stone": 5}


def test_opening_book_order():
    from brain.brain import ScriptedStrategist
    rep = {"base": [], "researched": [], "recent_results": [], "research": {}}
    s = ScriptedStrategist()
    first = s.plan(rep)["goals"][0]
    assert first == {"do": "build", "module": "burner_mine", "resource": "coal", "count": 2}   # fuel first
    rep["recent_results"] = [{"goal": first, "ok": False}]          # a failed step is skipped, not looped
    assert s.plan(rep)["goals"][0]["resource"] == "iron-ore"
    rep = {"base": [{"kind": "burner_mine", "resource": r, "count": n} for r, n in
                    (("iron-ore", 9), ("coal", 2), ("stone", 1), ("copper-ore", 4))]
           + [{"kind": "steam_power"}, {"kind": "labs", "count": 2}],
           "researched": ["automation", "electric-mining-drill", "logistics"], "recent_results": [], "research": {}}
    assert s.plan(rep)["goals"][0] == {"do": "build", "module": "factory", "item": "automation-science-pack",
                                       "per_minute": 6}
    rep["base"].append({"kind": "factory", "item": "automation-science-pack", "per_minute": 4.0})
    assert s.plan(rep)["goals"][0]["per_minute"] == 2.0                # a smaller factory: build the rest


def test_llm_strategist_times_its_call_and_falls_back():
    from brain.brain import LLMStrategist

    class Usage:
        prompt_tokens, completion_tokens, prompt_tokens_details = 1000, 50, None

    class Fake:
        def __init__(self, fail=False):
            self.fail, self.opts = fail, None
            self.chat = type("C", (), {"completions": self})()

        def with_options(self, **kw):
            self.opts = kw
            return self

        def create(self, **kw):
            if self.fail:
                raise TimeoutError("took too long")
            msg = type("M", (), {"content": '{"note": "go", "doctrine": "d", "goals": [{"do": "wait"}]}'})()
            return type("R", (), {"usage": Usage(), "choices": [type("Ch", (), {"message": msg})()]})()

    rep = {"base": [], "researched": [], "recent_results": [], "research": {}, "game_minute": 1.0}
    ok = Fake()
    s = LLMStrategist(ok, "gpt-6.1-terra", {}, log=lambda *a: None)
    plan = s.plan(rep)
    assert plan["goals"] == [{"do": "wait"}] and plan["latency_s"] >= 0
    assert ok.opts == {"timeout": 120.0, "max_retries": 1}      # never the client's 10-minute default
    s2 = LLMStrategist(Fake(fail=True), "gpt-6.1-terra", {}, log=lambda *a: None)
    out = s2.plan(rep)
    assert "model call failed" in out["note"] and out["goals"]    # the opening book steps in
