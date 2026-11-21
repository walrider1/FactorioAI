"""Stop a run and carry it on later: the game save, the session's clock and counters, the bot's memory, and
the monitor page's Saves tab that asks for them."""

import json
import pickle
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from arena.backends.sim import SimBackend
from arena.controller import ArenaError, RunManager, RunSession
from brain import persist
from brain.crew import Crew
from brain.modules import Base, Site


# ------------------------------------------------------------------ the bot's memory
def _crew_graph():
    crew = Crew.__new__(Crew)
    crew.base, crew.links, crew.urgent = Base(), [], [{"do": "link"}]
    site = crew.base.add(Site("steam_power", {"engines": 2}, (5, 5)))
    port = {"item": "coal", "end": [1, 1], "approach": [0, 1], "fuel": True, "per_minute": 30}
    site.params["ports"] = [port]
    first = {"source": (0.5, 0.5), "to_site": site.id, "item": "coal", "port": port, "steps": []}
    crew.links += [first, {"source": (3, 0), "to_site": site.id, "item": "coal", "port": port, "tap_of": first}]
    crew.log = print                                     # a callable: left out
    crew.foreman = SimpleNamespace(crew=crew, priority=["coal"], last=123.0, fuel_fixes=7)
    world = SimpleNamespace(patches=[{"name": "coal", "tiles": 250}], lock=threading.Lock(), tick=7200,
                            _tick_wall=99.0, kn=SimpleNamespace(researched={"logistics"}))
    return crew, world


def test_the_bots_memory_survives_with_shared_objects_still_shared():
    crew, world = _crew_graph()
    data, dropped = persist.capture(crew, world, loop={"goals": [{"do": "research", "tech": "plastics"}]})
    assert "world.lock" in dropped                      # a lock cannot go to another process
    fresh = Crew.__new__(Crew)
    fresh.log, fresh.foreman = print, SimpleNamespace(crew=fresh, last=5.0)
    learned = []
    w2 = SimpleNamespace(tick=0, _tick_wall=1.0, kn=SimpleNamespace(learn_research=learned.append))
    loop = persist.restore(data, fresh, w2)
    site = fresh.base.sites[0]
    assert fresh.links[0]["port"] is site.params["ports"][0]       # the link still feeds that very port
    assert fresh.links[1]["tap_of"] is fresh.links[0]              # and the tap still points at its link
    assert fresh.foreman.priority == ["coal"] and fresh.foreman.crew is fresh and fresh.foreman.last == 5.0
    assert w2.patches[0]["name"] == "coal" and w2.tick == 7200 and w2._tick_wall == 1.0   # no old clocks
    assert loop["goals"][0]["tech"] == "plastics" and learned == [["logistics"]]


def test_a_save_from_another_bot_version_is_refused():
    crew, world = _crew_graph()
    snap = pickle.loads(persist.capture(crew, world)[0])
    snap["version"] = 999
    with pytest.raises(ValueError):
        persist.restore(pickle.dumps(snap), crew, world)


def test_saves_are_listed_and_the_newest_complete_one_is_read(tmp_path):
    for name in ("m00010-auto-1", "m00020-manual-2"):
        (tmp_path / f"{name}.session.json").write_text(json.dumps({"tick": 36000}))
        (tmp_path / f"{name}.zip").write_bytes(b"zip")
        persist.write(tmp_path, name, b"state-" + name.encode(), {"kind": name.split("-")[1]})
        time.sleep(0.02)
    (tmp_path / "m00030-auto-3.session.json").write_text("{}")      # the game is there, the bot is not
    rows = persist.index(tmp_path)
    assert [r["name"] for r in rows] == ["m00010-auto-1", "m00020-manual-2", "m00030-auto-3"]
    assert rows[1]["kind"] == "manual" and rows[0]["minute"] == 10.0 and not rows[2]["bot"]
    assert persist.read(tmp_path) == ("m00020-manual-2", b"state-m00020-manual-2")
    assert persist.read(tmp_path, "m00010-auto-1")[1] == b"state-m00010-auto-1"


