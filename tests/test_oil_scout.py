"""Scouting for crude oil uses what is known (run 20261120-211555: with 12+ wells found and a pumpjack on them, a
scout goal walked rings ~700 tiles out for an oil "patch" crude oil never forms; it was sent because a plastic
workshop could not route its crude pipe over 470 tiles of ground nobody had looked at)."""

import math
from types import SimpleNamespace

import pytest

from brain import outposts
from brain.crew import Crew
from brain.workshop import Workshop
from brain.world import BuildError


class _Base:
    def __init__(self, jacks=(), centre=(0, -50)):
        self.centre = centre
        self._jacks = [SimpleNamespace(entities=[("pumpjack", x, y, "north") for x, y in jacks])]

    def of(self, kind):
        return self._jacks if kind == "pumpjack" else []


def _crew(wells, jacks=()):
    w = SimpleNamespace(resources={"crude-oil": set(wells)}, pos=(0, -50), walked=[])
    w.oil_wells = lambda near=None: w.walked.append(near) or []
    w.oil_searched = lambda: 1200
    c = SimpleNamespace(w=w, base=_Base(jacks), OIL_FIELD_GAP=Crew.OIL_FIELD_GAP)
    c.oil_fields = lambda: Crew.oil_fields(c)
    return c


WEST = [(-499, -112), (-506, -110), (-501, -120), (-501, -123), (-507, -117), (-512, -116)]
NORTH = [(40, -400), (44, -404)]


def test_known_oil_is_grouped_into_fields_nearest_first_with_what_is_pumped():
    c = _crew(WEST + NORTH, jacks=[(-498.5, -111.5)])
    north, west = Crew.oil_fields(c)
    assert north["wells"] == 2 and north["pumped"] == 0 and north["distance"] < west["distance"]
    assert west["wells"] == 6 and west["pumped"] == 1 and abs(west["centre"][0] + 504) <= 1


def test_scouting_for_oil_reports_the_known_fields_and_walks_nowhere():
    c = _crew(WEST, jacks=[(-498.5, -111.5)])
    msg = Crew.scout_oil(c)
    assert "6 wells in 1 field" in msg and "Nothing to scout" in msg and c.w.walked == []


def test_with_no_oil_known_it_carries_the_ring_search_on_from_the_base():
    c = _crew([])
    with pytest.raises(BuildError, match="no crude oil within"):
        Crew.scout_oil(c)
    assert c.w.walked == [(0, -50)]


def test_ore_scouting_rings_go_round_the_base_not_where_the_character_stands():
    went = []
    w = SimpleNamespace(patches=[], pos=(600, 600), oil_step=lambda: 100.0, doing=lambda *a: None,
                        sleep_ticks=lambda n: None, scan=lambda: None)
    w.go = lambda x, y: went.append((x, y))
    assert outposts.scout(w, "iron-ore", [], max_rings=1, home=(0, 0)) is None
    assert all(math.isclose(math.dist(p, (0, 0)), 100.0) for p in went) and len(went) == 8


def test_the_way_to_a_far_oil_field_is_looked_at_once_before_the_pipe_is_routed():
    looked = []
    w = SimpleNamespace(seen=set(), doing=lambda *a: None)
    w.look = lambda x, y, radius=32: looked.append((round(x), round(y)))
    d = SimpleNamespace(box=lambda: (-40, -110, 0, -90))
    ws = Workshop()
    ws._survey_line(w, d, [(-498, -113)])
    assert len(looked) >= 10 and looked[-1] == (-498, -113)              # all along the ~480 tiles
    assert all(-498 <= x <= -20 and -114 <= y <= -99 for x, y in looked)
    n = len(looked)
    ws._survey_line(w, d, [(-498, -113)])
    assert len(looked) == n                                               # not walked twice
    ws._survey_line(w, d, [(-30, -95)])                                  # a source by the floor: nothing to do
    assert len(looked) == n


