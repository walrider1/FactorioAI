"""The day's AI bill (spend.py): every session books its own running cost, the day's total is what every bot
process on the machine spent, and past 75% / 100% of the cap the bot goes to saver mode, then plays on rules."""

from brain.spend import DailySpend, spent_today


def test_sessions_add_up_and_the_modes_follow_the_day_total(tmp_path):
    led = tmp_path / "spend.json"
    a = DailySpend(cap=10.0, key="stream@1", path=led)
    b = DailySpend(cap=10.0, key="test@2", path=led)
    assert a.update(2.0, today="2026-11-27") == "normal"
    assert a.update(1.0, today="2026-11-27") == "normal"            # a cost never goes back down
    assert b.update(5.0, today="2026-11-27") == "normal"            # 7.0 of 10
    assert b.update(5.6, today="2026-11-27") == "saver"             # 7.6: past 75%
    assert spent_today(led, "2026-11-27") == 7.6
    assert a.update(4.5, today="2026-11-27") == "off"               # 10.1: the cap
    # past midnight the session's earlier spend stays on yesterday; only what comes after counts today
    assert a.update(5.0, today="2026-11-28") == "normal"
    assert spent_today(led, "2026-11-28") == 0.5 and spent_today(led, "2026-11-27") == 10.1


def test_no_cap_never_slows_anything(tmp_path):
    d = DailySpend(cap=0, key="x", path=tmp_path / "s.json")
    assert d.update(500.0) == "normal"
