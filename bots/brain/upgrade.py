"""Better machines where the old ones stand, what a sealed site may be rebuilt into, and sites nobody needs.

Players upgrade in place far more often than they rebuild: assembling machine 1 -> 2 -> 3, stone -> steel
furnaces, basic -> fast inserters, all the same size, so the layout stays and only the machines get faster.
The game here has no upgrade planner and will not place one machine over another, so the crew does it the way a
player without one would: pick the machine up (it comes back with what it held), place the better one on the same
tiles facing the same way, and give it its recipe (or its coal) again.

    candidates(crew, site)   the machines in a site with a faster same-size successor unlocked
    worth_it(site)           does the site wait on its machines (not on its inputs)? only then do faster ones help
    upgrade_site(crew, site) the swap, one machine at a time; a machine the game refuses goes back as it was
    successor_ok(old, goal)  a sealed site may be replaced by more of the same thing (rebuild bigger)
    idle sites               a factory whose output nobody has taken for IDLE_MIN minutes loses its seal and is
                             listed for retirement (its machines go back into the next build)
"""

from __future__ import annotations

import math

from .world import BuildError

# same footprint, faster (or the same machine with more slots): what replaces what
SUCCESSOR = {
    "assembling-machine-1": "assembling-machine-2",
    "assembling-machine-2": "assembling-machine-3",
    "stone-furnace": "steel-furnace",
    "inserter": "fast-inserter",
}
# belts are left alone: the links carry a few items a second, far under a yellow belt's 15, and every belt and
# underground pair would have to come up and go down again for nothing
MACHINES = {"assembling-machine-1", "assembling-machine-2", "assembling-machine-3", "stone-furnace",
            "steel-furnace"}
STARVED = ("no_ingredients", "item_ingredient_shortage", "waiting_for_source_items", "no_input_fluid",
           "fluid_ingredient_shortage")
UPGRADE_MAX = 24                # machines one upgrade goal swaps at most (the rest next time)
IDLE_MIN = 45.0                 # game minutes a factory's output may sit untouched before it counts as idle


def best(kn, name: str) -> str | None:
    """The fastest unlocked same-size successor of a machine (skipping steps already passed)."""
    unlocked = getattr(kn, "unlocked", set()) or set()
    out, nxt = None, SUCCESSOR.get(name)
    while nxt:
        if nxt in unlocked:
            out = nxt
        nxt = SUCCESSOR.get(nxt)
    return out


def candidates(crew, site) -> list[tuple[tuple, str]]:
    """(entity, successor) for the machines in the site worth swapping; inserters only beside a swapped
    machine's faster speed (a furnace twice as fast needs its inserters to keep up)."""
    out = []
    machines = [(e, best(crew.kn, e[0])) for e in site.entities if e[0] in MACHINES]
    machines = [(e, b) for e, b in machines if b]
    out += machines
    if machines:
        out += [(e, b) for e in site.entities if e[0] == "inserter" and (b := best(crew.kn, e[0]))]
    return out


def worth_it(site) -> bool:
    """Faster machines help a site that waits on its machines, not one that waits on its inputs (then the fix
    is more supply). A furnace row whose drills back up behind fuelled furnaces is machine-bound too."""
    total = sum(site.health.values())
    if not total:
        return False
    starved = sum(site.health.get(s, 0) for s in STARVED)
    if site.health.get("no_fuel") or site.health.get("no_power"):
        return False                                  # broken first: an upgrade does not fix that
    return starved / total < 0.5


