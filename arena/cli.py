"""Organizer command line: `arena <command>` (or `python -m arena.cli <command>`)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
import time
from pathlib import Path

from .config import load_settings


def _settings(args):
    s = load_settings(args.config)
    if getattr(args, "backend", None):
        s.competition.backend = args.backend
    return s


def cmd_serve(args) -> None:
    import uvicorn
    from .gateway import BodyLimit, create_app
    s = _settings(args)
    app = BodyLimit(create_app(s), s.competition.gateway.max_body_bytes)
    g = s.competition.gateway
    uvicorn.run(app, host=args.host or g.host, port=args.port or g.port, log_level="info")


def cmd_add_team(args) -> None:
    s = _settings(args)
    token = s.add_team(args.name)
    print(f"Team {args.name!r} added. Give them this token (it is shown only once):\n\n  {token}\n")


def cmd_eval_seeds(args) -> None:
    s = _settings(args)
    seeds = s.generate_eval_seeds(args.task, args.count)
    print(f"{len(seeds)} hidden evaluation maps written to {s.eval_seeds_file} for {args.task}.")


def cmd_local_run(args) -> None:
    """Run the scripted bot for the task (automation_bot or scripted_bot) through the real gateway code in-process."""
    from fastapi.testclient import TestClient
    from .controller import RunManager
    from .gateway import create_app
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sdk"))
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bots"))
    from arena_sdk import ArenaClient  # type: ignore
    import automation_bot  # type: ignore
    import scripted_bot  # type: ignore

    s = _settings(args)
    resume_id = resume_save = None
    if args.resume:                                  # carry a saved run on: its own task, same run folder
        resume_id, _, resume_save = args.resume.partition(":")
        if args.bot != "brain":
            print(f"--resume carries on a brain bot's save (its memory is in the .bot.pkl files), but this "
                  f"command would start the {args.bot} bot. Add --bot brain (and the run's other flags), or "
                  f"check runs/local/{resume_id}/launch.json")
            return
        state = _save_state(s.root / "runs" / "local" / resume_id / "saves", resume_save or None)
        if state is None:
            print(f"no save {resume_save or ''} in runs/local/{resume_id}/saves")
            return
        args.task, resume_save = state["task"], state["name"]
    if args.speed is not None:
        s.competition.factorio.advance_speed = args.speed
    if args.clock_speed is not None:                # realtime levels: run the game faster than real time
        s.tasks[args.task] = s.tasks[args.task].model_copy(update={"clock_speed": args.clock_speed})
    tmp = Path(tempfile.mkdtemp(prefix="arena-local-"))
    s.secrets_dir = tmp
    s.runs_dir = s.root / "runs" / "local"
    token = s.add_team("local-test")
    manager = RunManager(s)
    app = create_app(s, manager, start_reaper=False)
    with TestClient(app) as http:
        client = ArenaClient(token=token, http=http)
        if resume_id:
            session = manager.resume_run("local-test", resume_id, resume_save)
            print(f"resuming run {resume_id} from save {resume_save}", flush=True)
        else:
            session = manager.create_run("local-test", args.task, "practice", wait=True, seed_override=args.seed)
        from arena_sdk import Run  # type: ignore
        run = Run(client, session.id, session.public_state(True))
        if session.status != "active":
            print("run failed to start:", session.end_reason)
            return
        # a resume keeps the flags the run was started with (plus any given now): writing only this command
        # line wiped them, and the next resume started the default bot (run 20261119-202811: the crew bot
        # played a brain save)
        _write_launch(s.runs_dir / session.id, getattr(args, "launch_argv", None) or sys.argv[1:])
        can_save = session.backend is not None and session.backend.info().get("backend") == "factorio"
        saver = (lambda name: session.save(name)) if can_save else None
        url = None
        if args.monitor:                              # the live page reads the run folders; it never touches the game
            from .monitor import start_monitor
            url = start_monitor(s.runs_dir, args.monitor)
            if url:
                print(f"monitor: {url}#{session.id}", flush=True)
        try:
            if args.watch:
                port = (session.backend.info() if session.backend else {}).get("spectator_port_udp")
                if port is None:
                    print("--watch needs the factorio backend (spectators join a real game)")
                else:
                    print(f"Spectators: Multiplayer > Connect to address > 127.0.0.1:{port} (or this PC's address)")
                    print(f"The bot starts 10 s after someone joins, or in {args.watch} s; game speed while time "
                          f"runs: {s.competition.factorio.advance_speed}x", flush=True)
                    _wait_for_spectator(session, args.watch)   # the world stays paused meanwhile
            if args.bot == "openai":
                import openai_bot  # type: ignore
                from dotenv_lite import load_env  # type: ignore
                load_env()                                  # OPENAI_API_KEY from the project's .env
                llm = openai_bot.make_client(args.provider, None, None)
                model = args.model or openai_bot.PROVIDERS[args.provider]["model"]
                effort = args.reasoning_effort
                api = args.api or ("responses" if args.provider == "openai" else "chat")
                if api == "responses":         # tools and reasoning together (Chat Completions refuses that for some models)
                    llm, effort = openai_bot.ResponsesAdapter(llm, args.reasoning_effort or "low"), None
                print(f"model {model} ({api} API, reasoning {args.reasoning_effort or ('low' if api == 'responses' else 'default')}), "
                      f"cost cap ${args.max_cost}, up to {args.max_turns} turns", flush=True)
                tfile = open(s.runs_dir / session.id / "transcript.jsonl", "a", encoding="utf-8")

                def note(entry: dict) -> None:            # what the model wrote and called, turn by turn
                    tfile.write(json.dumps(entry) + "\n")
                    tfile.flush()
                try:
                    result = openai_bot.play(run, session.task.public(), client.rules()["constants"], llm, model,
                                             max_turns=args.max_turns, max_cost_usd=args.max_cost,
                                             reasoning_effort=effort, log=lambda *a: print(*a, flush=True),
                                             transcript=note, confirm_early_end=0.25)
                finally:
                    tfile.close()
            elif args.bot == "brain":
                from brain.bot import play as brain_play  # type: ignore
                from brain.brain import LLMStrategist  # type: ignore
                strategist = None
                if args.strategist == "openai":
                    import openai_bot  # type: ignore
                    from dotenv_lite import load_env  # type: ignore
                    load_env()
                    model = args.model or "gpt-6.1-terra"
                    strategist = LLMStrategist(openai_bot.make_client(args.provider, None, None), model,
                                               session.task.public(), max_cost_usd=args.max_cost,
                                               log=lambda *a: print(*a, flush=True))
                    print(f"brain bot, strategist {model}, cost cap ${args.max_cost}", flush=True)
                designer = None
                if args.placer != "search" or args.designer_model:
                    from fab.bench import make_designer  # type: ignore
                    designer = make_designer(args.designer_model or "gpt-6.1-sol",
                                             None if args.designer_effort == "none" else args.designer_effort,
                                             args.designer_max_cost or args.max_cost, args.provider)
                    print(f"factory chiplets: placer {args.placer}, designer {designer.model} "
                          f"(reasoning {designer.effort}), cost cap ${designer.max_cost}", flush=True)
                assessor = None
                if args.strategist == "openai" and not args.no_assessor:
                    import openai_bot  # type: ignore
                    from brain.assess import LLMAssessor  # type: ignore
                    from dotenv_lite import load_env  # type: ignore
                    load_env()
                    assessor = LLMAssessor(openai_bot.make_client(args.provider, None, None), args.assessor_model,
                                           max_cost_usd=args.assessor_max_cost, log=lambda *a: print(*a, flush=True))
                    print(f"supervisor: assessor {args.assessor_model}, cost cap ${args.assessor_max_cost}",
                          flush=True)
                fixer = None
                if args.strategist == "openai" and not args.no_fixer:
                    import openai_bot  # type: ignore
                    from brain.fixer import LLMFixer  # type: ignore
                    from dotenv_lite import load_env  # type: ignore
                    load_env()
                    fixer = LLMFixer(openai_bot.make_client(args.provider, None, None),
                                     args.fixer_model or args.assessor_model, max_cost_usd=args.fixer_max_cost,
                                     log=lambda *a: print(*a, flush=True))
                    print(f"fixer: {fixer.model}, cost cap ${args.fixer_max_cost}", flush=True)
                overseer = None
                if args.strategist == "openai" and not args.no_overseer:
                    import openai_bot  # type: ignore
                    from brain.overseer import Overseer  # type: ignore
                    from dotenv_lite import load_env  # type: ignore
                    load_env()
                    overseer = Overseer(openai_bot.make_client(args.provider, None, None), args.overseer_model,
                                        effort=None if args.overseer_effort == "none" else args.overseer_effort,
                                        period_min=args.overseer_minutes, max_cost_usd=args.overseer_max_cost,
                                        log=lambda *a: print(*a, flush=True))
                    print(f"overseer: {overseer.model} (reasoning {args.overseer_effort}), every "
                          f"{args.overseer_minutes:g} game min, cost cap ${args.overseer_max_cost}", flush=True)
                chiplets, placer = None, "fallback" if args.placer in ("best", "search") else args.placer
                if args.designs != "library":
                    from brain.designer import ChipletDesigner  # type: ignore
                    if designer is None:
                        from fab.bench import make_designer  # type: ignore
                        designer = make_designer(args.designer_model or "gpt-6.1-sol",
                                                 None if args.designer_effort == "none" else args.designer_effort,
                                                 args.designer_max_cost or args.max_cost, args.provider)
                    chiplets = ChipletDesigner(designer, mode=args.designs, log=lambda *a: print(*a, flush=True))
                    placer = "llm"                        # factory floors: the designer first too
                    print(f"designs: {args.designs} (steam stations and factory floors drawn by {designer.model} "
                          f"this run{'' if args.designs == 'ai-only' else ', hand-made ones as the fallback'})",
                          flush=True)
                result = brain_play(run, client, session.task.public(), strategist,
                                    log=lambda *a: print(*a, flush=True), telemetry_dir=s.runs_dir / session.id,
                                    designer=designer, assessor=assessor, fixer=fixer, chiplets=chiplets,
                                    placer=placer, saver=saver,
                                    resume_from=(resume_save or "") if resume_id else None,
                                    autosave_minutes=args.autosave, overseer=overseer)
            elif args.bot == "atlas":
                import os
                from atlas.bot import play as atlas_play  # type: ignore
                stop = os.environ.get("ATLAS_STOP_AFTER") or None        # short checks: stop after a phase
                cap = float(os.environ["ATLAS_MAX_MINUTES"]) if os.environ.get("ATLAS_MAX_MINUTES") else None
                gateway = None
                if os.environ.get("ATLAS_LLM"):                           # e.g. ATLAS_LLM=openai: doctrine + escalation
                    from atlas.llm.gateway import Gateway  # type: ignore
                    from dotenv_lite import load_env  # type: ignore
                    load_env()
                    gateway = Gateway.from_env(os.environ["ATLAS_LLM"],
                                               budget_usd=float(os.environ.get("ATLAS_LLM_BUDGET") or 1.0),
                                               log=lambda *a: print(*a, flush=True))
                result = atlas_play(run, client, session.task.public(), log=lambda *a: print(*a, flush=True),
                                    telemetry_dir=s.runs_dir / session.id, stop_after=stop, max_minutes=cap,
                                    gateway=gateway)
            elif args.bot == "crew" or (args.bot == "scripted" and session.task.clock == "realtime"):
                import crew_bot  # type: ignore
                director = None
                if args.director == "openai":
                    import openai_bot  # type: ignore
                    from dotenv_lite import load_env  # type: ignore
                    load_env()
                    model = args.model or "gpt-6.1-terra"
                    director = crew_bot.LLMDirector(openai_bot.make_client(args.provider, None, None), model,
                                                    max_cost_usd=args.max_cost, log=print)
                    print(f"crew bot, director {model}, cost cap ${args.max_cost}", flush=True)
                designer = None
                if args.placer != "search":
                    import openai_bot  # type: ignore
                    from dotenv_lite import load_env  # type: ignore
                    from fab.llm_place import LLMDesigner  # type: ignore
                    load_env()
                    designer = LLMDesigner(openai_bot.make_client(args.provider, None, None),
                                           args.designer_model or args.model or "gpt-6.1-terra",
                                           max_cost_usd=args.designer_max_cost or args.max_cost,
                                           reasoning_effort=None if args.designer_effort == "none"
                                           else args.designer_effort)
                    print(f"factory layouts: {args.placer}, designer {designer.model}, cost cap ${args.max_cost}",
                          flush=True)
                result = crew_bot.play(run, director, log=lambda *a: print(*a, flush=True),
                                       placer="best" if args.placer == "fallback" else args.placer,
                                       designer=designer)
                for f in (result.get("bot") or {}).get("factories", []):     # the floor plans, next to the logs
                    (s.runs_dir / session.id / f"factory{f['id']}.txt").write_text(
                        f["summary"] + "\n\n" + f["layout"] + "\n", encoding="utf-8")
            else:
                bot = automation_bot if session.task.score.type == "automation" else scripted_bot
                result = bot.play(run, log=print)
            print(json.dumps(result, indent=2))
            print(f"logs: {s.runs_dir / session.id}")
            if result.get("bot"):                          # model, turns, tokens, cost: kept with the run
                (s.runs_dir / session.id / "bot.json").write_text(json.dumps(result["bot"], indent=2), encoding="utf-8")
            if not args.no_replay_page:
                from .replay_view import write_replay_page
                print(f"replay page: {write_replay_page(s.runs_dir / session.id)}")
            if (result.get("bot") or {}).get("stopped_for_resume"):
                print(f"stopped and saved. Carry on: arena local-run --resume {session.id} (or the Resume "
                      f"button on the monitor's Saves tab)", flush=True)
                if args.monitor and url:              # the page stays up so it can start the resumed run
                    print("the monitor stays up for that; Ctrl+C to quit", flush=True)
                    while True:
                        time.sleep(3600)
        except KeyboardInterrupt:
            if session.status == "active":            # score what it reached; a long open game is not wasted
                print("\nstopped (Ctrl+C): scoring what the run reached so far...", flush=True)
                session.end("stopped_by_organizer")
                r = session.result or {}
                print(f"score {r.get('score')} ({(r.get('breakdown') or {}).get('reached', '?')} milestones); "
                      f"logs: {s.runs_dir / session.id}", flush=True)
            else:
                print("\nstopped (Ctrl+C)", flush=True)
        finally:
            session.void("stopped_by_organizer")    # no-op once the run has finished
            manager.stop()


def _save_state(folder: Path, name: str | None) -> dict | None:
    """A save's session file (the newest when no name is given)."""
    files = sorted(folder.glob("*.session.json"), key=lambda p: p.stat().st_mtime) if folder.exists() else []
    if name:
        files = [p for p in files if p.name == f"{name}.session.json"]
    return json.loads(files[-1].read_text(encoding="utf-8")) if files else None


