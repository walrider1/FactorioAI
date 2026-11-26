"""Electric smelting, the power planner and oil scouting (docs/hybrid-plan.md, section 5). Run 20261125-214645:
burner smelting all game, 301 refuels by hand, at minute 590 two iron columns and two copper rows out of coal (iron
down to 95/min, research stalled), one crude well known for the whole oil chain."""

import math
from types import SimpleNamespace

import pytest

from brain import upgrade
from brain.assess import (electric_swap_alerts, fuel_alerts, oil_scout_alerts, power_headroom_alerts,
                          power_short_kw)
from brain.crew import Crew
from brain.modules import ELECTRIC_FURNACE, Base, ElectricMine, Pumpjack, Site, furnace_choice
from brain.outposts import POLE_SUPPLY, SmelterColumn
from brain.roadmap import electric_smelting_step
from brain.world import BuildError
from test_fuel_and_links import GridWorld
from test_lines import READY, MineWorld
from test_upkeep_outposts import _crew

ELECTRIC = READY | {"steel-furnace", ELECTRIC_FURNACE}
SIZE = {ELECTRIC_FURNACE: 3, "electric-mining-drill": 3, "stone-furnace": 2, "steel-furnace": 2}


class TileWorld(MineWorld):
    """MineWorld that books every tile a building covers and refuses overlaps (a layout bug shows here)."""

    def __init__(self, unlocked=ELECTRIC):
        super().__init__(unlocked)
        self.covers: dict = {}

    def place(self, name, x, y, d="north", tiles=()):
        k = SIZE.get(name, 1)
        want = {(math.floor(x - k / 2) + dx, math.floor(y - k / 2) + dy) for dx in range(k) for dy in range(k)}
        if tiles:
            assert set(tiles) == want, (name, x, y, sorted(tiles), sorted(want))
        clash = want & set(self.covers)
        assert not clash, f"{name} at ({x}, {y}) overlaps {[self.covers[t] for t in clash]}"
        for t in want:
            self.covers[t] = name
        self.used |= want
        self.placed.append((name, x, y, d))
        return {"x": x, "y": y}

    def survey_area(self, *a, **k):
        pass


def _base():
    b = Base()
    b.add(Site("steam_power", {"engines": 1}, (-40, 0), poles=[(-40, 0)], powered=True))
    return b


def _powered(w, poles, names) -> list:
    """Buildings of these names that no pole's supply area (5x5) touches."""
    dark = []
    for name, x, y, _ in w.placed:
        if name not in names:
            continue
        k = SIZE.get(name, 1)
        tiles = {(math.floor(x - k / 2) + dx, math.floor(y - k / 2) + dy) for dx in range(k) for dy in range(k)}
        if not any(abs(p[0] - t[0]) <= POLE_SUPPLY and abs(p[1] - t[1]) <= POLE_SUPPLY for p in poles for t in tiles):
            dark.append((name, x, y))
    return dark


# ------------------------------------------------------------------ 1. research: electric furnaces right after blue
def _research_crew(made=("automation-science-pack", "logistic-science-pack", "chemical-science-pack")):
    techs = {"advanced-material-processing-2": {"prerequisites": ["chemical-science-pack"], "unit_count": 250,
                                                "unit_ingredients": [{"name": p} for p in made[:3]]},
             "chemical-science-pack": {"prerequisites": [], "unit_count": 75, "unit_ingredients": []},
             "production-science-pack": {"prerequisites": ["chemical-science-pack"], "unit_count": 100,
                                         "unit_ingredients": [{"name": p} for p in made[:3]]}}
    kn = SimpleNamespace(techs=techs, researched={"chemical-science-pack"},
                         tech_path=lambda t: [t] if t in techs else [])
    sent = []
    w = SimpleNamespace(act=lambda a: (sent.append(a), {"ok": True, "data": {"current": a["technologies"][0]}})[1],
                        research={})
    crew = Crew.__new__(Crew)
    crew.w, crew.kn, crew.base = w, kn, Base()
    crew.base.add(Site("labs", {"count": 4}, (0, 0)))
    crew.base.add(Site("workshop", {}, (9, 9), chests=[(1.5, 1.5, p) for p in made]))
    crew.target_techs = ["production-science-pack"]
    return crew, sent