def upgrade_site(crew, site, limit: int = UPGRADE_MAX) -> str:
    """Swap the site's machines for better ones in place. Returns what was done; raises if nothing could be."""
    w = crew.w
    todo = candidates(crew, site)[:limit]
    if not todo:
        raise BuildError(f"site {site.id} has no machine with a faster same-size successor unlocked "
                         f"({', '.join(sorted(set(SUCCESSOR)))} -> research what makes the next one)")
    need: dict[str, int] = {}
    for _, b in todo:
        need[b] = need.get(b, 0) + 1
    crew.unlock_items(list(need))
    crew.make(need, extra=True)
    recipes = _recipes(crew, site, [e for e, _ in todo])
    done, kept, swaps = 0, [], {}
    for e, new in todo:
        name, x, y, d = e
        if name.startswith("assembling-machine") and not recipes.get((x, y)):
            kept.append(f"{name} at ({x:.0f}, {y:.0f}): its recipe is unknown")
            continue
        if w.inventory.get(new, 0) < 1:
            kept.append(f"no {new} on hand")
            break
        w.doing(f"upgrading {name} to {new} in site {site.id}")
        try:
            if not w.pickup(name, x, y, d):
                kept.append(f"{name} at ({x:.0f}, {y:.0f}) was gone")
                swaps[e] = None
                continue
        except BuildError as exc:
            kept.append(str(exc)[:80])
            break
        try:
            w.place(new, x, y, d, w.footprint(new, x, y, d))
        except BuildError as exc:                     # put the old one back exactly as it was
            try:
                w.place(name, x, y, d, w.footprint(name, x, y, d))
                _restart(w, name, x, y, recipes.get((x, y)))
            except BuildError:
                swaps[e] = None
            kept.append(f"{new} at ({x:.0f}, {y:.0f}): {str(exc)[:60]}")
            continue
        _restart(w, new, x, y, recipes.get((x, y)))
        swaps[e] = (new, x, y, d)
        done += 1
    _rewrite(crew, site, swaps)
    if done:
        _rescale(crew, site, todo, swaps)
    if not done:
        raise BuildError(f"no machine in site {site.id} was upgraded: {'; '.join(kept[:3])}")
    site.params["upgraded_minute"] = round(crew._minute(), 1)
    gained = ", ".join(f"{n}x {k}" for k, n in sorted(_count(swaps).items()))
    return f"upgraded {done} machine(s) in site {site.id} ({gained})" + (f"; kept: {'; '.join(kept[:3])}" if kept else "")


FALLBACK_SPEED = {"assembling-machine-1": 0.5, "assembling-machine-2": 0.75, "assembling-machine-3": 1.25,
                  "stone-furnace": 1.0, "steel-furnace": 2.0}


def speed(kn, name: str) -> float:
    ent = (getattr(kn, "entities", {}) or {}).get(name) or {}
    return float(ent.get("crafting_speed") or FALLBACK_SPEED.get(name, 1.0))


def _rescale(crew, site, todo: list, swaps: dict) -> None:
    """Faster machines make more and want more: the site's designed rate, its input ports' rates and its
    cells' rates grow with the machines' speed, so the planner's books and the input budget see the new site."""
    machines = [e for e in site.entities if e[0] in MACHINES]
    if not machines:
        return
    before = {e: e[0] for e in machines}
    for old, new in swaps.items():
        if new and new in before:
            before[new] = old[0]
    old_speed = sum(speed(crew.kn, n) for n in before.values())
    new_speed = sum(speed(crew.kn, e[0]) for e in machines)
    if old_speed <= 0 or abs(new_speed - old_speed) < 1e-6:
        return
    f = new_speed / old_speed
    if site.params.get("per_minute"):
        site.params["per_minute"] = round(float(site.params["per_minute"]) * f, 1)
    for pt in site.params.get("ports", []):
        if pt.get("item") and not pt.get("fuel") and pt.get("per_minute"):
            pt["per_minute"] = round(float(pt["per_minute"]) * f, 2)
    for c in site.cells:
        for k in ("item_in", "item_out"):
            if c.get(k):
                c[k] = {i: round(r * f, 2) for i, r in c[k].items()}
    site.params["speedup"] = round(float(site.params.get("speedup", 1.0)) * f, 2)