def _write_launch(run_dir: Path, argv: list[str]) -> None:
    """How this run was started, minus --resume and --monitor: the monitor's Resume button starts it again."""
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
            continue
        if a in ("--resume", "--monitor"):
            skip = True
            continue
        if a.startswith(("--resume=", "--monitor=")):
            continue
        out.append(a)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "launch.json").write_text(json.dumps({"argv": out}, indent=2), encoding="utf-8")


def _spectate(session) -> None:
    """Move every connected player onto the run's surface, next to the bot (a first join can land elsewhere)."""
    try:
        session.backend.caller.rcon.command('/sc rcon.print(remote.call("arena", "spectate"))')
    except (OSError, AttributeError) as exc:
        print("could not move spectators:", exc, flush=True)


def _wait_for_spectator(session, seconds: int) -> None:
    """Organizer-only wait before the bot plays: ends 10 s after a player joins the game."""
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        session.last_activity = time.time()          # waiting for a viewer is not the team idling
        try:
            n = int(session.backend.caller.rcon.command("/sc rcon.print(#game.connected_players)").strip() or 0)
        except (ValueError, OSError, AttributeError):
            n = 0
        if n:
            _spectate(session)
            print("spectator joined; the bot starts in 10 s (type /bot in game to jump to it)", flush=True)
            time.sleep(10)
            _spectate(session)                       # anyone who joined during those 10 s
            break
        time.sleep(2)
    waited = time.monotonic() - start
    if session.deadline:
        session.deadline += waited                   # the wall-clock limit starts when the bot does
    session.last_activity = time.time()


