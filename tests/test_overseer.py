"""The overseer: a deep-thinking model that reads everything every few game minutes and whose orders run first
(run 20261119-202811: drills idle for an hour behind unfuelled furnaces, 36 unfed smelting columns, power built
for a load that never came; nobody in the bot saw it)."""

from types import SimpleNamespace

import pytest

from brain.assess import Supervisor
from brain.modules import Site
from brain.overseer import apply_orders, forbidden, full_report, machine_census
from brain.world import BuildError
from test_brain_gm import _crew, _mine


def _base_with_idle_drills(crew):
    mine = _mine(crew, "iron-ore", "iron-plate")
    mine.census = {"electric-mining-drill": {"waiting_for_space_in_destination": 3, "working": 1},
                   "stone-furnace": {"no_fuel": 3, "working": 1}}
    return mine


def test_it_sees_every_machine_by_state_and_how_much_of_each_type_stands_still():
    crew = _crew()
    crew.w.steps, crew.w.activity = [], "walking"
    mine = _base_with_idle_drills(crew)
    per_site, idle = machine_census(crew)
    assert per_site == [{"site": mine.id, "kind": "burner_mine", "machines": mine.census}]
    assert idle["electric-mining-drill"] == {"working": 1, "not_working": 3, "share_idle": 0.75}
    rep = full_report(crew, {"base": ["short"], "game_minute": 12}, [], [{"assessment": "earlier"}])
    assert "base" not in rep and rep["sites"][0]["id"] == mine.id and rep["idle"] == idle
    assert rep["previous_overseer"] == [{"assessment": "earlier"}] and "power" in rep and "queues" in rep


def test_its_orders_go_first_and_it_can_stop_drop_forbid_and_replan():
    crew = _crew()
    crew.urgent = [{"do": "feed", "site": 3, "item": "coal", "count": 10, "reflex": "rules: x"}]
    crew.plan_queue = queue = [{"do": "build", "module": "smelter_column", "resource": "iron-ore", "count": 7}]
    crew.current = {"goal": {"do": "build", "module": "outpost", "resource": "iron-ore"}, "tick": 0}
    crew.strategist = SimpleNamespace(doctrine="old")
    changed = apply_orders(crew, {
        "assessment": "an outpost goal keeps building unfed columns; drills wait on unfuelled furnaces",
        "orders": [{"do": "feed", "site": 17, "item": "coal", "count": 160}, {"do": "remove", "site": 46,
                                                                             "force": True},
                   {"do": "launch"}],                                       # not a goal: dropped
        "interrupt": True, "drop_current": True,
        "forbid": [{"match": {"do": "build", "module": "smelter_column"}, "minutes": 30, "why": "36 unfed"}],
        "plan": [{"do": "link"}], "plan_why": "the columns are the mistake", "priority": ["17", "coal"],
        "doctrine": "new", "guidance": "fuel first"},
        log=lambda *a: None)
    # the rule floor's fuel fix first, then its orders, before anyone else's
    assert [g["site"] for g in crew.urgent] == [3, 17, 46]
    assert crew.urgent[1]["reflex"].startswith("overseer:")
    assert crew.preempt and crew.drop_current
    assert queue == [{"do": "link"}] and crew.plan_queue is queue and crew.overseer_plan_tick == 0
    assert crew.foreman.priority == ["17", "coal"] and crew.strategist.doctrine == "new"
    assert crew.overseer_guidance["text"] == "fuel first" and len(changed) >= 6
    crew.urgent.append({"do": "link", "reflex": "assessor: y"})
    apply_orders(crew, {"clear_urgent": True, "allow": ["all"]}, log=lambda *a: None)
    assert [g["reflex"] for g in crew.urgent] == ["rules: x"] and crew.overseer_forbid == []   # fuel fix kept
    apply_orders(crew, {"plan": [{"do": "wait"}]}, log=lambda *a: None)    # no plan_why: the plan stays
    assert queue == [{"do": "link"}]


