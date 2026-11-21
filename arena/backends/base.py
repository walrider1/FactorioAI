"""Backend interface: something that can run one game for one run."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..config import Score, Task
from ..models import Outcome


class BackendError(RuntimeError):
    """The game itself failed (crash, lost connection). The run is voided, not scored."""


class Backend(ABC):
    @abstractmethod
    def setup(self, task: Task, seed: int) -> dict:
        """Create the world for this run. The world must be paused when this returns."""

    @abstractmethod
    def tick(self) -> int:
        """Current game tick."""

    @abstractmethod
    def act(self, action, max_ticks: int) -> Outcome:
        """Run one validated action. Must never run more than ``max_ticks`` game ticks.
        Actions that would need more return ``insufficient_ticks`` and change nothing."""

    @abstractmethod
    def run_ticks(self, n: int) -> None:
        """Let the world run ``n`` ticks, then pause again."""

    @abstractmethod
    def score(self, score: Score) -> int:
        """Current value of the score metric."""

    def score_breakdown(self, score: Score) -> dict:
        return {"score": self.score(score)}

    def item_stats(self, items: list[str]) -> dict[str, list[int]]:
        """Production statistics: {item: [made, used]} on the arena since the run started."""
        raise BackendError("this backend has no production statistics")

    def milestones(self) -> dict[str, int]:
        """Milestone ladder: {milestone id: tick it was first reached}."""
        raise BackendError("this backend has no milestones")

    def start_clock(self, ticks: int, run: bool = True) -> int:
        """Realtime clock: the build time starts now and lasts ``ticks``; ``run`` unpauses the world
        (replays keep it paused and run it to each logged tick). Returns the tick the clock started at."""
        raise BackendError("this backend has no realtime clock")

    def stop_clock(self) -> int:
        """Realtime clock: pause the world (for scoring). Returns the current tick."""
        raise BackendError("this backend has no realtime clock")

    def save(self, name: str, dest: "Path") -> "Path":
        """Save the running game as ``name`` and copy the save file into the folder ``dest``. Returns its path."""
        raise BackendError("this backend cannot save a game")

    def resume(self, task: Task, save_file: "Path") -> dict:
        """Instead of setup: load a game saved by ``save``. The world must be paused when this returns."""
        raise BackendError("this backend cannot load a saved game")

    def scorer(self) -> "Backend":
        """An independent handle for the scorer (a separate connection where possible)."""
        return self

    def info(self) -> dict:
        """Organizer-only details (ports, container name) for the run's meta.json."""
        return {}

    @abstractmethod
    def close(self) -> None:
        """Destroy the game. Called exactly once per run."""
