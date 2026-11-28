"""Research follows the floorplan era (brain/era_research.py). Run 4f7178: while blue science was not automated
the labs worked on modules, battery and concrete, and the overseer had to drop a premature tech by hand."""

from types import SimpleNamespace

import pytest

from brain import zones
from brain.crew import Crew
from brain.era_research import drop_off_era, hold
from brain.knowledge import Knowledge
from brain.modules import Base, Site

RED, GREEN, BLUE = "automation-science-pack", "logistic-science-pack", "chemical-science-pack"
# tech: (prerequisites, recipes it unlocks, packs it costs)
TECHS = {"automation": ([], ["assembling-machine-1", "long-handed-inserter"], [RED]),
         "logistics": ([], ["underground-belt"], [RED]),
         "logistic-science-pack": ([], [GREEN], [RED]),
         "logistics-2": (["logistics", "logistic-science-pack"], ["fast-transport-belt"], [RED, GREEN]),
         "steel-processing": ([], ["steel-plate"], [RED]),
         "plastics": (["logistic-science-pack"], ["plastic-bar"], [RED, GREEN]),
         "sulfur-processing": (["logistic-science-pack"], ["sulfur"], [RED, GREEN]),
         "advanced-circuit": (["plastics"], ["advanced-circuit"], [RED, GREEN]),
         "engine": (["steel-processing"], ["engine-unit"], [RED, GREEN]),
         "chemical-science-pack": (["advanced-circuit", "sulfur-processing"], [BLUE], [RED, GREEN]),
         "production-science-pack": (["chemical-science-pack"], ["production-science-pack", "rail"], [RED, GREEN, BLUE]),
         "battery": ([], ["battery"], [RED, GREEN]),           # no prerequisite here: ready, and the cheapest
         "advanced-material-processing": (["steel-processing"], ["steel-furnace"], [RED])}


def _crew(made=(RED,), era_open=True):
    recipes = {r: {"products": [{"name": r, "type": "item", "amount": 1}], "ingredients": []}
               for _, unlocks, _ in TECHS.values() for r in unlocks}
    techs = {t: {"prerequisites": pre, "unlocks": unl, "unit_count": 10 if t == "battery" else 50,
                 "unit_ingredients": [{"name": p} for p in packs]} for t, (pre, unl, packs) in TECHS.items()}
    kn = Knowledge({"recipes": recipes, "technologies": techs})
    kn.researched |= {"automation", "logistics", "logistic-science-pack"}
    asked, logged = [], []
    crew = Crew.__new__(Crew)
    crew.kn, crew.base, crew.target_techs, crew.log = kn, Base(), [], logged.append
    crew.base.add(Site("labs", {"count": 8}, (0, 0)))
    crew.base.add(Site("factory", {"item": "x"}, (9, 0), chests=[(1.5, 1.5, p) for p in made]))
    crew.w = SimpleNamespace(act=lambda a: asked.append(a) or {"ok": True, "data": {"current": a["technologies"][0]}},
                             research={})
    crew.zones = SimpleNamespace(opened={1}, current=1, era_of=lambda n: n) if era_open else None
    return crew, asked, logged


@pytest.fixture(autouse=True)
def _zones_on(monkeypatch):
    monkeypatch.setattr(zones, "ENABLED", True)


def test_research_holds_to_the_eras_road_while_its_science_is_not_automated(monkeypatch):
    import brain.era_research as er
    monkeypatch.setattr(er, "furnaces_short", lambda crew: False)
    crew, asked, logged = _crew(made=(RED,))                  # era 1 live, green not automated
    why = hold(crew)
    assert why("chemical-science-pack") is None and why("plastics") is None    # the next era's gate and its prereqs
    assert why("logistics-2") is None and why("steel-processing") is None      # tier essentials
    assert why("battery") and why("production-science-pack")                   # era 4's input, era 3's pack
    assert why("advanced-material-processing")                                 # plates are not short
    monkeypatch.setattr(er, "furnaces_short", lambda crew: True)
    assert hold(crew)("advanced-material-processing") is None
    # green automated: blue is the aim, so era 3's techs open; battery (era 4) still waits
    crew, _, _ = _crew(made=(RED, GREEN))
    assert hold(crew)("production-science-pack") is None and hold(crew)("battery")
    # zones off / no era open: nothing held
    crew, _, _ = _crew(era_open=False)
    assert hold(crew) is None and drop_off_era(crew, "battery") is None


def test_an_off_era_pick_is_dropped_once_logged_and_idle_labs_get_an_era_tech(monkeypatch):
    import brain.era_research as er
    monkeypatch.setattr(er, "furnaces_short", lambda crew: False)
    crew, asked, logged = _crew(made=(RED, GREEN))
    msg = drop_off_era(crew, "battery")
    assert msg.startswith("dropped research battery") and "the labs were idle, so queued" in msg
    assert asked and asked[0]["technologies"][0] != "battery"          # the cheapest tech, battery, was skipped
    crew.w.research = {"current": "plastics"}
    assert drop_off_era(crew, "battery")
    assert len([m for m in logged if "dropped battery" in m]) == 1      # logged once