def test_a_forbidden_goal_is_refused_even_when_another_build_would_start_it():
    crew = _crew()
    apply_orders(crew, {"forbid": [{"match": {"do": "build", "module": "smelter_column"}, "minutes": 10,
                                    "why": "unfed columns"}]}, log=lambda *a: None)
    goal = {"do": "build", "module": "smelter_column", "resource": "iron-ore", "count": 7, "reflex": "x"}
    assert forbidden(crew, goal)["why"] == "unfed columns"
    assert forbidden(crew, {"do": "build", "module": "factory", "item": "x"}) is None
    with pytest.raises(BuildError, match="overseer forbids"):
        crew.build({"do": "build", "module": "smelter_column", "resource": "iron-ore", "count": 7})
    with pytest.raises(BuildError, match="overseer forbids"):
        crew.run_goal(goal)
    crew.w.now_tick = lambda: 11 * 3600                                     # its time is up
    assert forbidden(crew, goal) is None and crew.overseer_forbid == []


def test_the_supervisor_asks_it_in_the_background_and_carries_out_its_word_at_a_safe_point():
    crew = _crew()
    crew.w.steps, crew.w.activity = [], "crafting"
    _base_with_idle_drills(crew)
    asked = []

    class Overseer:
        name, cost_usd, calls, period_min, history = "fake", 0.0, 0, 7.0, []

        def next_gap_min(self, d):
            return 5.0

        def decide(self, rep):
            asked.append(rep)
            return {"assessment": "drills idle behind furnaces with no fuel",
                    "orders": [{"do": "feed", "site": 1, "item": "coal", "count": 120}], "guidance": "coal first"}

    from brain.overseer import FIRST_LOOK_MIN
    ov = Overseer()
    sup = Supervisor(None, log=lambda *a: None, overseer=ov,
                     report_fn=lambda: ({"game_minute": 0}, [{"goal": {"do": "feed", "reflex": "overseer: x"},
                                                              "ok": True, "result": "fed"}]))
    now = {"t": 0}
    crew.w.now_tick = lambda: now["t"]
    sup.tick(crew, force=True)
    assert sup.overseer_pending is None                                     # minute 0: nothing to oversee yet
    now["t"] = FIRST_LOOK_MIN * 3600
    sup.tick(crew, force=True)
    assert sup.overseer_pending is not None
    sup.overseer_pending.result(timeout=5)
    sup.tick(crew, force=True)
    assert crew.urgent[0]["do"] == "feed" and crew.overseer_guidance["text"] == "coal first"
    assert asked[0]["idle"]["electric-mining-drill"]["not_working"] == 3
    assert asked[0]["results_of_your_orders"][0]["result"] == "fed"
    assert sup.overseer_next_tick == now["t"] + 5.0 * 3600 and ov.history[-1]["assessment"].startswith("drills idle")
    sup.tick(crew, force=True)
    assert sup.overseer_pending is None                                     # not again before its next look
    sup.close()


def test_the_real_overseer_prompt_formats():
    """It crashed at start (a JSON example in the prompt read as a format field): build it for real."""
    from brain.overseer import Overseer
    ov = Overseer(client=object(), model="gpt-6.1-sol")
    assert '{"match": {"do": "build"' in ov.system and '"assessment"' in ov.system and "{goals}" not in ov.system


def test_a_goal_that_does_nothing_for_long_is_dropped_by_the_stall_guard():
    from brain.crew import STALL_MIN
    from brain.world import Preempted
    crew = _crew()
    crew.w.steps, crew.w.activity = [], "walking to a coal chest"
    crew.w.last_progress_tick = 0
    now = {"t": 0}
    crew.w.now_tick = lambda: now["t"]
    crew.current = {"goal": {"do": "replace", "site": 5}, "tick": 0, "urgent": True}
    now["t"] = int((STALL_MIN - 1) * 3600)
    crew.checkpoint()                                             # not yet
    now["t"] = int((STALL_MIN + 1) * 3600)
    with pytest.raises(Preempted, match="stalled"):
        crew.checkpoint()                                         # urgent goals too: a loop is a loop
    assert crew.drop_current
    crew.drop_current = False
    crew.current = {"goal": {"do": "wait", "seconds": 1200}, "tick": 0}
    now["t"] = int((STALL_MIN + 3) * 3600)                         # past the stall limit, inside its 23-min budget
    crew.checkpoint()                                             # waiting is what a wait goal does


