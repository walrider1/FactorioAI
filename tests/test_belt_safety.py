"""Belt safety and rate targets, offline (run 20261120-211555: fuel strips spilled coal onto iron feeds unseen; the
bot built purple and yellow for 6/min against 10/min milestones).

The game side (merges, the isolated guard, lanes, foreign_items) runs in test_belt_safety_factorio.py."""

import math
import zipfile
from types import SimpleNamespace

import pytest
from pydantic import TypeAdapter, ValidationError

from arena import config, models
from arena.backends.factorio import _refresh_script
from brain import belts, purity, roadmap
from brain.world import FreeWorld

ACTION = TypeAdapter(models.Action)


# ------------------------------------------------------------------ the API
def test_belt_options_validate():
    a = ACTION.validate_python({"type": "place", "entity": "transport-belt", "x": 1, "y": 2, "direction": "east",
                                "carries": ["coal"], "isolated": True, "allow": [[1, 2], [3.5, 4.5]]})
    assert a.carries == ["coal"] and a.isolated and a.allow == [(1.0, 2.0), (3.5, 4.5)]
    line = ACTION.validate_python({"type": "place_line", "entity": "transport-belt", "x1": 0, "y1": 0, "x2": 4,
                                   "y2": 0, "carries": ["iron-plate", "copper-plate"]})
    assert line.carries == ["iron-plate", "copper-plate"] and not line.isolated
    assert ACTION.validate_python({"type": "observe", "belts": True}).belts
    with pytest.raises(ValidationError):
        ACTION.validate_python({"type": "place", "entity": "transport-belt", "x": 1, "y": 2, "carries": ["nope"]})


def test_a_rate_milestone_can_ask_several_items_at_once():
    m = config.Milestone(id="science-30", name="x", points=1, item="logistic-science-pack",
                         also=["automation-science-pack"], per_minute=30, window=10)
    assert m.lua()["also"] == ["automation-science-pack"]
    with pytest.raises(ValidationError):
        config.Milestone(id="x", name="x", points=1, item="iron-plate", produced=10, also=["copper-plate"])


def test_resuming_a_save_runs_the_current_server_script(tmp_path):
    save = tmp_path / "m1.zip"
    with zipfile.ZipFile(save, "w") as z:
        z.writestr("m1/control.lua", "old")
        z.writestr("m1/level.dat0", b"\x00\x01")
    scen = tmp_path / "scen"
    scen.mkdir()
    (scen / "control.lua").write_text("new")
    assert _refresh_script(save, scen) == ["control.lua"]
    with zipfile.ZipFile(save) as z:
        assert z.read("m1/control.lua") == b"new" and z.read("m1/level.dat0") == b"\x00\x01"


# ------------------------------------------------------------------ belt jobs (prevention)
def _world():
    return SimpleNamespace(BELT_ENTITIES=FreeWorld.BELT_ENTITIES, belt_guard=True, entities={})


def test_belts_in_a_job_are_tagged_and_isolated_with_their_joins_allowed():
    w = _world()
    assert FreeWorld.belt_opts(w, "transport-belt") == {}                      # outside a job: as before
    with FreeWorld.belt_job(w, ["coal"], route=[(0, 0), (1, 0)], joins=[(2, 0)]):
        opts = FreeWorld.belt_opts(w, "transport-belt")
        assert opts == {"isolated": True, "allow": [[0, 0], [1, 0], [2, 0]], "carries": ["coal"]}
        assert FreeWorld.belt_opts(w, "inserter") == {}                        # only belts
    assert FreeWorld.belt_opts(w, "underground-belt") == {}                    # the job ended


def test_a_link_may_join_only_at_its_ends():
    steps = [("belt", (0, 0), "east"), ("belt", (1, 0), "east"), ("belt", (2, 0), "north")]
    assert belts.link_joins(steps, from_belt=False) == [(2, -1)]               # the port's belt ahead
    assert belts.link_joins(steps, from_belt=True) == [(2, -1), (-1, 0)]       # and the line tail behind


def test_a_fuel_strip_is_not_laid_against_a_belt():
    strip = [(0, 5), (1, 5), (2, 5), (3, 5), (4, 5), (5, 5)]
    assert belts.strip_ends(strip) == [(6, 5)]
    w = _world()
    w.belt_at = lambda t: FreeWorld.belt_at(w, t)
    assert belts.strip_blocked(w, strip) is None
    w.entities[(6.5, 5.5)] = {"name": "transport-belt"}                        # an iron belt laid earlier
    assert belts.strip_blocked(w, strip) == (6, 5)


