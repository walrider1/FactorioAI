"""Big electric poles on long power lines (Crew._big_line): straight steps, each pole on the free 2x2 spot nearest
its point, within wire reach of the one before, the end poles within a small pole's wire of the network and the
site; nothing when they are not researched or no steel is to be had."""

import math

from brain.crew import BIG_POLE, BIG_WIRE
from brain.power import REACH
from test_upkeep_outposts import _crew


def _big(unlocked=(BIG_POLE,), steel=1000, blocked=frozenset()):
    crew, w, _ = _crew()
    crew.kn.unlocked = set(crew.kn.unlocked) | set(unlocked)
    w.inventory = {"steel-plate": steel}
    crew.stored = lambda item: 0
    crew.ready_items = lambda: set()
    free = lambda t: t not in blocked
    return crew, free


def test_a_long_line_goes_over_big_poles_in_straight_steps():
    crew, free = _big(blocked={(x, y) for x in range(95, 105) for y in range(-8, 9)})   # a lake on the way
    net, group = {(0, 0)}, [(300, 20)]
    line = crew._big_line(group, net, free)
    assert line and all(kind == BIG_POLE for kind, _ in line)
    centres = [(q[0] + 1.0, q[1] + 1.0) for _, q in line]
    assert math.dist(centres[0], (0.5, 0.5)) <= REACH and math.dist(centres[-1], (300.5, 20.5)) <= REACH
    assert all(math.dist(a, b) <= BIG_WIRE for a, b in zip(centres, centres[1:]))
    assert len(line) <= 14                                    # vs ~43 small poles for 300 tiles
    for _, q in line:                                         # never on the lake
        assert not {(q[0] + i, q[1] + j) for i in (0, 1) for j in (0, 1)} & {(x, y) for x in range(95, 105)
                                                                               for y in range(-8, 9)}


def test_no_big_poles_without_the_research_or_steel():
    crew, free = _big(unlocked=())
    assert crew._big_line([(300, 20)], {(0, 0)}, free) is None
    crew, free = _big(steel=0)
    assert crew._big_line([(300, 20)], {(0, 0)}, free) is None


def test_a_belt_tier_is_picked_by_flow_and_every_check_knows_all_tiers():
    from brain import belts, gaps, purity
    from brain.world import FreeWorld as World
    assert belts.tier(set(), 500)["belt"] == "transport-belt"
    assert belts.tier({"fast-transport-belt"}, 500)["belt"] == "transport-belt"       # yellow carries it
    t = belts.tier({"fast-transport-belt"}, 1200)
    assert t["belt"] == "fast-transport-belt" and t["ug"] == "fast-underground-belt" and t["ug_reach"] == 7
    assert belts.tier(set(), 1200)["belt"] == "transport-belt"                         # the best researched one
    assert belts.count([("belt", (0, 0), "east"), ("ug", (1, 0), "east", "input"), ("ug", (5, 0), "east", "output")],
                       t) == {"fast-transport-belt": 1, "fast-underground-belt": 2}
    assert set(belts.BELT_NAMES + belts.UG_NAMES) <= set(purity.BELTS)
    assert set(belts.BELT_NAMES + belts.UG_NAMES + belts.SPLITTER_NAMES) <= set(World.BELT_ENTITIES)
    # a fast belt line is still one line to the hole finder
    ents = [{"name": "fast-transport-belt", "x": 0.5, "y": 0.5, "direction": "east", "feeds": [[1.5, 0.5]]}]
    assert gaps is not None and ents[0]["name"].endswith("transport-belt")