def test_no_walking_for_coal_with_a_full_inventory_that_storage_cannot_empty():
    crew = _crew()
    crew.w.inventory = {"copper-plate": 8000}                     # 80 full stacks
    crew.kn.items = {"copper-plate": {"stack_size": 100}, "coal": {"stack_size": 50}}
    crew.ensure_space = lambda *a, **k: 0                         # storage takes nothing
    crew.base.add(Site("electric_mine", {"resource": "coal", "count": 2, "into": "chest"}, (0, 0),
                       chests=[(0.5, 0.5, "coal")]))
    tried = []
    crew.w.extract = lambda *a: tried.append(a) or 0
    crew.w.fuel_units = lambda: 0
    assert crew.no_room("coal")
    crew.get_fuel(30)
    assert tried == [] and any("no room for coal" in p for p in crew.foreman.problems)
    crew.w.inventory = {"copper-plate": 7900}                     # one slot free: go and get it
    assert not crew.no_room("coal")


def test_resume_keeps_the_runs_flags_and_never_grows_them():
    """run 20261119-202811: a resume rewrote launch.json with its own short command line, and the next
    resume started the default (crew) bot on a brain save."""
    import argparse
    from arena import cli
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("local-run")
    for f in ("--backend", "--task", "--bot", "--strategist", "--clock-speed", "--resume", "--monitor",
              "--overseer-minutes"):
        p.add_argument(f)
    p.add_argument("--no-overseer", action="store_true")
    stored = ["local-run", "--backend", "factorio", "--task", "freeplay-open", "--bot", "brain",
              "--strategist", "openai", "--clock-speed", "2"]
    out = cli._merge_argv(ap, stored, ["local-run", "--resume", "run:save", "--clock-speed", "3", "--no-overseer"])
    assert out[:9] == ["local-run", "--backend", "factorio", "--task", "freeplay-open", "--bot", "brain",
                       "--strategist", "openai"]
    assert out.count("--clock-speed") == 1 and out[out.index("--clock-speed") + 1] == "3" and "--no-overseer" in out
    again = cli._merge_argv(ap, out, stored + ["--resume", "run:save2"])  # the monitor's Resume: all flags again
    assert again.count("--bot") == 1 and len([a for a in again if a.startswith("--")]) == len(
        [a for a in out if a.startswith("--")])


def test_its_interrupt_stops_an_urgent_goal_at_the_next_moment_nothing_is_half_placed():
    """run 20261119-202811: its pauses waited out a 28-minute urgent replace and an 18-minute link pass."""
    from brain.world import Preempted
    crew = _crew()
    crew.w.activity = "crafting"
    crew.w.last_progress_tick = 0
    crew.current = {"goal": {"do": "replace", "site": 5}, "tick": 0, "urgent": True}
    apply_orders(crew, {"assessment": "this replace is the mistake", "drop_current": True}, log=lambda *a: None)
    crew.w.steps = ["build"]                                       # placing: wait
    crew.checkpoint()
    crew.w.steps = ["build", "make"]                               # crafting for the next part: stop here
    with pytest.raises(Preempted):
        crew.checkpoint()
    assert crew.drop_current and not crew.preempt_force
    # the assessor's word alone never pauses an urgent goal (as before)
    crew.preempt, crew.preempt_force = "assessor: x", False
    crew.checkpoint()


def test_the_assessor_holds_back_while_the_overseers_orders_run_and_forbids_stay_few():
    from brain.overseer import FORBID_MAX
    crew = _crew()
    crew.urgent = [{"do": "feed", "site": 17, "item": "coal", "count": 160, "reflex": "overseer: fuel first"}]
    sup = Supervisor(None, log=lambda *a: None)
    sup.apply(crew, {"note": "again", "urgent_goals": [{"do": "remove", "site": 39}], "interrupt": True}, who="assessor")
    assert [g["do"] for g in crew.urgent] == ["feed"] and not crew.preempt
    sup.apply(crew, {"note": "fuel", "urgent_goals": [{"do": "feed", "site": 20, "item": "coal", "count": 40}],
                     "keys": []}, who="rules")
    assert [g["do"] for g in crew.urgent] == ["feed", "feed"]      # the rule floor still acts
    for k in range(FORBID_MAX + 4):
        apply_orders(crew, {"forbid": [{"match": {"do": "replace", "site": k}, "minutes": 30}]}, log=lambda *a: None)
    assert len(crew.overseer_forbid) == FORBID_MAX and crew.overseer_forbid[-1]["match"]["site"] == FORBID_MAX + 3


