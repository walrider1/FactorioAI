"""The general-manager parts of the brain bot: pole routing that reuses existing poles, removal guard rails."""

from contextlib import contextmanager

from brain.crew import Crew
from brain.world import BuildError
from brain.modules import Site
from brain.power import route_poles


def free_everywhere(t):
    return True


def test_route_reuses_existing_poles():
    start, net = [(0, 0)], [(30, 0)]
    fresh = route_poles(start, net, set(), free_everywhere)
    assert fresh is not None and len(fresh) == 4                         # 30 tiles at reach 7: four new poles
    old_line = {(7, 0), (14, 0), (21, 0)}                                 # a line someone already built
    assert len(route_poles(start, net, old_line, free_everywhere)) == 1  # bridge the 9-tile gap only
    assert route_poles(start, net, old_line | {(28, 0)}, free_everywhere) == []   # fully joined: nothing new
    blocked = {(x, y) for x in range(10, 20) for y in range(-12, 13)}     # a lake in the way
    path = route_poles(start, net, set(), lambda t: t not in blocked, margin=14)
    assert path and not set(path) & blocked


class StubWorld:
    def __init__(self, kn=None):
        self.kn, self.pos, self.inventory, self.research = kn, (0.0, 0.0), {}, {}
        self.entities, self.picked = {}, []
        self.stats, self.speed = {}, 1.0

    def pickup(self, name, x, y, direction="north"):
        self.picked.append((name, x, y))
        self.inventory[name] = self.inventory.get(name, 0) + 1
        return True

    def doing(self, text):
        pass

    @contextmanager
    def step(self, name):
        yield

    def now_tick(self):
        return 0


class StubKn:
    unlocked, researched, techs = set(), set(), {}


def _crew():
    return Crew(StubWorld(StubKn()), log=lambda *a: None)


def _mine(crew, resource, plate=None, depleted=False):
    s = Site("burner_mine", {"resource": resource, "count": 2}, (0, 0),
             entities=[("burner-mining-drill", 1, 1, "north"), ("burner-mining-drill", 3, 1, "north"),
                       ("stone-furnace", 1, -1, "north"), ("small-electric-pole", 5, 5, "north")])
    if plate:
        s.plate_furnaces = [(1, -1, plate)]
    if depleted:
        s.health = {"no_minable_resources": 2}
    return crew.base.add(s)


def test_removal_guard_rails_and_removal():
    crew = _crew()
    iron = _mine(crew, "iron-ore", "iron-plate")
    assert "last working source of iron-plate" in crew.removal_blocker(iron)
    second = _mine(crew, "iron-ore", "iron-plate")
    assert crew.removal_blocker(iron) is None                    # another source exists now
    power = crew.base.add(Site("steam_power", {"engines": 2}, (50, 0), poles=[(50, 0)], powered=True))
    crew.base.add(Site("labs", {"count": 2}, (60, 0)))
    assert "only power station" in crew.removal_blocker(power)

    msg = crew.remove(iron.id)
    assert "3 buildings" in msg and crew.base.get(iron.id) is None
    assert [n for n, *_ in crew.w.picked].count("small-electric-pole") == 0   # poles stay by default
    stub = [s for s in crew.base.sites if s.kind == "poles"]
    assert stub and stub[0].params["from_site"] == iron.id
    assert crew.removal_blocker(second) is not None              # now it is the last one again


def test_hints_name_what_to_refactor():
    crew = _crew()
    _mine(crew, "coal", depleted=True)
    crew.base.add(Site("factory", {"item": "automation-science-pack"}, (0, 0), powered=False,
                       entities=[("assembling-machine-1", 0, 0, "north")]))
    hints = " | ".join(crew.hints())
    assert "depleted" in hints and "no power" in hints


def test_churn_guard_and_detection():
    crew = _crew()
    _mine(crew, "iron-ore", "iron-plate")                       # a second plate source, so only age matters
    coal = _mine(crew, "coal")
    _mine(crew, "coal")
    coal.health = {"working": 2}
    coal.built_minute = 0.0
    crew.w.now_tick = lambda: 5 * 3600                          # minute 5: young and working
    assert "built 5.0 minutes ago" in crew.removal_blocker(coal)
    crew.remove(coal.id, force=True)                            # the strategist can still insist
    again = Site("burner_mine", {"resource": "coal", "count": 2}, (0, 0))
    crew.w.now_tick = lambda: 9 * 3600
    crew._note_churn(again)
    assert crew.churn and "wasted work" in crew.churn[-1] and "churn" in crew.hints()[0]


