"""Buildings a site was meant to have and does not: the game refused their tile, or a belt line has a hole.

Run 20261122-133537: a gear factory's build placed 32 buildings and three belts were refused (a tree the map did
not know stood beside the port). Two of them were the factory's first belts behind its iron port, the third one
sat in the middle of its plate line. The belt from the iron mine ended one tile short of the factory, filled up and
backed up into the mine; the factory never made a gear. The build only wrote "3 failed" into the site's note, so
nobody looked again, and the planners read the starving factory as a plate shortage (another mine, an outpost, a
second gear factory) for the rest of the run.

    record(site, gaps)     a build's refused buildings, kept on the site (params["gaps"]): what, where, which way
    audit(crew)            every AUDIT_EVERY_MIN: for each starving site with belts, one look at its belts. A belt
                           that runs out onto an empty tile, with an unfed belt of the same site a few free tiles
                           on, is a hole: the belts that close it become gaps. It finds holes from any cause (and
                           in sites built before gaps were recorded)
    alerts(crew)           an urgent "gap-<site>" alert per site with gaps, fixed by {"do": "repair", "site"}; gaps
                           that failed MAX_TRIES repairs say so (rebuild the site elsewhere)
    repair(crew, site)     place them (Crew.repair calls it, with the pipe gaps of workshops)
"""

from __future__ import annotations

import math
from collections import deque

from .world import BuildError

AUDIT_EVERY_MIN = 5.0         # game minutes between audits
SITE_EVERY_MIN = 15.0         # one site is looked at again after this long at the earliest
MAX_BRIDGE = 4                # belts a hole may need: farther than that it is no hole but two separate lines
MAX_TRIES = 2                 # repairs a gap gets; after that the tile counts as blocked for good
STARVING_SHARE = 0.5          # a site with at least this share of its machines waiting for input gets audited
WAITING = ("waiting_for_source_items", "item_ingredient_shortage", "waiting_for_ingredients", "no_ingredients")
VEC = {"north": (0, -1), "east": (1, 0), "south": (0, 1), "west": (-1, 0)}
INSERTERS = ("burner-inserter", "inserter", "long-handed-inserter", "fast-inserter", "filter-inserter",
             "stack-inserter", "stack-filter-inserter", "bulk-inserter")
Tile = tuple[int, int]


def _tile(p) -> Tile:
    return math.floor(p[0]), math.floor(p[1])


def _key(g: dict) -> tuple:
    return g["name"], round(float(g["x"]), 1), round(float(g["y"]), 1)


# ------------------------------------------------------------------ recording
def record(site, gaps: list[dict], why: str = "") -> int:
    """Keep these refused buildings on the site (each once). Returns how many are new."""
    have = site.params.setdefault("gaps", [])
    known = {_key(g) for g in have}
    new = 0
    for g in gaps:
        g = dict(g)
        g.setdefault("tries", 0)
        if why and not g.get("why"):
            g["why"] = why
        if _key(g) not in known:
            have.append(g)
            known.add(_key(g))
            new += 1
    if not have:
        site.params.pop("gaps", None)
    return new


def pending(site) -> list[dict]:
    return [g for g in site.params.get("gaps") or [] if g.get("tries", 0) < MAX_TRIES]


def stuck(site) -> list[dict]:
    return [g for g in site.params.get("gaps") or [] if g.get("tries", 0) >= MAX_TRIES]


# ------------------------------------------------------------------ the belt-hole audit
def _starving(site) -> bool:
    total = sum((site.health or {}).values())
    return total > 0 and sum(site.health.get(k, 0) for k in WAITING) >= STARVING_SHARE * total


def _site_belts(site) -> list[tuple[Tile, str]]:
    return [(_tile((x, y)), d) for name, x, y, d in site.entities if name == "transport-belt"]


def _feed_ends(site) -> list[Tile]:
    """The last belt of each link that runs straight into the site (a feed port): it belongs to the link, but a
    hole right behind it starves the site just the same."""
    return [tuple(pt["end"]) for pt in site.params.get("ports", []) if pt.get("feed") and pt.get("fed")
            and pt.get("end")]


def _look(crew, tiles: list[Tile]) -> list[dict]:
    xs, ys = [t[0] for t in tiles], [t[1] for t in tiles]
    r = int(max(max(xs) - min(xs), max(ys) - min(ys)) / 2) + 4
    if r > 64:
        raise BuildError("site too big for one look")
    d = crew.w.observe(r, x=(min(xs) + max(xs)) / 2 + 0.5, y=(min(ys) + max(ys)) / 2 + 0.5,
                       include=["entities", "obstacles"], belts=True)
    return d.get("entities") or []