def test_a_spent_cost_cap_is_named_so_the_bot_saves_and_stops():
    from brain.bot import spent_budgets
    parts = {"strategist": SimpleNamespace(max_cost=5.0, cost_usd=1.2),
             "overseer": SimpleNamespace(max_cost=5.0, cost_usd=5.01), "assessor": None,
             "designer": SimpleNamespace(max_cost=0, cost_usd=3.0)}           # no cap: never "spent"
    assert spent_budgets(parts) == ["overseer $5.01/$5.00"]
    parts["overseer"].cost_usd = 4.0
    assert spent_budgets(parts) == []


def test_a_pause_asked_for_one_goal_never_stops_the_next():
    """run 20261120-023103: "drop the goal in hand" outlived the goal it meant (which ended on its own) and killed
    the next one, the overseer's own iron expansion."""
    from brain.world import Preempted
    crew = _crew()
    crew.w.steps, crew.w.activity = [], "crafting"
    crew.w.last_progress_tick = 0
    crew.current = {"goal": {"do": "build", "module": "factory", "item": "transport-belt"}, "tick": 0}
    apply_orders(crew, {"assessment": "x", "drop_current": True}, log=lambda *a: None)
    assert crew.preempt and crew.preempt_for == 0
    crew.current = {"goal": {"do": "build", "module": "electric_mine"}, "tick": 600}   # the next goal began
    crew.checkpoint()                                             # no pause, no drop
    assert crew.preempt is None and not crew.drop_current and not crew.preempt_force
    apply_orders(crew, {"assessment": "y", "interrupt": True}, log=lambda *a: None)    # one for this goal
    with pytest.raises(Preempted):
        crew.checkpoint()
    assert not crew.drop_current


def test_the_overseer_looks_every_ten_minutes_never_twice_in_a_row_and_waits_for_its_orders():
    from brain.overseer import MIN_GAP_MIN, Overseer
    assert Overseer(client=object(), model="m").period_min == 10.0
    crew = _crew()
    crew.w.steps, crew.w.activity = [], "x"
    now = {"t": 100 * 3600}
    crew.w.now_tick = lambda: now["t"]
    sup = Supervisor(None, log=lambda *a: None, overseer=SimpleNamespace(period_min=10.0, history=[], cost_usd=0),
                     report_fn=lambda: ({}, []))
    sup.overseer_last_tick, sup.overseer_next_tick = now["t"] - 60, now["t"] + 9 * 3600
    sup.overseer_soon(now["t"])                                   # the watchdog: not 1 minute after its answer
    assert sup.overseer_next_tick == sup.overseer_last_tick + MIN_GAP_MIN * 3600
    crew.urgent = [{"do": "link", "reflex": "overseer: x"}]       # its orders have not started
    sup.overseer_next_tick = now["t"]
    sup._oversee(crew)
    assert sup.overseer_pending is None and sup.overseer_next_tick == now["t"] + 2 * 3600
    sup.close()


def test_idle_labs_never_spend_packs_on_techs_off_the_rocket_path():
    import json as _j
    from arena import catalog
    from brain.crew import Crew
    from brain.knowledge import Knowledge
    from test_brain_gm import StubWorld
    kn = Knowledge(_j.loads(_j.dumps(catalog.get().data)))
    kn.learn_research(["steam-power", "electronics", "automation-science-pack", "automation", "logistics",
                       "logistic-science-pack", "steel-processing", "electric-mining-drill", "radar"])
    crew = Crew(StubWorld(kn), log=lambda *a: None)
    sent = []
    crew.w.act = lambda a: (sent.append(a), {"ok": True, "data": {"current": a["technologies"][0]}})[1]
    crew.base.add(Site("labs", {"count": 2}, (0, 0)))
    crew.base.add(Site("factory", {"item": "automation-science-pack"}, (5, 5), chests=[(5, 5, "automation-science-pack")]))
    crew.target_techs = ["oil-processing"]                         # its path needs green packs: not ready
    pick = crew.auto_research()
    assert pick is None or pick in set(kn.tech_path("rocket-silo"))
    assert pick not in ("gun-turret", "lamp")