def test_a_request_from_the_page_is_taken_once(tmp_path):
    (tmp_path / "control.json").write_text(json.dumps({"cmd": "stop"}))
    assert persist.take_request(tmp_path) == "stop" and persist.take_request(tmp_path) is None
    (tmp_path / "control.json").write_text(json.dumps({"cmd": "rm -rf"}))
    assert persist.take_request(tmp_path) is None


# ------------------------------------------------------------------ the arena session
class SavingBackend(SimBackend):
    def __init__(self):
        super().__init__()
        self.clock = None

    def save(self, name, dest):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / f"{name}.zip").write_bytes(b"game")
        return dest / f"{name}.zip"

    def resume(self, task, save_file):
        self._tick = 90_000
        return {"resumed_from": save_file.name, "tick": self._tick}

    def start_clock(self, ticks, run=True):
        self.clock = ticks
        return self._tick

    def info(self):
        return {"backend": "factorio"}


def test_a_saved_session_carries_on_with_its_clock_and_counters(settings, tmp_path):
    task = settings.tasks["freeplay-open"]
    run_dir = settings.runs_dir / "20261119-170000-abcdef"
    s = RunSession(run_dir.name, "local-test", task, "practice", 7, "practice-7", run_dir)
    s.start(SavingBackend())
    s.clock_tick, s.actions_used, s.observes_used, s.seq = 0, 1234, 567, 1800
    s._tick = 0
    s.backend._tick = 90_000
    state = s.save("m01500-manual-1")
    assert (run_dir / "saves" / "m01500-manual-1.zip").exists() and state["actions_used"] == 1234
    s.end("stopped_by_organizer")
    assert (run_dir / "result.json").exists()

    m = RunManager(settings, backend_factory=lambda t: SavingBackend())
    r = m.resume_run("local-test", run_dir.name)
    assert r.status == "active" and r.id == run_dir.name and r.dir == run_dir
    assert (r.actions_used, r.observes_used, r.seq, r.clock_tick) == (1234, 567, 1800, 0)
    assert r.backend.clock == task.budgets.ticks - 90_000      # the build time left, not a fresh one
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["resumes"][0]["save"] == "m01500-manual-1" and not (run_dir / "result.json").exists()
    with pytest.raises(ArenaError):
        m.resume_run("other", run_dir.name, "no-such-save")


# ------------------------------------------------------------------ the monitor's Saves tab
def _run_dir(tmp_path, playing):
    d = tmp_path / "runs" / "local" / "20261119-170000-abcdef"
    (d / "saves").mkdir(parents=True)
    (d / "meta.json").write_text("{}")
    (d / "bot_live.json").write_text(json.dumps({"status": "playing" if playing else "finished",
                                                 "updated": time.time()}))
    (d / "launch.json").write_text(json.dumps({"argv": ["local-run", "--bot", "brain", "--strategist", "openai"]}))
    for ext in (".zip", ".bot.pkl"):
        (d / "saves" / f"m00100-auto-1{ext}").write_bytes(b"x")
    (d / "saves" / "m00100-auto-1.session.json").write_text(json.dumps({"tick": 360000}))
    return d


def test_the_page_asks_a_playing_bot_to_save_and_resumes_a_stopped_run(tmp_path, monkeypatch):
    from arena import monitor
    d = _run_dir(tmp_path, playing=True)
    assert monitor.request_control(d, "stop")["ok"]
    assert json.loads((d / "control.json").read_text())["cmd"] == "stop"
    assert not monitor.request_resume(d, "m00100-auto-1")["ok"]      # still playing
    (d / "bot_live.json").write_text(json.dumps({"status": "finished", "updated": time.time()}))
    assert not monitor.request_control(d, "save")["ok"]               # nothing to ask
    started = []
    monkeypatch.setattr(monitor.subprocess, "Popen", lambda cmd, **kw: started.append(cmd))
    assert not monitor.request_resume(d, "../../etc")["ok"]
    r = monitor.request_resume(d, "m00100-auto-1")
    assert r["ok"] and started[0][-9:] == ["local-run", "--bot", "brain", "--strategist", "openai",
                                           "--resume", f"{d.name}:m00100-auto-1", "--monitor", "0"]
    assert monitor.saves(d)[0]["complete"] and monitor.saves(d)[0]["minute"] == 100.0


