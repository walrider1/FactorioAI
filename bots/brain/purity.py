"""Belt purity: every belt carries what it was laid for, and a belt that does not is cleaned and its leak stopped.

Run 20261120-211555: three coal fuel strips were laid with their last belt pointing into the side of iron belts
laid earlier. From minute 108 their spare coal ran onto the iron feeds of two gear factories and red science. One
lane of each feed filled with coal that no assembler takes, and the sites ran on half their iron for the rest of
the run. Nobody noticed: the bot could not see belt contents, and relinks only cleared the coal until it came back.

Prevention is in world.belt_job (belts laid isolated: the game refuses joins nobody planned). This is the cure,
for what slipped through and for belts laid before the guard:

    check(crew)              every CHECK_EVERY_MIN: the last belts of each link, by observe {belts: true}; a link
                             whose belts hold MIN_FOREIGN+ items of another kind is an urgent "belt-foreign" alert,
                             fixed by the rule floor with {"do": "purge", "site", "item"}
    purge(crew, site, item)  find every belt outside the link that feeds into it (not its declared joins), turn
                             it away or take it up, then take up and lay again each belt of the link that holds
                             foreign items (picking a belt up returns what is on it), upstream first
"""

from __future__ import annotations

import math

from .belts import VEC, job_for, lay_belt, link_joins
from .world import BuildError

CHECK_EVERY_MIN = 5.0       # game minutes between looks
MIN_FOREIGN = 3             # foreign items on a link's last belts before it counts (one stray plate is noise)
END_TILES = 4               # a link's last belts: where foreign items pile up, since nothing takes them
CHUNK = 40                  # tiles across one observe covers when a whole link is looked at
BELTS = ["transport-belt", "underground-belt"]


def _tile(p) -> tuple[int, int]:
    return math.floor(p[0]), math.floor(p[1])


def lane_items(e: dict) -> dict[str, int]:
    """What is on a belt, both lanes together (observe {belts: true})."""
    lanes = e.get("lanes") or []
    if isinstance(lanes, dict):                  # an empty Lua table can come back as {} or []
        lanes = list(lanes.values())
    out: dict[str, int] = {}
    for lane in lanes:
        if isinstance(lane, dict):
            for k, v in lane.items():
                out[k] = out.get(k, 0) + int(v or 0)
    return out


def _chunks(tiles: list[tuple[int, int]]) -> list[list[tuple[int, int]]]:
    out, cur = [], []
    for t in tiles:
        if cur and (max(abs(t[0] - c[0]) for c in cur) > CHUNK or max(abs(t[1] - c[1]) for c in cur) > CHUNK):
            out.append(cur)
            cur = []
        cur.append(t)
    return out + ([cur] if cur else [])


def look_belts(crew, tiles) -> dict[tuple[int, int], dict]:
    """The belts on these tiles with their lanes and neighbours: tile -> entity."""
    seen: dict[tuple[int, int], dict] = {}
    for part in _chunks([tuple(t) for t in tiles]):
        xs, ys = [t[0] for t in part], [t[1] for t in part]
        cx, cy = (min(xs) + max(xs)) / 2 + 0.5, (min(ys) + max(ys)) / 2 + 0.5
        r = int(max(max(xs) - min(xs), max(ys) - min(ys)) / 2) + 2
        d = crew.w.observe(min(64, r), x=cx, y=cy, include=["entities"], names=BELTS, belts=True)
        want = set(part)
        for e in d.get("entities") or []:
            t = _tile((e["x"], e["y"]))
            if t in want:
                seen[t] = e
    return seen


def _foreign(ln: dict, seen: dict, tiles) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in tiles:
        e = seen.get(tuple(t))
        for k, v in (lane_items(e) if e else {}).items():
            if k != ln["item"]:
                out[k] = out.get(k, 0) + v
    return out


