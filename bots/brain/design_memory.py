"""Saved factory designs (docs/hybrid-plan.md, section 2): every belt factory that gets built is kept, relative to
an anchor, under runs/local/designs/<item>/<signature>.json, so a later goal for the same factory (this run or any
later one) reuses it instead of designing from scratch while the crew waits (run 20261125-214645: about 115 real
minutes of factory design, 3 of 35 advanced-circuit designs worked).

    store = design_memory.store_dir(case_dir)          # None (no case_dir, e.g. tests): memory off
    design_memory.save(store, d, focus, inputs, plates_in)
    for rec in design_memory.candidates(store, item, rate, inputs, ug=..., feeds=...):
        d = design_memory.place(rec, centre)            # the saved design moved so its middle sits at `centre`

A saved design is only a candidate: the caller (Factory._recall) checks every tile is free on today's ground, the
input reach and the route probe, exactly as for a design made now.

The signature is what decides whether a saved layout can stand in for a fresh one: the item, a rate bucket, the
set of inputs that come in from outside, plates fed or its own mines, and the kit it needs (underground belts,
belt feeds, the assembler tier). Translation only: no rotation (routes and pins would need turning with it).
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from fab.cells import Cell, PinSite
from fab.design import Design
from fab.place import Net
from fab.recipes import Plan
from fab.route import Pin, Route

RATE_LO, RATE_HI = 0.8, 1.25        # a saved design serves an asked rate between 0.8x and 1.25x its own
ASSEMBLER = "assembling-machine-1"  # the tier fab's cells place (fab.cells.Cell.entities)
VERSION = 1

Tile = tuple[int, int]


def store_dir(case_dir) -> Path | None:
    """runs/local/designs, from Factory.case_dir (runs/local/<run>/chiplets), as Crew._memory_path does."""
    return Path(case_dir).parents[1] / "designs" if case_dir is not None else None


def rate_ok(asked: float, saved: float) -> bool:
    return saved > 0 and RATE_LO * saved - 1e-9 <= asked <= RATE_HI * saved + 1e-9


def kit_of(d: Design) -> dict:
    """What a design needs beyond plain belts and inserters: underground belts, which inputs come in on a belt
    (feeds need lines that can cross), and the assembler tier."""
    return {"ug": d.undergrounds > 0, "feeds": sorted(c.product for c in d.cells if c.kind == "feed"),
            "asm": ASSEMBLER}


def signature(item: str, per_minute: float, inputs, plates_in: bool, kit: dict) -> dict:
    bucket = round(math.log(max(float(per_minute), 0.01), RATE_HI))      # one bucket per x1.25
    return {"item": item, "bucket": bucket, "inputs": sorted(set(inputs)), "plates": "in" if plates_in else "mine",
            "kit": kit}


def sig_hash(sig: dict) -> str:
    return hashlib.sha1(json.dumps(sig, sort_keys=True).encode("utf-8")).hexdigest()[:12]


# ------------------------------------------------------------------ design <-> json, relative to an anchor
def _rel(t, a: Tile) -> list[int]:
    return [int(t[0]) - a[0], int(t[1]) - a[1]]


def to_record(d: Design, anchor: Tile) -> dict:
    """The design's layout with every tile relative to `anchor`."""
    def site(s: PinSite) -> list:
        return [_rel(s.inserter, anchor), _rel(s.outer, anchor), _rel(s.machine, anchor)]
    return {
        "target": d.target, "per_minute": float(d.per_minute),
        "plan": {"target": d.plan.target, "per_minute": float(d.plan.per_minute), "machines": dict(d.plan.machines),
                 "supplied": dict(d.plan.supplied), "rate": dict(d.plan.rate)},
        "cells": [{"id": c.id, "kind": c.kind, "product": c.product, "recipe": c.recipe, "ore": c.ore,
                   "x": c.x - anchor[0], "y": c.y - anchor[1], "rot": c.rot} for c in d.cells],
        "nets": [{"item": n.item, "sources": list(n.sources), "sinks": list(n.sinks)} for n in d.nets],
        "routes": [{"item": r.item,
                    "path": [_rel(t, anchor) for t in r.path],
                    "kinds": [r.kinds[t] for t in r.path],
                    "facing": [r.facing.get(t, "north") for t in r.path],
                    "pins": [{"cell": p.cell, "site": site(p.site), "into": p.into_cell} for p in r.pins],
                    "chests": [_rel(t, anchor) for t in r.chests]} for r in d.routes],
        "poles": [_rel(t, anchor) for t in d.poles],
        "box": [d.box[0] - anchor[0], d.box[1] - anchor[1], d.box[2] - anchor[0], d.box[3] - anchor[1]],
    }


