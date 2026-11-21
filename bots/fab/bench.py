"""The chiplet bench: let a model design factory chiplets on saved terrain, check them, no game needed.

A case is terrain plus what to build:
    a case file    {"map": "<grid>.json", "item": ..., "per_minute": ..., "near": [x, y]}   (the brain writes one
                   for every factory it designs during a run: runs/<run>/chiplets/)
    a grid file    tests/fixtures/map_1008.json  (item and rate from --item / --rate, place: between the ores)
    a run folder   runs/local/<id>  (terrain rebuilt from its actions.jsonl)

Every case is designed by each placer asked for (search: simulated annealing, free; llm: the model draws the
floor plan, the engine validates, routes and powers it), then the design-rule check runs on the result. The
report says, per placer: how many designs pass, their cost (belts, undergrounds, poles), time and money.
The layouts are written as text next to the report, to look at.

    arena chiplet-bench tests/fixtures/*.json runs/local/<id>/chiplets --placer search llm \
        --designer-model gpt-6.1-sol --designer-effort medium --max-cost 2

When the llm placer passes as often as search (or more, or cheaper), try it in a run: --placer llm.
"""

from __future__ import annotations

import argparse
import glob
import json
import time
from pathlib import Path

from .check import check
from .design import design_factory
from .grid import Grid


def load_cases(paths: list[str], item: str, rate: float) -> list[dict]:
    """Every case under these paths (files, folders, globs)."""
    out: list[dict] = []
    for raw in paths:
        for p in [Path(q) for q in (glob.glob(raw) or [raw])]:
            if p.is_dir():
                if (p / "actions.jsonl").exists():
                    out.append({"name": p.name, "grid": lambda p=p: Grid.from_log(p / "actions.jsonl"),
                                "item": item, "per_minute": rate, "near": None})
                else:
                    out += load_cases([str(q) for q in sorted(p.glob("*.json"))], item, rate)
                continue
            if not p.exists() or p.name.endswith(".grid.json") and (p.with_name(p.name[:-10] + ".json")).exists():
                continue                                      # a case's map: loaded through its case file
            data = json.loads(p.read_text(encoding="utf-8"))
            if "map" in data:                                 # a case file
                m = p.parent / data["map"]
                out.append({"name": p.stem, "grid": lambda m=m: Grid.load(m), "item": data.get("item", item),
                            "per_minute": float(data.get("per_minute", rate)), "near": data.get("near"),
                            "was": data.get("outcome")})
            elif "seen" in data:                              # a bare grid
                out.append({"name": p.stem, "grid": lambda p=p: Grid.load(p), "item": item, "per_minute": rate,
                            "near": None})
    return out


