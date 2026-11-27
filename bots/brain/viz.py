"""What the stream overlay draws of the brain's floorplan and factory designer (arena/overlay.html).

Two feeds, both written by the telemetry thread (telemetry.Telemetry._live):

- "design" in bot_live.json: the factory being designed or built right now. Every design attempt (the designer
  model's or the layout search's) is published with its layout, so the overlay shows each candidate drawing in,
  then turning red when it is refused (the reason underneath) or orange when it is kept; while it is built its
  buildings light up as they are placed.
- zones_plan.json beside bot_live.json (rev in bot_live "zones"): the floorplan as a small raster (start yard,
  spine, oil corridor, laid bus belts, buildings, water, ore) and the slots with their state (reserved, building,
  built), for the overlay's floorplan panel.

Nothing here touches the game, and every function swallows its own errors: telemetry never stops the bot.
"""
from __future__ import annotations

import math
import threading
import time

LOCK = threading.Lock()
DESIGN: dict = {}              # the designer's feed (see design_attempt)
HISTORY_MAX = 8
CELL = 2                       # tiles per pixel of the floorplan raster
MAX_PX = 220                   # the raster's longest side, in pixels (the cell grows for a big base)


# ---------------------------------------------------------------------------------------------- the designer
def _fab_ents(d) -> tuple[int, int, list]:
    """A fab Design as [x, y, w, h, kind] boxes relative to its own corner. Kinds: m machine, f furnace/drill,
    s input port, b belt, u underground, c chest, i inserter, p pole."""
    boxes = []
    for c in getattr(d, "cells", []) or []:
        try:
            ts = list(c.footprint())
        except Exception:  # noqa: BLE001
            continue
        if not ts:
            continue
        xs, ys = [t[0] for t in ts], [t[1] for t in ts]
        kind = {"asm": "m", "mine": "f"}.get(getattr(c, "kind", ""), "s")
        boxes.append((min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1, kind))
    for r in getattr(d, "routes", []) or []:
        for t in r.path:
            boxes.append((t[0], t[1], 1, 1, "u" if r.kinds.get(t, "belt") != "belt" else "b"))
        for t in r.chests:
            boxes.append((t[0], t[1], 1, 1, "c"))
    try:
        for t, _ in d.inserters():
            boxes.append((t[0], t[1], 1, 1, "i"))
    except Exception:  # noqa: BLE001
        pass
    for t in getattr(d, "poles", []) or []:
        boxes.append((t[0], t[1], 1, 1, "p"))
    return _relative(boxes)


def _relative(boxes) -> tuple[int, int, list]:
    if not boxes:
        return 0, 0, []
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes)
    y1 = max(b[1] + b[3] for b in boxes)
    order = {"m": 0, "f": 0, "s": 1, "i": 2, "c": 3, "b": 4, "u": 4, "p": 5}   # roughly the order they are placed
    ents = sorted(([b[0] - x0, b[1] - y0, b[2], b[3], b[4]] for b in boxes), key=lambda e: order.get(e[4], 9))
    return x1 - x0, y1 - y0, ents


def _slot_ents(block) -> tuple[int, int, list]:
    """A zone slot's Atlas design (zones.SegBlock.wd) as boxes, like _fab_ents."""
    kinds = {"machine": "m", "belt": "b", "ug-in": "u", "ug-out": "u", "inserter": "i", "pole": "p",
             "splitter": "b", "chest": "c"}
    boxes = []
    try:
        for e in block.wd.entities():
            ts = e["tiles"]
            xs, ys = [t[0] for t in ts], [t[1] for t in ts]
            k = kinds.get(e.get("kind") or "", "c" if "chest" in str(e.get("name")) else "b")
            boxes.append((min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1, k))
    except Exception:  # noqa: BLE001
        return 0, 0, []
    return _relative(boxes)


REFUSED_SHOW_S = 120            # a refused attempt stays on the overlay this long unless another follows
DESIGN_ROSTER: dict[str, list] = {}       # item -> its last roster (kept across attempts)