def from_record(rec: dict, anchor: Tile) -> Design:
    """The saved layout rebuilt with its anchor at `anchor` (a fresh Design: nothing shared with the record)."""
    ax, ay = anchor

    def t(v) -> Tile:
        return (int(v[0]) + ax, int(v[1]) + ay)
    pl = rec["plan"]
    plan = Plan(pl["target"], float(pl["per_minute"]), dict(pl["machines"]), dict(pl["supplied"]), dict(pl["rate"]))
    cells = [Cell(c["id"], c["kind"], c["product"], c.get("recipe"), c.get("ore"), int(c["x"]) + ax,
                  int(c["y"]) + ay, int(c.get("rot", 0)), placed=True) for c in rec["cells"]]
    routes = []
    for r in rec["routes"]:
        path = [t(v) for v in r["path"]]
        routes.append(Route(r["item"], path, dict(zip(path, r["kinds"])), dict(zip(path, r["facing"])),
                            [Pin(p["cell"], PinSite(*(t(v) for v in p["site"])), bool(p["into"]))
                             for p in r["pins"]],
                            [t(v) for v in r["chests"]]))
    b = rec["box"]
    return Design(rec["target"], float(rec["per_minute"]), plan, cells,
                  [Net(n["item"], list(n["sources"]), list(n["sinks"])) for n in rec["nets"]], routes,
                  [t(v) for v in rec["poles"]], (b[0] + ax, b[1] + ay, b[2] + ax, b[3] + ay),
                  placer="memory", notes=["a saved design, reused"])


def anchor_of(d: Design) -> Tile:
    """The box's north-west corner: where every saved tile is measured from."""
    return (int(d.box[0]), int(d.box[1]))


def middle(rec: dict) -> tuple[float, float]:
    """The middle of a saved layout's buildings, relative to its anchor."""
    pts = [(c["x"], c["y"]) for c in rec["layout"]["cells"]] or [(0, 0)]
    return (sum(p[0] for p in pts) / len(pts) + 1, sum(p[1] for p in pts) / len(pts) + 1)


def place(rec: dict, centre) -> Design:
    """The saved design moved so the middle of its buildings sits at `centre`."""
    mx, my = middle(rec)
    return from_record(rec["layout"], (round(centre[0] - mx), round(centre[1] - my)))


# ------------------------------------------------------------------ the store
def save(store: Path | None, d: Design, focus, inputs, plates_in: bool) -> Path | None:
    """Keep a design that was built. The same signature again counts one more build and keeps the latest layout."""
    if store is None or not d.ok:
        return None
    sig = signature(d.target, d.per_minute, inputs, plates_in, kit_of(d))
    path = store / _safe(d.target) / f"{sig_hash(sig)}.json"
    builds = 0
    try:
        if path.exists():
            builds = int(json.loads(path.read_text(encoding="utf-8")).get("builds") or 0)
    except (OSError, ValueError):
        builds = 0
    anchor = anchor_of(d)
    rec = {"version": VERSION, "signature": sig, "item": d.target, "per_minute": float(d.per_minute),
           "inputs": sorted(set(inputs)), "plates": sig["plates"], "kit": sig["kit"], "builds": builds + 1,
           "at": list(anchor), "focus": [float(focus[0]) - anchor[0], float(focus[1]) - anchor[1]],
           "ports": [{k: (_rel(v, anchor) if k in ("chest", "inserter", "end", "approach") and v is not None else v)
                      for k, v in pt.items()} for pt in d.ports()],
           "layout": to_record(d, anchor)}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rec, separators=(",", ":")), encoding="utf-8")
    except OSError:
        return None                                  # a full disk never stops the crew
    return path


def load(store: Path | None, item: str) -> list[dict]:
    """Every saved design for `item` (a broken file is skipped)."""
    if store is None:
        return []
    folder = store / _safe(item)
    out = []
    try:
        files = sorted(folder.glob("*.json"))
    except OSError:
        return []
    for f in files:
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if rec.get("version") == VERSION and rec.get("item") == item and rec.get("layout"):
            out.append(rec)
    return out


def why_not(rec: dict, rate: float, inputs, plates_in: bool, ug: bool, feeds) -> str | None:
    """Why a saved design cannot stand in for this goal (None: it can, if today's ground has room)."""
    if sorted(set(inputs)) != rec.get("inputs"):
        return f"its inputs were {rec.get('inputs')}"
    if rec.get("plates") != ("in" if plates_in else "mine"):
        return "plates fed vs its own mines"
    if not rate_ok(rate, float(rec.get("per_minute") or 0)):
        return f"it was made for {rec.get('per_minute')}/min"
    kit = rec.get("kit") or {}
    if kit.get("ug") and not ug:
        return "it needs underground belts"
    if not set(kit.get("feeds") or ()) <= set(feeds):
        return "it takes plates on belts that cannot cross yet"
    if kit.get("asm", ASSEMBLER) != ASSEMBLER:
        return f"it uses {kit.get('asm')}"
    return None


def candidates(store: Path | None, item: str, rate: float, inputs, plates_in: bool, ug: bool, feeds) -> list[dict]:
    """The saved designs that fit this goal's signature: the nearest rate first, then the most built."""
    ok = [r for r in load(store, item) if why_not(r, rate, inputs, plates_in, ug, feeds) is None]
    return sorted(ok, key=lambda r: (abs(math.log(rate / float(r["per_minute"]))), -int(r.get("builds") or 0)))


def _safe(item: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(item))
