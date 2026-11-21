"""End to end on the simulator: SDK -> gateway -> controller -> scorer -> replay -> report."""

import json

import scripted_bot
from arena_sdk import ArenaClient

from arena.replay import replay_run
from arena.report import build_report


def test_scripted_bot_scores_and_replays(http, settings, token):
    client = ArenaClient(token=token, http=http)
    run = client.start_run("level1-iron")
    result = scripted_bot.play(run, log=lambda *a: None)
    assert result["status"] == "finished"
    assert result["score"] > 900, result
    assert result["budgets_used"]["actions"] <= 100
    assert result["budgets_used"]["ticks"] == 36_000
    run_dir = settings.runs_dir / run.id
    lines = (run_dir / "actions.jsonl").read_text().splitlines()
    assert len(lines) == result["budgets_used"]["actions"] + result["budgets_used"]["observes"] + 2  # + 2 advances
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["seed"] == 1000 and meta["map"] == "practice-1000"

    rep = replay_run(run_dir, settings)
    assert rep["match"], rep
    assert rep["replayed_score"] == result["score"]


def test_report_ranks_eval_runs(http, settings, token):
    beta = settings.add_team("beta")
    settings.competition.eval.open = True
    settings.generate_eval_seeds("level1-iron", 1)
    for tok, play in ((token, True), (beta, False)):
        run = ArenaClient(token=tok, http=http).start_run("level1-iron", mode="eval")
        if play:
            scripted_bot.play(run, log=lambda *a: None)
        else:
            run.end_and_wait()
    html_path, md_path = build_report(settings)
    md = md_path.read_text()
    assert md.index("| 1 | alpha") < md.index("| 2 | beta")
    assert "<svg" in html_path.read_text()


def test_level2_bootstrap_is_playable(http, token):
    """Hand-mine stone, craft a furnace, smelt by hand-fed ore: the level 2 loop works."""
    run = ArenaClient(token=token, http=http).start_run("level2-bootstrap")
    obs = run.observe(radius=20)["data"]
    near = lambda name: obs["resource_summary"][name]["nearest"]  # noqa: E731
    s, c, i = near("stone"), near("coal"), near("iron-ore")
    assert run.move(s["x"], s["y"])["ok"]
    assert run.mine(s["x"], s["y"], 5)["ok"]
    assert run.craft("stone-furnace")["ok"]
    assert run.move(c["x"], c["y"])["ok"]
    assert run.mine(c["x"], c["y"], 3)["ok"]
    assert run.move(i["x"], i["y"])["ok"]
    assert run.mine(i["x"], i["y"], 5)["ok"]
    fx, fy = round(i["x"]) + 3, round(i["y"])
    assert run.place("stone-furnace", fx, fy)["ok"]
    assert run.insert(fx, fy, "coal", 3)["ok"]
    assert run.insert(fx, fy, "iron-ore", 5)["ok"]
    run.advance(5 * 192 + 10)
    result = run.end_and_wait()
    assert result["score"] == 5