def test_watchdog_ladder_and_reset():
    from brain.watchdog import Watchdog
    dog = Watchdog()
    m = 3600
    assert dog.check(5 * m) is None
    assert dog.check(6 * m) == "refresh" and dog.check(7 * m) is None
    assert dog.check(10 * m) == "switch" and dog.check(15 * m) == "expand"
    dog.observe(16 * m, {"ok": True, "goal": {"do": "build"}}, set(), 0)      # progress resets the ladder
    assert dog.level == 0 and dog.check(20 * m) is None and dog.check(22 * m) == "refresh"


def test_book_tries_smaller_then_drops_a_failing_step():
    import json as _j
    from brain.brain import ScriptedStrategist
    base = [{"kind": "burner_mine", "resource": r, "count": n} for r, n in
            (("iron-ore", 5), ("coal", 2), ("stone", 1), ("copper-ore", 2))]
    rep = {"base": base, "researched": [], "recent_results": [], "research": {}}
    big = {"do": "build", "module": "steam_power", "engines": 2}
    assert ScriptedStrategist().plan(rep)["goals"][0] == big
    rep["failure_counts"] = {_j.dumps(big, sort_keys=True): 2}
    assert ScriptedStrategist().plan(rep)["goals"][0]["engines"] == 1        # smaller variant
    small = {**big, "engines": 1}
    rep["failure_counts"][_j.dumps(small, sort_keys=True)] = 2
    assert ScriptedStrategist().plan(rep)["goals"][0].get("module") != "steam_power"   # dropped, moves on


def test_idle_labs_get_the_next_milestone_tech():
    import json as _j
    from arena import catalog
    from brain.knowledge import Knowledge
    kn = Knowledge(_j.loads(_j.dumps(catalog.get().data)))
    kn.learn_research(["steam-power", "electronics", "automation-science-pack", "automation",
                       "logistic-science-pack"])
    crew = Crew(StubWorld(kn), log=lambda *a: None)
    sent = []
    crew.w.act = lambda a: (sent.append(a), {"ok": True, "data": {"current": a["technologies"][0]}})[1]
    crew.base.add(Site("labs", {"count": 2}, (0, 0)))
    assert crew.auto_research() is None                        # no factory makes packs yet: nothing queued
    crew.base.add(Site("factory", {"item": "automation-science-pack"}, (5, 5),
                       chests=[(5, 5, "automation-science-pack")]))
    crew.target_techs = ["steel-processing", "oil-processing"]
    assert crew.auto_research() == "steel-processing"          # the next milestone tech red alone can do
    assert sent[-1]["mode"] == "append"


def test_blocker_points_past_burner_mines():
    crew = _crew()
    for _ in range(5):
        _mine(crew, "iron-ore", "iron-plate")                   # 10 burner drills, no coal mine, no power
    assert crew.blocker()["resource"] == "coal"                 # fuel from a chest before anything else
    assert "no coal mine yet" in crew.hints()[0]
    _mine(crew, "coal")
    assert crew.blocker()["module"] == "steam_power"
    assert "blocked on steam_power" in " | ".join(crew.hints())
    crew.w.fuel_units = lambda: 100
    crew.w.rate = lambda item: 0.0
    assert crew.supply_goals()[0]["module"] == "steam_power"   # the watchdog's "expand" builds the next tier
    crew.base.add(Site("steam_power", {"engines": 1}, (50, 0), poles=[(50, 0)], powered=True))
    assert crew.blocker()["module"] == "labs"


