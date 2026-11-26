"""The shared agenda (docs/hybrid-plan.md, section 3) and the upkeep lane (section 4): each test is one fight or
stall seen in run 20261125-214645."""
from brain import upkeep
from brain.agenda import HOLD_DROPPED_MIN, agenda_of
from brain.modules import Site
from brain.overseer import apply_orders
from test_upkeep_outposts import _crew as _base_crew


def _crew():
    crew, w, minute = _base_crew()
    crew.urgent, crew.plan_queue, crew.closeout_queue, crew.current = [], [], [], None
    crew.preempt, crew.preempt_for, crew.preempt_force, crew.drop_current = None, None, False, False
    crew.overseer_forbid = []
    w.now_tick = lambda: minute["now"] * 3600
    return crew, w, minute

OUTPOST = {"do": "build", "module": "outpost", "resource": "iron-ore", "count": 4, "near_base": True}


def test_triage_cannot_requeue_what_the_overseer_dropped():
    crew, w, minute = _crew()
    ag = agenda_of(crew)
    ag.note_dropped(minute["now"], OUTPOST, "overseer", "a routing failure, not a shortage")
    ok, why = ag.propose(crew, {**OUTPOST, "reflex": "triage: source-short"}, "triage", front=True)
    assert not ok and "overseer dropped it" in why and crew.urgent == []
    ok, _ = ag.propose(crew, {"do": "feed", "site": 3, "item": "coal", "reflex": "rules: dry"}, "rules")
    assert ok                                                    # fuel and power are never held back
    minute["now"] += HOLD_DROPPED_MIN
    ok, _ = ag.propose(crew, {**OUTPOST, "reflex": "triage: source-short"}, "triage")
    assert ok                                                    # the hold runs out


def test_the_same_job_from_two_deciders_is_merged_and_both_are_named():
    crew, w, minute = _crew()
    ag = agenda_of(crew)
    assert ag.propose(crew, {"do": "relink", "site": 29, "item": "iron-plate", "reflex": "assessor: x"}, "assessor")[0]
    ok, why = ag.propose(crew, {"do": "relink", "site": 29, "item": "iron-plate", "reflex": "overseer: y"}, "overseer")
    assert not ok and why.startswith("already queued") and len(crew.urgent) == 1
    s = ag.summary(crew)
    assert "assessor, overseer" in s["urgent"][0]
    assert any("merged" in d for d in s["decisions"])


def test_a_job_just_done_is_not_queued_again_at_once():
    crew, w, minute = _crew()
    ag = agenda_of(crew)
    link = {"do": "link", "reflex": "foreman: taken up"}
    ag.note_done(minute["now"], link)
    assert not ag.propose(crew, link, "foreman")[0]


def test_the_overseer_does_not_order_a_second_research_station():
    crew, w, minute = _crew()
    crew.current = {"goal": {"do": "build", "module": "research_station", "count": 2}, "tick": 0}
    crew.plan_queue = []
    crew.overseer_forbid = []
    changed = apply_orders(crew, {"orders": [{"do": "build", "module": "research_station", "count": 6}]},
                           log=lambda *a: None)
    assert crew.urgent == [] and any("already queued or running" in c for c in changed)


def test_an_aged_turn_goes_to_the_plans_next_build_not_a_feed():
    crew, w, minute = _crew()
    crew.urgent = [{"do": "relink", "site": 1, "reflex": "assessor: x"}]
    crew.closeout_queue = []
    crew.plan_starved_since = 0
    green = {"do": "build", "module": "factory", "item": "logistic-science-pack", "per_minute": 15}
    plan = [{"do": "feed", "site": 4, "item": "coal"}, {"do": "remove", "site": 7}, green]
    goal, source, _ = upkeep.next_goal(crew, plan, now=30 * 3600)
    assert source == "aged" and goal is green and len(plan) == 2


def test_the_upkeep_lane_feeds_coal_inside_a_long_build():
    crew, w, minute = _crew()
    crew.current = {"goal": {"do": "build", "module": "factory", "item": "advanced-circuit"}, "tick": 0}
    crew.urgent = [{"do": "feed", "site": 61, "item": "coal", "count": 80, "reflex": "rules: out of fuel"},
                   {"do": "relink", "site": 9, "reflex": "assessor: x"}]
    w.steps, w.recorder = ["make"], ["build's own"]
    fed = []
    crew.feed = lambda site, item, n: fed.append((site, item, n)) or f"fed {n} {item} into site {site}"
    crew.log = lambda m: None
    crew.urgent.insert(1, {"do": "feed", "site": 68, "item": "coal", "count": 200, "reflex": "rules: dry row"})
    crew.request_pause("rules: site 61 out of fuel")
    crew._upkeep_lane()
    assert fed == [(61, "coal", 80), (68, "coal", 200)] and [g["do"] for g in crew.urgent] == ["relink"]
    fed.clear()
    fed.append("first round")
    assert w.recorder == ["build's own"] and crew.preempt is None   # recorder restored; no pause left for it
    crew.urgent.insert(0, {"do": "feed", "site": 62, "item": "coal", "reflex": "rules: dry"})
    w.steps = ["build"]                                          # placing: never in the middle of that
    crew._upkeep_lane()
    assert len(fed) == 1
    w.steps = ["make"]
    crew._lane_log = [(minute["now"], crew.LANE_SHARE_MIN)]      # its slice is used up for now
    crew._upkeep_lane()
    assert len(fed) == 1



def test_research_without_labs_builds_the_labs_first():
    crew, w, minute = _crew()
    built = []
    crew.kn.tech_path = lambda t: [t]
    crew.kn.is_trigger = lambda t: False
    crew.log = lambda m: None

    def build(goal):
        built.append(goal)
        crew.base.add(Site("labs", {"count": 2}, (0, 0)))
        raise RuntimeError("stop here: the labs stand")
    crew.build = build
    try:
        crew.research("automation")
    except RuntimeError:
        pass
    assert built == [{"do": "build", "module": "labs", "count": 2}]
