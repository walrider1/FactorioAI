"""The monitor page's data: run folders in, one JSON view out; the brain bot's telemetry file."""

import json
import time

from arena.monitor import Runs


def _run(tmp_path, rid="20261118-120000-abcdef"):
    d = tmp_path / "local" / rid
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"task": "freeplay-green", "map": "practice-1", "task_config": {
        "id": "freeplay-green", "title": "T", "budgets": {"ticks": 216000},
        "score": {"milestones": [{"id": "a", "name": "A", "points": 1}]}}}))
    rows = [{"seq": 1, "action": {"type": "scan", "radius": 100}, "ok": True, "data": {"patches": [1]}},
            {"seq": 2, "action": {"type": "move", "x": 1.234, "y": 2}, "ok": True,
             "data": {"position": {"x": 1, "y": 2}}, "budgets": {"ticks_used": 600}, "score": 10},
            {"seq": 3, "action": {"type": "stats"}, "ok": True, "data": {"milestones": {"a": 300}}}]
    (d / "actions.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return d


def test_monitor_reads_a_run_folder(tmp_path):
    d = _run(tmp_path)
    runs = Runs(tmp_path)
    assert [r["id"] for r in runs.listing()] == [d.name]
    assert runs.find("../../secrets") is None                       # only real run ids
    v = runs.detail(d)
    assert v["calls"] == 3 and v["position"] == [1, 2] and v["scan"] == {"patches": [1]}
    assert v["actions"][0]["type"] == "stats" and v["actions"][-1]["args"] == "radius=100"
    assert v["stats"]["milestones"] == {"a": 300} and v["live"] is None


def test_telemetry_writes_live_state_and_events(tmp_path):
    from brain.telemetry import Telemetry
    t = Telemetry(tmp_path)
    t.set(current_goal={"text": "build labs"})
    t.event("plan", note="go")
    t.close(status="finished", score=5)
    live = json.loads((tmp_path / "bot_live.json").read_text())
    assert live["status"] == "finished" and live["score"] == 5 and live["events"][0]["kind"] == "plan"
    assert json.loads((tmp_path / "bot_events.jsonl").read_text().splitlines()[0])["note"] == "go"
    assert time.time() - live["updated"] < 5


def test_the_stream_overlay_gets_a_light_view_of_the_run_that_is_playing(tmp_path):
    d = _run(tmp_path)
    older = _run(tmp_path, "20261117-120000-abcdef")
    (d / "bot_live.json").write_text(json.dumps({
        "status": "playing", "updated": time.time(), "game_minute": 12.5, "activity": "walking",
        "current_goal": {"text": "build factory {'item': 'x'}", "since_minute": 10, "budget_min": 30, "who": "overseer"},
        "roadmap": {"stages": [{"name": "Power", "done": True, "status": "done"}], "power": {"capacity_kw": 900},
                    "next_step": {"do": "x"}},
        "overseer": {"notes": [{"minute": 9, "assessment": "fine", "changes": []}], "thinking": True},
        "base": [{"id": 1}, {"id": 2}], "trace": [{"big": "x"}] * 50, "supervisor": {"notes": [{"minute": 1}]}}))
    runs = Runs(tmp_path)
    assert runs.latest() == d                                       # the playing one, not the older
    v = runs.overlay(d)
    assert v["score"] == 10 and v["milestones"][0]["name"] == "A" and not v["finished"] and not v["stale"]
    live = v["live"]
    assert live["current_goal"]["budget_min"] == 30 and live["overseer"]["thinking"] and live["sites"] == 2
    assert "trace" not in live and "next_step" not in live["roadmap"]   # only what the overlay draws
    assert runs.overlay(older)["live"]["status"] is None             # a run without telemetry still answers


def test_roadmap_stages_get_the_game_minute_they_were_first_cleared():
    from bots.brain.telemetry import stamp_stages
    seen = {}
    stages = [{"name": "Power", "done": True}, {"name": "Red", "done": False}]
    stamp_stages(stages, seen, 3.0, primed=False)             # done before the first look: time unknown
    assert stages[0]["done_minute"] is None and "done_minute" not in stages[1]
    later = [{"name": "Power", "done": True}, {"name": "Red", "done": True}]
    stamp_stages(later, seen, 41.5, primed=True)
    assert later[0]["done_minute"] is None and later[1]["done_minute"] == 41.5
    again = [{"name": "Power", "done": True}, {"name": "Red", "done": True}]
    stamp_stages(again, seen, 90.0, primed=True)              # the first time sticks
    assert again[1]["done_minute"] == 41.5


def test_the_overlay_races_the_best_earlier_run():
    from arena.monitor import personal_best, stage_splits
    live = {"stage_minutes": {"Power and labs": 15.0}, "milestones": {"red-auto": 3600 * 50, "oil": 3600 * 60},
            "roadmap": {"stages": [{"name": "Green science automated", "done": True, "done_minute": 99.0}]}}
    sp = stage_splits(live)
    assert sp == {"Power and labs": 15.0, "Red science automated": 50.0, "Green science automated": 99.0}  # no oil
    pb = personal_best([("a", {"Power and labs": 12.0}),
                        ("b", {"Power and labs": 20.0, "Red science automated": 70.0}),
                        ("c", {"Power and labs": 18.0, "Red science automated": 60.0}), ("d", {})])
    assert pb["run"] == "c" and pb["runs"] == 3                     # furthest, then fastest there
    assert pb["best"] == {"Power and labs": 12.0, "Red science automated": 60.0}
    assert personal_best([("x", {})]) is None


def test_a_science_split_is_the_games_automated_milestone_never_hand_crafting():
    from arena.monitor import stage_splits
    from brain.telemetry import stamp_stages
    # run 20261124-165213: stamped red at 27.4 from statistics that count hand-crafting; the milestone came at 50.5
    old = {"stage_minutes": {"Power and labs": 15.0, "Red science automated": 27.4},
           "milestones": {"red-auto": 3600 * 50.5}}
    assert stage_splits(old)["Red science automated"] == 50.5
    early = {"stage_minutes": {"Red science automated": 34.7},           # no milestone yet, stage not done now
             "roadmap": {"stages": [{"name": "Red science automated", "done": False}]}}
    assert "Red science automated" not in stage_splits(early)
    seen = {"Red science automated": 34.7}                                # a resumed run's old stamp goes too
    stamp_stages([{"name": "Red science automated", "done": False, "milestone": "red-auto"}], seen, 40.0, True)
    assert seen == {}
