"""The bus head gets its own supply (run 4f7178 review, step 1): every head port is weighed by what its belt
really brings against what the era takes, fed or not; a short one gets its own field (the smelting column beside
the head) when the spare sources cannot cover it, and a second trunk. Run 4f7178: the head's iron port counted as
fed by one 37.5/min mine for 350 game minutes while the bus took 130/min."""

from brain import zones
from brain.modules import Base, Site
from test_zones import bus_crew, fresh_zones


def _head(crew, z, seg):
    gear = next(b for b in seg.blocks if b.product == "iron-gear-wheel")
    z.mark_built(gear, (0, 0))
    crew.base.add(Site("factory", {"item": "iron-gear-wheel", "segment_block": gear.key,
                                   "ports": [dict(p) for p in zones.slot_build(gear).ports]}, (0, 0)))
    zones.run_bus_goal(crew, {"do": "bus", "segment": 1})
    site = zones.bus_site(crew, 1)
    return site, next(pt for pt in site.params["ports"] if pt["item"] == "iron-plate")


def _fed_by(crew, pt, supply):
    pt["fed"] = ["link"]
    crew.links = [{"port": pt, "item": "iron-plate", "source": (1.5, 1.5)}]
    crew._coal_root = lambda ln: tuple(ln["source"])
    crew._budget = lambda item=None: {(1.5, 1.5): [float(pt["per_minute"]), float(supply)]}


def test_a_fed_head_port_whose_belt_brings_too_little_gets_its_own_field_and_a_second_trunk():
    scan, w, z, seg = fresh_zones()
    crew = bus_crew(z, Base())
    site, pt = _head(crew, z, seg)
    crew.urgent.clear()
    z.head_asked = {}
    _fed_by(crew, pt, 0.4 * pt["per_minute"])
    assert zones.head_got(crew, pt) == round(0.4 * pt["per_minute"], 6) or \
        abs(zones.head_got(crew, pt) - 0.4 * pt["per_minute"]) < 1e-6
    note = zones.head_check(crew, z, 100.0)
    own = [g for g in crew.urgent if g.get("for_head") == "iron-plate"]
    assert own and own[0]["column_near"] == pt["approach"] and "own" in note
    assert [(g["site"], g["item"]) for g in crew.urgent if g.get("do") == "trunk"] == [(site.id, "iron-plate")]
    assert zones.head_check(crew, z, 105.0) == ""                  # looked at once every HEAD_CHECK_MIN


def test_a_head_port_that_gets_what_the_era_takes_asks_for_nothing():
    scan, w, z, seg = fresh_zones()
    crew = bus_crew(z, Base())
    site, pt = _head(crew, z, seg)
    crew.urgent.clear()
    z.head_asked = {}
    pt["planned_per_minute"] = pt["per_minute"]
    _fed_by(crew, pt, pt["per_minute"])
    assert zones.head_supply(crew, 1, 200.0) == ""
    assert not crew.urgent


def test_spare_sources_that_cover_the_gap_get_a_trunk_but_no_new_field():
    scan, w, z, seg = fresh_zones()
    crew = bus_crew(z, Base())
    site, pt = _head(crew, z, seg)
    crew.urgent.clear()
    z.head_asked = {}
    _fed_by(crew, pt, 0.5 * pt["per_minute"])
    crew.spare_rate = lambda item: 10 * pt["per_minute"]
    zones.head_supply(crew, 1, 300.0)
    assert not [g for g in crew.urgent if g.get("for_head")]
    assert [g["item"] for g in crew.urgent if g.get("do") == "trunk"] == ["iron-plate"]


def test_the_head_fields_column_goes_beside_the_bus_head():
    from brain.outposts import Outpost
    out = Outpost()
    out._build = lambda w, base, p: Site("outpost", {"resource": "iron-ore", "count": 4}, (0, 0))
    site = out.build(None, None, {"resource": "iron-ore", "count": 4, "column_near": [10, 20],
                                  "for_head": "iron-plate"})
    assert site.params["column_near"] == [10, 20] and site.params["for_head"] == "iron-plate"


def test_a_port_fed_through_one_inserter_gets_at_most_an_inserters_worth():
    """Run 4f7178, minute 341: the head's iron (130/min) came through one inserter off red science's belt."""
    from brain.crew import Crew
    c = Crew.__new__(Crew)
    pt = {"item": "iron-plate", "end": [97, -65], "per_minute": 130.5, "fed": ["link"]}
    belt = {"item": "iron-plate", "source": (46.5, -35.5), "to_site": 16, "port": {"end": [0, 0]}}
    tap = {"item": "iron-plate", "source": (46, -35), "to_site": 36, "port": pt, "tap_of": belt,
           "ins": ((47, -35), "west", "inserter")}
    c.links = [belt, tap]
    c._budget = lambda item=None: {(46.5, -35.5): [147.0, 147.0]}
    assert c.port_gets(pt) == 45.0                       # the books said 130.5
    side = {"item": "iron-plate", "source": (80.5, -10.5), "boost_into": tap, "port": {"end": [1, 1]}}
    c.links.append(side)
    c._source_rate = lambda root, item: 37.5
    assert c.port_gets(pt) == 82.5                       # a belt fed in from the side adds to it
    no_ins = dict(tap, ins=None)
    c.links = [belt, no_ins]
    assert c.port_gets(pt) == 130.5                      # a splitter or a line's tail: the source's share


def test_an_outpost_fed_columns_line_carries_no_more_than_its_drills_mine():
    """Run 4f7178: column 33's seven stone furnaces were booked at 131 iron a minute; its outpost had four
    drills, two of them mined out."""
    from brain.crew import Crew
    c = Crew.__new__(Crew)
    c.base = Base()
    col = c.base.add(Site("smelter_column", {"resource": "iron-ore", "count": 7, "into": "line",
                                             "ports": [{"item": "iron-ore", "fed": True, "trunk": True,
                                                        "end": [0, 0]}]}, (0, 0)))
    col.entities = [("stone-furnace", 0, k, "north") for k in range(7)]
    ln = {"item": "iron-plate", "source": (0, 0), "tiles": [], "rate": 131.25}
    out = c.base.add(Site("outpost", {"resource": "iron-ore", "count": 4, "trunk_to": col.id}, (50, 50)))
    out.entities = [("electric-mining-drill", k, 50, "north") for k in range(4)]
    assert c.line_rate(ln, col) == 120.0
    out.health = {"working": 2, "no_minable_resources": 2}
    assert c.line_rate(ln, col) == 60.0
