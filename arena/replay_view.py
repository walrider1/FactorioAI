"""Map-view replay page: turn a run's logs into one self-contained HTML file you can scrub through.

    uv run arena replay-page runs/local/<run_id>          # writes runs/local/<run_id>/replay.html

The headless game has no graphics, so this draws the run from the logs: ore, water and trees the
bot saw, every building as it appears, the character's walk, statuses, the score, and for the
automation level the hands-off window minute by minute. No network, no libraries.
"""

from __future__ import annotations

import json
import math
from pathlib import Path


def _key(x: float, y: float) -> str:
    return f"{x:g},{y:g}"


def build_replay(run_dir: Path) -> dict:
    run_dir = Path(run_dir)
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    result_path = run_dir / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {}
    bot_path = run_dir / "bot.json"
    if bot_path.exists() and "bot" not in result:
        result["bot"] = json.loads(bot_path.read_text(encoding="utf-8"))
    entries = [json.loads(x) for x in (run_dir / "actions.jsonl").read_text(encoding="utf-8").splitlines() if x]

    resources: dict[str, set[tuple[int, int]]] = {}
    water: set[tuple[int, int]] = set()
    obstacles: dict[str, dict] = {}          # key -> {x, y, kind, gone: step index or None}
    steps = []
    spawn = (meta.get("setup") or {}).get("spawn") or {"x": 0, "y": 0}
    pos = [spawn["x"], spawn["y"]]
    for i, e in enumerate(entries):
        a, d = e["action"], e.get("data") or {}
        ev: dict = {"tick": e.get("tick_after") or e.get("tick_before") or 0, "ok": e["ok"], "code": e.get("code"),
                    "type": a["type"], "text": _describe(a, e), "score": e.get("score"),
                    "budgets": {k: (e.get("budgets") or {}).get(k) for k in ("actions_left", "observes_left", "ticks_left")}}
        add, remove, status = [], [], []
        if a["type"] in ("move", "observe") and d.get("position"):
            pos = [d["position"]["x"], d["position"]["y"]]
        if a["type"] == "observe" and e["ok"]:
            for name, tiles in (d.get("resource_xy") or {}).items():
                resources.setdefault(name, set()).update((t[0], t[1]) for t in tiles)
            water.update((math.floor(w[0]), math.floor(w[1])) for w in d.get("water") or [])
            for x, y, kind in d.get("obstacles") or []:
                k = _key(x, y)
                if k not in obstacles:
                    obstacles[k] = {"x": x, "y": y, "kind": kind, "gone": None}
            for ent in d.get("entities") or []:
                status.append(_ent(ent))
        if e["ok"]:
            if a["type"] == "place" and d.get("entity"):
                add.append(_ent(d["entity"]))
            elif a["type"] == "place_line":
                for x, y in d.get("placed") or []:
                    add.append({"k": _key(x, y), "name": a["entity"], "x": x, "y": y,
                                "dir": d.get("direction", "north")})
            elif a["type"] in ("insert", "extract", "rotate", "set_recipe") and d.get("entity"):
                status.append(_ent(d["entity"]))
            elif a["type"] == "pickup":
                if d.get("cleared"):
                    near = min(obstacles.values(), key=lambda o: (o["x"] - a["x"]) ** 2 + (o["y"] - a["y"]) ** 2,
                               default=None)
                    if near and near["gone"] is None:
                        near["gone"] = i
                else:
                    remove.append([a["x"], a["y"]])
        ev.update(pos=pos, add=add, remove=remove, status=status)
        steps.append(ev)

    bd = result.get("breakdown") or {}
    return {
        "run": {"id": meta.get("run_id"), "task": meta.get("task"), "title": (meta.get("task_config") or {}).get("title"),
                "map": meta.get("map"), "team": meta.get("team"), "spawn": spawn,
                "budgets": (meta.get("task_config") or {}).get("budgets")},
        "result": {"status": result.get("status"), "score": result.get("score"), "metric": result.get("metric"),
                   "end_reason": result.get("end_reason"), "tier": bd.get("tier"), "tier_name": bd.get("tier_name"),
                   "points": bd.get("points"), "tiers": bd.get("tiers"), "per_minute": bd.get("per_minute"),
                   "made": bd.get("made"), "bot": result.get("bot") and {
                       k: result["bot"].get(k) for k in ("model", "turns", "stop", "cost_usd", "tool_calls", "bad_calls")},
                   "breakdown": None if bd.get("tiers") else bd},
        "resources": {k: sorted(v) for k, v in resources.items()},
        "water": sorted(water),
        "obstacles": list(obstacles.values()),
        "steps": steps,
    }


