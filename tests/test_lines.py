"""Phase 1 of docs/logistics-plan.md: plate rows and smelting columns put their plates onto one belt (a line); a
line is one source of its whole row, its first link runs on from its tail with no inserter, more ports tap it,
and a row of idle plate chests is turned into a line where it stands."""

from types import SimpleNamespace

import pytest

from brain.crew import Crew
from brain.modules import Base, ElectricMine, Site
from brain.nodes import port_report
from brain.outposts import SmelterColumn

from test_fuel_and_links import GridWorld, _link_crew

READY = {"underground-belt", "long-handed-inserter", "electric-mining-drill"}


class MineWorld(GridWorld):
    """Iron ore from x 0..40, y 0..10; the base to the west."""

    def __init__(self, unlocked=READY):
        super().__init__()
        self.kn = SimpleNamespace(unlocked=set(unlocked))
        self.resources = {"iron-ore": {(x, y) for x in range(0, 41) for y in range(0, 11)}}
        self.patches = [{"name": "iron-ore", "tiles": 451, "centre": {"x": 20, "y": 5}}]
        self.inventory = {"long-handed-inserter": 20}
        self.lanes, self.keepout, self.pos = set(), set(), (-30.0, 0.0)
        self.fueled = []

    def patch(self, resource, min_tiles=30):
        return next((p for p in self.patches if p["name"] == resource), None)

    def free_belt(self, t):
        return t not in self.used

    def fuel(self, x, y, n):
        self.fueled.append((x, y))


def _base():
    b = Base()
    b.add(Site("steam_power", {"engines": 1}, (-40, 0), poles=[(-40, 0)], powered=True))
    return b


def test_a_plate_row_once_belts_can_cross_is_one_line_toward_the_base():
    w, base = MineWorld(), _base()
    site = ElectricMine().build(w, base, {"resource": "iron-ore", "count": 4, "into": "furnace"})
    ln = site.params["line"]
    assert site.params["into"] == "line" and site.chests == []           # no chest per unit
    assert ln["item"] == "iron-plate" and ln["rate"] == 4 * 18.75
    belts = [(n, x, y, d) for n, x, y, d in w.placed if n == "transport-belt" and (x - 0.5, y - 0.5) in
             {tuple(t) for t in ln["tiles"]}]
    assert len(belts) == len(ln["tiles"]) and {d for *_, d in belts} == {"west"}   # the base is west
    xs = [t[0] for t in ln["tiles"]]
    assert xs == sorted(xs, reverse=True) and ln["start"] == (xs[-1] - 1, ln["tiles"][0][1])
    assert tuple(ln["start"]) in w.lanes                                  # kept for the first link's belt
    # every unit's plate inserter drops onto the line: the unit's chest tile is a line tile
    u = ElectricMine().unit(xs[-1], ln["tiles"][0][1] + 5, "line")
    assert u["chest"] in {tuple(t) for t in ln["tiles"]}


def test_without_underground_belts_or_when_asked_rows_keep_their_chests():
    w = MineWorld(unlocked={"electric-mining-drill"})
    site = ElectricMine().build(w, _base(), {"resource": "iron-ore", "count": 3})
    assert site.params["into"] == "furnace" and len(site.chests) == 3 and "line" not in site.params
    site = ElectricMine().build(MineWorld(), _base(), {"resource": "iron-ore", "count": 3, "line": False})
    assert "line" not in site.params


def test_a_furnace_column_is_a_line_but_steel_stays_in_chests():
    w = SimpleNamespace(kn=SimpleNamespace(unlocked=READY))
    col = SmelterColumn()
    assert col.layout(w, {"resource": "iron-ore"}) == "line"
    assert col.layout(w, {"resource": "iron-plate"}) == "furnace"        # steel: the crew takes it from chests
    assert "iron-chest" not in col.kit(w, None, {"resource": "iron-ore", "count": 4})


def _line_row(crew, tiles=((20, 0), (19, 0), (18, 0), (17, 0), (16, 0), (15, 0)), rate=37.5):
    from brain.modules import line_record
    site = Site("electric_mine", {"resource": "iron-ore", "count": 2, "into": "line"}, (18, 3))
    site.plate_furnaces = [(18.0, 3.0, "iron-plate"), (15.0, 3.0, "iron-plate")]
    site.params["line"] = line_record("iron-plate", list(tiles), "west", (tiles[-1][0] - 1, 0), rate)
    for t in tiles:
        crew.w.used.add(t)
    return crew.base.add(site)


