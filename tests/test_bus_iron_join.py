"""Run 20261127-101219: green science stood at 0/min for 115 game minutes.

- The bus head's only iron belt was 27 steps, all but five of them underground pairs: no three straight belts in a
  row, so no spare source could join it from the side ("its belts have no free straight stretch").
- Triage then called it "source-short" and built four more iron mines, none of which could join that belt either.
- A forced overseer move of the old organic gear feeder ran after the bus had its own gear slot factory, and spent
  a new 131/min iron line on a gear factory whose gears went nowhere.
- The oil scout never searched: every well is its own patch, so "the nearest two patches" were the two wells
  already known, and the new field the scan saw was never looked at.
"""

from types import SimpleNamespace

from brain import triage
from brain.crew import Crew
from brain.overseer import floorplan_guard
from brain.world import FreeWorld

# the bus head's iron link as the run laid it (steps as crew.links records them)
BUS_IRON = [("belt", (17, -59), "west"), ("ug", (16, -59), "west", "input"), ("ug", (12, -59), "west", "output"),
            ("ug", (11, -59), "west", "input"), ("ug", (6, -59), "west", "output"),
            ("ug", (-22, -59), "west", "output"), ("belt", (-24, -59), "north"),
            ("ug", (-24, -60), "north", "input"), ("ug", (-24, -83), "north", "output"),
            ("belt", (-24, -84), "north"), ("belt", (-24, -85), "east"), ("belt", (-23, -85), "east")]


def _join_crew(taken=()):
    c = SimpleNamespace(all_ports=lambda: [], _strip_tiles=lambda: set())
    c._belt_free = lambda t: t not in taken
    return c


def test_a_belt_fed_by_an_underground_exit_or_a_turn_can_be_joined_from_the_side():
    c = _join_crew()
    ends = {tuple(p["end"]) for p, _ in Crew._sideload_ports(c, [{"steps": BUS_IRON}], "iron-plate")}
    assert (-25, -84) in ends and (-23, -84) in ends            # beside the belt after the underground exit
    assert (-23, -84) in ends or (-23, -86) in ends              # beside the last belt, fed straight by the turn
    assert not any(e[1] == -59 and e[0] in (-23, -25) for e in ends)   # (-24,-59) is a turn: never joined


def test_a_turn_itself_is_never_a_join_point():
    steps = [("belt", (0, 0), "east"), ("belt", (1, 0), "north"), ("belt", (1, -1), "north")]
    ends = {tuple(p["end"]) for p, _ in Crew._sideload_ports(_join_crew(), [{"steps": steps}], "iron-plate")}
    assert (2, 0) not in ends                                    # nothing beside the turn at (1,0)
    assert ends == {(0, -1), (2, -1)}                            # (1,-1) is fed straight by it


def _site(id, kind, **params):
    return SimpleNamespace(id=id, kind=kind, params=params)


class _Base:
    def __init__(self, sites):
        self.sites = sites

    def get(self, i):
        return next((s for s in self.sites if s.id == i), None)

    def of(self, kind):
        return [s for s in self.sites if s.kind == kind]


def test_a_move_that_would_rebuild_an_item_the_bus_already_makes_is_dropped_even_forced():
    base = _Base([_site(18, "factory", item="iron-gear-wheel"),
                  _site(25, "factory", item="iron-gear-wheel", slot="make-1", bus_out=["iron-gear-wheel"])])
    c = SimpleNamespace(base=base)
    move = {"do": "replace", "site": 18, "force": True,
            "with": {"do": "build", "module": "factory", "item": "iron-gear-wheel", "per_minute": 34.1}}
    why = Crew.bus_superseded(c, move)
    assert why and "site 25" in why and "make-1" in why
    c.bus_superseded = lambda g: Crew.bus_superseded(c, g)
    assert floorplan_guard(c, move) == why
    other = {**move, "with": {**move["with"], "item": "electronic-circuit"}}
    assert Crew.bus_superseded(c, other) is None                 # nothing on the bus makes circuits here
    assert Crew.bus_superseded(c, {"do": "replace", "site": 25, "with": move["with"]}) is None   # the slot itself


def test_no_more_mines_when_spare_sources_wait_for_a_join_point():
    root = (18.5, -58.5)
    site = SimpleNamespace(id=19, kind="bus", health={"waiting_for_source_items": 3}, params={"ports": [
        {"item": "iron-plate", "end": (-23, -85), "fed": list(root), "per_minute": 130.5}]}, electric=True,
        powered=True)
    src = SimpleNamespace(id=17, kind="smelter_column", health={"working": 7}, params={})
    crew = SimpleNamespace(base=_Base([site, src]), _link_fails={("boost",) + root: {
        "reason": "its belts have no free straight stretch to join from the side"}},
        links=[{"item": "iron-plate", "source": root, "port": {"end": (-23, -85)}}])
    crew._coal_root = lambda ln: tuple(ln["source"])
    crew._spare = lambda item: [(17.5, -50.5, None)]
    crew.boost_ready = lambda r, item: False
    crew._budget = lambda item: {root: [130.5, 112.5]}
    crew._source_site = lambda r, item: src
    d = triage.diagnose(crew, 19, "iron-plate")
    assert d["cause"] == "source-short" and d["fix"] is None and "more mines would not help" in d["why"]
    crew.boost_ready = lambda r, item: True                      # a join point now: the link step feeds it in
    assert triage.diagnose(crew, 19, "iron-plate")["fix"] == {"do": "link"}


def test_the_oil_scan_looks_at_the_new_field_not_the_two_known_wells():
    scanned = [{"nearest": {"x": x, "y": y}, "centre": {"x": x, "y": y}, "tiles": 1}
               for x, y in [(-151.5, 74.5), (-156.5, 69.5), (-230.5, -121.5), (-241.5, -122.5), (-238.5, -131.5)]]
    looked = []
    w = object.__new__(FreeWorld)
    w.resources = {"crude-oil": {(-152, 74), (-157, 69)}}
    w.pos, w.scan_radius, w.oil_rings, w.oil_home = (0.0, 0.0), 384, 0, None
    w.run = SimpleNamespace(scan=lambda *a, **k: {"ok": True, "data": {"patches": scanned}})
    w._seen_tick = lambda res: None
    w.look = lambda x, y, radius=32: looked.append((x, y))
    wells = w.oil_wells((0, 0), more=True)
    assert looked and looked[0][0] < -200                        # the new field got its full look
    assert len(wells) == 5 and w.oil_rings == 0                  # every well the scan saw is known now
