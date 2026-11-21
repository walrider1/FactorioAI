"""The scorer: independent of the team AI.

After a run ends (team called end, budgets exhausted, or a time limit hit), the
scorer takes its own handle on the game, lets the world run the task's idle tail
with no actions allowed, then reads the score metric.

- production: number of score items *produced* on the arena surface during the
  run (Factorio's production statistics), wherever they ended up.
- delivered: number of score items inside the organizer's locked delivery chest.
- automation: only the idle tail counts. It is a hands-off window: nobody can
  touch the factory, so whatever it makes, it makes on its own. See
  ``score_automation``.
"""

from __future__ import annotations

from .backends.base import Backend
from .config import Score, Task

TIER_SPAN = 100_000      # automation: score = tier x TIER_SPAN + window points (capped below the next tier)
MINUTE = 3600


SCIENCE_PACKS = ("automation-science-pack", "logistic-science-pack", "military-science-pack",
                 "chemical-science-pack", "production-science-pack", "utility-science-pack", "space-science-pack")


def score_run(backend: Backend, task: Task, fill_ticks: int = 0, start_tick: int = 0) -> dict:
    """fill_ticks: build time the team left unused, run first with no actions (automation, window_at_build_end).
    start_tick: the tick the build time started at (milestone times are measured from it)."""
    if task.score.type == "automation":
        return score_automation(backend.scorer(), task, fill_ticks)
    if task.score.type == "milestones":
        return score_milestones(backend.scorer(), task, start_tick)
    scorer = backend.scorer()
    tick_before = scorer.tick()
    scorer.run_ticks(task.budgets.idle_tail_ticks)
    tick_after = scorer.tick()
    breakdown = scorer.score_breakdown(task.score)
    return {
        "score": int(breakdown["score"]),
        "metric": f"{task.score.type}:{task.score.item}",
        "breakdown": breakdown,
        "idle_tail": {"tick_before": tick_before, "tick_after": tick_after},
    }


def automation_items(score: Score) -> list[str]:
    names = set(score.points) | {t.item for t in score.tiers} | {c for t in score.tiers for c in t.chain}
    return sorted(names)


def score_automation(scorer: Backend, task: Task, fill_ticks: int = 0) -> dict:
    """Run the hands-off window a minute at a time, then rank the factory.

    For every item, Factorio's production statistics give how many were made and how many were
    used up. Over the window:

    - A tier is reached when the factory made the tier's item at its per-minute target *and*
      kept up with its own chain: for each chain item, made >= balance x used. A factory that
      lives off a stockpile (gears crafted earlier, plates stuffed into a chest) uses more than
      it makes, so it does not reach the tier, however many packs come out.
    - Points = sum over items of (made during the window x points per item). Deeper items are
      worth more, so a factory that gets further up the chain scores more, even below tier 1.
    - Score = highest tier reached x 100,000 + points (capped at 99,999). Any team in a higher tier
      ranks above every team in a lower one; points rank teams inside a tier.
    """
    sc = task.score
    items = automation_items(sc)
    window = task.budgets.idle_tail_ticks
    fill_from = scorer.tick()
    if fill_ticks > 0:                  # the rest of the build time: the factory runs, nobody acts, nothing counts
        scorer.run_ticks(fill_ticks)
    tick_before = scorer.tick()
    snaps = [scorer.item_stats(items)]
    left = window
    while left > 0:
        step = min(MINUTE, left)
        scorer.run_ticks(step)
        left -= step
        snaps.append(scorer.item_stats(items))
    tick_after = scorer.tick()
    return automation_breakdown(sc, snaps, window) | {
        "idle_tail": {"tick_before": tick_before, "tick_after": tick_after,
                      "build_time_filled": {"from_tick": fill_from, "ticks": max(0, fill_ticks)}}}


