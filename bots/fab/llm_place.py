"""The LLM floor-plan designer: a model draws where the assemblers go, the engine checks and wires it.

The model sees the area as text (one character per tile, with the mine and input cells already on it) and the
list of machines with what each one trades. Where the area holds enough of them, the map also shows a lattice of
machine slots (fab/slots.py): rows of 3x3 slots with belt corridors between the rows, so whatever the model
picks leaves the router room. It answers with three different layouts in one reply, each machine in a slot
(or, without slots, at a top-left tile). A validator returns every mistake for another try; the router then
wires each valid layout (design.py), and when one routes all but a few belts the model gets those failures
back, in its own terms, and moves only the machines involved (LayoutSession.repair). The search placer stays
the fallback.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .cells import Cell
from .grid import Grid
from .place import Net, _asm_gap, dilate
from .slots import Lattice, lattice

Tile = tuple[int, int]
ORE_CHAR = {"iron-ore": "i", "copper-ore": "c", "coal": "k", "stone": "s"}
NAME = {"iron-gear-wheel": "gear", "copper-cable": "copper cable", "electronic-circuit": "circuit",
        "inserter": "inserter", "transport-belt": "belt", "logistic-science-pack": "green science",
        "automation-science-pack": "red science", "iron-plate": "iron plate", "copper-plate": "copper plate"}
LAYOUTS = 3                       # layouts asked for in one reply: three chances for the price of one call
MAX_MOVES = 3                     # machines one repair may move

MAP_LEGEND = """The map: one character per tile. '.' open ground. 'T' a tree or rock (allowed, it gets cleared, a little
slower). 'i' 'c' 'k' 's' iron, copper, coal, stone ore (allowed, but it wastes ore). '~' water, '#' already built,
' ' unexplored: never build there. 'D' an electric drill and 'F'/'f' its iron/copper furnace: the mine cells that
make plates. 'S' an input chest and ':' its port: plates (or other inputs) arrive there by belt from outside; a
lone ':' pair without 'S' is a belt input that runs straight onto this factory's belt. Mine and input cells are
already placed; never build on them.
Columns are numbered left to right and rows top to bottom, both from 0 (read the two header lines as tens and
units digits)."""

PROMPT = """You are the floor-plan designer of a Factorio factory. You place assembling machines on a map;
an automatic router then connects them with inserters and belts, and lays power poles.

""" + MAP_LEGEND + """

Rules for every machine (each is 3x3 tiles; you give its top-left tile):
- all 9 tiles on '.', 'T' or ore;
- at least {gap} empty tiles between any two machines, and at least 2 between a machine and a mine or input
  cell: that is where inserters, belts and poles go;
- every connection listed becomes a belt, and belts that cross need underground pairs (only {ug} available),
  so put machines that trade items close together, near the cells that feed them, and keep corridors
  open between groups.

Give {n} different layouts (different groupings or a different part of the area), your best first.
Reply with JSON only: {{"layouts": [{{"note": "one sentence", "machines": [{{"id": "A1", "col": 0, "row": 0}}, ...]}},
...]}} with every machine exactly once in each layout."""

SLOT_PROMPT = """You are the floor-plan designer of a Factorio factory. You choose where assembling machines go;
an automatic router then connects them with inserters and belts, and lays power poles.

""" + MAP_LEGEND + """
Machine slots: each block of '=' on the map is a 3x3 slot a machine can take, its id written in its top row
(P1, P2, ...). Slots stand in rows. Between two rows runs a 4-tile corridor (inserter row, two belt lanes,
inserter row); neighbouring slots in a row keep a 2-3 tile gap where belts cross from one corridor to the next
and poles stand. Machines only ever go in slots, so the corridors stay open whatever you choose.

Rules:
- each machine in its own slot, at most one machine per slot;
- every connection listed becomes a belt, and belts that cross need underground pairs (only {ug} available):
  put machines that trade items in nearby slots (side by side in a row, or facing each other across one
  corridor), near the cells that feed them; a free slot between two busy groups gives belts room to turn.