def test_idle_labs_research_electric_furnaces_first_once_blue_science_is_made():
    crew, sent = _research_crew()
    assert crew.auto_research() == "advanced-material-processing-2"
    assert sent[0]["technologies"] == ["advanced-material-processing-2"]
    crew, sent = _research_crew(made=("automation-science-pack", "logistic-science-pack"))
    assert crew.auto_research() != "advanced-material-processing-2"     # no blue packs made yet: not ready


def test_the_rocket_book_asks_for_electric_furnaces_after_blue_science_unless_on_its_way():
    crew, _ = _research_crew()
    science = {"chemical-science-pack": {"made_per_min": 6.0}}
    assert electric_smelting_step(crew, science) == {"do": "research", "tech": "advanced-material-processing-2"}
    crew.w.research = {"current": "production-science-pack", "queue": ["advanced-material-processing-2"]}
    assert electric_smelting_step(crew, science) is None
    crew.w.research = {}
    assert electric_smelting_step(crew, {"chemical-science-pack": {"made_per_min": 0.0}}) is None
    crew.kn.researched.add("advanced-material-processing-2")
    assert electric_smelting_step(crew, science) is None


# ------------------------------------------------------------------ 2. new builds: electric furnaces, no coal
def test_furnace_choice_is_electric_once_unlocked_and_obtainable_else_burners():
    w = SimpleNamespace(kn=SimpleNamespace(unlocked=ELECTRIC))
    assert furnace_choice(w, {}) == ELECTRIC_FURNACE
    w.obtainable = lambda item, n: n <= 4                    # advanced circuits for 4 furnaces only
    assert furnace_choice(w, {}, 4) == ELECTRIC_FURNACE and furnace_choice(w, {}, 13) == "steel-furnace"
    assert furnace_choice(w, {"furnace": "stone-furnace"}, 1) == "stone-furnace"     # decided once by the crew
    assert furnace_choice(SimpleNamespace(kn=SimpleNamespace(unlocked=READY)), {}) == "stone-furnace"


def test_an_electric_row_has_no_coal_belt_no_burners_and_every_unit_on_a_pole():
    w = TileWorld()
    site = ElectricMine().build(w, _base(), {"resource": "iron-ore", "count": 6, "into": "furnace"})
    furnaces = [e for e in w.placed if e[0] == ELECTRIC_FURNACE]
    drills = sorted(e[1] for e in w.placed if e[0] == "electric-mining-drill")
    assert len(furnaces) == 6 and not any(e[0] in ("stone-furnace", "long-handed-inserter") for e in w.placed)
    assert all(b - a == ElectricMine.UNIT_W_ELECTRIC for a, b in zip(drills, drills[1:]))
    assert site.burners == [] and "ports" not in site.params and w.fueled == []
    assert site.params["fuel"] == "electric" and site.params["furnace"] == ELECTRIC_FURNACE
    ln = site.params["line"]
    assert site.params["into"] == "line" and ln["rate"] == 6 * 30.0
    # each unit's plate inserter drops onto the line, two rows behind the 3x3 furnace
    ins = [(math.floor(x), math.floor(y)) for n, x, y, _ in w.placed if n == "inserter"]
    assert {(x, y - 1) for x, y in ins} <= {tuple(t) for t in ln["tiles"]}
    assert _powered(w, site.poles, {"inserter", ELECTRIC_FURNACE, "electric-mining-drill"}) == []
    assert all(abs(a[0] - b[0]) <= 7 for a, b in zip(site.poles, site.poles[1:]))    # wired pole to pole
    kit = ElectricMine().kit(w, None, {"resource": "iron-ore", "count": 6, "into": "furnace"})
    assert kit[ELECTRIC_FURNACE] == 6 and "stone-furnace" not in kit and "long-handed-inserter" not in kit


