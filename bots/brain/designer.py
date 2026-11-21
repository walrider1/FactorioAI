"""AI-designed chiplets: the model draws a module's layout fresh each run, the code checks that it works.

A chiplet is a module's buildings relative to one anchor (for a steam station: the boiler), which the module then
fits to the ground in four rotations (`SteamPower._first_fit`). The hand-made chiplets in `modules.py` are the
library. With `--designs ai` the model designs each chiplet at the start of the run, in the background, and the
library is the fallback; with `--designs ai-only` there is no fallback (to see where the model fails).

A design is never trusted because it fits. `check_station` tests that it works the way the game will run it:
steam reaches every engine through ports that face each other, every engine is inside a pole's supply area and
the poles are wired to each other, the inserter takes from the chest and drops into the boiler, and a water
input stays free for the pipe. Its problems go back to the model, in words, for another try.

    designer = ChipletDesigner(llm, mode="ai")       # llm: anything with .chat(messages) -> str (fab LLMDesigner)
    designer.prefetch(kn, library)                   # design the 1- and 2-engine stations in the background
    tpl = designer.station(kn, 2, library)           # {"parts", "inlet", "clear", "by", ...}
"""

from __future__ import annotations

import json
import math
import re
import time
from concurrent.futures import Future, ThreadPoolExecutor

Tile = tuple[int, int]
DIRS = ("north", "east", "south", "west")
VEC = {"north": (0, -1), "east": (1, 0), "south": (0, 1), "west": (-1, 0)}
POLE, SUPPLY, WIRE = "small-electric-pole", 2.5, 7.5
STATION = {"boiler": 1, "iron-chest": 1, "burner-inserter": 1}   # plus the engines and 1-4 poles
CHAR = {"boiler": "B", "steam-engine": "E", POLE: "P", "iron-chest": "C", "burner-inserter": "I"}
ORIGIN = (0.5, 0.0)            # where a template's boiler (facing north) is put to check it: x on a half tile


class DesignError(Exception):
    pass


# ------------------------------------------------------------------ geometry
def footprint(kn, name: str, x: float, y: float, d: str = "north") -> set[Tile]:
    w, h = kn.size(name)
    if d in ("east", "west"):
        w, h = h, w
    return {(tx, ty) for tx in range(math.floor(x - w / 2 + 0.01), math.floor(x + w / 2 - 0.01) + 1)
            for ty in range(math.floor(y - h / 2 + 0.01), math.floor(y + h / 2 - 0.01) + 1)}


def _placed(parts):
    """Template parts (name, dx, dy, dir) relative to the boiler, put at ORIGIN: (name, x, y, dir)."""
    return [(name, ORIGIN[0] + dx, ORIGIN[1] + dy, d) for name, dx, dy, d in parts]


def supplied(pole_xy: tuple[float, float]) -> set[Tile]:
    px, py = pole_xy
    return {(x, y) for x in range(math.floor(px - SUPPLY), math.ceil(px + SUPPLY))
            for y in range(math.floor(py - SUPPLY), math.ceil(py + SUPPLY))}