def _ent(ent: dict) -> dict:
    return {"k": _key(ent["x"], ent["y"]), "name": ent["name"], "x": ent["x"], "y": ent["y"],
            "dir": ent.get("direction", "north"), "status": ent.get("status"), "recipe": ent.get("recipe")}


def _describe(a: dict, e: dict) -> str:
    t = a["type"]
    if t == "observe":
        return f"observe (radius {a.get('radius', 16)})"
    if t == "move":
        return f"move to ({a['x']:g}, {a['y']:g})"
    if t == "place":
        return f"place {a['entity']} at ({a['x']:g}, {a['y']:g}) facing {a.get('direction', 'north')}"
    if t == "place_line":
        return f"lay {a['entity']} ({a['x1']:g}, {a['y1']:g}) to ({a['x2']:g}, {a['y2']:g})"
    if t in ("insert", "extract"):
        return f"{t} {a['count']} {a['item']} at ({a['x']:g}, {a['y']:g})"
    if t == "advance":
        return f"advance {a['ticks']} ticks ({a['ticks'] / 60:g} s)"
    if t == "craft":
        return f"craft {a.get('count', 1)} {a['recipe']}"
    if t == "set_recipe":
        return f"set recipe {a['recipe']} at ({a['x']:g}, {a['y']:g})"
    return f"{t} at ({a.get('x', 0):g}, {a.get('y', 0):g})"


def write_replay_page(run_dir: Path, out: Path | None = None) -> Path:
    data = build_replay(run_dir)
    out = out or Path(run_dir) / "replay.html"
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    out.write_text(PAGE.replace("/*DATA*/null", blob), encoding="utf-8")
    return out


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Arena Run Replay</title>
<style>
:root {
  --bg: #f6f5f1; --panel: #ffffff; --ink: #1d1d1b; --muted: #6b6a64; --line: #e2e0d8;
  --ground: #ecebe4; --water: #8db8d8; --iron: #8e9aaf; --copper: #d08b5b; --coal: #3d3d3d; --stone: #b9ab8d;
  --tree: #6f9a5c; --rock: #a39f95; --building: #4b5563; --belt: #c9a227; --pipe: #7a8794; --pole: #7c5a3a;
  --assembler: #3d6fb6; --power: #a14d4d; --furnace: #8a6d4f; --drill: #5f6b7a; --chest: #9a7b4f;
  --ok: #2f8f5b; --bad: #c2453d; --warn: #c9861a; --accent: #3d6fb6; --char: #e0533d;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #161614; --panel: #1f1f1c; --ink: #ecebe6; --muted: #9c9a92; --line: #33322e;
    --ground: #24241f; --water: #2f5f80; --iron: #7d8aa0; --copper: #b8744a; --coal: #8a8a8a; --stone: #8f8468;
    --tree: #4f7a42; --rock: #77736b; --building: #9aa3b0; --belt: #c9a227; --pipe: #8b98a6; --pole: #a57a52;
    --assembler: #6b9be0; --power: #d07a7a; --furnace: #b0906e; --drill: #8d99a8; --chest: #b8975f;
    --ok: #4cbf82; --bad: #e26a62; --warn: #e0a640; --accent: #6b9be0; --char: #ff7a5c;
  }
}
:root[data-theme="dark"] {
  --bg: #161614; --panel: #1f1f1c; --ink: #ecebe6; --muted: #9c9a92; --line: #33322e;
  --ground: #24241f; --water: #2f5f80; --iron: #7d8aa0; --copper: #b8744a; --coal: #8a8a8a; --stone: #8f8468;
  --tree: #4f7a42; --rock: #77736b; --building: #9aa3b0; --belt: #c9a227; --pipe: #8b98a6; --pole: #a57a52;
  --assembler: #6b9be0; --power: #d07a7a; --furnace: #b0906e; --drill: #8d99a8; --chest: #b8975f;
  --ok: #4cbf82; --bad: #e26a62; --warn: #e0a640; --accent: #6b9be0; --char: #ff7a5c;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
       font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
header { padding: 14px 16px 6px; }
h1 { font-size: 18px; margin: 0 0 2px; font-weight: 650; }
.sub { color: var(--muted); font-size: 13px; }
main { display: grid; grid-template-columns: minmax(0, 1fr) 320px; gap: 12px; padding: 8px 16px 16px; }
@media (max-width: 860px) { main { grid-template-columns: 1fr; } }
.map { position: relative; background: var(--ground); border: 1px solid var(--line); border-radius: 10px;
       overflow: hidden; height: min(72vh, 760px); min-height: 360px; touch-action: none; }
canvas { display: block; width: 100%; height: 100%; cursor: grab; }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; }
.side { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
.controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 10px; }
button, select { font: inherit; color: var(--ink); background: var(--panel); border: 1px solid var(--line);
                 border-radius: 8px; padding: 6px 12px; cursor: pointer; }