# ------------------------------------------------------------------ purity (the cure)
class BeltWorld:
    """Belts by tile with lanes and neighbours, as observe {belts: true} gives them."""

    def __init__(self, belts_by_tile):
        self.b = belts_by_tile
        self.keepout, self.calls = set(), []

    def observe(self, radius, x=0, y=0, include=None, names=None, belts=False):
        out = []
        for (tx, ty), e in self.b.items():
            if abs(tx + 0.5 - x) <= radius and abs(ty + 0.5 - y) <= radius:
                out.append({"x": tx + 0.5, "y": ty + 0.5, **e})
        return {"entities": out}

    def near(self, x, y):
        pass

    def act(self, a):
        self.calls.append(("rotate", (math.floor(a["x"]), math.floor(a["y"])), a["direction"]))
        self.b[(math.floor(a["x"]), math.floor(a["y"]))]["direction"] = a["direction"]
        return {"ok": True, "data": {"merges": []}}

    def pickup(self, name, x, y, d="north"):
        self.calls.append(("pickup", (math.floor(x), math.floor(y))))
        return True

    def place(self, name, x, y, d="north", tiles=()):
        self.calls.append(("place", (math.floor(x), math.floor(y)), d))
        self.b[(math.floor(x), math.floor(y))].pop("lanes", None)              # laid again: empty
        return {}


def _iron_link():
    steps = [("belt", (x, 0), "east") for x in range(6)]
    return {"to_site": 14, "item": "iron-plate", "steps": steps, "ins": ((-1, 0), "east", "inserter")}


def _spilled_world():
    b = {(x, 0): {"name": "transport-belt", "direction": "east", "lanes": [{"iron-plate": 3}, {}], "fed_by": []}
         for x in range(6)}
    for x in (3, 4, 5):                                       # coal piled up on the far lane at the end
        b[(x, 0)]["lanes"] = [{"iron-plate": 3}, {"coal": 4}]
    b[(2, 0)]["fed_by"] = [[1.5, 0.5], [2.5, 1.5]]            # its own belt behind, and a strip's dead end
    b[(2, 1)] = {"name": "transport-belt", "direction": "north", "lanes": [{}, {"coal": 2}], "fed_by": []}
    return b


def test_a_belt_carrying_something_else_is_an_urgent_alert_for_the_rule_floor():
    crew = SimpleNamespace(w=BeltWorld(_spilled_world()), links=[_iron_link()], _minute=lambda: 120.0)
    [a] = purity.check(crew)
    assert a["level"] == "urgent" and a["key"].startswith("belt-foreign") and "coal" in a["what"]
    assert a["fix"] == {"do": "purge", "site": 14, "item": "iron-plate"}
    from brain.assess import CRITICAL
    assert a["key"].startswith(CRITICAL)
    crew.w.b.clear()                                          # cached until the next look is due
    assert purity.check(crew) == [a]


def test_purge_turns_the_leak_away_and_cleans_the_belts_upstream_first():
    w = BeltWorld(_spilled_world())
    crew = SimpleNamespace(w=w, links=[_iron_link()], _minute=lambda: 120.0)
    msg = purity.purge(crew, 14, "iron-plate")
    rot = [c for c in w.calls if c[0] == "rotate"]
    assert rot and rot[0][1] == (2, 1) and rot[0][2] in ("east", "west")       # turned off the link, not reversed
    cleaned = [c[1] for c in w.calls if c[0] == "pickup"]
    assert cleaned == [(3, 0), (4, 0), (5, 0)]                                 # only the dirty belts, in order
    assert [c[1] for c in w.calls if c[0] == "place"] == cleaned
    assert "3 belt(s) cleaned" in msg and crew._purity_at < 0
    with pytest.raises(Exception, match="no belt link"):
        purity.purge(crew, 99, "iron-plate")


# ------------------------------------------------------------------ rate targets (scoring in line with the run)
LADDER = [
    {"id": "red-auto", "item": "automation-science-pack", "per_minute": 5},
    {"id": "green-auto", "item": "logistic-science-pack", "per_minute": 10},
    {"id": "steel", "tech": "steel-processing"},
    {"id": "science-30", "item": "logistic-science-pack", "also": ["automation-science-pack"], "per_minute": 30},
    {"id": "oil", "tech": "oil-processing"},
    {"id": "plastics", "tech": "plastics"},
    {"id": "plastic-auto", "item": "plastic-bar", "per_minute": 30},
    {"id": "blue-auto", "item": "chemical-science-pack", "per_minute": 10},
    {"id": "purple-auto", "item": "production-science-pack", "per_minute": 10},
]


def _rate_crew(reached):
    return SimpleNamespace(w=SimpleNamespace(milestones={k: 1 for k in reached}), milestone_ladder=LADDER)


