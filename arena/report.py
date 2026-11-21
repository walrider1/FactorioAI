"""Leaderboard and run report (HTML + Markdown) built from runs/*/result.json."""

from __future__ import annotations

import html
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings


def _load_runs(runs_dir: Path) -> list[dict]:
    rows = []
    if not runs_dir.exists():
        return rows
    for d in sorted(runs_dir.iterdir()):
        r, m = d / "result.json", d / "meta.json"
        if not (d.is_dir() and r.exists()):
            continue
        res = json.loads(r.read_text(encoding="utf-8"))
        meta = json.loads(m.read_text(encoding="utf-8")) if m.exists() else {}
        series = []
        a = d / "actions.jsonl"
        if a.exists():
            for line in a.read_text(encoding="utf-8").splitlines():
                if line:
                    e = json.loads(line)
                    if e.get("score") is not None:
                        series.append((e.get("tick_after") or e.get("tick_before") or 0, e["score"]))
        if res.get("idle_tail") and res.get("score") is not None:
            series.append((res["idle_tail"]["tick_after"], res["score"]))
        res["_series"] = series
        res["_backend"] = meta.get("backend", {}).get("backend")
        rows.append(res)
    return rows


def leaderboard(settings: Settings, rows: list[dict]) -> dict[str, list[dict]]:
    seeds = settings.load_eval_seeds()
    boards: dict[str, list[dict]] = {}
    for task_id in settings.tasks:
        n_maps = len(seeds.get(task_id, [])) or 1
        per_team: dict[str, dict[str, dict]] = defaultdict(dict)
        for r in rows:
            if r["task"] == task_id and r["mode"] == "eval" and r["status"] == "finished":
                per_team[r["team"]][r["map"]] = r
        board = []
        for team, maps in per_team.items():
            scores = [m["score"] for m in maps.values()]
            board.append({
                "team": team,
                "mean_score": round(sum(scores) / n_maps, 1),      # unplayed maps count as 0
                "maps_played": f"{len(maps)}/{n_maps}",
                "mean_actions": round(sum(m["budgets_used"]["actions"] for m in maps.values()) / len(maps), 1),
                "mean_ticks": round(sum(m["budgets_used"]["ticks"] for m in maps.values()) / len(maps)),
                "per_map": {k: v["score"] for k, v in sorted(maps.items())},
            })
        if settings.tasks[task_id].score.window_at_build_end:
            # everyone is scored at the same game time, so build time left over is no tie-break
            board.sort(key=lambda b: (-b["mean_score"], b["mean_actions"], b["team"]))
        else:
            board.sort(key=lambda b: (-b["mean_score"], b["mean_actions"], b["mean_ticks"], b["team"]))
        for i, b in enumerate(board, 1):
            b["rank"] = i
        boards[task_id] = board
    return boards


def _spark(series: list[tuple[int, int]], w: int = 160, h: int = 32) -> str:
    if len(series) < 2:
        return ""
    xmax = max(t for t, _ in series) or 1
    ymax = max(s for _, s in series) or 1
    pts = " ".join(f"{t / xmax * w:.1f},{h - s / ymax * (h - 2) - 1:.1f}" for t, s in series)
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" aria-label="score over time">'
            f'<polyline fill="none" stroke="currentColor" stroke-width="1.5" points="{pts}"/></svg>')


def build_report(settings: Settings) -> tuple[Path, Path]:
    rows = _load_runs(settings.runs_dir)
    boards = leaderboard(settings, rows)
    out = settings.reports_dir
    out.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    e = html.escape

    md = [f"# {settings.competition.name}: results", f"_Generated {now}_", ""]
    parts = []
    for task_id, board in boards.items():
        task = settings.tasks[task_id]
        md += [f"## {task.title}", "", "| Rank | Team | Mean score | Maps | Mean actions | Mean ticks |",
               "|---:|---|---:|---:|---:|---:|"]
        md += [f"| {b['rank']} | {b['team']} | {b['mean_score']} | {b['maps_played']} | "
               f"{b['mean_actions']} | {b['mean_ticks']} |" for b in board] or ["| | no evaluation runs yet | | | | |"]
        md.append("")
        trs = "".join(
            f"<tr><td class=num>{b['rank']}</td><td>{e(b['team'])}</td><td class=num><b>{b['mean_score']}</b></td>"
            f"<td class=num>{b['maps_played']}</td><td class=num>{b['mean_actions']}</td>"
            f"<td class=num>{b['mean_ticks']}</td><td>{e(', '.join(f'{k}: {v}' for k, v in b['per_map'].items()))}</td></tr>"
            for b in board) or "<tr><td colspan=7 class=muted>No evaluation runs yet</td></tr>"
        parts.append(f"<h2>{e(task.title)}</h2><p class=muted>Score: {e(task.score.type)} of {e(task.score.item)}. "
                     f"Mean over all evaluation maps (unplayed = 0). Ties: fewer actions, then fewer ticks.</p>"
                     f"<table><thead><tr><th>#</th><th>Team</th><th>Mean score</th><th>Maps</th><th>Actions</th>"
                     f"<th>Ticks</th><th>Per map</th></tr></thead><tbody>{trs}</tbody></table>")

    recent = sorted(rows, key=lambda r: r.get("finished_at") or "", reverse=True)[:100]
    run_rows = "".join(
        f"<tr><td><code>{e(r['run_id'])}</code></td><td>{e(r['team'])}</td><td>{e(r['task'])}</td>"
        f"<td>{e(r['mode'])}</td><td>{e(r['map'])}</td><td>{e(r['status'])}</td>"
        f"<td class=num>{'' if r.get('score') is None else r['score']}</td>"
        f"<td class=num>{r['budgets_used']['actions']}</td><td class=num>{r['budgets_used']['ticks']}</td>"
        f"<td>{e(r.get('end_reason') or '')}</td><td>{_spark(r['_series'])}</td></tr>"
        for r in recent) or "<tr><td colspan=11 class=muted>No runs yet</td></tr>"
    page = f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Arena Results</title>
<style>
:root{{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#6b6a65;--line:#e4e1d8;--accent:#b4541a}}
@media (prefers-color-scheme:dark){{:root{{--bg:#171715;--fg:#ecebe6;--muted:#9a988f;--line:#2e2d29;--accent:#e08a4f}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;margin:0;padding:24px 16px}}
main{{max-width:1100px;margin:auto}} h1{{margin:0 0 4px}} h2{{margin-top:36px;color:var(--accent)}}
.muted{{color:var(--muted)}} table{{border-collapse:collapse;width:100%;display:block;overflow-x:auto}}
th,td{{padding:6px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}}
td.num,th.num{{text-align:right;font-variant-numeric:tabular-nums}} svg{{color:var(--accent);display:block}}
</style></head><body><main>
<h1>{e(settings.competition.name)}</h1><p class=muted>Generated {now}</p>
{''.join(parts)}
<h2>Recent runs</h2><table><thead><tr><th>Run</th><th>Team</th><th>Task</th><th>Mode</th><th>Map</th>
<th>Status</th><th>Score</th><th>Actions</th><th>Ticks</th><th>Ended</th><th>Score over time</th></tr></thead>
<tbody>{run_rows}</tbody></table></main></body></html>"""
    html_path, md_path = out / "leaderboard.html", out / "leaderboard.md"
    html_path.write_text(page, encoding="utf-8")
    md_path.write_text("\n".join(md) + "\n", encoding="utf-8")
    return html_path, md_path