def test_research_station_ports_keep_a_straight_lane_out():
    from brain.modules import ResearchStation
    lanes = ResearchStation.port_lanes(0, 0)
    assert (-4, 0) in lanes and (-9, 0) in lanes                  # west ports: lanes run west
    assert (0, -4) in lanes and (0, 6) in lanes                   # north and south ports: north and south
    assert not any(0 <= x <= 2 and 0 <= y <= 2 for x, y in lanes)  # never over the head lab


# ---------------------------------------------------------------- run 20261120-084046: it overstepped
def test_it_never_forbids_the_close_out_and_forbidden_goals_leave_the_queues():
    """It banned red science's own gear and cable factories; the planners kept planning them (four replans)."""
    from brain.overseer import FORBID_MAX
    crew = _crew()
    gear = {"do": "build", "module": "factory", "item": "iron-gear-wheel", "reflex": "close-out: tier 1"}
    crew.closeout_queue = [gear]
    crew.plan_queue = [{"do": "build", "module": "factory", "item": "copper-cable"}, {"do": "link"}]
    changed = apply_orders(crew, {"forbid": [
        {"match": {"do": "build", "module": "factory", "item": "iron-gear-wheel"}},
        {"match": {"do": "build", "module": "factory", "item": "copper-cable"}, "minutes": 999}]}, log=lambda *a: None)
    assert [f["match"].get("item") for f in crew.overseer_forbid] == ["copper-cable"]
    assert crew.overseer_forbid[0]["until"] == 30 * 3600                   # capped
    assert any("close-out needs" in c for c in changed) and crew.plan_queue == [{"do": "link"}]
    assert FORBID_MAX == 3


def test_a_forced_remove_still_meets_the_crews_guard():
    """It force-removed the starter iron smelter; iron then ran short."""
    crew = _crew()
    iron = _mine(crew, "iron-ore", "iron-plate")
    changed = apply_orders(crew, {"orders": [{"do": "remove", "site": iron.id, "force": True}]}, log=lambda *a: None)
    assert "force" not in crew.urgent[0] and any("last working source" in c for c in changed)
    _mine(crew, "iron-ore", "iron-plate")                                  # another source: force passes
    crew.urgent.clear()
    apply_orders(crew, {"orders": [{"do": "remove", "site": iron.id, "force": True}]}, log=lambda *a: None)
    assert crew.urgent[0]["force"] is True


def test_it_does_not_order_what_runs_or_waits_and_at_most_three():
    """Minute 1: it ordered the coal mine the strategist was already building, a duplicate site."""
    crew = _crew()
    coal = {"do": "build", "module": "burner_mine", "resource": "coal", "count": 2}
    crew.current = {"goal": dict(coal), "tick": 0}
    crew.plan_queue = [{"do": "build", "module": "labs", "count": 2}, {"do": "link"}]
    apply_orders(crew, {"orders": [coal, {"do": "build", "module": "labs", "count": 2}, {"do": "feed", "site": 1},
                                   {"do": "feed", "site": 2}, {"do": "feed", "site": 3}]}, log=lambda *a: None)
    assert [g.get("module") or g.get("site") for g in crew.urgent] == ["labs", 1, 2]
    assert crew.plan_queue == [{"do": "link"}]                             # moved up, not done twice


def test_a_build_with_work_in_it_is_paused_not_dropped():
    crew = _crew()
    now = {"t": 0}
    crew.w.now_tick = lambda: now["t"]
    crew.current = {"goal": {"do": "build", "module": "factory", "item": "logistic-science-pack"}, "tick": 0}
    now["t"] = 5 * 3600
    changed = apply_orders(crew, {"assessment": "x", "drop_current": True}, log=lambda *a: None)
    assert crew.preempt and not crew.drop_current and any("paused instead" in c for c in changed)
    crew.clear_pause()
    crew.current = {"goal": {"do": "link"}, "tick": now["t"]}               # a link may still be dropped
    apply_orders(crew, {"assessment": "x", "drop_current": True}, log=lambda *a: None)
    assert crew.drop_current