def holes(w, site, entities: list[dict]) -> list[dict]:
    """Belts that close the holes in a site's belt lines, from one look at it (entities with belt detail).

    A dead end: a belt of the site (or the last belt of a link into one of its feed ports) whose next tile holds
    nothing at all. An orphan: a belt of the site that nothing feeds, no other belt and no inserter (an
    underground's exit does not count: its entrance feeds it). A dead end with an orphan at most MAX_BRIDGE free
    tiles on is one line with a hole: the belts along the shortest way close it, entering the orphan from behind
    or from the side (never head-on). Each dead end and each orphan closes one hole at most."""
    own = {t for t, _ in _site_belts(site)}
    occupied: set[Tile] = set()
    belts: dict[Tile, dict] = {}
    drops: set[Tile] = set()
    for e in entities:
        try:
            occupied |= w.footprint(e["name"], e["x"], e["y"], e.get("direction", "north"))
        except Exception:  # noqa: BLE001 - an entity the catalog does not size: its own tile at least
            occupied.add(_tile((e["x"], e["y"])))
        if e["name"] == "transport-belt":
            belts[_tile((e["x"], e["y"]))] = e
        if e["name"] in INSERTERS and e.get("drop"):
            drops.add(_tile(e["drop"]))
    blocked = set(getattr(w, "water", ()) or ()) | set(getattr(w, "cliffs", ()) or ())

    def ahead(t: Tile, d: str) -> Tile:
        return t[0] + VEC[d][0], t[1] + VEC[d][1]

    ends = []
    for t in sorted(own | set(_feed_ends(site))):
        e = belts.get(t)
        if e is None or e.get("feeds"):
            continue
        a = ahead(t, e.get("direction", "north"))
        if a not in occupied and a not in blocked:
            ends.append((t, a))
    orphans = {}
    for t in own:
        e = belts.get(t)
        if e is not None and not e.get("fed_by") and t not in drops:
            orphans[t] = e.get("direction", "north")
    if not ends or not orphans:
        return []
    # a free tile another belt already points into would take that belt's items too: never part of a bridge
    into: dict[Tile, set[Tile]] = {}
    for t, e in belts.items():
        into.setdefault(ahead(t, e.get("direction", "north")), set()).add(t)
    pointed = set(into)

    def bridge(end: Tile, first: Tile):
        """Shortest way from the dead end's next tile to a tile beside an orphan, entering it legally."""
        if into.get(first, set()) - {end}:
            return None
        q = deque([(first, [first])])
        seen = {first}
        while q:
            t, path = q.popleft()
            for d, (dx, dy) in VEC.items():
                n = (t[0] + dx, t[1] + dy)
                if n in orphans and n != end and ahead(n, orphans[n]) != t:   # not into its front
                    return n, path
            if len(path) >= MAX_BRIDGE:
                continue
            for dx, dy in VEC.values():
                n = (t[0] + dx, t[1] + dy)
                if n in seen or n in occupied or n in blocked or n in pointed:
                    continue
                seen.add(n)
                q.append((n, path + [n]))
        return None

    found = []
    for end, first in ends:
        b = bridge(end, first)
        if b is not None:
            found.append((len(b[1]), end, b[0], b[1]))
    out, used_ends, used_orphans, used_tiles = [], set(), set(), set()
    for _, end, orphan, path in sorted(found):
        if end in used_ends or orphan in used_orphans or used_tiles & set(path):
            continue
        used_ends.add(end)
        used_orphans.add(orphan)
        used_tiles |= set(path)
        for i, t in enumerate(path):
            nxt = path[i + 1] if i + 1 < len(path) else orphan
            d = next(k for k, v in VEC.items() if v == (nxt[0] - t[0], nxt[1] - t[1]))
            out.append({"name": "transport-belt", "x": t[0] + 0.5, "y": t[1] + 0.5, "dir": d, "tries": 0,
                        "why": f"a hole in its belt line: the belt at {end} runs out {len(path)} tile(s) short of "
                               f"the belt at {orphan}"})
    return out