def _bench_args(p) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sdk"))
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bots"))
    from fab.bench import add_args  # type: ignore
    add_args(p)


def cmd_chiplet_bench(args) -> None:
    from fab.bench import main  # type: ignore
    main(args)


def cmd_replay(args) -> None:
    from .replay import replay_run
    s = _settings(args)
    print(json.dumps(replay_run(Path(args.run_dir), s), indent=2))


def cmd_replay_page(args) -> None:
    from .replay_view import write_replay_page
    print(f"wrote {write_replay_page(Path(args.run_dir))}")


def cmd_report(args) -> None:
    from .report import build_report
    s = _settings(args)
    html_path, md_path = build_report(s)
    print(f"wrote {html_path}\nwrote {md_path}")


def cmd_vet_seed(args) -> None:
    """Boot a map and print what is near spawn, to check a hidden seed is fair."""
    from pydantic import TypeAdapter
    from .models import Action
    from .replay import _make_backend
    s = _settings(args)
    task = s.tasks[args.task]
    b = _make_backend(s, s.competition.backend)
    try:
        b.setup(task, args.seed)
        obs = b.act(TypeAdapter(Action).validate_python({"type": "observe", "radius": 20}), 0).data
        report = {k: {"distance": v["distance"], "tiles": v["tiles"], "amount": v["amount"]}
                  for k, v in obs["resource_summary"].items()}
        print(json.dumps({"seed": args.seed, "resources_within_64": report,
                          "obstacles_within_20": len(obs.get("obstacles", [])),
                          "water_tiles_within_20": len(obs.get("water", [])),
                          "nearest_water": obs.get("water_summary")}, indent=2))
    finally:
        b.close()