def design_attempt(item: str, rate: float, focus, placer: str, d, status: str, why: str = "",
                   roster: list | None = None, room: int | None = None) -> None:
    """One design attempt: status "drawing" (just started), "kept" (it will be built) or "refused". roster: the
    machines and inputs the layout must fit ([name, count, "m"|"in"]), room: the area's margin in tiles."""
    try:
        w, h, ents = _fab_ents(d) if d is not None else (0, 0, [])
        with LOCK:
            same = DESIGN.get("item") == item and time.time() - DESIGN.get("since", 0) < 1800
            hist = list(DESIGN.get("history") or []) if same else []
            n = int(DESIGN.get("attempt") or 0) + (1 if status != "drawing" else 0) if same else (
                0 if status == "drawing" else 1)
            if status != "drawing":
                hist = (hist + [{"ok": status == "kept", "why": str(why)[:90], "placer": placer}])[-HISTORY_MAX:]
            DESIGN.clear()
            DESIGN.update({"item": item, "rate": round(float(rate), 1),
                           "at": [round(float(focus[0])), round(float(focus[1]))] if focus else None,
                           "placer": placer, "status": status, "why": str(why)[:140], "attempt": n,
                           "history": hist, "w": w, "h": h, "ents": ents, "t": time.time(),
                           "since": time.time() if not same else DESIGN.get("since", time.time()) or time.time(),
                           "slot": None,
                           "roster": roster if roster is not None else (DESIGN_ROSTER.get(item) or []),
                           "room": room})
            if roster:
                DESIGN_ROSTER[item] = roster
    except Exception:  # noqa: BLE001 - never let the overlay feed disturb a design
        pass


def design_slot(block) -> None:
    """A zone slot's factory: its design is fixed (no attempts), it goes straight to being built."""
    try:
        w, h, ents = _slot_ents(block)
        with LOCK:
            DESIGN.clear()
            DESIGN.update({"item": block.product, "rate": round(float(getattr(block.spec, "per_s", 0) or 0) * 60, 1),   # (BlockSpec has per_s)
                           "at": None, "placer": "slot", "status": "kept", "why": f"slot {block.slot.id}",
                           "attempt": 1, "history": [{"ok": True, "why": f"designed for slot {block.slot.id}",
                                                      "placer": "slot"}],
                           "w": w, "h": h, "ents": ents, "t": time.time(), "since": time.time(),
                           "slot": block.slot.id})
    except Exception:  # noqa: BLE001
        pass


def design_feed(crew) -> dict | None:
    """The designer's feed for bot_live.json, with how many buildings of it are placed so far (the build's
    recorder), or None when nothing was designed in the last 4 minutes and nothing is being built."""
    with LOCK:
        d = dict(DESIGN)
    if not d:
        return None
    age = time.time() - d.get("t", 0)
    if d.get("status") == "refused" and age > REFUSED_SHOW_S:
        return None                                   # the designer moved on (another place, a feeder, a saved one)
    rec = getattr(getattr(crew, "w", None), "recorder", None) if crew is not None else None
    if d.get("status") == "kept" and isinstance(rec, list) and age < 1800:
        # its build: what the recorder holds beyond what it held when the design was kept (a feeder or mine built
        # first in the same goal is not counted)
        with LOCK:
            if DESIGN.get("t") == d.get("t") and "rec0" not in DESIGN:
                DESIGN["rec0"] = len(rec)
            rec0 = DESIGN.get("rec0", 0) if DESIGN.get("t") == d.get("t") else 0
        placed = max(0, len(rec) - rec0)
        d["placed"] = min(placed, len(d.get("ents") or []))
        d["status"] = "building" if placed else "kept"
    elif age > (1200 if d.get("status") == "drawing" else 240):
        return None                                   # (a model drawing a layout can take minutes per attempt)
    d.pop("rec0", None)
    d["age"] = round(time.time() - d.get("t", time.time()), 1)
    d.pop("since", None)
    return d


