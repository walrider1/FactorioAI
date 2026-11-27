"""The act goal (Crew.act): the overseer and supervisors may order any direct action, done exactly as said; and
"feed" on labs takes packs to them (it only fuelled before)."""

import contextlib

import pytest

from brain import assess
from brain.modules import Site
from brain.world import BuildError
from test_upkeep_outposts import _crew


def _setup():
    crew, w, _ = _crew()
    w.inventory = {"automation-science-pack": 10}
    w.step = lambda name: contextlib.nullcontext()
    log = []
    w.go = lambda x, y: log.append(("go", x, y))
    w.insert = lambda x, y, item, n: log.append(("insert", x, y, item, n)) or n
    w.extract = lambda x, y, item, n: log.append(("take", x, y, item, n)) or n
    crew.wait = lambda s: log.append(("wait", s)) or f"waited {s}"
    crew.gather = lambda item, n: None
    labs = crew.base.add(Site("labs", {"count": 2}, (0, 0), entities=[("lab", 1.5, 1.5, "north"),
                                                                       ("lab", 5.5, 1.5, "north"),
                                                                       ("small-electric-pole", 3.5, 0.5, "north")]))
    return crew, w, labs, log


def test_any_list_of_direct_actions_is_done_as_said_even_walking_in_circles():
    crew, w, labs, log = _setup()
    circle = [{"op": "go", "at": [10, 0]}, {"op": "go", "at": [0, 10]}, {"op": "go", "at": [-10, 0]},
              {"op": "go", "at": [0, -10]}, {"op": "wait", "seconds": 5},
              {"op": "insert", "site": labs.id, "item": "automation-science-pack", "count": 10}]
    msg = crew.act(circle)
    assert [e[0] for e in log] == ["go", "go", "go", "go", "wait", "insert", "insert"]
    assert ("insert", 1.5, 1.5, "automation-science-pack", 5) in log      # spread over the labs, poles skipped
    assert "walked to (0, -10)" in msg and "put 10 automation-science-pack in" in msg
    assert "act" in assess.GOAL_DOS


def test_a_failing_step_says_why_and_what_was_done_before_it():
    crew, w, labs, log = _setup()
    with pytest.raises(BuildError, match=r"step 2 \(insert\) failed: there is no site 99; done before it: walked"):
        crew.act([{"op": "go", "at": [1, 1]}, {"op": "insert", "site": 99, "item": "x"}])
    with pytest.raises(BuildError, match="unknown op 'fly'"):
        crew.act([{"op": "fly"}])


def test_hand_steps_never_lay_or_cut_a_fluid_line():
    """The overnight zones test: 15 hand "act" goals re-laid the oil floor's crude line, picked up its inlet, and
    blue science never started. Pipes are the modules' (and repair's) to lay; nothing of the act is done."""
    crew, w, labs, log = _setup()
    oil = crew.base.add(Site("workshop", {"item": "plastic-bar"}, (24, 171),
                             entities=[("pipe-to-ground", 24.5, 171.5, "west"), ("oil-refinery", 30.5, 171.5, "north")]))
    with pytest.raises(BuildError, match="no hand-placed pipe-to-ground"):
        crew.act([{"op": "go", "at": [28, 168]}, {"op": "place", "entity": "pipe-to-ground", "at": [24, 171]}])
    with pytest.raises(BuildError, match=rf"part of site {oil.id}'s fluid line"):
        crew.act([{"op": "go", "at": [28, 168]}, {"op": "pickup", "at": [24.5, 171.5]}])
    assert log == []                                          # refused before the first step
    assert "Never pipes" in __import__("brain.brain", fromlist=["GOAL_HELP"]).GOAL_HELP