# ------------------------------------------------------------------ the check
def check_station(kn, parts, engines: int) -> tuple[list[str], dict]:
    """Problems with a steam-station template (empty: it works), and what the fitter needs: the water inlet (the
    tile the pump's pipe ends on) and the tiles a pipe must stay off (open fluid ports it would join)."""
    probs: list[str] = []
    names = [p[0] for p in parts]
    want = dict(STATION, **{"steam-engine": engines})
    for name, n in want.items():
        if names.count(name) != n:
            probs.append(f"needs exactly {n} {name}, has {names.count(name)}")
    poles = names.count(POLE)
    if not 1 <= poles <= 4:
        probs.append(f"needs 1 to 4 {POLE}, has {poles}")
    extra = sorted(set(names) - set(want) - {POLE})
    if extra:
        probs.append(f"unknown parts {extra}: use only boiler, steam-engine, {POLE}, iron-chest, burner-inserter")
    for name, dx, dy, d in parts:
        if d not in DIRS:
            probs.append(f"{name}: direction must be one of {DIRS}")
    boiler = next((p for p in parts if p[0] == "boiler"), None)
    if boiler and boiler[3] != "north":
        probs.append("the boiler must face north (the station is turned as a whole when it is placed)")
    if probs:
        return probs, {}

    placed = _placed(parts)
    tiles: dict[Tile, int] = {}
    for i, (name, x, y, d) in enumerate(placed):
        for t in footprint(kn, name, x, y, d):
            if t in tiles:
                other = placed[tiles[t]]
                probs.append(f"{name} at {_cr(x, y)} overlaps {other[0]} at {_cr(other[1], other[2])} on tile {t}")
                break
            tiles[t] = i
    if probs:
        return probs, {}

    # fluids: two ports join when each one's outside tile is the other's inside tile (they face each other)
    ports = []
    for i, (name, x, y, d) in enumerate(placed):
        for p in kn.ports(name, x, y, d):
            ports.append(dict(p, part=i))
    by_inside: dict[Tile, list[dict]] = {}
    for p in ports:
        by_inside.setdefault(p["inside"], []).append(p)

    def mate(p):
        return next((q for q in by_inside.get(p["target"], []) if q["target"] == p["inside"] and q["part"] != p["part"]),
                    None)

    b = names.index("boiler")
    water = [p for p in ports if p["part"] == b and p.get("filter") == "water"]
    steam_out = [p for p in ports if p["part"] == b and p.get("filter") == "steam"]
    for p in water:
        q = mate(p)
        if q is not None:
            probs.append(f"the boiler's water input at tile {p['inside']} joins the {placed[q['part']][0]}'s port: "
                         "only the pump's pipe may meet a water input")
    free_water = [p for p in water if p["target"] not in tiles]
    if not free_water:
        probs.append("both of the boiler's water inputs (its two short sides, the tile beside the middle of each) "
                     "are covered: one must stay free for the pump's pipe")
    reached, todo = set(), []
    for p in steam_out:
        q = mate(p)
        if q is None or placed[q["part"]][0] != "steam-engine":
            probs.append(f"the boiler's steam output (tile {p['inside']}, facing tile {p['target']}) does not meet a "
                         "steam engine's port: an engine's end port must sit on that tile, facing the boiler")
        else:
            reached.add(q["part"]), todo.append(q["part"])
    while todo:
        i = todo.pop()
        for p in (p for p in ports if p["part"] == i):
            q = mate(p)
            if q is not None and placed[q["part"]][0] == "steam-engine" and q["part"] not in reached:
                reached.add(q["part"]), todo.append(q["part"])
    for i, (name, x, y, d) in enumerate(placed):
        if name == "steam-engine" and i not in reached:
            probs.append(f"the steam engine at {_cr(x, y)} gets no steam: its end ports must meet the boiler's steam "
                         "output or another engine's end port, facing each other on neighbouring tiles")

    # power: every engine in a pole's 5x5 supply square, every pole wired to the others
    pole_xy = [(x, y) for name, x, y, d in placed if name == POLE]
    for name, x, y, d in placed:
        if name == "steam-engine" and not any(supplied(q) & footprint(kn, name, x, y, d) for q in pole_xy):
            probs.append(f"the steam engine at {_cr(x, y)} is outside every pole's supply area (a small pole "
                         "supplies the 5x5 square around it, 2.5 tiles each way from its centre)")
    seen, todo = {0}, [0]
    while todo:
        i = todo.pop()
        for j, q in enumerate(pole_xy):
            if j not in seen and math.dist(pole_xy[i], q) <= WIRE:
                seen.add(j), todo.append(j)
    if len(seen) < len(pole_xy):
        probs.append(f"the poles are not all within {WIRE} tiles of each other, so they are not wired together")

    # fuel: the inserter faces the side it takes from (the chest) and drops on the opposite side (the boiler)
    i = names.index("burner-inserter")
    _, ix, iy, idir = placed[i]
    it = (math.floor(ix), math.floor(iy))
    vx, vy = VEC[idir]
    pick, drop = (it[0] + vx, it[1] + vy), (it[0] - vx, it[1] - vy)
    if tiles.get(pick) != names.index("iron-chest"):
        probs.append(f"the burner inserter faces {idir}, so it takes from tile {pick}: the iron chest must be there")
    if tiles.get(drop) != b:
        probs.append(f"the burner inserter drops on tile {drop} (opposite the side it faces): the boiler must be there")
    if probs:
        return probs, {}

    inlet = free_water[0]["target"]
    clear = [p["target"] for p in ports if p["target"] != inlet and p["target"] not in tiles and mate(p) is None]
    off = lambda t: (t[0] + 0.5 - ORIGIN[0], t[1] + 0.5 - ORIGIN[1])     # tile -> offset from the boiler
    return [], {"inlet": off(inlet), "clear": [off(t) for t in dict.fromkeys(clear)]}