def check(crew) -> list[dict]:
    """Alerts for links whose belts carry something else (cached between looks)."""
    now = crew._minute()
    if now - crew.__dict__.get("_purity_at", -1e9) < CHECK_EVERY_MIN:
        return list(crew.__dict__.get("_purity_alerts", []))
    crew._purity_at = now
    alerts = []
    for ln in getattr(crew, "links", []) or []:
        steps = ln.get("steps") or []
        if not steps or not ln.get("item") or ln.get("to_site") is None:
            continue
        tail = [tuple(s[1]) for s in steps[-END_TILES:]]
        try:
            seen = look_belts(crew, tail)
        except Exception:  # noqa: BLE001 - out of observe range, a stub world: no verdict
            continue
        bad = _foreign(ln, seen, tail)
        if sum(bad.values()) >= MIN_FOREIGN:
            what = ", ".join(f"{n} {k}" for k, n in sorted(bad.items(), key=lambda kv: -kv[1]))
            alerts.append({"level": "urgent", "key": f"belt-foreign-{ln['to_site']}-{ln['item']}",
                           "what": f"the {ln['item']} belt into site {ln['to_site']} carries {what}: another belt "
                                   "joins it, and the foreign items block a lane the site never empties",
                           "fix": {"do": "purge", "site": ln["to_site"], "item": ln["item"]}})
    crew._purity_alerts = alerts
    return list(alerts)


def _stop_leak(crew, q: tuple[int, int], own: set) -> str:
    """A belt outside the link feeds into it at q: turn it toward an empty tile, or take it up."""
    w = crew.w
    d = w.observe(3, x=q[0] + 0.5, y=q[1] + 0.5, include=["entities"])
    ents = d.get("entities") or []
    here = next((e for e in ents if _tile((e["x"], e["y"])) == q and e["name"] in BELTS), None)
    if here is None:
        return f"the belt at {q} is gone"
    taken = {_tile((e["x"], e["y"])) for e in ents}
    if here["name"] == "transport-belt":
        cur = here.get("direction", "north")
        for side in [k for k in VEC if k != cur and VEC[k] != (-VEC[cur][0], -VEC[cur][1])]:
            ahead = (q[0] + VEC[side][0], q[1] + VEC[side][1])
            if ahead in own or ahead in taken:
                continue
            w.near(q[0] + 0.5, q[1] + 0.5)
            r = w.act({"type": "rotate", "x": q[0] + 0.5, "y": q[1] + 0.5, "direction": side})
            merges = (r.get("data") or {}).get("merges") or []
            if r.get("ok") and not any(m.get("way") == "into" for m in merges if isinstance(m, dict)):
                w.keepout |= {ahead}                  # nothing is routed in front of its new dead end
                return f"turned the belt at {q} {side}: it ends dead now"
            if r.get("ok"):                           # it found another belt: back as it was, then take it up
                w.act({"type": "rotate", "x": q[0] + 0.5, "y": q[1] + 0.5, "direction": cur})
            break
    w.pickup(here["name"], here["x"], here["y"], here.get("direction", "north"))
    return f"took up the {here['name']} at {q} that fed into it"


def purge(crew, site_id, item: str | None = None) -> str:
    """Stop every leak into the site's link(s) of this item and clean their belts."""
    lns = [ln for ln in getattr(crew, "links", []) or []
           if ln.get("to_site") == site_id and (item is None or ln.get("item") == item) and ln.get("steps")]
    if not lns:
        raise BuildError(f"no belt link{' of ' + item if item else ''} into site {site_id}")
    notes = []
    for ln in lns:
        steps = ln["steps"]
        own = {tuple(s[1]) for s in steps}
        joins = {tuple(t) for t in link_joins(steps, from_belt=ln.get("ins") is None)}
        seen = look_belts(crew, [s[1] for s in steps])
        leaks = sorted({_tile(p) for t in own for p in (seen.get(t) or {}).get("fed_by") or []} - own - joins)
        done = [_stop_leak(crew, q, own) for q in leaks]
        dirty = [s for s in steps if any(k != ln["item"] for k in lane_items(seen.get(tuple(s[1])) or {}))]
        with job_for(crew.w, [ln["item"]], list(own), list(joins)):
            for s in dirty:                          # upstream first: clean belts carry clean items on
                t = s[1]
                name = "underground-belt" if s[0] == "ug" else "transport-belt"
                if crew.w.pickup(name, t[0] + 0.5, t[1] + 0.5, s[2]):
                    lay_belt(crew.w, [s])
        notes.append(f"{ln['item']} into site {site_id}: " + "; ".join(
            done + [f"{len(dirty)} belt(s) cleaned" if dirty else "no foreign items left on it"]))
    crew._purity_at = -1e9                           # look again soon: did it hold?
    return " | ".join(notes)
