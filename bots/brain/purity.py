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

from .belts import VEC, job_for, lay_belt, link_joins, tier_of
from .world import BuildError

CHECK_EVERY_MIN = 5.0       # game minutes between looks
MIN_FOREIGN = 3             # foreign items on a link's last belts before it counts (one stray plate is noise)
END_TILES = 4               # a link's last belts: where foreign items pile up, since nothing takes them
CHUNK = 40                  # tiles across one observe covers when a whole link is looked at
BELTS = ["transport-belt", "underground-belt", "fast-transport-belt", "fast-underground-belt",
         "express-transport-belt", "express-underground-belt"]           # every tier (belts.TIERS)


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
    if here["name"].endswith("transport-belt"):
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


# the game's own inserters (data/factorio-base.json): an observe naming one the game does not have is refused
# whole. Run 20261127-101219: "filter-inserter" and "stack-inserter" (gone since Factorio 2.0) made every purge
# fail ("unknown entity name"), so a research station's polluted green belt stayed and all four labs idled.
INSERTERS = ["burner-inserter", "inserter", "long-handed-inserter", "fast-inserter", "bulk-inserter"]


def inserters_onto(crew, tiles) -> list[dict]:
    """Every inserter that drops onto one of these tiles (observe gives each inserter's drop position)."""
    want = {tuple(t) for t in tiles}
    out, seen = [], set()
    for part in _chunks(sorted(want)):
        xs, ys = [t[0] for t in part], [t[1] for t in part]
        cx, cy = (min(xs) + max(xs)) / 2 + 0.5, (min(ys) + max(ys)) / 2 + 0.5
        r = int(max(max(xs) - min(xs), max(ys) - min(ys)) / 2) + 4
        d = crew.w.observe(min(64, r), x=cx, y=cy, include=["entities"], names=INSERTERS)
        for e in d.get("entities") or []:
            drop = e.get("drop")
            key = (e.get("x"), e.get("y"))
            if drop and _tile(drop) in want and key not in seen:
                seen.add(key)
                out.append(e)
    return out


def _source_foreign(crew, ln: dict) -> dict[str, int]:
    """What other than the link's item its own inserter picks up: the belt it taps or the chest it empties."""
    ins = ln.get("ins")
    if not ins:
        return {}
    tile, facing, _ = ins
    vx, vy = VEC[facing]
    src = (tile[0] + vx, tile[1] + vy)              # an inserter faces the tile it picks up from
    d = crew.w.observe(2, x=src[0] + 0.5, y=src[1] + 0.5, include=["entities"], belts=True)
    e = next((q for q in d.get("entities") or [] if _tile((q["x"], q["y"])) == src), None)
    if e is None:
        return {}
    held = lane_items(e) if e.get("name") in BELTS else {
        k: v for inv in (e.get("inventories") or {}).values() if isinstance(inv, dict) for k, v in inv.items()}
    return {k: int(v) for k, v in held.items() if k != ln["item"] and v}


def purge(crew, site_id, item: str | None = None) -> str:
    """Find what feeds foreign items into the site's link(s) of this item, stop it, and clean their belts.
    Three ways in: a belt that joins it from the side, another inserter that drops onto it, and the link's own
    source (the belt it taps or the chest it empties holds other items). Cleaning without stopping the cause
    only buys minutes (run 20261121-140755: six purges of site 83's coal belt in an hour; its inserter was
    lifting copper plates off the line beside the coal belt it was meant to tap)."""
    lns = [ln for ln in getattr(crew, "links", []) or []
           if ln.get("to_site") == site_id and (item is None or ln.get("item") == item) and ln.get("steps")]
    if not lns:
        raise BuildError(f"no belt link{' of ' + item if item else ''} into site {site_id}")
    notes = []
    for ln in lns:
        steps = ln["steps"]
        own = {tuple(s[1]) for s in steps}
        bad_src = _source_foreign(crew, ln)
        if bad_src:                                  # the source itself is wrong: lay the link again elsewhere
            what = ", ".join(f"{n} {k}" for k, n in sorted(bad_src.items(), key=lambda kv: -kv[1]))
            src = tuple(ln["source"])
            taps = getattr(crew, "_bad_taps", None)
            if taps is None:
                taps = crew._bad_taps = set()
            taps.add((math.floor(src[0]), math.floor(src[1])))
            crew._unlink(ln)
            crew._link_fails.pop(tuple(ln["port"]["end"]), None)
            notes.append(f"{ln['item']} into site {site_id}: its inserter picked up {what} from its source at "
                         f"{src}: the link was taken up (with what was on it) and that source is not used again; "
                         "the link step lays it from a clean one")
            continue
        tail = crew.link_tail(ln) if ln.get("ins") is None and hasattr(crew, "link_tail") else None
        joins = {tuple(t) for t in link_joins(steps, from_belt=ln.get("ins") is None, tail=tail)}
        seen = look_belts(crew, [s[1] for s in steps])
        leaks = sorted({_tile(p) for t in own for p in (seen.get(t) or {}).get("fed_by") or []} - own - joins)
        done = [_stop_leak(crew, q, own) for q in leaks]
        same = {tuple(q["ins"][0]) for q in getattr(crew, "links", []) or []    # its own, and boosts of its item
                if q.get("ins") and q.get("item") == ln["item"]}
        for e in inserters_onto(crew, own):         # another site's inserter dropping onto this belt
            at = _tile((e["x"], e["y"]))
            if at in same:
                continue
            crew.w.pickup(e["name"], e["x"], e["y"], e.get("direction", "north"))
            done.append(f"took up the {e['name']} at {at} that dropped onto it")
        dirty = [s for s in steps if any(k != ln["item"] for k in lane_items(seen.get(tuple(s[1])) or {}))]
        with job_for(crew.w, [ln["item"]], list(own), list(joins)):
            for s in dirty:                          # upstream first: clean belts carry clean items on
                t = s[1]
                bt = tier_of(ln.get("belt_tier"))     # the tier the line was laid in (yellow if unsaid)
                name = bt["ug"] if s[0] == "ug" else bt["belt"]
                if crew.w.pickup(name, t[0] + 0.5, t[1] + 0.5, s[2]):
                    lay_belt(crew.w, [s], bt)
        cause = "" if done else "; no cause found (no side join, no other inserter, a clean source)"
        notes.append(f"{ln['item']} into site {site_id}: " + "; ".join(
            done + [f"{len(dirty)} belt(s) cleaned" if dirty else "no foreign items left on it"]) + (
            cause if dirty else ""))
    crew._purity_at = -1e9                           # look again soon: did it hold?
    return " | ".join(notes)