def _cr(x: float, y: float) -> str:
    return f"({x - ORIGIN[0]:g}, {y - ORIGIN[1]:g})"


def render(kn, parts, info: dict | None = None) -> str:
    """The template as text, one character per tile (B boiler, E engine, P pole, C chest, I inserter, w the water
    inlet, . open), for the model's next try and for the monitor."""
    canvas: dict[Tile, str] = {}
    for name, x, y, d in _placed(parts):
        for t in footprint(kn, name, x, y, d):
            canvas[t] = CHAR.get(name, "?")
    if info and info.get("inlet"):
        ix, iy = info["inlet"]
        canvas.setdefault((math.floor(ORIGIN[0] + ix), math.floor(ORIGIN[1] + iy)), "w")
    if not canvas:
        return ""
    xs, ys = [t[0] for t in canvas], [t[1] for t in canvas]
    return "\n".join("".join(canvas.get((x, y), ".") for x in range(min(xs) - 1, max(xs) + 2))
                     for y in range(min(ys) - 1, max(ys) + 2))


# ------------------------------------------------------------------ the prompt
def _port_table(kn, name: str) -> str:
    """Where an entity's fluid ports are, by direction, as tiles counted from its top-left tile."""
    w, h = kn.size(name)
    rows = []
    for d in DIRS:
        ww, hh = (h, w) if d in ("east", "west") else (w, h)
        ps = kn.ports(name, ww / 2, hh / 2, d)
        side = {(0, -1): "north", (1, 0): "east", (0, 1): "south", (-1, 0): "west"}
        txt = ", ".join(f"{p.get('filter') or 'fluid'} {p.get('production')} at tile {p['inside']} opening "
                        f"{side[(p['target'][0] - p['inside'][0], p['target'][1] - p['inside'][1])]}" for p in ps)
        rows.append(f"    facing {d} ({ww}x{hh} tiles): {txt}")
    return "\n".join(rows)


def station_prompt(kn, engines: int) -> str:
    return f"""You design the steam power station of a Factorio AI, as a chiplet the code then fits to the shore in
four rotations and pipes water to from an offshore pump. Draw it with the boiler facing north.

Parts (each: its top-left tile as column and row on your own grid, and a direction):
- boiler x1, 3x2 when facing north. Fluid ports, tiles counted from its top-left tile:
{_port_table(kn, "boiler")}
- steam-engine x{engines}, 3x5 when facing north or south, 5x3 facing east or west:
{_port_table(kn, "steam-engine")}
- {POLE} x1 to x4, 1x1. Supplies power in the 5x5 square around it (2.5 tiles each way from its centre); poles
  within {WIRE} tiles of each other are wired together.
- iron-chest x1, 1x1: the boiler's coal.
- burner-inserter x1, 1x1. Its direction is the side it takes from; it drops on the opposite side.

It works when:
1. no two parts share a tile;
2. steam flows: the boiler's steam output meets an engine's end port, and each further engine's end port meets
   the previous engine's other end port. Two ports meet when they sit on neighbouring tiles and open toward each
   other;
3. one of the boiler's two water inputs is left with its outside tile free (the pump's pipe ends there), and
   no part's fluid port meets a water input;
4. every engine overlaps the supply square of some pole, and the poles are wired to each other;
5. the burner inserter takes from the chest and drops into the boiler.
Keep it compact.

Reply with JSON only:
{{"note": "one sentence", "parts": [{{"name": "boiler", "col": 0, "row": 0, "dir": "north"}}, ...]}}"""


