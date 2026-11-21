"""Belt links that stop carrying (run 20261119-202811), outposts that keep building columns, and coal mines that
run dry under the belts they feed."""

import math
from types import SimpleNamespace

import pytest

from brain import assess, belts, upkeep
from brain.crew import Foreman
from brain.modules import Site
from brain.world import BuildError
from test_fuel_and_links import GridWorld, _link_crew
from test_upkeep_outposts import _crew as _upkeep_crew


# ------------------------------------------------------------------ undergrounds never pair with another link's
def test_a_new_underground_never_spans_another_links_pair_on_the_same_axis():
    """The iron link's pair runs south from (0, 3) to (0, 8); a wall at y=5 forces the new link under it. On
    column 0 its pair would sit inside the iron pair and the game would pair them up, so it goes elsewhere."""
    wall = {(x, 5) for x in range(-8, 9)}
    taken = wall | {(0, 3), (0, 8)}
    free = lambda t: t not in taken
    cover = belts.ug_cover([((0, 3), "south"), ((0, 8), "south")])
    assert {(0, y) for y in range(3, 9)} <= cover["ns"] and not cover["ew"]
    plain = belts.route_belt(free, (0, 10), (0, 0), (0, -1))
    assert any(s[0] == "ug" and s[1][0] == 0 for s in plain)          # without the check: straight up column 0
    steps = belts.route_belt(free, (0, 10), (0, 0), (0, -1), ug_block=cover)
    assert steps and not any(s[0] == "ug" and s[1][0] == 0 for s in steps)
    # crossing it at right angles is fine
    assert not belts.ug_cover([((0, 3), "south"), ((0, 8), "south")])["ew"]


def test_an_underground_the_game_turned_round_fails_the_link_so_it_is_routed_around():
    placed = []

    class Run:
        def place(self, name, x, y, d, kind):
            placed.append((x, y, d, kind))
            return {"ok": True, "data": {"entity": {"name": name, "x": x, "y": y, "direction": "south",
                                                    "belt_type": "output", "pairs_with": [x, y - 1]}}}

    w = SimpleNamespace(run=Run(), pos=(50.0, 50.0), used=set(), inventory={"underground-belt": 2},
                        clear=lambda tiles: None, near=lambda x, y: None, step_out=lambda tiles: None)
    with pytest.raises(BuildError) as e:
        belts.lay_belt(w, [("ug", (0, 4), "north", "input"), ("ug", (0, -1), "north", "output")])
    assert e.value.tile == (0, 4) and "paired with another underground" in str(e.value)
    assert len(placed) == 1                                            # stopped at the bad piece


# ------------------------------------------------------------------ the audit finds a link that stopped carrying
def _broken_link_crew(found_type="output", found_dir="south"):
    w = GridWorld()
    crew = _link_crew(w)
    crew.links, crew.urgent, crew.log = [], [], lambda *a: None
    boiler = crew.base.add(Site("steam_power", {"engines": 2}, (5, -6)))
    port = {"item": "coal", "end": [0, -5], "approach": [0, -4], "fuel": True, "fed": [0.5, 2.5]}
    steps = [("belt", (0, 1), "north"), ("ug", (0, 0), "north", "input"), ("ug", (0, -3), "north", "output"),
             ("belt", (0, -4), "north"), ("belt", (0, -5), "north")]
    ents = [("inserter", 0.5, 2.5, "north")] + [
        ("underground-belt" if s[0] == "ug" else "transport-belt", s[1][0] + 0.5, s[1][1] + 0.5, s[2]) for s in steps]
    rec = {"source": (0.5, 3.5), "to_site": boiler.id, "item": "coal", "belts": 5, "kind": "item", "port": port,
           "steps": steps, "ins": ((0, 2), "north", "inserter"), "entities": ents, "minute": 10.0}
    crew.links.append(rec)
    boiler.entities.extend(ents)
    standing = []
    for name, x, y, d in ents[1:]:
        e = {"name": name, "x": x, "y": y, "direction": d}
        if name == "underground-belt":
            e["belt_type"] = "input" if y > 0 else "output"
        standing.append(e)
    standing[1].update(belt_type=found_type, direction=found_dir)    # the entrance the game turned round
    calls = []
    w.observe = lambda r, **k: (calls.append((r, k)), {"entities": standing, "radius": r})[1]
    return crew, w, rec, port, calls


def test_a_link_whose_underground_was_turned_round_is_taken_up_and_laid_again():
    crew, w, rec, port, calls = _broken_link_crew()
    notes = crew.audit_links()
    assert len(notes) == 1 and "broken at (0.5, 0.5)" in notes[0] and "found output facing south" in notes[0]
    assert rec not in crew.links and port["fed"] is None              # unfed: the link step lays it again
    assert len(w.picked) == len(rec["entities"])                      # what stood of it came up
    assert len(calls) == 1                                            # one look covers a short link
    f = Foreman.__new__(Foreman)
    f.crew, f.problems = crew, []
    crew2, *_ = _broken_link_crew()
    f.crew = crew2
    Foreman._audit_links(f)
    assert crew2.urgent == [{"do": "link", "reflex": "foreman: a broken belt link was taken up"}]
    assert any("broken at" in p for p in f.problems)
    Foreman._audit_links(f)                                           # not again within AUDIT_GAP_MIN
    assert len(crew2.urgent) == 1