# ---------------------------------------------------------------------------------------------- the floorplan
def zones_key(crew) -> tuple | None:
    """What changes the floorplan picture: reservations, open segments, built slots, the laid bus."""
    z = getattr(crew, "zones", None)
    if z is None or not getattr(z, "reserved", False):
        return None
    built = tuple(sorted(k for st in (z.opened or {}).values() for k in (st.get("built") or {})))
    bus = sum(len(s.entities) for s in crew.base.sites if s.kind == "bus")
    return (len(z.block), tuple(sorted(z.opened or {})), built, bus // 20, len(getattr(z, "slots", []) or []))


def _slot_state(z, crew, slot) -> tuple[str, str | None]:
    """reserved | spare | planned | building | built, and the item its factory makes (if a segment has a block there)."""
    item = None
    state = "reserved"
    cur = ((getattr(crew, "current", None) or {}).get("goal") or {})
    cur_item = cur.get("item") or (cur.get("with") or {}).get("item")
    for n, st in (z.opened or {}).items():
        try:
            seg = z.segment(n)
        except Exception:  # noqa: BLE001 - no plan at hand (just resumed): what was built, by where it stands
            x0, y0, w, h = slot.rect
            for key, where in (st.get("built") or {}).items():
                if isinstance(where, (list, tuple)) and len(where) == 2 and                         x0 <= where[0] < x0 + w and y0 <= where[1] < y0 + h:
                    item = str(key).split("#")[0].split("-", 1)[-1]
                    state = "built"
            continue
        for b in seg.blocks:
            if b.slot.id != slot.id:
                continue
            item = b.product
            done = st.get("built") or {}
            first_built = not getattr(b, "growth", False) or any(
                o.key in done for o in seg.blocks if not getattr(o, "growth", False) and o.spec.id == b.spec.id)
            if b.key in done:
                state = "built"
            elif cur_item == b.product and first_built and                     cur.get("module", (cur.get("with") or {}).get("module")) == "factory" and                     not (getattr(b, "growth", False) and not z.__dict__.get("grow_asked", {}).get(b.key)):
                state = "building"
            elif state != "built":
                state = "spare" if getattr(b, "growth", False) else "planned"   # a copy, built only if needed
    return state, item


def zones_plan(crew) -> dict | None:
    """The floorplan picture for the overlay (zones_plan.json)."""
    z = getattr(crew, "zones", None)
    if z is None or not getattr(z, "reserved", False):
        return None
    w = crew.w
    yard = tuple(z.yard) if z.yard else None
    pts = list(z.block)
    if yard:
        pts += [(yard[0], yard[1]), (yard[2], yard[3])]
    bus_tiles = set()
    for s in crew.base.sites:
        if s.kind == "bus":
            for name, x, y, d in s.entities:
                if "belt" in name or "splitter" in name:
                    bus_tiles.add((math.floor(x), math.floor(y)))
    if not pts:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    x0, y0, x1, y1 = min(xs) - 12, min(ys) - 12, max(xs) + 12, max(ys) + 12
    cell = max(CELL, math.ceil(max(x1 - x0, y1 - y0) / MAX_PX))
    W, H = (x1 - x0) // cell + 1, (y1 - y0) // cell + 1
    spine = set().union(*(c.tiles for c in z.corridors.values() if c.kind == "spine")) if z.corridors else set()
    oil = set().union(*(c.tiles for c in z.corridors.values() if c.kind == "oil")) if z.corridors else set()
    ore = set()
    for tiles in (getattr(w, "resources", None) or {}).values():
        ore |= {(int(t[0]), int(t[1])) for t in tiles}
    seen, water, used = getattr(w, "seen", set()), getattr(w, "water", set()), getattr(w, "used", set())
    rows = []
    for py in range(H):
        row = []
        for px in range(W):
            tx, ty = x0 + px * cell, y0 + py * cell
            block = [(tx + a, ty + b) for a in range(cell) for b in range(cell)]
            if any(t in bus_tiles for t in block):
                ch = "L"
            elif any(t in used for t in block):
                ch = "#"
            elif any(t in spine for t in block):
                ch = "="
            elif any(t in oil for t in block):
                ch = "o"
            elif any(t in water for t in block):
                ch = "~"
            elif any(t in ore for t in block):
                ch = "r"
            elif yard and yard[0] <= tx < yard[2] and yard[1] <= ty < yard[3]:
                ch = "y"
            elif (tx, ty) in seen:
                ch = "."
            else:
                ch = " "
            row.append(ch)
        rows.append("".join(row))
    slots = []
    for s in getattr(z, "slots", []) or []:
        st, item = _slot_state(z, crew, s)
        slots.append({"id": s.id, "zone": s.zone, "rect": list(s.rect), "state": st, "item": item})
    return {"origin": [x0, y0], "cell": cell, "w": W, "h": H, "raster": rows, "slots": slots,
            "yard": list(yard) if yard else None, "bus_laid": len(bus_tiles), "t": time.time()}
