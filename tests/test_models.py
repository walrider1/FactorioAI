"""Schema validation: the gateway's first line of defence."""

import pytest
from pydantic import TypeAdapter, ValidationError

from arena.backends.factorio import BackendError, _fix_empty, _lua_string
from arena.models import Action, ActRequest

A = TypeAdapter(Action)


@pytest.mark.parametrize("bad", [
    {"type": "place", "entity": 'iron-chest"); game.print("x', "x": 0, "y": 0},
    {"type": "place", "entity": "not-a-building", "x": 0, "y": 0},      # names must be in the game catalog
    {"type": "set_recipe", "x": 0, "y": 0, "recipe": "made-up-recipe"},
    {"type": "set_recipe", "x": 0, "y": 0},
    {"type": "place_line", "entity": "inserter", "x1": 0, "y1": 0, "x2": 5, "y2": 0},
    {"type": "insert", "x": 0, "y": 0, "item": "coal]==]", "count": 1},
    {"type": "move", "x": float("nan"), "y": 0},
    {"type": "move", "x": 1e9, "y": 0},
    {"type": "advance", "ticks": 0},
    {"type": "advance", "ticks": -5},
    {"type": "advance", "ticks": 10**9},
    {"type": "insert", "x": 0, "y": 0, "item": "coal", "count": 0},
    {"type": "observe", "radius": 500},
    {"type": "place", "entity": "stone-furnace", "x": 0, "y": 0, "direction": "up"},
    {"type": "place", "entity": "stone-furnace", "x": 0, "y": 0, "lua": "game.speed=100"},
    {"type": "spawn_items", "item": "iron-plate", "count": 1000},
    {"type": "place_line", "entity": "transport-belt", "x1": 0, "y1": 0, "x2": 5, "y2": 5},
    {"type": "place_line", "entity": "transport-belt", "x1": 0, "y1": 0, "x2": 100, "y2": 0},
    {"type": "craft", "recipe": "Iron-Plate"},
])
def test_rejects_bad_actions(bad):
    with pytest.raises(ValidationError):
        A.validate_python(bad)


def test_accepts_good_actions():
    for good in [
        {"type": "observe"},
        {"type": "move", "x": 10.5, "y": -3},
        {"type": "place", "entity": "burner-mining-drill", "x": 1, "y": 2, "direction": "east"},
        {"type": "place", "entity": "assembling-machine-1", "x": 1.5, "y": 2.5},
        {"type": "set_recipe", "x": 1.5, "y": 2.5, "recipe": "automation-science-pack"},
        {"type": "place_line", "entity": "pipe", "x1": 0, "y1": 0, "x2": 0, "y2": 9},
        {"type": "insert", "x": 1, "y": 2, "item": "coal", "count": 20},
        {"type": "advance", "ticks": 600},
        {"type": "place_line", "entity": "transport-belt", "x1": 0, "y1": 0, "x2": 10, "y2": 0},
    ]:
        A.validate_python(good)


@pytest.mark.parametrize("rid", ["short", "has space here", "a" * 65, "semi;colon!", ""])
def test_request_id_format(rid):
    with pytest.raises(ValidationError):
        ActRequest(request_id=rid, action={"type": "observe"})


def test_every_valid_action_is_safe_for_lua():
    import json
    samples = [
        {"type": "place", "entity": "burner-mining-drill", "x": -12.5, "y": 3, "direction": "west", "max_ticks": 5},
        {"type": "insert", "x": 1, "y": 2, "item": "iron-gear-wheel", "count": 999, "max_ticks": 0},
    ]
    for s in samples:
        _lua_string(json.dumps(s, separators=(",", ":")))


@pytest.mark.parametrize("payload", ['{"a":"]==]"}', '{"a":"\n"}', '{"a":"é"}'])
def test_lua_string_refuses_unsafe(payload):
    with pytest.raises(BackendError):
        _lua_string(payload)


def test_fix_empty_tables():
    assert _fix_empty({"inventory": [], "entities": []}) == {"inventory": {}, "entities": []}
    assert _fix_empty({"data": {"inventories": {"fuel": []}}}) == {"data": {"inventories": {"fuel": {}}}}