def test_foreman_stays_out_of_the_way_of_a_build():
    crew = _crew()
    calls = []
    f = crew.foreman
    f._coal = lambda lean=False: calls.append(("coal", lean))
    f._sites = lambda lean=False: calls.append(("sites", lean))
    f._rewire = lambda: calls.append(("rewire", False))
    f._labs = lambda hard: calls.append(("labs", hard))
    f._keep_labs_busy = lambda quiet=False: calls.append(("research", quiet))
    crew.ensure_space = lambda *a: None
    crew.w.foreman = lambda step: None
    crew.w.activity = ""
    f.busy = "factory"
    f.patrol(force=True)
    # only urgent upkeep and a research pick (one call), no rewiring or walking to feed labs
    assert calls == [("coal", True), ("sites", True), ("research", True)]
    f.patrol(force=True)
    assert len(calls) == 3                                      # and not again within a game minute, forced or not
    f.busy, f.last = None, 0.0
    f.patrol(force=True)
    assert [c[0] for c in calls[3:]] == ["coal", "sites", "rewire", "labs"]


def test_coal_mine_comes_first_and_fuel_comes_from_it():
    crew = _crew()
    built = []

    def fake_build(goal):                                       # only the nested coal-mine build reaches here
        built.append(goal)
        _mine(crew, "coal")
        return "built burner_mine as site 9"
    crew.build = fake_build
    crew.w.trees = {(3, 3): (3.5, 3.5)}                         # a tree right here
    crew.w.trees_near = lambda r: any(abs(p[0]) + abs(p[1]) <= r for p in crew.w.trees.values())
    assert crew.fuel_item() == "wood"                           # no coal mine yet, trees close by
    note = crew._coal_first("burner_mine", {"resource": "iron-ore"})
    assert built and built[0]["resource"] == "coal" and note.startswith("first built")
    assert crew.fuel_item() == "coal"                           # from now on fuel comes from its chests
    assert crew._coal_first("steam_power", {}) == ""            # once is enough


def test_far_trees_mean_coal_not_a_walk():
    crew = _crew()
    crew.w.trees = {(200, 0): (200.5, 0.5)}                     # the only tree known is 200 tiles away
    crew.w.trees_near = lambda r: False
    crew.w.find_trees = lambda walk=True: bool(crew.w.trees)
    assert crew.fuel_item() == "coal"


def test_crew_waits_for_its_own_drills_before_hand_mining(monkeypatch):
    import brain.crew as crew_mod
    crew = _crew()
    coal = _mine(crew, "coal")
    coal.chests = [(2.5, 2.5, "coal")]
    stock = {"n": 0}

    def extract(x, y, item, n):                                 # the chest fills up over time
        take = min(n, stock["n"])
        stock["n"] -= take
        return take
    crew.w.extract = extract
    crew.w.pos, crew.w.speed = (0.0, 0.0), 1.0
    crew.foreman.patrol = lambda **k: stock.__setitem__("n", stock["n"] + 10)
    monkeypatch.setattr(crew_mod.time, "sleep", lambda s: None)
    assert crew.from_mines("coal", 30) == 30                    # waited for the drills, no hand-mining
    assert crew.from_mines("coal", 10_000) == 0                 # far too slow for 2 drills: hands do it


def test_foreman_tends_coal_and_power_first_then_the_strategists_pick():
    crew = _crew()
    stone = _mine(crew, "stone")
    iron = _mine(crew, "iron-ore", "iron-plate")
    labs = crew.base.add(Site("labs", {"count": 2}, (5, 5)))
    power = crew.base.add(Site("steam_power", {"engines": 1}, (90, 0)))
    coal = _mine(crew, "coal")
    order = [s.id for s in crew.foreman.route(list(crew.base.sites))]
    assert order == [coal.id, power.id, labs.id, iron.id, stone.id]
    assert "now tends" in crew.run_goal({"do": "priority", "sites": [stone.id]})
    assert [s.id for s in crew.foreman.route(list(crew.base.sites))][2] == stone.id   # right after coal, power


