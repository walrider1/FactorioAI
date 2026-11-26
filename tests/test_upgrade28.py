"""Self-upgrade #28: fixes from the architecture review of run 20261125-214645."""
from types import SimpleNamespace

import pytest

from brain import triage
from brain.modules import Site
from brain.overseer import goal_item
from brain.world import BuildError
from test_lifecycle import _copper_row
from test_upkeep_outposts import _crew


def test_a_port_goes_to_a_source_that_covers_it_before_a_nearer_short_one():
    crew, w, _ = _crew()
    fab = crew.base.add(Site("factory", {"item": "logistic-science-pack"}, (0, 0)))
    small = crew.base.add(Site("electric_mine", {"resource": "iron-ore"}, (10, 0)))
    big = crew.base.add(Site("electric_mine", {"resource": "iron-ore"}, (30, 0)))
    crew.outputs = lambda item: [(10.5, 0.5, small), (30.5, 0.5, big)]
    crew._source_rate = lambda root, item: 56.25 if root[0] < 20 else 112.5
    got = crew._free_outputs("iron-plate", fab, 90.75)
    assert [s for *_, s in got] == [big]
    assert len(crew._free_outputs("iron-plate", fab, 200.0)) == 2     # none covers it: every source, as before


def test_triage_relinks_from_a_covering_source_instead_of_building_a_mine():
    crew = SimpleNamespace(_free_outputs=lambda item, site, need: [(1.5, 1.5, None), (30.5, 0.5, Site("x", {}, (0, 0)))],
                           _budget=lambda item: {(1.5, 1.5): [91.0, 56.0]},
                           _left=lambda b, root, item: 56.0 if root == (1.5, 1.5) else 112.5)
    hit = triage._covering(crew, "iron-plate", None, (1.5, 1.5), 91.0)
    assert hit[:2] == (30.5, 0.5)
    assert "source-small" in triage.RELINK_CAUSES


def test_a_feed_that_puts_nothing_in_fails():
    crew, w, _ = _crew()
    site = crew.base.add(Site("steam_power", {}, (0, 0), chests=[(1.5, 1.5, "coal-in")]))
    w.inventory = {"coal": 100}
    crew.gather = lambda item, n: None
    w.insert = lambda x, y, item, n: 0
    with pytest.raises(BuildError, match="no coal went into"):
        crew.feed(site.id, "coal", 60)
    w.insert = lambda x, y, item, n: n
    assert crew.feed(site.id, "coal", 60).startswith("fed 60")


def test_the_coal_round_stops_once_it_has_enough_and_takes_belt_sources_last():
    from brain.crew import Foreman
    crew, w, _ = _crew()
    fm = Foreman(crew)
    w.inventory, w.pos = {"coal": 0}, (0, 0)
    taken = []

    def extract(x, y, item, n):
        taken.append((x, y))
        w.inventory["coal"] += n
        return n
    w.extract = extract
    w.fuel_units = lambda: 99
    crew.no_room = lambda item: False
    crew.coal_chests = lambda: [(1.5, 1.5), (2.5, 2.5), (3.5, 3.5)]
    crew.links = [{"item": "coal", "source": (1.5, 1.5)}]
    fm._coal()
    assert taken == [(2.5, 2.5)] and w.inventory["coal"] == fm.COAL_ROUND


def test_an_upsize_replace_is_never_dropped_as_a_move():
    crew, w, _ = _crew()
    site = crew.base.add(Site("factory", {"item": "engine-unit", "per_minute": 2,
                                          "ports": [{"item": "iron-plate", "fed": [0, 0]}]}, (0, 0)))
    crew.inputs_stuck = lambda sid: False
    same = {"do": "replace", "site": site.id, "with": {"module": "factory", "item": "engine-unit", "per_minute": 2}}
    assert crew.stale_replace(same)
    bigger = {"do": "replace", "site": site.id, "with": {"module": "factory", "item": "engine-unit", "per_minute": 12}}
    assert crew.stale_replace(bigger) is None


def test_a_forbid_holds_a_relink_even_of_a_short_item():
    assert goal_item({"do": "relink", "site": 29, "item": "iron-plate"}) is None
    assert goal_item({"do": "build", "module": "electric_mine", "resource": "iron-ore"}) == "iron-plate"


def test_relink_of_a_station_lays_its_pack_belts_again():
    crew, w, _ = _crew()
    st = crew.base.add(Site("labs", {"station": True, "ports": [{"pack": "automation-science-pack", "end": (0, 0)}]},
                            (0, 0)))
    rec = {"kind": "science", "to_site": st.id, "item": "automation-science-pack", "port": st.params["ports"][0]}
    crew.links = [rec]
    gone = []
    crew._unlink = lambda q: (gone.append(q), crew.links.remove(q))
    crew.link_science = lambda: "linked 1"
    msg = crew.relink(st.id, "automation-science-pack")
    assert gone == [rec] and "linked 1" in msg


def test_the_first_steel_column_is_sized_to_the_furnace():
    crew, w, _ = _crew()
    w.kn = crew.kn
    crew.kn.unlocked = set()
    assert crew.steel_column() == 4                  # stone: 3.75 steel a minute each
    crew.kn.unlocked = {"steel-furnace"}
    assert crew.steel_column() == 2                  # steel: 7.5 each


def test_a_boost_books_only_what_its_chain_lacks():
    crew, w, _ = _crew()
    rates = {(0.5, 0.5): 56.25, (9.5, 9.5): 487.5}
    crew._source_rate = lambda root, item: rates.get(tuple(root), 0.0)
    chain = {"item": "iron-plate", "source": (0.5, 0.5), "port": {"per_minute": 90.0}}
    crew.links = [chain, {"item": "iron-plate", "source": (9.5, 9.5), "boost_into": chain, "boost_of": (0.5, 0.5),
                          "port": {}}]
    b = crew._budget("iron-plate")
    assert b[(0.5, 0.5)] == [90.0, 90.0]
    assert b[(9.5, 9.5)] == [33.75, 487.5]           # the rest of the 487/min line stays linkable


def test_the_pole_on_a_line_start_moves_beside_it():
    crew, w, _ = _crew()
    site, ln = _copper_row(crew)
    pole = ("small-electric-pole", -7.5, 84.5, "north")
    site.entities.append(pole)
    site.poles.append((-8, 84))
    w.seen = {(x, y) for x in range(-20, 0) for y in range(78, 92)}
    w.blocked, w.keepout = (lambda t: False), set()
    placed, picked = [], []
    w.place = lambda name, x, y, d, tiles: placed.append((name, x, y))
    w.pickup = lambda *e: picked.append(e) or True
    w.free = lambda tiles: True
    crew.make = lambda kit, extra=False: None
    crew._recheck_power_near = lambda spots: None
    crew.existing_poles = lambda: {(-8, 84), (-13, 86)}
    [note] = crew._free_line_heads()
    assert picked == [pole] and len(placed) == 1 and "moved" in note
    x, y = placed[0][1] - 0.5, placed[0][2] - 0.5
    assert (x, y) not in crew.line_heads() and (x, y) not in {tuple(t) for t in ln["tiles"]}
    assert crew._free_line_heads() == []             # once per line
