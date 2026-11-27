"""The day's AI bill across every bot process on this machine (the stream, test games, both checkouts).

Each model part has its own cost cap per session, and those caps start from zero at every resume: together they
allowed ~$41 a session, and two overnight test games on 2026-11-27 spent about $25-30. This ledger adds up what
every session spent today (local date) and puts a ceiling on the day:

    from SAVER_AT x cap: "saver" mode: the overseer looks a third as often, factory layouts by search only
    from the cap:        "off": every model part is treated as spent (each falls back to its rules, as it does at
                         its own cap) and the bot plays on; the run is never stopped for the day's cap

    d = DailySpend(cap=20.0, key="20261127-101219-148b5e@1795774351")
    mode = d.update(session_cost)      # "normal" | "saver" | "off"

The ledger file: ~/.factorioai/ai_spend.json ({date: {session key: dollars}}), or $FACTORIOAI_SPEND. Each
session writes only its own entry, so two processes never undo each other's numbers.
"""

from __future__ import annotations

import json
import os
import time
from datetime import date
from pathlib import Path

DAILY_CAP = 20.0          # dollars a day across every bot process (--daily-max-cost, $FACTORIOAI_DAILY_CAP)
SAVER_AT = 0.75           # share of the cap from which the expensive parts slow down
KEEP_DAYS = 14


def ledger_path() -> Path:
    return Path(os.environ.get("FACTORIOAI_SPEND") or (Path.home() / ".factorioai" / "ai_spend.json"))


def default_cap() -> float:
    try:
        return float(os.environ.get("FACTORIOAI_DAILY_CAP") or DAILY_CAP)
    except ValueError:
        return DAILY_CAP


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a missing or half-written ledger counts as empty
        return {}


def spent_today(path: Path | None = None, day: str | None = None) -> float:
    d = _read(path or ledger_path())
    return round(sum(float(v) for v in (d.get(day or date.today().isoformat()) or {}).values()), 4)


class DailySpend:
    def __init__(self, cap: float | None = None, key: str = "", path: Path | None = None):
        self.cap = default_cap() if cap is None else float(cap)
        self.key = key or f"pid{os.getpid()}@{int(time.time())}"
        self.path = path or ledger_path()
        self.day = date.today().isoformat()
        self.carried = 0.0          # this session's spend already booked on earlier days
        self.last = 0.0
        self.mode = "normal"

    def update(self, session_cost: float, today: str | None = None) -> str:
        """Book this session's running cost (it only grows) and return the day's mode."""
        today = today or date.today().isoformat()
        session_cost = max(float(session_cost or 0.0), self.last)
        if today != self.day:                       # past midnight: what was spent so far stays yesterday's
            self.carried, self.day = self.last, today
        self.last = session_cost
        d = _read(self.path)
        d.setdefault(today, {})[self.key] = round(session_cost - self.carried, 4)
        for old in sorted(d)[:-KEEP_DAYS]:
            d.pop(old, None)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(d, indent=1, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass                                    # a ledger that cannot be written never stops a run
        total = sum(float(v) for v in d[today].values())
        self.total = round(total, 4)
        self.mode = "off" if self.cap and total >= self.cap else \
            "saver" if self.cap and total >= SAVER_AT * self.cap else "normal"
        return self.mode
