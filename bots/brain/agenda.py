"""One agenda every decider reads and writes (docs/hybrid-plan.md, section 3).

The deciders (strategist, overseer, rule floor, assessor, triage, fixer, foreman, close-out) used to push goals
into the crew's queues on their own, and they fought: run 20261125-214645 dropped 74 queued goals, triage
queued an iron outpost four times that the overseer had refused, and a paused fix came back as a protected
plan goal. The queues stay (the plan, the urgent fixes, the close-out), but every write goes through here:

- a goal for a target that is already running or queued is merged (its asker is noted), not queued twice;
- an overseer veto (a forbid) holds for everyone but the rule floor's fuel and power fixes;
- a goal the overseer dropped or paused is not queued again by another decider for HOLD_DROPPED_MIN;
- a job that just finished is not queued again for DONE_HOLD_MIN;
- a goal that failed FAIL_HOLD_N times with the same reason is held, whoever asks, until a site is built or
  removed within FAIL_NEAR tiles of it, or FAIL_HOLD_MIN game minutes pass (note_failed, stuck_hold);
- a relink that found no measurable cause UNKNOWN_RELINKS times for one port is not done again: that port gets
  a second trunk instead (unknown_relink);
- every decision is recorded with who made it, and every decider sees the same summary (who asked for what,
  what was vetoed, what was dropped and why).
"""
from __future__ import annotations

import math
import re
from collections import deque

HOLD_DROPPED_MIN = 20.0        # game minutes a goal the overseer dropped stays out (unless the overseer orders it)
DONE_HOLD_MIN = 3.0            # a job just done is not queued again this soon (its copies are dropped as done)
SAFETY = ("rules",)            # the rule floor's fuel and power fixes: never held back by a decider
CHIEF = ("overseer",)          # the one decider whose word holds over the others

FAIL_HOLD_N = 3                # the same goal failing with the same reason this often is held...
FAIL_NEAR = 40.0               # ...until a site is built or removed within these tiles of its site or spot...
FAIL_HOLD_MIN = 60.0           # ...or these game minutes pass; then it gets one more try
NEVER_HELD = ("wait", "feed", "act", None)   # fuel by hand changes minute to minute; "act" goals all share one target
REASON_CHARS = 120             # a reason is compared on its first characters (the rest lists alternatives tried)
UNKNOWN_RELINKS = 2            # relinks of one port that found "no measurable cause" before it gets a second trunk


def reason_key(msg) -> str:
    """A failure reason with its numbers taken out (coordinates, site ids, counts, minutes, belt lengths), so the
    same fault at the same place reads the same: "no route from (12, -40)" and "no route from (13, -41)" match."""
    s = re.sub(r"-?\d+(?:\.\d+)?", "#", str(msg or "").lower())
    s = re.sub(r"#(?:[\s,;:/.()\[\]-]*#)+", "#", s)       # a coordinate pair or a list of ids is one number
    return re.sub(r"\s+", " ", s).strip()[:REASON_CHARS]


def now_of(crew) -> float:
    """Game minutes now (0 for a stub crew with no clock)."""
    try:
        return float(crew._minute())
    except Exception:  # noqa: BLE001
        try:
            return float(crew.w.now_tick()) / 3600
        except Exception:  # noqa: BLE001
            return 0.0


def who_of(goal: dict, default: str = "plan") -> str:
    """The decider behind a goal: the tag before the colon of its reflex ("triage: ..." -> triage)."""
    r = str((goal or {}).get("reflex") or "")
    return (r.split(":", 1)[0].strip() or default) if ":" in r else default


def _target(goal: dict):
    from .overseer import same_target
    try:
        return repr(same_target({k: v for k, v in (goal or {}).items() if k != "reflex"}))
    except Exception:  # noqa: BLE001 - an odd goal still gets a key
        return repr(sorted((k, str(v)) for k, v in (goal or {}).items() if k != "reflex"))


def _short(goal: dict) -> str:
    rest = {k: v for k, v in (goal or {}).items() if k not in ("do", "module", "reflex")}
    return f"{goal.get('do')} {goal.get('module', '')} {rest}".replace("  ", " ")[:120]


def _say(crew, text: str) -> None:
    fn = getattr(crew, "log", None)
    if callable(fn):
        try:
            fn(text)
        except Exception:  # noqa: BLE001 - a log line never stops the run
            pass


