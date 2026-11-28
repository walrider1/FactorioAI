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
    w.inventory = {"coal": 500}
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
    crew.base.add(Site("steam_power", {"engines": 1}, (9, 9)))
    try:
        crew.research("automation")
    except RuntimeError:
        pass
    assert built == [{"do": "build", "module": "labs", "count": 2}]


def test_labs_first_builds_their_power_first_when_there_is_none():
    crew, w, minute = _crew()
    built = []
    crew.kn.tech_path = lambda t: [t]
    crew.kn.is_trigger = lambda t: False
    crew.log = lambda m: None

    def build(goal):
        built.append(goal["module"])
        if goal["module"] == "labs":
            crew.base.add(Site("labs", {"count": 2}, (0, 0)))
            raise RuntimeError("stop here")
    crew.build = build
    try:
        crew.research("automation")
    except RuntimeError:
        pass
    assert built == ["steam_power", "labs"]


def test_the_lane_never_goes_fetching_coal():
    crew, w, minute = _crew()
    crew.current = {"goal": {"do": "build", "module": "factory", "item": "advanced-circuit"}, "tick": 0}
    crew.urgent = [{"do": "feed", "site": 61, "item": "coal", "count": 80, "reflex": "rules: out of fuel"}]
    w.steps, w.recorder, w.inventory = ["make"], None, {"coal": 2}
    fed = []
    crew.feed = lambda site, item, n: fed.append(n) or "fed"
    crew.log = lambda m: None
    crew._upkeep_lane()
    assert fed == [] and len(crew.urgent) == 1                   # too little on hand: it waits for the goal to end
    w.inventory = {"coal": 30}
    crew._upkeep_lane()
    assert fed == [30]                                           # what it carries, not a trip for the rest


def test_a_lines_start_is_never_banned_and_old_bans_rest():
    from test_lifecycle import _copper_row
    crew, w, minute = _crew()
    crew.log = lambda m: None
    site, ln = _copper_row(crew)
    crew._bad_belt = set()
    crew.ban_tile(ln["start"])
    assert tuple(ln["start"]) not in crew._bad_belt              # the one tile its plates can leave by
    crew.ban_tile((5, 5))
    assert (5, 5) in crew.bad_tiles()
    minute["now"] += crew.BAD_BELT_REST_MIN
    assert (5, 5) not in crew.bad_tiles()                        # a ban rests, then the tile is tried again


def test_a_crew_box_goes_past_the_last_furnace_and_is_collected_from_first():
    from test_lifecycle import _copper_row
    crew, w, minute = _crew()
    crew.log = lambda m: None
    site, ln = _copper_row(crew)
    placed = []
    w.free = lambda tiles: all(t[1] != 84 and t[1] != 85 for t in tiles)   # the line row and its inserter row taken
    w.place = lambda name, x, y, d, tiles: placed.append((name, x, y, d))
    crew.make = lambda kit, extra=False: None
    crew._power_tile = lambda t, avoid=frozenset(), plan=None: None
    [note] = crew.crew_boxes()
    assert "crew box" in note and [p[0] for p in placed] == ["inserter", "wooden-chest"]
    ins, box = placed
    assert (ins[1], ins[2]) == (-8.5, 83.5) and ins[3] == "south"          # beside the tail, picking from it
    assert site.params["crew_boxes"]["copper-plate"] == [[-8.5, 82.5]]
    assert not any(what == "copper-plate" for *_, what in site.chests)    # never a source for links
    assert crew.crew_boxes() == []                                        # once per line


def test_a_slot_factorys_own_slot_is_open_to_its_poles_other_slots_are_not():
    from types import SimpleNamespace
    crew, w, minute = _crew()
    own = SimpleNamespace(id="make-3", tiles={(1, 1), (1, 2)})
    other = SimpleNamespace(id="make-2", tiles={(5, 5)})
    w.zones = SimpleNamespace(slots=[own, other], refused_slots=lambda: own.tiles | other.tiles)
    w.seen, w.blocked, w.keepout = {(1, 1), (1, 2), (5, 5)}, (lambda t: False), set()
    site = Site("factory", {"item": "logistic-science-pack", "slot": "make-3"}, (0, 0))
    ok = crew._pole_freer(crew._own_slot(site))
    assert ok((1, 1)) and ok((1, 2)) and not ok((5, 5))
    assert not crew._pole_freer()((1, 1))                         # any other site: no pole in a slot


# ------------------------------------------------------------------ fewer wasted goals (run 4f7178 review, step 7)
def test_a_failure_reason_reads_the_same_whatever_its_numbers():
    from brain.agenda import reason_key
    a = reason_key("no coal trunk into site 21: from site 20: no route inside a corridor (12, -40), 366 tiles")
    b = reason_key("no coal trunk into site 21: from site 20: no route inside a corridor (13.5, -41), 490 tiles")
    assert a == b and "#" in a
    assert reason_key("no route") != reason_key("no power")


