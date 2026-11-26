"""One agenda every decider reads and writes (docs/hybrid-plan.md, section 3).

The deciders (strategist, overseer, rule floor, assessor, triage, fixer, foreman, close-out) used to push goals
into the crew's queues on their own, and they fought: run 20261125-214645 dropped 74 queued goals, triage
queued an iron outpost four times that the overseer had refused, and a paused fix came back as a protected
plan goal. The queues stay (the plan, the urgent fixes, the close-out), but every write goes through here:

- a goal for a target that is already running or queued is merged (its asker is noted), not queued twice;
- an overseer veto (a forbid) holds for everyone but the rule floor's fuel and power fixes;
- a goal the overseer dropped or paused is not queued again by another decider for HOLD_DROPPED_MIN;
- a job that just finished is not queued again for DONE_HOLD_MIN;
- every decision is recorded with who made it, and every decider sees the same summary (who asked for what,
  what was vetoed, what was dropped and why).
"""
from __future__ import annotations

from collections import deque

HOLD_DROPPED_MIN = 20.0        # game minutes a goal the overseer dropped stays out (unless the overseer orders it)
DONE_HOLD_MIN = 3.0            # a job just done is not queued again this soon (its copies are dropped as done)
SAFETY = ("rules",)            # the rule floor's fuel and power fixes: never held back by a decider
CHIEF = ("overseer",)          # the one decider whose word holds over the others


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


class Agenda:
    def __init__(self):
        self.history: deque = deque(maxlen=80)    # decisions: minute, who, action, goal, why
        self.dropped: dict[str, tuple] = {}       # target -> (minute, who, why)
        self.done: dict[str, float] = {}          # target -> minute
        self.asked: dict[str, list[str]] = {}     # target -> deciders that asked for it

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
        self.record(minute, who_of(goal), "done", goal)

    # ------------------------------------------------------------------ checks
    def holds(self, crew, goal: dict, who: str) -> str | None:
        """Why this decider may not queue this goal now (None: it may)."""
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
