"""Long pipe lines get pumps so no fluid segment overextends (Factorio 2.x: 320 tiles; run 20261120-211555: a
460-tile crude line from the oil field carried nothing to the blue science workshop)."""

from brain import pipes
from brain.pipes import NAME, Nets, add_pumps, line_from_entities, route_length


def _ug_line(x0: int, x1: int, y: int = 0) -> list[tuple]:
    """A route as the router builds it: from a consumer at x0 westward to a source near x1, pipe-to-ground pairs
    of span 10 (entrance open side facing back east, exit facing on west), a pipe at each end."""
    steps = [("pipe", (x0, y))]
    x = x0 - 1
    while x - 10 >= x1:
        steps += [("ptg", (x, y), "east"), ("ptg", (x - 10, y), "west")]
        x -= 11
    steps.append(("pipe", (x, y)))
    return steps


def _segments(steps):
    cut = [i for i, s in enumerate(steps) if s[0] == "pump"]
    b = [0] + cut + [len(steps) - 1]
    return [route_length(steps[x:y + 1]) for x, y in zip(b, b[1:])]


def test_a_long_line_gets_a_pump_every_200_tiles_and_no_segment_reaches_the_limit():
    steps = _ug_line(0, -470)
    assert route_length(steps) > pipes.PIPELINE_EXTENT
    new, changes = add_pumps(steps, lambda t: True, flow_to_start=True)
    assert len(changes) == 2
    assert all(seg < pipes.PIPELINE_EXTENT - 60 for seg in _segments(new)), _segments(new)
    for c in changes:                                  # the fluid runs east, toward the consumer at x = 0
        (a, b), facing = c["pump"], c["facing"]
        assert facing == "east" and b == (a[0] + 1, a[1])
        old, moved, side = c["move"]
        assert old == a and moved == (a[0] + 2, a[1]) and side == "west"       # still opens upstream
    i = next(i for i, s in enumerate(new) if s[0] == "pump")
    assert new[i - 1] == ("ptg", changes[-1]["move"][1], "west") or new[i + 1][0] == "ptg"
    pairs = [s for s in new if s[0] == "ptg"]
    assert len(pairs) % 2 == 0                         # every underground still has its partner


def test_short_lines_are_left_alone_and_a_blocked_spot_moves_the_pump_on():
    short = _ug_line(0, -150)
    assert add_pumps(short, lambda t: True) == (short, [])
    steps = _ug_line(0, -470)
    first = add_pumps(steps, lambda t: True)[1][0]["pump"][1]
    new, changes = add_pumps(steps, lambda t: t != first)
    assert changes and changes[0]["pump"][1] != first


def test_two_plain_pipes_in_a_straight_run_become_a_pump():
    steps = [("pipe", (x, 0)) for x in range(0, -260, -1)]
    new, changes = add_pumps(steps, lambda t: True, flow_to_start=False)       # the source at x = 0 this time
    [c] = changes
    (a, b) = c["pump"]
    assert c["facing"] == "west" and b == (a[0] - 1, 0) and c["move"] is None
    assert sum(1 for s in new if s[0] == "pipe") == 258


def test_pump_tiles_are_taken_for_later_routes():
    nets = Nets()
    nets.commit("crude-oil", [("pipe", (0, 0)), ("pump", ((1, 0), (2, 0)), "east"), ("pipe", (3, 0))])
    assert (1, 0) in nets.occupied() and nets.count()["pump"] == 1
    assert not pipes._pipe_ok(nets, "water", (2, 0), lambda t: True)


def test_a_laid_line_is_found_among_a_floor_s_nets_from_its_floor_end():
    steps = _ug_line(0, -470, y=5)
    ents = [("pipe", t[0] + 0.5, t[1] + 0.5, "north") if s == "pipe" else ("pipe-to-ground", t[0] + 0.5, t[1] + 0.5, d)
            for s, t, *rest in steps for d in (rest[0] if rest else "north",)]
    ents += [("pipe", x + 0.5, 0.5, "north") for x in range(-3, 4)]        # a short water net by the floor centre
    line = line_from_entities(ents, near=(0, 0))
    assert line[0][1] == (0, 5) and route_length(line) == route_length(steps)
    assert [s[0] for s in line] == [s[0] for s in steps]
    assert all(NAME.index(a[2]) == NAME.index(b[2]) for a, b in zip(line, steps) if a[0] == "ptg")


def test_a_long_line_waits_for_pumps_until_engine_units_can_be_had():
    """Run 20261120-211555: once pumps were planned, every oil workshop failed on "cannot hand-craft engine-unit"
    for 90 minutes. Now the line goes in without them, marked, and pumps follow once something makes engines."""
    from types import SimpleNamespace

    from brain.crew import Crew
    steps = _ug_line(0, -470)
    ents = [("pipe", t[0] + 0.5, t[1] + 0.5, "north") if s == "pipe" else ("pipe-to-ground", t[0] + 0.5, t[1] + 0.5, r[0])
            for s, t, *r in steps]
    site = SimpleNamespace(id=7, params={}, entities=ents, centre=(0, 0))
    can = {"pump": False}
    crew = SimpleNamespace(base=SimpleNamespace(sites=[site]), w=SimpleNamespace(blocked=lambda t: False),
                           can_obtain=lambda item, n=1: can[item])
    notes = Crew.split_pipelines(crew)
    assert site.params.get("needs_pumps") and not site.params.get("pumps_checked")
    assert notes and "once engine units are made" in notes[0]
    assert Crew.split_pipelines(crew) == []                    # said once; tried again later, quietly


def test_big_extracts_and_inserts_go_in_calls_the_api_accepts():
    from types import SimpleNamespace

    from brain.world import FreeWorld
    calls, chest = [], {"steel-plate": 12000}
    w = SimpleNamespace(MOVE_MAX=FreeWorld.MOVE_MAX, inventory={}, near=lambda x, y: None)

    def act(a):
        assert a["count"] <= 5000, a                                 # what the gateway would refuse
        calls.append(a["count"])
        if a["type"] == "extract":
            n = min(a["count"], chest["steel-plate"])
            chest["steel-plate"] -= n
            return {"ok": True, "data": {"extracted": n}}
        return {"ok": True, "data": {"inserted": a["count"]}}
    w.act = act
    assert FreeWorld.extract(w, 0, 0, "steel-plate", 12000) == 12000 and calls == [5000, 5000, 2000]
    calls.clear()
    assert FreeWorld.insert(w, 0, 0, "steel-plate", 7000) == 7000 and calls == [5000, 2000]
    assert w.inventory["steel-plate"] == 5000
