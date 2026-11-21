"""The watchdog: notices when the bot stops making progress and climbs a recovery ladder on its own.

Progress is anything that moves the game forward: a milestone, a technology researched, a site built, replaced,
removed or wired to power. When none of that happens for a while, whoever is planning is stuck: a goal that
cannot work here, a world the bot misread, a planner going in circles. The ladder, one rung per stall:

    6 game min   refresh    look again: rescan patches, re-survey the base, refuel, reconnect power, make room
                            in the inventory; then ask the strategist with the stall spelled out in its report
    10 game min  switch     let the other planner have a go (the opening book for an LLM, and the other way)
    15 game min  expand     more supply: burner mines on the plate that is scarcest (exploring if needed)
    22 game min  restart    note the stall for the monitor and start the ladder again

Progress at any point resets the ladder. Nothing here waits for a person.
"""

from __future__ import annotations

LADDER = [(6.0, "refresh"), (10.0, "switch"), (15.0, "expand"), (22.0, "restart")]
PROGRESS_GOALS = {"build", "replace", "remove", "connect"}


class Watchdog:
    def __init__(self):
        self.last_progress_tick = 0
        self.last_progress = "start of the run"
        self.level = 0
        self.stalls = 0
        self.researched = 0

    def seen(self, tick: int, what: str) -> None:
        self.last_progress_tick, self.last_progress, self.level = tick, what, 0

    def observe(self, tick: int, result: dict | None, new_milestones, researched_count: int) -> None:
        """Feed it after every goal."""
        if new_milestones:
            self.seen(tick, f"milestone {', '.join(sorted(new_milestones))}")
        if researched_count > self.researched:
            if self.researched:
                self.seen(tick, "a technology was researched")
            self.researched = researched_count
        if result and result["ok"] and (result["goal"] or {}).get("do") in PROGRESS_GOALS:
            self.seen(tick, f"{result['goal'].get('do')} succeeded")

    def stuck_minutes(self, tick: int) -> float:
        return max(0.0, (tick - self.last_progress_tick) / 3600)

    def check(self, tick: int) -> str | None:
        """The next rung to climb, if the stall has lasted long enough for it."""
        if self.level >= len(LADDER):
            return None
        at, action = LADDER[self.level]
        if self.stuck_minutes(tick) >= at:
            self.level += 1
            if action == "restart":
                self.stalls += 1
                self.seen(tick, f"ladder restarted after stall {self.stalls}")
            return action
        return None

    def status(self, tick: int) -> dict:
        return {"minutes_without_progress": round(self.stuck_minutes(tick), 1), "ladder_step": self.level,
                "last_progress": self.last_progress, "stalls_so_far": self.stalls}
