"""Frontier aging and fresh stats (run 20261120-180703: the green-science build waited 170 minutes at queue
position 5 behind everyone's fixes and never ran; the overseer called green science locked 18 minutes after it
was researched, because research and rates were only read between goals)."""

from types import SimpleNamespace

from brain import upkeep
from brain.overseer import apply_orders
from brain.world import FreeWorld
from test_brain_gm import _crew

MIN = 3600


def _queues(urgent=(), closeout=()):
    return SimpleNamespace(urgent=list(urgent), closeout_queue=list(closeout), plan_starved_since=None)


GREEN = {"do": "build", "module": "factory", "item": "logistic-science-pack", "per_minute": 15}
FIX = {"do": "link", "reflex": "assessor: x"}
FEED = {"do": "feed", "site": 3, "item": "coal", "count": 10, "reflex": "rules: out of fuel"}


def test_fixes_go_first_until_the_plan_has_waited_too_long():
    crew, plan = _queues(urgent=[dict(FIX) for _ in range(9)], closeout=[{"do": "replace", "site": 4}]), [GREEN]
    goal, source, _ = upkeep.next_goal(crew, plan, now=80 * MIN)
    assert source == "urgent" and crew.plan_starved_since == 80 * MIN       # the plan's clock starts here
    goal, source, waited = upkeep.next_goal(crew, plan, now=87 * MIN)
    assert source == "urgent" and round(waited) == 7
    goal, source, waited = upkeep.next_goal(crew, plan, now=88 * MIN)        # progress first: 8 minutes, not 20
    assert source == "aged" and goal is GREEN and round(waited) == 8
    assert crew.plan_starved_since is None and plan == []                   # it ran: the clock stops


def test_only_quick_fuel_fixes_still_go_before_an_aged_goal_and_waits_are_skipped():
    crew = _queues(urgent=[dict(FIX), dict(FEED)])
    plan = [{"do": "wait", "seconds": 300}, GREEN]
    crew.plan_starved_since = 0
    goal, source, _ = upkeep.next_goal(crew, plan, now=25 * MIN)
    assert source == "urgent" and goal["do"] == "feed"                      # seconds of work, keeps plates coming
    goal, source, _ = upkeep.next_goal(crew, plan, now=25 * MIN)
    assert source == "aged" and goal is GREEN and plan == []                # the wait was pointless


def test_research_goals_pass_through_without_ending_the_wait():
    crew = _queues(urgent=[dict(FIX)] * 3)
    plan = [{"do": "research", "tech": "logistics"}, GREEN]
    crew.plan_starved_since = 0
    goal, source, _ = upkeep.next_goal(crew, plan, now=30 * MIN)
    assert source == "aged" and goal["do"] == "research" and crew.plan_starved_since == 0
    goal, source, _ = upkeep.next_goal(crew, plan, now=30 * MIN)
    assert source == "aged" and goal is GREEN


def test_an_empty_plan_or_waits_only_never_age():
    crew = _queues(urgent=[dict(FIX)])
    crew.plan_starved_since = 0
    goal, source, _ = upkeep.next_goal(crew, [{"do": "wait", "seconds": 30}], now=60 * MIN)
    assert source == "urgent" and crew.plan_starved_since is None
    crew = _queues()
    assert upkeep.next_goal(crew, [], now=0)[1] == "idle"
    plan = [GREEN]
    assert upkeep.next_goal(crew, plan, now=0) == (GREEN, "plan", 0.0)


def test_an_aged_goal_is_paused_only_for_base_wide_trouble():
    crew = SimpleNamespace(current={"goal": {"do": "build", "module": "electric_mine"}, "urgent": False,
                                    "aged": True}, _minute=lambda: 100.0)
    assert not upkeep.may_interrupt(crew, ["fuel-site-12"])                 # not even once in a while
    assert upkeep.may_interrupt(crew, ["power-capacity"])


def test_the_overseer_cannot_pause_or_drop_an_aged_goal_but_its_orders_queue_behind_it():
    crew = _crew()
    crew.current = {"goal": dict(GREEN), "tick": 0, "aged": True}
    changed = apply_orders(crew, {"assessment": "labs idle", "interrupt": True, "drop_current": True,
                                  "orders": [{"do": "feed", "site": 16, "item": "coal", "count": 40}]},
                           log=lambda *a: None)
    assert not crew.preempt and not crew.drop_current
    assert any("protected" in c for c in changed) and crew.urgent[0]["site"] == 16
    crew.current = {"goal": dict(GREEN), "tick": 0}                         # not aged: paused as before
    apply_orders(crew, {"assessment": "x", "interrupt": True}, log=lambda *a: None)
    assert crew.preempt


def test_fresh_stats_reads_again_only_when_the_last_read_is_a_game_minute_old():
    reads = []
    w = SimpleNamespace(STATS_MAX_AGE_S=60.0, stats={}, tick=0)
    w.now_tick = lambda: w.tick

    def read_stats(minutes=5):
        reads.append(minutes)
        w.stats_tick, w.stats_window = w.tick, minutes
        return w.stats
    w.read_stats = read_stats
    FreeWorld.fresh_stats(w)                                    # never read: read now
    w.tick = 30 * 60
    FreeWorld.fresh_stats(w)                                    # 30 s old: kept
    assert reads == [5]
    w.tick = 61 * 60
    FreeWorld.fresh_stats(w)
    w.stats_window = 1                                          # a crew wait read a 1-minute window: not the
    FreeWorld.fresh_stats(w)                                    # planners' 5-minute rates, read again
    assert reads == [5, 5, 5]


def test_a_build_still_placing_runs_past_its_budget_up_to_a_hard_cap():
    crew = _crew()
    crew.log = lambda *a: None
    now = {"t": 0}
    crew.w.now_tick = lambda: now["t"]
    crew.w.steps, crew.w.activity = ["build", "make"], "crafting gears"
    crew.current = {"goal": {"do": "build", "module": "workshop", "item": "chemical-science-pack"}, "tick": 0}
    now["t"] = 45 * MIN                                       # over the workshop's 40 minutes
    crew.w.last_place_tick = 42 * MIN                         # placed something 3 minutes ago
    assert crew._over_budget() is None and crew.current["overrun"] == 45.0
    crew.w.last_place_tick = 30 * MIN                         # nothing placed for 15 minutes: dropped
    assert crew._over_budget().startswith("over budget")
    crew.w.last_place_tick = now["t"] = 101 * MIN             # past 2.5 x 40 minutes: dropped whatever it does
    assert crew._over_budget().startswith("over budget")
    crew.w.steps = ["make"]                                   # still gathering its kit, nothing placed yet
    now["t"], crew.w.last_place_tick = 45 * MIN, 44 * MIN
    assert crew._over_budget().startswith("over budget")
