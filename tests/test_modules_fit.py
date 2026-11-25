"""Productivity modules (modfit.py): the silo first, then science and rocket intermediates, once modules can be
had; machines whose recipe takes none are passed over, and a site half-filled for want of modules is finished
on a later goal."""

import contextlib
from types import SimpleNamespace

import pytest

from brain import assess, modfit
from brain.modules import Site
from brain.world import BuildError
from test_upkeep_outposts import _crew


def _setup(unlocked=("productivity-module",), inventory=8, refuse=()):
    crew, w, minute = _crew()
    crew.kn.unlocked = set(crew.kn.unlocked) | set(unlocked)
    w.inventory = {"productivity-module": inventory}
    w.stats = {"items": {}}
    w.step = lambda name: contextlib.nullcontext()
    w.doing = lambda text: None
    inserted = []

    def insert(x, y, item, n):
        if (x, y) in refuse:
            return 0
        got = min(n, w.inventory.get(item, 0))
        w.inventory[item] -= got
        inserted.append((x, y, got))
        return got
    w.insert = insert
    crew.make_for = lambda kit: None
    silo = crew.base.add(Site("silo", {}, (0, 0), entities=[("rocket-silo", 4.5, 4.5, "north")]))
    blue = crew.base.add(Site("factory", {"item": "chemical-science-pack"}, (30, 0), entities=[
        ("assembling-machine-2", 30.5, 0.5, "north"), ("assembling-machine-2", 34.5, 0.5, "north"),
        ("inserter", 32.5, 0.5, "north")]))
    gears = crew.base.add(Site("factory", {"item": "iron-gear-wheel"}, (60, 0), entities=[
        ("assembling-machine-1", 60.5, 0.5, "north")]))                   # no slots
    return crew, w, silo, blue, gears, inserted


def test_the_silo_is_filled_first_then_science_and_machines_that_refuse_are_passed_over():
    crew, w, silo, blue, gears, inserted = _setup(refuse=((34.5, 0.5),))
    assert [t[0].id for t in modfit.targets(crew)] == [silo.id, blue.id]      # gears: assembler 1 has no slots
    [a] = modfit.alerts(crew)
    assert a["key"] == f"modules-{silo.id}" and a["fix"] == {"do": "modules", "site": silo.id}
    assert a["key"].startswith(assess.SOON)                                    # the rule floor queues it itself
    msg = crew.fit_modules(silo.id)
    assert inserted == [(4.5, 4.5, 4)] and silo.params["modules_done"] and "fitted 4" in msg
    msg = crew.fit_modules(None)                                               # next: the blue science factory
    assert inserted[1:] == [(30.5, 0.5, 2)] and blue.params["modules_done"] and "1 machine(s) take none" in msg
    assert modfit.targets(crew) == [] and modfit.alerts(crew) == []


def test_no_alert_before_modules_can_be_had_and_a_half_filled_site_is_finished_later():
    crew, w, silo, blue, gears, inserted = _setup(inventory=0)
    assert modfit.alerts(crew) == []                                           # none on hand, no advanced circuits
    w.stats = {"items": {"advanced-circuit": {"made_per_min": 3.0}}}
    assert modfit.alerts(crew)[0]["key"] == f"modules-{silo.id}"
    with pytest.raises(BuildError, match="no productivity-module to fit"):
        crew.make_for = lambda kit: (_ for _ in ()).throw(BuildError("no advanced circuits on hand"))
        crew.fit_modules(silo.id)
    crew.make_for = lambda kit: None
    w.inventory["productivity-module"] = 2                                     # two made so far
    crew.fit_modules(silo.id)
    assert inserted == [(4.5, 4.5, 2)] and not silo.params.get("modules_done")
    w.inventory["productivity-module"] = 4
    crew.fit_modules(silo.id)
    assert silo.params["modules_done"] and silo.params["modules"] == 4


def test_nothing_without_the_research():
    crew, w, silo, blue, gears, inserted = _setup(unlocked=())
    assert modfit.module_item(SimpleNamespace(unlocked=set())) is None and modfit.alerts(crew) == []
    with pytest.raises(BuildError, match="no productivity module is researched"):
        crew.fit_modules(silo.id)