def test_a_burner_row_is_unchanged_before_electric_furnaces():
    w = TileWorld(unlocked=READY)
    site = ElectricMine().build(w, _base(), {"resource": "iron-ore", "count": 6, "into": "furnace"})
    assert site.params["fuel"] == "belt" and site.params["ports"][0]["item"] == "coal"
    assert sum(1 for e in w.placed if e[0] == "stone-furnace") == 6


def test_an_electric_column_takes_its_trunk_and_puts_plates_on_one_line_without_coal():
    w = TileWorld()
    col = SmelterColumn()
    site = col.build(w, _base(), {"resource": "iron-ore", "count": 5, "near": [-10, 30]})
    assert [pt["item"] for pt in site.params["ports"]] == ["iron-ore"] and site.params["ports"][0]["trunk"]
    assert sum(1 for e in w.placed if e[0] == ELECTRIC_FURNACE) == 5 and site.burners == []
    assert not any(e[0] in ("long-handed-inserter", "stone-furnace", "steel-furnace") for e in w.placed)
    ln = site.params["line"]
    assert ln["rate"] == 5 * 37.5 and len(ln["tiles"]) == 15 and site.params["fuel"] == "electric"
    assert _powered(w, site.poles, {"inserter", ELECTRIC_FURNACE}) == []
    kit = col.kit(w, None, {"resource": "iron-ore", "count": 5})
    assert kit[ELECTRIC_FURNACE] == 5 and "long-handed-inserter" not in kit and "iron-chest" not in kit
    assert len(col.tiles(0, 0, 5, "line", electric=True)) > len(col.tiles(0, 0, 5, "line"))   # 3 rows a furnace


def test_an_electric_steel_column_keeps_its_chests_and_its_iron_link():
    w = TileWorld()
    site = SmelterColumn().build(w, _base(), {"resource": "iron-plate", "count": 2, "near": [-10, 30]})
    [plates] = site.params["ports"]
    assert plates["item"] == "iron-plate" and not plates.get("trunk") and plates["per_minute"] == 75.0
    assert [c[2] for c in site.chests] == ["steel-plate", "steel-plate"]


def test_electric_sites_get_no_coal_belt_retrofit_and_no_fuel_alert():
    crew, w, _ = _crew(unlocked=tuple(ELECTRIC))
    crew.retrofit_due = Crew.retrofit_due.__get__(crew)
    row = crew.base.add(Site("electric_mine", {"resource": "iron-ore", "into": "line", "fuel": "electric"}, (0, 0)))
    old = crew.base.add(Site("electric_mine", {"resource": "iron-ore", "into": "furnace", "fuel": "hand"}, (0, 30)))
    assert crew.retrofit_due() == [old]
    assert crew._row_strip(row) is None
    crew.stuck_links = lambda: []
    assert [a for a in fuel_alerts(crew) if str(row.id) in a["key"]] == []


# ------------------------------------------------------------------ 3. the swap: one at a time, power booked first
def _coal_column(crew, n=4, outpost=None):
    ents = [("steel-furnace", 2.0, 2.0 * k, "north") for k in range(n)] + [("inserter", 0.5, k, "east") for k in range(n)]
    p = {"resource": "iron-ore", "count": n, "into": "line", "fuel": "belt",
         "line": {"item": "iron-plate", "source": (95.5, 9.5), "tiles": [], "rate": 150.0}}
    if outpost is not None:
        p["outpost"] = outpost
    return crew.base.add(Site("smelter_column", p, (100, 0), entities=ents,
                              burners=[(2.0, 2.0 * k) for k in range(n)]))


