"""The crew's view of the world and its hands: map memory, inventory, research, and the primitive moves.

FreeWorld extends the scripted World of automation_bot (so the fab engine and the power builder run on it
unchanged) with what a game from nothing needs: resource patches from scan, trees for wood, hand mining
and hand crafting that wait for the character's hands, the production panel (stats) and research state.
"""

from __future__ import annotations

import math
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager

import automation_bot as ab

from .knowledge import Knowledge

BuildError = ab.BuildError


def _same_patch(a: list, b: list) -> bool:
    """Two boxes [x0, y0, x1, y1] of one patch: they overlap by at least half of the smaller one."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return False
    small = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return w * h >= 0.5 * max(1, small)


class Preempted(Exception):
    """The supervisor paused the goal in hand at a safe point (something more urgent came up); it resumes later.
    Not a BuildError: upkeep code that swallows build failures must let this through."""
Tile = tuple[int, int]
HAND_MINE_TICKS = 120          # mining time 1 at hand-mining speed 0.5: two seconds an ore


class FreeWorld(ab.World):
    def __init__(self, run, kn: Knowledge, log=print, speed: float = 1.0):
        super().__init__(run, log)
        self.kn = kn
        self.speed = speed
        self.scan_radius = 192                 # set from the level's limits at the start of a run
        self.inventory: dict[str, int] = {}
        self.trees: dict[Tile, tuple[float, float]] = {}
        self.patches: list[dict] = []
        self.entities: dict[tuple[float, float], dict] = {}     # last seen state, by position
        self.stats: dict = {}
        self.research: dict = {}
        self.milestones: dict[str, int] = {}
        self.tick = 0
        self._tick_wall = time.monotonic()
        self.hands_free_tick = 0
        self.activity = "starting"            # one line for the monitor: what the character is doing now
        self.recorder: list | None = None     # while a module builds: every building placed, for its Site
        # shore tiles the game refused a pump on (kept for the whole run, so a retry tries other tiles), and
        # the directions the game said do fit on a tile
        self.bad_pumps: set[tuple[Tile, str]] = set()
        self.bad_shore: set[Tile] = set()
        self.pump_fits: dict[Tile, set[str]] = {}
        self.blocking = False                 # the goal being built blocks progress: modules try harder
        self.checkpoint = lambda: None        # set by the crew: the supervisor may pause the goal here
        self.tend = lambda: None              # set by the crew: keep the base running while the crew thinks
        # the search for crude oil, kept across goals: rings walked around a fixed home, so each try goes farther
        self.oil_home: tuple[float, float] | None = None
        self.oil_rings = 0
        self.observe_range = 60       # tiles from the character an observe may be centred (the level's limit)
        # storage chests are in nobody's way: layouts may plan over them, and clearing a tile to build on moves
        # the chest there first (set by the crew: the tiles, and the call that empties and moves a chest)
        self.movable: set[Tile] = set()
        # tiles kept free of buildings so belts can always get through: a margin around every site and along
        # trunks (crew._reserve_margin). Belts may use them (free_belt); buildings and poles may not (free)
        self.lanes: set[Tile] = set()
        self.make_way = None
        self.walk_fails = 0                   # moves in a row the game refused (no path, blocked)
        self.walk_fail: str | None = None     # the last refused move, for error messages and the strategist
        self.unstucks = 0                     # times the character got walled in and dug itself out
        # trap watch: far walks the game refused (no_path), cleared only by a real walk away. Short walks inside
        # a pocket work, so a counter that any good move resets never sees a pocket (run 20261119-105322)
        self._far_fails: deque = deque(maxlen=40)
        self.escapes: deque = deque(maxlen=20)  # game ticks of every dig-out, to see the same trap come back
        self.trap_notes: deque = deque(maxlen=8)   # what each dig-out did (the strategist and monitor see it)
        self.on_dug = None                    # set by the crew: forget buildings a dig-out took (their sites)
        self.on_trapped = None                # set by the bot: ask the fixer (an LLM) when digging out is not enough
        self._escalating = False
        self._unsticking = False
        # where the control loop is, for the monitor's live graph: the main node, the steps inside it, and the
        # foreman (who borrows the character between steps); every change goes into the trace
        self.stage = "start"
        self.steps: list[str] = []
        self.foreman_step: str | None = None
        self.trace: deque = deque(maxlen=60)
        self._trace("main", "start")
        orig_act = run.act

        self.refused: deque = deque(maxlen=30)         # the last game actions the game refused (for the fixer)

        # progress, for the stall guard (Crew.checkpoint): the last tick anything was built, crafted, picked up,
        # put in or taken out. Walking and refused actions are not progress (run 20261119-202811: 66 game
        # minutes of walking between coal chests with a full inventory, 3392 refused extracts, one goal)
        self.last_progress_tick = 0
        self.last_full_tick = -1e9                     # the last time the game said the inventory is full
        self.last_place_tick = -1e9                    # the last time a building went down (a build still placing)
        PRODUCTIVE = ("place", "place_line", "batch", "craft", "insert", "pickup", "research", "rotate", "recipe")

        def recording_act(action: dict) -> dict:      # sees every place, line and batch, whoever sends it
            res = orig_act(action)
            if self.recorder is not None and res.get("ok"):
                self._record(action, res.get("data") or {})
            t = action.get("type")
            if res.get("ok") and (t in PRODUCTIVE or (t == "extract" and (res.get("data") or {}).get("extracted"))):
                self.last_progress_tick = self.tick or self.last_progress_tick
                if t in ("place", "place_line", "batch"):
                    self.last_place_tick = self.tick or self.last_place_tick
            elif res.get("code") == "inventory_full":
                self.last_full_tick = self.tick or 0
            if not res.get("ok") and action.get("type") not in ("observe", "stats", "scan"):
                self.refused.append({"action": {k: v for k, v in action.items() if k in (
                    "type", "entity", "x", "y", "item", "recipe", "direction")},
                    "code": res.get("code"), "message": (res.get("message") or "")[:100]})
            return res
        run.act = recording_act

    def doing(self, text: str) -> None:
        self.activity = text

    def think(self, label: str, fn, *args, **kwargs):
        """Run a long computation (a factory layout, a model call) on a worker thread. Meanwhile the crew's
        thread keeps the base running (`tend`: a foreman patrol, set by the crew) and the activity line says
        what is being worked out and for how long. fn must not touch the game or this world's live state:
        give it a snapshot. A pause the supervisor asks for (Preempted) still goes through; the worker's result
        is then dropped."""
        pool = ThreadPoolExecutor(max_workers=1)
        fut = pool.submit(fn, *args, **kwargs)
        t0 = time.monotonic()
        try:
            while True:
                done, _ = wait([fut], timeout=2.0)
                if done:
                    return fut.result()
                self.doing(f"{label} ({time.monotonic() - t0:.0f} s)")
                self.tend()
                self.doing(f"{label} ({time.monotonic() - t0:.0f} s)")
        finally:
            pool.shutdown(wait=False)

    def _trace(self, lane: str, node: str | None) -> None:
        self.trace.append({"t": round(time.time(), 2), "minute": round(self.now_tick() / 3600, 2) if self.tick else 0.0,
                           "lane": lane, "node": node})

    def enter(self, stage: str) -> None:
        """The main loop moved to another node."""
        if stage != self.stage:
            self.stage, self.steps = stage, []
            self._trace("main", stage)

    @contextmanager
    def step(self, name: str):
        """A step inside the current node (unlock, make, plates, build, connect, remove...)."""
        self.steps.append(name)
        self._trace("step", name)
        try:
            yield
        finally:
            if self.steps and self.steps[-1] == name:
                self.steps.pop()
            self._trace("step", self.steps[-1] if self.steps else None)

    def foreman(self, step: str | None) -> None:
        if step != self.foreman_step:
            self.foreman_step = step
            self._trace("foreman", step)

    def _record(self, action: dict, data: dict) -> None:
        t = action.get("type")
        if t == "place" and data.get("entity"):
            e = data["entity"]
            self.recorder.append((e["name"], e["x"], e["y"], e.get("direction", "north")))
        elif t == "place_line":
            for x, y in data.get("placed") or []:
                self.recorder.append((action["entity"], x, y, data.get("direction", "north")))
        elif t == "batch":
            for op, r in zip(action.get("ops") or [], data.get("results") or []):
                if r.get("ok"):
                    self._record(op, r.get("data") or {})

    def footprint(self, name: str, x: float, y: float, direction: str = "north") -> set[Tile]:
        w, h = self.kn.size(name)
        if direction in ("east", "west"):
            w, h = h, w
        return {(tx, ty) for tx in range(math.floor(x - w / 2 + 0.01), math.floor(x + w / 2 - 0.01) + 1)
                for ty in range(math.floor(y - h / 2 + 0.01), math.floor(y + h / 2 - 0.01) + 1)}

    def pickup(self, name: str, x: float, y: float, direction: str = "north") -> bool:
        """Take one of our buildings back into the inventory (with whatever it holds)."""
        self.near(x, y)
        self.wait_hands()
        r = self.act({"type": "pickup", "x": x, "y": y})
        if not r["ok"]:
            if r["code"] == "inventory_full":
                raise BuildError("inventory full while removing")
            return False                          # already gone
        self.hands_free_tick = r["data"].get("hands_free_tick", self.now_tick())
        for k, v in (r["data"].get("picked_up") or {}).items():
            self.inventory[k] = self.inventory.get(k, 0) + v
        self.used -= self.footprint(name, x, y, direction)
        self.entities.pop((x, y), None)
        return True

    # ------------------------------------------------------------- clock
    def _seen_tick(self, res: dict) -> None:
        t = res.get("tick")
        if t is not None:
            self.tick, self._tick_wall = int(t), time.monotonic()

    def now_tick(self) -> int:
        return int(self.tick + (time.monotonic() - self._tick_wall) * 60 * self.speed)

    @property
    def minutes_left(self) -> float:
        return (self.run.budgets.get("ticks_left") or 0) / 3600

    def sleep_ticks(self, ticks: float) -> None:
        if ticks > 0:
            time.sleep(ticks / 60 / self.speed)

    def wait_hands(self) -> None:
        self.checkpoint()                     # every mine, chop and craft passes here: a safe point to pause
        self.sleep_ticks(self.hands_free_tick - self.now_tick() + 2)

    # ----------------------------------------------------------- sensing
    def observe(self, radius: int = 32, **opts) -> dict:
        res = self.run.observe(radius, **opts)
        self._seen_tick(res)
        if not res.get("ok"):
            raise BuildError(f"observe failed: {res.get('code')} {res.get('message', '')}")
        d = res["data"]
        self.pos = (d["position"]["x"], d["position"]["y"])
        cx, cy = (d.get("centre") or d["position"])["x"], (d.get("centre") or d["position"])["y"]
        r = d.get("radius", radius)
        amounts = self.__dict__.setdefault("ore_amount", {})   # tile -> ore left there, as of its last look
        if "resources" in d:                      # forget ore tiles that were mined out since the last look
            for tiles in self.resources.values():
                for t in [t for t in tiles if math.hypot(t[0] + 0.5 - cx, t[1] + 0.5 - cy) <= r - 1]:
                    tiles.discard(t)
                    amounts.pop(t, None)
        # Only a full look (water, ore and obstacles all included) makes tiles "seen": a tile marked seen after a
        # trees-only look would count as dry land for good, and the power builder would never find the shore.
        if "obstacles" in d and "water" in d and "resources" in d:
            for tx in range(math.floor(cx - r), math.ceil(cx + r) + 1):
                for ty in range(math.floor(cy - r), math.ceil(cy + r) + 1):
                    if math.hypot(tx + 0.5 - cx, ty + 0.5 - cy) <= r - 1:
                        self.seen.add((tx, ty))
                        self.obstacles.pop((tx, ty), None)
                        self.trees.pop((tx, ty), None)
        for name, tiles in (d.get("resources") or {}).items():
            self.resources.setdefault(name, set()).update(ab.tile(x, y) for x, y, *_ in tiles)
            for x, y, *rest in tiles:            # the game says how much each tile holds: mines plan on it
                if rest and isinstance(rest[0], (int, float)):
                    amounts[ab.tile(x, y)] = int(rest[0])
        self.water.update(ab.tile(x, y) for x, y in d.get("water") or [])
        for x, y, kind in d.get("obstacles") or []:
            if kind == "cliff":
                self.cliffs.update(self._box(x, y, 1.5, 1.5))
                continue
            half_w, half_h = (0.45, 0.45) if kind == "tree" else (1.6, 1.2)
            for t in self._box(x, y, half_w, half_h):
                self.obstacles.add(t, (x, y))       # every one of them: two trees can cover one tile
            if kind == "tree":
                self.trees[ab.tile(x, y)] = (x, y)
        if "entities" in d:
            for e in d["entities"]:
                self.entities[(e["x"], e["y"])] = e
                w, h = self.kn.size(e["name"])
                if e.get("direction") in ("east", "west"):
                    w, h = h, w
                for tx in range(math.floor(e["x"] - w / 2 + 0.01), math.floor(e["x"] + w / 2 - 0.01) + 1):
                    for ty in range(math.floor(e["y"] - h / 2 + 0.01), math.floor(e["y"] + h / 2 - 0.01) + 1):
                        self.used.add((tx, ty))
        if d.get("resource_summary"):
            self.summary = d["resource_summary"]
        self.inventory = d.get("inventory") or {}
        return d

    def look(self, x: float, y: float, radius: int = 32) -> dict:
        """A full look at (x, y): remote if the level's observe range allows it, else walk close first."""
        x, y = float(x), float(y)
        if math.dist((x, y), self.pos) > max(0, getattr(self, "observe_range", 60) - 2):
            self.near(x, y, dist=8)
        return self.observe(radius, x=x, y=y, include=["entities", "resources", "obstacles", "water"])

    def survey_area(self, x: float, y: float, half: int = 40, step: int = 40) -> None:
        """Full looks over a square around (x, y) so every tile in it is really known (shores, ore, trees)."""
        pts = [(x + dx, y + dy) for dx in range(-half, half + 1, step) for dy in range(-half, half + 1, step)]
        for px, py in sorted(pts, key=lambda q: math.dist(q, self.pos)):
            around = [(math.floor(px) + ox, math.floor(py) + oy) for ox in (-14, 0, 14) for oy in (-14, 0, 14)]
            if all(t in self.seen for t in around):
                continue
            try:
                self.look(px, py)
            except BuildError:
                continue

    def explore(self, want: str, max_rings: int = 3, step: float = 150) -> bool:
        """Walk outwards and scan again until a patch of `want` (a resource, or "water") is known.
        Like a player leaving the start area: 8 directions per ring, rings `step` tiles apart."""
        def known() -> bool:
            return bool(self.water_hints) if want == "water" else any(p["name"] == want for p in self.patches)
        if known():
            return True
        if want != "water" and want not in self.kn.resource_names:
            self.log(f"[world] not exploring for {want!r}: no such resource ({self.kn.resource_names})")
            return False                         # walking 450 tiles out for a name that cannot exist
        home = self.pos
        for ring in range(1, max_rings + 1):
            for a in range(8):
                ang = a * math.pi / 4
                self.doing(f"exploring for {want} (ring {ring}, direction {a + 1}/8)")
                self.go(home[0] + ring * step * math.cos(ang), home[1] + ring * step * math.sin(ang))
                self.sleep_ticks(720)            # the game generates and charts the map around us every 10 s
                self.scan()
                if known():
                    return True
        return False

    def refresh(self) -> dict[str, int]:
        """Cheap look: inventory and position only."""
        self.observe(1, include=["entities"])
        return self.inventory

    def scan(self, radius: int | None = None) -> list[dict]:
        """Every patch within the level's scan radius of the character (merged with patches found before)."""
        res = self.run.scan(radius or self.scan_radius)
        self._seen_tick(res)
        if not res.get("ok") and res.get("code") in (None, "radius_too_large"):
            res = self.run.scan(192)
        if res.get("ok"):
            new = res["data"]["patches"]
            # the same patch seen again (its box a tile wider as more of it is charted) replaces the old record:
            # two records of one patch made "the 3 nearest patches" one patch twice
            keep = [p for p in self.patches if not any(p["name"] == q["name"] and _same_patch(p["box"], q["box"])
                                                       for q in new)]
            here = self.pos
            merged: list = []
            for p in sorted(new, key=lambda q: -q.get("tiles", 0)) + keep:     # one record per patch
                if not any(p["name"] == q["name"] and _same_patch(p["box"], q["box"]) for q in merged):
                    merged.append(p)
            self.patches = sorted(merged, key=lambda p: math.dist((p["nearest"]["x"], p["nearest"]["y"]), here))
            w = res["data"].get("water")
            if w:
                self.water_hints.append((w["nearest"]["x"], w["nearest"]["y"]))
        return self.patches

    OIL_RINGS_PER_TRY = 2         # rings walked by the first pumpjack goal; one per goal after that (far rings are
                                  # long walks the boilers wait through), so one goal never walks for an hour
    OIL_MAX_RINGS = 5             # ~2,100 tiles out at scan radius 384: past that, plan without oil

    def oil_step(self) -> float:
        """Tiles between search rings: scans from neighbouring rings overlap (scan radius 384: 345 tiles)."""
        return min(400.0, max(150.0, 0.9 * self.scan_radius))

    def oil_searched(self) -> int:
        """How far from the oil search's home the scans have covered, in tiles."""
        return int(self.oil_rings * self.oil_step() + self.scan_radius)

    def oil_wells(self, near: tuple[float, float] | None = None, more: bool = False) -> list[tuple[float, float]]:
        """Crude-oil wells (each one resource entity, where a pumpjack's centre goes), nearest `near` first.
        Wells sit a few tiles apart, so the default scan (patches of 4+ tiles) misses them: scan for single
        wells, look at the nearest fields to learn every well in them, explore when none is in range.

        The search remembers how far it went: rings around one home (the base), each farther out, with as many
        stops as it takes for the scans to cover the ring. A goal walks at most OIL_RINGS_PER_TRY new rings, so
        the next pumpjack goal goes on where this one stopped instead of walking the same rings again.

        more: the wells known are all taken, so the rings go on outward until a field nobody knows yet shows up
        (run 20261124-015025: two wells known, both pumped for plastic, and chemical science never got crude)."""
        def scan_oil() -> list[dict]:
            res = self.run.scan(self.scan_radius, resources=["crude-oil"], min_tiles=1)
            self._seen_tick(res)
            return (res.get("data") or {}).get("patches") or [] if res.get("ok") else []
        known = set(self.resources.get("crude-oil", ()))

        def fresh(fs: list[dict]) -> list[dict]:        # fields with no well we knew of before
            return [f for f in fs if not any(math.dist(ab.tile(f["nearest"]["x"], f["nearest"]["y"]), k) <= 24
                                             for k in known)]
        fields = scan_oil()
        if more and not fresh(fields):
            fields = []
        if not fields and (more or not self.resources.get("crude-oil")):
            if self.oil_home is None:
                self.oil_home = tuple(near or self.pos)
            home, step = self.oil_home, self.oil_step()
            stop = min(self.OIL_MAX_RINGS, self.oil_rings + (self.OIL_RINGS_PER_TRY if self.oil_rings < 2 else 1))
            while not fields and self.oil_rings < stop:
                ring = self.oil_rings + 1
                r = ring * step
                n = max(8, math.ceil(2 * math.pi * r / (1.6 * self.scan_radius)))   # stops whose scans meet
                start = min(range(n), key=lambda a: math.dist(self.pos, (home[0] + r * math.cos(2 * math.pi * a / n),
                                                                        home[1] + r * math.sin(2 * math.pi * a / n))))
                for k in range(n):
                    a = (start + k) % n
                    self.doing(f"exploring for crude oil (ring {ring}, {r:.0f} tiles out, stop {k + 1}/{n})")
                    try:
                        self.go(home[0] + r * math.cos(2 * math.pi * a / n), home[1] + r * math.sin(2 * math.pi * a / n))
                    except BuildError:                    # water or cliffs in the way: scan from where it got
                        pass
                    self.sleep_ticks(720)                 # the game charts the map around the character
                    fields = scan_oil()
                    if more:
                        fields = fresh(fields)
                    if fields:
                        break
                if not fields:
                    self.oil_rings = ring
                    self.log(f"[world] no crude oil within ~{self.oil_searched()} tiles of the base yet")
        here = near or self.pos
        for f in sorted(fields, key=lambda f: math.dist((f["nearest"]["x"], f["nearest"]["y"]), here))[:2]:
            try:
                self.look(f["centre"]["x"], f["centre"]["y"], radius=16)
            except BuildError:
                self.resources.setdefault("crude-oil", set()).add(ab.tile(f["nearest"]["x"], f["nearest"]["y"]))
        wells = [ab.centre(t) for t in self.resources.get("crude-oil", ())]
        return sorted(wells, key=lambda q: math.dist(q, here))

    STATS_MAX_AGE_S = 60.0          # game seconds: older than this, the planners' view is read again

    def read_stats(self, minutes: float = 5) -> dict:
        res = self.run.stats(minutes=minutes)
        self._seen_tick(res)
        if res.get("ok"):
            d = res["data"]
            self.stats = d
            self.research = d.get("research") or {}
            self.milestones = d.get("milestones") or {}
            self.kn.learn_research(d.get("researched") or [])
            self.stats_tick, self.stats_window = self.now_tick(), minutes
        return self.stats

    def fresh_stats(self, max_age_s: float | None = None) -> dict:
        """The production panel, research and milestones as the planners should see them: read again unless the
        last 5-minute read is under STATS_MAX_AGE_S game seconds old. Otherwise they are only read between goals
        and in waits, so a model asked 20 minutes into a long goal plans from 20-minute-old rates and research
        (run 20261120-180703: the overseer called green science locked 18 minutes after it was researched and
        pushed its factory back again). Read-only: never rate-limited."""
        age = self.now_tick() - getattr(self, "stats_tick", -1e9)
        if age >= (self.STATS_MAX_AGE_S if max_age_s is None else max_age_s) * 60 \
                or getattr(self, "stats_window", 5) != 5:
            self.read_stats()
        return self.stats

    def rate(self, item: str) -> float:
        return float(((self.stats.get("items") or {}).get(item) or {}).get("made_per_min") or 0)

    def patch(self, resource: str, near: tuple[float, float] | None = None, min_tiles: int = 8) -> dict | None:
        here = near or self.pos
        ps = [p for p in self.patches if p["name"] == resource and p["tiles"] >= min_tiles]
        if not ps and self.explore(resource):          # not within scan range: go and find some
            ps = [p for p in self.patches if p["name"] == resource and p["tiles"] >= min_tiles]
        if not ps:
            return None
        return min(ps, key=lambda p: math.dist((p["nearest"]["x"], p["nearest"]["y"]), here))

    # ------------------------------------------------------------- hands
    def act(self, action: dict) -> dict:
        res = self.run.act(action)
        self._seen_tick(res)
        return res

    def go(self, x: float, y: float) -> None:
        if math.hypot(x - self.pos[0], y - self.pos[1]) >= 0.5:
            self.doing(f"walking to ({x:.0f}, {y:.0f})")
        while math.hypot(x - self.pos[0], y - self.pos[1]) > 180:     # one move is at most 200 tiles: walk in legs
            d = math.hypot(x - self.pos[0], y - self.pos[1])
            if not self._step(self.pos[0] + (x - self.pos[0]) * 170 / d, self.pos[1] + (y - self.pos[1]) * 170 / d):
                break
        self._step(x, y)

    def _step(self, x: float, y: float) -> bool:
        """One move. Never onto a building's tile: the game would squeeze the character into a gap between
        buildings that its pathing then cannot leave. Boxed in anyway: take buildings back until a way out
        opens, walk out, put them back."""
        if math.hypot(x - self.pos[0], y - self.pos[1]) < 0.5:
            return True
        x, y = self.stand_spot(x, y)
        far = math.hypot(x - self.pos[0], y - self.pos[1]) > self.SEAL_REACH
        r = self.run.move(x, y)
        if not r.get("ok") and r.get("code") == "no_path" and not self._unsticking:
            if far:
                self._far_fails.append(self.now_tick())
            pattern = self.trap_pattern()
            if pattern or self.boxed_in():
                why = (f"{len(self._recent_far_fails())} far walks refused in a row" if pattern
                       else "no way out within 24 tiles")
                if self._dig_out(why, toward=(x, y)):
                    r = self.run.move(x, y)
        if r.get("ok"):
            p = r["data"]["position"]
            if far or math.dist((p["x"], p["y"]), self.pos) > self.SEAL_REACH:
                self._far_fails.clear()               # a real walk away: not in a pocket
            self.pos = (p["x"], p["y"])
            self.walk_fails = 0
            return True
        self.walk_fails += 1
        self.walk_fail = f"could not walk to ({x:.0f}, {y:.0f}): {r.get('code')}"
        self.log(f"[world] {self.walk_fail}")
        return False

    def why(self, msg: str) -> str:
        """An out-of-reach error usually means the walk there failed: say so, or nobody can tell what to fix."""
        if "out_of_reach" in msg and self.walk_fails and self.walk_fail:
            return f"{msg} (the character {self.walk_fail})"
        return msg

    def stand_spot(self, x: float, y: float) -> tuple[float, float]:
        """Where to stand for (x, y): the spot itself if no building is on it, else the nearest open tile
        beside it (one with open tiles around it too, not a one-tile crack between buildings)."""
        t = ab.tile(x, y)
        if not self.blocked(t):
            return x, y

        def opening(c: Tile) -> int:
            return sum(not self.blocked((c[0] + dx, c[1] + dy)) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))
        best = None
        for r in range(1, 6):
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    if max(abs(dx), abs(dy)) != r:
                        continue
                    c = (t[0] + dx, t[1] + dy)
                    if self.blocked(c):
                        continue
                    cost = math.dist(ab.centre(c), (x, y)) + (4 if opening(c) < 2 else 0)
                    if best is None or cost < best[0]:
                        best = (cost, c)
            if best and best[0] <= r + 1:
                break
        return ab.centre(best[1]) if best else (x, y)

    SEAL_REACH = 12          # a way out is a walk this many tiles away; run 20261119-105322 sealed itself in a
                             # 7x4 pocket of burner mines that a 4-tile look called open

    def sealed(self, extra=frozenset(), start=None) -> bool:
        """Would the character be walled in (by buildings, water, cliffs, plus `extra` tiles) where it stands
        (or at `start`)? Walled in: no walk leads SEAL_REACH tiles away."""
        if start is not None and (start in extra or self.blocked(start)):
            return True                                  # nobody can stand there
        start = start or ab.tile(*self.pos)
        seen, q = {start}, deque([start])
        while q:
            t = q.popleft()
            if abs(t[0] - start[0]) >= self.SEAL_REACH or abs(t[1] - start[1]) >= self.SEAL_REACH:
                return False
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                n = (t[0] + dx, t[1] + dy)
                if n not in seen and n not in extra and not self.blocked(n):
                    seen.add(n)
                    q.append(n)
        return True

    TRAP_FAILS = 4                 # far walks refused within TRAP_WINDOW with no real walk between: trapped
    TRAP_WINDOW = 3 * 3600         # game ticks
    TRAP_REPEAT = 3                # dig-outs within TRAP_REPEAT_WINDOW: the same trap keeps coming back
    TRAP_REPEAT_WINDOW = 20 * 3600

    def _recent_far_fails(self) -> list[int]:
        now = self.now_tick()
        return [t for t in self._far_fails if now - t <= self.TRAP_WINDOW]

    def trap_pattern(self) -> bool:
        """Trapped by the record, whatever a probe says: far walks keep being refused and none got away."""
        return len(self._recent_far_fails()) >= self.TRAP_FAILS

    def _dig_out(self, why: str, toward=None) -> bool:
        """Get out: the map-based dig first, the pick-up-what-is-near fallback second, and the fixer (an LLM)
        when both fail or the same trap keeps coming back. Returns whether a move is worth trying again."""
        now = self.now_tick()
        repeats = sum(1 for t in self.escapes if now - t <= self.TRAP_REPEAT_WINDOW)
        try:
            taken = self.escape(toward)
            self._far_fails.clear()
            note = f"dug out ({why}): took {[t[0] for t in taken]}"
            ok = True
        except BuildError as exc:
            note, ok = f"could not dig out ({why}): {str(exc)[:160]}", False
        self.trap_notes.append({"minute": round(now / 3600, 1), "at": [round(self.pos[0]), round(self.pos[1])],
                                "note": note})
        self.log(f"[world] {note}")
        if (not ok or repeats + 1 >= self.TRAP_REPEAT) and self.on_trapped is not None and not self._escalating:
            self._escalating = True               # the fixer's own walks must not call it again
            try:
                self.on_trapped(f"{note}; {repeats + 1} dig-out(s) in the last "
                                f"{self.TRAP_REPEAT_WINDOW // 3600} game minutes" + ("" if ok else "; still trapped"))
                ok = True
            except BuildError as exc:
                self.log(f"[world] the fixer could not free the character: {str(exc)[:120]}")
            finally:
                self._escalating = False
        return ok

    def local_map(self, r: int = 14) -> str:
        """The ground around the character as text, for the fixer: @ character, letters our buildings (first
        letter of the name), ~ water, # cliff, T tree/rock, . free, ? unseen. Rows top (north) to bottom."""
        cx, cy = ab.tile(*self.pos)
        owner = {}
        for (ex, ey), e in self.entities.items():
            for t in self.footprint(e["name"], ex, ey, e.get("direction", "north")):
                owner[t] = e["name"][0].upper()
        rows = [f"x {cx - r}..{cx + r}, y {cy - r}..{cy + r}"]
        for y in range(cy - r, cy + r + 1):
            line = ""
            for x in range(cx - r, cx + r + 1):
                t = (x, y)
                line += ("@" if t == (cx, cy) else "~" if t in self.water else "#" if t in self.cliffs
                         else owner.get(t) or ("B" if t in self.used else "T" if t in self.obstacles
                                               else "." if t in self.seen else "?"))
            rows.append(line)
        return "\n".join(rows)

    def dig_plan(self, radius: int = 30) -> list[tuple] | None:
        """The fewest of our buildings (and trees or rocks) to take away so the character can walk out: a
        cheapest-path search from where it stands to open ground SEAL_REACH+2 tiles away, where free tiles
        cost nothing and stepping onto another building costs one. Water and cliffs never open. Returns what to
        pick up, nearest first ((name, x, y, direction), or ("obstacle", x, y, "")), [] when already open, or
        None when no way out exists within `radius`."""
        start = ab.tile(*self.pos)
        owner: dict = {}
        for (ex, ey), e in self.entities.items():
            for t in self.footprint(e["name"], ex, ey, e.get("direction", "north")):
                owner[t] = (e["name"], ex, ey, e.get("direction", "north"))
        for t, pos in self.obstacles.items():
            owner.setdefault(t, ("obstacle", pos[0], pos[1], ""))
        goal = self.SEAL_REACH + 2
        best: dict = {start: 0}
        parent: dict = {}
        q = deque([start])
        end = None
        while q:
            t = q.popleft()
            d = best[t]
            if max(abs(t[0] - start[0]), abs(t[1] - start[1])) >= goal and t not in owner and not self.blocked(t):
                end = t
                break
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                n = (t[0] + dx, t[1] + dy)
                if abs(n[0] - start[0]) > radius or abs(n[1] - start[1]) > radius:
                    continue
                if n in self.water or n in self.cliffs:
                    continue
                if n in owner:
                    step = 0 if owner.get(t) == owner[n] else 1
                elif self.blocked(n):
                    step = 1                              # built on, but nothing known to pick up: a guess
                else:
                    step = 0
                if d + step < best.get(n, 1 << 30):
                    best[n], parent[n] = d + step, t
                    (q.appendleft if step == 0 else q.append)(n)
        if end is None:
            return None
        out: list = []
        t = end
        while t != start:
            o = owner.get(t)
            if o is not None and o not in out:
                out.append(o)
            t = parent[t]
        return out[::-1]

    def escape(self, toward=None) -> list:
        """Out of a pocket for good: look at what stands around, take away the fewest buildings that open a way
        out (dig_plan), and leave that door open (kept free of new buildings), so the character cannot be shut
        in at the same spot again. The pick-up-what-is-near dig (unstuck) when the map shows no way. Raises
        BuildError when still trapped."""
        self._unsticking = True
        self.escapes.append(self.now_tick())
        try:
            try:
                self.observe(24, include=["entities", "obstacles", "water"], detail="brief")
            except BuildError:
                pass
            plan = self.dig_plan()
            before = self.unstucks
            if not plan:
                self._unsticking = False
                return self.unstuck()
            self.log(f"[world] trapped at ({self.pos[0]:.0f}, {self.pos[1]:.0f}): opening a door through "
                     f"{[p[0] for p in plan]}")
            taken, door = [], set()
            for name, x, y, dirn in plan:
                if math.dist((x, y), self.pos) > ab.REACH - 1.5:
                    self.near(x, y, dist=ab.REACH - 2)    # inside the pocket: walks there work
                if name == "obstacle":
                    # the tiles this one covers open only when every tree or rock on them is gone
                    covers = [t for t, on in self.obstacles._every().items() if (x, y) in on]
                    for q in self.obstacles.on(covers) or [(x, y)]:
                        if math.dist(q, self.pos) > ab.REACH - 1.5:
                            self.near(*q, dist=ab.REACH - 2)
                        r = self.run.pickup(*q)
                        door.update(self.obstacles.remove(q))
                        if r.get("ok"):
                            taken.append(("tree or rock", q[0], q[1], ""))
                    continue
                if self.pickup(name, x, y, dirn):
                    taken.append((name, x, y, dirn))
                    door |= self.footprint(name, x, y, dirn)
            if self.boxed_in():
                # the map was wrong (something it does not show): pick up what is near until a way opens
                self.log(f"[world] the door through {[t[0] for t in taken]} did not open a way: digging by hand")
                self._unsticking = False
                try:
                    taken += [t[:4] for t in self.unstuck()]
                except BuildError as exc:
                    raise BuildError(f"took {[t[0] for t in taken]} and the character at ({self.pos[0]:.0f}, "
                                     f"{self.pos[1]:.0f}) still cannot get out: {str(exc)[:80]}") from None
            self.keepout |= door                          # the door stays open: nothing is built in it again
            if self.unstucks == before:                   # (a hand dig counted itself)
                self.unstucks += 1
            if self.on_dug is not None:
                self.on_dug([t for t in taken if t[0] != "tree or rock"])
            return taken
        finally:
            self._unsticking = False

    def boxed_in(self) -> bool:
        """Ask the game: can the character get away from here? A probe 7 tiles out that works only proves the
        space is not tiny; then probes 24 tiles out. Run 20261119-105322 sat in a 7x4 pocket of its own burner
        mines for good: a 7-tile probe walked to the pocket's other end, so the dig-out never ran."""
        refused = False
        for dist in (7, 24):
            moved = False
            for a in range(8):
                px = self.pos[0] + dist * math.cos(a * math.pi / 4)
                py = self.pos[1] + dist * math.sin(a * math.pi / 4)
                r = self.run.move(px, py)
                if r.get("ok"):
                    p = r["data"]["position"]
                    self.pos = (p["x"], p["y"])
                    moved = True
                    break
                refused = refused or r.get("code") == "no_path"
            if not moved:
                return refused
        return False

    def unstuck(self) -> list:
        """Walled in by our own buildings: pick up the nearest ones until a way out opens, then put them back
        from outside (fuel included). Raises BuildError if nothing opens a way."""
        self._unsticking = True
        taken: list = []
        try:
            self.log(f"[world] boxed in at ({self.pos[0]:.1f}, {self.pos[1]:.1f}): taking buildings back to get out")
            d = self.observe(8, include=["entities"])
            near = sorted((e for e in d.get("entities") or []
                           if math.dist((e["x"], e["y"]), self.pos) <= ab.REACH - 2.5),
                          key=lambda e: math.dist((e["x"], e["y"]), self.pos))
            free = False
            for e in near[:8]:
                dirn = e.get("direction", "north")
                fuel = dict((e.get("inventories") or {}).get("fuel") or {})
                if self.pickup(e["name"], e["x"], e["y"], dirn):
                    taken.append((e["name"], e["x"], e["y"], dirn, fuel))
                    if not self.boxed_in():
                        free = True
                        break
            if not free:
                raise BuildError(f"the character is walled in at ({self.pos[0]:.0f}, {self.pos[1]:.0f}) and "
                                 "picking up the buildings around it did not open a way out")
            self.unstucks += 1
            for name, x, y, dirn, fuel in taken:          # from outside now: put them back where they were
                try:
                    self.place(name, x, y, dirn, self.footprint(name, x, y, dirn))
                    for item, n in fuel.items():
                        self.insert(x, y, item, n)
                except BuildError as exc:
                    self.log(f"[world] could not put {name} back at ({x}, {y}): {exc}")
            self.log(f"[world] out again; put back {[t[0] for t in taken]}")
            return taken
        finally:
            self._unsticking = False

    def mine(self, resource: str, count: int) -> int:
        """Hand-mine `count` of a resource from the nearest known tile of it. Returns how many were mined."""
        self.doing(f"hand-mining {count} {resource}")
        tiles = self.resources.get(resource)
        if not tiles:
            p = self.patch(resource, min_tiles=1)
            if not p:
                raise BuildError(f"no {resource} known")
            self.go(p["nearest"]["x"], p["nearest"]["y"])
            self.observe(16)
            tiles = self.resources.get(resource)
            if not tiles:
                raise BuildError(f"no {resource} tiles near {p['nearest']}")
        got = 0
        tries = 0
        skip: set[Tile] = set()               # tiles this call could not get to (walled in, under buildings)
        last = ""
        while got < count and tries < 12:
            tries += 1
            # ore under our own buildings cannot be hand-mined, and the nearest free tile may be walled in:
            # try the next one instead of failing the whole goal on one tile
            cands = [q for q in tiles if q not in skip and q not in self.used] or [q for q in tiles if q not in skip]
            if not cands:
                break
            t = min(cands, key=lambda q: math.dist(ab.centre(q), self.pos))
            x, y = ab.centre(t)
            if math.dist((x, y), self.pos) > 2.2:
                d = math.dist((x, y), self.pos)
                f = (d - 1.5) / d
                self.go(self.pos[0] + (x - self.pos[0]) * f, self.pos[1] + (y - self.pos[1]) * f)
            self.wait_hands()
            self.doing(f"hand-mining {resource} ({got}/{count})")
            n = min(100, count - got)
            r = self.act({"type": "mine", "x": x, "y": y, "count": n})
            if not r["ok"]:
                if r["code"] == "no_resource":
                    tiles.discard(t)
                    if not tiles:
                        break
                    continue
                if r["code"] == "inventory_full":
                    raise BuildError("inventory full")
                if r["code"] == "out_of_reach":   # the walk there failed: another tile, maybe from another side
                    skip.add(t)
                    last = self.why(f"mine {resource}: {r['code']} {r.get('message', '')}")
                    continue
                raise BuildError(self.why(f"mine {resource}: {r['code']} {r.get('message', '')}"))
            n_got = sum((r["data"].get("mined") or {}).values())
            got += n_got
            self.hands_free_tick = r["data"].get("hands_free_tick", self.now_tick())
            self.wait_hands()
            for k, v in (r["data"].get("mined") or {}).items():
                self.inventory[k] = self.inventory.get(k, 0) + v
        if not got and skip:
            raise BuildError(f"{last}; tried {len(skip)} {resource} tiles and could reach none of them")
        return got

    def find_trees(self, walk: bool = True) -> bool:
        """Know some trees: look around remotely (up to the observe range), then walk outwards if allowed."""
        if self.trees:
            return True
        self.doing("looking for trees")
        ring = [(0, 0)] + [(56 * math.cos(a * math.pi / 4), 56 * math.sin(a * math.pi / 4)) for a in range(8)]
        for dx, dy in ring:
            try:
                self.look(self.pos[0] + dx, self.pos[1] + dy)
            except BuildError:
                continue
            if self.trees:
                return True
        if not walk:
            return False
        start = self.pos
        for a in range(8):                       # walk out 110 tiles in each direction until trees show up
            tx, ty = start[0] + 110 * math.cos(a * math.pi / 4), start[1] + 110 * math.sin(a * math.pi / 4)
            self.go(tx, ty)
            for dx, dy in ring[:1] + ring[1::2]:
                try:
                    self.look(self.pos[0] + dx, self.pos[1] + dy)
                except BuildError:
                    continue
            if self.trees:
                return True
        return False

    def trees_near(self, radius: float) -> bool:
        """A known tree within `radius` tiles of the character (no walking, no looking)."""
        return any(math.dist(p, self.pos) <= radius for p in self.trees.values())

    def chop(self, wood: int, within: float | None = None) -> int:
        """Cut trees until we have `wood` more wood (each tree gives about 4).
        within: only trees this close to where we stand (never walk off looking for more); None: any tree."""
        self.doing(f"chopping trees for {wood} wood")
        got = 0
        tries = 0
        here = self.pos
        while got < wood and tries < wood + 10:
            tries += 1
            if within is not None:
                if not any(math.dist(p, here) <= within for p in self.trees.values()):
                    if got:
                        break                         # the near trees are gone: what we have will do
                    raise BuildError(f"no trees within {within:.0f} tiles")
            elif not self.trees and not self.find_trees():
                raise BuildError("no trees found within ~170 tiles")
            near = {t: p for t, p in self.trees.items() if within is None or math.dist(p, here) <= within}
            t = min(near, key=lambda q: math.dist(ab.centre(q), self.pos))
            pos = self.trees.pop(t)
            self.obstacles.remove(pos)
            self.near(*pos, dist=8)
            self.wait_hands()
            r = self.act({"type": "pickup", "x": pos[0], "y": pos[1]})
            if r["ok"]:
                g = (r["data"].get("picked_up") or {}).get("wood", 0)
                got += g
                self.inventory["wood"] = self.inventory.get("wood", 0) + g
                self.hands_free_tick = r["data"].get("hands_free_tick", self.now_tick())
        self.wait_hands()
        return got

    def craft(self, recipe: str, times: int) -> None:
        left = times
        while left > 0:
            n = min(500, left)
            self.wait_hands()
            self.doing(f"crafting {n} x {recipe}")
            r = self.act({"type": "craft", "recipe": recipe, "count": n})
            if not r["ok"]:
                raise BuildError(f"craft {n} {recipe}: {r['code']} {r.get('message', '')}")
            for k, v in (r["data"].get("crafted") or {}).items():
                self.inventory[k] = self.inventory.get(k, 0) + v
            for ing in self.kn.recipes[recipe]["ingredients"]:
                self.inventory[ing["name"]] = max(0, self.inventory.get(ing["name"], 0) - ing["amount"] * n)
            self.hands_free_tick = r["data"].get("hands_free_tick", self.now_tick())
            left -= n
        self.wait_hands()

    MOVE_MAX = 5000                 # the API's most items one insert or extract moves (models.Insert/Extract)

    def insert(self, x: float, y: float, item: str, count: int) -> int:
        """Put up to count of an item in, in calls of at most MOVE_MAX (a bigger count is refused outright)."""
        done = 0
        while True:
            have = self.inventory.get(item, 0)
            want = min(have, int(count) - done, getattr(self, "MOVE_MAX", 5000))
            if want <= 0:
                return done
            self.near(x, y)
            r = self.act({"type": "insert", "x": x, "y": y, "item": item, "count": want})
            if not r["ok"]:
                if r["code"] == "not_in_inventory":
                    self.inventory[item] = 0
                return done
            n = r["data"].get("inserted", 0)
            self.inventory[item] = max(0, have - n)
            done += n
            if n < want:                      # it is full
                return done

    def extract(self, x: float, y: float, item: str, count: int) -> int:
        """Take up to count of an item out, in calls of at most MOVE_MAX. Run 20261120-211555: emptying a steel
        column's chests asked for over 5000 at once, the API refused, and retiring the old site failed."""
        done = 0
        while True:
            want = min(int(count) - done, getattr(self, "MOVE_MAX", 5000))
            if want <= 0:                     # the server refuses a count under 1 (it failed whole goals)
                return done
            self.near(x, y)
            r = self.act({"type": "extract", "x": x, "y": y, "item": item, "count": want})
            if not r["ok"]:
                return done
            n = r["data"].get("extracted", 0)
            self.inventory[item] = self.inventory.get(item, 0) + n
            done += n
            if n < want:                      # nothing more there (or no room)
                return done

    def fuel(self, x: float, y: float, count: int, current: dict | None = None) -> bool:
        """Burner fuel, in coal-equivalents: coal if we have it, else twice as much wood (half the energy).
        current: what the fuel slot holds now; a slot with coal in it cannot take wood, and the other way round."""
        held = [k for k, v in (current or {}).items() if v > 0]
        for item in (held or ["coal", "wood"]):
            if self.inventory.get(item, 0) > 0:
                return self.insert(x, y, item, count if item == "coal" else count * 2) > 0
        return False

    def fuel_units(self) -> int:
        """Fuel on hand in coal-equivalents."""
        return self.inventory.get("coal", 0) + self.inventory.get("wood", 0) // 2

    def place(self, entity: str, x: float, y: float, direction: str = "north", tiles=()) -> dict:
        if self.inventory.get(entity, 0) <= 0:
            self.refresh()
            if self.inventory.get(entity, 0) <= 0:
                raise BuildError(f"no {entity} in inventory")
        self.doing(f"placing {entity} at ({x:.0f}, {y:.0f})")
        if tiles:
            self.near(x, y)
            if self.sealed(set(tiles)) and not self.sealed():    # this building would wall the character in
                self.step_out(tiles)
        belt = self.belt_opts(entity)
        try:
            ent = super().place(entity, x, y, direction, tiles, **belt)
        except BuildError as exc:
            if "would_merge" in str(exc):           # the guard refused a join nobody planned: the caller routes
                raise BuildError(str(exc)) from None    # around this tile (lay_belt marks it), no retry here
            if "cannot_place" not in str(exc) or (entity == "offshore-pump" and not tiles):
                raise BuildError(self.why(str(exc))) from None      # a pump is refused for the shore's shape
            # often a second rock or tree on the tile the last look missed: look again (the obstacles there
            # become known), and place once more (place clears the tiles first). A caller that gave no tiles
            # gets the building's own footprint cleared (run 20261122-133537: a tree beside a factory's port)
            retry_tiles = set(tiles) or self.footprint(entity, x, y, direction)
            try:
                self.relook(retry_tiles)
            except BuildError:
                pass
            try:
                ent = super().place(entity, x, y, direction, retry_tiles, **belt)
            except BuildError as exc2:
                raise BuildError(self.why(str(exc2))) from None
        self.inventory[entity] = max(0, self.inventory.get(entity, 0) - 1)
        self.entities[(ent["x"], ent["y"])] = ent
        return ent

    # ------------------------------------------------------------------ belt jobs
    BELT_ENTITIES = ("transport-belt", "underground-belt", "splitter")
    belt_guard = True               # off: belts are laid as before (no tags, no isolation), e.g. an older server

    @contextmanager
    def belt_job(self, carries, route=(), joins=()):
        """The belts placed inside are one job: a link, a fuel strip, a plate line. Each is tagged with what it
        carries (observe flags anything else on it) and laid isolated: the game refuses any join with a belt
        outside the job except at the declared joins (a planned side-load, the factory belt a feed runs into,
        the strip a coal link continues). Run 20261120-211555: three fuel strips' dead ends pointed into the side
        of iron belts, and from minute 108 their spare coal ran onto the iron feeds of two gear factories and
        red science. Route and joins are tiles (tx, ty)."""
        prev = getattr(self, "_belt_job", None)
        self._belt_job = {"carries": sorted({c for c in carries if c}),
                          "allow": {(int(t[0]), int(t[1])) for t in list(route) + list(joins)}}
        try:
            yield self._belt_job
        finally:
            self._belt_job = prev

    def belt_at(self, t) -> bool:
        """A belt of ours was last seen on this tile."""
        e = self.entities.get((t[0] + 0.5, t[1] + 0.5))
        return bool(e) and e.get("name") in self.BELT_ENTITIES

    def belt_opts(self, entity: str) -> dict:
        """What a belt placed now passes to the game: its job's tag and guard ({} outside a job)."""
        job = getattr(self, "_belt_job", None)
        if job is None or entity not in self.BELT_ENTITIES or not self.belt_guard:
            return {}
        opts = {"isolated": True, "allow": [[x, y] for x, y in sorted(job["allow"])]}
        if job["carries"]:
            opts["carries"] = job["carries"]
        return opts

    def free(self, tiles, need_seen: bool = True) -> bool:
        """Free to build on. A storage chest's tile counts as free: the chest moves when the build clears it.
        Lanes (margins kept for belts) are not free for buildings."""
        if self.lanes and any(t in self.lanes for t in tiles):
            return False
        return self._free_ground(tiles, need_seen)

    def free_belt(self, t) -> bool:
        """Free for a belt: like free, but a lane is exactly where belts belong."""
        return self._free_ground((t,), True)

    def _free_ground(self, tiles, need_seen: bool) -> bool:
        if not self.movable:
            return super().free(tiles, need_seen)
        return all((not self.blocked(t) or t in self.movable) and t not in self.keepout
                   and (t in self.seen or not need_seen) for t in tiles)

    def relook(self, tiles) -> None:
        """A look at what stands on and around these tiles (remote: no walk), so their trees and rocks are known."""
        tiles = list(tiles)
        xs, ys = [t[0] for t in tiles], [t[1] for t in tiles]
        r = int(max(max(xs) - min(xs), max(ys) - min(ys)) / 2) + 4
        self.observe(min(32, r), x=(min(xs) + max(xs)) / 2 + 0.5, y=(min(ys) + max(ys)) / 2 + 0.5,
                     include=["entities", "obstacles"])

    def clear(self, tiles) -> None:
        """Trees and rocks off these tiles, and any storage chest on them moved elsewhere (contents and all)."""
        tiles = set(tiles)
        if self.make_way is not None and tiles & self.movable:
            self.make_way(tiles)
        super().clear(tiles)

    def step_out(self, tiles) -> None:
        """Walk off a footprint we are about to build on, to a spot that is still open once it stands (the
        nearest ring tile could be inside the pocket the building closes)."""
        tiles = set(tiles)
        xs, ys = [t[0] for t in tiles], [t[1] for t in tiles]
        for m in (2, 4, 7):
            x0, x1, y0, y1 = min(xs) - m, max(xs) + m, min(ys) - m, max(ys) + m
            ring = [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)
                    if (x in (x0, x1) or y in (y0, y1)) and not self.blocked((x, y))]
            for t in sorted(ring, key=lambda r: math.dist(ab.centre(r), self.pos)):
                if math.dist(ab.centre(t), (sum(xs) / len(xs) + 0.5, sum(ys) / len(ys) + 0.5)) > ab.REACH - 1:
                    continue                                      # it must still reach the building from there
                if not self.sealed(tiles, start=t):
                    self.go(*ab.centre(t))
                    return
        super().step_out(tiles)

    def free_box(self, x0: int, y0: int, w: int, h: int) -> bool:
        return self.free({(x, y) for x in range(x0, x0 + w) for y in range(y0, y0 + h)})