def automation_breakdown(sc: Score, snaps: list[dict], window_ticks: int) -> dict:
    """Pure scoring from per-minute statistics snapshots: {item: [made, used]} at each minute mark."""
    first, last = snaps[0], snaps[-1]
    made = {i: int(last[i][0] - first[i][0]) for i in first}
    used = {i: int(last[i][1] - first[i][1]) for i in first}
    minutes = window_ticks / MINUTE if window_ticks else 1.0
    tiers = []
    reached = 0
    for n, tier in enumerate(sc.tiers, start=1):
        rate = made[tier.item] / minutes
        short = [c for c in tier.chain if made[c] < sc.balance * used[c]]
        ok = rate >= tier.per_minute and not short
        if ok:
            reached = n
        tiers.append({"tier": n, "name": tier.name, "item": tier.item, "target_per_minute": tier.per_minute,
                      "per_minute": round(rate, 2), "rate_ok": rate >= tier.per_minute,
                      "chain_short": short, "reached": ok})
    points = sum(made[i] * p for i, p in sc.points.items())
    score = reached * TIER_SPAN + min(points, TIER_SPAN - 1)
    per_minute = {t.item: [int(snaps[k + 1][t.item][0] - snaps[k][t.item][0]) for k in range(len(snaps) - 1)]
                  for t in sc.tiers}
    return {
        "score": score,
        "metric": "automation",
        "breakdown": {
            "tier": reached,
            "tier_name": sc.tiers[reached - 1].name if reached else "none",
            "points": points,
            "window_minutes": round(minutes, 2),
            "tiers": tiers,
            "made": {i: v for i, v in made.items() if v},
            "used": {i: v for i, v in used.items() if v},
            "per_minute": per_minute,
        },
    }


def live_points(sc: Score, stats: dict) -> int:
    """score_so_far while the team plays: points for everything made so far (the final score uses only the window)."""
    return int(sum(stats[i][0] * p for i, p in sc.points.items()))


def milestone_breakdown(sc: Score, reached: dict[str, int], start_tick: int, budget_ticks: int) -> dict:
    """Pure scoring of a milestone ladder from {milestone id: tick first reached}.

    Each reached milestone scores its points, plus up to time_bonus x points for reaching it early (full bonus
    at the start, none at the end of the build time). Higher milestones carry more points, so getting further
    always beats getting somewhere faster unless the organizer sets a large time_bonus.
    """
    rows, total, furthest = [], 0, None
    horizon = sc.bonus_horizon_ticks or budget_ticks
    for m in sc.milestones:
        t = reached.get(m.id)
        if t is None:
            rows.append({"id": m.id, "name": m.name, "points": m.points, "reached": False})
            continue
        elapsed = max(0, int(t) - start_tick)
        bonus = int(m.points * sc.time_bonus * max(0, horizon - elapsed) / horizon) if horizon else 0
        total += m.points + bonus
        furthest = m.name
        rows.append({"id": m.id, "name": m.name, "points": m.points, "time_bonus": bonus, "reached": True,
                     "tick": elapsed, "game_minute": round(elapsed / MINUTE, 2)})
    n = sum(1 for r in rows if r["reached"])
    return {"score": total, "reached": n, "of": len(sc.milestones), "furthest": furthest,
            "complete": n == len(sc.milestones), "milestones": rows}


def score_milestones(scorer: Backend, task: Task, start_tick: int) -> dict:
    """Milestone ladder: no idle tail by default (what counts is when each milestone was reached)."""
    tick_before = scorer.tick()
    if task.budgets.idle_tail_ticks:
        scorer.run_ticks(task.budgets.idle_tail_ticks)
    tick_after = scorer.tick()
    reached = scorer.milestones()
    bd = milestone_breakdown(task.score, reached, start_tick, task.budgets.ticks)
    try:                                        # science consumed by labs: how much research the factory did
        used = scorer.item_stats(list(SCIENCE_PACKS))
        bd["science_used"] = {k: v[1] for k, v in used.items() if v[1]}
        bd["science_made"] = {k: v[0] for k, v in used.items() if v[0]}
    except Exception:  # noqa: BLE001 - a missing statistic never voids a scored run
        pass
    return {"score": bd["score"], "metric": "milestones", "breakdown": bd,
            "idle_tail": {"tick_before": tick_before, "tick_after": tick_after}}