def _coal_row(crew, n=6):
    ents = [("stone-furnace", 3.0 * k, -2.0, "north") for k in range(n)] + \
           [("electric-mining-drill", 3.0 * k + 0.5, 0.5, "north") for k in range(n)]
    return crew.base.add(Site("electric_mine", {"resource": "copper-ore", "count": n, "into": "line", "fuel": "belt"},
                              (0, 50), entities=ents))


def _swap_crew(cap=9000, demand=2000):
    crew, w, minute = _crew(unlocked=tuple(ELECTRIC))
    crew.urgent, crew.plan_queue = [], []
    crew.patch_full = lambda r: False
    crew.can_obtain = lambda item, n=1: True
    crew.power_status = lambda: {"capacity_kw": cap, "demand_kw": demand, "expected_kw": 0,
                                 "booked_kw": upgrade.booked_power_kw(crew), "stations": 3}
    w.milestones = {"blue-auto": 1}                               # blue science runs, with circuits to spare
    w.stats = {"items": {"advanced-circuit": {"made_per_min": 10.0, "used_per_min": 6.0}}}
    return crew, w, minute


def test_no_furnace_swap_takes_the_circuits_blue_science_runs_on():
    crew, w, _ = _swap_crew()
    _coal_row(crew)
    w.stats = {"items": {"advanced-circuit": {"made_per_min": 6.0, "used_per_min": 5.5}}}
    assert electric_swap_alerts(crew) == []                       # barely any to spare
    w.stats = {"items": {"advanced-circuit": {"made_per_min": 10.0, "used_per_min": 6.0}}}
    w.milestones = {}
    assert electric_swap_alerts(crew) == []                       # blue science not automated yet
    w.milestones = {"blue-auto": 1}
    assert len(electric_swap_alerts(crew)) == 1


def test_the_least_risky_coal_fed_site_is_swapped_first_with_its_power_booked():
    crew, w, _ = _swap_crew()
    out = crew.base.add(Site("outpost", {"resource": "iron-ore", "head": [200, 0]}, (210, 0)))
    col = _coal_column(crew, outpost=out.id)
    row = _coal_row(crew)
    [a] = electric_swap_alerts(crew)
    assert a["key"] == f"swap-electric-{row.id}-0" and a["level"] == "urgent"     # a row before a trunk column
    g = a["fix"]
    assert g["do"] == "replace" and g["site"] == row.id and g["electric"] and g["power_kw"] == 6 * 180
    assert g["with"] == {"do": "build", "module": "electric_mine", "resource": "copper-ore", "count": 6,
                         "into": "furnace", "furnace": ELECTRIC_FURNACE}
    crew.urgent.append(g)                                         # queued: booked, and nothing else in flight
    assert upgrade.booked_power_kw(crew) == 1080 and electric_swap_alerts(crew) == []
    crew.urgent.clear()
    upgrade.note_swap_start(crew, g)                              # started (and failed): the column is next
    [a] = electric_swap_alerts(crew)
    cg = a["fix"]
    assert cg["site"] == col.id and cg["with"]["outpost"] == out.id and cg["with"]["toward"] == [200, 0]
    assert cg["with"]["furnace"] == ELECTRIC_FURNACE and cg["with"]["count"] == 4


def test_no_swap_before_electric_furnaces_or_while_they_cannot_be_made():
    crew, w, _ = _swap_crew()
    _coal_row(crew)
    crew.can_obtain = lambda item, n=1: False
    assert electric_swap_alerts(crew) == []
    crew.can_obtain = lambda item, n=1: True
    crew.kn.unlocked.discard(ELECTRIC_FURNACE)
    assert electric_swap_alerts(crew) == []