# ------------------------------------------------------------------ the patrol (run 20261120-211555: every 3 game
# minutes of a workshop build it walked ~500 tiles to look at two pumpjacks, then back)
def _patrol_crew():
    from brain.crew import Crew, Foreman
    from brain.modules import Site
    from test_brain_gm import StubKn, StubWorld

    class W(StubWorld):
        observe_range = 256

        def __init__(self):
            super().__init__(StubKn())
            self.walks, self.looks = [], []

        def near(self, x, y, dist=1.5):
            self.walks.append((x, y))
            self.pos = (x, y)

        def observe(self, radius, x=None, y=None, **kw):
            assert math.dist((x, y), self.pos) <= self.observe_range
            self.looks.append((x, y))
            return {"entities": []}

        def fuel_units(self):
            return 100

    crew = Crew(W(), log=lambda *a: None)
    far_jack = crew.base.add(Site("pumpjack", {"count": 2}, (-500, -112),
                                  entities=[("pumpjack", -498.5, -111.5, "north")]))
    near_jack = crew.base.add(Site("pumpjack", {"count": 1}, (120, 0), entities=[("pumpjack", 120.5, 0.5, "north")]))
    burner = crew.base.add(Site("burner_mine", {"resource": "iron-ore", "count": 1}, (-480, 40),
                                entities=[("burner-mining-drill", -480, 40, "north")], burners=[(-480, 40)]))
    return crew, crew.w, far_jack, near_jack, burner


def test_the_patrol_never_walks_out_just_to_look_at_a_hands_free_site():
    from brain.crew import Foreman
    crew, w, far_jack, near_jack, burner = _patrol_crew()
    assert not Foreman.needs_hands(far_jack) and Foreman.needs_hands(burner)
    crew.foreman._sites()
    assert (near_jack.centre[0], near_jack.centre[1]) in w.looks          # in range: looked at from where it stands
    assert all(math.dist(p, burner.centre) < 25 for p in w.walks)         # the only walk: to the burner's fuel
    assert not any(math.dist(p, far_jack.centre) < 100 for p in w.walks)   # never walked to; looked at only
                                                                            # from where the burner trip got it


def test_without_a_trip_out_the_far_pumpjack_is_not_looked_at_at_all():
    crew, w, far_jack, near_jack, burner = _patrol_crew()
    crew.base.sites.remove(burner)
    crew.foreman._sites()
    assert w.walks == [] and not any(math.dist(p, far_jack.centre) < 100 for p in w.looks)


def test_sites_that_run_without_hands_are_looked_at_rarely():
    from brain.crew import AUTOMATED_REVISIT, SEALED_REVISIT
    from brain.modules import Site
    crew, w, far_jack, near_jack, burner = _patrol_crew()
    crew.base.sites[:] = []
    now = {"t": 0}
    w.now_tick = lambda: now["t"]
    hand = crew.base.add(Site("electric_mine", {"resource": "iron-ore", "count": 2, "fuel": "hand"}, (20, 0),
                              entities=[("stone-furnace", 20, 0, "north")], burners=[(20, 0)]))
    belted = crew.base.add(Site("electric_mine", {"resource": "iron-ore", "count": 2, "fuel": "belt",
                                                  "ports": [{"item": "coal", "fuel": True, "fed": [1, 1]}]},
                                (40, 0), entities=[("stone-furnace", 40, 0, "north")], burners=[(40, 0)]))
    sealed = crew.base.add(Site("pumpjack", {"count": 1}, (60, 0), entities=[("pumpjack", 60.5, 0.5, "north")],
                                health={"working": 1}))
    crew.sealed = {sealed.id: 0.0}

    def looked():
        w.looks.clear()
        crew.foreman._sites()
        sealed.health = {"working": 1}                               # what the game reports (the stub sees none)
        return {round(x) for x, _ in w.looks}
    assert looked() == {20, 40, 60}                                  # the first round looks at everything
    now["t"] = 60 * 60
    assert looked() == {20}                                          # a minute on: only the hand-fed row
    now["t"] = AUTOMATED_REVISIT + 60
    assert looked() == {20, 40}                                      # the belted row's health, now and then
    now["t"] = SEALED_REVISIT + 60
    assert 60 in looked()                                            # the sealed pumpjack, rarer still