def _anchor(crew, goal: dict) -> tuple[float, float] | None:
    """Where a goal acts: its site's centre, else its "at" spot (None: nowhere in particular)."""
    if goal.get("site") is not None:
        try:
            s = crew.base.get(goal["site"])
        except Exception:  # noqa: BLE001 - stub crews
            s = None
        if s is not None:
            return float(s.centre[0]), float(s.centre[1])
    at = goal.get("at")
    if isinstance(at, (list, tuple)) and len(at) >= 2:
        try:
            return float(at[0]), float(at[1])
        except (TypeError, ValueError):
            return None
    return None


def _near(crew, anchor) -> frozenset:
    """The sites standing within FAIL_NEAR tiles of a spot (every site when there is no spot): one built or
    removed there changes this set."""
    try:
        sites = list(crew.base.sites)
    except Exception:  # noqa: BLE001 - stub crews
        return frozenset()
    return frozenset(s.id for s in sites if anchor is None or math.dist(tuple(s.centre[:2]), anchor) <= FAIL_NEAR)


class Agenda:
    def __init__(self):
        self.history: deque = deque(maxlen=80)    # decisions: minute, who, action, goal, why
        self.dropped: dict[str, tuple] = {}       # target -> (minute, who, why)
        self.done: dict[str, float] = {}          # target -> minute
        self.asked: dict[str, list[str]] = {}     # target -> deciders that asked for it
        self.fails: dict[str, dict] = {}          # target -> {reason key: failures since it last worked}
        self.stuck: dict[str, dict] = {}          # target -> its failure hold (minute, why, anchor, near sites)
        self.unknown: dict[tuple, int] = {}       # (site, item) -> relinks that found no measurable cause

    def _book(self, name: str) -> dict:
        """One of the books above (an agenda saved before it existed gets it empty)."""
        return self.__dict__.setdefault(name, {})

    # ------------------------------------------------------------------ records
    def record(self, minute: float, who: str, action: str, goal: dict | None, why: str = "") -> None:
        self.history.append({"minute": round(float(minute), 1), "who": who, "action": action,
                             "goal": _short(goal) if goal else "", "why": str(why)[:160]})

    def note_dropped(self, minute: float, goal: dict, who: str, why: str = "") -> None:
        self.dropped[_target(goal)] = (float(minute), who, str(why)[:160])
        self.record(minute, who, "drop", goal, why)

    def note_done(self, minute: float, goal: dict) -> None:
        t = _target(goal)
        self.done[t] = float(minute)
        self.asked.pop(t, None)
        self._book("fails").pop(t, None)          # it works now: its old failures say nothing any more
        self._book("stuck").pop(t, None)
        self.record(minute, who_of(goal), "done", goal)

    # ------------------------------------------------------------------ failures that repeat
    def note_failed(self, crew, goal: dict, msg) -> str | None:
        """Count a failed goal under its reason (numbers taken out, reason_key). The FAIL_HOLD_N-th failure with one
        reason holds the goal (stuck_hold) whoever asks, and says why once. Returns that reason when this failure
        started a hold, else None. Run 4f7178: the coal trunk into steam site 21 failed 10-12 times with one reason
        over 100 game minutes; nodes.futile_block let it try again after every site built anywhere in the base and
        after each 30-minute window, so it never stopped."""
        if not isinstance(goal, dict) or goal.get("do") in NEVER_HELD:
            return None
        t = _target(goal)
        key = reason_key(msg)
        by = self._book("fails").setdefault(t, {})
        by[key] = by.get(key, 0) + 1
        if by[key] < FAIL_HOLD_N or t in self._book("stuck"):
            return None
        now = now_of(crew)
        anchor = _anchor(crew, goal)
        if anchor is None:
            until = "a site is built or removed anywhere"
        else:
            where = f"site {goal['site']}" if goal.get("site") is not None else f"({anchor[0]:.0f}, {anchor[1]:.0f})"
            until = f"a site is built or removed within {FAIL_NEAR:.0f} tiles of {where}"
        why = (f"failed {by[key]}x with the same reason ({str(msg)[:100]}); held until {until}, or for "
               f"{FAIL_HOLD_MIN:.0f} game min")
        self._book("stuck")[t] = {"minute": now, "why": why, "reason": key, "anchor": anchor,
                                  "near": _near(crew, anchor), "goal": _short(goal)}
        self.record(now, "agenda", "held", goal, why)
        _say(crew, f"[agenda] {_short(goal)}: {why}")
        return why

    def stuck_hold(self, crew, goal: dict) -> str | None:
        """Why a goal that keeps failing the same way may not run now (None: it may). The hold ends when a site is
        built or removed near it (what it depends on changed) or FAIL_HOLD_MIN game minutes pass; then it gets one
        more try, and one more failure with that reason holds it again."""
        if not isinstance(goal, dict):
            return None
        t = _target(goal)
        h = self._book("stuck").get(t)
        if h is None:
            return None
        now = now_of(crew)
        changed = _near(crew, h["anchor"]) != h["near"]
        if changed or now - h["minute"] >= FAIL_HOLD_MIN:
            self._book("stuck").pop(t, None)
            by = self._book("fails").get(t) or {}
            if h["reason"] in by:
                by[h["reason"]] = FAIL_HOLD_N - 1
            why = "a site was built or removed near it" if changed else f"held {FAIL_HOLD_MIN:.0f} game min"
            self.record(now, "agenda", "released", goal, why)
            _say(crew, f"[agenda] {h['goal']} may run again: {why}")
            return None
        return f"{h['why']} ({max(1.0, FAIL_HOLD_MIN - (now - h['minute'])):.0f} game min left)"

    # ------------------------------------------------------------------ relinks that find nothing wrong
    @staticmethod
    def _port(goal: dict) -> tuple:
        return str(goal.get("site")), str(goal.get("item") or "")

    def unknown_relink(self, crew, goal: dict, note: str) -> str | None:
        """A relink whose diagnosis found no measurable cause (triage: belt there, source running, not overdrawn):
        the first UNKNOWN_RELINKS of them on one port run; after that the port gets a second trunk from another
        source (Crew.trunk -> _second_trunk) instead, and the relink is not done. Returns what was done instead,
        or None when the relink should run. Run 4f7178: 43 relinks, many "diagnosed: unknown cause, relinked
        once", the same ports again and again, and each relink took up a belt that was there and laid it again."""
        if not str(note or "").startswith("diagnosed (unknown)"):
            return None
        port = self._port(goal)
        book = self._book("unknown")
        n = book.get(port, 0)
        book[port] = n + 1
        if n < UNKNOWN_RELINKS:
            return None
        trunk = {"do": "trunk", "site": goal.get("site"),
                 **({"item": goal["item"]} if goal.get("item") else {}),
                 "reflex": f"triage: relinked {UNKNOWN_RELINKS}x with no measurable cause: a second trunk instead"}
        queued, why = self.propose(crew, trunk, "triage", front=True)
        if queued or why.startswith("already"):
            then = "a second trunk into it goes first instead"
        else:
            then = f"its second trunk is {why[len('not queued: '):] if why.startswith('not queued: ') else why}"
        out = (f"{note}; relinked {UNKNOWN_RELINKS}x before with no measurable cause, so no more relinks of it: "
               f"{then}")
        if n == UNKNOWN_RELINKS:                  # said once; later relinks of it are held by the agenda
            _say(crew, f"[agenda] relink site {port[0]} {port[1]}: no more relinks, {then}")
            self.record(now_of(crew), "agenda", "held", goal, f"no more relinks: {then}")
        return out

    def relink_capped(self, goal: dict) -> str | None:
        """Why a relink of this port is not queued: it already went to the second-trunk path (unknown_relink)."""
        if not isinstance(goal, dict) or goal.get("do") != "relink":
            return None
        port = self._port(goal)
        if self._book("unknown").get(port, 0) <= UNKNOWN_RELINKS:
            return None
        trunk = {"do": "trunk", "site": goal.get("site"), **({"item": goal["item"]} if goal.get("item") else {})}
        return (f"relinked {UNKNOWN_RELINKS}x with no measurable cause: no more relinks of site {port[0]} "
                f"{port[1]}; more supply comes by a second trunk instead ({trunk})")

    # ------------------------------------------------------------------ checks
    def holds(self, crew, goal: dict, who: str) -> str | None:
        """Why this decider may not queue this goal now (None: it may)."""
        why = self.stuck_hold(crew, goal) or self.relink_capped(goal)   # facts about the goal: they hold for all
        if why is not None:
            return why
        if who in SAFETY:
            return None
        now = now_of(crew)
        t = _target(goal)
        if who not in CHIEF:
            try:
                from .overseer import forbidden
                f = forbidden(crew, {k: v for k, v in goal.items() if k != "reflex"})
            except Exception:  # noqa: BLE001 - stub crews in tests
                f = None
            if f is not None:
                return f"vetoed by the overseer: {str(f.get('why') or '')[:100]}"
            d = self.dropped.get(t)
            if d is not None and now - d[0] < HOLD_DROPPED_MIN:
                return f"the {d[1]} dropped it {now - d[0]:.0f} min ago ({d[2][:80]})"
        m = self.done.get(t)
        if m is not None and now - m < DONE_HOLD_MIN and goal.get("do") not in ("feed", "wait"):
            return f"just done ({now - m:.0f} min ago)"
        return None

    def where_is(self, crew, goal: dict) -> str | None:
        """Where a goal for this target already is: "current", "urgent", "plan" or "close-out" (None: nowhere)."""
        t = _target(goal)
        cur = (getattr(crew, "current", None) or {}).get("goal")
        if cur and _target(cur) == t:
            return "current"
        for name, q in (("urgent", getattr(crew, "urgent", [])), ("plan", getattr(crew, "plan_queue", [])),
                        ("close-out", getattr(crew, "closeout_queue", []))):
            if any(_target(g) == t for g in q):
                return name
        return None

    # ------------------------------------------------------------------ writes
    def propose(self, crew, goal: dict, who: str | None = None, front: bool = False, why: str = "") -> tuple[bool, str]:
        """Queue a fix (crew.urgent) for a decider, through the checks. Returns (queued, what happened)."""
        who = who or who_of(goal, "plan")
        now = now_of(crew)
        t = _target(goal)
        hold = self.holds(crew, goal, who)
        if hold is not None:
            self.record(now, who, "refused", goal, hold)
            return False, f"not queued: {hold}"
        at = self.where_is(crew, goal)
        if at is not None:
            asked = self.asked.setdefault(t, [])
            if who not in asked:
                asked.append(who)
            self.record(now, who, "merged", goal, f"already {at}")
            return False, f"already {'running' if at == 'current' else 'queued (' + at + ')'}"
        if front:
            from .overseer import rules_end
            at_i = 0 if who in SAFETY else rules_end(crew.urgent)
            crew.urgent.insert(at_i, goal)
        else:
            crew.urgent.append(goal)
        self.asked.setdefault(t, [])
        if who not in self.asked[t]:
            self.asked[t].append(who)
        self.record(now, who, "propose", goal, why or str(goal.get("reflex") or "")[len(who) + 1:])
        return True, "queued"

    # ------------------------------------------------------------------ the shared picture
    def summary(self, crew, n: int = 14) -> dict:
        """What every decider sees: who asked for what is queued, what the overseer vetoed or dropped, and the
        latest decisions with their authors."""
        now = now_of(crew)

        def tag(g):
            t = _target(g)
            by = self.asked.get(t) or [who_of(g)]
            return f"{_short(g)} [asked by {', '.join(by)}]"
        out = {
            "urgent": [tag(g) for g in getattr(crew, "urgent", [])[:8]],
            "plan": [tag(g) for g in getattr(crew, "plan_queue", [])[:8]],
            "dropped_recently": [f"{t[:90]} by {d[1]} {now - d[0]:.0f} min ago: {d[2][:80]}"
                                 for t, d in self.dropped.items() if now - d[0] < HOLD_DROPPED_MIN][:6],
            "held_after_failing": [f"{h['goal'][:80]}: {h['why'][:120]}" for h in self._book("stuck").values()][:6],
            "decisions": [f"{h['minute']} {h['who']} {h['action']} {h['goal'][:80]}"
                          + (f" ({h['why'][:60]})" if h["why"] else "") for h in list(self.history)[-n:]],
        }
        try:
            from .overseer import active_forbids
            out["vetoes"] = [f"{f['match']}: {str(f.get('why') or '')[:80]}" for f in active_forbids(crew)][:6]
        except Exception:  # noqa: BLE001
            pass
        return out


def agenda_of(crew) -> Agenda:
    """The crew's agenda (made on first use, so saves from before it existed load)."""
    a = crew.__dict__.get("agenda")
    if not isinstance(a, Agenda):
        a = Agenda()
        crew.__dict__["agenda"] = a
    return a