def test_coal_deficit_is_an_alert_and_the_supervisor_answers_it_once():
    from brain.assess import Supervisor
    crew = _crew()
    crew.w.steps, crew.w.activity = [], ""
    for _ in range(4):
        _mine(crew, "iron-ore", "iron-plate")
    coal = _mine(crew, "coal")
    crew.w.stats = {"items": {"coal": {"made_per_min": 15.0, "used_per_min": 40.0}}}
    crew.w.inventory = {"coal": 60}                             # 60 coal at -25/min: ~2.4 minutes left
    alerts = {a["key"]: a for a in crew.alerts()}
    assert alerts["coal-deficit"]["level"] == "urgent" and "runs out" in alerts["coal-deficit"]["what"]
    assert alerts["coal-deficit"]["fix"]["resource"] == "coal" and alerts["coal-deficit"]["fix"]["count"] >= 3
    coal.health = {"no_fuel": 2, "working": 0}                  # its own drills ran dry
    sup = Supervisor(None, log=lambda *a: None)
    crew.current = {"goal": {"do": "build", "module": "factory", "item": "x"}, "tick": 0, "urgent": False}
    sup.tick(crew, force=True)
    assert {g["do"] for g in crew.urgent} == {"build", "feed"} and all(g.get("reflex") for g in crew.urgent)
    assert crew.preempt                                         # the factory goal will pause for them
    sup.tick(crew, force=True)
    assert len(crew.urgent) == 2                                # answered: not again while it lasts
    crew.w.stats = {"items": {"coal": {"made_per_min": 40.0, "used_per_min": 20.0}}}
    coal.health = {"working": 2}
    sup.tick(crew, force=True)
    assert sup.alerts == [] and not sup.answered                # gone: forgotten, so it is answered again later


def test_a_goal_pauses_only_at_a_safe_point():
    import pytest
    from brain.world import Preempted
    crew = _crew()
    crew.w.steps = ["build"]                                    # placing buildings: never paused
    crew.current = {"goal": {"do": "build"}, "tick": 0, "urgent": False}
    crew.preempt = "coal"
    crew.checkpoint()
    assert crew.preempt == "coal"
    crew.w.steps = ["make"]                                     # gathering and crafting: a safe point
    with pytest.raises(Preempted):
        crew.checkpoint()
    assert crew.preempt is None
    crew.current = {"goal": {"do": "feed"}, "tick": 0, "urgent": True}
    crew.preempt = "again"
    crew.checkpoint()                                           # an urgent goal is never paused itself


def test_ai_assessor_amends_the_plan_in_the_background():
    from brain.assess import Supervisor
    crew = _crew()
    crew.w.steps, crew.w.activity = [], "crafting"
    mine = _mine(crew, "iron-ore", "iron-plate")

    class Assessor:
        name, cost_usd = "fake", 0.0

        def decide(self, sit, alerts, answered):
            assert "current_goal" in sit and "flows" in sit and "alerts" in sit
            return {"note": "copper runs short before the factory needs it", "interrupt": True, "replan": True,
                    "urgent_goals": [{"do": "build", "module": "burner_mine", "resource": "copper-ore", "count": 3},
                                     {"do": "launch", "rocket": True}],          # not a goal: dropped
                    "priority": [str(mine.id)]}
    sup = Supervisor(Assessor(), log=lambda *a: None, min_gap_s=0)
    crew.current = {"goal": {"do": "build", "module": "electric_mine", "resource": "iron-ore"}, "tick": 0,
                    "urgent": False}
    sup.tick(crew, force=True)
    sup.pending.result(timeout=5)
    sup.tick(crew, force=True)
    # no urgent alert: the fix runs right after the goal in hand, which is not paused for it (run 20261120-092648)
    assert [g["resource"] for g in crew.urgent] == ["copper-ore"] and crew.preempt is None
    assert crew.foreman.priority == [str(mine.id)] and "copper" in sup.take_replan()
    sup.close()
    # a next-tier build (a factory) is not paused for it: the fix waits in the queue and runs right after
    crew.urgent, crew.preempt, crew.last_interrupt = [], None, -1e9
    sup = Supervisor(Assessor(), log=lambda *a: None, min_gap_s=0)
    crew.current = {"goal": {"do": "build", "module": "factory", "item": "x"}, "tick": 0, "urgent": False}
    sup.tick(crew, force=True)
    sup.pending.result(timeout=5)
    sup.tick(crew, force=True)
    assert [g["resource"] for g in crew.urgent] == ["copper-ore"] and crew.preempt is None
    sup.close()