button.primary { background: var(--accent); border-color: var(--accent); color: #fff; min-width: 84px; }
input[type=range] { flex: 1 1 220px; accent-color: var(--accent); }
.stat { display: flex; justify-content: space-between; gap: 8px; padding: 3px 0; border-bottom: 1px dashed var(--line); }
.stat:last-child { border-bottom: 0; }
.stat span:first-child { color: var(--muted); }
.step { font-weight: 600; margin: 2px 0 6px; overflow-wrap: anywhere; }
.badge { display: inline-block; font-size: 12px; padding: 1px 8px; border-radius: 999px; border: 1px solid currentColor; }
.ok { color: var(--ok); } .bad { color: var(--bad); }
.legend { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 4px 10px; font-size: 12px; color: var(--muted); }
.legend i { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 6px; vertical-align: -1px; }
h2 { font-size: 14px; margin: 0 0 8px; }
.bars { display: flex; gap: 4px; align-items: flex-end; height: 90px; margin-top: 6px; }
.bars div { flex: 1; background: var(--accent); border-radius: 3px 3px 0 0; min-height: 2px; position: relative; }
.bars div span { position: absolute; top: -16px; left: 0; right: 0; text-align: center; font-size: 10px; color: var(--muted); }
.axis { display: flex; justify-content: space-between; font-size: 11px; color: var(--muted); margin-top: 3px; }
.tier { padding: 4px 0; }
.hint { font-size: 12px; color: var(--muted); }
</style>
</head>
<body>
<header>
  <h1 id="title">Arena run replay</h1>
  <div class="sub" id="subtitle"></div>
</header>
<main>
  <div>
    <div class="map"><canvas id="cv"></canvas></div>
    <div class="controls">
      <button class="primary" id="play">Play</button>
      <button id="back" aria-label="Previous step">&#9664;</button>
      <button id="fwd" aria-label="Next step">&#9654;</button>
      <input type="range" id="slider" min="0" value="0" aria-label="Step">
      <select id="speed" aria-label="Playback speed">
        <option value="2">2 steps/s</option><option value="5" selected>5 steps/s</option>
        <option value="12">12 steps/s</option><option value="30">30 steps/s</option>
      </select>
      <button id="fit">Fit</button>
    </div>
    <div class="hint">Drag to pan, scroll or pinch to zoom. The world is paused between actions; game time only moves when an action spends ticks.</div>
  </div>
  <div class="side">
    <div class="panel">
      <div class="sub" id="stepno"></div>
      <div class="step" id="steptext"></div>
      <div class="stat"><span>Game time</span><span id="gtime"></span></div>
      <div class="stat"><span>Actions left</span><span id="aleft"></span></div>
      <div class="stat"><span>Ticks left</span><span id="tleft"></span></div>
      <div class="stat"><span>Points so far</span><span id="score"></span></div>
      <div class="stat"><span>Buildings</span><span id="nb"></span></div>
    </div>
    <div class="panel" id="resultpanel"></div>
    <div class="panel">
      <div class="legend" id="legend"></div>
    </div>
  </div>
</main>
<script>
const D = /*DATA*/null;
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const SIZE = { "burner-mining-drill": [2, 2], "electric-mining-drill": [3, 3], "stone-furnace": [2, 2],
  "assembling-machine-1": [3, 3], "boiler": [3, 2], "steam-engine": [3, 5] };
const KIND = name => name.includes("drill") ? "--drill" : name.includes("furnace") ? "--furnace" :
  name.includes("assembling") ? "--assembler" : (name === "boiler" || name === "steam-engine" || name === "offshore-pump") ? "--power" :
  name.includes("belt") ? "--belt" : name === "pipe" ? "--pipe" : name.includes("pole") ? "--pole" :
  name.includes("chest") ? "--chest" : name.includes("inserter") ? "--building" : "--building";
const RES = { "iron-ore": "--iron", "copper-ore": "--copper", "coal": "--coal", "stone": "--stone" };
const DV = { north: [0, -1], east: [1, 0], south: [0, 1], west: [-1, 0] };
const steps = D.steps, N = steps.length;
const $ = id => document.getElementById(id);
const cv = $("cv"), ctx = cv.getContext("2d");
let cur = 0, playing = false, timer = null;
let view = { s: 10, x: 0, y: 0 };

$("title").textContent = (D.run.title || D.run.task || "Run") + " replay";
$("subtitle").textContent = [D.run.map, D.run.id, D.result.bot && D.result.bot.model ? "model " + D.result.bot.model : "scripted bot"]
  .filter(Boolean).join(" · ");
$("slider").max = Math.max(0, N - 1);

function stateAt(i) {
  const ents = new Map(); const trail = [];
  for (let k = 0; k <= i && k < N; k++) {
    const s = steps[k];
    for (const r of s.remove) {
      let best = null, bd = 2.25;
      for (const [key, e] of ents) { const d = (e.x - r[0]) ** 2 + (e.y - r[1]) ** 2; if (d <= bd) { bd = d; best = key; } }
      if (best) ents.delete(best);
    }
    for (const e of s.add) ents.set(e.k, Object.assign({}, ents.get(e.k) || {}, e));
    for (const e of s.status) if (ents.has(e.k)) Object.assign(ents.get(e.k), e);
    const p = s.pos; if (!trail.length || trail[trail.length - 1][0] !== p[0] || trail[trail.length - 1][1] !== p[1]) trail.push(p);
  }
  return { ents, trail };
}

function fit() {
  const pts = [[D.run.spawn.x, D.run.spawn.y]];
  for (const s of steps) { pts.push(s.pos); for (const e of s.add) pts.push([e.x, e.y]); }
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const [x, y] of pts) { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); }
  const pad = 10, w = cv.clientWidth, h = cv.clientHeight;
  view.s = Math.max(2, Math.min(40, Math.min(w / (x1 - x0 + 2 * pad), h / (y1 - y0 + 2 * pad))));
  view.x = (x0 + x1) / 2; view.y = (y0 + y1) / 2;
}