def audit(crew, force: bool = False) -> list[str]:
    """Look for belt holes in starving sites (throttled). Returns a note per site where holes were found."""
    now = crew._minute()
    if not force and now - crew.__dict__.get("_gap_audit_at", -1e9) < AUDIT_EVERY_MIN:
        return []
    crew._gap_audit_at = now
    seen_at = crew.__dict__.setdefault("_gap_site_at", {})
    notes = []
    for site in list(crew.base.sites):
        if not _site_belts(site) or not _starving(site) or pending(site):
            continue
        if not force and now - seen_at.get(site.id, -1e9) < SITE_EVERY_MIN:
            continue
        seen_at[site.id] = now
        tiles = [t for t, _ in _site_belts(site)] + _feed_ends(site)
        try:
            found = holes(crew.w, site, _look(crew, tiles))
        except Exception:  # noqa: BLE001 - out of observe range, a stub world: no verdict this time
            continue
        n = record(site, found)
        if n:
            notes.append(f"site {site.id}: {n} belt(s) missing in its belt lines (first at "
                         f"({found[0]['x']}, {found[0]['y']}))")
    return notes


# ------------------------------------------------------------------ alerts and repair
def alerts(crew) -> list[dict]:
    out = []
    for site in crew.base.sites:
        todo, given_up = pending(site), stuck(site)
        if todo:
            first = todo[0]
            names = sorted({g["name"] for g in todo})
            out.append({"level": "urgent", "key": f"gap-{site.id}",
                        "what": f"site {site.id} ({site.kind}{' ' + str(site.params.get('item')) if site.params.get('item') else ''}): "
                                f"{len(todo)} of its buildings were never placed ({', '.join(names)}; first at "
                                f"({first['x']}, {first['y']}): {str(first.get('why', ''))[:90]}). A belt line with "
                                "a hole carries nothing past it: repair places them",
                        "fix": {"do": "repair", "site": site.id}})
        elif given_up:
            fix = None
            if site.kind == "factory" and site.params.get("item"):
                fix = {"do": "replace", "site": site.id,
                       "with": {"do": "build", "module": "factory", "item": site.params["item"],
                                "per_minute": float(site.params.get("per_minute") or 5)}}
            out.append({"level": "warn", "key": f"gap-stuck-{site.id}",
                        "what": f"site {site.id} ({site.kind}): {len(given_up)} of its buildings could not be placed "
                                f"after {MAX_TRIES} repairs (first: {given_up[0]['name']} at ({given_up[0]['x']}, "
                                f"{given_up[0]['y']})): the site stays broken; rebuild it elsewhere",
                        "fix": fix})
    return out


def repair(crew, site) -> tuple[int, list[str]]:
    """Place a site's pending gaps. Returns (placed, notes on what is still open)."""
    todo = pending(site)
    if not todo:
        return 0, []
    w = crew.w
    need: dict[str, int] = {}
    for g in todo:
        if not g.get("placed"):
            need[g["name"]] = need.get(g["name"], 0) + 1
    if need:
        crew.unlock_items(list(need))
        crew.make(need, extra=True)
    done, left = 0, []
    for g in sorted(todo, key=lambda g: math.dist((g["x"], g["y"]), w.pos)):
        name, x, y, d = g["name"], float(g["x"]), float(g["y"]), g.get("dir") or "north"
        try:
            if not g.get("placed"):
                if name == "underground-belt":
                    t = _tile((x, y))
                    w.clear([t])
                    w.near(x, y)
                    r = w.retry_blocked([t], lambda: w.run.place(name, x, y, d, g.get("type") or "input"))
                    if not r.get("ok"):
                        raise BuildError(f"place {name} at ({x}, {y}): {r.get('code')} {r.get('message', '')}")
                    w.used.add(t)
                else:
                    w.place(name, x, y, d, w.footprint(name, x, y, d))
                site.entities.append((name, x, y, d))
                if name.endswith("electric-pole") or name == "substation":
                    site.poles.append(_tile((x, y)))
                if g.get("fuel"):
                    w.fuel(x, y, int(g["fuel"]))
            if g.get("recipe"):
                r = w.act({"type": "set_recipe", "x": x, "y": y, "recipe": g["recipe"]})
                if not r.get("ok"):
                    g["placed"] = True             # standing: only its recipe is left to set
                    raise BuildError(f"set_recipe {g['recipe']} at ({x}, {y}): {r.get('code')}")
            site.params["gaps"].remove(g)
            done += 1
        except BuildError as exc:
            g["tries"] = g.get("tries", 0) + 1
            g["why"] = f"repair: {str(exc)[:110]}"
            left.append(f"{name} at ({x}, {y}): {str(exc)[:70]}")
    if not site.params.get("gaps"):
        site.params.pop("gaps", None)
    return done, left