def test_a_swap_without_power_headroom_asks_for_a_steam_station_first():
    crew, w, _ = _swap_crew(cap=3600, demand=2000)       # 2000 * 1.3 = 2600 fits; with 1080 more (4004) it does not
    _coal_row(crew)
    [a] = electric_swap_alerts(crew)
    assert a["key"] == "swap-power-electric-3600" and a["fix"] == {"do": "build", "module": "steam_power", "engines": 2}
    crew, w, _ = _swap_crew(cap=2400, demand=2000)       # short already: the headroom alert asks, not this one
    _coal_row(crew)
    crew.base.add(Site("steam_power", {"engines": 3}, (-40, 0), powered=True))
    assert electric_swap_alerts(crew) == []
    assert power_headroom_alerts(crew)[0]["key"] == "headroom-power-2400"


def test_power_keeps_thirty_percent_headroom_and_counts_booked_swaps():
    crew, w, _ = _swap_crew(cap=9000, demand=6000)
    crew.base.add(Site("steam_power", {"engines": 10}, (-40, 0), powered=True))
    assert power_short_kw(crew) == 0 and power_headroom_alerts(crew) == []
    crew.urgent.append({"do": "replace", "site": 9, "electric": True, "power_kw": 1440, "with": {}})
    assert power_short_kw(crew) == pytest.approx((6000 + 1440) * 1.3 - 9000)
    [a] = power_headroom_alerts(crew)
    assert a["key"] == "headroom-power-9000" and "1440 kW booked" in a["what"]
    crew, w, _ = _swap_crew(cap=6000, demand=5900)       # at the limit: the urgent power-capacity alert has it
    crew.base.add(Site("steam_power", {"engines": 7}, (-40, 0), powered=True))
    assert power_headroom_alerts(crew) == []


def test_a_sealed_coal_column_may_be_replaced_by_its_electric_twin_and_the_swap_is_never_stale():
    crew, w, _ = _swap_crew()
    col = _coal_column(crew)
    g = upgrade.swap_goal(crew, col)
    assert upgrade.successor_ok(col, g["with"])
    assert not upgrade.successor_ok(col, {**g["with"], "furnace": "steel-furnace"})
    assert not upgrade.successor_ok(col, {**g["with"], "resource": "copper-ore"})
    assert crew.stale_replace(g) is None


def test_the_crew_books_power_first_and_falls_back_to_burners_when_none_can_be_built():
    crew, w, _ = _swap_crew(cap=1800, demand=1000)
    crew.base.add(Site("steam_power", {"engines": 2}, (-40, 0), powered=True))
    crew.log = lambda *a: None
    crew._replacing = None
    em = ElectricMine()
    p = {"resource": "iron-ore", "count": 6, "into": "furnace", "furnace": ELECTRIC_FURNACE}
    note = crew._electric_power_check(em, p)
    assert p["furnace"] == "stone-furnace" and "power short" in note
    p = {"resource": "iron-ore", "count": 6, "into": "furnace", "furnace": ELECTRIC_FURNACE}
    crew._replacing = object()                             # an electric swap: it waits for power instead
    with pytest.raises(BuildError, match="waits for power"):
        crew._electric_power_check(em, p)
    crew._replacing = None
    crew.power_status = lambda: {"capacity_kw": 9000, "demand_kw": 1000, "expected_kw": 0, "booked_kw": 0}
    p = {"resource": "iron-ore", "count": 6, "into": "furnace", "furnace": ELECTRIC_FURNACE}
    assert crew._electric_power_check(em, p) == "" and p["furnace"] == ELECTRIC_FURNACE


def test_power_first_keeps_thirty_percent_over_the_load_with_the_new_module():
    crew, w, _ = _swap_crew()
    crew.base.add(Site("steam_power", {"engines": 6}, (-40, 0), powered=True))
    crew.log = lambda *a: None
    cap = {"kw": 5400}
    crew.power_status = lambda: {"capacity_kw": cap["kw"], "headroom_kw": cap["kw"] - 3000}
    crew._module_kw = lambda m, p: 1500.0
    built = []
    crew.build = lambda g: (built.append(g), cap.__setitem__("kw", cap["kw"] + 1800))[1] or "built steam_power"
    crew._power_first("smelter_column", {})
    # 4500 kW with the column: 5850 wanted, so one station (7200) and not the old 500 kW margin's none
    assert len(built) == 1 and cap["kw"] == 7200


