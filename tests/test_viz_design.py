"""The designer feed for the stream overlay (viz.design_feed): a refusal fades once the bot moves on, and a kept
design counts only its own buildings as placed (not a feeder built earlier in the same goal)."""

import time
from types import SimpleNamespace

from brain import viz


def _crew(rec):
    return SimpleNamespace(current={"goal": {"do": "build", "module": "factory", "item": "logistic-science-pack"}},
                           w=SimpleNamespace(recorder=rec))


def test_a_refusal_fades_and_a_kept_design_counts_its_own_buildings():
    with viz.LOCK:
        viz.DESIGN.clear()
        viz.DESIGN.update({"item": "logistic-science-pack", "status": "refused", "t": time.time() - 600,
                           "ents": [[0, 0, 3, 3, "m"]] * 4})
    assert viz.design_feed(_crew([])) is None              # ten minutes old, the goal still on: gone anyway
    rec = [("electric-mining-drill", 0, 0, "north")] * 5       # a feeder's mine, built first in the same goal
    with viz.LOCK:
        viz.DESIGN.clear()
        viz.DESIGN.update({"item": "iron-gear-wheel", "status": "kept", "t": time.time(), "placer": "saved",
                           "ents": [[0, 0, 3, 3, "m"]] * 4})
    d = viz.design_feed(_crew(rec))
    assert d["status"] == "kept" and d["placed"] == 0 and "rec0" not in d
    rec += [("assembling-machine-1", 0, 0, "north")] * 3
    d = viz.design_feed(_crew(rec))
    assert d["status"] == "building" and d["placed"] == 3