def test_any_item_running_short_is_an_alert_not_only_coal():
    crew = _crew()
    crew.w.stats = {"items": {"iron-plate": {"made_per_min": 10.0, "used_per_min": 40.0}}}
    crew.w.inventory = {"iron-plate": 30}
    a = next(a for a in crew.alerts() if a["key"] == "short-iron-plate")
    # sized to the gap (supply.supply_goal): 30/min short x 1.3 at 15/min a burner drill (no electric drills yet)
    assert a["fix"] == {"do": "build", "module": "burner_mine", "resource": "iron-ore", "count": 3, "into": "furnace"}


def test_opening_book_answers_alerts_first():
    from brain.brain import ScriptedStrategist
    fix = {"do": "build", "module": "burner_mine", "resource": "coal", "count": 3}
    rep = {"base": [], "researched": [], "recent_results": [], "research": {},
           "alerts": [{"level": "warn", "what": "x", "fix": None}, {"level": "urgent", "what": "coal", "fix": fix}]}
    assert ScriptedStrategist().plan(rep)["goals"] == [fix]


def test_a_failed_storage_chest_never_fails_the_goal():
    crew = _crew()

    def boom(*a, **k):
        raise BuildError("place iron-chest at (2.5, -12.5) north: out_of_reach")
    crew.ensure_space = boom
    assert "foreman" in crew.run_goal({"do": "priority", "sites": ["coal"]})
    assert any("storage" in p for p in crew.foreman.problems)


def test_pole_groups_split_a_site_into_wired_parts():
    from brain.crew import _pole_groups
    groups = _pole_groups([(0, 0), (5, 0), (40, 0), (44, 3), (90, 90)])
    assert sorted(len(g) for g in groups) == [1, 2, 2]


def test_idle_labs_get_research_even_while_a_build_runs():
    """run 20261119-202811: builds ran back to back for 35 game minutes and the labs stood empty, because only
    the full patrol queued research; a research goal for a finished tech did nothing either."""
    import json as _j
    from arena import catalog
    from brain.knowledge import Knowledge
    kn = Knowledge(_j.loads(_j.dumps(catalog.get().data)))
    kn.learn_research(["steam-power", "electronics", "automation-science-pack", "automation", "logistics",
                       "logistic-science-pack"])
    crew = Crew(StubWorld(kn), log=lambda *a: None)
    sent = []
    crew.w.act = lambda a: (sent.append(a), {"ok": True, "data": {"current": a["technologies"][0]}})[1]
    crew.w.read_stats = lambda m=5: {}
    crew.w.research = {}
    crew.base.add(Site("labs", {"count": 2}, (0, 0)))
    crew.base.add(Site("factory", {"item": "automation-science-pack"}, (5, 5),
                       chests=[(5, 5, "automation-science-pack")]))
    crew.target_techs = ["steel-processing"]
    f = crew.foreman
    f._coal = lambda lean=False: None
    f._sites = lambda lean=False: None
    crew.ensure_space = lambda *a: None
    crew.w.foreman = lambda step: None
    crew.w.activity = ""
    f.busy = "factory"
    f.patrol(force=True)
    assert sent and sent[-1]["technologies"] == ["steel-processing"] and sent[-1]["mode"] == "append"
    assert any("labs were idle" in p for p in f.problems)
    crew.w.research = {}
    assert "queued steel-processing" in crew.research("logistics")


def test_power_demand_is_what_the_network_draws_not_every_machine_at_full_work():
    """run 20261119-202811: 434 kW drawn while ~15 MW was counted (every inserter and drill at its maximum), so
    the bot kept building steam engines it did not need."""
    import json as _j
    from arena import catalog
    from brain.knowledge import Knowledge
    kn = Knowledge(_j.loads(_j.dumps(catalog.get().data)))
    crew = Crew(StubWorld(kn), log=lambda *a: None)
    crew.base.add(Site("steam_power", {"engines": 2}, (50, 0), powered=True))
    mine = Site("electric_mine", {"resource": "iron-ore", "count": 10}, (0, 0),
                entities=[("electric-mining-drill", x, 0, "north") for x in range(10)]
                + [("inserter", x, 3, "north") for x in range(100)])
    mine.health = {"working": 2, "waiting_for_space_in_destination": 8}
    crew.base.add(mine)
    ps = crew.power_status()
    assert ps["capacity_kw"] == 1800 and ps["demand_source"] == "estimated"
    assert ps["full_load_kw"] > 2000                         # 10 drills x 90 kW + 100 inserters x 13.2 kW
    assert ps["demand_kw"] < 0.3 * ps["full_load_kw"]        # 2 drills working, inserters mostly idle
    assert crew._draw_kw("inserter") < 1.5                    # drain plus a few swings, not 13.2 kW
    crew.w.stats = {"power": {"networks": 1, "consumption_kw": 300.0, "consumption_5s_kw": 340.0,
                              "production_kw": 310.0}}
    ps = crew.power_status()
    assert ps["demand_source"] == "measured" and ps["demand_kw"] == 340 and ps["headroom_kw"] == 1460
    # a reading that cannot be right (more than the engines can give: units mixed up) is ignored
    crew.w.stats = {"power": {"networks": 1, "consumption_kw": 18000.0, "production_kw": 18000.0}}
    assert crew.power_status()["demand_source"] == "estimated"


