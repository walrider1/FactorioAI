"""The LLM floor-plan designer: a model draws where the assemblers go, the engine checks and wires it.

The model sees the area as text (one character per tile, with the mine cells already on it) and the list of
machines with what each one trades. It answers with a top-left tile per machine. A validator returns every
mistake (overlap, water, too close) for another try; the router then wires the plan, and a routing failure
goes back to the model too. The search placer stays the fallback.
"""

from __future__ import annotations

import json
import re

from .cells import Cell
from .grid import Grid
from .place import Net, _asm_gap, dilate

Tile = tuple[int, int]
ORE_CHAR = {"iron-ore": "i", "copper-ore": "c", "coal": "k", "stone": "s"}
NAME = {"iron-gear-wheel": "gear", "copper-cable": "copper cable", "electronic-circuit": "circuit",
        "inserter": "inserter", "transport-belt": "belt", "logistic-science-pack": "green science",
        "automation-science-pack": "red science"}

PROMPT = """You are the floor-plan designer of a Factorio factory. You place assembling machines on a map;
an automatic router then connects them with inserters and belts, and lays power poles.

The map: one character per tile. '.' open ground. 'T' a tree or rock (allowed, it gets cleared, a little slower).
'i' 'c' 'k' 's' iron, copper, coal, stone ore (allowed, but it wastes ore). '~' water, '#' already built,
' ' unexplored: never build there. 'D' an electric drill and 'F'/'f' its iron/copper furnace: the mine cells that
make plates, already placed; never build on them.
Columns are numbered left to right and rows top to bottom, both from 0 (read the two header lines as tens and
units digits).

Rules for every machine (each is 3x3 tiles; you give its top-left tile):
- all 9 tiles on '.', 'T' or ore;
- at least {gap} empty tiles between any two machines, and at least 2 between a machine and a mine cell:
  that is where inserters, belts and poles go;
- every connection listed becomes a belt, and belts that cross need underground pairs (only 10 available),
  so put machines that trade items close together, near the mine cells that feed them, and keep corridors
  open between groups.

Reply with JSON only: {{"note": "one sentence about your layout", "machines": [{{"id": "A1", "col": 0, "row": 0}}, ...]}}
with every machine exactly once."""


def render_map(grid: Grid, box: tuple[int, int, int, int], mines: list[Cell]) -> str:
    x0, y0, x1, y1 = box
    canvas: dict[Tile, str] = {}
    for c in mines:
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


def describe_nets(asms: list[Cell], mines: list[Cell], nets: list[Net], box: tuple[int, int, int, int]) -> str:
    lines = ["Machines to place:"]
    for c in asms:
        lines.append(f"- {c.id}: makes {NAME.get(c.recipe, c.recipe)}")
    lines.append("Connections (each becomes one belt, in this order):")
    kinds = {c.id: (f"mine cell {c.id} ({'iron' if c.ore == 'iron-ore' else 'copper'} furnace)") for c in mines}
    for net in nets:
        src = ", ".join(kinds.get(s, s) for s in net.sources)
        if net.sinks:
            lines.append(f"- {NAME.get(net.item, net.item)}: from {src} to {', '.join(net.sinks)}")
        else:
            lines.append(f"- {NAME.get(net.item, net.item)}: from {src} into a chest next to each (the product)")
    if mines:
        lines.append("Mine cells (map letters D/F/f) at: " + ", ".join(
            f"{c.id} around col {int(c.centre()[0]) - box[0]}, row {int(c.centre()[1]) - box[1]}" for c in mines))
    return "\n".join(lines)


def validate(grid: Grid, placements: dict[str, tuple[int, int]], asms: list[Cell], mines: list[Cell],
             box: tuple[int, int, int, int], gap: int) -> list[str]:
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
            errs.append(f"{k} at col {col}, row {row}: closer than 2 tiles to a mine cell")
    keys = [k for k in placements if k in ids]
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            g = _asm_gap(placements[a], placements[b])
            if g < gap:
                errs.append(f"{a} and {b} are only {max(g, 0)} tiles apart (need {gap})")
    return errs


def _parse(text: str, box: tuple[int, int, int, int]) -> tuple[dict[str, tuple[int, int]], str]:
    m = re.search(r"\{.*\}", text or "", re.S)
    data = json.loads(m.group(0)) if m else {}
    out = {}
    for it in data.get("machines") or []:
        try:
            out[str(it["id"])] = (box[0] + int(it["col"]), box[1] + int(it["row"]))
        except (KeyError, TypeError, ValueError):
            continue
    return out, str(data.get("note", ""))[:200]


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


def llm_place_assemblers(llm, grid: Grid, asms: list[Cell], mines: list[Cell], nets: list[Net],
                         box: tuple[int, int, int, int], gap: int, feedback: list[str] = (), log=print,
                         rounds: int = 3) -> tuple[list[Cell], list[str], list[str]]:
    """Returns (placed cells, problems, notes)."""
    if llm is None:
        return [], ["no LLM designer configured"], []
    notes: list[str] = []
    user = render_map(grid, box, mines) + "\n\n" + describe_nets(asms, mines, nets, box)
    if feedback:
        user += "\n\nYour last layout could not be wired: " + "; ".join(feedback)[:600] + \
                ". Move those machines closer together or open a corridor."
    messages = [{"role": "system", "content": PROMPT.format(gap=gap)}, {"role": "user", "content": user}]
    errs: list[str] = ["no reply"]
    placements: dict[str, tuple[int, int]] = {}
    for k in range(rounds):
        try:
            text = llm.chat(messages)
        except Exception as exc:  # noqa: BLE001 - network, cap, refusal: fall back to the search placer
            return [], [f"designer unavailable: {str(exc)[:120]}"], notes
        try:
            placements, note = _parse(text, box)
        except (json.JSONDecodeError, AttributeError):
            placements, note = {}, ""
        errs = validate(grid, placements, asms, mines, box, gap)
        notes.append(f"round {k + 1}: {note or '(no note)'}; {len(errs)} problem(s)")
        log(f"[designer] round {k + 1}: {note} -> {len(errs)} problem(s)")
        if not errs:
            break
        messages.append({"role": "assistant", "content": text})
        messages.append({"role": "user", "content": "Fix these and send the whole layout again:\n- " +
                         "\n- ".join(errs[:20])})
    if errs:
        return [], [f"designer layout still invalid after {rounds} rounds: {errs[0]}"], notes
    return [c.clone(x=placements[c.id][0], y=placements[c.id][1], placed=True) for c in asms], [], notes