def test_the_coal_trunk_that_failed_twelve_times_is_held_after_three_until_a_site_goes_up_near_it():
    """Run 4f7178: the coal trunk into steam site 21 failed 10-12 times with one reason."""
    from brain.agenda import FAIL_NEAR
    crew, w, minute = _crew()
    logs = []
    crew.log = logs.append
    steam = crew.base.add(Site("steam_power", {"engines": 2}, (300, 300)))
    trunk = {"do": "trunk", "site": steam.id, "item": "coal", "reflex": "assessor: hand-fed"}
    ag = agenda_of(crew)
    for k in range(2):
        assert ag.note_failed(crew, trunk, f"no coal trunk into site {steam.id}: no route ({k}, {k + 7})") is None
    assert ag.stuck_hold(crew, trunk) is None
    why = ag.note_failed(crew, trunk, f"no coal trunk into site {steam.id}: no route (5, 9)")
    assert "failed 3x" in why and f"{FAIL_NEAR:.0f} tiles of site {steam.id}" in why
    assert [x for x in logs if x.startswith("[agenda]")] == [f"[agenda] {logs[-1].split('] ', 1)[1]}"]
    for who in ("assessor", "overseer", "rules"):                 # a fact about the goal: whoever asks
        ok, said = ag.propose(crew, {**trunk, "reflex": f"{who}: x"}, who)
        assert not ok and "failed 3x" in said
    assert crew.urgent == [] and ag.summary(crew)["held_after_failing"]
    crew.base.add(Site("electric_mine", {"resource": "coal"}, (300 + 3 * FAIL_NEAR, 300)))   # far: no change
    assert ag.stuck_hold(crew, trunk) is not None
    crew.base.add(Site("electric_mine", {"resource": "coal"}, (320, 290)))                   # near: it may try
    assert ag.stuck_hold(crew, trunk) is None and "may run again" in logs[-1]
    assert ag.note_failed(crew, trunk, f"no coal trunk into site {steam.id}: no route (1, 2)") is not None
    assert ag.stuck_hold(crew, trunk) is not None                 # one more try, failed the same way: held again


def test_a_hold_runs_out_by_the_clock_and_other_reasons_or_a_success_reset_the_count():
    from brain.agenda import FAIL_HOLD_MIN
    crew, w, minute = _crew()
    crew.log = lambda *a: None
    mine = {"do": "build", "module": "electric_mine", "resource": "iron-ore", "count": 4}
    ag = agenda_of(crew)
    for msg in ("no room on the patch", "no room on the patch", "no power pole reaches it"):
        assert ag.note_failed(crew, mine, msg) is None            # two reasons: neither came three times
    assert ag.note_failed(crew, mine, "no room on the patch") is not None
    assert ag.stuck_hold(crew, mine) is not None                  # no site: held until anything changes...
    minute["now"] += FAIL_HOLD_MIN
    assert ag.stuck_hold(crew, mine) is None                      # ...or the hour is up
    ag.note_done(minute["now"], mine)
    for _ in range(2):
        assert ag.note_failed(crew, mine, "no room on the patch") is None   # it worked in between: counted afresh
    feed = {"do": "feed", "site": 3, "item": "coal"}
    assert all(ag.note_failed(crew, feed, "no coal on hand") is None for _ in range(5))   # fuel is never held


def test_a_port_relinked_twice_with_no_cause_gets_a_second_trunk_instead_of_a_third_relink(monkeypatch):
    """Run 4f7178: 43 relinks, many "diagnosed: unknown cause, relinked once", on the same ports."""
    from brain import triage
    from brain.crew import Crew
    crew, w, minute = _crew()
    crew.log = lambda *a: None
    relinks = []
    crew.relink = lambda site, item=None, avoid=(): relinks.append((site, item)) or "relinked"
    cause = {"note": "diagnosed (unknown): no measurable cause (belt there, source running): relinked once"}
    monkeypatch.setattr(triage, "route", lambda c, g: (None, cause["note"], []))
    goal = {"do": "relink", "site": 29, "item": "iron-plate"}
    Crew.triaged_relink(crew, goal)
    Crew.triaged_relink(crew, goal)
    assert len(relinks) == 2 and crew.urgent == []
    msg = Crew.triaged_relink(crew, goal)
    assert len(relinks) == 2 and "no more relinks" in msg
    assert [{k: v for k, v in g.items() if k != "reflex"} for g in crew.urgent] == \
        [{"do": "trunk", "site": 29, "item": "iron-plate"}]
    ok, why = agenda_of(crew).propose(crew, {**goal, "reflex": "assessor: starving"}, "assessor")
    assert not ok and "second trunk" in why                       # nobody queues that relink again
    cause["note"] = "diagnosed (no-link): its iron-plate port has no belt: the link step lays one"
    Crew.triaged_relink(crew, goal)
    assert len(relinks) == 3                                      # a belt really missing: that relink still runs
    Crew.triaged_relink(crew, {"do": "relink", "site": 30, "item": "iron-plate"})
    assert len(relinks) == 4                                      # another port counts on its own
