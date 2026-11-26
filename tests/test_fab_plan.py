"""Self-upgrade #29: factories are planned ahead (run 20261125-214645: 35 advanced-circuit designs over 73 real
minutes, most of them for places no plastic could reach, before the split that then fitted at once)."""
from types import SimpleNamespace

from brain import modules
from brain.modules import MODULES, Site
from brain.world import BuildError
from test_upkeep_outposts import _crew


def _fac(monkeypatch):
    fac = MODULES["factory"]
    w = SimpleNamespace(blocking=False, logs=[], log=lambda m: w.logs.append(m), think=lambda msg, fn, *a: fn(*a))
    tried = []
    monkeypatch.setattr(fac, "_survey", lambda w, f: None)
    monkeypatch.setattr(fac, "_save_case", lambda *a, **k: None)
    monkeypatch.setattr(modules.Grid, "from_world", staticmethod(lambda w: SimpleNamespace(keepout=set(), ore=set())))

    def design_at(w, base, grid, item, r, focus, pairs, margin, full, supplied):
        tried.append((tuple(focus), r))
        return SimpleNamespace(ok=False, notes=[], problems=["copper-cable: no belt route from A6 to A7"])
    monkeypatch.setattr(fac, "_design_at", design_at)
    monkeypatch.setattr(fac, "probe", None)
    return fac, w, tried


def test_places_out_of_reach_of_an_input_are_never_designed(monkeypatch):
    fac, w, tried = _fac(monkeypatch)
    places = [(0, 0), (100, 0), (200, 0)]
    monkeypatch.setattr(fac, "reach", lambda f: ["plastic-bar 260 tiles off"] if f[0] < 150 else [])
    fac._ladder(w, None, "advanced-circuit", 18.0, set(), places, 10, ("advanced-circuit", 18.0), off_ore=True)
    assert {f for f, _ in tried} == {(200, 0)}
    monkeypatch.setattr(fac, "reach", lambda f: ["plastic-bar 260 tiles off"])
    tried.clear()
    why = fac._ladder(w, None, "advanced-circuit", 18.0, set(), places, 10, ("advanced-circuit", 18.0), off_ore=True)
    assert tried == [] and "beyond a link's reach of an input" in why


def test_a_quick_try_is_two_places_at_the_full_rate(monkeypatch):
    fac, w, tried = _fac(monkeypatch)
    monkeypatch.setattr(fac, "reach", None)
    monkeypatch.setattr(fac, "quick", True)
    fac._ladder(w, None, "advanced-circuit", 18.0, set(), [(0, 0), (50, 0), (99, 0)], 10,
                ("advanced-circuit", 18.0), off_ore=True)
    assert tried == [((0, 0), 18.0), ((50, 0), 18.0)]


def test_a_big_factory_is_split_after_its_quick_try_and_the_split_is_remembered(monkeypatch, tmp_path):
    crew, w, _ = _crew()
    crew.log = lambda m: None
    fac = MODULES["factory"]
    monkeypatch.setattr(fac, "case_dir", tmp_path / "local" / "run1" / "chiplets")
    monkeypatch.setattr(fac, "can_make", lambda kn: ["advanced-circuit", "copper-cable", "electronic-circuit"])
    crew._big_chain = lambda goal: len(goal.get("inputs") or []) == 0
    crew._split_candidate = lambda goal: "copper-cable"
    calls = []

    def build(goal):
        calls.append((list(goal.get("inputs") or []), fac.quick))
        if not goal.get("inputs"):
            raise BuildError("no advanced-circuit factory fits. plate-fed: no belt route")
        return "built factory"
    crew._build = build
    goal = {"do": "build", "module": "factory", "item": "advanced-circuit", "per_minute": 18}
    assert crew.build(goal) == "built factory"
    assert calls == [([], True), (["copper-cable"], False)] and fac.quick is False
    assert (tmp_path / "local" / "fab_memory.json").exists()
    # the next run plans the split from the start
    crew2, _, _ = _crew()
    crew2.log = lambda m: None
    crew2._build = build
    crew2._big_chain = lambda goal: False
    calls.clear()
    assert crew2.build({**goal, "per_minute": 15}) == "built factory"
    assert calls == [(["copper-cable"], False)]
    calls.clear()
    try:                                                    # much smaller: tried as one block again
        crew2.build({**goal, "per_minute": 5})
    except BuildError:
        pass
    assert calls[0][0] == []


def test_a_factory_that_cannot_reach_an_input_anywhere_is_not_split(monkeypatch):
    crew, w, _ = _crew()
    crew.log = lambda m: None
    fac = MODULES["factory"]
    monkeypatch.setattr(fac, "case_dir", None)
    crew._big_chain = lambda goal: True
    crew._split_candidate = lambda goal: "copper-cable"
    n = []

    def build(goal):
        n.append(1)
        raise BuildError("no advanced-circuit factory fits. plate-fed: every place is beyond a link's reach of an "
                         "input (plastic-bar 260 tiles off at (0, 0))")
    crew._build = build
    try:
        crew.build({"do": "build", "module": "factory", "item": "advanced-circuit", "per_minute": 18})
    except BuildError as exc:
        assert "beyond a link's reach" in str(exc)
    assert len(n) == 1


def test_supply_spots_count_every_input_not_only_plates():
    crew, w, _ = _crew()
    plastic = crew.base.add(Site("workshop", {"item": "plastic-bar"}, (-90, 150)))
    MOD = MODULES["factory"]
    crew.kn.unlocked = set()
    orig = MOD.supplied_for
    MOD.supplied_for = lambda kn, p: ({"plastic-bar"}, SimpleNamespace(supplied={}))
    try:
        crew._free_outputs = lambda it, consumer, need=0.0: [(-90.5, 150.5, plastic)] if it == "plastic-bar" else []
        crew.item_ports = lambda: []
        spots = crew.supply_spots({"item": "advanced-circuit", "per_minute": 18})
    finally:
        MOD.supplied_for = orig
    assert (-90.5, 150.5) in [tuple(s[:2]) for s in spots]
