"""Jams inside a site (purity.cell_check / purge_cell) and the gap audit that caused one (gaps.holes).

Layout: site 22 of run 20261127-101219 (circuits slot), as the game showed it at minute 281. The iron line runs
south down x=-41 and ends turning west at y=-107 into the cable line, which runs north up x=-43 (the cable
inserters drop at y=-112 and y=-109), turns east at y=-113 and south down x=-42 to a dead end at y=-110.
"""

from bots.brain import purity
from bots.brain.modules import Site

T = "transport-belt"


def belt(x, y, d, lanes=(), fed_by=(), feeds=()):
    return {"name": T, "x": x + 0.5, "y": y + 0.5, "direction": d, "lanes": [dict(), dict(lanes)],
            "fed_by": [[a + 0.5, b + 0.5] for a, b in fed_by], "feeds": [[a + 0.5, b + 0.5] for a, b in feeds]}


def site22_entities():
    I, C = "iron-plate", "copper-cable"
    b = [
        # iron line down x=-41, then west at y=-107 into the cable line
        belt(-41, -110, "south", {I: 4}, [(-41, -111)], [(-41, -109)]),
        belt(-41, -109, "south", {I: 4}, [(-41, -110)], [(-41, -108)]),
        belt(-41, -108, "south", {I: 4}, [(-41, -109)], [(-41, -107)]),
        belt(-41, -107, "west", {I: 2}, [(-41, -108)], [(-42, -107)]),
        belt(-42, -107, "west", {I: 4}, [(-41, -107)], [(-43, -107)]),
        # the cable line: north up x=-43, east at y=-113, south down x=-42 to a dead end
        belt(-43, -107, "north", {I: 1}, [(-42, -107)], [(-43, -108)]),
        belt(-43, -108, "north", {I: 4}, [(-43, -107)], [(-43, -109)]),
        belt(-43, -109, "north", {I: 2, C: 2}, [(-43, -108)], [(-43, -110)]),
        belt(-43, -110, "north", {I: 1, C: 3}, [(-43, -109)], [(-43, -111)]),
        belt(-43, -111, "north", {I: 2, C: 2}, [(-43, -110)], [(-43, -112)]),
        belt(-43, -112, "north", {I: 2, C: 2}, [(-43, -111)], [(-43, -113)]),
        belt(-43, -113, "east", {C: 2}, [(-43, -112)], [(-42, -113)]),
        belt(-42, -113, "south", {I: 1, C: 1}, [(-43, -113)], [(-42, -112)]),
        belt(-42, -112, "south", {I: 1, C: 3}, [(-42, -113)], [(-42, -111)]),
        belt(-42, -111, "south", {I: 4}, [(-42, -112)], [(-42, -110)]),
        belt(-42, -110, "south", {C: 4}, [(-42, -111)], []),
    ]
    ins = [
        {"name": "inserter", "x": -43.5, "y": -111.5, "direction": "east", "status": "waiting_for_space_in_destination",
         "pickup": [-44.5, -111.5], "drop": [-42.5, -111.5]},
        {"name": "inserter", "x": -43.5, "y": -108.5, "direction": "east", "status": "waiting_for_space_in_destination",
         "pickup": [-44.5, -108.5], "drop": [-42.5, -108.5]},
        {"name": "long-handed-inserter", "x": -39.5, "y": -110.5, "direction": "east",
         "status": "waiting_for_source_items", "pickup": [-41.5, -110.5], "drop": [-37.5, -110.5]},
    ]
    return b + ins


class W:
    def __init__(self, ents):
        self.ents = ents
        self.picked, self.placed, self.acts = [], [], []
        self.keepout = set()

    def observe(self, r, x=0, y=0, include=None, names=None, belts=False):
        return {"entities": [e for e in self.ents if names is None or e["name"] in names]}

    def footprint(self, name, x, y, d):
        return {(int(x // 1), int(y // 1))}

    def pickup(self, name, x, y, d):
        self.picked.append((x, y))
        return True

    def place(self, name, x, y, d, fp):
        self.placed.append((x, y, d))

    def near(self, x, y):
        pass

    def act(self, a):
        self.acts.append(a)
        return {"ok": True, "data": {"merges": []}}


class Base:
    def __init__(self, sites):
        self.sites = sites

    def get(self, i):
        return next((s for s in self.sites if s.id == i), None)


class Crew:
    def __init__(self, site, ents):
        self.w = W(ents)
        self.base = Base([site])
        self.links = []

    def _minute(self):
        return 100.0


def site22():
    ents = site22_entities()
    s = Site("factory", {"item": "electronic-circuit", "segment_block": "1/make-0"}, (-37, -101))
    s.id = 22
    s.entities = [(e["name"], e["x"], e["y"], e["direction"]) for e in ents if e["name"] == T]
    s.health = {"waiting_for_space_in_destination": 4, "item_ingredient_shortage": 1}
    return s, ents


def test_jam_found_with_iron_as_the_intruder():
    s, ents = site22()
    j = purity.jams(Crew(s, ents), s)
    assert j and j[0]["intruder"] == "iron-plate"


def test_cell_check_alerts_and_purge_cuts_the_iron_turn():
    s, ents = site22()
    crew = Crew(s, ents)
    alerts = purity.cell_check(crew)
    assert alerts and alerts[0]["key"] == "cell-jam-22"
    assert alerts[0]["fix"] == {"do": "purge", "site": 22, "item": "iron-plate", "cell": True}
    note = purity.purge_cell(crew, 22, "iron-plate")
    # the leak is the first turn upstream of every drop that carries iron: (-42, -107) heading west
    rotated = [a for a in crew.w.acts if a.get("type") == "rotate"]
    assert rotated and (rotated[0]["x"], rotated[0]["y"]) == (-41.5, -106.5), note
    assert len(crew.w.placed) >= 5          # the cable line's iron-holding belts taken up and laid again


def test_no_jam_on_a_clean_line():
    s, ents = site22()
    for e in ents:
        if e["name"] == T:
            e["lanes"] = [{}, {"copper-cable": 2}]
    assert purity.jams(Crew(s, ents), s) == []


def test_output_line_leaving_the_site_is_not_a_jam():
    s, ents = site22()
    for e in ents:
        if e["name"] == T and (e["x"], e["y"]) == (-41.5, -109.5):
            e["feeds"] = [[-41.5, -108.5]]
    assert purity.jams(Crew(s, ents), s) == []


def test_gap_audit_never_bridges_into_an_output_line_tail():
    """The original cause: the cable line's first belts (behind the cable drops) were read as orphans."""
    from bots.brain import gaps
    s, ents = site22()
    # as built from the design: the iron line ends at (-41, -108) heading south; no turn into the cable line
    keep = [e for e in ents if e["name"] != T or (e["x"], e["y"]) not in
            {(-40.5, -106.5), (-41.5, -106.5), (-40.5, -107.5)}]
    for e in keep:
        if (e.get("x"), e.get("y")) == (-42.5, -106.5):
            e["fed_by"] = []
        if (e.get("x"), e.get("y")) == (-40.5, -108.5):
            e["feeds"] = []
    s.params.pop("segment_block")
    s.entities = [(e["name"], e["x"], e["y"], e["direction"]) for e in keep if e["name"] == T]

    class GW:
        water, cliffs = (), ()

        def footprint(self, name, x, y, d):
            return {(int(x // 1), int(y // 1))}

    assert gaps.holes(GW(), s, keep) == []
