"""Progress first, upkeep only where it is needed (run 20261121-140755: from minute 600 the rule floor's mine
rebuilds took 130 game minutes and the plan 60, while 81% of the drills stood idle)."""

from types import SimpleNamespace

from brain import upkeep
from brain.modules import Site


def _mine(i, resource="coal", drills=2, into="chest"):
    s = Site("electric_mine", {"resource": resource, "count": drills, "into": into}, (i * 10, 0))
    s.id = i
    s.entities = [("electric-mining-drill", i * 10 + k * 3.5, 0.5, "north") for k in range(drills)]
    return s


def _crew(sites, used):
    base = SimpleNamespace(sites=sites, powered_poles=lambda: [(0, 0)])
    w = SimpleNamespace(stats={"items": {k: {"used_per_min": v} for k, v in used.items()}}, ore_amount={},
                        resources={})
    return SimpleNamespace(base=base, w=w, kn=SimpleNamespace(researched={"electric-mining-drill"}), _minute=lambda: 0)


def test_a_mine_running_out_is_not_rebuilt_while_the_other_mines_give_plenty():
    old, *rest = [_mine(i) for i in range(1, 5)]                     # 3 other coal mines: 180 coal a minute
    crew = _crew([old, *rest], {"coal": 60})
    assert not upkeep.still_needed(crew, old)
    assert upkeep.depleted_fix(crew, old) == {"do": "remove", "site": old.id, "force": True}


def test_a_mine_the_base_would_run_short_without_is_rebuilt():
    old, other = _mine(1), _mine(2)                                  # the other gives 60, the base burns 50
    crew = _crew([old, other], {"coal": 50})
    assert upkeep.still_needed(crew, old)                            # coal keeps 1.5x headroom: 75 > 60
    assert upkeep.depleted_fix(crew, old)["do"] == "replace"


def test_plates_count_against_plates_and_no_statistics_means_needed():
    old = _mine(1, "iron-ore", into="furnace")
    rows = [_mine(i, "iron-ore", drills=4, into="line") for i in (2, 3)]
    assert not upkeep.still_needed(_crew([old, *rows], {"iron-plate": 100}), old)   # 150 plates a minute stand
    assert upkeep.still_needed(_crew([old, *rows], {"iron-plate": 140}), old)
    assert upkeep.still_needed(_crew([old, *rows], {}), old)


def test_the_plan_waits_minutes_not_tens_of_minutes_and_the_closeout_skips_optional_upgrades():
    assert upkeep.PLAN_STARVE_MIN <= 8 and upkeep.CLOSEOUT_MAX_GOALS <= 4
    import inspect
    src = inspect.getsource(upkeep.closeout)
    assert "upgrade_goals(crew)" not in src and "growth_goals(crew)" not in src