def test_a_line_is_one_source_and_its_first_link_runs_on_from_its_tail():
    w = MineWorld()
    crew = _link_crew(w)                                  # plain belts: undergrounds need the game itself
    row = _line_row(crew)
    ln = row.params["line"]
    assert crew.outputs("iron-plate") == [(15.5, 0.5, row)]
    assert crew._source_rate((15.5, 0.5), "iron-plate") == 37.5
    factory = crew.base.add(Site("factory", {"item": "x"}, (-10, 0)))
    port = {"item": "iron-plate", "end": [-5, 0], "approach": [-4, 0], "fed": None, "per_minute": 30.0}
    srcs = crew._free_outputs("iron-plate", factory, 30.0)
    plan = crew._plan_link(port, srcs, [], 30, False)
    assert plan["ins"] is None and plan["steps"][0][1] == tuple(ln["start"])   # straight on, nothing lifts it
    rec = crew._build_link(plan, port, factory, "item", None)
    assert rec["ins"] is None and not any(n.endswith("inserter") for n, *_ in w.placed)
    assert port["fed"] == [15.5, 0.5]
    assert crew._budget("iron-plate")[(15.5, 0.5)] == [30.0, 37.5]
    # its tail feeds one link; the next port takes an inserter tap off the line while it has plates to spare
    assert crew._free_outputs("iron-plate", factory, 5.0) == []
    taps = crew._belt_taps("iron-plate", None, 5.0)
    assert taps and all(owner is ln for _, owner in taps if owner is ln) and any(o is ln for _, o in taps)
    assert crew._belt_taps("iron-plate", None, 20.0) == [] or all(o is not ln for _, o in
                                                                   crew._belt_taps("iron-plate", None, 20.0))


def test_the_crew_takes_from_a_line_row_only_what_its_links_leave():
    crew = _link_crew(MineWorld(), unlocked=READY)
    row = _line_row(crew)
    assert sorted(r for *_, r, _ in crew._plate_sources("iron-plate")) == [18.0, 18.0]   # nothing draws yet
    crew.links.append({"source": (15.5, 0.5), "item": "iron-plate", "kind": "item", "to_site": 99,
                       "port": {"per_minute": 30.0}, "ins": None})
    left = sorted(r for *_, r, _ in crew._plate_sources("iron-plate"))
    assert left == pytest.approx([3.6, 3.6]) and row.params["line"]["rate"] == 37.5


def test_a_row_of_idle_plate_chests_becomes_a_line_where_it_stands():
    w = MineWorld()
    crew = _link_crew(w, unlocked=READY)
    crew._empty_container = lambda x, y: None
    crew._unlink = lambda rec: crew.links.remove(rec)
    crew.lines_tried = {}
    site = Site("electric_mine", {"resource": "iron-ore", "count": 3, "into": "furnace"}, (6, 3))
    site.chests = [(x + 0.5, -4.5, "iron-plate") for x in (0, 3, 6)]
    site.entities = [("iron-chest", x + 0.5, -4.5, "north") for x in (0, 3, 6)]
    for x in (0, 3, 6):
        w.used.add((x, -5))
    crew.base.add(site)
    crew.base.add(Site("steam_power", {"engines": 1}, (-40, 0), poles=[(-40, 0)], powered=True))
    assert crew.rows_for_lines() == [site]
    msg = crew.to_line(site)
    ln = site.params["line"]
    assert "3 plate chests turned into one iron-plate line" in msg and site.params["into"] == "line"
    assert site.chests == [] and [n for n, *_ in w.picked] == ["iron-chest"] * 3
    assert [tuple(t) for t in ln["tiles"]] == [(6, -5), (5, -5), (4, -5), (3, -5), (2, -5), (1, -5), (0, -5)]
    assert ln["rate"] == 3 * 18.75 and crew.rows_for_lines() == []
    # a row whose chests feed two factories stays as it is
    other = Site("electric_mine", {"resource": "iron-ore", "count": 3, "into": "furnace"}, (6, 30))
    other.chests = [(x + 0.5, 25.5, "iron-plate") for x in (0, 3, 6)]
    crew.base.add(other)
    crew.links += [{"source": other.chests[k][:2], "item": "iron-plate", "to_site": 9, "port": {"end": [0, k]}}
                   for k in (0, 1)]
    assert other not in crew.rows_for_lines()


def test_a_line_nobody_draws_from_shows_in_the_port_report():
    crew = _link_crew(MineWorld(), unlocked=READY)
    crew.item_ports = lambda: []
    row = _line_row(crew)
    row.health = {"waiting_for_space_in_destination": 2}
    rep = port_report(crew)
    assert rep["stranded"] == [{"site": row.id, "item": "iron-plate", "chests": 0, "line": True,
                                "per_minute": 37.5}]
    assert any("line of site" in s and "backs up" in s for s in rep["summary"])


def test_plates_first_sizes_new_rows_by_rate_not_by_counting_chests():
    crew = _link_crew(MineWorld(), unlocked=READY)
    _line_row(crew, rate=112.5)                                           # a six-unit line nobody draws from
    assert crew.spare_rate("iron-plate") == 112.5
    assert Crew.spare_rate.__doc__
