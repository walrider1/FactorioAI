"""What halted run 20261119-202811 most: the courier carrying off a goal's materials, goals without a time limit,
steel by hand, hand-fed furnace rows whose coal strip got built over, and factories whose input ports their own
floor walled in."""

from types import SimpleNamespace

import pytest

from brain import assess
from brain.crew import Foreman
from brain.modules import ElectricMine, Site
from brain.world import BuildError, Preempted
from fab.design import port_access_problems, port_escapes
from fab.grid import Grid
from test_brain_gm import _crew
from test_fuel_and_links import GridWorld, _link_crew


# ------------------------------------------------------------------ the courier leaves the goal's materials alone
def test_what_the_goal_in_hand_gathers_is_reserved_and_the_courier_gives_only_the_rest():
    crew = _crew()
    with crew.hold({"iron-plate": 25}):
        with crew.hold({"iron-plate": 10, "steel-plate": 5}):      # nested holds add up
            assert crew.reserved == {"iron-plate": 35, "steel-plate": 5}
        assert crew.reserved == {"iron-plate": 25}
    assert crew.reserved == {}
    crew.kn.items = {"iron-plate": {"stack_size": 100}}
    crew.reserved = {"iron-plate": 30}
    assert crew._keep("iron-plate") >= 30                           # storage keeps it on hand too


def test_a_kit_reserves_the_iron_its_steel_is_smelted_from():
    crew = _crew()
    crew.kn.kit_plan = lambda kit, inv, **k: SimpleNamespace(raw={"steel-plate": 5, "iron-plate": 10})
    crew.ready_items = lambda: set()
    raw = crew._kit_raw({"pumpjack": 1})
    assert raw["steel-plate"] == 5 and raw["iron-plate"] == 10 + 25 and raw["pumpjack"] == 1


# ------------------------------------------------------------------ a time budget per goal
def test_a_goal_over_its_budget_is_stopped_at_a_safe_moment_and_counts_as_a_failure():
    crew = _crew()
    crew.w.activity, crew.w.steps = "smelting 5 steel-plate", ["make", "smelt"]
    now = {"t": 0}
    crew.w.now_tick = lambda: now["t"]
    crew.w.last_progress_tick = 0
    crew.current = {"goal": {"do": "build", "module": "pumpjack", "count": 1}, "tick": 0, "urgent": True}
    assert crew.goal_budget(crew.current["goal"]) == 15.0
    now["t"] = 14 * 3600
    crew.w.last_progress_tick = now["t"]                            # still making progress
    crew.checkpoint()
    now["t"] = 16 * 3600
    crew.w.last_progress_tick = now["t"]
    crew.w.steps = ["build"]                                        # placing: let it finish the piece
    crew.checkpoint()
    crew.w.steps = ["make", "smelt"]
    with pytest.raises(Preempted, match="over budget.*step make/smelt"):
        crew.checkpoint()
    assert crew.drop_current
    assert crew.goal_budget({"do": "wait", "seconds": 60}) == 4.0
    assert crew.goal_budget({"do": "research", "tech": "x"}) is None
    assert crew.goal_budget({"do": "replace", "site": 5, "with": {"do": "build", "module": "factory"}}) == 38.0


# ------------------------------------------------------------------ steel from a column, not by hand
def test_a_kit_that_needs_steel_builds_a_steel_column_first_once():
    crew = _crew()
    crew.kn.researched = {"steel-processing"}
    crew.base.powered_poles = lambda exclude=None: [(0, 0)]
    crew.ready_items = lambda: set()
    built = []
    crew.build = lambda g: built.append(g) or "built smelter_column"
    assert crew._steel_first() == "built smelter_column"
    assert built[0] == {"do": "build", "module": "smelter_column", "resource": "iron-plate", "count": 4}
    crew.ready_items = lambda: {"steel-plate"}                      # one makes steel now: no second column
    assert crew._steel_first() == ""
    crew.ready_items = lambda: set()

    def fail(g):
        raise BuildError("no clear ground")
    crew.build = fail
    assert crew._steel_first() == "" and "steel-plate" in crew.failed_suppliers
    crew.build = lambda g: built.append(g) or "x"
    assert crew._steel_first() == "" and len(built) == 1            # it just failed: not again for a while