def test_targets_step_up_with_the_ladder():
    early = roadmap.rate_targets(_rate_crew([]))
    assert early["automation-science-pack"] == 15 and early["logistic-science-pack"] == 15   # the road's rates
    assert "plastic-bar" not in early
    now = roadmap.rate_targets(_rate_crew(["red-auto", "green-auto", "steel"]))
    assert now["automation-science-pack"] == now["logistic-science-pack"] == 34.5            # 30/min each next
    later = roadmap.rate_targets(_rate_crew(["red-auto", "green-auto", "steel", "science-30", "oil", "plastics"]))
    assert later["plastic-bar"] == 34.5 and later["chemical-science-pack"] == 11.5
    assert later["production-science-pack"] == 11.5 > roadmap.SCIENCE_RATE["production-science-pack"]
    assert roadmap.milestone_rate(_rate_crew([]), "logistic-science-pack") == 10             # the stage's rate
    assert roadmap.milestone_rate(_rate_crew([]), "utility-science-pack") is None


def test_the_task_asks_red_and_green_together():
    import yaml
    from pathlib import Path
    raw = yaml.safe_load((Path(__file__).parent.parent / "config/tasks/freeplay-open.yaml").read_text(encoding="utf-8"))
    task = config.Task.model_validate(raw)
    m = next(m for m in task.score.milestones if m.id == "science-30")
    assert m.item == "logistic-science-pack" and m.also == ["automation-science-pack"]


# run 20261121-140755: site 83's coal link tapped the copper-plate line beside a mine's coal belt; its inserter lifted
# copper onto the coal belt, and six purges in an hour cleaned the belt while the inserter kept feeding copper
def _tapped_world():
    b = {(x, 0): {"name": "transport-belt", "direction": "east", "lanes": [{"coal": 2}, {"copper-plate": 3}],
                  "fed_by": []} for x in range(6)}
    b[(-2, 0)] = {"name": "transport-belt", "direction": "west", "lanes": [{"copper-plate": 4}, {}], "fed_by": []}
    b[(-1, 0)] = {"name": "inserter", "direction": "west", "drop": [0.5, 0.5]}
    return b


def _coal_link():
    steps = [("belt", (x, 0), "east") for x in range(6)]
    return {"to_site": 83, "item": "coal", "steps": steps, "ins": ((-1, 0), "west", "inserter"),
            "source": (-2, 0), "port": {"end": [6, 0]}}


def test_purge_finds_a_link_whose_own_source_feeds_the_wrong_item_and_lays_it_again_elsewhere():
    w = BeltWorld(_tapped_world())
    ln = _coal_link()
    unlinked = []
    crew = SimpleNamespace(w=w, links=[ln], _minute=lambda: 120.0, _link_fails={(6, 0): {}},
                           _unlink=unlinked.append)
    msg = purity.purge(crew, 83, "coal")
    assert unlinked == [ln] and (-2, 0) in crew._bad_taps and "copper-plate" in msg
    assert (6, 0) not in crew._link_fails                                     # the link step tries it again now


def test_purge_takes_up_another_sites_inserter_dropping_onto_the_link():
    b = _spilled_world()
    for x in range(6):
        b[(x, 0)]["fed_by"] = []
    b[(4, 1)] = {"name": "inserter", "direction": "north", "drop": [4.5, 0.5]}   # a copper unit's output, say
    b[(-1, 0)] = {"name": "inserter", "direction": "east", "drop": [0.5, 0.5]}   # the link's own: stays
    b[(-2, 0)] = {"name": "wooden-chest", "inventories": {"chest": {"iron-plate": 50}}}
    ln = {**_iron_link(), "ins": ((-1, 0), "west", "inserter"), "source": (-2, 0)}
    w = BeltWorld(b)
    crew = SimpleNamespace(w=w, links=[ln], _minute=lambda: 120.0)
    msg = purity.purge(crew, 14, "iron-plate")
    assert ("pickup", (4, 1)) in w.calls and ("pickup", (-1, 0)) not in w.calls
    assert "dropped onto it" in msg and "no cause found" not in msg


def test_a_belt_tap_is_never_another_items_line_nor_a_belt_tagged_with_something_else():
    from brain.crew import Crew
    crew = Crew.__new__(Crew)
    line = {"item": "copper-plate", "tiles": [(-18, 42), (-19, 42)]}
    crew.lines = lambda item=None: [(None, line)]
    crew.w = SimpleNamespace(entities={(-17.5, 41.5): {"carries": ["coal"]}, (-17.5, 43.5): {"carries": ["iron-plate"]}})
    assert not crew._may_tap((-18, 42), "coal")                                # the copper line beside the fuel belt
    assert crew._may_tap((-18, 41), "coal")                                    # the fuel belt itself
    assert not crew._may_tap((-18, 43), "coal")                                # tagged: it carries iron
    crew._bad_taps = {(-18, 41)}
    assert not crew._may_tap((-18, 41), "coal")                                # a purge found it feeding wrong