def _linked_iron_mine(crew, linked=(0, 1)):
    """An electric iron mine of two units (drill, furnace, inserter, chest) whose chests links belt away."""
    s = Site("electric_mine", {"resource": "iron-ore", "count": 2, "into": "furnace"}, (0, 0),
             entities=[("electric-mining-drill", 0, -3, "north"), ("electric-mining-drill", 3, -3, "north")])
    s.plate_furnaces = [(0.0, 0.0, "iron-plate"), (3.0, 0.0, "iron-plate")]
    s.chests = [(0.5, 2.5, "iron-plate"), (3.5, 2.5, "iron-plate")]
    crew.base.add(s)
    crew.links = [{"source": s.chests[k][:2], "item": "iron-plate", "kind": "item", "to_site": 11,
                   "ins": ((int(s.chests[k][0]), 4), "south", "inserter"), "port": {"per_minute": 30.0}}
                  for k in linked]
    return s


def test_plates_a_link_belts_away_are_not_counted_as_ours():
    """run 20261120-084046: the crew waited for plates that all went down the belt to the red-science factory."""
    crew = _crew()
    _linked_iron_mine(crew, linked=(0,))
    rates = sorted(r for *_, r, _ in crew._plate_sources("iron-plate"))
    assert rates == [0.0, 0.0, 0.0, 18.0]                        # the linked unit gives the crew nothing


def test_short_of_plates_the_crew_lifts_link_inserters_and_always_puts_them_back(monkeypatch):
    import pytest
    import brain.crew as crew_mod
    from brain.world import Preempted
    crew = _crew()
    _linked_iron_mine(crew)
    w = crew.w
    w.steps, w.activity, w.last_progress_tick = [], "x", 0
    w.refresh = lambda: None
    placed, stock = [], {"n": 0}
    w.place = lambda name, x, y, d="north", tiles=(): placed.append((name, x, y, d)) or {}

    def extract(x, y, item, n):                                  # only a lifted link lets plates pile up
        take = min(n, stock["n"])
        stock["n"] -= take
        w.inventory[item] = w.inventory.get(item, 0) + take
        return take
    w.extract = extract
    crew.foreman.patrol = lambda **k: stock.__setitem__("n", stock["n"] + 20 * len(crew.lifted_links))
    crew.hand_smelt = lambda *a, **k: (_ for _ in ()).throw(AssertionError("hand-smelted with drills at hand"))
    monkeypatch.setattr(crew_mod.time, "sleep", lambda s: None)
    crew.plates("iron-plate", 100)
    assert w.inventory["iron-plate"] >= 100
    assert [p[0] for p in w.picked] == ["inserter", "inserter"]  # both links lifted (one is too slow for 100)
    assert len(placed) == 2 and crew.lifted_links == []          # and both back
    assert all("lifted" not in ln for ln in crew.links)
    # an interrupt mid-collection: the inserters still go back
    w.inventory["iron-plate"], w.picked[:], placed[:] = 0, [], []
    crew.checkpoint = lambda: (_ for _ in ()).throw(Preempted("overseer: x")) if crew.lifted_links else None
    with pytest.raises(Preempted):
        crew.plates("iron-plate", 100)
    assert len(w.picked) == len(placed) == 2 and crew.lifted_links == []