def cmd_monitor(args) -> None:
    """Serve the live monitor page for every run in runs/ (local-run and serve both log there)."""
    from .monitor import serve_monitor
    s = load_settings(args.config, load_tasks=False)
    serve_monitor(s.root / s.competition.paths.runs, args.port)


def cmd_catalog(args) -> None:
    """Boot a game and write config/catalog.json: every item, recipe, technology and entity of this game version."""
    from .backends.factorio import FactorioBackend, SlotPool
    s = load_settings(args.config, load_tasks=False)
    pool = SlotPool(1)
    b = FactorioBackend(s.competition.factorio, s.root, pool, pool.acquire())
    out = Path(args.out) if args.out else s.root / "config" / "catalog.json"
    try:
        b._docker_run()
        b._connect()
        data = b.dump_catalog(out)
    finally:
        b.close()
    print(f"wrote {out}: {len(data.get('items', {}))} items, {len(data.get('recipes', {}))} recipes, "
          f"{len(data.get('technologies', {}))} technologies, {len(data.get('entities', {}))} buildable entities")


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpx2", "httpcore"):        # one line per API call buries the bot's own output
        logging.getLogger(noisy).setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(prog="arena", description="Factorio AI Arena organizer tools")
    ap.add_argument("--config", default="config/competition.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="run the public game API")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--backend", choices=["sim", "factorio"])
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("add-team", help="create a team and print its token once")
    p.add_argument("name")
    p.set_defaults(fn=cmd_add_team)

    p = sub.add_parser("eval-seeds", help="generate hidden evaluation maps for a task")
    p.add_argument("task")
    p.add_argument("--count", type=int, default=3)
    p.set_defaults(fn=cmd_eval_seeds)

    p = sub.add_parser("local-run", help="run the scripted baseline in-process")
    p.add_argument("--task", default="level1-iron")
    p.add_argument("--seed", type=int)
    p.add_argument("--backend", choices=["sim", "factorio"])
    p.add_argument("--watch", type=int, default=0, metavar="SECONDS",
                   help="factorio only: print the spectator port and wait this long before the bot plays")
    p.add_argument("--speed", type=float, help="game speed while time runs (default: factorio.advance_speed)")
    p.add_argument("--bot", choices=["scripted", "openai", "crew", "brain", "atlas"], default="scripted",
                   help="scripted = the task's scripted baseline; openai = bots/openai_bot.py (bills your key); "
                        "crew = bots/crew_bot.py, a director that plans + a scripted crew that builds (realtime)")
    p.add_argument("--strategist", choices=["scripted", "openai"], default="scripted",
                   help="brain bot: who plans; openai uses --model and --max-cost (bills your key)")
    p.add_argument("--monitor", type=int, default=8730, metavar="PORT",
                   help="serve the live monitor page on 127.0.0.1:PORT while the run plays (0 = off)")
    p.add_argument("--clock-speed", type=float,
                   help="realtime levels: game speed while the clock runs (e.g. 2 = twice real time)")
    p.add_argument("--director", choices=["scripted", "openai"], default="scripted",
                   help="crew bot: who plans; openai uses --model and --max-cost (bills your key)")
    p.add_argument("--placer", choices=["search", "llm", "best", "fallback"], default="search",
                   help="who draws factory floor plans. search: the layout engine alone (free); llm: the designer "
                        "model first, search when it fails; fallback (brain) / best (crew): search first, the "
                        "designer where it finds nothing / both. Bills your key")
    p.add_argument("--designer-model", help="the floor-plan designer's model (default gpt-6.1-sol for the brain; "
                                            "giving one turns the designer on with placer fallback)")
    p.add_argument("--designer-effort", choices=["none", "minimal", "low", "medium", "high", "xhigh"],
                   default="medium", help="the designer's reasoning effort (none: plain chat, no reasoning)")
    p.add_argument("--designer-max-cost", type=float, help="the designer's own cost cap in USD (default --max-cost)")
    p.add_argument("--designs", choices=["library", "ai", "ai-only"], default="library",
                   help="brain bot: who draws the chiplets. library: the hand-made ones; ai: the designer model "
                        "draws the steam station (and factory floors) fresh each run, each checked to work before "
                        "it is built, hand-made ones as the fallback; ai-only: no fallback. Bills your key"),
    p.add_argument("--assessor-model", default="gpt-6.1-terra",
                   help="brain bot: the supervisor's fast model that watches the base and amends the plan as "
                        "problems appear (on with --strategist openai)")
    p.add_argument("--assessor-max-cost", type=float, default=1.5, help="the assessor's own cost cap in USD")
    p.add_argument("--no-assessor", action="store_true",
                   help="brain bot: the supervisor's rules only (fuel and power fixes), no model")
    p.add_argument("--fixer-model", help="brain bot: the troubleshooter's model, asked when one error fails several "
                                         "goals or one goal keeps failing (default: --assessor-model)")
    p.add_argument("--fixer-max-cost", type=float, default=1.0, help="the fixer's own cost cap in USD")
    p.add_argument("--no-fixer", action="store_true", help="brain bot: the fixer's rules only, no model")
    p.add_argument("--overseer-model", default="gpt-6.1-sol",
                   help="brain bot: the overseer, a deep-thinking model that reads everything every few game "
                        "minutes, finds what is wrong or wasted and gives orders that run before anything else "
                        "(on with --strategist openai)")
    p.add_argument("--overseer-effort", choices=["none", "minimal", "low", "medium", "high", "xhigh"],
                   default="high", help="the overseer's reasoning effort")
    p.add_argument("--overseer-minutes", type=float, default=10.0,
                   help="game minutes between the overseer's looks (it may ask for 5-20)")
    p.add_argument("--overseer-max-cost", type=float, default=5.0, help="the overseer's own cost cap in USD")
    p.add_argument("--no-overseer", action="store_true", help="brain bot: no overseer")
    p.add_argument("--provider", default="openai", help="openai bot: openai | ollama | ollama-cloud")
    p.add_argument("--model", help="openai bot: model name (default: the provider's default)")
    p.add_argument("--max-cost", type=float, default=5.0, help="openai bot: stop before the bill passes this (USD)")
    p.add_argument("--max-turns", type=int, default=1000, help="openai bot: turn limit")
    p.add_argument("--reasoning-effort", choices=["none", "minimal", "low", "medium", "high", "xhigh"])
    p.add_argument("--api", choices=["chat", "responses"],
                   help="openai bot: OpenAI API (default: responses for provider openai, chat otherwise)")
    p.add_argument("--no-replay-page", action="store_true", help="skip writing runs/.../replay.html")
    p.add_argument("--resume", metavar="RUN_ID[:SAVE]",
                   help="factorio + brain: carry a saved run on (same run folder; newest save unless named). "
                        "Saves are made every --autosave game minutes and from the monitor's Saves tab")
    p.add_argument("--autosave", type=float, default=20.0, metavar="MINUTES",
                   help="brain bot on factorio: save the game and the bot every this many game minutes (0 = off)")
    p.set_defaults(fn=cmd_local_run)

    p = sub.add_parser("chiplet-bench", help="let a model design factory chiplets on saved terrain and check "
                                             "them offline (no game); compare with the search placer")
    _bench_args(p)
    p.set_defaults(fn=cmd_chiplet_bench)

    p = sub.add_parser("replay-page", help="write a map-view replay page (replay.html) for a logged run")
    p.add_argument("run_dir")
    p.set_defaults(fn=cmd_replay_page)

    p = sub.add_parser("replay", help="re-run a logged run and compare")
    p.add_argument("run_dir")
    p.set_defaults(fn=cmd_replay)

    p = sub.add_parser("report", help="build reports/leaderboard.html and .md")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("monitor", help="live page of every run on this machine: http://127.0.0.1:8730")
    p.add_argument("--port", type=int, default=8730)
    p.set_defaults(fn=cmd_monitor)

    p = sub.add_parser("catalog", help="boot a game and write config/catalog.json (the game's own data)")
    p.add_argument("--out", help="where to write it (default: config/catalog.json)")
    p.set_defaults(fn=cmd_catalog)

    p = sub.add_parser("vet-seed", help="inspect a map before using it as a hidden eval map")
    p.add_argument("--task", default="level1-iron")
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--backend", choices=["sim", "factorio"])
    p.set_defaults(fn=cmd_vet_seed)

    args = ap.parse_args(argv)
    if args.cmd == "local-run" and args.resume:
        args = _with_launch_args(ap, args, argv if argv is not None else sys.argv[1:])
    args.fn(args)