def test_only_the_page_itself_may_send_requests(tmp_path):
    import http.client
    from arena.monitor import _handler, Runs
    from http.server import ThreadingHTTPServer
    d = _run_dir(tmp_path, playing=True)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(Runs(tmp_path / "runs")))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def post(headers):
            c = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=5)
            c.request("POST", f"/api/runs/{d.name}/control", body=json.dumps({"cmd": "save"}),
                      headers={"Content-Type": "application/json", **headers})
            return c.getresponse().status
        assert post({}) == 403                                       # another web page cannot ask
        assert post({"X-Arena-Monitor": "1", "Host": "evil.example"}) == 403
        assert post({"X-Arena-Monitor": "1"}) == 200 and (d / "control.json").exists()
    finally:
        srv.shutdown()


# ------------------------------------------------------------------ the command line
def test_a_resume_keeps_the_flags_the_run_was_started_with(tmp_path):
    from arena import cli
    run = tmp_path / "runs" / "local" / "20261119-170000-abcdef"
    cli._write_launch(run, ["local-run", "--bot", "brain", "--strategist", "openai", "--monitor", "8731",
                            "--resume=x", "--designs", "ai"])
    assert json.loads((run / "launch.json").read_text())["argv"] == [
        "local-run", "--bot", "brain", "--strategist", "openai", "--designs", "ai"]
    (tmp_path / "config").mkdir()
    cfg = str(tmp_path / "config" / "competition.yaml")
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--config")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("local-run")
    for flag, default in (("--bot", "scripted"), ("--strategist", "scripted"), ("--designs", "library"),
                          ("--resume", None), ("--clock-speed", None)):
        p.add_argument(flag, default=default)
    argv = ["--config", cfg, "local-run", "--resume", run.name, "--clock-speed", "3"]
    args = cli._with_launch_args(ap, ap.parse_args(argv), argv)
    assert (args.bot, args.strategist, args.designs, args.clock_speed) == ("brain", "openai", "ai", "3")


def test_a_save_and_its_bookkeeping_never_end_the_run(tmp_path):
    """Run 20261119-200940 saved fine and then died on the line that logged it (a keyword clash in the
    telemetry call): the whole save step runs with the real telemetry here, and no failure in it escapes."""
    from brain.telemetry import Telemetry
    crew, world = _crew_graph()
    tel = Telemetry(tmp_path)
    saved = []
    state = {"crew": crew, "world": world, "loop": {"goals": []}}
    summary = {"kind": "auto", "minute": 21.0}

    def saver(name):
        (tmp_path / "saves").mkdir(exist_ok=True)
        (tmp_path / "saves" / f"{name}.zip").write_bytes(b"game")
        (tmp_path / "saves" / f"{name}.session.json").write_text(json.dumps({"tick": 75600}))
        saved.append(name)
    assert persist.save_point(saver, tmp_path / "saves", "m00021-auto-1", state, summary, tel=tel, log=lambda m: None)
    ev = [e for e in tel.state["events"] if e["kind"] == "save"]
    assert ev[-1]["ok"] and ev[-1]["save_kind"] == "auto" and persist.read(tmp_path / "saves")[0] == "m00021-auto-1"

    def broken_saver(name):
        raise RuntimeError("rcon failure while saving: connection closed by server")
    assert not persist.save_point(broken_saver, tmp_path / "saves", "m00040-auto-2", state, summary, tel=tel,
                                  log=lambda m: None)
    assert tel.state["events"][-1]["ok"] is False
    bad_tel = SimpleNamespace(event=lambda *a, **k: 1 / 0, set=lambda **k: 1 / 0)
    assert persist.save_point(saver, tmp_path / "saves", "m00060-auto-3", state, summary, tel=bad_tel,
                              log=lambda m: 1 / 0)                  # broken logging: still saved, no crash
    tel.close()
