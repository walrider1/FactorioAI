"""Coal running low is urgent, like iron and copper: its next patch is scouted or mined now (run 20261127-161020:
an 89-tile coal patch, coal mined 0.8/min against 12 burned, and only a warning)."""

from types import SimpleNamespace

from brain.assess import _ore_soon


def test_coal_running_low_or_full_while_short_is_urgent():
    crew = SimpleNamespace(coal_budget=lambda: {"mined_per_min": 0.8, "burned_per_min": 12.0, "net_per_min": -11.2})
    assert "coal left" in _ore_soon(crew, "coal", {"minutes_left": 40, "room_for_more": True})
    assert "burns faster" in _ore_soon(crew, "coal", {"minutes_left": None, "room_for_more": False})
    crew.coal_budget = lambda: {"mined_per_min": 20.0, "burned_per_min": 12.0, "net_per_min": 8.0}
    assert _ore_soon(crew, "coal", {"minutes_left": 900, "room_for_more": False}) is None
