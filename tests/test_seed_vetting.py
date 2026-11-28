"""Seed vetting rejects hard maps (docs/run-4f7178-review.md 5.6): run 4f7178 had one 89-tile coal patch near home,
crude 369 tiles out and too little near iron, and pick_seed's vet let it through."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "stream"))

from arena import seed_survey  # noqa: E402

NEAR = {k: {"distance": 20.0, "tiles": 300, "amount": 200000} for k in ("iron-ore", "copper-ore", "coal", "stone")}


def _survey(coal_tiles=400, coal_amount=300_000, crude=180.0, iron_tiles=3000, iron_amount=2_000_000):
    raw = {"origin": {"x": 0.5, "y": 0.5}, "ore_radius": 150, "crude_radius": 256,
           "patches": [{"name": "coal", "tiles": 30, "amount": 20000, "distance": 12.3, "x": 10.5, "y": 6.5},
                       {"name": "coal", "tiles": coal_tiles, "amount": coal_amount, "distance": 40.0, "x": 40, "y": 0},
                       {"name": "coal", "tiles": 900, "amount": 900000, "distance": 120.0, "x": 120, "y": 0},
                       {"name": "iron-ore", "tiles": iron_tiles // 2, "amount": iron_amount // 2, "distance": 25.0},
                       {"name": "iron-ore", "tiles": iron_tiles // 2, "amount": iron_amount // 2, "distance": 140.0}],
           "crude": {"wells": 0 if crude is None else 6, "amount": 0 if crude is None else 1800000}}
    if crude is not None:
        raw["crude"].update(distance=crude, x=crude, y=0)
    return seed_survey.summarize(raw)


def _vet(monkeypatch, survey, near=NEAR, water=30.0):
    import watchdog as w
    rep = {"seed": 7, "resources_within_64": near, "nearest_water": {"distance": water, "tiles": 50}}
    if survey is not None:
        rep["survey"] = survey
    monkeypatch.setattr(w, "_run", lambda cmd, timeout=20: "INFO boot\n" + json.dumps(rep) + "\n")
    return w.vet_seed(7)


def test_summarize_finds_nearest_and_biggest_near_patch_totals_and_crude():
    s = _survey()
    assert s["nearest_patch"]["coal"]["tiles"] == 30
    assert s["largest_patch_within_64"]["coal"]["tiles"] == 400        # the 900-tile patch is 120 out
    assert s["totals"]["coal"] == {"tiles": 1330, "amount": 1220000, "patches": 3}
    assert s["totals"]["iron-ore"]["amount"] == 2_000_000
    assert s["crude"]["distance"] == 180.0 and s["crude"]["wells"] == 6
    assert _survey(crude=None)["crude"]["distance"] is None


def test_lua_command_is_one_line_and_reaches_the_crude_radius():
    cmd = seed_survey.lua_command()
    assert cmd.startswith("/sc ") and "\n" not in cmd and "--" not in cmd
    assert "request_to_generate_chunks(o, 8)" in cmd and "radius = 256" in cmd and "radius = 150" in cmd


def test_a_good_map_passes(monkeypatch):
    ok, why = _vet(monkeypatch, _survey())
    assert ok, why
    assert "coal patch 400 tiles" in why and "crude 180.0" in why


@pytest.mark.parametrize("kw, reason", [
    ({"coal_tiles": 89, "coal_amount": 60000}, "coal patch near spawn too small (89 tiles)"),   # run 4f7178
    ({"coal_amount": 50_000}, "coal patch near spawn too poor"),
    ({"crude": 369.0}, "crude oil too far (369.0)"),                                         # run 4f7178
    ({"crude": None}, "no crude oil within 256 tiles"),
    ({"iron_amount": 600_000}, "too little iron within 150 tiles"),
])
def test_hard_maps_are_rejected(monkeypatch, kw, reason):
    ok, why = _vet(monkeypatch, _survey(**kw))
    assert not ok and reason in why


def test_iron_without_amounts_counts_tiles_times_typical_ore(monkeypatch):
    import watchdog as w
    s = _survey()
    s["totals"]["iron-ore"] = {"tiles": 1000, "amount": 0}
    ok, why = _vet(monkeypatch, s)
    assert not ok and f"({1000 * w.VET_ORE_PER_TILE} ore)" in why
    s["totals"]["iron-ore"] = {"tiles": 2000, "amount": 0}
    assert _vet(monkeypatch, s)[0]


def test_a_report_without_survey_is_rejected_and_old_checks_still_apply(monkeypatch):
    assert _vet(monkeypatch, None) == (False, "no map survey in the vet report (patch sizes and crude distance "
                                              "unknown)")
    near = {k: v for k, v in NEAR.items() if k != "stone"}
    assert _vet(monkeypatch, _survey(), near=near) == (False, "no stone near spawn")
    assert _vet(monkeypatch, _survey(), water=75.0) == (False, "water too far (75.0)")