def _count(swaps: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in swaps.values():
        if v:
            out[v[0]] = out.get(v[0], 0) + 1
    return out


def _recipes(crew, site, ents: list) -> dict:
    """Each assembler's recipe: from a fresh look at it (what the game says it makes), else the site's cells."""
    w, out = crew.w, {}
    asm = [e for e in ents if e[0].startswith("assembling-machine")]
    looked: list = []
    for name, x, y, d in asm:
        if not any(math.dist((x, y), q) < 20 for q in looked):
            try:
                w.look(x, y, radius=24)
            except BuildError:
                pass
            looked.append((x, y))
        seen = (getattr(w, "entities", {}) or {}).get((x, y)) or {}
        r = seen.get("recipe")
        if not r:
            r = next((c.get("recipe") for c in site.cells if c.get("recipe") and abs(c.get("x", 1e9) - x) < 1
                      and abs(c.get("y", 1e9) - y) < 1), None)
        if r:
            out[(x, y)] = r
    return out


def _restart(w, name: str, x: float, y: float, recipe: str | None) -> None:
    """A placed machine back to work: its recipe, or (a furnace) some coal to start with."""
    if name.startswith("assembling-machine") and recipe:
        r = w.act({"type": "set_recipe", "x": x, "y": y, "recipe": recipe})
        if not r.get("ok"):
            raise BuildError(f"set_recipe {recipe}: {r.get('code')}")
    elif name.endswith("furnace"):
        w.fuel(x, y, 5)


def _rewrite(crew, site, swaps: dict) -> None:
    """The site's (and its links') records name the new machines, so removing it later picks up the right ones."""
    def swap(lst: list) -> list:
        return [x for x in ((swaps.get(e, e) if e in swaps else e) for e in lst) if x is not None]
    site.entities[:] = swap(site.entities)
    for ln in getattr(crew, "links", []):
        if not any(e in swaps for e in ln.get("entities", [])):
            continue
        ln["entities"] = swap(ln["entities"])
        ins = ln.get("ins")
        for e, v in swaps.items():                    # a link's own inserter, upgraded with the site
            if ins and v and (math.floor(e[1]), math.floor(e[2])) == tuple(ins[0]):
                ln["ins"] = (ins[0], ins[1], v[0])


def upgradable_sites(crew) -> list:
    """Sites with faster machines available that wait on their machines, most machines first."""
    out = [(len(candidates(crew, s)), s) for s in crew.base.sites if worth_it(s)]
    return [s for n, s in sorted(out, key=lambda q: -q[0]) if n]


# ------------------------------------------------------------------ rebuilding a sealed site
CAPACITY = {"factory": "per_minute", "workshop": "per_minute", "electric_mine": "count", "burner_mine": "count",
            "outpost": "count", "smelter_column": "count", "steam_power": "engines", "labs": "count",
            "research_station": "count", "pumpjack": "count"}
PRODUCT = {"factory": "item", "workshop": "item", "electric_mine": "resource", "burner_mine": "resource",
           "outpost": "resource", "smelter_column": "resource"}


def successor_ok(old, goal: dict | None) -> bool:
    """May a sealed site be replaced by this build? Yes when it makes the same thing at a higher rate (rebuild
    bigger), or when it is the electric version of a burner mine on the same ore."""
    if not goal or goal.get("do") != "build":
        return False
    mod = goal.get("module")
    if old.kind == "burner_mine" and mod == "electric_mine":
        return goal.get("resource") == old.params.get("resource")
    if mod != old.kind and not (old.kind == "labs" and mod == "research_station"):
        return False
    key = PRODUCT.get(old.kind)
    if key and goal.get(key) != old.params.get(key):
        return False
    cap = CAPACITY.get(old.kind)
    if cap is None:
        return False
    try:
        return float(goal.get(cap) or 0) > float(old.params.get(cap) or 0)
    except (TypeError, ValueError):
        return False


# ------------------------------------------------------------------ sites nobody needs any more
BLOCKED = ("full_output", "waiting_for_space_in_destination")


def output_unused(crew, site) -> bool:
    """A factory or workshop whose machines all wait on a full output, whose chests feed no link, and whose
    product neither the builder (a building) nor the labs (science) take: nothing needs it now."""
    if site.kind == "labs" and site.params.get("station"):
        total = sum(site.health.values())
        return not any(pt.get("pack") for pt in site.params.get("ports", [])) and total > 0 \
            and site.health.get("missing_science_packs", 0) + site.health.get("waiting_for_source_items", 0) >= total * 0.6
    if site.kind not in ("factory", "workshop"):
        return False
    total = sum(site.health.values())
    if not total or sum(site.health.get(s, 0) for s in BLOCKED) < max(1, math.ceil(total * 0.6)):
        return False
    item = site.params.get("item") or ""
    kn = crew.kn
    if item.endswith("science-pack") or item in (getattr(kn, "entities", {}) or {}):
        return False                                  # labs or the builder take these out by hand
    chests = {(round(x, 1), round(y, 1)) for x, y, _ in site.chests}
    return not any((round(ln["source"][0], 1), round(ln["source"][1], 1)) in chests
                   for ln in getattr(crew, "links", []) if ln.get("kind") != "trunk")


def update_idle(crew) -> None:
    """Note when each factory's output stopped being used; one unused for IDLE_MIN minutes is idle (and not
    sealed: it may be taken down for its machines)."""
    now = crew._minute()
    since = crew.__dict__.setdefault("idle_since", {})
    for s in crew.base.sites:
        if output_unused(crew, s):
            since.setdefault(s.id, now)
        else:
            since.pop(s.id, None)
    for sid in list(since):
        if crew.base.get(sid) is None:
            since.pop(sid, None)


def idle_sites(crew) -> list[dict]:
    now = crew._minute()
    out = []
    for sid, t in crew.__dict__.get("idle_since", {}).items():
        s = crew.base.get(sid)
        if s is not None and now - t >= IDLE_MIN:
            out.append({"site": sid, "kind": s.kind, "item": s.params.get("item"), "idle_minutes": round(now - t),
                        "fix": {"do": "remove", "site": sid, "force": True}})
    return out


def is_idle(crew, site_id) -> bool:
    return any(r["site"] == site_id for r in idle_sites(crew))


# ------------------------------------------------------------------ the mall: buildings made, not hand-crafted
# what the crew builds with most; once it has hand-crafted MALL_HAND_MIN of one, a small factory makes it and
# kits take it from that factory's chest (crew.ready_items) instead of the character crafting it
MALL_ITEMS = ("transport-belt", "inserter", "iron-gear-wheel", "electronic-circuit", "small-electric-pole",
              "electric-mining-drill", "assembling-machine-1", "assembling-machine-2", "stone-furnace",
              "steel-furnace", "iron-chest", "pipe", "underground-belt", "long-handed-inserter", "fast-inserter",
              "medium-electric-pole")
MALL_HAND_MIN = 40
MALL_RATE = 6.0
PROMOTE_MIN = 8.0               # a workshop item made this fast a minute gets a belt factory (the courier is one man)
FAIL_GAP_MIN = 30.0


def _makers(crew, item: str) -> list:
    return [s for s in crew.base.sites if s.kind == "factory" and s.params.get("item") == item]


def _recently_failed(crew, item: str) -> bool:
    now = crew._minute()
    return any(now - getattr(crew, k, {}).get(item, -1e9) < FAIL_GAP_MIN for k in ("_factory_failed", "failed_suppliers"))


def mall_goal(crew) -> dict | None:
    """The building the crew hand-crafted most that no factory makes yet, as a small factory goal."""
    try:
        from .modules import MODULES
        can = set(MODULES["factory"].can_make(crew.kn))
    except Exception:  # noqa: BLE001 - no catalog in some tests
        return None
    items = ((getattr(crew.w, "stats", None) or {}).get("items") or {})
    ranked = sorted(((int((items.get(i) or {}).get("hand_made") or 0), i) for i in MALL_ITEMS), reverse=True)
    for n, item in ranked:
        if n < MALL_HAND_MIN:
            break
        if item in can and not _makers(crew, item) and not _recently_failed(crew, item):
            return {"do": "build", "module": "factory", "item": item, "per_minute": MALL_RATE}
    return None


def promote_goal(crew) -> dict | None:
    """A workshop item made fast enough that one courier cannot keep it fed, and that belts can make: a belt
    factory for it (its inputs come by belt, workshop-made ones like plastic from the cell's chest)."""
    try:
        from .modules import MODULES
        can = set(MODULES["factory"].can_make(crew.kn))
    except Exception:  # noqa: BLE001
        return None
    rates: dict[str, float] = {}
    for _, c in crew.cells():
        for item, r in c.get("item_out", {}).items():
            rates[item] = rates.get(item, 0.0) + r
    for item, r in sorted(rates.items(), key=lambda kv: -kv[1]):
        if r < PROMOTE_MIN:
            break
        if item in can and not _makers(crew, item) and not _recently_failed(crew, item):
            return {"do": "build", "module": "factory", "item": item, "per_minute": round(r * 1.25, 1)}
    return None


def retire_promoted(crew) -> list[str]:
    """Workshop cells whose item a belt factory now makes (and has made for 10 minutes): the courier stops
    feeding them, and their chests stop counting as sources unless a belt already takes from them."""
    now, done = crew._minute(), []
    linked = {(round(ln["source"][0], 1), round(ln["source"][1], 1)) for ln in getattr(crew, "links", [])}
    for s in crew.base.of("workshop"):
        for c in s.cells:
            if c.get("retired") or c.get("machine") == "input" or len(c.get("item_out", {})) != 1:
                continue
            item = next(iter(c["item_out"]))
            fac = [f for f in _makers(crew, item) if now - f.built_minute >= 10 and f.health.get("working", 0)]
            if not fac:
                continue
            c["retired"] = True
            outs = {(round(x, 1), round(y, 1)) for x, y in c.get("out", [])}
            s.chests[:] = [ch for ch in s.chests if (round(ch[0], 1), round(ch[1], 1)) not in outs
                           or (round(ch[0], 1), round(ch[1], 1)) in linked]
            done.append(f"workshop cell for {item} in site {s.id} retired: factory site {fac[0].id} makes it by belt")
    return done
