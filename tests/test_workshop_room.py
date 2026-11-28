"""A workshop floor keeps room below it for its own growth (run 4f7178 review, step 5): the oil floor was boxed
in, the sulfur and solid fuel workshops failed six times on "no pipe route for crude-oil", and no oil zone was
ever reserved. The room is reserved land no other building, belt or pole may use; only that floor grows into it."""

from types import SimpleNamespace

from brain import workshop as wsm
from brain.workshop import District, Workshop
from brain.zones import Zones


def _world():
    z = Zones()
    seen = {(x, y) for x in range(-50, 150) for y in range(-50, 250)}
    w = SimpleNamespace(zones=z, seen=seen, resources={}, keepout=set(), used=set(),
                        survey_area=lambda *a, **k: None, log=lambda *a: None)
    w.blocked = lambda t: t in w.used or z.blocks({t})
    return w, z


def test_a_new_floor_keeps_its_growth_room_and_grows_into_it():
    w, z = _world()
    ws = Workshop.__new__(Workshop)
    d = District(0, 0, 4, 3)
    d.idx = 0
    kept = ws._keep_room(w, d)
    x0, y0, x1, y1 = d.box()
    assert kept == (x1 - x0 + 1) * wsm.FLOOR_ROOM_ROWS * wsm.PITCH
    below = (x0, y1 + 1)
    assert z.blocks({below}) and below in z.refused_slots()              # no building, belt or pole of others
    ws.district = d
    d.nets = SimpleNamespace(walk=set(), noend=set())
    d.walkways = lambda: set()
    d.gap_tiles = lambda: set()
    assert ws._grow(w, None, d.cols, d)                                   # one more row: into its own room
    assert not z.blocks({below})                                          # given back to the floor
    assert z.room_of(ws._room_key(d)) and len(z.room_of(ws._room_key(d))) < kept


def test_another_floor_cannot_take_a_kept_room():
    w, z = _world()
    ws = Workshop.__new__(Workshop)
    d = District(0, 0, 4, 3)
    d.idx = 0
    ws._keep_room(w, d)
    room = z.room_of(ws._room_key(d))
    assert not ws._area_ok(w, room)                                       # someone else's: refused
    assert ws._area_ok(w, room, room)                                     # its own: fine