function W(x, y) { return [(x - view.x) * view.s + cv.width / devicePixelRatio / 2, (y - view.y) * view.s + cv.height / devicePixelRatio / 2]; }

function draw() {
  const dpr = devicePixelRatio || 1, w = cv.clientWidth, h = cv.clientHeight;
  if (cv.width !== w * dpr || cv.height !== h * dpr) { cv.width = w * dpr; cv.height = h * dpr; }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = css("--ground"); ctx.fillRect(0, 0, w, h);
  const s = view.s;
  const tileRect = (tx, ty, col) => { const [px, py] = W(tx, ty); ctx.fillStyle = col; ctx.fillRect(px, py, s + 0.5, s + 0.5); };
  ctx.globalAlpha = 0.55;
  for (const [name, tiles] of Object.entries(D.resources)) { const col = css(RES[name] || "--stone"); for (const t of tiles) tileRect(t[0], t[1], col); }
  ctx.globalAlpha = 1;
  const wc = css("--water"); for (const t of D.water) tileRect(t[0], t[1], wc);
  for (const o of D.obstacles) {
    if (o.gone !== null && o.gone <= cur) continue;
    const [px, py] = W(o.x, o.y); ctx.fillStyle = css(o.kind === "tree" ? "--tree" : "--rock");
    ctx.beginPath(); ctx.arc(px, py, Math.max(1.5, s * (o.kind === "tree" ? 0.4 : 1.1)), 0, 7); ctx.fill();
  }
  const st = stateAt(cur);
  const sorted = [...st.ents.values()].sort((a, b) => (SIZE[a.name] ? 1 : 0) - (SIZE[b.name] ? 1 : 0));
  for (const e of sorted) {
    let [ww, hh] = SIZE[e.name] || [1, 1];
    if (e.dir === "east" || e.dir === "west") [ww, hh] = [hh, ww];
    const [px, py] = W(e.x - ww / 2, e.y - hh / 2);
    const col = css(KIND(e.name));
    if (e.name.includes("belt") || e.name === "pipe") {
      ctx.fillStyle = col; const m = e.name === "pipe" ? s * 0.3 : s * 0.12;
      ctx.fillRect(px + m, py + m, ww * s - 2 * m, hh * s - 2 * m);
      if (e.name.includes("belt") && s >= 6) arrow(e.x, e.y, e.dir, css("--panel"));
      continue;
    }
    ctx.fillStyle = col; ctx.globalAlpha = 0.9;
    ctx.fillRect(px + 1, py + 1, ww * s - 2, hh * s - 2); ctx.globalAlpha = 1;
    if (e.status) {
      const good = /working|normal|fully_charged|charging/.test(e.status);
      const bad = /no_power|no_fuel|no_minable|no_ingredients|low_power|disabled|no_recipe|item_ingredient_shortage/.test(e.status);
      ctx.strokeStyle = css(good ? "--ok" : bad ? "--bad" : "--warn"); ctx.lineWidth = Math.max(1, s * 0.12);
      ctx.strokeRect(px + 1, py + 1, ww * s - 2, hh * s - 2);
    }
    if (e.name.includes("inserter") && s >= 6) arrow(e.x, e.y, opposite(e.dir), css("--panel"));
    if (e.recipe && s >= 9) {
      ctx.fillStyle = "#fff"; ctx.font = `${Math.max(8, s * 0.55)}px system-ui`; ctx.textAlign = "center"; ctx.textBaseline = "middle";
      const [cx, cy] = W(e.x, e.y); ctx.fillText(short(e.recipe), cx, cy);
    }
  }
  if (st.trail.length) {
    ctx.strokeStyle = css("--char"); ctx.globalAlpha = 0.35; ctx.lineWidth = 1.5; ctx.beginPath();
    st.trail.slice(-40).forEach((p, k) => { const [px, py] = W(p[0], p[1]); k ? ctx.lineTo(px, py) : ctx.moveTo(px, py); });
    ctx.stroke(); ctx.globalAlpha = 1;
    const p = st.trail[st.trail.length - 1]; const [px, py] = W(p[0], p[1]);
    ctx.fillStyle = css("--char"); ctx.beginPath(); ctx.arc(px, py, Math.max(3, s * 0.45), 0, 7); ctx.fill();
    ctx.strokeStyle = css("--panel"); ctx.lineWidth = 1.5; ctx.stroke();
  }
  panel(st);
}
const opposite = d => ({ north: "south", south: "north", east: "west", west: "east" })[d] || d;
function arrow(x, y, dir, col) {
  const [dx, dy] = DV[dir] || [0, -1], s = view.s, [cx, cy] = W(x, y);
  ctx.strokeStyle = col; ctx.lineWidth = Math.max(1, s * 0.12); ctx.beginPath();
  ctx.moveTo(cx - dx * s * 0.25 - dy * s * 0.2, cy - dy * s * 0.25 + dx * s * 0.2);
  ctx.lineTo(cx + dx * s * 0.25, cy + dy * s * 0.25);
  ctx.lineTo(cx - dx * s * 0.25 + dy * s * 0.2, cy - dy * s * 0.25 - dx * s * 0.2); ctx.stroke();
}
const short = r => ({ "iron-gear-wheel": "gear", "copper-cable": "cable", "electronic-circuit": "circuit",
  "automation-science-pack": "red", "logistic-science-pack": "green", "transport-belt": "belt", "inserter": "ins" })[r] || r;