Give {n} different layouts (different groupings or a different part of the area), your best first.
Reply with JSON only: {{"layouts": [{{"note": "one sentence", "machines": [{{"id": "A1", "slot": "P3"}}, ...]}},
...]}} with every machine exactly once in each layout."""


def render_map(grid: Grid, box: tuple[int, int, int, int], mines: list[Cell], lat: Lattice | None = None) -> str:
    """The box as text. mines: every fixed cell (mine cells, input cells). lat: slots drawn as '=' blocks."""
    x0, y0, x1, y1 = box
    canvas: dict[Tile, str] = {}
    if lat is not None:
        for s in lat.slots:
            label = (s.id + "===")[:3]
            for i in range(3):
                for j in range(3):
                    canvas[(s.x + i, s.y + j)] = label[i] if j == 0 else "="
    for c in mines:
        if c.kind in ("src", "feed"):
            pt = c.port()
            for t in c.footprint():
                canvas[t] = ":"
            if pt.get("chest") is not None:
                canvas[tuple(pt["chest"])] = "S"
            canvas[tuple(pt["end"])] = ":"
            continue
        for t in c.drill_tiles():
            canvas[t] = "D"
        for t in c.footprint() - c.drill_tiles():
            canvas[t] = "F" if c.ore == "iron-ore" else "f"
    rows = []
    width = x1 - x0 + 1
    rows.append("     " + "".join(str((i // 10) % 10) for i in range(width)))
    rows.append("     " + "".join(str(i % 10) for i in range(width)))
    for j, y in enumerate(range(y0, y1 + 1)):
        line = []
        for x in range(x0, x1 + 1):
            t = (x, y)
            if t in canvas:
                ch = canvas[t]
            elif t not in grid.seen:
                ch = " "
            elif t in grid.water:
                ch = "~"
            elif t in grid.cliffs or t in grid.used or t in grid.keepout:
                ch = "#"
            elif t in grid.ore:
                ch = ORE_CHAR.get(grid.ore[t], "o")
            elif t in grid.obstacles:
                ch = "T"
            else:
                ch = "."
            line.append(ch)
        rows.append(f"{j:4d} " + "".join(line))
    return "\n".join(rows)


def render_wiring(grid: Grid, box: tuple[int, int, int, int], fixed: list[Cell], lat: Lattice | None, d) -> str:
    """The map with a routed design on it: machines labelled by id, belts, inserters and chests (for a repair)."""
    arrow = {"north": "^", "east": ">", "south": "v", "west": "<"}
    taken = {(c.x, c.y) for c in d.cells if c.kind == "asm"}
    free = None
    if lat is not None:
        free = Lattice(lat.orient, lat.pitch, lat.origin, lat.box, [s for s in lat.slots if (s.x, s.y) not in taken])
    lines = render_map(grid, box, fixed, free).split("\n")
    x0, y0 = box[0], box[1]
    grid_rows = [list(r) for r in lines[2:]]

    def put(t, ch):
        j, i = t[1] - y0, t[0] - x0
        if 0 <= j < len(grid_rows) and 0 <= i + 5 < len(grid_rows[j]):
            grid_rows[j][i + 5] = ch
    for c in d.cells:
        if c.kind != "asm":
            continue
        label = (c.id + "###")[:3]
        for i in range(3):
            for j in range(3):
                put((c.x + i, c.y + j), label[i] if j == 1 else "#")
    for r in d.routes:
        for t in r.path:
            k = r.kinds[t]
            put(t, arrow[r.facing[t]] if k == "belt" else ("U" if k == "ug_in" else "u"))
        for t in r.chests:
            put(t, "C")
        for p in r.pins:
            if p.cell in {c.id for c in d.cells if c.kind == "feed"}:
                continue
            put(p.site.inserter, "I")
    return "\n".join(lines[:2] + ["".join(r) for r in grid_rows])


def _cell_label(c: Cell) -> str:
    if c.kind == "mine":
        return f"mine cell {c.id} ({'iron' if c.ore == 'iron-ore' else 'copper'} furnace)"
    if c.kind in ("src", "feed"):
        return f"input {c.id} ({NAME.get(c.product, c.product)} arriving by belt)"
    return c.id


def describe_nets(asms: list[Cell], mines: list[Cell], nets: list[Net], box: tuple[int, int, int, int]) -> str:
    lines = ["Machines to place:"]
    for c in asms:
        lines.append(f"- {c.id}: makes {NAME.get(c.recipe, c.recipe)}")
    lines.append("Connections (each becomes one belt, in this order):")
    kinds = {c.id: _cell_label(c) for c in mines}
    for net in nets:
        src = ", ".join(kinds.get(s, s) for s in net.sources)
        if net.sinks:
            lines.append(f"- {NAME.get(net.item, net.item)}: from {src} to {', '.join(net.sinks)}")
        else:
            lines.append(f"- {NAME.get(net.item, net.item)}: from {src} into a chest next to each (the product)")
    own = [c for c in mines if c.kind == "mine"]
    ins = [c for c in mines if c.kind in ("src", "feed")]
    if own:
        lines.append("Mine cells (map letters D/F/f) at: " + ", ".join(
            f"{c.id} around col {int(c.centre()[0]) - box[0]}, row {int(c.centre()[1]) - box[1]}" for c in own))
    if ins:
        lines.append("Input cells (map letters S and :) at: " + ", ".join(
            f"{c.id} ({NAME.get(c.product, c.product)}) around col {int(c.centre()[0]) - box[0]}, "
            f"row {int(c.centre()[1]) - box[1]}" for c in ins))
    return "\n".join(lines)


def validate(grid: Grid, placements: dict[str, tuple[int, int]], asms: list[Cell], mines: list[Cell],
             box: tuple[int, int, int, int], gap: int, slotted: set[str] = frozenset(),
             slot_of: dict[str, str] | None = None) -> list[str]:
    """Every mistake in a layout. slotted: machines placed in lattice slots, which need no gap check between
    each other (the lattice keeps their corridors), only one machine per slot."""
    errs = []
    x0, y0, x1, y1 = box
    forbidden = set()
    for c in mines:
        forbidden |= dilate(c.footprint(), 2)
    ids = {c.id for c in asms}
    missing = ids - set(placements)
    if missing:
        errs.append(f"missing machines: {', '.join(sorted(missing))}")
    for k in placements:
        if k not in ids:
            errs.append(f"{k} is not one of the machines")
    for k, (x, y) in placements.items():
        if k not in ids:
            continue
        col, row = x - x0, y - y0
        fp = [(x + i, y + j) for i in range(3) for j in range(3)]
        if x < x0 or y < y0 or x + 2 > x1 or y + 2 > y1:
            errs.append(f"{k} at col {col}, row {row}: outside the map")
            continue
        bad = [t for t in fp if not grid.free(t)]
        if bad:
            errs.append(f"{k} at col {col}, row {row}: covers water, a building or unexplored ground at "
                        f"col {bad[0][0] - x0}, row {bad[0][1] - y0}")
        elif any(t in forbidden for t in fp):
            errs.append(f"{k} at col {col}, row {row}: closer than 2 tiles to a mine or input cell")
    keys = [k for k in placements if k in ids]
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            if a in slotted and b in slotted:
                if placements[a] == placements[b]:
                    where = f"slot {slot_of[a]}" if slot_of and a in slot_of else "the same place"
                    errs.append(f"{a} and {b} are both in {where}")
                continue
            g = _asm_gap(placements[a], placements[b])
            if g < gap:
                errs.append(f"{a} and {b} are only {max(g, 0)} tiles apart (need {gap})")
    return errs


@dataclass
class Layout:
    """One floor plan from the model: machine id -> top-left tile, and the slot each one took (if any)."""
    pos: dict[str, tuple[int, int]]
    slot_of: dict[str, str]
    note: str = ""
    index: int = 1                      # its place in the reply (1-based)
    errors: tuple = ()

    def cells(self, asms: list[Cell]) -> list[Cell]:
        return [c.clone(x=self.pos[c.id][0], y=self.pos[c.id][1], placed=True) for c in asms]

    def key(self) -> tuple:
        return tuple(sorted(self.pos.items()))


def _json(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _entries(items, box, lat: Lattice | None) -> tuple[dict, dict, list[str]]:
    """Machine entries -> (positions, slot ids, parse errors). An entry names a slot or a col/row."""
    slots = lat.by_id() if lat is not None else {}
    pos: dict[str, tuple[int, int]] = {}
    slot_of: dict[str, str] = {}
    errs: list[str] = []
    for it in items or []:
        if not isinstance(it, dict) or "id" not in it:
            continue
        k = str(it["id"])
        if k in pos:
            errs.append(f"{k} is listed twice")
            continue
        if it.get("slot") is not None:
            sid = str(it["slot"]).strip().upper()
            if sid.isdigit():
                sid = "P" + sid
            if sid not in slots:
                errs.append(f"{k}: there is no slot {it['slot']}")
                continue
            pos[k], slot_of[k] = (slots[sid].x, slots[sid].y), sid
            continue
        try:
            pos[k] = (box[0] + int(it["col"]), box[1] + int(it["row"]))
        except (KeyError, TypeError, ValueError):
            errs.append(f"{k}: give a slot" if lat is not None else f"{k}: give col and row")
    return pos, slot_of, errs


def _parse(text: str, box: tuple[int, int, int, int]) -> tuple[dict[str, tuple[int, int]], str]:
    """The first layout of a reply as {id: top-left}, and its note (kept for callers of the old API)."""
    lays = parse_layouts(text, box, None)
    return (lays[0].pos, lays[0].note) if lays else ({}, "")


def parse_layouts(text: str, box: tuple[int, int, int, int], lat: Lattice | None) -> list[Layout]:
    """Every layout in a reply: {"layouts": [...]} or a single {"machines": [...]}."""
    data = _json(text)
    raw = data.get("layouts") if isinstance(data.get("layouts"), list) else [data] if data else []
    out = []
    for i, lay in enumerate(raw, start=1):
        if not isinstance(lay, dict):
            continue
        pos, slot_of, errs = _entries(lay.get("machines"), box, lat)
        out.append(Layout(pos, slot_of, str(lay.get("note", ""))[:200], i, tuple(errs)))
    return out


# a router failure: "<item>: <what> <cell ids>"
_FAIL = [(re.compile(r"^(?P<item>[^:]+): no belt route from (?P<a>\S+) to (?P<b>\S+)$"), "route"),
         (re.compile(r"^(?P<item>[^:]+): no free pin on (?P<a>\S+)$"), "pin"),
         (re.compile(r"^(?P<item>[^:]+): no room for an? output chest at (?P<a>\S+)$"), "pin"),
         (re.compile(r"^(?P<item>[^:]+): underground pairs overlap near (?P<a>\S+)$"), "ug"),
         (re.compile(r"^(?P<item>[^:]+): chain crossed its own inserter near (?P<a>\S+)$"), "cross")]


def explain_failures(fails: list[str], lay: Layout, asm_ids: set[str]) -> tuple[list[str], list[str]]:
    """Router failure strings -> (lines in the model's terms, machine ids involved, in order)."""
    lines, involved = [], []

    def who(cid: str) -> str:
        if cid in lay.slot_of:
            return f"{cid} (slot {lay.slot_of[cid]})"
        if cid in asm_ids:
            return cid
        return f"{cid} (an input or mine cell, fixed)" if cid != "__out__" else "the output chest"

    for f in fails:
        for rx, kind in _FAIL:
            m = rx.match(f.strip())
            if not m:
                continue
            item = NAME.get(m["item"], m["item"])
            ids = [m[g] for g in ("a", "b") if g in rx.groupindex and m[g]]
            if kind == "route":
                lines.append(f"{item}: no belt route from {who(ids[0])} to {who(ids[1])}")
            elif kind == "pin":
                lines.append(f"{item}: {who(ids[0])} has no free side left for its {item} inserter and belt "
                             "(boxed in by belts or neighbours)")
            elif kind == "ug":
                lines.append(f"{item}: too many belts cross near {who(ids[0])} (underground pairs collide)")
            else:
                lines.append(f"{item}: its belt had to run over its own inserter near {who(ids[0])}")
            involved += [i for i in ids if i in asm_ids and i not in involved]
            break
        else:
            lines.append(f.strip())
    return lines, involved


class LLMDesigner:
    """Wraps an OpenAI-compatible chat client, with a cost cap.

    reasoning_effort: let a reasoning model think before it draws (OpenAI Responses API). Floor plans are
    spatial puzzles, so a stronger model with some reasoning places machines that route far more often."""

    def __init__(self, client, model: str, max_cost_usd: float = 1.0, reasoning_effort: str | None = None):
        import openai_bot
        self.model, self.max_cost, self.effort = model, max_cost_usd, reasoning_effort
        self.client = openai_bot.ResponsesAdapter(client, reasoning_effort) if reasoning_effort else client
        self._prices = openai_bot
        self.cost_usd = 0.0
        self.calls = 0

    def chat(self, messages: list[dict]) -> str:
        if self.cost_usd >= self.max_cost:
            raise RuntimeError("designer cost cap reached")
        if self.effort:                       # the adapter: no response_format, the prompt asks for JSON only
            resp = self.client.chat.completions.create(model=self.model, messages=messages)
        else:
            resp = self.client.chat.completions.create(model=self.model, messages=messages,
                                                       response_format={"type": "json_object"})
        self.calls += 1
        u = resp.usage
        if u is not None:
            cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
            self.cost_usd += self._prices.cost_usd(self.model, u.prompt_tokens, cached, u.completion_tokens) or 0.0
        return resp.choices[0].message.content or "{}"


class LayoutSession:
    """One conversation with the model about one floor: propose() asks for LAYOUTS layouts (re-asking with the
    validator's errors while none is valid), repair() reports a layout's failed belts and takes its moves.

    The slot lattice is used when the box holds a slot for every machine; otherwise free placement (col/row)."""

    def __init__(self, llm, grid: Grid, asms: list[Cell], fixed: list[Cell], nets: list[Net],
                 box: tuple[int, int, int, int], gap: int, feedback: list[str] = (), log=print,
                 max_ug_pairs: int = 10, layouts: int = LAYOUTS, rounds: int = 3, slots: bool = True):
        self.llm, self.grid, self.asms, self.fixed, self.nets = llm, grid, asms, fixed, nets
        self.box, self.gap, self.log, self.rounds, self.n = box, gap, log, rounds, layouts
        self.ids = {c.id for c in asms}
        self.lat = lattice(grid, box, fixed, len(asms)) if slots and asms else None
        self.notes: list[str] = []
        self.replied = 0                          # layouts in the last reply, valid or not
        self.unavailable: str | None = None
        self.pending: list[str] = []              # a repair's mistakes, told on the next repair
        self.tried: list[str] = []                # repairs that routed no better, so they are not asked twice
        user = render_map(grid, box, fixed, self.lat) + "\n\n" + describe_nets(asms, fixed, nets, box)
        if self.lat is not None:
            user += "\n\nSlots (top-left col/row on the map):\n" + self.lat.describe(box)
            system = SLOT_PROMPT.format(ug=max_ug_pairs, n=layouts)
        else:
            system = PROMPT.format(gap=gap, ug=max_ug_pairs, n=layouts)
        if feedback:
            user += "\n\nAn earlier layout could not be wired: " + "; ".join(feedback)[:600] + \
                    ". Move those machines closer together or open a corridor."
        self.messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        self.mode = "slots" if self.lat is not None else "free"

    # ---------------------------------------------------------------- helpers
    def _ask(self) -> str | None:
        try:
            text = self.llm.chat(self.messages)
        except Exception as exc:  # noqa: BLE001 - network, cap, refusal: the caller falls back
            self.unavailable = f"designer unavailable: {str(exc)[:120]}"
            return None
        self.messages.append({"role": "assistant", "content": text})
        return text

    def _check(self, lay: Layout) -> list[str]:
        return list(lay.errors) + validate(self.grid, lay.pos, self.asms, self.fixed, self.box, self.gap,
                                           set(lay.slot_of), lay.slot_of)

    def _where(self, lay: Layout, k: str) -> str:
        if k in lay.slot_of:
            return lay.slot_of[k]
        x, y = lay.pos[k]
        return f"col {x - self.box[0]}, row {y - self.box[1]}"

    def record(self, moved: list[str], lay: Layout, problems: list[str]) -> None:
        """A repair that wired no better than the layout it started from: told on the next repair."""
        what = ", ".join(f"{k} to {self._where(lay, k)}" for k in moved) or "nothing"
        self.tried.append(f"moving {what} left {len(problems)} problem(s)"
                          + (f" ({problems[0][:120]})" if problems else ""))

    def free_slots(self, lay: Layout) -> list[str]:
        if self.lat is None:
            return []
        taken = set(lay.pos.values())
        return [s.id for s in self.lat.slots if (s.x, s.y) not in taken]

    # ---------------------------------------------------------------- the first ask
    def propose(self) -> tuple[list[Layout], list[str]]:
        """(valid layouts, best first and without repeats; problems when there is none)."""
        self.unavailable = None
        if self.llm is None:
            return [], ["no LLM designer configured"]
        errs: list[str] = ["no reply"]
        for k in range(self.rounds):
            text = self._ask()
            if text is None:
                return [], [self.unavailable]
            lays = parse_layouts(text, self.box, self.lat)
            self.replied = len(lays)
            good, bad, seen = [], [], set()
            for lay in lays:
                e = self._check(lay)
                if e:
                    bad.append((lay, e))
                elif lay.key() not in seen:
                    seen.add(lay.key())
                    good.append(lay)
            self.notes.append(f"round {k + 1} ({self.mode}): {len(lays)} layout(s), {len(good)} valid"
                              + (f"; layout 1: {lays[0].note}" if lays and lays[0].note else ""))
            self.log(f"[designer] round {k + 1}: {len(lays)} layouts, {len(good)} valid")
            if good:
                return good, []
            if not lays:
                errs = ["no layout in the reply"]
                fix = "No layout found in your reply. Reply with JSON only, as asked."
            else:
                errs = bad[0][1]
                fix = "None of your layouts can be used. Fix these and send the layouts again:\n" + "\n".join(
                    f"Layout {lay.index}:\n- " + "\n- ".join(e[:8]) for lay, e in bad[:self.n])
            self.messages.append({"role": "user", "content": fix})
        return [], [f"designer layout still invalid after {self.rounds} rounds: {errs[0]}"]

    # ---------------------------------------------------------------- repair
    def repair(self, lay: Layout, fails: list[str], routed: int, total: int,
               wired=None) -> tuple[Layout | None, list[str]]:
        """Tell the model which belts of `lay` failed and let it move up to MAX_MOVES machines; every other one
        stays. wired: the routed Design, drawn for the model (where the belts that did route run).
        (the new layout, or None; the machines it moved)."""
        lines, involved = explain_failures(fails, lay, self.ids)
        current = ", ".join(f"{k} {self._where(lay, k)}" for k in sorted(lay.pos, key=lambda s: (len(s), s)))
        msg = []
        if self.pending:
            msg.append("Your last moves could not be used: " + "; ".join(self.pending[:6]) + ".")
            self.pending = []
        if self.tried:
            msg.append("Already tried, it did not help (do not repeat it): " + "; ".join(self.tried[-3:]) + ".")
        msg.append(f"Layout {lay.index} was wired best: {routed} of {total} connections routed. These failed:\n- "
                   + "\n- ".join(lines[:8]))
        if involved:
            msg.append("Machines involved: " + ", ".join(involved) + ".")
        msg.append("Current layout: " + current + ".")
        if wired is not None:
            msg.append("The layout as wired so far (machines by id, ^>v< belts, U/u underground in/out, I inserter, "
                       "C chest, = a free slot):\n" + render_wiring(self.grid, self.box, self.fixed, self.lat, wired))
        if self.lat is not None:
            msg.append("Free slots: " + ", ".join(self.free_slots(lay)) + ".")
            how = '{"note": "one sentence", "moves": [{"id": "A5", "slot": "P11"}]}'
            where = "to free slots (or swap two of them)"
        else:
            how = '{"note": "one sentence", "moves": [{"id": "A5", "col": 10, "row": 4}]}'
            where = f"to open ground (keeping the {self.gap}-tile gaps)"
        msg.append(f"Move at most {MAX_MOVES} machines, the ones involved or one standing in their way, {where}, "
                   "so those belts find a way; every other machine stays where it is. Reply with JSON only: " + how)
        self.messages.append({"role": "user", "content": "\n".join(msg)})
        self.unavailable = None
        text = self._ask()
        if text is None:
            self.notes.append(f"repair: {self.unavailable}")
            return None, []
        data = _json(text)
        moves = data.get("moves")
        if not isinstance(moves, list):                    # a whole layout instead: take what changed
            lays = parse_layouts(text, self.box, self.lat)
            moves = None
            if lays:
                new = lays[0]
                moved = [k for k in new.pos if lay.pos.get(k) != new.pos[k]]
                errs = self._check(new)
                if errs:
                    self.pending = errs
                    return None, moved
                return Layout(new.pos, new.slot_of, new.note, lay.index), moved
            self.pending = ["no moves in the reply"]
            return None, []
        pos, slot_of, errs = _entries(moves, self.box, self.lat)
        unknown = [k for k in pos if k not in self.ids]
        errs += [f"{k} is not one of the machines" for k in unknown]
        moved = [k for k in pos if k in self.ids and lay.pos.get(k) != pos[k]][:MAX_MOVES]
        new_pos, new_slots = dict(lay.pos), dict(lay.slot_of)
        for k in moved:
            new_pos[k] = pos[k]
            if k in slot_of:
                new_slots[k] = slot_of[k]
            else:
                new_slots.pop(k, None)
        new = Layout(new_pos, new_slots, str(data.get("note", ""))[:200], lay.index)
        errs += self._check(new)
        if errs:
            self.pending = errs
            return None, moved
        return new, moved


def llm_place_assemblers(llm, grid: Grid, asms: list[Cell], mines: list[Cell], nets: list[Net],
                         box: tuple[int, int, int, int], gap: int, feedback: list[str] = (), log=print,
                         rounds: int = 3) -> tuple[list[Cell], list[str], list[str]]:
    """The first valid layout only: (placed cells, problems, notes). design.py uses LayoutSession directly."""
    s = LayoutSession(llm, grid, asms, mines, nets, box, gap, feedback, log, rounds=rounds)
    lays, probs = s.propose()
    if probs:
        return [], probs, s.notes
    return lays[0].cells(asms), [], s.notes