def parse_parts(kn, text: str) -> tuple[list[tuple[str, float, float, str]], str]:
    """The model's reply -> template parts (name, dx, dy, dir) relative to the boiler's centre."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise DesignError("the reply had no JSON object")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        raise DesignError(f"the JSON did not parse: {exc}") from None
    raw = data.get("parts") or []
    if not isinstance(raw, list) or not raw:
        raise DesignError('the JSON needs a "parts" list')
    centres = []
    for p in raw:
        try:
            name, col, row, d = str(p["name"]), int(p["col"]), int(p["row"]), str(p.get("dir", "north"))
        except (KeyError, TypeError, ValueError):
            raise DesignError(f"each part needs name, col, row and dir: {json.dumps(p)[:80]}") from None
        if name not in kn.entities:
            raise DesignError(f"unknown part {name!r}")
        w, h = kn.size(name)
        if d in ("east", "west"):
            w, h = h, w
        centres.append((name, col + w / 2, row + h / 2, d))
    bx, by = next(((x, y) for n, x, y, d in centres if n == "boiler"), (None, None))
    if bx is None:
        raise DesignError("there is no boiler")
    return [(n, x - bx, y - by, d) for n, x, y, d in centres], str(data.get("note", ""))[:200]


# ------------------------------------------------------------------ the designer
class ChipletDesigner:
    """Designs chiplets with a model, checks them, and falls back to the library (mode "ai") or fails ("ai-only").
    Designs are made once per run, in the background, and kept; `records` is what the monitor shows."""

    def __init__(self, llm, mode: str = "ai", tries: int = 3, log=print):
        assert mode in ("ai", "ai-only")
        self.llm, self.mode, self.tries, self.log = llm, mode, tries, log
        self.records: list[dict] = []
        self._done: dict[str, dict] = {}
        self._jobs: dict[str, Future] = {}
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="designer")

    @property
    def model(self) -> str:
        return getattr(self.llm, "model", "?")

    @property
    def cost_usd(self) -> float:
        return float(getattr(self.llm, "cost_usd", 0.0) or 0.0)

    def prefetch(self, kn, library) -> None:
        """Start designing the 1- and 2-engine stations now, while the crew mines (library(engines) -> template)."""
        for e in (2, 1):
            key = f"steam_power:{e}"
            if key not in self._jobs and key not in self._done:
                self._jobs[key] = self._pool.submit(self._design_station, kn, e, library)

    def station(self, kn, engines: int, library) -> dict:
        key = f"steam_power:{engines}"
        tpl = self._done.get(key)
        if tpl is None:
            job = self._jobs.get(key) or self._pool.submit(self._design_station, kn, engines, library)
            self._jobs[key] = job
            tpl = self._done[key] = job.result()
        if tpl.get("failed"):                 # ai-only: a failed design fails every build that asks for it
            raise DesignError(tpl["failed"])
        return tpl

    def _design_station(self, kn, engines: int, library) -> dict:
        t0, history = time.monotonic(), []
        messages = [{"role": "system", "content": station_prompt(kn, engines)},
                    {"role": "user", "content": f"Design the station with {engines} steam engine(s)."}]
        for attempt in range(1, self.tries + 1):
            try:
                reply = self.llm.chat(messages)
                parts, note = parse_parts(kn, reply)
                probs, info = check_station(kn, parts, engines)
            except DesignError as exc:
                reply, parts, note, probs, info = locals().get("reply", ""), [], "", [str(exc)], {}
            except Exception as exc:  # noqa: BLE001 - a dead model (cost cap, network) ends the attempts
                history.append({"try": attempt, "problems": [f"model call failed: {str(exc)[:120]}"]})
                break
            history.append({"try": attempt, "problems": probs[:6], "drawing": render(kn, parts, info) if parts else ""})
            if not probs:
                tpl = {"parts": parts, "inlet": info["inlet"], "clear": info["clear"], "by": "ai", "note": note}
                return self._record("steam_power", engines, tpl, history, t0, kn)
            messages += [{"role": "assistant", "content": reply or ""},
                         {"role": "user", "content": "It does not work yet:\n- " + "\n- ".join(probs[:8])
                          + ("\nYour layout (B boiler, E engine, P pole, C chest, I inserter):\n" + render(kn, parts)
                             if parts else "") + "\nFix it and reply with the whole JSON again."}]
        why = "; ".join((history[-1]["problems"] if history else ["no reply"])[:2])
        if self.mode == "ai-only":
            return self._record("steam_power", engines, {"failed": f"the AI's station design failed {len(history)} "
                                                          f"tries ({why[:160]})", "by": "none"}, history, t0, kn)
        tpl = dict(library(engines), by="library", note=f"the AI's design failed {len(history)} tries: {why[:160]}")
        return self._record("steam_power", engines, tpl, history, t0, kn)

    def _record(self, module: str, engines: int, tpl: dict, history: list, t0: float, kn) -> dict:
        rec = {"module": module, "variant": f"{engines} engine(s)", "by": tpl.get("by"), "tries": len(history),
               "seconds": round(time.monotonic() - t0, 1), "note": tpl.get("note") or tpl.get("failed", ""),
               "drawing": render(kn, tpl["parts"], tpl) if tpl.get("parts") else "",
               "history": history[-3:], "cost_usd": round(self.cost_usd, 4)}
        self.records.append(rec)
        self.log(f"[designer] {module} {rec['variant']}: {rec['by']} after {rec['tries']} tries, {rec['seconds']} s"
                 + (f" ({rec['note'][:120]})" if rec["note"] else ""))
        return tpl