const fmtTime = t => { const sec = Math.floor(t / 60); return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`; };
const num = v => v === null || v === undefined ? "–" : Number(v).toLocaleString();

function panel(st) {
  const s = steps[cur] || { budgets: {}, text: "no actions" };
  $("stepno").textContent = `Step ${N ? cur + 1 : 0} of ${N}`;
  $("steptext").innerHTML = "";
  $("steptext").append(document.createTextNode(s.text + " "));
  if (s.ok !== undefined) { const b = document.createElement("span"); b.className = "badge " + (s.ok ? "ok" : "bad");
    b.textContent = s.ok ? "ok" : (s.code || "rejected"); $("steptext").append(b); }
  $("gtime").textContent = fmtTime(s.tick || 0);
  $("aleft").textContent = s.budgets.actions_left === null ? "no limit" : num(s.budgets.actions_left);
  $("tleft").textContent = num(s.budgets.ticks_left);
  $("score").textContent = num(s.score);
  $("nb").textContent = num(st.ents.size);
  $("slider").value = cur;
}

function result() {
  const r = D.result, el = $("resultpanel"); el.innerHTML = "";
  const h = document.createElement("h2"); h.textContent = "Result"; el.append(h);
  const row = (a, b) => { const d = document.createElement("div"); d.className = "stat";
    const x = document.createElement("span"); x.textContent = a; const y = document.createElement("span"); y.textContent = b; d.append(x, y); el.append(d); };
  row("Score", num(r.score)); row("Ended by", r.end_reason || r.status || "–");
  if (r.tiers) {
    row("Tier", r.tier ? r.tier_name : "none reached"); row("Window points", num(r.points));
    for (const t of r.tiers) {
      const d = document.createElement("div"); d.className = "tier";
      const mark = document.createElement("span"); mark.className = t.reached ? "ok" : "bad"; mark.textContent = t.reached ? "✓ " : "✗ ";
      d.append(mark, document.createTextNode(`${t.name}: ${t.per_minute}/min (needs ${t.target_per_minute})` +
        (t.chain_short && t.chain_short.length ? `, short on ${t.chain_short.join(", ")}` : "")));
      el.append(d);
    }
    const item = (r.tiers.find(t => t.reached) || r.tiers[0]).item;
    const series = (r.per_minute || {})[item] || [];
    if (series.length) {
      const cap = document.createElement("div"); cap.className = "sub"; cap.style.marginTop = "8px";
      cap.textContent = `${item} made per minute in the hands-off window`; el.append(cap);
      const bars = document.createElement("div"); bars.className = "bars"; const mx = Math.max(1, ...series);
      for (const v of series) { const b = document.createElement("div"); b.style.height = (v / mx * 100) + "%";
        const l = document.createElement("span"); l.textContent = v; b.append(l); bars.append(b); }
      el.append(bars);
      const ax = document.createElement("div"); ax.className = "axis"; ax.innerHTML = "<span>min 1</span><span>min " + series.length + "</span>"; el.append(ax);
    }
  }
  if (r.bot) { row("Model turns", num(r.bot.turns)); row("Model cost", r.bot.cost_usd != null ? "$" + Number(r.bot.cost_usd).toFixed(2) : "–");
    row("Stopped because", r.bot.stop || "–"); }
}

function legend() {
  const items = [["--iron", "iron ore"], ["--copper", "copper ore"], ["--coal", "coal"], ["--water", "water"],
    ["--drill", "drill"], ["--furnace", "furnace"], ["--assembler", "assembler"], ["--power", "pump, boiler, engine"],
    ["--belt", "belt"], ["--pipe", "pipe"], ["--pole", "pole"], ["--building", "inserter"], ["--chest", "chest"], ["--char", "character"],
    ["--ok", "outline: working"], ["--bad", "outline: stuck"]];
  for (const [c, t] of items) { const d = document.createElement("div"); const i = document.createElement("i"); i.style.background = css(c); d.append(i, document.createTextNode(t)); $("legend").append(d); }
}

function go(i) { cur = Math.max(0, Math.min(N - 1, i)); draw(); }
function tick() { if (cur >= N - 1) { stop(); return; } go(cur + 1); }
function start() { playing = true; $("play").textContent = "Pause"; timer = setInterval(tick, 1000 / Number($("speed").value)); }
function stop() { playing = false; $("play").textContent = "Play"; clearInterval(timer); }
$("play").onclick = () => { if (playing) stop(); else { if (cur >= N - 1) cur = 0; start(); } };
$("speed").onchange = () => { if (playing) { stop(); start(); } };
$("back").onclick = () => { stop(); go(cur - 1); };
$("fwd").onclick = () => { stop(); go(cur + 1); };
$("slider").oninput = e => { stop(); go(Number(e.target.value)); };
$("fit").onclick = () => { fit(); draw(); };
let drag = null;
cv.addEventListener("pointerdown", e => { drag = [e.clientX, e.clientY]; cv.setPointerCapture(e.pointerId); cv.style.cursor = "grabbing"; });
cv.addEventListener("pointermove", e => { if (!drag) return; view.x -= (e.clientX - drag[0]) / view.s; view.y -= (e.clientY - drag[1]) / view.s; drag = [e.clientX, e.clientY]; draw(); });
cv.addEventListener("pointerup", () => { drag = null; cv.style.cursor = "grab"; });
cv.addEventListener("wheel", e => { e.preventDefault(); const f = e.deltaY < 0 ? 1.15 : 1 / 1.15; view.s = Math.max(1.5, Math.min(60, view.s * f)); draw(); }, { passive: false });
addEventListener("resize", draw);
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", draw);
legend(); result(); fit(); go(0);
</script>
</body>
</html>
"""
