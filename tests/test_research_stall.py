"""A goal that needs research gives up when the research stands still (run 20261122-133537: "build pumpjack"
waited 15 game minutes twice on fluid-handling stuck at 36%, labs without green packs)."""

from types import SimpleNamespace

import pytest

from brain.crew import Crew
from brain.world import BuildError


def _crew(progress_seq, researched_at=None):
    crew = Crew.__new__(Crew)
    clock = {"m": 100.0, "calls": 0}

    def minute():
        return clock["m"]

    def read_stats(_n):
        clock["calls"] += 1
        clock["m"] += 0.5                                    # every look is half a game minute later
        i = min(clock["calls"], len(progress_seq) - 1)
        w.research = {"current": "fluid-handling", "progress": progress_seq[i]}
        if researched_at is not None and clock["calls"] >= researched_at:
            crew.kn.researched.add("fluid-handling")

    w = SimpleNamespace(speed=1000.0, minutes_left=500.0, research={"current": "fluid-handling", "progress": 0.36},
                        doing=lambda *_: None, read_stats=read_stats)
    crew.w = w
    crew.kn = SimpleNamespace(researched=set(), research_seconds=lambda t, labs: 750.0,
                              techs={"fluid-handling": {"unit_ingredients": [
                                  {"name": "automation-science-pack", "amount": 1},
                                  {"name": "logistic-science-pack", "amount": 1}]}})
    crew.base = SimpleNamespace(count=lambda kind: 6)
    crew.foreman = SimpleNamespace(patrol=lambda **_: None)
    crew.checkpoint = lambda: None
    crew._minute = minute
    return crew, clock


def test_research_that_does_not_move_fails_the_wait_in_minutes_and_names_the_packs(monkeypatch):
    monkeypatch.setattr("brain.crew.time.sleep", lambda s: None)
    crew, clock = _crew([0.36] * 100)
    with pytest.raises(BuildError) as e:
        crew._wait_research("fluid-handling", ["fluid-handling"])
    msg = str(e.value)
    assert "research stalled" in msg and "36%" in msg and "logistic-science-pack" in msg
    assert clock["m"] - 100.0 <= Crew.RESEARCH_STALL_MIN + 1.0     # not the goal's whole 15-minute budget


def test_research_that_moves_is_waited_for_until_it_is_done(monkeypatch):
    monkeypatch.setattr("brain.crew.time.sleep", lambda s: None)
    seq = [0.36 + 0.01 * k for k in range(40)]                     # slow but moving: never called stalled
    crew, _ = _crew(seq, researched_at=30)
    # packs by hand (defer=False): the crew must stay and feed the labs, so it waits to the end
    assert crew._wait_research("fluid-handling", ["fluid-handling"], defer=False) == "researched fluid-handling"


def test_research_that_moves_but_is_far_off_lets_the_crew_go(monkeypatch):
    """Run 20261124-120854: a pumpjack goal waited 16 game minutes on oil-gathering creeping to 53%."""
    monkeypatch.setattr("brain.crew.time.sleep", lambda s: None)
    seq = [0.36 + 0.01 * k for k in range(40)]
    crew, clock = _crew(seq, researched_at=30)
    with pytest.raises(BuildError) as e:
        crew._wait_research("fluid-handling", ["fluid-handling"])
    assert "more game minutes" in str(e.value) and clock["m"] - 100.0 < 5
