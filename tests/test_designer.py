"""AI-designed chiplets (--designs ai | ai-only): the steam station drawn by a model, checked to work before use."""

import json

import pytest

from arena import catalog

pytestmark = pytest.mark.skipif(catalog.get().empty, reason="needs config/catalog.json (arena catalog)")


@pytest.fixture(scope="module")
def kn():
    from brain.knowledge import Knowledge
    return Knowledge(json.loads(json.dumps(catalog.get().data)))


def _reply(parts):
    return json.dumps({"note": "test", "parts": [{"name": n, "col": c, "row": r, "dir": d} for n, c, r, d in parts]})


# boiler at the top-left (0, 0); one engine above it; the pole west of the engine; the chest under the boiler
ALT_1 = [("boiler", 0, 0, "north"), ("steam-engine", 0, -5, "north"), ("small-electric-pole", -1, -3, "north"),
         ("burner-inserter", 0, 2, "south"), ("iron-chest", 0, 3, "north")]


@pytest.mark.parametrize("engines", [1, 2])
def test_the_hand_made_station_passes_and_the_check_finds_its_inlet(kn, engines):
    from brain.designer import check_station
    from brain.modules import SteamPower
    lib = SteamPower().library_template(engines)
    probs, info = check_station(kn, lib["parts"], engines)
    assert probs == []
    assert info["inlet"] == lib["inlet"] and sorted(info["clear"]) == sorted(lib["clear"])


def test_the_old_one_pole_station_fails_the_check(kn):
    from brain.designer import check_station
    from brain.modules import SteamPower
    old = [p for p in SteamPower.station_parts(2) if not (p[0] == "small-electric-pole" and p[2] < -4)]
    probs, _ = check_station(kn, old, 2)
    assert any("outside every pole's supply area" in p for p in probs)


@pytest.mark.parametrize("change, expect", [
    (lambda ps: [("steam-engine", 1, -5, "north") if p[0] == "steam-engine" else p for p in ps], "gets no steam"),
    (lambda ps: [("burner-inserter", 0, 2, "north") if p[0] == "burner-inserter" else p for p in ps], "burner inserter"),
    (lambda ps: ps + [("iron-chest", 0, 1, "north")], "needs exactly 1 iron-chest"),
    (lambda ps: [("small-electric-pole", 1, 0, "north") if p[0] == "small-electric-pole" else p for p in ps], "overlaps"),
    (lambda ps: ps + [("iron-chest", -1, 1, "north"), ("iron-chest", 3, 1, "north")], "needs exactly 1"),
])
def test_broken_designs_are_caught_with_a_reason(kn, change, expect):
    from brain.designer import check_station, parse_parts
    parts, _ = parse_parts(kn, _reply(change(ALT_1)))
    probs, _ = check_station(kn, parts, 1)
    assert any(expect in p for p in probs), probs


def test_covered_water_inputs_are_caught(kn):
    from brain.designer import check_station, parse_parts
    # both short sides of the boiler blocked by poles (a third pole keeps the count legal)
    parts, _ = parse_parts(kn, _reply(ALT_1 + [("small-electric-pole", -1, 1, "north"),
                                               ("small-electric-pole", 3, 1, "north")]))
    probs, _ = check_station(kn, parts, 1)
    assert any("water inputs" in p for p in probs), probs


class FakeLLM:
    model, cost_usd = "fake", 0.0

    def __init__(self, replies):
        self.replies, self.seen = list(replies), []

    def chat(self, messages):
        self.seen.append(messages[-1]["content"])
        return self.replies.pop(0) if self.replies else self.replies_last

    replies_last = '{"parts": []}'


def test_designer_feeds_problems_back_and_uses_the_fixed_design(kn):
    from brain.designer import ChipletDesigner
    from brain.modules import SteamPower
    bad = [p for p in ALT_1 if p[0] != "small-electric-pole"] + [("small-electric-pole", -4, -3, "north")]
    llm = FakeLLM([_reply(bad), _reply(ALT_1)])
    d = ChipletDesigner(llm, mode="ai", log=lambda *a: None)
    tpl = d.station(kn, 1, SteamPower().library_template)
    assert tpl["by"] == "ai" and d.records[-1]["tries"] == 2
    assert "outside every pole's supply area" in llm.seen[1]          # the second try heard why
    assert "E" in d.records[-1]["drawing"] and "w" in d.records[-1]["drawing"]


def test_designer_falls_back_to_the_hand_made_station_or_fails_in_ai_only(kn):
    from brain.designer import ChipletDesigner, DesignError
    from brain.modules import SteamPower
    from brain.world import BuildError
    lib = SteamPower().library_template
    d = ChipletDesigner(FakeLLM(["no json here"] * 3), mode="ai", log=lambda *a: None)
    tpl = d.station(kn, 2, lib)
    assert tpl["by"] == "library" and tpl["parts"] == lib(2)["parts"] and d.records[-1]["tries"] == 3
    strict = ChipletDesigner(FakeLLM(["no json here"] * 3), mode="ai-only", log=lambda *a: None)
    with pytest.raises(DesignError):
        strict.station(kn, 2, lib)
    sp = SteamPower()
    sp.designer = strict
    try:
        with pytest.raises(BuildError, match="AI's station design failed"):
            sp.template(type("W", (), {"kn": kn})(), 2)
    finally:
        sp.designer = None


def test_an_ai_station_fits_the_shore_and_kits_what_it_uses(kn):
    from brain.designer import ChipletDesigner
    from brain.modules import SteamPower
    from brain.world import FreeWorld

    class NoRun:
        budgets: dict = {}

        def act(self, action):
            raise AssertionError("design only: no game actions")

    w = FreeWorld(NoRun(), kn, log=lambda *a: None)
    w.seen = {(x, y) for x in range(-40, 60) for y in range(-40, 60)}
    w.water = {(x, y) for x in range(-40, 60) for y in range(-40, 60) if y < 0}   # straight shore along y = 0
    sp = SteamPower()
    sp.designer = ChipletDesigner(FakeLLM([_reply(ALT_1)]), mode="ai", log=lambda *a: None)
    try:
        lay = sp.find_layout(w, (10, -2), (10, 10), engines=1)
        assert lay and not lay["tiles"] & w.water and lay["path"]
        assert {p[3] for p in lay["parts"]} >= {"boiler", "steam-engine", "burner-inserter"}
        kit = sp.kit(w, None, {"engines": 1})
        assert kit["steam-engine"] == 1 and kit["small-electric-pole"] == 2 and kit["offshore-pump"] == 1
    finally:
        sp.designer = None