def test_an_urgent_fix_gets_past_its_orders_and_rule_fixes_go_first():
    """'Replace the depleted iron mine' was held back five times while iron ran out."""
    crew = _crew()
    crew.urgent = [{"do": "build", "module": "research_station", "reflex": "overseer: x"}]
    sup = Supervisor(None, log=lambda *a: None)
    sup.alerts = [{"level": "urgent", "key": "depleted-4", "what": "site 4 depleted"}]
    sup.apply(crew, {"note": "replace it", "urgent_goals": [{"do": "replace", "site": 4}, {"do": "link"}]},
              who="assessor")
    assert [g["do"] for g in crew.urgent] == ["replace", "build"]          # one, ahead of the orders
    sup.apply(crew, {"note": "fuel", "urgent_goals": [{"do": "feed", "site": 9, "item": "coal", "count": 80}],
                     "keys": []}, who="rules")
    assert [g["do"] for g in crew.urgent] == ["feed", "replace", "build"]
    sup.alerts = []
    sup.apply(crew, {"note": "more", "urgent_goals": [{"do": "remove", "site": 3}]}, who="assessor")
    assert len(crew.urgent) == 3                                           # nothing urgent: held back as before


# ---------------------------------------------------------------- run 20261120-092648: fighting the run
def test_its_drop_of_someone_elses_fix_is_a_drop_even_with_work_in_it():
    """The assessor's 2.1/min gear factory, paused instead of dropped, came back in front of every new plan."""
    crew = _crew()
    now = {"t": 5 * 3600}
    crew.w.now_tick = lambda: now["t"]
    crew.current = {"goal": {"do": "build", "module": "factory", "item": "iron-gear-wheel", "per_minute": 2.1,
                             "reflex": "assessor: gear shortage"}, "tick": 0}
    apply_orders(crew, {"assessment": "premature", "drop_current": True}, log=lambda *a: None)
    assert crew.drop_current


def test_the_assessor_moves_a_queued_job_up_instead_of_queueing_it_twice():
    crew = _crew()
    crew.w.research = {"current": None}
    crew.current = {"goal": {"do": "build", "module": "labs", "count": 2}, "tick": 0}
    copper = {"do": "build", "module": "burner_mine", "resource": "copper-ore", "count": 2, "into": "furnace"}
    gear = {"do": "build", "module": "factory", "item": "iron-gear-wheel", "per_minute": 6.0, "reflex": "close-out: t1"}
    crew.plan_queue = [{"do": "research", "tech": "automation"}, dict(copper)]
    crew.closeout_queue = [gear]
    sup = Supervisor(None, log=lambda *a: None)
    sup.apply(crew, {"note": "short", "urgent_goals": [
        {**copper, "count": 3},                                            # in the plan: moved up, its own size
        {"do": "build", "module": "factory", "item": "iron-gear-wheel", "per_minute": 2.1},   # the close-out's
        {"do": "build", "module": "labs", "count": 4}]}, who="assessor")   # running now: left alone
    assert crew.urgent == [copper, gear] and crew.plan_queue == [{"do": "research", "tech": "automation"}]
    assert crew.closeout_queue == []
    sup.apply(crew, {"note": "labs idle", "urgent_goals": [{"do": "research", "tech": "electric-mining-drill"}]},
              who="assessor")
    assert len(crew.urgent) == 2                                           # the plan's research comes first


def test_the_assessor_pauses_only_for_an_urgent_alert_and_replans_rarely():
    crew = _crew()
    crew.w.steps, crew.w.activity = [], "x"
    crew.current = {"goal": {"do": "build", "module": "labs", "count": 2}, "tick": 0}
    sup = Supervisor(None, log=lambda *a: None)
    sup.apply(crew, {"note": "stone low", "urgent_goals": [{"do": "build", "module": "burner_mine",
                                                            "resource": "stone", "count": 2}],
                     "interrupt": True, "replan": True}, who="assessor")
    assert not crew.preempt and sup.replan_reason and len(crew.urgent) == 1
    sup.take_replan()
    sup.apply(crew, {"note": "again", "replan": True}, who="assessor")
    assert sup.replan_reason is None                                       # asked minutes ago
    sup.alerts = [{"level": "urgent", "key": "power-capacity", "what": "power short"}]
    sup.apply(crew, {"note": "power", "urgent_goals": [{"do": "build", "module": "steam_power", "engines": 1}],
                     "interrupt": True}, who="assessor")
    assert crew.preempt