def test_a_link_that_stands_as_planned_is_left_alone():
    crew, w, rec, port, _ = _broken_link_crew(found_type="input", found_dir="north")
    assert crew.audit_links() == [] and rec in crew.links and not w.picked


# ------------------------------------------------------------------ an outpost builds one column, not one per link step
def test_an_outpost_builds_one_column_even_when_the_columns_own_link_step_comes_back_to_it(monkeypatch):
    from brain import outposts
    crew, w, _ = _upkeep_crew()
    crew.kn.unlocked = set()
    out = crew.base.add(Site("outpost", {"resource": "iron-ore", "count": 4, "per_minute": 120.0, "head": [300, 300],
                                         "trunk_to": None}, (300, 300)))
    built, seen = [], {}

    def build(goal):                       # the column's build ends with the link step, which finishes outposts
        built.append(goal)
        crew.base.add(Site("smelter_column", {"resource": "iron-ore", "count": goal["count"]}, (10, 10)))
        seen["pending"] = crew.pending_trunks()
        seen["again"] = crew._finish_outpost(out)
        return "built"

    crew.build = build
    monkeypatch.setattr(outposts, "lay_trunk", lambda c, o, col: {"belts": 40})
    msg = crew._finish_outpost(out)
    assert len(built) == 1 and len(crew.base.of("smelter_column")) == 1
    assert seen["pending"] == [] and "being finished already" in seen["again"]
    col = crew.base.of("smelter_column")[0]
    assert col.params["outpost"] == out.id and f"column site {col.id}" in msg


def test_a_column_that_stands_although_its_build_failed_is_claimed_not_built_again(monkeypatch):
    crew, w, _ = _upkeep_crew()
    crew.kn.unlocked = set()
    out = crew.base.add(Site("outpost", {"resource": "iron-ore", "count": 2, "head": [300, 300], "trunk_to": None},
                             (300, 300)))

    def build(goal):                       # placed, then its power step failed
        crew.base.add(Site("smelter_column", {"resource": "iron-ore", "count": goal["count"]}, (10, 10)))
        raise BuildError("no pole route")

    crew.build = build
    msg = crew._finish_outpost(out)
    assert crew.base.of("smelter_column")[0].params["outpost"] == out.id and "stands" in msg


# ------------------------------------------------------------------ a coal mine that runs dry moves to fresh ore
def _dry_coal_mine(crew, into="chest"):
    s = Site("electric_mine", {"resource": "coal", "count": 2, "into": into}, (-30, 25),
             entities=[("electric-mining-drill", -30.5, 26.5, "north"), ("electric-mining-drill", -27.5, 26.5, "north")],
             chests=[(-29.5, 26.5, "coal")])
    s.health = {"no_minable_resources": 1, "working": 1}
    return crew.base.add(s)


def test_a_dry_coal_mine_feeding_a_belt_is_rebuilt_on_fresh_ore_at_once():
    crew, w, _ = _upkeep_crew()
    mine = _dry_coal_mine(crew)
    crew.links = [{"source": (-29.5, 26.5), "to_site": 7, "item": "coal", "kind": "item", "port": {}, "entities": []}]
    fix = upkeep.depleted_fix(crew, mine)
    assert fix == {"do": "replace", "site": mine.id, "force": True,
                   "with": {"do": "build", "module": "electric_mine", "resource": "coal", "count": 2, "into": "chest"}}
    alerts = assess.depleted_alerts(crew)
    assert len(alerts) == 1 and alerts[0]["level"] == "urgent" and alerts[0]["fix"] == fix
    assert alerts[0]["key"].startswith(assess.CRITICAL)                 # the rule floor acts on it by itself
    decided = assess.RuleAssessor().decide({}, alerts, set())
    assert decided["urgent_goals"] == [fix]
    assert upkeep.needs(crew, mine)[0]["fix"] == fix


def test_a_dry_mine_is_only_removed_when_its_patch_has_no_room_left():
    crew, w, _ = _upkeep_crew()
    mine = _dry_coal_mine(crew)
    crew._patch_full = {"coal": (29.0, crew._base_sig())}
    assert upkeep.depleted_fix(crew, mine) == {"do": "remove", "site": mine.id, "force": True}
    crew.removals += 1                                                  # the base changed: try the patch again
    assert upkeep.depleted_fix(crew, mine)["do"] == "replace"


def test_a_mine_with_most_drills_still_mining_is_not_moved():
    crew, w, _ = _upkeep_crew()
    mine = _dry_coal_mine(crew)
    mine.entities.append(("electric-mining-drill", -24.5, 26.5, "north"))
    mine.health = {"no_minable_resources": 1, "working": 2}
    assert assess.depleted_alerts(crew) == []