# ------------------------------------------------------------------ a hand-fed row's coal strip stays free
def _hand_row(w, crew, cxs=(10, 13, 16), cy=10):
    em = ElectricMine()
    row = Site("electric_mine", {"resource": "iron-ore", "count": len(cxs), "into": "furnace", "fuel": "hand"},
               (13, 8))
    for cx in cxs:
        u = em.unit(cx, cy, "furnace")
        row.entities += [("electric-mining-drill", cx + 0.5, cy + 0.5, "north"),
                         ("stone-furnace", cx + 1.0, cy - 2.0, "north"),
                         ("inserter", cx + 0.5, cy - 3.5, "south"), ("iron-chest", cx + 0.5, cy - 4.5, "north")]
        w.used |= u["drill"] | u["rest"]
    return crew.base.add(row)


def test_a_hand_fed_rows_coal_strip_is_kept_free_and_its_belt_still_fits_there():
    w = GridWorld()
    w.lanes, w.keepout = set(), set()
    w.free_belt = lambda t: t not in w.used and t not in w.keepout
    crew = _link_crew(w, unlocked={"long-handed-inserter", "underground-belt"})
    row = _hand_row(w, crew)
    crew.reserve_strips()
    strip = set(map(tuple, crew.fuel_strips[row.id]))
    em = ElectricMine()
    belt = set(em.unit(10, 10, "furnace", fuel=True)["belt"])
    assert belt <= strip and belt <= w.lanes                        # no building goes there
    assert not any(crew._belt_free(t) for t in belt)                # and no other link's belt either
    crew._retrofit_row(row)                                         # its own belt does fit
    assert row.params["fuel"] == "belt" and row.id not in crew.fuel_strips and not (belt & w.lanes)


def test_a_row_whose_strip_was_taken_before_is_rebuilt_where_a_belt_fits():
    w = GridWorld()
    w.lanes, w.keepout = set(), set()
    crew = _link_crew(w, unlocked={"long-handed-inserter", "underground-belt"})
    crew._patch_full = {}
    row = _hand_row(w, crew, cxs=(40,))
    w.used.add((40, 4))                                             # built over before the strip was kept
    with pytest.raises(BuildError):
        crew._retrofit_row(row)
    assert crew.blocked_rows() == [row]
    alerts = assess.rebuild_alerts(crew)
    assert alerts and alerts[0]["key"].startswith(assess.CRITICAL) and alerts[0]["fix"] == {
        "do": "replace", "site": row.id, "force": True,
        "with": {"do": "build", "module": "electric_mine", "resource": "iron-ore", "count": 1, "into": "furnace"}}
    crew._patch_full = {"iron-ore": (30.0, crew._base_sig())}       # no room on the patch: keep feeding it
    assert assess.rebuild_alerts(crew) == []


# ------------------------------------------------------------------ a factory's input port must be reachable
def _design(cells_tiles, port, box):
    return SimpleNamespace(box=box, tiles=lambda: set(cells_tiles),
                           ports=lambda: [{"item": "iron-plate", "end": port[0], "approach": port[1],
                                           "chest": port[2], "inserter": port[3]}])


def test_an_input_port_walled_in_by_its_own_floor_fails_the_design_and_an_open_one_keeps_a_lane():
    seen = {(x, y) for x in range(-10, 30) for y in range(-10, 30)}
    grid = Grid(seen=seen)
    box = (0, 0, 20, 20)
    port = ((10, 10), (10, 11), (10, 8), (10, 9))                   # chest, inserter above; belt arrives from below
    ring = {(x, y) for x in range(4, 17) for y in range(4, 17) if max(abs(x - 10), abs(y - 10)) in (5, 6)}
    walled = _design(ring, port, box)                               # a 2-thick wall of machines all round it
    assert port_access_problems(grid, walled, underground=False)
    assert port_access_problems(grid, walled, underground=True) == []   # undergrounds jump a 2-tile wall
    thick = {(x, y) for x in range(2, 19) for y in range(2, 19) if max(abs(x - 10), abs(y - 10)) >= 3}
    assert port_access_problems(grid, _design(thick, port, box), underground=True)   # 6 thick: too far
    open_ = _design(set(), port, box)
    path = port_escapes(grid, open_, underground=False)[(10, 11)]
    assert path[0] == (10, 11) and (path[-1][1] > 21 or path[-1][0] < -1 or path[-1][0] > 21 or path[-1][1] < -1)