def run_bench(cases: list[dict], placers: list[str], designer=None, log=print, out_dir: Path | None = None,
              max_attempts: int | None = None) -> dict:
    rows = []
    for case in cases:
        grid = case["grid"]()
        near = tuple(case["near"]) if case.get("near") else None
        for placer in placers:
            spent = getattr(designer, "cost_usd", 0.0)
            t0 = time.monotonic()
            try:
                d = design_factory(grid, case["item"], case["per_minute"], near=near, placer=placer,
                                   llm=designer if placer != "search" else None,
                                   max_attempts=max_attempts if placer != "search" else None)
                errs = check(d, grid) if d.ok else []
                row = {"case": case["name"], "placer": d.placer, "asked": placer, "item": case["item"],
                       "per_minute": case["per_minute"], "ok": d.ok and not errs,
                       "problems": (d.problems + errs)[:4], "cost": round(d.cost), "belts": d.belts,
                       "undergrounds": d.undergrounds, "poles": len(d.poles)}
                if out_dir:
                    (out_dir / f"{case['name']}.{placer}.txt").write_text(
                        d.summary() + "\n\n" + "\n".join(d.notes) + "\n\n" + d.render(grid) + "\n", encoding="utf-8")
            except Exception as exc:  # noqa: BLE001 - one broken case never stops the bench
                row = {"case": case["name"], "placer": placer, "asked": placer, "item": case["item"],
                       "per_minute": case["per_minute"], "ok": False, "problems": [f"crashed: {exc!r}"[:200]],
                       "cost": None}
            row["seconds"] = round(time.monotonic() - t0, 1)
            row["usd"] = round(getattr(designer, "cost_usd", 0.0) - spent, 4)
            # the llm placer falls back to search when the model is unavailable: that is not an llm pass
            if placer == "llm" and row["placer"] != "llm":
                row["ok"], row["problems"] = False, ["model unavailable: fell back to search"] + row["problems"]
            rows.append(row)
            log(f"{row['case']:<28} {placer:<7} {'PASS' if row['ok'] else 'fail'}  cost {row['cost']}  "
                f"{row['seconds']} s  ${row['usd']}" + ("" if row["ok"] else f"  {row['problems'][:1]}"))
    summary = {}
    for placer in placers:
        mine = [r for r in rows if r["asked"] == placer]
        good = [r for r in mine if r["ok"]]
        summary[placer] = {"cases": len(mine), "pass": len(good),
                           "pass_rate": round(len(good) / len(mine), 2) if mine else 0.0,
                           "mean_cost_of_passes": round(sum(r["cost"] for r in good) / len(good)) if good else None,
                           "seconds": round(sum(r["seconds"] for r in mine), 1),
                           "usd": round(sum(r["usd"] for r in mine), 4)}
    return {"summary": summary, "rows": rows,
            "designer": {"model": getattr(designer, "model", None), "effort": getattr(designer, "effort", None)}}


def make_designer(model: str, effort: str | None, max_cost: float, provider: str = "openai"):
    import openai_bot
    from dotenv_lite import load_env

    from .llm_place import LLMDesigner
    load_env()
    return LLMDesigner(openai_bot.make_client(provider, None, None), model, max_cost_usd=max_cost,
                       reasoning_effort=effort or None)


def add_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("cases", nargs="+", help="case files, grid files, run folders or chiplets/ folders (globs ok)")
    p.add_argument("--placer", nargs="+", choices=["search", "llm"], default=["search", "llm"])
    p.add_argument("--item", default="automation-science-pack", help="for grids and runs without a case file")
    p.add_argument("--rate", type=float, default=5.0, help="per minute, for grids and runs without a case file")
    p.add_argument("--designer-model", default="gpt-6.1-sol")
    p.add_argument("--designer-effort", choices=["none", "minimal", "low", "medium", "high", "xhigh"],
                   default="medium")
    p.add_argument("--provider", default="openai")
    p.add_argument("--max-cost", type=float, default=2.0, help="stop calling the model past this (USD)")
    p.add_argument("--attempts", type=int, default=3, help="re-placements per llm design (each is 1-3 calls)")
    p.add_argument("--out", default="runs/chiplet-bench", help="where the report and layouts go")


def main(args: argparse.Namespace) -> dict:
    cases = load_cases(args.cases, args.item, args.rate)
    if not cases:
        raise SystemExit("no cases found")
    designer = make_designer(args.designer_model, None if args.designer_effort == "none" else args.designer_effort,
                             args.max_cost, args.provider) if "llm" in args.placer else None
    out = Path(args.out) / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    print(f"{len(cases)} case(s), placers {args.placer}"
          + (f", designer {designer.model} (reasoning {designer.effort})" if designer else ""), flush=True)
    rep = run_bench(cases, args.placer, designer, log=lambda s: print(s, flush=True), out_dir=out,
                    max_attempts=args.attempts)
    (out / "report.json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
    for placer, s in rep["summary"].items():
        print(f"{placer:<7} {s['pass']}/{s['cases']} pass, mean cost {s['mean_cost_of_passes']}, "
              f"{s['seconds']} s, ${s['usd']}")
    print(f"report and layouts: {out}")
    return rep