def _with_launch_args(ap, args, argv: list[str]):
    """`arena local-run --resume RUN` alone: the flags the run was started with (its launch.json), with any
    given now on top. A resumed run keeps its strategist, models and designs unless told otherwise."""
    run_id = args.resume.partition(":")[0]
    launch = Path(args.config).resolve().parent.parent / "runs" / "local" / run_id / "launch.json"
    if not launch.exists():
        return args
    try:
        stored = json.loads(launch.read_text(encoding="utf-8")).get("argv") or []
    except (OSError, ValueError):
        return args
    if "local-run" not in stored:
        return args
    merged = ap.parse_args(stored + ["--resume", args.resume])
    given = {a.split("=")[0].lstrip("-").replace("-", "_") for a in argv if a.startswith("--")}
    for k, v in vars(args).items():
        if k in given:
            setattr(merged, k, v)
    # what launch.json must say from now on: the stored flags, with the ones given now in their place
    merged.launch_argv = _merge_argv(ap, stored, list(argv))
    return merged


def _merge_argv(ap, stored: list[str], now: list[str]) -> list[str]:
    """local-run flags: `stored` with every option `now` gives replaced by its new value (one entry each, so
    resuming again and again never grows launch.json)."""
    sub = next(a for a in ap._actions if getattr(a, "choices", None) and "local-run" in (a.choices or {}))
    actions = sub.choices["local-run"]._option_string_actions

    def pairs(argv: list[str]) -> list[tuple[str, list[str]]]:
        out, i = [], 0
        argv = [a for a in argv if a != "local-run"]
        while i < len(argv):
            a = argv[i]
            key, _, inline = a.partition("=")
            act = actions.get(key)
            if act is None:
                out.append((a, []))
                i += 1
                continue
            if inline or act.nargs == 0:
                out.append((key, [inline] if inline else []))
                i += 1
            else:
                out.append((key, argv[i + 1:i + 2]))
                i += 2
        return out

    merged: dict[str, list[str]] = {}
    for key, vals in pairs(stored) + pairs(now):
        merged.pop(key, None)                         # the later one wins, at the end
        merged[key] = vals
    return ["local-run"] + [t for key, vals in merged.items() for t in [key, *vals]]


if __name__ == "__main__":
    main()