# ------------------------------------------------------------------ 4. oil: three wells before the chain
def _oil_crew(wells, research=None, rings=0):
    w = SimpleNamespace(resources={"crude-oil": set(wells)}, research=research or {}, oil_rings=rings,
                        OIL_MAX_RINGS=5)
    crew = SimpleNamespace(w=w, kn=SimpleNamespace(researched=set()), urgent=[], closeout_queue=[], plan_queue=[],
                           _minute=lambda: 100.0)
    return crew


def test_oil_processing_on_its_way_sends_a_scout_until_three_wells_are_known():
    crew = _oil_crew([(-59, 196)], research={"current": "oil-processing"})
    [a] = oil_scout_alerts(crew)
    assert a["fix"] == {"do": "scout", "resource": "crude-oil"} and a["key"].startswith("scout-oil-1")
    assert oil_scout_alerts(_oil_crew([(0, 0), (5, 5), (9, 1)], research={"current": "oil-processing"})) == []
    assert oil_scout_alerts(_oil_crew([(0, 0)])) == []                       # nobody wants oil yet
    crew = _oil_crew([(0, 0)])
    crew.plan_queue.append({"do": "build", "module": "workshop", "item": "plastic-bar", "per_minute": 8})
    assert oil_scout_alerts(crew)                                            # the plastic chain asked for
    assert oil_scout_alerts(_oil_crew([(0, 0)], research={"current": "plastics"}, rings=5)) == []   # searched out
    crew = _oil_crew([(0, 0)], research={"current": "plastics"})
    crew._oil_scouted = 95.0                                                 # scouted 5 minutes ago
    assert oil_scout_alerts(crew) == []


def test_scouting_with_one_well_known_searches_on_for_another_field():
    calls = []
    wells = {(-59, 196)}
    w = SimpleNamespace(resources={"crude-oil": wells}, pos=(0, 0), oil_rings=0, OIL_MAX_RINGS=5,
                        oil_searched=lambda: 700)

    def oil_wells(near=None, more=False):
        calls.append(more)
        if more:
            wells.update({(300, 10), (304, 12), (299, 16)})
        return []
    w.oil_wells = oil_wells
    jacks = SimpleNamespace(entities=[])
    c = SimpleNamespace(w=w, base=SimpleNamespace(centre=(0, 0), of=lambda k: [jacks] if k == "pumpjack" else []),
                        OIL_FIELD_GAP=Crew.OIL_FIELD_GAP)
    c.oil_fields = lambda: Crew.oil_fields(c)
    msg = Crew.scout_oil(c)
    assert calls == [True] and "found more crude oil: 4 wells in 2 field(s)" in msg


def test_a_pumpjack_build_with_too_few_wells_looks_for_another_field_first():
    searched = []

    class OilWorld(GridWorld):
        oil_rings, OIL_MAX_RINGS = 0, 5
        inventory = {"pumpjack": 4}
        pos = (0, 0)

        def oil_wells(self, near=None, more=False):
            searched.append(more)
            return [(10.5, 10.5), (16.5, 12.5), (12.5, 18.5)] if more else [(10.5, 10.5)]

        def blocked(self, t):
            return False

        def footprint(self, name, x, y, d="north"):
            return {(math.floor(x) + dx, math.floor(y) + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)}

        def observe(self, *a, **k):
            return {}

        def doing(self, text):
            pass

    w = OilWorld()
    site = Pumpjack().build(w, _base(), {"count": 4})
    assert searched == [False, True] and site.params["count"] == 3
