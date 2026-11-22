"""The scheduled review measures what a person checks when asked to go through the run (run 20261121-140755: it said
ok five times while the bot looped, starved for plates, left the labs idle and 22 of 62 sites stood still)."""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "stream"))


def _run(tmp_path, events, live):
    d = tmp_path / "20261121-140755-c977a4"
    d.mkdir()
    (d / "bot_events.jsonl").write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    (d / "bot_live.json").write_text(json.dumps(live), encoding="utf-8")
    (d / "meta.json").write_text("{}", encoding="utf-8")
    (d / "actions.jsonl").write_text("", encoding="utf-8")
    return d


def test_the_review_packet_lists_every_problem_a_person_would_find(tmp_path):
    import watchdog as w
    now = time.time()
    ev = [{"t": now - 600, "kind": "milestone", "minute": 100.0, "ids": ["oil"]},
          {"t": now - 500, "kind": "goal_end", "goal": "build workshop {'item': 'plastic-bar'}", "ok": False,
           "result": "could not get 801 iron-plate in time", "game_minutes": 18.8},
          {"t": now - 400, "kind": "goal_end", "goal": "relink {'site': 56, 'item': 'iron-plate'}", "ok": True,
           "result": "1/1 fed", "game_minutes": 2},
          {"t": now - 300, "kind": "goal_end", "goal": "relink {'site': 56, 'item': 'iron-plate'}", "ok": True,
           "result": "1/1 fed", "game_minutes": 2},
          {"t": now - 200, "kind": "aged", "goal": "build pumpjack", "waited_min": 25.5},
          {"t": now - 100, "kind": "pumps", "note": "site 81: its 441-tile pipe line overextends"}]
    sites = [{"id": i, "kind": "electric_mine", "resource": "iron-ore",
              "health": {"waiting_for_space_in_destination": 2}} for i in range(5)]
    live = {"updated": now, "game_minute": 220.0, "research": {"progress": 0, "queue": {}},
            "foreman": {"problems": ["labs were idle: queued plastics"]}, "base": sites}
    d = _run(tmp_path, ev, live)
    keys = {f["key"] for f in w.packet(d, 3600, None)["findings"]}
    assert {"gathering", "slow-goals", "repeated-repairs", "plan-starved", "research-idle", "idle-sites",
            "fluid", "no-milestone"} <= keys


def test_the_packet_says_where_game_time_went(tmp_path):
    """Real goal_end events carry no duration: it comes from the goal's start and end minutes, and a long goal is a
    finding even then (the blue-science build that spent 15 minutes on power before failing on its water)."""
    import watchdog as w
    now = time.time()
    blue = "build workshop {'item': 'chemical-science-pack', 'per_minute': 12}"
    ev = [{"t": now - 900, "kind": "milestone", "minute": 630.0, "ids": ["x"]},
          {"t": now - 800, "kind": "goal_start", "goal": blue, "minute": 635.0},
          {"t": now - 700, "kind": "goal_end", "goal": blue, "ok": False, "minute": 650.3,
           "result": "chemical-science-pack needs water and the nearest shore is 407 tiles"},
          {"t": now - 600, "kind": "goal_start", "goal": "relink {'site': 22}", "minute": 650.3},
          {"t": now - 500, "kind": "goal_end", "goal": "relink {'site': 22}", "ok": True, "minute": 653.3}]
    live = {"updated": now, "game_minute": 660.0, "research": {"current": "x", "queue": ["y"]},
            "foreman": {"problems": []}, "base": []}
    d = _run(tmp_path, ev, live)
    (d / "resume-1.log").write_text("[crew] power first: 4490 kW more would leave -1535 kW spare\n", encoding="utf-8")
    p = w.packet(d, 3600, None)
    assert p["window"]["time_by_kind"] == {"build": 15.3, "relink": 3.0}
    assert p["window"]["longest_goals"][0]["goal"].startswith("build workshop")
    assert any("power first" in x for x in p["process_log_tail"])
    assert "slow-goals" in {f["key"] for f in p["findings"]}


def test_a_clean_hour_has_no_findings(tmp_path):
    import watchdog as w
    now = time.time()
    ev = [{"t": now - 600, "kind": "milestone", "minute": 200.0, "ids": ["oil"]},
          {"t": now - 500, "kind": "goal_end", "goal": "build factory {'item': 'x'}", "ok": True,
           "result": "built", "game_minutes": 4}]
    live = {"updated": now, "game_minute": 220.0, "research": {"current": "plastics", "queue": ["engine"]},
            "foreman": {"problems": []}, "base": [{"id": 1, "kind": "factory", "health": {"working": 4}}]}
    d = _run(tmp_path, ev, live)
    assert w.packet(d, 3600, None)["findings"] == []
