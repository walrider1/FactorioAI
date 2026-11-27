"""Scheduler, crew and foreman: everything that turns the strategist's goals into game actions.

There is one character, so these share it and take turns on one thread:

    Crew.run_goal(goal)   a goal from the strategist -> unlock what it needs (trigger techs, research, the
                          recipes its machines will run), craft its kit, build it, wire it to power and check
    Foreman.patrol()      every half minute or so: restock coal, look at every site (fuel, power, depleted ore,
                          starved machines), refuel, re-wire unpowered sites, feed labs

Goals (what the strategist, the base's general manager, may ask for):
    {"do": "build", "module": <name>, ...module args}   see modules.MODULES
    {"do": "research", "tech": <technology>}            queues it and every missing prerequisite
    {"do": "stock", "item": <item>, "count": n}          hand-craft items to hold
    {"do": "remove", "site": <id>, "force": false}       pick a site's buildings back up (guarded)
    {"do": "replace", "site": <id>, "with": <goal>}      build the new thing first, then remove the old site
    {"do": "connect", "site": <id>}                      wire a site to the power network
    {"do": "wait", "seconds": n}                         let the base run (the foreman keeps patrolling)
    {"do": "feed", "site": <id>, "item": "coal", "count": n}   put fuel (or any item) into a site now
    {"do": "priority", "sites": [<id>|<kind>|<resource>]}      what the foreman tends first after coal and power
"""

from __future__ import annotations

import math
import time
from collections import Counter, defaultdict
from contextlib import contextmanager

from arena_sdk import ArenaError

from .knowledge import PLATES, SMELTED
from .modules import ELECTRIC, GENERATORS, MODULES, UNPLUGGED, Base, Site, coal_belts_ready, lines_ready
from .power import MEDIUM_REACH, REACH, route_poles
from .upkeep import ORE_LOOK_MIN
from .world import BuildError, FreeWorld, Preempted

FUEL = {"coal": 1, "wood": 1}
DEAD_RUN = ("run_not_active", "run_voided")      # the run is over: stop, never retry
SCIENCE = ("automation-science-pack", "logistic-science-pack", "military-science-pack", "chemical-science-pack",
           "production-science-pack", "utility-science-pack")
POLE = "small-electric-pole"
ELECTRIC_TECH = "advanced-material-processing-2"    # electric furnaces: researched first once blue science is made
MEDIUM_POLE = "medium-electric-pole"
POLES = (POLE, MEDIUM_POLE)
LONG_LINE = 40               # tiles: a bridge this long uses medium poles once they can be made
BIG_POLE = "big-electric-pole"
BIG_LINE = 60                # tiles: a line this long goes over big poles once they can be made (30-tile wires)
BIG_STEP = 26.0              # tiles between big poles aimed for (the wire spans 30: room to dodge an obstacle)
BIG_WIRE = 29.0              # tiles between two big poles' centres at most
PLATE_OF = {"iron-ore": "iron-plate", "copper-ore": "copper-plate", "stone": "stone-brick"}
INVENTORY_SLOTS = 80       # the character's main inventory
KEEP_ON_HAND = {"iron-plate": 400, "copper-plate": 300, "stone-brick": 100, "coal": 200, "wood": 100, "stone": 100,
                "iron-ore": 50, "copper-ore": 50}     # anything over this goes to storage when space runs low
KEEP_MINUTES = 10          # a working site younger than this is not removed without force: removing and
                           # rebuilding the same thing minutes apart is the churn a general manager must avoid
TREES_NEAR = 30            # wood is fuel only before the first coal mine, and only from trees this close
MINE_WAIT_MIN = 4.0        # wait up to this many game minutes for our own drills before mining by hand
CELL_WAIT_MIN = 12.0       # ... and this long for workshop cells to make a kit item (nothing else makes it)
STALL_MIN = 15.0           # a goal that builds, crafts and moves nothing for this long is dropped (stall guard)
# A build still putting buildings down is finished, not dropped at its budget: up to BUILD_OVERRUN_MAX times the
# budget while something was placed in the last PLACE_GRACE_MIN game minutes. Run 20261120-211555: a blue
# science workshop was 34 minutes into its 40, still placing (16 assemblers, 400 pipe and belt ends); dropped,
# its cells would have become an unfinished site nobody counts, and a rebuild would hit the same 40 minutes.
BUILD_OVERRUN_MAX = 2.5
PLACE_GRACE_MIN = 8.0
# Game minutes a goal may take before it is stopped (at a moment nothing is half-placed) and recorded as a
# failure with the step it was stuck in, so the planners and the overseer see it and decide again. Run
# 20261119-202811: five goals took 86% of 114 game minutes (a pumpjack 34+, a mine replacement 28, a link pass 18).
EXPECTED_LOAD = 0.7     # share of their full load the sites with every input fed draw once they run (power_status)
GOAL_BUDGET_MIN = {"build": 25.0, "replace": 30.0, "remove": 8.0, "link": 10.0, "link_science": 10.0, "stock": 8.0,
                   "act": 15.0,
                   "modules": 15.0,
                   "feed": 4.0, "connect": 6.0, "upgrade": 20.0, "repair": 8.0, "repipe": 8.0, "scout": 20.0, "design": 4.0,
                   "priority": 1.0, "purge": 10.0, "relink": 12.0, "trunk": 40.0,
                   "bus": 30.0}                 # a zone segment's bus (zones.BUS_BUDGET_MIN)
RESEARCH_BUDGET_MIN = 25.0
ELECTRIC_SWAP_EXTRA_MIN = 15.0   # an electric furnace swap's budget on top: crafting its furnaces (advanced circuits)
# (scout: 20 minutes, so one oil search ring ~700 tiles out is walked whole; a ring cut short is walked again)
MODULE_BUDGET_MIN = {"outpost": 45.0, "workshop": 40.0, "silo": 60.0, "factory": 30.0, "steam_power": 15.0,
                     "research_station": 15.0, "electric_mine": 12.0, "burner_mine": 8.0, "pumpjack": 15.0,
                     "smelter_column": 15.0, "labs": 8.0}
COURIER_GAP_S = 45         # game seconds between the courier's rounds of the workshop
COURIER_BUFFER_MIN = 8.0   # each cell's input chest holds this many minutes of its inputs
HAND_LIMIT_S = 120         # crafting by hand beats building a factory only below this many game seconds of
                           # crafting (10 red packs ~ 55 s: by hand; 100 green ~ 750 s: a factory first)
AUTO_RETRY_MIN = 15        # after an automation build fails, hand-craft that item for this long, then try again
LEAN_GAP_S = 60           # while a build runs: at most one foreman patrol per game minute
LEAN_REACH = 40            # ... refuelling what is near the character
LEAN_REVISIT = 3 * 3600    # ... and far sites only when not looked at for 3 game minutes (8 coal ~ 3.5 min)
LEAN_REVISIT_CRITICAL = 90 * 60   # ... but coal mines and the power station every 1.5 game minutes
# The patrol looks where hands can help: a hand-fed site every round. A site that runs without hands (electric, or
# fuel by belt) only has its health read for the planners, so it is looked at rarely; a sealed one that was fine
# rarer still. Run 20261120-211555: ~17 looks every game minute, most at sites the foreman could do nothing for.
AUTOMATED_REVISIT = 6 * 3600
SEALED_REVISIT = 15 * 3600
FAR_REVISIT = 5 * 3600            # a hand-fed site out of observe range: walked to at most this often
# Spare parts carried for repairs and rebuilds. Run 20261121-140755: repairs spent most of their time gathering
# (a relink 21 game minutes, an upgrade 21, a replace 28 "collecting plates"), so the factories they were meant to
# fix starved meanwhile. The foreman tops these up in quiet patrols, only from plates already on hand or in
# storage (never by hand-mining), so a repair starts with its parts in the inventory.
SPARES = {"transport-belt": 100, "underground-belt": 10, "inserter": 16, "long-handed-inserter": 6,
          "small-electric-pole": 16, "pipe": 20, "electric-mining-drill": 4, "stone-furnace": 6}
SPARES_EVERY_MIN = 4.0
SPARES_CAP = 3             # more than this many times a spare part on hand goes into storage (run 20261121-140755
                           # carried 800 underground belts and still crafted more)
FINE = ("working", "normal", "waiting_for_space_in_destination")   # health that needs nobody
SCARCE_FUEL = 40           # below this much fuel on hand, coal goes to coal mines and power first
FEEDER_TARGET = 200        # packs of each kind the foreman keeps in a lab row's feeder chest (refilled at half)
BOILER_CHEST_TARGET = 60   # coal the foreman keeps in each boiler's chest (a boiler at full load burns 27/min)
IDLE = {"no_ingredients", "item_ingredient_shortage", "waiting_for_source_items", "no_input_fluid",
        "fluid_ingredient_shortage", "waiting_for_space_in_destination", "full_output"}


class Crew:
    def __init__(self, world: FreeWorld, log=print):
        self.w, self.kn, self.log = world, world.kn, log
        self.base = Base()
        self.foreman = Foreman(self)
        self.removed: list[dict] = []      # what was taken down, when and why (the strategist sees it)
        self.removals = 0                  # sites removed so far (`removed` keeps only the last 10)
        self.watchdog = None
        self.target_techs: list[str] = []  # the level's milestone technologies, in order (auto-research aims here)
        self.churn: list[str] = []         # rebuilt soon after removing the same kind of thing
        self._in_coal_first = False
        self._progress: tuple | None = None   # (technology, progress, tick) when research last moved
        # the supervisor's hold on the crew (see assess.py): urgent goals go before the plan, and the goal in
        # hand may be paused at a safe point (preempt) and resumed after them
        self.supervisor = None
        self.urgent: list[dict] = []
        self.overseer_forbid: list[dict] = []   # goals the overseer refused for a while ({match, until, why})
        self.overseer_guidance: dict | None = None   # its standing word to the planners
        self.overseer_plan_tick = -1e9          # when it last replaced the plan
        self.drop_current = False               # its interrupt also drops the goal in hand
        self.preempt_for = None                 # the goal (its start tick) a pause was asked for: a pause outlives
                                                # nothing: when that goal ends first, the pause is dropped
        self.preempt_force = False              # its interrupt: urgent goals too, at the next moment nothing is
                                                # half-placed (not only at the end of a build step)
        self.strategist = None                  # bot.play sets it (the overseer may rewrite its doctrine)
        self.preempt: str | None = None
        self.current: dict | None = None      # {"goal", "tick", "urgent"} while a goal runs
        self.plan_queue: list[dict] = []      # the strategist's goals still to do (the main loop keeps it)
        self._bad_storage: set[tuple[int, int]] = set()   # tiles the game refused a storage chest on
        self.storage_held: dict[tuple, dict[str, int]] = {}   # what each storage chest holds (the crew's book)
        self.yard: tuple[int, int] | None = None          # top-left tile of the storage yard
        self.kit_hold: dict[str, int] = {}                # the kit of the build in hand: never left in storage
        self.outpost_hold: dict[str, int] = {}            # an outpost's column, trunk and poles, until they stand
        self.reserved: dict[str, int] = {}                # what the goal in hand is gathering (raw plates, the iron
                                                          # for its steel): the courier and storage leave it alone
        self._claim: set = set()                          # tiles a build is clearing (no storage goes there)
        self._leaving: set = set()                        # storage chests being emptied to move them
        self._courier_tick = -10 ** 9
        self.cell_stock: dict[str, int] = {}     # what the cells' output chests held at the courier's last look
        self.cell_short: dict[str, int] = {}     # inputs the courier could not find at its last round
        self.hand_allow: dict[str, int] = {}     # science packs the foreman may hand-craft for the current research
        self._auto_failed: dict[str, float] = {}   # item -> game minute its automation build last failed
        self._factory_failed: dict[str, float] = {}   # item -> game minute its belt factory (fab) last failed
        self.science_links: dict[tuple, dict] = {}    # science source chest -> the station port its belt feeds
        self.links: list[dict] = []                   # every belt link: source chest -> the site it feeds
        self.failed_suppliers: dict[str, float] = {}  # item -> game minute a build to supply a port last failed
        self._link_failed: dict[tuple, float] = {}    # source chest -> game minute its belt route last failed
        self._link_fails: dict[tuple, dict] = {}      # port end / science source -> {n, reason, minute, base}
        self._bad_belt: set[tuple[int, int]] = set()  # tiles the game refused a link's belt on
        self._patch_full: dict[str, tuple] = {}         # resource -> (minute, base) a mine found no room on its patches
        self.sealed: dict[int, float] = {}            # site id -> minute it was sealed (ran on its own; upkeep.py)
        self.healthy_since: dict[int, float] = {}     # site id -> minute since it runs on its own
        self.last_interrupt = -1e9                    # game minute the supervisor last paused a goal
        self.closeout_queue: list[dict] = []          # the planned trip back before the next tier (upkeep.py)
        self.lifted_links: list[dict] = []            # links whose inserter is taken up while the crew collects
                                                      # plates for a build (put back right after, see _plates)
        self.idle_since: dict[int, float] = {}        # site id -> minute its output stopped being used (upgrade.py)
        self.site_lanes: dict[int, list] = {}         # site id -> the margin tiles kept for belts around it
        self.fuel_strips: dict[int, list] = {}        # hand-fed furnace row -> the tiles its coal belt will need
        self.zones = None                             # the hybrid floorplan (zones.py): reserved land, era segments;
        world.zones = None                            # planned at the first electric build (zones.after_build)
        try:
            MODULES["factory"].zones = None           # a fresh crew: no segment of an earlier crew's world
        except Exception:  # noqa: BLE001
            pass
        self.retrofit_blocked: dict[int, float] = {}  # row -> minute its coal belt found its strip taken
        world.checkpoint = self.checkpoint
        world.tend = lambda: self.foreman.patrol(force=True)   # while a layout is worked out in the background
        world.make_way = self.make_way                    # a build that needs a storage chest's tile moves it
        world.obtainable = self.can_obtain                # can the crew get n of an item (a design asks first)
        world.on_dug = self.forget_dug                    # a dig-out took buildings: their sites forget them

    # ============================================================ supervisor
    # steps that place or take buildings, and designs (minutes of layout search a pause would throw away):
    # never paused; the foreman still keeps fuel and power going while a design runs (FreeWorld.think)
    PINNED = ("build", "remove", "connect", "storage", "design")

    def can_pause(self) -> bool:
        return bool(self.current) and not self.current.get("urgent") and not any(
            st in self.PINNED for st in self.w.steps)

    def checkpoint(self) -> None:
        """A safe point in a long goal: let the supervisor look, and pause the goal if it asked to. A goal that
        has made no progress for STALL_MIN game minutes (nothing built, crafted, picked up, put in or taken
        out: only walking and refused actions) is dropped: a loop like that never ends by itself (run
        20261119-202811: 66 game minutes of walking between coal chests with a full inventory)."""
        if getattr(self, "supervisor", None) is not None:
            self.supervisor.tick(self)
        if getattr(self, "_in_lane", False):            # an upkeep job inside a goal: no pause, no lane in a lane
            return
        if not getattr(self, "current", None):          # between goals (or a bare crew in a test): no pause
            return
        self._upkeep_lane()
        if self.preempt and self.preempt_for is not None and self.current.get("tick") != self.preempt_for:
            self.clear_pause()                        # asked for a goal that has ended: not for this one
        if self._stalled():
            self.drop_current = True
            self.w.last_progress_tick = self.w.now_tick()
            raise Preempted(f"stalled: no progress for {STALL_MIN:.0f} game minutes ({self.w.activity[:120]})")
        over = self._over_budget()
        if over and self._safe_now():
            self.drop_current = True
            raise Preempted(over)
        if self.preempt and (self.can_pause() or (self.preempt_force and self._safe_now())):
            why, self.preempt, self.preempt_force, self.preempt_for = self.preempt, None, False, None
            raise Preempted(why)

    # The upkeep lane (docs/hybrid-plan.md, section 4): the rule floor's fuel feeds run inside a long goal at its
    # safe points, within a time slice, instead of waiting for the goal to end or pausing it. Run 20261125-214645:
    # two iron columns and two copper rows ran out of coal behind a 96-minute build, iron fell to 95/min, research
    # stalled.
    LANE_DOS = ("feed",)
    LANE_SHARE_MIN = 2.0           # game minutes of upkeep allowed per LANE_WINDOW_MIN inside one goal
    LANE_WINDOW_MIN = 10.0

    LANE_BATCH = 4                 # feeds one safe point may do (all that are due, so the old pause is not needed)
    LANE_MIN_HELD = 5              # a feed runs in the lane only with at least this much of its item on hand

    def _upkeep_lane(self) -> None:
        for _ in range(self.LANE_BATCH):
            if not self._lane_once():
                return

    def _lane_once(self) -> bool:
        """One due fuel feed inside the goal in hand. Returns whether one ran."""
        cur = self.current or {}
        if not cur or cur.get("urgent") or not self.urgent or not self._safe_now():
            return False
        held = lambda g: self.w.inventory.get(g.get("item", "coal"), 0)
        # only what the crew carries: gathering it is a trip of its own (the test game, seed 515151: one feed in
        # the lane took 4.9 minutes fetching coal), which waits for the goal to end like any other fix
        i = next((k for k, g in enumerate(self.urgent) if g.get("do") in self.LANE_DOS
                  and str(g.get("reflex", "")).startswith("rules:") and held(g) >= self.LANE_MIN_HELD), None)
        if i is None:
            return False
        now = self._minute()
        log = [x for x in (self.__dict__.get("_lane_log") or []) if now - x[0] < self.LANE_WINDOW_MIN]
        if sum(x[1] for x in log) >= self.LANE_SHARE_MIN:
            self._lane_log = log
            return False
        g = self.urgent.pop(i)
        rec, self.w.recorder = self.w.recorder, None   # nothing it does belongs to the goal's site
        self._in_lane = True
        try:
            with self.w.step("upkeep"):                 # the feed itself only: no tidying, no other repairs
                n = min(int(g.get("count", 50)), held(g))
                msg, ok = self.feed(g.get("site"), g.get("item", "coal"), n), True
        except BuildError as exc:
            msg, ok = str(exc)[:160], False
        finally:
            self._in_lane = False
            self.w.recorder = rec
        spent = max(0.0, self._minute() - now)
        self._lane_log = log + [(now, spent)]
        main = cur.get("goal") or {}
        self.log(f"[crew] upkeep lane, inside {main.get('do')} {main.get('module') or main.get('site') or ''}: "
                 f"{g.get('do')} site {g.get('site')} {g.get('item') or ''}: {'ok' if ok else 'FAILED'} - "
                 f"{str(msg)[:160]} ({spent:.1f} min)")
        try:
            from .agenda import agenda_of
            ag = agenda_of(self)
            ag.note_done(now, g) if ok else ag.record(now, "rules", "failed", g, str(msg)[:120])
        except Exception:  # noqa: BLE001 - bookkeeping never stops a goal
            pass
        if self.preempt and str(self.preempt).startswith("rules") and not any(
                str(q.get("reflex", "")).startswith("rules:") for q in self.urgent):
            self.clear_pause()                        # what the pause was for is done: the goal goes on
        return True

    def request_pause(self, why: str, force: bool = False, drop: bool = False) -> None:
        """Pause (drop: and discard) the goal in hand at its next safe point. Tied to that goal: when it ends
        before reaching one, the request goes with it (run 20261120-023103: an overseer's "drop the goal in hand"
        outlived the goal it meant and killed the next one, its own iron expansion; later the green-science build
        that everything waited on)."""
        if not self.current:
            return
        if self.preempt_for != self.current.get("tick"):
            self.clear_pause()                        # anything left from an earlier goal does not add up here
        self.preempt, self.preempt_for = why, self.current.get("tick")
        self.preempt_force = self.preempt_force or force
        self.drop_current = self.drop_current or drop

    def clear_pause(self) -> None:
        self.preempt, self.preempt_for, self.preempt_force, self.drop_current = None, None, False, False

    def _safe_now(self) -> bool:
        """Nothing is half-placed right now: the innermost step is crafting, gathering or waiting, not placing.
        The overseer's interrupts stop here (run 20261119-202811: its pauses waited out a 28-minute replace and
        an 18-minute link pass, because they were urgent goals and mostly inside a build step)."""
        return bool(self.current) and (not self.w.steps or self.w.steps[-1] not in self.PINNED)

    def goal_budget(self, goal: dict) -> float | None:
        """Game minutes this goal may take (None: no limit)."""
        do = goal.get("do")
        if do == "wait":
            return float(goal.get("seconds", 30)) / 60 + 3.0
        if do == "research":
            # the labs go on researching whatever happens to the goal: stopping it only frees the crew. Run
            # 20261124-015025: production science held the crew 35 game minutes while the fixes blue science
            # waited on stood behind it (unlimited until then)
            return RESEARCH_BUDGET_MIN
        if do in ("build", "replace"):
            mod = (goal.get("with") or {}).get("module") if do == "replace" else goal.get("module")
            base = MODULE_BUDGET_MIN.get(mod or "", GOAL_BUDGET_MIN["build"])
            if goal.get("electric"):                  # an electric swap: its furnaces take advanced circuits to make
                base += ELECTRIC_SWAP_EXTRA_MIN
            return base + (8.0 if do == "replace" else 0.0)      # a replace also takes the old site down
        return GOAL_BUDGET_MIN.get(do)

    def _over_budget(self) -> str | None:
        cur = self.current or {}
        budget = self.goal_budget(cur.get("goal") or {})
        if budget is None:
            return None
        budget += float(cur.get("extra_min") or 0.0)     # feeder factories built first (_intermediates_first)
        mins = (self.w.now_tick() - cur.get("tick", self.w.now_tick())) / 3600
        if mins <= budget:
            return None
        goal = cur.get("goal") or {}
        placing = self.w.now_tick() - getattr(self.w, "last_place_tick", -1e9) <= PLACE_GRACE_MIN * 3600
        if goal.get("do") in ("build", "replace") and "build" in self.w.steps and placing                 and mins <= budget * BUILD_OVERRUN_MAX:
            if cur.get("overrun") is None:            # said once: the monitor and the planners see why it runs on
                cur["overrun"] = round(mins, 1)
                self.log(f"[crew] {goal.get('module') or goal.get('do')} is over its {budget:.0f}-minute budget but "
                         f"still placing: it goes on (up to {budget * BUILD_OVERRUN_MAX:.0f} minutes)")
            return None
        step = "/".join(self.w.steps) or "-"
        return (f"over budget: ran {mins:.0f} game minutes (budget {budget:.0f}), stuck in step {step}: "
                f"{self.w.activity[:120]}")

    def _stalled(self) -> bool:
        goal = (self.current or {}).get("goal") or {}
        if goal.get("do") in ("wait", "research") or "research" in self.w.steps:
            return False                              # waiting is what these do
        since = max(getattr(self.w, "last_progress_tick", 0), (self.current or {}).get("tick", 0))
        return self.w.now_tick() - since > STALL_MIN * 3600

    def no_room(self, item: str) -> bool:
        """No slot can take any more of an item: the inventory is full and it has no part-filled stack. Tries to
        leave things in storage first (ensure_space); True when that freed nothing."""
        if not hasattr(self.kn, "items"):              # no stack sizes known (a stub): cannot tell, so no
            return False
        stack = int((self.kn.items.get(item) or {}).get("stack_size") or 50)
        have = self.w.inventory.get(item, 0)
        if INVENTORY_SLOTS - self.slots_used() > 0 or (have and have % stack):
            return False
        try:
            self.ensure_space(8, force=True)
        except BuildError as exc:
            self.foreman.note(f"storage: {str(exc)[:100]}")
        return INVENTORY_SLOTS - self.slots_used() <= 0

    # ================================================================ goals
    def _check_forbidden(self, goal: dict) -> None:
        from .overseer import forbidden
        f = forbidden(self, goal)
        if f is not None:
            raise BuildError(f"the overseer forbids {f['match']} for now: {f.get('why', '')}"[:240])

    def run_goal(self, goal: dict) -> str:
        goal = {k: v for k, v in goal.items() if k != "reflex"}     # the crew's own tag, not a module argument
        self._check_forbidden(goal)
        do = goal.get("do")
        self.prefetch_designs(goal)
        if getattr(self, "lifted_links", None):
            self.restore_lifted()         # a link inserter taken up for plates that could not go back then
        try:
            self.ensure_space()
        except BuildError as exc:         # tidying the inventory must never fail the goal itself
            self.foreman.note(f"storage: {str(exc)[:120]}")
        if do == "feed":
            return self.feed(goal.get("site"), goal.get("item", "coal"), goal.get("count", 50))
        if do == "priority":
            order = [str(x) for x in (goal.get("sites") or goal.get("order") or [])][:8]
            self.foreman.priority = order
            return (f"the foreman now tends {order} right after coal mines and power" if order
                    else "foreman priority cleared")
        if do == "build":
            return self.build(goal)
        if do in ("link", "link_science"):
            return self.link()
        if do == "relink":
            return self.triaged_relink(goal)
        if do == "trunk":
            return self.trunk(goal.get("site"), goal.get("item"))
        if do == "bus":                   # a zone segment's bus: its poles, the lines its slots use, head trunks
            from . import zones
            return zones.run_bus_goal(self, goal)
        if do == "purge":
            from .purity import purge
            return purge(self, goal.get("site"), goal.get("item"))
        if do == "research":
            tech = goal["tech"]
            if tech not in self.kn.techs:
                raise BuildError(f"unknown technology {tech}")
            # research() decides: automate the packs it lacks, or (only when that is cheap or impossible) hand-feed
            return self.research(tech, wait=False)
        if do == "act":
            return self.act(goal.get("steps") or [goal])
        if do == "stock":
            item, n = goal["item"], int(goal.get("count", 1))
            with self.w.step("unlock"):
                self.unlock_items([item])
            with self.w.step("make"):
                self.make({item: n})
            return f"holding {self.w.inventory.get(item, 0)} {item}"
        if do == "remove":
            self._sealed_guard(goal.get("site"), goal)
            return self.remove(goal.get("site"), force=bool(goal.get("force")), keep_poles=goal.get("keep_poles", True))
        if do == "replace":
            old = self.base.get(goal.get("site"))
            if old is None:
                raise BuildError(f"there is no site {goal.get('site')}")
            self._sealed_guard(old.id, goal)
            new = goal.get("with") or {}
            if new.get("do") != "build":
                raise BuildError("replace needs a build goal in 'with'")
            if goal.get("clear_first"):
                return self._clear_and_build(old, new)
            if goal.get("electric"):                      # an electric furnace swap: tried again only after a while
                from .upgrade import note_swap_start
                note_swap_start(self, goal)
            before = {s.id for s in self.base.sites}
            self._note_bad_place(old)                    # walled in where it stands: not the same spot again
            self._replacing = old
            try:
                built = self.build(new)                      # make before break: the old one runs meanwhile
            except BuildError as exc:
                raise BuildError(self._undo_replacement(new, before, old, exc)) from None
            finally:
                self._replacing = None
            self._replaced = old.id                      # built: the sole-maker guard has nothing to keep
            try:
                done = built + "; " + self.remove(old.id, force=False)
            finally:
                self._replaced = None
            if goal.get("electric") and old.params.get("outpost") is not None and "relinked:" not in done:
                # an outpost's column swapped for electric furnaces: its trunk goes into the new one now (the link
                # step lays pending trunks first), not whenever a link goal comes by
                try:
                    done += "; relinked: " + self.link()[:160]
                except BuildError as exc:
                    done += f"; its trunk is laid by the next link step ({str(exc)[:120]})"
            return done
        if do == "design":
            return self.design(goal)
        if do == "connect":
            site = self.base.get(goal.get("site"))
            if site is None:
                raise BuildError(f"there is no site {goal.get('site')}")
            return self.connect(site)
        if do == "wait":
            return self.wait(min(300.0, max(5.0, float(goal.get("seconds", 30)))))
        if do == "scout":
            return self.scout(goal.get("resource"))
        if do == "upgrade":
            return self.upgrade(goal.get("site"))
        if do == "repair":
            return self.repair(goal.get("site"))
        if do == "repipe":
            return self.repipe(goal.get("site"))
        if do == "modules":
            return self.fit_modules(goal.get("site"))
        raise BuildError(f"unknown goal {do}")

    def _clear_and_build(self, old: Site, new: dict) -> str:
        """Break before make, for a small mine on a full patch: its drills (and furnaces) come down first so a
        dense field can take that ground (supply.small_mines). Its belts stay, and the link step feeds the field's
        plates into them like any new source. Should the field not fit even so, the old mine goes back up, and
        no other small mine of the ore is cleared for a while (supply.CLEAR_RETRY_MIN)."""
        r = old.params.get("resource")
        back = {"do": "build", "module": old.kind, "resource": r, "count": int(old.params.get("count") or 2),
                **({"into": old.params["into"]} if old.params.get("into") else {})}
        self._replaced = old.id                          # cleared on purpose: the sole-maker guard steps aside
        try:
            gone = self.remove(old.id, force=True)
        finally:
            self._replaced = None
        self._patch_full.pop(r, None)                    # its ground is free now
        if old.kind.startswith("unfinished"):            # a half-built site (assess.unfinished_alerts): the same
            from .assess import unfinished_key           # build again from its parts (once), nothing to put back
            self.__dict__.setdefault("_unfinished_retried", set()).add(
                unfinished_key(old.kind[len("unfinished "):], {k: v for k, v in new.items() if k not in ("do", "module")}))
            try:
                return f"{gone}; " + self.build(new)
            except BuildError as exc:
                raise BuildError(f"took up half-built site {old.id}; building it again failed: "
                                 f"{str(exc)[:200]}") from None
        try:
            return f"{gone}; " + self.build(new)
        except BuildError as exc:
            self.__dict__.setdefault("_clear_failed", {})[r] = self._minute()
            try:
                again = self.build(back)
            except BuildError as exc2:
                again = f"putting it back failed too: {str(exc2)[:120]}"
            raise BuildError(f"cleared site {old.id} for a dense {r} field, which did not fit "
                             f"({str(exc)[:160]}); {again}") from None

    ACT_OPS = ("insert", "take", "craft", "go", "place", "pickup", "mine", "wait")
    ACT_MAX_STEPS = 40

    def act(self, steps: list) -> str:
        """Do exactly what the overseer or a supervisor says, step by step: any of ACT_OPS, nothing judged. The
        planners may order anything (walk in circles, stop everything, hand-feed a machine); each step is a plain
        game action. A step that fails says why and the rest stop.

            {"op": "insert", "site": id | "at": [x, y], "item": i, "count": n}   into a site's machines and chests
                                                                               (spread over them) or one building
            {"op": "take", "site": id | "at": [x, y], "item": i, "count": n}     out of them
            {"op": "craft", "item": i, "count": n}                             by hand (ingredients made as needed)
            {"op": "go", "at": [x, y]}                                         walk there
            {"op": "place", "entity": e, "at": [x, y], "dir": d}              one building (recorded as a "manual"
                                                                               site, so it can be removed later)
            {"op": "pickup", "at": [x, y]}                                     take a building of ours back
            {"op": "mine", "item": resource, "count": n}                       by hand, at the nearest patch
            {"op": "wait", "seconds": s}                                       let the base run"""
        why = self._act_pipe_refusal(steps)
        if why:
            raise BuildError(why)
        done: list[str] = []
        for k, st in enumerate(list(steps)[:self.ACT_MAX_STEPS]):
            op = (st or {}).get("op")
            if op not in self.ACT_OPS:
                raise BuildError(f"step {k + 1}: unknown op {op!r} (one of {', '.join(self.ACT_OPS)})"
                                 + (f"; done before it: {'; '.join(done)}" if done else ""))
            try:
                done.append(self._act_step(op, st))
            except BuildError as exc:
                raise BuildError(f"step {k + 1} ({op}) failed: {str(exc)[:200]}"
                                 + (f"; done before it: {'; '.join(done)[:300]}" if done else "")) from None
        return "; ".join(done)[:600] or "nothing to do"

    # The overnight zones test (seed 737373): from minute 521 the overseer (and plans after it) laid and re-laid the
    # oil floor's crude line by hand in 15 "act" goals ("a crude bridge"); they picked up its inlet at (24, 171),
    # the refinery stood dry, and blue science never started. A site's pipes are laid by the module that routed
    # them; gaps are closed by repair, which knows the net. Hand steps keep away from pipes.
    ACT_PIPE_PARTS = ("pipe", "pipe-to-ground", "pump")

    def _act_pipe_refusal(self, steps) -> str | None:
        for st in list(steps or [])[:self.ACT_MAX_STEPS]:
            st = st or {}
            if st.get("op") == "place" and st.get("entity") in self.ACT_PIPE_PARTS:
                return (f"act refused: no hand-placed {st['entity']} (a site's fluid lines are routed by its module): "
                        'close a gap with {"do": "repair", "site": id}, or build the workshop again so it routes '
                        "its own pipes")
            if st.get("op") == "pickup" and st.get("at") is not None:
                x, y = float(st["at"][0]), float(st["at"][1])
                for s in self.base.sites:
                    hit = next((e for e in s.entities if abs(e[1] - x) < 0.6 and abs(e[2] - y) < 0.6), None)
                    if hit is not None and hit[0] in self.ACT_PIPE_PARTS:
                        return (f"act refused: the {hit[0]} at ({x:g}, {y:g}) is part of site {s.id}'s fluid line; "
                                f'picking it up cuts the line. {{"do": "repair", "site": {s.id}}} closes gaps')
        return None

    def _act_targets(self, st: dict) -> list[tuple[str, float, float]]:
        """The buildings a step means: one at "at", or every machine and chest of a site."""
        if st.get("at") is not None:
            x, y = float(st["at"][0]), float(st["at"][1])
            e = (getattr(self.w, "entities", None) or {}).get((x, y))
            return [((e or {}).get("name", "?"), x, y)]
        site = self.base.get(st.get("site"))
        if site is None:
            raise BuildError(f"there is no site {st.get('site')}")
        out = [(name, x, y) for name, x, y, _ in site.entities if name not in POLES
               and not name.endswith(("transport-belt", "underground-belt", "splitter", "pipe", "pipe-to-ground"))]
        if not out:
            raise BuildError(f"site {site.id} has no buildings to act on")
        return out

    def _act_step(self, op: str, st: dict) -> str:
        item, n = st.get("item"), max(1, int(st.get("count") or 1))
        if op == "wait":
            return self.wait(min(600.0, max(1.0, float(st.get("seconds") or 10))))
        if op == "go":
            x, y = st["at"]
            self.w.go(float(x), float(y))
            return f"walked to ({float(x):.0f}, {float(y):.0f})"
        if op == "craft":
            self.unlock_items([item])
            self.make({item: n}, extra=True)
            return f"crafted {n} {item}"
        if op == "mine":
            got = self.w.mine(item, n)
            return f"mined {got} {item} by hand"
        if op == "insert":
            if self.w.inventory.get(item, 0) < n:
                self.gather(item, n)
            targets = self._act_targets(st)
            per, put = max(1, n // len(targets)), 0
            for _, x, y in targets:
                put += self.w.insert(x, y, item, min(per, self.w.inventory.get(item, 0)))
            return f"put {put} {item} in"
        if op == "take":
            got = 0
            for _, x, y in self._act_targets(st):
                if got >= n:
                    break
                got += self.w.extract(x, y, item, n - got)
            return f"took {got} {item}"
        if op == "place":
            ent, (x, y) = st["entity"], st["at"]
            self.unlock_items([ent])
            if self.w.inventory.get(ent, 0) < 1:
                self.make({ent: 1}, extra=True)
            d = st.get("dir") or "north"
            tiles = self.w.footprint(ent, float(x), float(y), d)
            rec: list = []
            self.w.recorder = rec
            try:
                self.w.place(ent, float(x), float(y), d, tiles)
            finally:
                self.w.recorder = None
            self.base.add(Site("manual", {"entity": ent}, (float(x), float(y)), entities=rec,
                               built_minute=self._minute()))
            return f"placed {ent} at ({float(x)}, {float(y)})"
        if op == "pickup":
            x, y = float(st["at"][0]), float(st["at"][1])
            for s in self.base.sites:
                hit = next((e for e in s.entities if abs(e[1] - x) < 0.6 and abs(e[2] - y) < 0.6), None)
                if hit is not None:
                    if self.w.pickup(*hit):
                        s.entities.remove(hit)
                        if not s.entities:
                            self.base.remove(s)
                        return f"picked up the {hit[0]} at ({x}, {y})"
                    raise BuildError(f"the {hit[0]} at ({x}, {y}) could not be picked up")
            raise BuildError(f"no building of ours at ({x}, {y})")
        raise BuildError(f"unknown op {op}")

    def fit_modules(self, site_id=None) -> str:
        """Productivity modules into a site's machines (modfit.py): the silo first, then science and rocket
        intermediates. Makes what is missing (from advanced circuits), then inserts them machine by machine. A
        machine that refuses (its recipe allows no productivity) is passed over; the site is done either way."""
        from . import modfit
        item = modfit.module_item(self.kn)
        if item is None:
            raise BuildError("no productivity module is researched yet")
        todo = [t for t in modfit.targets(self) if site_id is None or t[0].id == site_id]
        if not todo:
            raise BuildError(f"site {site_id} takes no modules (or has them already)" if site_id is not None
                             else "no site takes modules now")
        site, machines, slots = todo[0]
        need = min(modfit.MAX_PER_GOAL, sum(slots))
        have = self.w.inventory.get(item, 0)
        if have < need:
            try:
                with self.w.step("make"):
                    self.make_for({item: need})
            except BuildError as exc:
                if not self.w.inventory.get(item, 0):
                    raise BuildError(f"no {item} to fit: {str(exc)[:160]}") from None
        fitted, refused, ran_out = 0, 0, False
        fill = site.params.setdefault("modules_in", {})   # "x,y" -> modules in it; -1: its recipe takes none
        with self.w.step("build"):
            for (name, x, y, d), n in zip(machines, slots):
                key = f"{x},{y}"
                have_in = int(fill.get(key, 0))
                if have_in < 0 or have_in >= n:
                    continue                          # full (or refused) on an earlier goal
                left = self.w.inventory.get(item, 0)
                if left <= 0:
                    ran_out = True
                    break
                self.w.doing(f"fitting {item} into the {name} at ({x:.0f}, {y:.0f})")
                got = self.w.insert(x, y, item, min(n - have_in, left))
                if got == 0 and have_in == 0:
                    fill[key] = -1                    # its recipe allows no productivity
                    refused += 1
                    continue
                fill[key] = have_in + got
                fitted += got
                if have_in + got < n:                 # part filled: out of modules, the rest next time
                    ran_out = True
                    break
        site.params["modules"] = int(site.params.get("modules") or 0) + fitted
        if not ran_out:                               # every machine filled or refused: nothing more to do here
            site.params["modules_done"] = True
        return (f"fitted {fitted} {item} into site {site.id}" + (f" ({refused} machine(s) take none)" if refused
                                                                 else "")
                + ("" if site.params.get("modules_done") else "; the rest once more modules are made"))

    def repipe(self, site_id) -> str:
        """A workshop machine piped on the wrong connection (a refinery's crude on its water input): the right
        connection is piped to the same net, then placed like any pipe gap. Run 20261121-140755: crude stood at
        the refinery's first input for hours, both pumpjacks full, no plastic and no blue science."""
        site = self.base.get(site_id)
        if site is None or site.kind != "workshop":
            raise BuildError(f"site {site_id} is not a workshop")
        gaps = MODULES["workshop"].repipe_plan(self.w, site)
        if not gaps:
            return f"site {site.id}: every pipe already joins the right connection"
        site.params["pipe_gaps"] = (site.params.get("pipe_gaps") or []) + gaps
        return f"site {site.id} piped again: " + self.repair(site.id)

    def repair(self, site_id=None) -> str:
        """Place what a build could not: a workshop's pipes (pipe_gaps: a gap stops the fluid behind it) and any
        site's buildings the game refused or a belt line's holes (gaps.py). No site: every site with either."""
        from . import gaps as gapmod
        has = lambda s: s is not None and (s.params.get("pipe_gaps") or gapmod.pending(s))  # noqa: E731
        sites = [self.base.get(site_id)] if site_id is not None else [s for s in self.base.sites if has(s)]
        sites = [s for s in sites if has(s)]
        if not sites:
            raise BuildError("no pipe gaps or missing buildings to repair"
                             + (f" in site {site_id}" if site_id is not None else ""))
        done, pipes, left = 0, 0, []
        with self.w.step("build"):
            for s in sites:
                if gapmod.pending(s):
                    n, rest = gapmod.repair(self, s)
                    done += n
                    left += [f"site {s.id} {r}" for r in rest]
                gaps = s.params.get("pipe_gaps")
                if not gaps:
                    continue
                need = {"pipe": sum(1 for g in gaps if g[1] == "pipe"),
                        "pipe-to-ground": sum(1 for g in gaps if g[1] != "pipe")}
                need = {k: v for k, v in need.items() if v}
                self.unlock_items(list(need))
                self.make(need, extra=True)
                keep = []
                for fl, kind, t, d in gaps:
                    name = "pipe" if kind == "pipe" else "pipe-to-ground"
                    try:
                        self.w.place(name, t[0] + 0.5, t[1] + 0.5, d if name != "pipe" else "north", {tuple(t)})
                        s.entities.append((name, t[0] + 0.5, t[1] + 0.5, d if name != "pipe" else "north"))
                        pipes += 1
                    except BuildError as exc:
                        keep.append([fl, kind, t, d])
                        left.append(f"{fl} {name} at {tuple(t)}: {str(exc)[:60]}")
                if keep:
                    s.params["pipe_gaps"] = keep
                else:
                    s.params.pop("pipe_gaps", None)
                    if any(g[0] == "crude-oil" for g in gaps):
                        self._join_pumpjacks(s)
        if not done and not pipes:
            raise BuildError("nothing could be placed: " + "; ".join(left[:3]))
        said = ([f"closed {pipes} pipe gap(s)"] if pipes else []) + ([f"placed {done} missing building(s)"] if done else [])
        return ", ".join(said) + (f"; still open: {'; '.join(left[:3])}" if left else "")

    def _join_pumpjacks(self, site: Site) -> None:
        """The workshop floor's crude line is whole now: its pumpjacks count as joined (no second line)."""
        floors = {c.get("floor") for c in site.cells}
        for d in getattr(MODULES.get("workshop"), "floors", []):
            if d.idx in floors:
                for s in self.base.of("pumpjack"):
                    d.pumpjacks_joined |= {(x, y) for name, x, y, _ in s.entities if name == "pumpjack"}

    def upgrade(self, site_id=None) -> str:
        """Faster machines where the old ones stand (upgrade.py). No site: the one that gains most."""
        from .upgrade import upgradable_sites, upgrade_site
        if site_id is None:
            sites = upgradable_sites(self)
            if not sites:
                raise BuildError("no site waits on its machines with a faster same-size machine unlocked")
            site = sites[0]
        else:
            site = self.base.get(site_id)
            if site is None:
                raise BuildError(f"there is no site {site_id}")
        with self.w.step("build"):
            return upgrade_site(self, site)

    def scout(self, resource: str | None) -> str:
        """Walk out until a patch of the ore that nobody works yet is known (for an outpost)."""
        from . import outposts
        real = self.kn.resource_name(resource) if resource else None
        if real is None:
            raise BuildError(f"scout needs a resource: one of {self.kn.resource_names}")
        if real == "crude-oil":
            return self.scout_oil()
        boxes = [p["box"] for p in self.w.patches if p["name"] == real and outposts.worked(self.base, p)]
        p = outposts.scout(self.w, real, boxes, home=self.base.centre)
        if p is None:
            raise BuildError(f"no other {real} patch within two rings of the base")
        return (f"found a {real} patch of {p.get('tiles')} tiles ({p.get('amount')} ore) {p.get('distance')} tiles "
                f"from here: an outpost can mine it")

    OIL_FIELD_GAP = 24             # wells further apart than this are different fields

    def oil_fields(self) -> list[dict]:
        """The crude oil the bot knows, as fields: {centre, wells, pumped, distance (from the base)}, nearest
        first. Wells are single tiles, so they never show up as patches (w.patches) the way ore does."""
        wells = sorted(self.w.resources.get("crude-oil", ()))
        jacks = [(x, y) for s in self.base.of("pumpjack") for name, x, y, _ in s.entities if name == "pumpjack"]
        fields: list[list] = []
        for t in wells:
            near = next((f for f in fields if any(math.dist(t, q) <= self.OIL_FIELD_GAP for q in f)), None)
            if near is None:
                fields.append([t])
            else:
                near.append(t)
        home = self.base.centre or self.w.pos
        out = []
        for f in fields:
            cx, cy = sum(t[0] for t in f) / len(f) + 0.5, sum(t[1] for t in f) / len(f) + 0.5
            out.append({"centre": (round(cx), round(cy)), "wells": len(f),
                        "pumped": sum(1 for t in f if any(math.dist((t[0] + 0.5, t[1] + 0.5), j) < 1.6 for j in jacks)),
                        "distance": round(math.dist((cx, cy), home))})
        return sorted(out, key=lambda f: f["distance"])

    def scout_oil(self) -> str:
        """Scouting for crude oil: report what is known; search on (the pumpjack's ring search, from where it
        stopped) only when nothing is. Run 20261120-211555: with 12+ wells known and a pumpjack on them, a scout
        goal walked rings ~700 tiles out looking for an oil "patch" of 60+ tiles, which crude oil never forms."""
        from .assess import OIL_WELLS_MIN
        if hasattr(self, "_minute"):
            self.__dict__["_oil_scouted"] = self._minute()   # assess.oil_scout_alerts: not again for a while
        fields = self.oil_fields()
        if not fields:
            self.w.oil_wells(self.base.centre)
            fields = self.oil_fields()
            if not fields:
                raise BuildError(f"no crude oil within ~{self.w.oil_searched()} tiles of the base")
        known = sum(q["wells"] for q in fields)
        if known < OIL_WELLS_MIN and not all(q["pumped"] >= q["wells"] for q in fields) \
                and getattr(self.w, "oil_rings", 0) < getattr(self.w, "OIL_MAX_RINGS", 5):
            # too few wells for the oil chain (run 20261125-214645: one well, 188 tiles out, all game): the ring
            # search goes on outward (one ring a goal) for another field before the chain is built on this one
            self.w.oil_wells(self.base.centre, more=True)
            fields = self.oil_fields()
            now = sum(q["wells"] for q in fields)
            if now > known:
                f = max(fields, key=lambda q: q["wells"] - q["pumped"])
                return (f"found more crude oil: {now} wells in {len(fields)} field(s) (was {known}); the most free "
                        f"wells at {f['centre']} ({f['distance']} tiles from the base): pumpjacks go there")
            return (f"only {known} crude-oil well(s) known and no other field within ~{self.w.oil_searched()} tiles "
                    f"yet ({getattr(self.w, 'oil_rings', 0)} of {getattr(self.w, 'OIL_MAX_RINGS', 5)} rings searched); "
                    "the next scout goes farther out")
        if all(q["pumped"] >= q["wells"] for q in fields):    # every well is taken: a new field is what helps
            before = len(fields)
            self.w.oil_wells(self.base.centre, more=True)
            fields = self.oil_fields()
            if len(fields) <= before:
                raise BuildError(f"every known crude-oil well is pumped and no other field within "
                                 f"~{self.w.oil_searched()} tiles of the base yet; the next scout goes farther out")
            f = max(fields, key=lambda q: q["wells"] - q["pumped"])
            return (f"found another crude-oil field at {f['centre']} ({f['distance']} tiles from the base, "
                    f"{f['wells']} wells): build pumpjacks, they go on the free wells")
        f = fields[0]
        return (f"crude oil is known: {sum(q['wells'] for q in fields)} wells in {len(fields)} field(s), the nearest "
                f"at {f['centre']} ({f['distance']} tiles from the base, {f['wells']} wells, {f['pumped']} pumped). "
                "Nothing to scout: build pumpjacks there; a workshop pipes its crude in from them")

    def _crude_first(self, exc: BuildError) -> bool:
        """A workshop whose design found no crude for it ("build pumpjacks first", "every pumpjack already feeds
        another floor") gets its pumpjacks now, inside the same goal: the pumpjack goal searches farther out when
        every known well is taken. A workshop that needs a resource always goes and gets it. Run 20261124-015025:
        chemical science failed five times on taken wells and nobody looked for another field. Returns True when
        pumpjacks were built (design again), False when the error is about something else."""
        msg = str(exc)
        if "build pumpjacks first" not in msg and "every pumpjack already feeds" not in msg:
            return False
        import re
        m = re.search(r'"count": (\d+)', msg)
        k = max(1, min(4, int(m.group(1)) if m else 2))
        self.log(f"[crew] the workshop needs crude oil nobody pumps for it: {k} pumpjack(s) first")
        self.build({"do": "build", "module": "pumpjack", "count": k})
        return True

    def _outpost_extra(self, params: dict) -> dict[str, int]:
        """What an outpost's job needs past its drills, made before the walk out: its smelting column (sized as
        _finish_outpost sizes it), the trunk home and the pole line. Run 20261124-015025: iron outposts took 20
        and 33 game minutes, mostly trips back to the base for belts, poles and furnaces. Estimated from the
        patch it will likely go on (the plan's box, else the nearest big spare patch)."""
        from . import outposts
        r = params.get("resource")
        if not r:
            return {}
        home = self.base.centre or self.w.pos
        c = None
        if params.get("box"):
            b = params["box"]
            c = ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
        elif params.get("near_base") or params.get("near_first"):
            near = outposts.near_patches(self.w, self.base, r)
            if near:
                q = near[0]
                c = (q["nearest"]["x"], q["nearest"]["y"]) if q.get("nearest") else (q["centre"]["x"], q["centre"]["y"])
        if c is None and not params.get("box") and not params.get("near_base"):
            spare = outposts.spare_patches(self.w, self.base, r)
            big = [q for q in spare if q.get("tiles", 0) >= outposts.BIG_PATCH_TILES] or spare
            if big:
                q = big[0]
                c = (q["centre"]["x"], q["centre"]["y"]) if q.get("centre") else (q["nearest"]["x"], q["nearest"]["y"])
        if c is None:
            return {}
        d = math.dist(c, home)
        drills = MODULES["outpost"]._n(params)
        into = outposts.column_into(params)
        rate = 45.0 if into == "chest" else (37.5 if "steel-furnace" in self.kn.unlocked else 18.75)
        if r == "stone" and into == "furnace":
            rate *= 2
        n = max(1, min(24, math.ceil(outposts.DRILL_PER_MIN * drills / rate)))
        try:
            out = dict(MODULES["smelter_column"].kit(self.w, self.base, {"resource": r, "count": n, "into": into}))
        except Exception:  # noqa: BLE001 - an estimate: the column's own build makes what is missing
            out = {}
        beside = params.get("near_base") or ((params.get("field") or d <= outposts.FIELD_SMELT_MAX) and into == "furnace")
        out["transport-belt"] = out.get("transport-belt", 0) + (24 if beside else int(d * 1.25) + 10)
        kind = self._line_pole(d)
        out[kind] = out.get(kind, 0) + math.ceil(d / (MEDIUM_REACH if kind == MEDIUM_POLE else REACH)) + 4
        return out

    def _finish_outpost(self, site: Site) -> str:
        """After the drills stand: the column at the base end (sized to what they mine), then the trunk.
        Never re-entered for the same outpost: building the column runs the link step, which finishes pending
        outposts; before this guard that started a second column, whose link step started a third... (run
        20261119-202811: 8 unfed 7-furnace columns in one goal, ~570 buildings, and still going)."""
        busy = self.__dict__.setdefault("_finishing", set())
        if site.id in busy:
            return f"outpost site {site.id} is being finished already"
        busy.add(site.id)
        try:
            return self._finish_outpost_once(site)
        finally:
            busy.discard(site.id)

    def _finish_outpost_once(self, site: Site) -> str:
        from . import outposts
        r = site.params["resource"]
        col = next((s for s in self.base.of("smelter_column") if s.params.get("outpost") == site.id), None)
        if col is None:
            per = float(site.params.get("per_minute") or 30.0 * site.params.get("count", 2))
            into = outposts.column_into(site.params)
            rate = 45.0 if into == "chest" else (37.5 if "steel-furnace" in self.kn.unlocked else 18.75)
            if r == "stone" and into == "furnace":
                rate *= 2                              # two stone a brick
            n = max(1, min(24, math.ceil(per / rate)))
            before = {s.id for s in self.base.sites}
            home = self.base.centre or self.w.pos
            # beside the field when it is near: plates ride out, the ore never crowds the base (mining, then
            # smelting, then the belt out); far out the column stays at the base, where its coal belt is short
            beside = site.params.get("field") or math.dist(tuple(site.params["head"]), home) <= outposts.FIELD_SMELT_MAX
            col_goal = {"do": "build", "module": "smelter_column", "resource": r, "count": n,
                        "toward": site.params["head"], **({"into": "chest"} if into == "chest" else {})}
            if site.params.get("near_base") or (beside and into == "furnace"):
                # near the base its column (depot chests too) stands beside the field: no trunk across the base
                col_goal["near"] = list(site.params["head"])
            if site.params.get("column_at"):           # the spot its trunk route was checked into (outpost_probe)
                col_goal["at"] = list(site.params["column_at"])
            try:
                self.build(col_goal)
            except BuildError as exc:
                # the column may stand even so (its power or link step failed after it was placed): claim it,
                # or the next try builds yet another one
                col = self._new_column(before)
                if col is None:
                    # a half-built column comes back up (its parts are the next try's kit): run 20261125-154120
                    # left one with 125 buildings and no power, and the next try built a second column elsewhere
                    left = [s for s in self.base.sites if s.id not in before and s.kind == "unfinished smelter_column"]
                    for s in left:
                        try:
                            self.remove(s.id, force=True)
                        except BuildError as exc2:
                            self.foreman.note(f"half-built column site {s.id} not taken up: {str(exc2)[:80]}")
                    return (f"its smelting column failed for now ({str(exc)[:120]})"
                            + ("; its half-built column was taken up again" if left else "")
                            + "; the link step tries again")
                col.params["outpost"] = site.id
                return (f"column site {col.id} stands but its build ended with: {str(exc)[:120]}; the link step "
                        "lays the trunk")
            col = self._new_column(before)
            if col is None:
                return "its smelting column did not show up in the base; the link step tries again"
            col.params["outpost"] = site.id
        try:
            with self.w.step("build"):
                rec = outposts.lay_trunk(self, site, col)
        except BuildError as exc:
            return f"column site {col.id} stands; the trunk failed for now ({str(exc)[:140]}); the link step retries"
        return f"column site {col.id} and a {rec['belts']}-belt trunk from the outpost"

    def _outpost_trunk(self, col: Site, port: dict) -> str:
        """A trunk goal into a column's ore port: from its outpost's head (outposts.lay_trunk, routed by plan_trunk
        with undergrounds once they are in)."""
        from . import outposts
        out = self.base.get(col.params.get("outpost"))
        if out is None or out.kind != "outpost":
            res = col.params.get("resource")
            cands = [s for s in self.base.of("outpost") if s.params.get("resource") == res
                     and s.params.get("trunk_to") in (None, col.id)]
            out = min(cands, key=lambda s: math.dist(s.centre, col.centre)) if cands else None
        if out is None:
            raise BuildError(f"site {col.id}'s {port['item']} port is fed by an outpost's trunk, and no outpost of "
                             f"{port['item']} is left to feed it: remove the column or build an outpost")
        try:
            with self.w.step("build"):
                rec = outposts.lay_trunk(self, out, col)
        except BuildError as exc:
            self._note_link_fail(tuple(port["end"]), port["item"], col, "trunk: " + str(exc)[:200])
            raise BuildError(f"no trunk from outpost {out.id} into site {col.id}: {str(exc)[:200]}. The outpost "
                             "and its column stand where no belt joins them: remove both (an outpost build "
                             "plans its trunk before placing anything now)") from None
        self._link_fails.pop(tuple(port["end"]), None)
        return f"a {rec['belts']}-belt trunk carries {port['item']} from outpost {out.id} into site {col.id}"

    def outpost_probe(self, p: dict, head: tuple, start: tuple, taken: set, drills: int):
        """Before an outpost places anything (outposts.Outpost.trunk_probe): where its smelting column would go
        and whether a trunk route joins its head to the column's ore port. Returns (why not or None, the
        column's spot (bx, by) or None)."""
        from . import outposts
        r = p["resource"]
        col = MODULES["smelter_column"]
        per = outposts.DRILL_PER_MIN * max(1, drills)
        into = outposts.column_into(p)
        rate = 45.0 if into == "chest" else (37.5 if "steel-furnace" in self.kn.unlocked else 18.75)
        if r == "stone" and into == "furnace":
            rate *= 2
        n = max(1, min(24, math.ceil(per / rate)))
        home = self.base.centre or self.w.pos
        cp = {"resource": r, "count": n, "toward": list(head), **({"into": "chest"} if into == "chest" else {})}
        if p.get("near_base") or ((p.get("field") or math.dist(head, home) <= outposts.FIELD_SMELT_MAX)
                                  and into == "furnace"):
            cp["near"] = list(head)
        skip: list = []
        for _ in range(3):
            found = col.find_spot(self.w, self.base, cp, skip=skip)
            if found is None:
                return (f"no clear ground for its {n}-furnace smelting column", None)
            bx, by, size = found
            area = col.tiles(bx, by, size, col.layout(self.w, cp), col.electric(self.w, cp))
            port = {"item": r, "end": [bx, by - 1], "approach": [bx, by - 2]}
            appr = (bx, by - 2)
            k = max(1, int(math.dist(start, appr) // 40))
            self.w.doing(f"checking a trunk route from the {r} patch to its column")
            for i in range(k + 1):
                try:
                    self.w.look(start[0] + (appr[0] - start[0]) * i / k, start[1] + (appr[1] - start[1]) * i / k,
                                radius=32)
                except BuildError:
                    pass
            steps = outposts.plan_trunk(self, tuple(start), port,
                                        block=(set(taken) | set(area)) - {(bx, by - 1), appr})
            if steps:
                self.log(f"[crew] outpost probe: a {len(steps)}-step trunk from {list(start)} reaches a column "
                         f"at ({bx}, {by})")
                return None, (bx, by)
            skip.append((bx, by))
        return (f"no trunk route from its head at {list(start)} to any of {len(skip)} column spots "
                f"({round(math.dist(start, home))} tiles from the base)", None)

    def _new_column(self, before: set) -> Site | None:
        """The smelter column a build just added (None: it added none)."""
        return next((s for s in reversed(self.base.of("smelter_column")) if s.id not in before
                     and not s.params.get("outpost")), None)

    def pending_trunks(self) -> list:
        """Outposts whose trunk (or column) is still missing (not one being finished right now)."""
        out = []
        busy = self.__dict__.get("_finishing", set())
        for s in self.base.of("outpost"):
            if s.id in busy:
                continue
            if s.params.get("trunk_to") is None and self._may_retry(("trunk", s.id)):
                col = next((c for c in self.base.of("smelter_column") if c.params.get("outpost") == s.id), None)
                out.append((s, col))
        return out

    def _sealed_guard(self, site_id, goal: dict) -> None:
        """A sealed site (it ran on its own for a while, see upkeep.py) is not torn down while it works."""
        from .upgrade import is_idle, successor_ok
        from .upkeep import is_sealed, needs
        site = self.base.get(site_id)
        if site is None or goal.get("force") or not is_sealed(self, site.id):
            return
        if any(n["kind"] == "broken" for n in needs(self, site)):
            return                                    # sealed but broken now: repairs may replace it
        if goal.get("do") == "replace" and successor_ok(site, goal.get("with")):
            return                                    # rebuilt bigger: more of the same thing, new one first
        if is_idle(self, site.id):
            return                                    # nobody takes its output: its machines may go elsewhere
        if self.inputs_stuck(site.id):
            # its own inputs cannot reach it: it may move (run 20261121-140755: labs site 53 stayed sealed while
            # red and green failed 14x into it)
            return
        raise BuildError(f"site {site.id} ({site.kind}) is sealed: it has run on its own since minute "
                         f"{self.sealed[site.id]:.0f}, so it stays as it is. To make it faster use "
                         '{"do": "upgrade", "site": ' + str(site.id) + '} (better machines in place); to make '
                         "more of the same, replace it with a bigger build of the same thing; otherwise build "
                         "elsewhere (force: true only if it really must go)")

    def _undo_replacement(self, new: dict, before: set, old: Site, exc: Exception) -> str:
        """A replace either completes or leaves the base as it was: the new site a failed build left standing (it
        was added, then connect or a link raised; or its build stopped half-way as an "unfinished" site) comes
        down again. Feeders and mines built first for it stay: they are useful to the old site too. Run
        20261122-133537: "replace site 15" left site 22, a second unpowered red-science factory of 131 buildings,
        next to site 15; both starved and later links were laid into the dead one. Returns the failure text."""
        mod = str(new.get("module") or "")
        keys = ("item", "resource")
        mine = [s for s in self.base.sites if s.id not in before and s.kind in (mod, f"unfinished {mod}")
                and all(s.params.get(k) == new.get(k) for k in keys if new.get(k) is not None)]
        msg = str(exc)[:200]
        for s in mine:
            try:
                self.remove(s.id, force=True)
                msg += f"; the half-done replacement (site {s.id}) was taken down again, site {old.id} stays"
            except BuildError as e2:
                msg += f"; taking the failed replacement site {s.id} down failed too: {str(e2)[:80]}"
        return msg

    def bus_superseded(self, goal: dict) -> str | None:
        """Why a replace that rebuilds an off-bus factory is not wanted any more, or None: a zones slot factory
        (one that puts the item on the bus) already makes that item. Run 20261127-101219: the overseer ordered the
        old organic gear feeder (site 18) moved at minute 123; the bus got its own gear slot factory (site 25) at
        137, the forced move still ran at 147 and spent 11 game minutes on an iron outpost, a 131/min smelter
        column and a gear factory whose gears went nowhere, while the bus head starved for iron."""
        if goal.get("do") != "replace":
            return None
        new = goal.get("with") or {}
        item = new.get("item")
        if new.get("module") not in ("factory", "workshop") or not item:
            return None
        old = self.base.get(goal.get("site"))
        if old is not None and (old.params or {}).get("slot"):
            return None                               # a slot factory rebuilt in its slot (floorplan_guard decides)
        slot = next((s for s in self.base.of("factory") + self.base.of("workshop")
                     if (s.params or {}).get("slot") and item in ((s.params or {}).get("bus_out") or ())), None)
        if slot is None:
            return None
        return (f"{item} already comes from the bus: slot factory site {slot.id} ({slot.params['slot']}) puts it "
                f"there, so site {goal.get('site')} is not rebuilt as another {item} factory; a short input is "
                "fixed at the bus head (plates into it), not with a second factory off the bus")

    def stale_replace(self, goal: dict) -> str | None:
        """Why a queued replace that moves a site (the same thing again elsewhere, ordered because its inputs could
        not reach it or it had no power) is no longer needed, or None. Checked when it is about to start: every
        port of the old site is fed now, it has power and no link into it is stuck. Upgrades (another module, e.g.
        burner to electric), forced replaces and rebuilds of depleted mines are never dropped. Run 20261122-133537:
        "relocate red science" was queued at minute 75 while a relink was running; the relink fed both ports, and
        the replace still ran four minutes later and left a duplicate factory."""
        bus = self.bus_superseded(goal) if hasattr(self, "bus_superseded") else None
        if bus is not None:
            return bus                                # even forced: the bus makes it now, a move only duplicates it
        if goal.get("do") != "replace" or goal.get("force") or goal.get("early") or goal.get("electric"):
            return None                               # (an electric furnace swap is an upgrade, never a move)
        site = self.base.get(goal.get("site"))
        new = goal.get("with") or {}
        if site is None or new.get("module") != site.kind:
            return None
        if any(new.get(k) is not None and new.get(k) != site.params.get(k) for k in ("item", "resource")):
            return None
        if _depleted(site) or site.kind.startswith("unfinished"):
            return None
        for k in ("per_minute", "count"):             # a bigger one is an upsize, not a move: it stands
            try:
                if new.get(k) is not None and float(new[k]) > float(site.params.get(k) or 0) * 1.05:
                    return None
            except (TypeError, ValueError):
                pass
        try:                                          # "fed" is a belt attached, not enough on it
            from . import triage
            if triage._starving(site) > max(1, triage._blocked(site)):
                return None
            for it in {pt.get("item") for pt in site.params.get("ports", []) if pt.get("item")}:
                if triage.diagnose(self, site.id, it).get("cause") in ("source-short", "source-small"):
                    return None                       # run 20261125-214645: dropped 3x while it got 56 of 91/min
        except Exception:  # noqa: BLE001 - stub sites and crews in tests
            pass
        ports = [pt for pt in site.params.get("ports", []) if pt.get("item") or "pack" in pt]
        if not ports or any(not (pt.get("fed") or pt.get("pack")) for pt in ports):
            return None
        if site.electric and (site.powered is False or site.health.get("no_power")):
            return None
        if self.inputs_stuck(site.id):
            return None
        return (f"site {site.id} no longer needs moving: all {len(ports)} of its input ports are fed and it has "
                "power (fixed in place since the replace was queued)")

    SMALL_NEED_MAX = 20            # an item needed fewer times than this is made by the crew, not by a factory
    SMALL_NEED_DEFAULT = {"pump": 2}   # wanted in handfuls whatever asks: one per 200 tiles of a fluid line

    def small_need(self, item: str | None) -> int | None:
        """How many of an item the base needs when that is a handful (below SMALL_NEED_MAX), else None. The
        number comes from what asked for it (a workshop's fluid line records its pumps in w.small_needs)."""
        if not item:
            return None
        n = (getattr(self.w, "small_needs", None) or {}).get(item, self.SMALL_NEED_DEFAULT.get(item))
        return int(n) if n is not None and 0 < n < self.SMALL_NEED_MAX else None

    def _make_instead(self, item: str) -> str:
        """A factory goal for an item needed only a handful of times: the crew makes that many instead (from
        machine-made parts where it must: pumps from engine units). Run 20261121-140755: the planners ordered a
        pump factory twice for a line that needed 3 pumps; one ran 32 game minutes and was dropped, while
        split_pipelines had already hand-crafted 2 pumps."""
        n = self.small_need(item) or 1                  # a total: what is on hand or stored counts
        why = f"{item} is needed {n}x: made by the crew, not by a factory"
        self.log(f"[crew] build factory {item} refused: {why}")
        short = n - self.w.inventory.get(item, 0) - self.stored(item)
        plan = self.kn.kit_plan({item: short}, self.w.inventory, supplied=self.ready_items()) if short > 0 else None
        if plan is not None and plan.blocked:
            parts = ", ".join(plan.blocked)
            rate = getattr(self.w, "rate", lambda _i: 0.0)
            made = "; ".join(f"our {b} factories make {rate(b):.1f}/min" for b in plan.blocked
                             if any(s.params.get("item") == b for s in self.base.of("factory")))
            raise BuildError(f"{why}, but it needs {parts}, which nothing we built makes yet"
                             + (f" fast enough ({made})" if made else
                                f': build {{"do": "build", "module": "factory", "item": "{plan.blocked[0]}", '
                                '"per_minute": 2}')
                             + f", then {n} {item} are hand-crafted from it (no {item} factory)")
        with self.w.step("unlock"):
            self.unlock_items([item])
        with self.w.step("make"):
            self.make({item: n})
        return f"{why}: holding {self.w.inventory.get(item, 0)} {item}"

    INPUT_STUCK_FAILS = 3

    def inputs_stuck(self, site_id) -> bool:
        """Has a link into this site (an item port, or science into a station) failed INPUT_STUCK_FAILS times and
        still stands missing?"""
        try:
            return any(q.get("into_site") == site_id and q.get("failed", 0) >= self.INPUT_STUCK_FAILS
                       for q in self.stuck_links())
        except Exception:  # noqa: BLE001 - a stub crew without links: nothing stuck
            return False

    def prefetch_designs(self, current: dict | None = None) -> str | None:
        """Start the layout of the next factory goal waiting in the queue (urgent first) on a thread of its
        own, so it is ready when that goal comes up. Never fails a goal."""
        mod = MODULES.get("factory")
        if mod is None or not hasattr(mod, "prefetch"):
            return None
        for g in (self.urgent + self.plan_queue)[:6]:
            if g is current or g.get("do") != "build" or g.get("module") != "factory" or not g.get("item"):
                continue
            p = {k: v for k, v in g.items() if k not in ("do", "module", "reflex")}
            try:                                          # where its plates come from: a head start only
                if not p.get("supply_at"):
                    p["supply_at"] = self.supply_spots(p)
            except Exception:  # noqa: BLE001
                pass
            try:
                try:
                    probe = self.route_probe(p)
                except Exception:  # noqa: BLE001 - a head start only
                    probe = None
                if mod.prefetch(self.w, self.base, p, probe=probe):
                    return p["item"]
            except Exception as exc:  # noqa: BLE001 - a head start only
                self.log(f"[crew] background design of {p.get('item')} not started: {str(exc)[:100]}")
            return None                                   # only the first factory waiting
        return None

    SPLIT_MAX = 2                   # feeder factories split off one factory that does not fit as one block

    def build(self, goal: dict) -> str:
        """Build a module. A belt factory whose layout fits nowhere as one block is split: its biggest made-here
        intermediate gets a factory of its own (built first by _intermediates_first) and this one takes it by
        belt, up to SPLIT_MAX times. Run 20261124-120854: advanced circuits from plates (4 circuit and 6
        advanced-circuit assemblers plus three input belts) found no belt route in 8 designs at 4 places, so
        blue science failed after 26 minutes; six assemblers fed circuits, cable and plastic route easily."""
        if goal.get("module") == "outpost" and not goal.get("near_base") and not goal.get("box") \
                and goal.get("resource") in ("iron-ore", "copper-ore", "coal", "stone") \
                and not self.patch_full(goal["resource"]):
            # the near patches first while they have room: a far trip, a pole line and a trunk only once they
            # are full (run 20261124-120854: an iron outpost 360 tiles out while the iron patch at home had room)
            goal = {**goal, "near_first": True}
        if goal.get("module") != "factory":
            return self._build(goal)
        split: list[str] = []
        fac = MODULES["factory"]
        planned = self._split_memory(goal)
        if planned:
            # split as the last time this factory had to be (this run or an earlier one): no design is tried
            # as one block first (run 20261125-214645: 35 advanced-circuit designs, 73 real minutes, before the
            # split that then fitted at once)
            raw = goal.get("inputs") or []
            raw = [raw] if isinstance(raw, str) else list(raw)
            goal = {**goal, "inputs": raw + [q for q in planned if q not in raw]}
            split += planned
            self.log(f"[crew] planned ahead: the {goal.get('item')} factory takes {', '.join(planned)} by belt "
                     "from feeder factories, as it had to before")
        try:
            while True:
                # a big chain gets two tries as one block, then it is split (Modules.Factory.quick)
                fac.quick = len(split) < self.SPLIT_MAX and (self._big_chain(goal) or any(split))
                try:
                    out = self._build(goal)
                    if split:
                        self._remember_split(goal, split)
                    return out
                except BuildError as exc:
                    item = goal.get("item")
                    no_fit = f"no {item} factory fits" in str(exc)
                    near = "beyond a link's reach of an input" not in str(exc)     # a split brings it no nearer
                    if not (no_fit and near) or len(split) >= self.SPLIT_MAX:
                        raise
                    part = self._split_candidate(goal)
                    if part is None:
                        if fac.quick:                 # nothing to split off after all: the whole ladder next
                            split.append("")
                            continue
                        raise
                    split.append(part)
                    raw = goal.get("inputs") or []
                    raw = [raw] if isinstance(raw, str) else list(raw)
                    goal = {**goal, "inputs": raw + [part]}
                    self.log(f"[crew] no {item} factory fits as one block: {part} gets a factory of its own "
                             f"first, and the {item} factory takes it by belt")
                    if self.current is not None:      # the new feeder's time on top of the goal's budget
                        self.current["extra_min"] = (float(self.current.get("extra_min") or 0.0)
                                                     + MODULE_BUDGET_MIN.get("factory", GOAL_BUDGET_MIN["build"]))
        finally:
            fac.quick = False

    QUICK_SPLIT_ASM = 10           # a chain this big gets two tries as one block before it is split

    def _big_chain(self, goal: dict) -> bool:
        """Does this factory's own chain (what it makes itself) have QUICK_SPLIT_ASM assemblers or more, with
        something to split off?"""
        try:
            fac = MODULES["factory"]
            params = {k: v for k, v in goal.items() if k not in ("do", "module")}
            supplied, pl = fac.supplied_for(self.kn, params)
            from fab import recipes as fr
            n = sum(v for k, v in (getattr(pl, "machines", None) or {}).items()
                    if k in fr.RECIPES and k not in supplied)
        except Exception:  # noqa: BLE001 - stub crews in tests
            return False
        return n >= self.QUICK_SPLIT_ASM and self._split_candidate(goal) is not None

    def _memory_path(self):
        from pathlib import Path
        cd = getattr(MODULES["factory"], "case_dir", None)
        return (Path(cd).parents[1] / "fab_memory.json") if cd is not None else None

    def _split_memory(self, goal: dict) -> list[str]:
        """The feeders this factory needed the last time at about this rate (runs/local/fab_memory.json)."""
        import json
        mem = getattr(self, "_fab_memory", None)
        if mem is None:
            p = self._memory_path()
            try:
                mem = json.loads(p.read_text(encoding="utf-8")) if p is not None and p.exists() else {}
            except Exception:  # noqa: BLE001 - a broken file is no memory
                mem = {}
            self._fab_memory = mem
        rec = mem.get(str(goal.get("item")))
        try:
            rate = float(MODULES["factory"].rate(goal))
            makeable = set(MODULES["factory"].can_make(self.kn))
        except Exception:  # noqa: BLE001
            return []
        if not rec or rate < 0.6 * float(rec.get("per_minute") or 0):
            return []
        return [q for q in rec.get("split") or [] if q and q in makeable]

    def _remember_split(self, goal: dict, split: list[str]) -> None:
        """A factory that had to be split: the next one at about this rate (any run) is split from the start."""
        import json
        parts = [q for q in split if q]
        if not parts:
            return
        mem = getattr(self, "_fab_memory", None) or {}
        old = mem.get(str(goal.get("item"))) or {}
        rate = float(MODULES["factory"].rate(goal))
        mem[str(goal.get("item"))] = {"split": parts,
                                      "per_minute": min(rate, float(old.get("per_minute") or rate))}
        self._fab_memory = mem
        p = self._memory_path()
        if p is None:
            return
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(mem, indent=1), encoding="utf-8")
        except OSError as exc:
            self.log(f"[crew] fab memory not saved: {exc}")

    def _split_candidate(self, goal: dict) -> str | None:
        """The intermediate a too-big factory should take by belt instead of making: the one with the most
        assemblers in its chain that an assembler makes (not a plate, not already an input)."""
        try:
            fac = MODULES["factory"]
            params = {k: v for k, v in goal.items() if k not in ("do", "module")}
            supplied, pl = fac.supplied_for(self.kn, params)
            from fab import recipes as fr
            makeable = set(fac.can_make(self.kn))
        except Exception:  # noqa: BLE001 - nothing to split by: the failure stands
            return None
        item = goal.get("item")
        cands = [(n, k) for k, n in (getattr(pl, "machines", None) or {}).items()
                 if k != item and k not in supplied and k in fr.RECIPES and k in makeable and k not in self.PLATE_ORE]
        return max(cands)[1] if cands else None

    def _build(self, goal: dict) -> str:
        self._check_forbidden({"do": "build", **{k: v for k, v in goal.items() if k != "do"}})
        mod = MODULES.get(goal.get("module", ""))
        if mod is None:
            raise BuildError(f"unknown module {goal.get('module')}")
        params = {k: v for k, v in goal.items() if k not in ("do", "module")}
        steel = mod.name == "smelter_column" and params.get("resource") == "iron-plate"   # its steel column
        if "resource" in params and not steel:  # "iron" -> "iron-ore"; a made-up name fails here, never explores
            real = self.kn.resource_name(params["resource"])
            if real is None:
                raise BuildError(f"unknown resource {params['resource']!r}: use one of {self.kn.resource_names}")
            params["resource"] = real
        if mod.name == "factory" and self.small_need(params.get("item")) is not None:
            return self._make_instead(params["item"])
        if mod.name == "factory":             # items/s or items/min: one rate from here on
            params["per_minute"] = mod.rate(params)
            params.pop("per_second", None)
        if mod.name in ("research_station", "smelter_column") and not params.get("near"):
            near = self._inputs_centre(mod.name, params)    # build where its inputs are: short links
            if near is not None:
                params["near"] = near
        if mod.name == "workshop":
            # its design (cached for the build below) says first if a fluid cannot reach the floor. Run
            # 20261121-140755: blue science built a 2-engine steam station for 15 game minutes, then failed on a
            # shore 407 tiles away that the design would have named at once
            with self.w.step("design"):
                try:
                    mod.kit(self.w, self.base, params)
                except BuildError as exc:
                    crude = getattr(self, "_crude_first", None)
                    if crude is None or not crude(exc):
                        raise
                    mod.kit(self.w, self.base, params)    # once more, with the new pumpjacks
        self._pick_furnace(mod, params)
        first = self._coal_first(mod.name, params)
        first += self._power_first(mod.name, params)
        first += self._electric_power_check(mod, params)
        first += self._fuel_first(mod.name, params)
        first += self._intermediates_first(mod.name, params)     # feeders first: their plate mines count below
        first += self._plates_first(mod.name, params)
        from . import zones as _zones          # a slot factory: the bus lines it draws on and plates into the head
        first += _zones.bus_first(self, mod.name, params)
        if mod.name == "factory" and not params.get("supply_at"):
            spots = self.supply_spots(params)              # after plates first: its new rows count too
            if spots:
                params["supply_at"] = spots
        blk = self.blocker()
        outer = (self.w.blocking, self.foreman.busy)
        paused = False
        # the next tier everything waits on: its chiplet keeps adapting to the ground until one fits
        self.w.blocking = bool(blk and blk.get("module") == mod.name and blk.get("resource") == params.get("resource")
                               and (mod.name != "factory" or blk.get("item") == params.get("item")))
        self.foreman.busy = mod.name          # the build comes first: the foreman only does what is urgent
        probes = self._set_probes(mod, params)
        try:
            pre = getattr(mod, "needs", None)            # what the design itself depends on (underground belts):
            if pre is not None:                           # researched and waited for BEFORE designing
                with self.w.step("unlock"):
                    self.unlock_recipes(pre(self.w, self.base, params))
            with self.w.step("design"):
                kit = mod.kit(self.w, self.base, params)
            with self.w.step("unlock"):
                self.unlock_items(list(kit))
                self.unlock_recipes(mod.recipes(self.w, self.base, params))   # e.g. green science before its factory
            with self.w.step("design"):
                kit = mod.kit(self.w, self.base, params)       # a design may change once more is unlocked
            if mod.name == "outpost":         # the whole trip's kit before the walk: column, trunk, pole line
                self.outpost_hold = self._outpost_extra(params)
                kit = {k: kit.get(k, 0) + self.outpost_hold.get(k, 0) for k in set(kit) | set(self.outpost_hold)}
            fuel = 8 * kit.get("burner-mining-drill", 0) + 5 * kit.get("stone-furnace", 0) + (40 if "boiler" in kit else 0)
            if fuel:                  # coal from our coal mine; wood only before one exists, from trees close by
                item = self.fuel_item()
                kit = {**kit, item: kit.get(item, 0) + fuel * (2 if item == "wood" else 1)}
            with self.w.step("make"):
                self.make(kit)
            self.kit_hold = dict(kit)
            self.w.doing(f"building {mod.name}")
            rec: list = []
            self.w.recorder = rec
            try:
                with self.w.step("build"):
                    site = mod.build(self.w, self.base, params)
            except Exception as exc:
                if isinstance(exc, Preempted):
                    paused = True
                if mod.name == "outpost":
                    self.outpost_hold = {}
                if mod.name == "outpost" and params.get("near_base") and isinstance(exc, BuildError):
                    # a near field that failed (no trunk route to its column, say): mine rows for a while, so a
                    # coal or plate shortage is never stuck on the same field again and again (supply.near_field)
                    self.__dict__.setdefault("_field_failed", {})[params.get("resource")] = self._minute()
                if mod.name == "factory" and isinstance(exc, BuildError):   # its layout did not fit: not again
                    self._factory_failed[params.get("item")] = self._minute()  # for a while
                if mod.name in ("factory", "workshop") and isinstance(exc, BuildError):
                    self.failed_suppliers[params.get("item")] = self._minute()
                if mod.name == "smelter_column" and params.get("resource") == "iron-plate" \
                        and isinstance(exc, BuildError):
                    self.failed_suppliers["steel-plate"] = self._minute()
                if (mod.name == "electric_mine" or (mod.name == "outpost" and params.get("near_base"))) \
                        and isinstance(exc, BuildError) and \
                        ("no room" in str(exc) or "patches with room are" in str(exc)):
                    r = params.get("resource")
                    self._patch_full[r] = (self._minute(), self._patch_sig(r))
                if rec:                              # what did get built stays known, so it can be removed
                    left = self.base.add(Site(f"unfinished {mod.name}", params, _centre(rec), entities=rec,
                                              built_minute=self._minute()))
                    left.note = f"stopped after {len(rec)} buildings: {str(exc)[:160]}"   # why: clear it or retry
                raise
            finally:
                self.w.recorder = None
        except Preempted:
            paused = True
            raise
        finally:
            if not paused:                    # a paused goal comes back to the same design (Factory._design checks it)
                getattr(mod, "designs", {}).clear()
            for attr, old in probes.items():
                setattr(mod, attr, old)
            self.w.blocking, self.foreman.busy = outer
            self.kit_hold = {}
        site.entities, site.built_minute = rec, self._minute()
        self.base.add(site)
        unpowered = self._power_pumps(site)
        self._reserve_margin(site)
        if site.kind == "electric_mine" and site.params.get("fuel") == "hand":
            self.reserve_strips()
        self._note_churn(site)
        msg = first + f"built {mod.name} as site {site.id} {site.brief_params()}"
        from . import zones                   # the first electric build reserves the floorplan; segment 1 opens
        znote = zones.after_build(self, site)  # once its conditions hold (never raises)
        if znote:
            msg += f"; {znote}"
        if site.params.get("pumps"):
            msg += f"; {len(site.params['pumps'])} pump(s) on its long pipe line" + (
                f", {len(unpowered)} without power: {unpowered[0]}" if unpowered else "")
        if "designed for" in site.note:
            msg += f" ({site.note.split('; ', 1)[-1]})"
        if site.electric or (site.kind == "steam_power" and len(self.base.of("steam_power")) > 1):
            try:
                msg += "; " + self.connect(site)
            except BuildError as exc:
                # the site stands: an unpowered build is not a failed one (the zones test, seed 626262: green science
                # stood complete after 28 minutes, was reported FAILED for its power, and the deciders ordered it
                # rebuilt); the power alert queues its connect
                site.powered = False
                msg += f"; NOT POWERED YET: {str(exc)[:140]} (a connect goal follows)"
                from .agenda import agenda_of
                agenda_of(self).propose(self, {"do": "connect", "site": site.id,
                                               "reflex": "rules: built, but not on the power network yet"}, "rules")
        if site.kind == "steam_power" and coal_belts_ready(self.kn):
            try:
                self._coal_port(site)
            except BuildError as exc:         # the foreman keeps filling the chest by hand meanwhile
                self.foreman.note(f"site {site.id}: no coal belt input ({str(exc)[:80]})")
        elif site.kind == "steam_power":
            site.params["fuel"] = "hand"      # until logistics: then link() fits it a coal belt
        if site.kind == "outpost":                # its smelting column at the base and the trunk between them
            try:
                msg += "; " + self._finish_outpost(site)
            finally:
                self.outpost_hold = {}
        made = {what for *_, what in site.chests if what not in self.TAGS}
        if site.params.get("line"):
            made.add(site.params["line"]["item"])
        # a source built to replace one of the same product is kept for the old one's belts: once the old site
        # comes down its belts stay and this one is fed into their side (_detach, boost_links), so it is not
        # handed to other ports first
        old = getattr(self, "_replacing", None)
        holding = old is not None and bool(made & self._makes(old))
        if not holding and (any(pt["item"] in made for s, pt in self.unfed_ports() if s is not site)
                            or any(c["item"] in made for c in self.short_chains())):   # a source for a short belt
            try:                              # a new source (a coal mine, say): belt it to the ports that wait for it
                msg += "; " + self.link()
            except BuildError as exc:
                msg += f"; linking it to the ports that wait for it failed for now: {str(exc)[:160]}"
        elif any(self._free_outputs(pt["item"], site, self._need(pt)) or self._belt_taps(pt["item"], site, self._need(pt))
                 for pt in site.params.get("ports", []) if pt.get("item")):   # station ports carry "pack"
            try:                              # belt what already exists into its ports now (the courier until then)
                msg += "; " + self.link()
            except BuildError as exc:
                msg += f"; linking its inputs failed for now: {str(exc)[:120]}"
        return msg

    PLATE_ORE = {"iron-plate": "iron-ore", "copper-plate": "copper-ore"}
    INTERMEDIATE_DEPTH = 2          # feeders of feeders at most (blue science: engines, then their gears)
    FEEDER_HEADROOM = 1.15

    def _intermediates_first(self, module: str, params: dict) -> str:
        """A factory whose chain is split (Factory.inputs: over MAX_ASM assemblers) or that is asked to take gears,
        circuits... through its ports gets the factories that make them first: one per input that has an assembler
        recipe and is not a plate, sized to what the base does not already have spare. Each feeder is a full build
        (its own plates and power first, its own feeders down to INTERMEDIATE_DEPTH). A feeder that cannot be built
        fails this build too, naming the input: a factory with ports nothing can feed is a shell (run
        20261122-133537: green science, 346 buildings in 26 minutes, 49 of 51 machines waiting on gears and
        circuits the base made 22 and 0.4 a minute of). Returns a note."""
        if module != "factory":
            return ""
        depth = int(getattr(self, "_intermediate_depth", 0) or 0)
        if depth >= self.INTERMEDIATE_DEPTH:
            return ""
        fac = MODULES["factory"]
        try:
            supplied, pl = fac.supplied_for(self.kn, params)
            from fab import recipes as fr
            makeable = set(fac.can_make(self.kn))
        except BuildError:
            raise
        except Exception:  # noqa: BLE001 - an estimate only: the design itself will say what is wrong
            return ""
        item = params.get("item")
        todo = []
        slot = getattr(pl, "block", None)     # a zone slot's block (modules.SlotPlan): its inputs come by bus
        for it in sorted(supplied):
            if it == item or it in self.PLATE_ORE or it not in fr.RECIPES or it not in makeable:
                continue                      # plates: _plates_first; steel, plastic...: columns and workshops
            want = float((getattr(pl, "supplied", None) or {}).get(it, 0))
            if want <= 0:
                continue
            if slot is not None and getattr(self, "zones", None) is not None:
                maker = self.zones.made_by(slot.n, it)
                if maker is not None:         # the segment's own block of it feeds the bus: build that one
                    if not self.zones.is_built(maker):
                        todo.append((it, round(maker.spec.per_s * 60, 1) / self.FEEDER_HEADROOM, 0.0))
                    continue
            spare = self.spare_rate(it)
            if want - spare > 0.5:
                todo.append((it, want, spare))
        notes = []
        for it, want, spare in todo:
            rate = round((want - spare) * self.FEEDER_HEADROOM, 1)
            self.log(f"[crew] intermediates first: {item} takes {want:.0f} {it}/min, {spare:.0f}/min to spare: "
                     f"a {rate}/min {it} factory first")
            if self.current is not None:      # the goal in hand gets the feeder's time on top of its own budget
                self.current["extra_min"] = float(self.current.get("extra_min") or 0.0) + \
                    MODULE_BUDGET_MIN.get("factory", GOAL_BUDGET_MIN["build"])
            self._intermediate_depth = depth + 1
            try:
                others = sorted(q for q in supplied if q not in (it, item) and q not in self.PLATE_ORE)
                notes.append(self.build({"do": "build", "module": "factory", "item": it, "per_minute": rate,
                                         **({"for_inputs": others} if others else {})}))
            except BuildError as exc:
                raise BuildError(f"not building {item}: its {it} input ({want:.0f}/min) has no supplier, and the "
                                 f"{rate}/min {it} factory it needs first failed: {str(exc)[:160]}") from None
            finally:
                self._intermediate_depth = depth
        return "".join(f"first {n}; " for n in notes)

    def _plates_first(self, module: str, params: dict) -> str:
        """A factory fed plates from outside gets the electric mines that feed it first: one mine chest per plate
        input cell (one drill into one furnace each), counting the chests no belt takes from yet. Returns a note."""
        if module != "factory" or getattr(self, "_in_plates_first", False):
            return ""
        try:
            supplied, pl = MODULES["factory"].supplied_for(self.kn, params)
        except BuildError:
            raise
        except Exception:  # noqa: BLE001 - an estimate only: the design itself will say what is wrong
            return ""
        if getattr(pl, "block", None) is not None:
            return ""                         # a zone slot: its plates come in at the bus head (zones.head_supply)
        notes = []
        for plate in self.PLATE_ORE:
            want = float(getattr(pl, "supplied", {}).get(plate, 0)) if plate in supplied else 0.0
            if want <= 0:
                continue
            spare = self.spare_rate(plate)
            if want - spare <= 0:
                continue
            self.log(f"[crew] plates first: {want:.0f} {plate}/min wanted, {spare:.0f}/min to spare")
            got = Crew._more_plates(self, plate, want - spare)    # (also on the tests' stub crews)
            if got:
                notes.append(got)
        return "".join(f"first {n}; " for n in notes)

    def _more_plates(self, plate: str, short: float) -> str:
        """`short` more plates a minute: a dense field near the base smelted beside it, else an electric mine row
        into furnaces. Returns what was built ("" when nothing was needed or it failed: noted for the planner)."""
        if getattr(self, "_in_plates_first", False):
            return ""
        ore = self.PLATE_ORE[plate]
        rows = math.ceil(short / MODULES["electric_mine"].PLATE_RATE)
        if rows <= 0:
            return ""
        self._in_plates_first = True
        try:
            from .supply import near_field
            full = getattr(self, "patch_full", lambda r: False)
            field = near_field(self, ore, short, "furnace") if not full(ore) else None
            if field is not None:             # a dense field near the base, smelted beside it; a row if none fits
                try:
                    return self.build(field)
                except BuildError as exc:
                    self.log(f"[crew] plates first: no dense {ore} field ({str(exc)[:100]}): a mine row")
            try:
                return self.build({"do": "build", "module": "electric_mine", "resource": ore,
                                   "count": max(2, min(12, rows)), "into": "furnace"})
            except BuildError as exc:         # the courier can still carry plates from wherever they are
                self.foreman.note(f"plates first ({plate}) failed: {str(exc)[:100]}")
                return ""
        finally:
            self._in_plates_first = False

    def supply_spots(self, params: dict) -> list[tuple[float, float]]:
        """Where a plate-fed factory's plates can come from (docs/logistics-plan.md, phase 3): the start tiles of
        lines with plates to spare first, then free plate chests, for the plate it needs most. The factory is
        designed on open ground next to them, its feeds facing them, so its links are short instead of crossing
        the base from wherever open ground happened to be."""
        try:
            supplied, pl = MODULES["factory"].supplied_for(self.kn, params)
        except Exception:  # noqa: BLE001 - an unknown item fails in the design itself
            return []
        want = {p: float(getattr(pl, "supplied", {}).get(p, 0)) for p in self.PLATE_ORE if p in supplied}
        want = {p: r for p, r in want.items() if r > 0}
        if not want and not ((set(supplied) | set(params.get("for_inputs") or ())) - set(self.PLATE_ORE)
                             - {params.get("item")}):
            return []
        # every plate it takes, not only the one it needs most: a circuit factory placed beside copper had its
        # iron walled off behind the base core, four times (run 20261122-133537). And the port that waits for
        # its product. Each spot is (x, y, what), what groups them (Factory._supply_places)
        out: list[tuple] = []
        for plate in sorted(want, key=lambda q: -want[q]):
            budget = self._budget(plate)
            spots: list[tuple[float, tuple[float, float]]] = []
            for site, ln in self.lines(plate):
                load, supply = budget.get(tuple(ln["source"])) or [0.0, self.line_rate(ln, site)]
                if supply - load > 1.0:
                    spots.append((-(supply - load), tuple(ln["start"])))
            for x, y, s in self._free_outputs(plate, None):
                if self.line_at((x, y)) is None:
                    spots.append((0.0, (x, y)))
            out += [(p[0], p[1], plate) for _, p in sorted(spots)[:(4 if not out else 2)]]
        # inputs other than plates (plastic, circuits, cable from a feeder...): their sources count where the
        # factory goes too (run 20261125-214645: advanced circuits were placed by the plates near the base, with
        # plastic 223-354 tiles off at the oil field; no design there could ever be linked)
        # a feeder built for a consumer (Crew._intermediates_first): beside that consumer's other inputs as well,
        # so the consumer can stand within reach of both (run 20261125-214645: the cable feeder for advanced
        # circuits went to the far copper patch, 250+ tiles from the plastic they also take)
        extra = set(params.get("for_inputs") or ())
        for it in sorted((set(supplied) | extra) - set(want) - set(self.PLATE_ORE) - {params.get("item")}):
            srcs = [(x, y) for x, y, _s in self._free_outputs(it, None)]
            if srcs:
                home = self.base.centre or self.w.pos
                srcs.sort(key=lambda q: math.dist(q, home))
                out += [(q[0], q[1], it) for q in srcs[:2]]
        if out:
            item = params.get("item")
            waits = [tuple(pt["approach"]) for _, pt in self.item_ports() if pt.get("item") == item
                     and not pt.get("fed")]
            out += [(t[0], t[1], "out") for t in waits[:2]]
        if len({s[2] for s in out}) == 1:            # one plate and nothing waiting: plain spots, as before
            return [(s[0], s[1]) for s in out]
        return out

    # ------------------------------------------------------------- where a factory may stand (route probe)
    BAD_PLACE_TILES = 20.0         # a replace is refused this close to where the same factory failed
    BAD_PLACE_MIN = 180.0          # ...for this many game minutes

    def _set_probes(self, mod, params: dict) -> dict:
        """Give a build its dry route checks for this goal (reset after it): a factory its route probe and the
        spots where the same factory already failed, an outpost its trunk probe. Returns the old values."""
        if mod.name == "factory":
            old = {"probe": getattr(mod, "probe", None), "avoid": getattr(mod, "avoid", ()),
                   "reach": getattr(mod, "reach", None)}
            try:
                mod.probe = self.route_probe(params)
            except Exception as exc:  # noqa: BLE001 - no probe is the old behaviour, never a failed build
                self.log(f"[crew] route probe not set up for {params.get('item')}: {str(exc)[:120]}")
                mod.probe = None
            mod.reach = getattr(mod.probe, "reach", None)
            mod.avoid = self.bad_places(params.get("item"))
            return old
        if mod.name == "outpost":
            old = {"trunk_probe": getattr(mod, "trunk_probe", None)}
            mod.trunk_probe = self.outpost_probe
            return old
        return {}

    def route_probe(self, params: dict):
        """fn(grid, design) -> [why an input or the output cannot be belted]: before a factory design is built,
        a dry route (belts.route_belt, undergrounds once unlocked, a link's reach and length) on the same ground
        snapshot, the design's own tiles blocked: from the spare sources of every input it takes through a port
        (chests, line tails, belts that carry it) into each port, and from its output chests to a port that waits
        for its item. Run 20261122-133537: the circuit factory for green science was built 4x (~700 buildings
        placed and taken up) beside copper with its iron walled off behind the base core."""
        from . import belts
        from fab import recipes as fr  # noqa: F401 - catalogue loaded by supplied_for
        item = params.get("item")
        supplied, _ = MODULES["factory"].supplied_for(self.kn, params)
        ug = "underground-belt" in self.kn.unlocked
        sources: dict[str, list[tuple[str, tuple]]] = {}
        for it in sorted(set(supplied) - {item}):
            cands: list[tuple[str, tuple]] = []
            for x, y, _s in self._free_outputs(it, None, 0.0):
                ln = self.line_at((x, y))
                cands.append(("line", tuple(ln["start"])) if ln is not None
                             else ("side", (math.floor(x), math.floor(y))))
            for t, _owner in self._belt_taps(it, None, 0.0)[:60]:
                cands.append(("side", tuple(t)))
            if cands:
                sources[it] = cands
        consumers = [(tuple(pt["end"]), tuple(pt["approach"])) for s, pt in self.item_ports()
                     if pt.get("item") == item and not pt.get("fed")]
        others = {tuple(t) for _, pt in self.all_ports() for t in (pt["end"], pt["approach"])}
        blocked = set(self.w.keepout) | set(self.bad_tiles()) | self._strip_tiles()   # bans that rest
        ug_block = self.ug_cover() if ug else None
        log = self.log

        def reach(free, cands, end, appr, avoid) -> str | None:
            near = [c for c in cands if self._manhattan(c[1], end) <= belts.LINK_REACH]
            if not near:
                gap = min(self._manhattan(c[1], end) for c in cands)
                if gap <= self.FACTORY_INPUT_REACH:
                    return None                       # a trunk goal belts it in (far-link alert): no reason to refuse
                return f"its nearest spare source is {gap:.0f} tiles away (a trunk reaches {self.FACTORY_INPUT_REACH})"
            starts: dict = {}
            for kind, c in sorted(near, key=lambda q: self._manhattan(q[1], end))[:12]:
                if kind == "line":
                    if free(c) and c not in avoid:
                        starts[c] = 0.0
                    continue
                for dx, dy in belts.VEC.values():
                    ins, st = (c[0] + dx, c[1] + dy), (c[0] + 2 * dx, c[1] + 2 * dy)
                    if free(ins) and free(st) and not {ins, st} & avoid:
                        starts.setdefault(st, 0.0)
            if not starts:
                return "no free side at any spare source for an inserter and a belt"
            steps = belts.route_belt(free, starts, appr, end, avoid=avoid, underground=ug, margin=40,
                                     limit=150_000, ug_block=ug_block)
            if not steps:
                return "no belt route" + ("" if ug else " (no underground belts yet)")
            if belts.route_len(steps) > belts.LINK_ROUTE_MAX:
                return f"the only belt route runs {belts.route_len(steps)} tiles (a link lays {belts.LINK_ROUTE_MAX})"
            return None

        def probe(grid, d) -> list[str]:
            ports = d.ports()
            taken = set(d.tiles())
            for pt in ports:
                taken |= {tuple(pt[k]) for k in ("chest", "inserter") if pt.get(k) is not None}
            mine = {tuple(pt[k]) for pt in ports for k in ("end", "approach")}

            def ground(t) -> bool:
                return (t in grid.seen and t not in grid.water and t not in grid.cliffs and t not in grid.used
                        and t not in blocked and t not in taken)
            out = []
            for pt in ports:
                it = pt.get("item")
                if it not in sources:
                    continue                          # nothing of it to spare yet: supply, not the route
                end, appr = tuple(pt["end"]), tuple(pt["approach"])
                why = reach(lambda t, e=end, a=appr: t in (e, a) or ground(t), sources[it], end, appr,
                            (others | mine) - {end, appr})
                if why is not None:
                    out.append(f"{it} into its port at {list(appr)}: {why}")
            chests = [tuple(c) for r in d.routes for c in r.chests]
            if consumers and chests:
                errs = []
                for end, appr in sorted(consumers, key=lambda q: min(self._manhattan(c, q[0]) for c in chests))[:3]:
                    why = reach(lambda t, e=end, a=appr: t in (e, a) or ground(t), [("side", c) for c in chests],
                                end, appr, (others | mine) - {end, appr})
                    if why is None:
                        errs = []
                        break
                    errs.append(why)
                if errs:
                    out.append(f"its {item} out to the port waiting for it: {errs[0]}")
            if out:
                log(f"[fab] route probe for {item} at ({d.box[0]}, {d.box[1]}): {out[0]}")
            return out

        def roomy(kind, c) -> bool:
            """Has this source room for a link to start: a line's start tile free, or a free side for the
            inserter and the belt beyond it?"""
            if kind == "line":
                return self._belt_free(c)
            return any(self._belt_free((c[0] + dx, c[1] + dy)) and self._belt_free((c[0] + 2 * dx, c[1] + 2 * dy))
                       for dx, dy in belts.VEC.values())
        # an input none of whose sources has room for a link fails at every place alike: no design is worth
        # trying until room is made there (the zones test, seed 626262: six engine-factory designs, each refused
        # with "no free side at any spare source" for iron, while a burner mine boxed the iron line in)
        boxed = sorted(it for it, cands in sources.items() if not any(roomy(k, c) for k, c in cands[:40]))

        def reach_at(focus) -> list[str]:
            """Inputs with spare sources none of which a trunk could bring to this place (FACTORY_INPUT_REACH), and
            inputs whose sources have no room for a link at all."""
            out = [f"{it}: no spare source has room for a belt to start" for it in boxed]
            for it, cands in sources.items():
                gap = min(self._manhattan(c[1], focus) for c in cands)
                if gap > self.FACTORY_INPUT_REACH - self.REACH_SLACK:
                    out.append(f"{it} {gap:.0f} tiles off")
            return out

        def far_at(focus) -> float:
            """Tiles of trunk this place needs beyond a link's reach, over every input: places are tried least first."""
            return sum(max(0.0, min(self._manhattan(c[1], focus) for c in cands) - (belts.LINK_REACH - self.REACH_SLACK))
                       for cands in sources.values())
        probe.reach, probe.far = reach_at, far_at
        return probe

    # A factory's input from a source beyond a link's reach is a trunk's job (a goal of its own, queued by the
    # far-link alert once the factory stands): it never rules a place out. Run 20261125-214645: advanced circuits
    # were refused at every place 220+ tiles from plastic or cable, and the design ran on for an hour.
    FACTORY_INPUT_REACH = 600

    REACH_SLACK = 15               # tiles from a factory's centre to its farthest port, about

    def bad_places(self, item: str | None) -> list:
        """Where a factory of this item stood with an input no belt could reach (recently): [(centre, input)]."""
        now = self._minute()
        return [(tuple(c), inp) for c, inp, m in (getattr(self, "_bad_places", None) or {}).get(item, [])
                if now - m <= self.BAD_PLACE_MIN]

    def _note_bad_place(self, site: Site) -> None:
        """A factory taken down (replaced or removed) while an input port of it never got a belt for the route
        (not for supply): its spot is remembered, so the same factory is not placed there again (bad_places)."""
        if site is None or site.kind != "factory":
            return
        from .nodes import routing_fail
        walled = []
        for pt in site.params.get("ports", []):
            if not pt.get("item") or pt.get("fed"):
                continue
            f = self._link_fails.get(tuple(pt["end"])) or {}
            if routing_fail(f.get("reason")):
                walled.append(pt["item"])
        if not walled:
            return
        if not isinstance(getattr(self, "_bad_places", None), dict):
            self._bad_places = {}
        item = site.params.get("item")
        if any(math.dist(c, site.centre) < 1.0 for c, _, _ in self._bad_places.get(item, [])):
            return                                    # a replace notes it, then its remove: once is enough
        self._bad_places.setdefault(item, []).append((list(site.centre), sorted(set(walled))[0], self._minute()))
        self._bad_places[item] = self._bad_places[item][-6:]
        self.log(f"[crew] site {site.id} ({item}) goes with its {sorted(set(walled))} input walled in: no {item} "
                 f"factory within {self.BAD_PLACE_TILES:.0f} tiles of ({site.centre[0]:.0f}, {site.centre[1]:.0f}) "
                 "unless its route probe passes")

    def spare_rate(self, item: str) -> float:
        """What the free sources of an item could still give a minute: an unused chest or line all it gets, a
        used line what its links leave over."""
        budget = self._budget(item)
        out = 0.0
        for x, y, s in self._free_outputs(item, None):
            load, supply = budget.get((x, y)) or [0.0, self._source_rate((x, y), item)]
            out += max(0.0, supply - load)
        return out

    BURNS_FUEL = {"burner_mine", "steam_power", "factory"}     # burner drills, stone furnaces, boilers

    def _coal_first(self, module: str, params: dict) -> str:
        """Automation before hands: anything that burns fuel gets a small coal mine first, so its fuel (and
        every refuel after it) comes out of a chest instead of from trees or hand-mining. Returns a note."""
        if module not in self.BURNS_FUEL or self.coal_mines() or self._in_coal_first:
            return ""
        if module == "burner_mine" and params.get("resource") == "coal":
            return ""
        self._in_coal_first = True
        try:
            self.log("[crew] no coal mine yet: building one first, so fuel comes from a chest")
            done = self.build({"do": "build", "module": "burner_mine", "resource": "coal", "count": 2})
            return f"first {done}; then "
        except BuildError as exc:         # no coal near, no room: the build goes on with what fuel we can get
            self.foreman.note(f"coal mine first failed: {str(exc)[:100]}")
            return ""
        finally:
            self._in_coal_first = False

    def _fuel_first(self, module: str, params: dict) -> str:
        """Coal by belt, never by hand: furnace rows and boilers get a coal belt that is linked to an electric
        coal mine's chest, so that mine comes first when no coal chest can take another link. A crew carrying
        coal to every furnace and boiler falls behind as the base grows (run 20261119-121625: 26 furnaces out
        of fuel while 112 coal a minute piled up in chests). Returns a note."""
        if getattr(self, "_in_fuel_first", False) or not self.base.powered_poles() or not coal_belts_ready(self.kn):
            return ""
        em = MODULES["electric_mine"]
        if module == "electric_mine":
            if params.get("resource") == "coal" or em._into(params) not in ("furnace", "line") \
                    or not em.fuel_belt(self.w):
                return ""
        elif module != "steam_power":
            return ""
        return self._coal_source()

    def _coal_source(self) -> str:
        """An electric coal mine with a chest free for one more coal belt, built when there is none."""
        if getattr(self, "_in_fuel_first", False) or "electric-mining-drill" not in self.kn.unlocked \
                or self._free_outputs("coal", None):
            return ""
        self._in_fuel_first = True
        try:
            self.log("[crew] fuel first: an electric coal mine, so the coal belt has a chest to start from")
            return f"first {self.build({'do': 'build', 'module': 'electric_mine', 'resource': 'coal', 'count': 2, 'into': 'chest'})}; "
        except BuildError as exc:            # the build goes on; its coal port waits for a source
            self.foreman.note(f"fuel first (electric coal mine) failed: {str(exc)[:100]}")
            return ""
        finally:
            self._in_fuel_first = False

    # The switch to coal belts: once logistics is researched, the hand-fed furnace rows and power stations built
    # before it get their coal belt where they stand (no tearing down, no rebuilding), one link step at a time.
    RETROFIT_GAP_MIN = 30.0

    def retrofit_due(self) -> list[Site]:
        """Hand-fed electric furnace rows and power stations that should get a coal belt now."""
        if not coal_belts_ready(self.kn) or not self.base.powered_poles():
            return []
        tried = getattr(self, "_retrofit_tried", {})
        now = self._minute()
        return [s for s in self.base.sites if not s.params.get("ports") and now - tried.get(s.id, -1e9) >= self.RETROFIT_GAP_MIN
                and ((s.kind == "steam_power" and any(w == "coal-in" for *_, w in s.chests))
                     or (s.kind == "electric_mine" and s.params.get("into") in ("furnace", "line")
                         and s.params.get("fuel") != "electric"))]      # electric furnaces burn nothing

    def blocked_rows(self) -> list[Site]:
        """Hand-fed furnace rows whose coal strip is taken for good: they are rebuilt with their belt."""
        blocked = getattr(self, "retrofit_blocked", {})
        return [s for s in self.base.of("electric_mine") if s.id in blocked and s.params.get("fuel") == "hand"
                and s.params.get("into") in ("furnace", "line")]

    def retrofit_fuel(self) -> list[str]:
        """Give the due sites their coal input (a source for it first). Returns notes."""
        self.reserve_strips()
        due = self.retrofit_due()
        if not due:
            return []
        if not hasattr(self, "_retrofit_tried"):
            self._retrofit_tried = {}
        notes = []
        first = self._coal_source()
        if first:
            notes.append(first.rstrip("; "))
        for s in due[:6]:
            self._retrofit_tried[s.id] = self._minute()
            try:
                if s.kind == "steam_power":
                    self._coal_port(s)
                else:
                    self._retrofit_row(s)
            except BuildError as exc:            # stays hand-fed; tried again in a while
                self.foreman.note(f"site {s.id}: no room for a coal belt ({str(exc)[:80]})")
                continue
            notes.append(f"site {s.id} ({s.kind}) got a coal belt input")
        return notes

    def _retrofit_row(self, site: Site) -> None:
        """Fit an electric furnace row built before logistics with the coal belt and long-handed inserters a new
        row gets (ElectricMine.unit with fuel), when the tiles behind its chests are free."""
        em = MODULES["electric_mine"]
        drills = sorted({(math.floor(x), math.floor(y)) for name, x, y, _ in site.entities
                         if name == "electric-mining-drill"})
        if not drills:
            raise BuildError("no drills")
        cy = drills[0][1]
        xs = [cx for cx, y in drills if y == cy]
        if len(xs) != len(drills) or any(b - a != em.UNIT_W for a, b in zip(xs, xs[1:])):
            raise BuildError("its drills are not one straight row")
        units = [em.unit(cx, cy, "furnace", fuel=True) for cx in xs]
        need = {u["fuel_inserter"] for u in units} | {t for u in units for t in u["belt"]} | set(em.fuel_port(xs[0], cy))
        own = set(map(tuple, getattr(self, "fuel_strips", {}).get(site.id, [])))
        fb = getattr(self.w, "free_belt", None)
        if not all((t in own and (fb is None or fb(t))) or self.w.free({t}) for t in need):
            self.__dict__.setdefault("retrofit_blocked", {})[site.id] = self._minute()
            raise BuildError("the tiles behind its chests are taken")
        from . import belts
        hit = belts.strip_blocked(self.w, [t for u in units for t in u["belt"]])
        if hit is not None:
            self.__dict__.setdefault("retrofit_blocked", {})[site.id] = self._minute()
            raise BuildError(f"its coal strip would end against the belt at {hit} and spill coal onto it")
        n = len(units)
        self.unlock_items(["long-handed-inserter", "transport-belt"])
        self.make({"long-handed-inserter": n, "transport-belt": 3 * n}, extra=True)
        self.w.recorder = site.entities
        from . import belts
        try:
            # a fuel strip ends dead: it joins no belt (run 20261120-211555: three strips' last belts pointed into
            # the side of iron belts laid earlier, and their spare coal ran onto those iron feeds)
            strip = [t for u in units for t in u["belt"]]
            with belts.job_for(self.w, ["coal"], strip):
                for u in units:
                    qx, qy = u["fuel_inserter"]
                    self.w.place("long-handed-inserter", qx + 0.5, qy + 0.5, "north", {(qx, qy)})
                    for t in u["belt"]:
                        self.w.place("transport-belt", t[0] + 0.5, t[1] + 0.5, "east", {t})
            self.w.keepout |= set(belts.strip_ends(strip))  # no later route passes in front of its dead end
        finally:
            self.w.recorder = None
        end, appr = em.fuel_port(xs[0], cy)
        site.params["ports"] = [{"item": "coal", "end": list(end), "approach": list(appr), "fuel": True,
                                 "per_minute": round(1.4 * n, 1), "fed": None}]
        site.params["fuel"] = "belt"
        self.w.keepout |= {end, appr}
        self.release_strip(site.id)
        getattr(self, "retrofit_blocked", {}).pop(site.id, None)

    def _coal_port(self, site: Site) -> None:
        """A belt input for a power station's coal chest: a burner inserter (it needs no power and burns a little
        of the coal it moves) lifts coal off the end of a link's belt into the chest."""
        from . import belts
        chest = next(((x, y) for x, y, what in site.chests if what == "coal-in"), None)
        if chest is None or site.params.get("ports"):
            return
        c = (math.floor(chest[0]), math.floor(chest[1]))
        for d, (dx, dy) in belts.VEC.items():
            ins, end, appr = (c[0] + dx, c[1] + dy), (c[0] + 2 * dx, c[1] + 2 * dy), (c[0] + 3 * dx, c[1] + 3 * dy)
            if not self.w.free({ins, end, appr}):
                continue
            self.unlock_items(["burner-inserter"])
            self.make({"burner-inserter": 1}, extra=True)
            self.w.recorder = site.entities
            try:
                self.w.place("burner-inserter", ins[0] + 0.5, ins[1] + 0.5, d, {ins})   # picks from the belt end
            except BuildError:
                continue
            finally:
                self.w.recorder = None
            self.w.fuel(ins[0] + 0.5, ins[1] + 0.5, 2)
            site.params["ports"] = [{"item": "coal", "end": list(end), "approach": list(appr), "inserter": list(ins),
                                     "chest": list(c), "fuel": True, "per_minute": 30, "fed": None}]
            site.params["fuel"] = "belt"
            self.w.keepout |= {end, appr}
            return
        site.params["fuel"] = "hand"
        raise BuildError("no free side of the coal chest for a belt")

    # Electric smelting: the furnace a column or row gets is decided once per build (the kit and the build agree),
    # electric once advanced-material-processing-2 is in and the furnaces can be made, and only with the power for
    # them: _power_first builds stations for them first; should that fail, the build falls back to burners.
    SMELTING_MODULES = ("smelter_column", "electric_mine")

    def _pick_furnace(self, mod, params: dict) -> None:
        if mod.name not in self.SMELTING_MODULES or params.get("furnace"):
            return
        try:
            if mod.name == "electric_mine":
                f = mod.furnace(self.w, params)
            elif mod.layout(self.w, params) == "chest":
                f = None
            else:
                f = mod.furnace(self.w, params)
        except Exception:  # noqa: BLE001 - stub modules or params: the module decides itself
            return
        if f:
            params["furnace"] = f

    def _electric_power_check(self, mod, params: dict) -> str:
        """After power first: an electric column or row whose furnaces the network still cannot carry (with
        POWER_HEADROOM; no station could be built) goes up with burner furnaces instead, and says so."""
        from .modules import ELECTRIC_FURNACE, ELECTRIC_FURNACE_KW, burner_furnace
        if mod.name not in self.SMELTING_MODULES or params.get("furnace") != ELECTRIC_FURNACE:
            return ""
        if not self.base.of("steam_power"):
            return ""
        try:
            n = mod._count(params) if mod.name == "electric_mine" else max(1, min(24, int(params.get("count") or 8)))
            from .assess import power_short_kw
            short = power_short_kw(self, n * ELECTRIC_FURNACE_KW)
        except Exception:  # noqa: BLE001 - stub crews: no verdict
            return ""
        if short <= 0:
            return ""
        if getattr(self, "_replacing", None) is not None:
            # an electric swap: burners again would rebuild the same thing for nothing, so it waits for power
            raise BuildError(f"the electric swap waits for power: its {n} furnaces ({n * ELECTRIC_FURNACE_KW:.0f} kW) "
                             f"would leave the network {short:.0f} kW short of its headroom (a steam station first)")
        fallback = burner_furnace(self.kn) if mod.name == "smelter_column" else "stone-furnace"
        params["furnace"] = fallback
        self.log(f"[crew] electric furnaces need {n * ELECTRIC_FURNACE_KW:.0f} kW the network cannot spare "
                 f"({short:.0f} kW short): {fallback} this time")
        return f"{fallback}s (power short by {short:.0f} kW for electric ones); "

    ELECTRIC_MODULES = {"factory", "workshop", "electric_mine", "labs", "research_station", "pumpjack", "silo",
                        "smelter_column"}
    POWER_MARGIN_KW = 500
    POWER_HEADROOM = 1.3          # capacity kept over what the network draws with the new module (assess.py)

    def _power_first(self, module: str, params: dict) -> str:
        """Energy before building: a module that draws power gets the steam stations it needs first, so the
        network never sinks into low power while the base grows. Returns a note."""
        if module not in self.ELECTRIC_MODULES or getattr(self, "_in_power_first", False):
            return ""
        if not self.base.of("steam_power"):
            return ""                                   # the opening builds the first station itself
        try:
            add = self._module_kw(module, params)
        except Exception:  # noqa: BLE001 - an estimate only: the build itself will say what is wrong
            return ""
        notes = []
        self._in_power_first = True
        try:
            for _ in range(4):                          # up to 4 stations (7.2 MW) ahead of one build
                ps = self.power_status()
                # the margin, or POWER_HEADROOM over what it all draws with the new module, whichever is more
                load = ps["capacity_kw"] - ps["headroom_kw"] + add
                if ps["headroom_kw"] - add >= max(self.POWER_MARGIN_KW, (self.POWER_HEADROOM - 1) * load):
                    break
                self.log(f"[crew] power first: {add:.0f} kW more would leave {ps['headroom_kw'] - add:.0f} kW spare")
                try:
                    notes.append(self.build({"do": "build", "module": "steam_power", "engines": 2}))
                except BuildError as exc:
                    self.foreman.note(f"power first failed: {str(exc)[:100]}")
                    break
        finally:
            self._in_power_first = False
        return "".join(f"first {n}; " for n in notes)

    def _module_kw(self, module: str, params: dict) -> float:
        """What a module will draw at full work (kW), estimated from its plan, never from a full layout. Its
        machines all working; its inserters at their average draw (they swing now and then, see _draw_kw)."""
        rate = float(params.get("per_minute", 5))
        kw = lambda name: self._draw_kw(name, 1.0)
        if module == "factory":
            from fab import recipes as fr
            _, pl = MODULES["factory"].supplied_for(self.kn, params)
            asm = sum(v for k, v in pl.machines.items() if k in fr.RECIPES and k not in pl.supplied)
            drills = sum(v for k, v in pl.machines.items() if k in fr.ORE_FOR and k not in pl.supplied)
            return asm * (kw("assembling-machine-1") + 2 * kw("inserter")) + drills * kw("electric-mining-drill")
        if module == "workshop":
            from .production import Planner
            pl = Planner(self.kn).plan(params["item"], rate)
            return Planner(self.kn).power_kw(pl)
        kit = MODULES[module].kit(self.w, self.base, params)
        return sum(kw(n) * k for n, k in kit.items() if (self.kn.entities.get(n) or {}).get("electric"))

    def coal_mines(self) -> list[Site]:
        """Coal mines that run: burner drills into chests, and electric drills on coal (which replace them)."""
        return [s for s in self.base.of("burner_mine") + self.base.of("electric_mine")
                if s.params.get("resource") == "coal" and not _depleted(s)]

    def fuel_item(self) -> str:
        """What to burn: coal once a coal mine runs; before that wood, but only from trees close by (far trees
        cost a long walk every time); with none close, coal mined by hand at the coal patch."""
        if self.coal_mines():
            return "coal"
        if self.w.trees_near(TREES_NEAR):
            return "wood"
        try:
            self.w.find_trees(walk=False)        # remote looks only
        except BuildError:
            pass
        return "wood" if self.w.trees_near(TREES_NEAR) else "coal"

    def _note_churn(self, site: Site) -> None:
        key = _site_key(site.kind, site.params)
        for r in self.removed[::-1]:
            if r["key"] == key and self._minute() - r["minute"] <= 15:
                self.churn.append(f"built {key} again at minute {self._minute()}, {self._minute() - r['minute']:.1f} "
                                  f"min after removing site {r['site']}: that removal was wasted work")
                self.churn = self.churn[-5:]
                return

    def wait(self, seconds: float) -> str:
        """Let the base run for `seconds` of GAME time (not real time: at clock speed 2 that is half as long).
        Ends early when something happens worth replanning for: research done, a milestone, idle labs."""
        until = self.w.now_tick() + seconds * 60
        researched, milestones = len(self.kn.researched), len(self.w.milestones)
        with self.w.step("wait"):
            while self.w.now_tick() < until and self.w.minutes_left > 0.2:
                self.checkpoint()
                self.foreman.patrol(force=True)
                self.w.read_stats(1)
                if len(self.kn.researched) > researched:
                    return "stopped waiting: a technology finished"
                if len(self.w.milestones) > milestones:
                    return "stopped waiting: a milestone was reached"
                if self.base.of("labs") and not (self.w.research or {}).get("current"):
                    return "stopped waiting: the labs have nothing to research"
                time.sleep(max(0.5, 3 / self.w.speed))
        return f"waited {seconds:.0f} game seconds"

    def auto_research(self, exclude: set = frozenset()) -> str | None:
        """Labs must never sit idle: queue the next technology toward the level's milestones that the packs we
        can supply allow, else the cheapest one they allow (`exclude`: already queued)."""
        if not self.base.of("labs"):
            return None
        # packs our own factories put in chests: the foreman carries those to the labs without being asked
        made = {what for s in self.base.sites for *_, what in s.chests if what.endswith("science-pack")}
        def ready(t: str) -> bool:
            tech = self.kn.techs.get(t) or {}
            return (t not in self.kn.researched and t not in exclude and not tech.get("trigger")
                    and all(p in self.kn.researched for p in tech.get("prerequisites", []))
                    and {i["name"] for i in tech.get("unit_ingredients", [])} <= made)
        order = [t for goal in self.target_techs for t in self.kn.tech_path(goal)]
        # electric furnaces first once blue science is made (docs/hybrid-plan.md, section 5): every coal-fed smelter
        # after that is swapped for them, and the coal hauling ends (it is no milestone's prerequisite, so the
        # milestone path alone never picks it)
        if ELECTRIC_TECH in self.kn.techs and "chemical-science-pack" in made:
            order = [ELECTRIC_TECH] + order
        pick = next((t for t in order if ready(t)), None)
        if pick is None:
            # else the cheapest tech on the way to the rocket, never one off it: packs went into radar, gun-turret
            # and lamp while the milestone techs waited for green packs (run 20261120-023103)
            useful = set(self.kn.tech_path("rocket-silo")) if "rocket-silo" in self.kn.techs else set(self.kn.techs)
            avail = [t for t in useful if ready(t) and (self.kn.techs[t].get("unit_count") or 0) > 0]
            pick = min(avail, key=lambda t: self.kn.techs[t].get("unit_count") or 1e9, default=None)
        if pick is None:
            return None
        r = self.w.act({"type": "research", "technologies": [pick], "mode": "append"})
        if r.get("ok"):
            self.w.research = r.get("data") or {"current": pick}
            return pick
        return None

    def design(self, goal: dict) -> str:
        """Preview a chiplet without building it: does it fit here, what does it cost, what must be unlocked."""
        mod = MODULES.get(goal.get("module", "factory"))
        if mod is None:
            raise BuildError(f"unknown module {goal.get('module')}")
        params = {k: v for k, v in goal.items() if k not in ("do", "module")}
        try:
            with self.w.step("design"):
                kit = mod.kit(self.w, self.base, params)
                recipes = mod.recipes(self.w, self.base, params)
                d = getattr(mod, "designs", {}).get((params.get("item"), float(params.get("per_minute", 5))))
        finally:
            getattr(mod, "designs", {}).clear()
        locked = sorted(r for r in recipes if r not in self.kn.unlocked)
        plates = self.kn.kit_plan(kit, {}).raw
        size = ""
        if d:
            x0, y0, x1, y1 = d[0].box
            size = f", about {x1 - x0} x {y1 - y0} tiles, {d[0].plan.summary()}"
        return (f"design {mod.name} {params}: fits{size}; kit {kit}; raw cost {plates}"
                + (f"; needs research for {locked}" if locked else ""))

    def _minute(self) -> float:
        return round(self.w.now_tick() / 3600, 1)

    # ============================================================= recovery
    def refresh_world(self) -> None:
        """The watchdog's first rung: re-read the world and fix the basics before anyone plans again."""
        with self.w.step("refresh"):
            for job in (lambda: self.w.scan(),
                        lambda: self.w.survey_area(*(self.base.centre or self.w.pos), half=48, step=40),
                        self.ensure_space,
                        lambda: self.foreman.patrol(force=True)):
                try:
                    job()
                except BuildError as exc:
                    self.foreman.note(f"refresh: {str(exc)[:100]}")
            for s in list(self.base.sites):
                if s.powered is False and s.electric:
                    try:
                        self.connect(s)
                    except BuildError as exc:
                        self.foreman.note(f"site {s.id}: {str(exc)[:100]}")

    def forget_dug(self, taken: list) -> None:
        """Buildings a dig-out took away for good (its door stays open): out of their sites' books, so nobody
        refuels or empties what is gone; the items are in the inventory for the next build."""
        gone = {(x, y) for _, x, y, _ in taken}
        for s in self.base.sites:
            hit = [e for e in s.entities if (e[1], e[2]) in gone]
            if not hit:
                continue
            s.entities = [e for e in s.entities if (e[1], e[2]) not in gone]
            s.burners = [b for b in s.burners if tuple(b) not in gone]
            s.plate_furnaces = [f for f in s.plate_furnaces if (f[0], f[1]) not in gone]
            s.chests = [c for c in s.chests if (c[0], c[1]) not in gone]
            s.labs = [lab for lab in s.labs if tuple(lab) not in gone]
            s.note = (s.note + "; " if s.note else "") + f"{len(hit)} building(s) taken out to free the character"
        self.foreman.note(f"dug the character out: took {[t[0] for t in taken]} (the way out stays open)")

    def unjam(self, error: str) -> str:
        """The same error keeps failing different goals: fix the crew, not the plan. A character that cannot
        walk is dug out (buildings around it picked up and put back from outside); then the world is re-read."""
        w, did = self.w, []
        if "out_of_reach" in error or "no_path" in error or w.walk_fails:
            with w.step("unjam"):
                if w.trap_pattern() or w.boxed_in():
                    w.escape()
                    did.append("dug the character out")
                else:
                    did.append("the character can move")
        w.walk_fails = 0
        self.refresh_world()
        did.append("rescanned, tidied the inventory, re-wired unpowered sites")
        return "; ".join(did)

    def supply_goals(self) -> list[dict]:
        """The watchdog's "expand": once electric drills run, the biggest measured shortfalls, each sized to its
        gap (supply.shortfalls / supply_goal, the same rule as every shortage); before that the next tier first
        when enough burner drills already run (more of them unblock nothing), else more of whatever plate is
        scarcest (by production per minute), on a patch with room."""
        from .supply import shortfalls, supply_goal
        if "electric-mining-drill" in self.kn.unlocked and self.base.powered_poles():
            sized = [g for g in (supply_goal(self, item, gap) for item, gap in shortfalls(self)) if g]
            if sized:
                return sized[:2]
        rate = lambda p: self.w.rate(p) or 0.0
        scarce = min(("iron-plate", "copper-plate"), key=rate)
        ore = PLATES[scarce]
        goals = []
        if self.burner_drills(ore) < 8:
            goals.append({"do": "build", "module": "burner_mine", "resource": ore, "count": 3})
        if self.w.fuel_units() < 50 and not [s for s in self.base.of("burner_mine")
                                              if s.params.get("resource") == "coal" and not _depleted(s)]:
            goals.insert(0, {"do": "build", "module": "burner_mine", "resource": "coal", "count": 2})
        blk = self.blocker()
        if blk and self.burner_drills() >= 6:
            goals.insert(0, blk)
        return goals or [{"do": "build", "module": "burner_mine", "resource": ore, "count": 2}]

    # ================================================================ alerts
    def coal_budget(self) -> dict:
        """Coal mined against coal burned (the game's own statistics; an estimate from the buildings before
        the first reading), the stock on hand and in chests, and how long it lasts at this rate."""
        st = ((self.w.stats or {}).get("items") or {}).get("coal") or {}
        burners = sum(1 for s in self.base.sites for e in s.entities if e[0] in ("burner-mining-drill",))
        furnaces = sum(1 for s in self.base.sites for e in s.entities if e[0] == "stone-furnace")
        boilers = sum(1 for s in self.base.sites for e in s.entities if e[0] == "boiler")
        est_used = 2.25 * burners + 1.35 * furnaces + 6.0 * boilers      # a boiler at a third of its load
        coal_drills = self.burner_drills("coal")
        made = float(st.get("made_per_min") or 0) if st else 15.0 * coal_drills
        used = float(st.get("used_per_min") or 0) if st else est_used
        stock = self.w.inventory.get("coal", 0) + sum(self.foreman.boiler_coal.values())
        net = made - used
        return {"mined_per_min": round(made, 1), "burned_per_min": round(used, 1), "net_per_min": round(net, 1),
                "stock": stock, "coal_drills": coal_drills,
                "minutes_left": round(stock / -net, 1) if net < 0 else None}

    def alerts(self) -> list[dict]:
        """What is going wrong in the base right now, each with the goal that fixes it (see assess.detect)."""
        from .assess import detect
        return detect(self)

    def research_stall(self) -> str | None:
        """Research that has not moved for 5 game minutes, and the likely reason."""
        rs = self.w.research or {}
        cur, prog, now = rs.get("current"), float(rs.get("progress") or 0), self.w.now_tick()
        if not cur:
            self._progress = None
            return None
        if self._progress is None or self._progress[0] != cur or abs(self._progress[1] - prog) > 1e-3:
            self._progress = (cur, prog, now)
            return None
        mins = (now - self._progress[2]) / 3600
        if mins < 5:
            return None
        packs = [i["name"] for i in self.kn.techs.get(cur, {}).get("unit_ingredients", [])]
        made = {what for s in self.base.sites for *_, what in s.chests}
        missing = [p for p in packs if p not in made and not self.kn.recipe_for(p, hand=True)]
        unmade = [p for p in packs if p not in made]
        why = (f"nothing makes {', '.join(missing)}" if missing else
               f"no factory makes {', '.join(unmade)} (packs are hand-crafted, slowly)" if unmade else
               "the labs may have no power or no packs reaching them")
        return f"research {cur} has not moved for {mins:.0f} min at {prog:.0%}: {why}"

    # The overnight zones test (seed 737373): "feed 200 coal" into hand-fed furnace rows hand-mined 150+ coal, 4 to
    # 5 game minutes a feed, over budget, again and again, while the electric coal mine that would end it waited
    # behind them (feeds are the quick fixes that go first). A feed takes what storage and the coal mines hold, and
    # hand-mines at most this much on top: it bridges until the next feed, it does not replace a coal mine.
    FEED_HAND_MINE_MAX = 50

    def _coal_for_feed(self, count: int) -> int:
        """Coal on hand for a feed of `count`: storage and the coal mines first, then at most FEED_HAND_MINE_MAX
        mined by hand. Returns how much the feed can put in."""
        short = count - self.w.inventory.get("coal", 0)
        short -= self.from_storage("coal", short)
        if short > 0:
            short -= self.from_mines("coal", short)
        if short > 0:
            have = self.w.inventory.get("coal", 0)
            self.gather("coal", have + min(short, self.FEED_HAND_MINE_MAX))
        return max(1, min(count, self.w.inventory.get("coal", 0)))

    def feed(self, site_id, item: str, count: int) -> str:
        """Put `count` of an item into a site: a power station's boiler chest, else its burners' fuel slots."""
        site = self.base.get(site_id)
        if site is None:
            raise BuildError(f"there is no site {site_id}")
        if site.kind == "labs" or str(item).endswith("science-pack"):
            # labs: the foreman's lab round, now (packs from the factories' chests first, feeders and labs filled).
            # Before this "feed" only fuelled: the overseer could not get packs into idle labs (run 20261125-184025:
            # red packs backed up in their factory while every lab waited, research stalled for an hour)
            before = self.foreman.hand_packs
            if self.research_stall():
                self._top_up_stalled(self.w.research or {})   # a handful left: crafted on this round
            self.foreman._labs(True)
            held = {k: v for k, v in self.w.inventory.items() if k.endswith("science-pack") and v}
            return (f"lab round done: packs taken to the labs ({self.foreman.hand_packs - before} crafted by hand)"
                    + (f"; still carrying {held}" if held else ""))
        count = max(1, min(400, int(count)))
        if item == "coal" and self.w.inventory.get(item, 0) < count:
            count = self._coal_for_feed(count)
        else:
            self.gather(item, max(count, self.w.inventory.get(item, 0)))
        targets = [(x, y) for x, y, what in site.chests if what == "coal-in"] or list(site.burners)
        if not targets:
            raise BuildError(f"site {site.id} has nothing that takes {item}")
        per, put = max(1, count // len(targets)), 0
        for x, y in targets:
            put += self.w.insert(x, y, item, per)
        if put <= 0:
            # nothing went in (out of reach, no path, nothing on hand): a failure, so the repeat guards see it
            # (run 20261125-214645: "fed 0 coal into site 11" came back ok six times, the boiler alert fired again
            # each time and paused the goal in hand)
            raise BuildError(f"no {item} went into site {site.id} ({site.kind}): its chest or burners could not be "
                             f"reached or took nothing ({self.w.inventory.get(item, 0)} on hand)")
        return f"fed {put} {item} into site {site.id} ({site.kind})"

    def burner_drills(self, resource: str | None = None) -> int:
        return sum(int(s.params.get("count", 0)) for s in self.base.of("burner_mine")
                   if resource is None or s.params.get("resource") == resource)

    def blocker(self) -> dict | None:
        """The next tier of the base that everything after it waits on (power, labs, the research that
        unlocks automation, the first automated science), as a goal; None once all of it stands."""
        b, kn = self.base, self.kn
        if not self.coal_mines():                  # fuel from a chest, not from trees or hands
            return {"do": "build", "module": "burner_mine", "resource": "coal", "count": 2}
        if not b.of("steam_power"):
            return {"do": "build", "module": "steam_power", "engines": 1}
        if not b.of("labs"):
            return {"do": "build", "module": "labs", "count": 2}
        for tech in ("automation", "electric-mining-drill"):
            if tech in kn.techs and tech not in kn.researched:
                return {"do": "research", "tech": tech}
        if not any(s.params.get("item") == "automation-science-pack" for s in b.of("factory")):
            return {"do": "build", "module": "factory", "item": "automation-science-pack", "per_minute": 5}
        # logistics right after: underground belts let belts cross, which every link and every coal belt needs
        # (players research it among the very first technologies)
        if "logistics" in kn.techs and "logistics" not in kn.researched:
            return {"do": "research", "tech": "logistics"}
        return None

    # ============================================================== workshop
    def cells(self) -> list[tuple[Site, dict]]:
        """Every workshop cell still at work (one a belt factory took over is retired: see upgrade.py)."""
        return [(s, c) for s in self.base.sites for c in s.cells if not c.get("retired")]

    def ready_items(self) -> set[str]:
        """What our automation already makes and a kit may take from a chest instead of hand-crafting: workshop
        cells, belt factories (the mall: belts, inserters, gears...), smelting columns (steel)."""
        out = set(self.cell_items())
        out |= {s.params["item"] for s in self.base.of("factory") if s.params.get("item") and s.chests}
        out |= {what for s in self.base.of("smelter_column") for *_, what in s.chests}
        return out

    def cell_items(self) -> set[str]:
        """Items our workshop cells make (kits may take them from the cells' chests)."""
        return {i for _, c in self.cells() for i in c.get("item_out", {})}

    def cell_rate(self, item: str) -> float:
        return round(sum(c.get("item_out", {}).get(item, 0.0) for _, c in self.cells()), 2)

    # What the network must carry is what it really draws, not every machine's maximum at once: inserters swing
    # now and then (a drain of 1/30 of their maximum the rest of the time) and starved machines wait. Counting
    # maxima overbuilt power many times over (run 20261119-202811: 434 kW drawn, ~15 MW counted, 6.3-9.9 MW
    # built). The game's own reading (stats.power, scenarios from 2026-11-20 on) comes first; older saves get an
    # estimate from each site's machine states.
    INSERTER_DUTY = 0.05           # share of the time an inserter draws its maximum
    UTIL_FLOOR = 0.1               # a site with nothing working still counts this share of its machines

    def _draw_kw(self, name: str, util: float = 1.0) -> float:
        """What one electric building draws on average (kW): its maximum when working, a drain otherwise."""
        ent = self.kn.entities.get(name) or {}
        if not ent.get("electric") or name in POLES:
            return 0.0
        mx = float(ent.get("max_energy_usage") or ent.get("energy_usage") or 0) * 60 / 1000
        drain = mx / 30
        if name.endswith("inserter"):
            return drain + self.INSERTER_DUTY * (mx - drain)
        return drain + util * (mx - drain)

    def measured_power(self, capacity_kw: float) -> dict | None:
        """The game's reading of the networks, when it is there and makes sense: generators deliver about what
        consumers draw, and never more than the engines can give (a unit mix-up fails this)."""
        p = (getattr(self.w, "stats", None) or {}).get("power") or {}
        try:
            use = max(float(p.get("consumption_kw") or 0), float(p.get("consumption_5s_kw") or 0))
            made = float(p.get("production_kw") or 0)
        except (TypeError, ValueError):
            return None
        if not p.get("networks") or use <= 0 or made <= 0:
            return None
        if made > capacity_kw * 1.1 + 50 or not 0.8 * use <= made <= 1.25 * use + 50:
            return None
        return {"consumption_kw": use, "production_kw": made}

    def power_status(self) -> dict:
        """Power capacity (steam engines) against what our electric buildings draw, in kW. demand_kw: the game's
        reading if it has one, else an estimate; full_load_kw: every machine at its maximum at once."""
        # only engines that feed the grid: a station not wired in, or an engine no pole reaches, adds nothing
        cap = 900.0 * sum(max(0, int(s.params.get("engines", 1)) - int(s.health.get(UNPLUGGED, 0)))
                          for s in self.base.of("steam_power") if s.powered is not False)
        full = est = ready = 0.0
        for s in self.base.sites:
            n = sum(s.health.values())
            util = (s.health.get("working", 0) + s.health.get("low_power", 0)) / n if n else 1.0
            util = max(self.UTIL_FLOOR, util)
            mine = 0.0
            for e in s.entities:
                mine += self._draw_kw(e[0], 1.0) if not e[0].endswith("inserter") else \
                    float((self.kn.entities.get(e[0]) or {}).get("max_energy_usage") or 0) * 60 / 1000
                est += self._draw_kw(e[0], util)
            full += mine
            ins = [pt for pt in s.params.get("ports", []) if (pt.get("item") or "pack" in pt) and not pt.get("fuel")]
            if ins and all(pt.get("fed") or pt.get("pack") for pt in ins):
                ready += mine                         # a consumer with every input belted: it will run
        meas = self.measured_power(cap)
        demand = meas["consumption_kw"] if meas else est
        # what the base draws once its fed sites run (run 20261122-133537: 1.48 MW measured on 4.5 MW with almost
        # everything idle for lack of circuits; green, red, circuits and gears at work want more than 4.5 MW)
        expected = EXPECTED_LOAD * ready
        # power booked for electric furnace swaps queued but not built yet (upgrade.booked_power_kw): the planner
        # counts it as drawn, so nothing else takes the headroom the swap needs
        try:
            from .upgrade import booked_power_kw
            booked = booked_power_kw(self)
        except Exception:  # noqa: BLE001 - stub crews in tests
            booked = 0.0
        return {"capacity_kw": round(cap), "demand_kw": round(demand), "demand_source": "measured" if meas else
                "estimated", "full_load_kw": round(full), "expected_kw": round(expected), "booked_kw": round(booked),
                "stations": len(self.base.of("steam_power")),
                "headroom_kw": round(cap - max(demand, expected) - booked)}

    def courier(self, force: bool = False) -> str | None:
        """One round of the workshop: empty the output chests of everything some cell needs, then top up every
        cell's input chest to COURIER_BUFFER_MIN minutes of its inputs. Final products (science packs, kit items
        nobody consumes) stay in their chests for the labs and the builder. Never waits and never hand-mines:
        what is missing is noted, and the next round tries again."""
        cells = self.cells()
        if not cells:
            return None
        now = self.w.now_tick()
        if not force and now - self._courier_tick < COURIER_GAP_S * 60:
            return None
        self._courier_tick = now
        with self.w.step("courier"):
            contents = self._chest_contents(cells)
            stock: dict[str, int] = {}
            for _, c in cells:
                for q in c["out"]:
                    for it, n in contents.get(tuple(q), {}).items():
                        stock[it] = stock.get(it, 0) + n
            self.cell_stock = stock
            needed = {i for _, c in cells for i in c.get("item_in", {})}
            took = gave = 0
            for s, c in self._route(cells):
                for q in c["out"]:
                    for it, n in contents.get(tuple(q), {}).items():
                        if (it in needed or (c.get("sink") and n >= 200)) and n > 0:   # sinks go to storage
                            self.ensure_space(6)
                            took += self.w.extract(q[0], q[1], it, n)
            short: dict[str, int] = {}
            order = sorted(cells, key=lambda sc: sum(sum(contents.get(tuple(q), {}).values()) for q in sc[1]["in"]))
            for s, c in order:
                if not c["in"]:
                    continue
                have = {}
                for q in c["in"]:
                    for it, n in contents.get(tuple(q), {}).items():
                        have[it] = have.get(it, 0) + n
                for it, rate in c.get("item_in", {}).items():
                    if it in (c.get("belted") or ()):    # a belt from another factory brings it now
                        continue
                    target = max(math.ceil(rate * COURIER_BUFFER_MIN), 10)
                    stack = int((self.kn.items.get(it) or {}).get("stack_size") or 50)
                    target = min(target, stack * 8)
                    give = target - have.get(it, 0)
                    if give < max(5, target // 4):
                        continue
                    spare = lambda: self.w.inventory.get(it, 0) - self.reserved.get(it, 0)
                    if spare() < give:
                        self._grab(it, give - max(0, spare()))
                    give = min(give, max(0, spare()))           # never what the goal in hand is gathering
                    if give <= 0:
                        short[it] = short.get(it, 0) + target - have.get(it, 0)
                        continue
                    chests = c["in"]
                    if c.get("in_map") and it in c["in_map"]:
                        chests = [c["in"][c["in_map"][it]]]
                    per = math.ceil(give / len(chests))
                    for q in chests:
                        gave += self.w.insert(q[0], q[1], it, per)
            self.cell_short = short
            msg = f"courier: took {took}, delivered {gave}" + (f"; short of {dict(list(short.items())[:5])}" if short else "")
            if short:
                self.foreman.note(f"workshop short of {', '.join(list(short)[:4])}: more supply needed")
            return msg

    def _grab(self, item: str, n: int) -> int:
        """Up to n of an item without waiting: storage, then chests and furnaces of our mines and cells."""
        got = self.from_storage(item, n)
        if got >= n:
            return got
        spots = [(x, y) for s in self.base.sites for x, y, what in s.chests if what == item]
        spots += [(x, y) for s in self.base.sites for x, y, p in s.plate_furnaces if p == item]
        for x, y in sorted(spots, key=lambda q: math.dist(q, self.w.pos)):
            if got >= n:
                break
            self.ensure_space(6)
            got += self.w.extract(x, y, item, n - got)
        return got

    def _chest_contents(self, cells) -> dict[tuple, dict[str, int]]:
        """What every cell chest holds, from a few remote looks (one per cluster of cells)."""
        spots = {tuple(q) for _, c in cells for q in c["in"] + c["out"]}
        out: dict[tuple, dict[str, int]] = {}
        todo = set(spots)
        while todo:
            p = min(todo, key=lambda q: math.dist(q, self.w.pos))
            if math.dist(p, self.w.pos) > 200:
                self.w.near(p[0], p[1], dist=30)
            try:
                d = self.w.observe(32, x=p[0], y=p[1], include=["entities"], names=["iron-chest", "steel-chest"])
            except BuildError:
                todo.discard(p)
                continue
            for e in d.get("entities") or []:
                k = (e["x"], e["y"])
                if k in spots:
                    out[k] = dict(((e.get("inventories") or {}).get("contents") or {}))
                    todo.discard(k)
            todo = {q for q in todo if math.dist(q, p) > 30}
        return out

    def _route(self, cells):
        """Cells in walking order: always the nearest next."""
        left, here, out = list(cells), self.w.pos, []
        while left:
            sc = min(left, key=lambda sc: math.dist((sc[1]["x"], sc[1]["y"]), here))
            left.remove(sc)
            out.append(sc)
            here = (sc[1]["x"], sc[1]["y"])
        return out

    # ============================================================ inventory
    # There is no "drop" action: whatever the crew cannot carry goes into storage chests. They stand together in
    # a yard in a clearing (never beside wherever the character happened to stand: run 20261119-011655 parked
    # them around a boiler, so the next pipe had nowhere to be laid from), and a build that needs a chest's tile
    # moves the chest, contents and all (FreeWorld.free/clear call make_way).
    STORAGE_CHESTS = ("iron-chest", "wooden-chest", "steel-chest")
    YARD = (4, 2)              # storage chests stand in a 4x2 block (8 iron chests hold 256 stacks)
    YARD_CLEAR = 2             # ... with this much free ground around it, so walks and builds pass by
    YARD_AWAY = 12             # a yard a build overran is set up again at least this far from that build

    def slots_used(self) -> int:
        stack = lambda i: int((self.kn.items.get(i) or {}).get("stack_size") or 50)
        return sum(math.ceil(n / stack(i)) for i, n in self.w.inventory.items() if n > 0)

    def storage_chests(self) -> list[tuple[float, float]]:
        return [(x, y) for s in self.base.of("storage") for x, y, _ in s.chests]

    def stored(self, item: str) -> int:
        """How many of an item the storage chests hold (the crew's own book: only the crew uses them)."""
        return sum(held.get(item, 0) for held in self.storage_held.values())

    def _sync_storage(self) -> None:
        """Tell the world which tiles hold storage chests: a build may claim them, and the chest moves."""
        self.w.movable = {(math.floor(x), math.floor(y)) for x, y in self.storage_chests()}

    def ensure_space(self, min_free: int = 12, force: bool = False) -> int:
        """Keep room in the inventory by leaving things in storage chests (built when needed); kits and crafts
        take them back out (make, gather, plates). force: an action already failed for room, so free `min_free`
        slots whatever the count says. Returns the slots freed."""
        if not force and INVENTORY_SLOTS - self.slots_used() >= min_free:
            return 0
        with self.w.step("storage"):
            self.w.refresh()
            free = INVENTORY_SLOTS - self.slots_used()
            need = min_free if force else min_free - free
            if need <= 0:
                return 0
            plan, freed = self._stash_plan(need)
            if not plan:
                self.foreman.note("inventory full of what the build in hand needs: build something with it")
                return 0
            for item, n in plan.items():
                self._stash(item, n)
            return freed

    def _keep(self, item: str) -> int:
        """What stays on hand whatever happens: fuel for the base, chests and poles to build with, and the kit
        of the build in hand (an outpost's column, trunk and pole line too, until they stand)."""
        stack = int((self.kn.items.get(item) or {}).get("stack_size") or 50)
        keep = max(self.kit_hold.get(item, 0), self.reserved.get(item, 0),
                   (getattr(self, "outpost_hold", None) or {}).get(item, 0))
        if item in FUEL:
            keep = max(keep, KEEP_ON_HAND.get(item, 100))
        if item in self.STORAGE_CHESTS:
            keep = max(keep, 10)
        if item == POLE:
            keep = max(keep, stack)
        return keep

    def _stash_plan(self, need: int) -> tuple[dict[str, int], int]:
        """What to leave in storage to free `need` slots, the least wanted first: surplus plates, ore and stone
        (KEEP_ON_HAND) and workshop intermediates over two stacks; then anything over one stack; then whole
        stacks of buildings and intermediates; plates, ore and stone last. _keep's items always stay. Returns (item -> count, slots freed)."""
        inv = {i: n for i, n in self.w.inventory.items() if n > 0}
        stack = lambda i: int((self.kn.items.get(i) or {}).get("stack_size") or 50)
        made = self.cell_items()
        plan: dict[str, int] = {}
        freed = 0
        left = lambda i: inv[i] - plan.get(i, 0)
        tiers = (lambda i: KEEP_ON_HAND.get(i, 2 * stack(i) if i in made else None),
                 lambda i: max(stack(i), KEEP_ON_HAND.get(i, 0)),
                 lambda i: None if i in KEEP_ON_HAND else 0,
                 lambda i: 0)
        for tier in tiers:
            for i in sorted(inv, key=lambda q: -math.ceil(left(q) / stack(q))):
                if freed >= need:
                    return plan, freed
                keep = tier(i)
                if keep is None:
                    continue
                over = left(i) - max(keep, self._keep(i))
                if over <= 0:
                    continue
                before = math.ceil(left(i) / stack(i))
                plan[i] = plan.get(i, 0) + over
                freed += before - math.ceil(left(i) / stack(i))
        return plan, freed

    def _stash(self, item: str, n: int, exclude=frozenset()) -> int:
        """Put up to n of an item into storage chests (those that hold it already first), building chests as
        they fill. Returns how many went in."""
        exclude = set(exclude) | self._leaving
        left, put = min(n, self.w.inventory.get(item, 0)), 0
        chests = sorted((p for p in self.storage_chests() if p not in exclude),
                        key=lambda p: (item not in self.storage_held.get(p, {}), math.dist(p, self.w.pos)))
        for p in chests + [None] * 3:
            if left <= 0:
                break
            new = p is None
            if new:
                p = self._new_storage()
            k = self.w.insert(p[0], p[1], item, left)
            if k:
                held = self.storage_held.setdefault(p, {})
                held[item] = held.get(item, 0) + k
                left, put = left - k, put + k
            elif new:
                break                                 # a new, empty chest took nothing: something else is wrong
        return put

    def _strict_free(self, t) -> bool:
        z = getattr(self.w, "zones", None)
        return not self.w.blocked(t) and t not in self.w.keepout and t in self.w.seen \
            and not (z is not None and z.blocks((t,)))

    def _yard_tiles(self, yard) -> list[tuple[int, int]]:
        return [(yard[0] + i, yard[1] + j) for j in range(self.YARD[1]) for i in range(self.YARD[0])]

    def _find_yard(self, avoid: set) -> tuple[int, int] | None:
        """A clearing for the storage yard near the middle of the base: the block and the ground around it free,
        seen, off ore, and away from tiles a build just claimed."""
        cx, cy = self.base.centre or self.w.pos
        try:
            self.w.observe(32, x=cx, y=cy)
        except BuildError:
            pass
        ore = set().union(*(set(v) for v in self.w.resources.values())) if self.w.resources else set()
        m, (W, H) = self.YARD_CLEAR, self.YARD
        claimed = set(avoid) | self._claim
        if claimed:
            xs, ys = [t[0] for t in claimed], [t[1] for t in claimed]
            box = (min(xs) - self.YARD_AWAY, max(xs) + self.YARD_AWAY, min(ys) - self.YARD_AWAY, max(ys) + self.YARD_AWAY)
        else:
            box = None
        here = (math.floor(cx), math.floor(cy))
        for r in range(6, 41, 2):
            ring = [(here[0] + dx, here[1] + dy) for dx in range(-r, r + 1) for dy in range(-r, r + 1)
                    if max(abs(dx), abs(dy)) == r]
            for x0, y0 in sorted(ring, key=lambda q: math.dist(q, self.w.pos)):
                block = [(x0 - m + i, y0 - m + j) for i in range(W + 2 * m) for j in range(H + 2 * m)]
                if box and any(box[0] <= t[0] <= box[1] and box[2] <= t[1] <= box[3] for t in block):
                    continue
                if all(t not in ore and self._strict_free(t) for t in block):
                    return (x0, y0)
        return None

    def _storage_spot(self, avoid: set) -> tuple[int, int] | None:
        """The next free place in the yard (a new yard when it is full or a build claimed it); beside the
        character when the base has no clearing at all."""
        avoid = set(avoid) | self._claim
        for _ in range(2):
            if self.yard is None:
                self.yard = self._find_yard(avoid)
                if self.yard is None:
                    break
                self.log(f"[crew] storage yard at {self.yard}")
            spot = next((t for t in self._yard_tiles(self.yard)
                         if t not in avoid and t not in self._bad_storage and self._strict_free(t)), None)
            if spot is not None:
                return spot
            self.yard = None                              # full, or built over: another clearing
        self.w.observe(16)
        here = (math.floor(self.w.pos[0]), math.floor(self.w.pos[1]))
        ore = set().union(*(self.w.resources.get(r, set()) for r in ("iron-ore", "copper-ore", "coal", "stone")))
        for r in range(2, 7):
            ring = [(here[0] + dx, here[1] + dy) for dx in range(-r, r + 1) for dy in range(-r, r + 1)
                    if max(abs(dx), abs(dy)) == r]
            for t in sorted(ring, key=lambda q: (q in ore, math.dist(q, here))):
                if t not in avoid and t not in self._bad_storage and self._strict_free(t) and not self.w.sealed({t}):
                    return t
        return None

    def _new_storage(self, avoid=frozenset()) -> tuple[float, float]:
        # never a chest the build in hand needs: run 20261125-154120 set the iron chests made for an outpost's
        # smelting column down as storage mid-build, and the column stopped after 125 buildings "no iron-chest"
        held = max((getattr(self, "kit_hold", None) or {}).get("iron-chest", 0),
                   (getattr(self, "outpost_hold", None) or {}).get("iron-chest", 0))
        spare = self.w.inventory.get("iron-chest", 0) - held
        chest = "iron-chest" if spare > 0 or self.w.inventory.get("iron-plate", 0) >= 8 else "wooden-chest"
        if self.w.inventory.get(chest, 0) - (held if chest == "iron-chest" else 0) <= 0:
            self.w.craft(self.kn.recipe_for(chest, hand=True) or chest, 1)
        spot = self._storage_spot(set(avoid))
        if spot is None:
            raise BuildError("no free tile for a storage chest: no clearing near the base and none beside the character")
        sites = self.base.of("storage")
        rec: list = []
        outer, self.w.recorder = self.w.recorder, rec   # a chest set down mid-build is no part of that build
        try:
            self.w.place(chest, spot[0] + 0.5, spot[1] + 0.5, "north", {spot})
        except BuildError:
            self._bad_storage.add(spot)           # never the same refused tile again
            raise
        finally:
            self.w.recorder = outer
        pos = (spot[0] + 0.5, spot[1] + 0.5)
        if sites:
            sites[0].chests.append((*pos, "storage"))
            sites[0].entities += rec
        else:
            self.base.add(Site("storage", {}, pos, chests=[(*pos, "storage")], entities=rec,
                               built_minute=self._minute()))
        self.storage_held[pos] = {}
        self._sync_storage()
        return pos

    def make_way(self, tiles: set) -> None:
        """A build needs these tiles: each storage chest on them is emptied into the yard and picked up (a new
        yard in another clearing when the build claims the yard itself)."""
        hit = [p for p in self.storage_chests() if (math.floor(p[0]), math.floor(p[1])) in tiles]
        if not hit:
            return
        if self.yard is not None and set(self._yard_tiles(self.yard)) & tiles:
            self.yard = None
        outer = self._claim
        self._claim = outer | set(tiles)
        try:
            with self.w.step("storage"):
                for p in hit:
                    self._move_chest(p)
        finally:
            self._claim = outer
        self.log(f"[crew] moved {len(hit)} storage chest(s) out of a build's way")

    def _chest_held(self, p) -> dict[str, int]:
        """What a storage chest holds: the game's word if it answers, else the crew's book."""
        try:
            d = self.w.observe(2, x=p[0], y=p[1], include=["entities"], names=list(self.STORAGE_CHESTS))
            for e in d.get("entities") or []:
                if (e["x"], e["y"]) == tuple(p):
                    return {k: v for k, v in ((e.get("inventories") or {}).get("contents") or {}).items() if v > 0}
        except BuildError:
            pass
        return {k: v for k, v in self.storage_held.get(p, {}).items() if v > 0}

    def _move_chest(self, p) -> None:
        x, y = p
        self._leaving = self._leaving | {p}
        try:
            for item, n in self._chest_held(p).items():
                left = n
                for _ in range(12):
                    if left <= 0:
                        break
                    got = self.w.extract(x, y, item, left)
                    if got <= 0:                          # no room on hand: leave other things in the yard first
                        if not self.ensure_space(8, force=True):
                            break
                        got = self.w.extract(x, y, item, left)
                        if got <= 0:
                            break
                    left -= got
                    held = self.storage_held.setdefault(p, {})
                    held[item] = max(0, held.get(item, 0) - got)
                    self._stash(item, got)
                if left > 0:
                    raise BuildError(f"a storage chest at ({x}, {y}) is in the way of the build and still holds "
                                     f"{left} {item} that found no room elsewhere")
            site = next((s for s in self.base.of("storage") if any((cx, cy) == (x, y) for cx, cy, _ in s.chests)), None)
            ent = next((e for e in (site.entities if site else []) if (e[1], e[2]) == (x, y)), None)
            name = ent[0] if ent else "iron-chest"
            self.w.pickup(name, x, y)
            if site is not None:
                site.chests = [c for c in site.chests if (c[0], c[1]) != (x, y)]
                site.entities = [e for e in site.entities if (e[1], e[2]) != (x, y)]
                if not site.chests:
                    self.base.remove(site)
            self.storage_held.pop(p, None)
            self._sync_storage()
        finally:
            self._leaving = self._leaving - {p}

    def from_storage(self, item: str, want: int) -> int:
        """Up to `want` of an item out of the storage chests that hold it (the crew's book says which)."""
        got = 0
        for p in sorted(self.storage_chests(), key=lambda q: math.dist(q, self.w.pos)):
            if got >= want:
                break
            held = self.storage_held.get(p)
            if held is not None and held.get(item, 0) <= 0:
                continue
            n = self.w.extract(p[0], p[1], item, want - got)
            got += n
            if held is not None:
                held[item] = max(0, held.get(item, 0) - n)
                if not n and INVENTORY_SLOTS - self.slots_used() > 1:
                    held[item] = 0                        # nothing came out though there was room: the book was wrong
        return got

    def coal_chests(self) -> list[tuple[float, float]]:
        return [(x, y) for s in self.base.sites for x, y, what in s.chests if what == "coal"]

    # ================================================================ power
    def existing_poles(self) -> set[tuple[int, int]]:
        poles = {(math.floor(x), math.floor(y)) for (x, y), e in self.w.entities.items() if e.get("name") in POLES}
        for s in self.base.sites:
            poles |= {tuple(q) for q in s.poles}
        return poles

    def connect(self, site: Site) -> str:
        """Wire a site to the power network: the cheapest bridge over poles that already stand, then a check."""
        with self.w.step("connect"):
            return self._connect(site)

    def _connect(self, site: Site) -> str:
        plugged = self._plug_generators(site) if site.kind == "steam_power" else 0
        anchors = self.base.powered_poles(exclude=site)
        if not anchors:
            site.powered = False
            return "no power network to connect to yet"
        if not site.poles:
            site.powered = self.check_power(site)
            return "powered" if site.powered else "has no poles to connect"
        if site.powered and self.check_power(site):
            if getattr(self, "_power_by_grid", False) and site.kind != "steam_power":
                # "powered" only because its poles are on the grid while more than a tenth of its machines say
                # no_power: machines outside every pole's supply area are covered first. Run 20261122-133537:
                # "connect site 33" answered "already powered" 7x while 30 of its inserters stood dark
                covered = self._cover_unpowered(site)
                left = getattr(self, "_cover_left", 0)
                if covered:
                    time.sleep(1.0 / self.w.speed)
                    site.powered = self.check_power(site)
                    return (f"{covered} pole(s) placed for machines no pole reached"
                            + (f"; {left} still out of reach" if left else "") + self.short_note())
                if left:
                    raise BuildError(f"site {site.id}: {left} machine(s) say no_power and no free tile for a pole "
                                     "reaches them (the layout leaves none): connecting again changes nothing; the "
                                     "site has to be rebuilt or removed")
            return (f"plugged in {plugged} engine(s) with a pole each" if plugged else "already powered") + \
                (self.short_note() if site.kind != "steam_power" else "")
        self.w.doing(f"routing power to site {site.id}")
        free = self._pole_freer(self._own_slot(site))
        # Every group of the site's poles out of wire reach of the others (two pumpjacks on wells far apart,
        # cells between empty slots) is joined on its own: one bridge powers only the group it starts from.
        net = set(anchors)
        try:                       # the game's word over our records: poles cut off from the grid are no anchor
            net -= self._off_grid_anchors(site, anchors)
        except BuildError:
            pass
        except ArenaError as exc:
            if exc.code in DEAD_RUN:
                raise
        if not net:
            net = set(anchors)
        path: list = []
        for group in sorted(_pole_groups(site.poles), key=lambda g: min(math.dist(a, q) for a in net for q in g)):
            if net & set(group):
                continue
            hop = route_poles(group, list(net), self.existing_poles() | set(path), free)
            if hop is None:
                # a far group (an oil field): poles may only stand on ground we have seen, so look along the line
                # to the network first (remote looks, walking where they do not reach), then route again
                s0 = group[0]
                a = min(net, key=lambda q: math.dist(q, s0))
                n = max(1, int(math.dist(a, s0) // 40))
                self.w.doing(f"surveying a power line for site {site.id}")
                for k in range(n + 1):
                    x, y = s0[0] + (a[0] - s0[0]) * k / n, s0[1] + (a[1] - s0[1]) * k / n
                    try:
                        self.w.look(x, y, radius=32)
                    except BuildError:
                        pass
                big = self._big_line(group, net, free) if math.dist(a, s0) >= BIG_LINE else None
                if big is not None:                         # big poles: a fifth of the poles a long line needs
                    self._place_big_line(site, big)
                    path += [q for _, q in big]
                    net |= set(group) | {q for _, q in big}
                    continue
                kind = self._line_pole(math.dist(a, s0))
                reach = {"reach": MEDIUM_REACH, "old_reach": REACH} if kind == MEDIUM_POLE else {}
                hop = route_poles(group, list(net), self.existing_poles() | set(path), free, margin=24,
                                  max_nodes=150000, **reach)
                if hop is None and kind == MEDIUM_POLE:     # the longer reach found nothing either: small ones
                    kind = POLE
                    hop = route_poles(group, list(net), self.existing_poles() | set(path), free, margin=24,
                                      max_nodes=150000)
            else:
                kind = POLE
            if hop is None:
                site.powered = False
                raise BuildError(f"no pole route from site {site.id} to the network"
                                 + (f" ({len(path)} poles placed for its other parts)" if path else ""))
            if hop:
                if self.w.inventory.get(kind, 0) < len(hop):
                    self.unlock_items([kind])
                    self.make({kind: len(hop)})
                self.w.recorder = site.entities
                try:
                    for t in hop:
                        self.w.place(kind, t[0] + 0.5, t[1] + 0.5, "north", {t})
                        site.poles.append(t)
                finally:
                    self.w.recorder = None
            path += hop
            net |= set(group) | set(hop)
        time.sleep(1.0 / self.w.speed)                       # let the game update the machines' status
        site.powered = self.check_power(site) if site.electric or site.kind == "steam_power" else True
        covered = 0
        if not site.powered and site.kind != "steam_power":
            covered = self._cover_unpowered(site)            # machines outside every pole's supply area
            if covered:
                time.sleep(1.0 / self.w.speed)
                site.powered = self.check_power(site)
        if not site.powered:
            if site.kind == "steam_power":
                raise BuildError(f"site {site.id}: placed {len(path)} pole(s) but the station is still not on the "
                                 "grid (an engine unplugged, or its poles on a network of their own)")
            grid = self.on_grid(site.poles)
            raise BuildError(f"site {site.id}: placed {len(path)} pole(s)" + (f" and {covered} for uncovered machines"
                                                                            if covered else "")
                             + (" but its poles are on a network of their own (the game's network id differs from "
                                "the grid's): no pole route from it reaches a pole that is on the grid"
                                if grid is False else
                                " and its poles are on the grid, but its machines still have no power: the grid "
                                "itself has none to give (engines, boilers or their coal)" if grid is True else
                                " but its machines still have no power"))
        extra = (f", plugged in {plugged} engine(s)" if plugged else "") + \
            (f", {covered} pole(s) for machines no pole reached" if covered else "") + \
            (self.short_note() if site.kind != "steam_power" else "")
        return (f"powered with {len(path)} new pole(s)" if path else "powered over existing poles") + extra

    def _off_grid_anchors(self, site: Site, anchors) -> set:
        """Anchor poles near the site that the game puts on another network than the grid's first pole: our
        records call their sites powered, but a pole taken up somewhere cut them off. Sites with such poles are
        marked unpowered (the foreman's rewire connects them again). Run 20261122-133537: a relink took up a pole
        that site 19's power ran through; four connects routed to the dead poles next door ("placed 0 pole(s) but
        its machines still have no power") and the watchdog stalled for 15 minutes. Empty without network ids."""
        grid = self.base.power_pole
        if grid is None:
            return set()
        main = {n for n in self.pole_networks([grid]).values() if n is not None}
        if not main:
            return set()
        c = site.centre
        near = [tuple(a) for a in anchors if math.dist((a[0] + 0.5, a[1] + 0.5), c) <= 28]
        if not near:
            return set()
        nets = self.pole_networks(near)
        off = {a for a, n in nets.items() if n is not None and n not in main}
        for s in self.base.sites:
            if s is not site and s.powered and off & {tuple(q) for q in s.poles}:
                s.powered = False
                self.foreman.note(f"site {s.id}: its poles are cut off from the grid (another network id)")
        return off

    def _cover_unpowered(self, site: Site) -> int:
        """A pole (and its bridge to the grid) for each of the site's machines that reports no power and that no
        pole's supply area reaches (a pole taken up, a layout with a hole). Returns the poles placed."""
        self._cover_left = 0                      # dark machines no pole could be placed for (see _connect)
        names = sorted({e[0] for e in site.entities if e[0] in ELECTRIC})
        if not names:
            return 0
        try:
            dark = [p for p, st in self.look(site, names).items() if st == "no_power"]
        except BuildError:
            return 0
        placed = 0
        left = max(0, len(dark) - 6)
        sizes = {(round(e[1], 2), round(e[2], 2)): e[0] for e in site.entities}
        for x, y in dark[:6]:
            t = (math.floor(x), math.floor(y))
            name = sizes.get((round(x, 2), round(y, 2)))
            half = (max(self.kn.size(name)) - 1) // 2 if name and hasattr(self.kn, "size") else 0
            try:
                plan = self._pole_plan(t, half=half)    # a drill is powered from 3 tiles off its centre
            except BuildError as exc:
                self.foreman.note(f"site {site.id}: no pole for the machine at ({x}, {y}): {str(exc)[:80]}")
                left += 1
                continue
            if not plan:
                continue                          # a pole reaches it: its network is the problem, not coverage
            kit = Counter(kind for kind, _ in plan)
            self.unlock_items(list(kit))
            self.make_for(dict(kit))
            self.w.recorder = site.entities
            try:
                for kind, q in plan:
                    self.w.place(kind, q[0] + 0.5, q[1] + 0.5, "north", {q})
                    site.poles.append(q)
                    placed += 1
            except BuildError as exc:
                self.foreman.note(f"site {site.id}: a pole for the machine at ({x}, {y}) failed: {str(exc)[:80]}")
                left += 1
            finally:
                self.w.recorder = None
        self._cover_left = left
        return placed

    def _big_line(self, group: list, net: set, free) -> list[tuple[str, tuple[int, int]]] | None:
        """A straight line of big poles from beside the network to beside a far group of the site's poles, or
        None (not unlocked, no steel, or a stretch with no free 2x2 spot). Each big pole stands on the free 2x2
        spot nearest its point on the line, within wire reach of the one before; the end poles stand within a
        small pole's wire of the network pole and of the site's pole (a wire spans the shorter reach of its two
        poles). Planned in straight steps, not searched tile by tile: a 30-tile reach makes that search crawl.
        Returns [(kind, top-left tile)] in order from the network."""
        unlocked = getattr(self.kn, "unlocked", None) or set()
        if BIG_POLE not in unlocked:
            return None
        s0 = min(group, key=lambda q: min(math.dist(q, a) for a in net))
        a = min(net, key=lambda q: math.dist(q, s0))
        length = math.dist(a, s0)
        n = max(2, math.ceil((length - 12) / BIG_STEP) + 1)
        steel = self.w.inventory.get("steel-plate", 0) + self.stored("steel-plate")
        if self.w.inventory.get(BIG_POLE, 0) < n and steel < 5 * n and "steel-plate" not in self.ready_items():
            return None
        ux, uy = (s0[0] - a[0]) / length, (s0[1] - a[1]) / length
        first = (a[0] + 5 * ux, a[1] + 5 * uy)
        last = (s0[0] - 5 * ux, s0[1] - 5 * uy)
        points = [(first[0] + (last[0] - first[0]) * k / (n - 1), first[1] + (last[1] - first[1]) * k / (n - 1))
                  for k in range(n)]
        taken: set = set()
        out: list = []
        prev = None
        for k, (px, py) in enumerate(points):
            spot = None
            for r in range(0, 7):
                ring = sorted(((int(px) + dx, int(py) + dy) for dx in range(-r, r + 1) for dy in range(-r, r + 1)
                               if max(abs(dx), abs(dy)) == r), key=lambda q: math.dist(q, (px, py)))
                for q in ring:
                    tiles = {(q[0] + i, q[1] + j) for i in (0, 1) for j in (0, 1)}
                    c = (q[0] + 1.0, q[1] + 1.0)
                    if tiles & taken or not all(free(t) for t in tiles):
                        continue
                    if prev is not None and math.dist(c, prev) > BIG_WIRE:
                        continue
                    if k == 0 and math.dist(c, (a[0] + 0.5, a[1] + 0.5)) > REACH:
                        continue
                    if k == n - 1 and math.dist(c, (s0[0] + 0.5, s0[1] + 0.5)) > REACH:
                        continue
                    spot = q
                    break
                if spot is not None:
                    break
            if spot is None:
                return None
            taken |= {(spot[0] + i, spot[1] + j) for i in (0, 1) for j in (0, 1)}
            prev = (spot[0] + 1.0, spot[1] + 1.0)
            out.append((BIG_POLE, spot))
        return out

    def _place_big_line(self, site: Site, line: list) -> None:
        """Make and place a big pole line (_big_line), recorded as the site's poles."""
        need = sum(1 for kind, _ in line if kind == BIG_POLE)
        if self.w.inventory.get(BIG_POLE, 0) < need:
            self.unlock_items([BIG_POLE])
            self.make({BIG_POLE: need})
        self.w.recorder = site.entities
        try:
            for kind, q in line:
                tiles = {(q[0] + i, q[1] + j) for i in (0, 1) for j in (0, 1)}
                self.w.place(kind, q[0] + 1.0, q[1] + 1.0, "north", tiles)
                site.poles.append(q)
        finally:
            self.w.recorder = None

    def _line_pole(self, length: float) -> str:
        """Medium poles for a long line once their recipe is in and steel is at hand or made by our automation
        (9 tiles a wire instead of 7.5: a sixth fewer poles to craft and place on a 400-tile line)."""
        if length < LONG_LINE or MEDIUM_POLE not in (self.kn.unlocked or set()):
            return POLE
        steel = self.w.inventory.get("steel-plate", 0) + self.stored("steel-plate")
        n = math.ceil(length / MEDIUM_REACH) + 2
        if self.w.inventory.get(MEDIUM_POLE, 0) >= n or steel >= 2 * n or "steel-plate" in self.ready_items():
            return MEDIUM_POLE
        return POLE

    def check_power(self, site: Site) -> bool:
        self._power_by_grid = False           # True: said powered only because its poles are on the grid
        if site.kind == "steam_power":
            return self.station_on_grid(site)
        names = sorted({e[0] for e in site.entities if e[0] in ELECTRIC})
        if not names:
            return True
        statuses = self.look(site, names)
        mine = [st for st in statuses.values()]
        # powered when (nearly) all of them are: a few machines cut off is a broken line, not a powered site
        if bool(mine) and sum(st == "no_power" for st in mine) <= len(mine) // 10:
            return True
        self._power_by_grid = bool(mine)
        # Idle machines on poles that are on the grid's network are wired in: the grid is short (too little
        # power, boilers out of coal), which more poles cannot fix (run 20261119-011655: "connect site 59" five
        # times, 0 poles placed each time, at 2.7 MW for 12 MW of machines)
        return self.on_grid(site.poles) is True

    def station_on_grid(self, site: Site) -> bool:
        """A steam station feeds the base only when every engine is plugged in (a pole reaches it) and its poles
        are on the grid's network: the game's network id of its poles is the one of the grid's first pole. A
        station beyond wire reach of the grid is a network of its own: its engines are plugged in but power
        nothing. Without network ids (the simulator, an older scenario) the plugs decide."""
        gens = sorted({e[0] for e in site.entities if e[0] in GENERATORS})
        if gens and any(st == UNPLUGGED for st in self.look(site, gens).values()):
            return False
        return self.on_grid(site.poles) is not False     # no ids to compare (None): the plugs said yes

    def on_grid(self, poles) -> bool | None:
        """Are these poles on the grid's network (the game's network id of the grid's first pole)? None when the
        game gives no ids (the simulator, an older scenario)."""
        grid = self.base.power_pole
        if grid is None:
            return None
        if tuple(grid) in {tuple(q) for q in poles}:
            return True                                  # it is the grid
        mine = {n for n in self.pole_networks(poles).values() if n is not None}
        main = {n for n in self.pole_networks([grid]).values() if n is not None}
        if not mine or not main:
            return None
        return mine == main

    def short_note(self) -> str:
        """A word on the grid when it cannot carry what is built: a site wired in still runs slowly."""
        p = self.power_status()
        if p["headroom_kw"] >= 0:
            return ""
        return (f"; wired in, but the grid is short of power ({p['capacity_kw']} kW for {p['demand_kw']} kW of "
                "machines): it runs slowly until more power is built")

    def pole_networks(self, tiles) -> dict[tuple[int, int], int | None]:
        """The game's electric network id of the poles on these tiles (one observe around them)."""
        tiles = [tuple(t) for t in tiles]
        if not tiles:
            return {}
        cx = sum(t[0] for t in tiles) / len(tiles) + 0.5
        cy = sum(t[1] for t in tiles) / len(tiles) + 0.5
        radius = min(32, max(4, int(max(math.dist((t[0] + 0.5, t[1] + 0.5), (cx, cy)) for t in tiles)) + 3))
        if math.dist((cx, cy), self.w.pos) > 60:
            self.w.near(cx, cy, dist=20)
        d = self.w.observe(radius, x=cx, y=cy, include=["entities"], names=list(POLES))
        want = set(tiles)
        return {(math.floor(e["x"]), math.floor(e["y"])): e.get("network") for e in d.get("entities") or []
                if (math.floor(e["x"]), math.floor(e["y"])) in want}

    def _plug_generators(self, site: Site) -> int:
        """A pole beside every engine of the station that no pole reaches, within wire reach of one of its poles
        (stations built before each engine got its own pole, or a pole that was picked up). Returns how many."""
        gens = {(e[1], e[2]): e for e in site.entities if e[0] in GENERATORS}
        if not gens or not site.poles:
            return 0
        status = self.look(site, sorted({e[0] for e in gens.values()}))
        unplugged = [gens[p] for p, st in status.items() if st == UNPLUGGED and p in gens]
        free = self._pole_freer()
        done = 0
        for name, x, y, d in unplugged:
            fp = self.w.footprint(name, x, y, d)
            ring = {(i + dx, j + dy) for i, j in fp for dx in (-1, 0, 1) for dy in (-1, 0, 1)} - fp
            poles = [tuple(q) for q in site.poles]
            spots = sorted((min(math.dist(t, q) for q in poles), t) for t in ring if free(t))
            spot = next((t for dist, t in spots if dist <= 7.0), None)
            if spot is None:
                self.foreman.note(f"site {site.id}: no free tile beside the engine at ({x}, {y}) for a pole")
                continue
            if self.w.inventory.get(POLE, 0) < 1:
                self.unlock_items([POLE])
                self.make({POLE: 1})
            self.w.recorder = site.entities
            try:
                self.w.place(POLE, spot[0] + 0.5, spot[1] + 0.5, "north", {spot})
            finally:
                self.w.recorder = None
            site.poles.append(spot)
            done += 1
        if done:
            time.sleep(1.0 / self.w.speed)                # let the game update the engines' status
        return done

    def look(self, site: Site, names: list[str]) -> dict[tuple[float, float], str]:
        """Status of the site's own buildings of these kinds, by position (one observe, remote when in range)."""
        cx, cy = site.centre
        own = {(e[1], e[2]) for e in site.entities if e[0] in names}
        radius = min(32, max(6, int(max((math.dist((x, y), (cx, cy)) for x, y in own), default=0)) + 3))
        if math.dist((cx, cy), self.w.pos) > 60:
            self.w.near(cx, cy, dist=20)
        d = self.w.observe(radius, x=cx, y=cy, include=["entities"], names=names[:20])
        return {(e["x"], e["y"]): e.get("status", "") for e in d.get("entities") or [] if (e["x"], e["y"]) in own}

    # =============================================================== removal
    SOLE_MAKER = "it is the only site making"

    def removal_blocker(self, site: Site) -> str | None:
        b = self.base
        age = self._minute() - site.built_minute
        healthy = (not _depleted(site) and not site.kind.startswith("unfinished") and site.powered is not False
                   and site.health.get("working", 0) > 0)
        if healthy and age < KEEP_MINUTES:
            return (f"it was built {age:.1f} minutes ago and is working; removing it now usually means rebuilding it "
                    "soon. Only remove it if a failure message named it as in the way")
        item = site.params.get("item")
        if (healthy and site.kind in ("factory", "workshop") and item
                and site.id not in (getattr(self, "orphans", None) or {})
                and not any(s is not site and s.kind in ("factory", "workshop") and s.params.get("item") == item
                            for s in b.sites)):
            # make before break: run 20261124-120854 removed blue workshop 64 (26 machines working, its first
            # packs just out) on "blue science is still zero", and nothing made blue for the next hour
            return (f"{self.SOLE_MAKER} {item} and {site.health.get('working', 0)} of its machines are "
                    f"working; removing it leaves nothing making {item}. Replace it instead (the new one is built "
                    "before this one comes down)")
        if site.kind == "storage":
            return ("it holds what the crew cannot carry; storage chests move by themselves when a build needs "
                    "their tiles")
        if site.kind == "steam_power" and len(b.of("steam_power")) <= 1 and b.of("labs") + b.of("factory"):
            return "it is the only power station"
        if site.kind == "labs" and (self.w.research or {}).get("current") and len(b.of("labs")) <= 1:
            return "the labs are researching and there are no others"
        for plate in {p for *_, p in site.plate_furnaces if p}:
            others = [s for s in b.sites if s is not site and not _depleted(s)
                      and any(q == plate for *_, q in s.plate_furnaces)]
            if not others and not _depleted(site):
                return f"it is the last working source of {plate} for crafting"
        if site.kind == "burner_mine" and site.params.get("resource") == "coal" and not _depleted(site):
            if not [s for s in b.of("burner_mine") + b.of("electric_mine")
                    if s is not site and s.params.get("resource") == "coal"]:
                return "it is the last coal mine"
        return None

    REMOVE_STASHES = 40            # trips to storage one removal may make (each must free room to count)

    def _empty_container(self, x: float, y: float) -> None:
        """Leave what a chest of a site being removed holds in storage first, so picking it up does not fill
        the inventory at once. Best effort: whatever stays comes along with the chest."""
        try:
            held = self._chest_held((x, y))
        except BuildError:
            return
        for item, n in held.items():
            left = n
            for _ in range(12):
                if left <= 0:
                    break
                if INVENTORY_SLOTS - self.slots_used() < 4 and not self.ensure_space(8, force=True):
                    return
                try:
                    got = self.w.extract(x, y, item, left)
                    if got <= 0:
                        break
                    left -= got
                    self._stash(item, got)
                except BuildError:
                    return

    def remove(self, site_id, force: bool = False, keep_poles: bool = True) -> str:
        """Pick a site's buildings back up (contents come too). Poles stay by default: other sites may draw power
        through them, and the power router reuses them for free."""
        with self.w.step("remove"):
            return self._remove(site_id, force, keep_poles)

    def _remove(self, site_id, force: bool, keep_poles: bool) -> str:
        site = self.base.get(site_id)
        if site is None:
            raise BuildError(f"there is no site {site_id}")
        why = self.removal_blocker(site)
        if why and why.startswith(self.SOLE_MAKER) and getattr(self, "_replaced", None) == site.id:
            why = None                                # its replacement already stands (replace: make before break)
        if why and not force:
            raise BuildError(f"not removing site {site.id} ({site.kind}): {why}. Replace it, or pass force: true")
        self._note_bad_place(site)
        feeders = self._feeders_of(site)
        detached = self._detach(site)
        cut = bool(detached) and ("get linked again" in detached or "new source fed in" in detached)
        self._release_margin(site)
        self.release_strip(site.id)
        if site.kind in ("workshop", "unfinished workshop") and hasattr(MODULES.get("workshop"), "forget"):
            MODULES["workshop"].forget(site)                  # its slots, pipes and pumpjacks are free again
        getattr(self, "retrofit_blocked", {}).pop(site.id, None)
        todo = [e for e in site.entities if not (keep_poles and e[0] in POLES)]
        here, done, stashes = self.w.pos, 0, 0
        while todo:
            e = min(todo, key=lambda q: math.dist((q[1], q[2]), here))
            todo.remove(e)
            self.w.doing(f"removing site {site.id}: {e[0]} ({done + 1}/{done + 1 + len(todo)})")
            if e[0] in self.STORAGE_CHESTS:
                self._empty_container(e[1], e[2])     # a full plate chest alone is 32 slots
            try:
                if self.w.pickup(*e):
                    done += 1
            except BuildError:
                # inventory full (the building comes with what it holds): leave things in storage and go on
                # (run 20261119-011655: 22 removals stopped half-way here; run 20261120-092648: site 37 stopped
                # after 325 of its buildings when four stashes were all it was allowed)
                freed = self.ensure_space(16, force=True) if stashes < self.REMOVE_STASHES else 0
                stashes += 1
                if freed:
                    todo.append(e)
                    continue
                site.entities = [x for x in site.entities if x in todo or x[0] in POLES] + [e]
                raise BuildError(f"inventory full after taking {done} buildings from site {site.id}, and nothing "
                                 "more can be left in storage; the rest is still standing")
            here = (e[1], e[2])
        kept = [e for e in site.entities if e[0] in POLES] if keep_poles else []
        self.removed.append({"site": site.id, "key": _site_key(site.kind, site.params), "minute": self._minute(),
                             "built_minute": site.built_minute, "forced": force})
        self.removed = self.removed[-10:]
        self.removals += 1
        self.base.remove(site)
        self.foreman.forget(site)
        self._prune_link_fails()
        if kept:                              # its poles stay standing: a powered stub other routes can reuse
            stub = Site("poles", {"from_site": site.id}, site.centre, poles=[tuple(map(math.floor, e[1:3])) for e in kept],
                        entities=kept, powered=site.powered, built_minute=site.built_minute)
            self.base.add(stub)
        for out in self.base.of("outpost"):           # its column gone: the outpost gets a new one and a trunk
            if out.params.get("trunk_to") == site.id:
                out.params["trunk_to"] = None
        if cut:                                       # its belts fed other sites: connect them again now (to a
            try:                                      # replacement, say), not whenever a link goal comes by
                detached += "; relinked: " + self.link()[:200]
            except BuildError as exc:
                detached += f"; relinking failed for now (the link step retries): {str(exc)[:160]}"
        orphaned = self._note_orphans(feeders, site)
        return (f"removed site {site.id} ({site.kind}): {done} buildings back in the inventory"
                + (f"; {detached}" if detached else "") + (f"; {orphaned}" if orphaned else ""))

    def _feeders_of(self, site: Site) -> dict[int, str]:
        """{producer site id: item} of every site whose output a belt carries into this one."""
        out: dict[int, str] = {}
        for ln in self.links:
            if ln.get("to_site") != site.id or ln.get("kind") == "science":
                continue
            src = self._source_site(tuple(ln["source"]), ln.get("item"))
            if src is not None and src is not site:
                out[src.id] = ln.get("item")
        return out

    def _note_orphans(self, feeders: dict, gone: Site) -> str:
        """Producers whose only consumer was just removed: nothing draws from them any more. Kept as orphans
        (assess.orphan_alerts) until linked to another consumer or removed. Run 20261122-133537: cable factory 43
        (143/min) lost circuit factory 46 and stood 12 of 13 machines idle, still tapping a copper belt."""
        if not isinstance(getattr(self, "orphans", None), dict):
            self.orphans = {}
        out = []
        for sid, item in feeders.items():
            s = self.base.get(sid)
            if s is None or s.kind not in ("factory", "workshop") or self._draws_from(s):
                continue
            self.orphans[sid] = {"item": item, "lost": gone.id, "minute": self._minute()}
            out.append(f"site {sid} ({item}) lost its only consumer")
        return "; ".join(out)

    def _draws_from(self, site: Site) -> bool:
        """Does any link take from this site's outputs (its chests or its line's tail)?"""
        srcs = {(round(x, 2), round(y, 2)) for x, y, _ in site.chests}
        if site.params.get("line"):
            srcs.add(tuple(round(v, 2) for v in site.params["line"]["source"]))
        return any((round(ln["source"][0], 2), round(ln["source"][1], 2)) in srcs for ln in self.links)

    def orphan_sites(self) -> list[tuple[Site, dict]]:
        """Orphans still standing that nothing draws from (a link laid since clears one)."""
        orphans = getattr(self, "orphans", None) or {}
        out = []
        for sid, o in list(orphans.items()):
            s = self.base.get(sid)
            if s is None or self._draws_from(s):
                orphans.pop(sid, None)
                continue
            out.append((s, o))
        return out

    # A margin of one tile around each site (and along each trunk) stays free of buildings, so belts can always
    # reach a site's edge and pass between neighbours. Run 20261119-154451: three research stations built in the
    # middle of the base lost most of their science links to "no belt route", walled in by other sites.
    MARGIN_KINDS = ("factory", "workshop", "research_station", "labs", "smelter_column", "silo", "outpost")

    def _reserve_margin(self, site: Site, tiles: set | None = None) -> None:
        if tiles is None:
            if site.kind not in self.MARGIN_KINDS:
                return
            foot = set()
            for name, x, y, d in site.entities:
                foot |= self.w.footprint(name, x, y, d)
            tiles = {(tx + dx, ty + dy) for tx, ty in foot for dx in (-1, 0, 1) for dy in (-1, 0, 1)} - foot
            tiles |= set(map(tuple, getattr(site, "port_lanes", None) or []))   # each input port's way in
        lanes = getattr(self.w, "lanes", None)
        if lanes is None:
            return
        ring = {t for t in tiles if self._belt_free(t)}
        lanes |= ring
        self.site_lanes = getattr(self, "site_lanes", {})
        self.site_lanes[site.id] = sorted(ring)

    def _release_margin(self, site: Site) -> None:
        mine = set(map(tuple, getattr(self, "site_lanes", {}).pop(site.id, [])))
        others = {tuple(t) for ts in getattr(self, "site_lanes", {}).values() for t in ts}
        lanes = getattr(self.w, "lanes", None)
        if lanes is not None:
            lanes -= mine - others

    def _inputs_centre(self, module: str, params: dict) -> tuple[float, float] | None:
        """Where a station's science or a steel column's plates come from (their sources' centre)."""
        if module == "research_station":
            pts = [(x, y) for s in self.base.sites for x, y, what in s.chests if what.endswith("science-pack")]
        elif params.get("resource") == "iron-plate":
            pts = [(x, y) for x, y, s in self.outputs("iron-plate")]
        else:
            return None
        if not pts:
            return None
        return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))

    def _detach(self, site: Site) -> str:
        """Before a site comes down: take up every belt link into it or out of it (and the links that tap or
        feed into those, which would carry nothing once they are gone), so the ports they fed count as unfed
        and the link step connects them again (to a replacement, say). Without this a removed factory left its
        consumers "fed" by a belt from nowhere, and the courier stopped carrying for them too."""
        chests = {(round(x, 2), round(y, 2)) for x, y, _ in site.chests}
        if site.params.get("line"):
            chests.add(tuple(round(v, 2) for v in site.params["line"]["source"]))
        into = lambda ln: (ln.get("to_site") == site.id or ln.get("outpost") == site.id
                           or ln.get("tap_site") == site.id)
        # belts OUT of it stay where they are: the factories they feed keep their belts, and the link step feeds a
        # new source into the side of those belts (boost_links: the replacement first, or any spare source). Run
        # 20261121-140755: every mined-out mine took its belts with it, each consumer was relinked, and each relink
        # took up the belts other factories tapped too: 45% of the game time after minute 120 went into that.
        kept = self._keep_outbound(site, [ln for ln in self.links if not into(ln) and ln.get("kind") != "science"
                                          and (round(ln["source"][0], 2), round(ln["source"][1], 2)) in chests])
        direct = [ln for ln in self.links if into(ln)
                  or ((round(ln["source"][0], 2), round(ln["source"][1], 2)) in chests
                      and not any(ln is k for k in kept))]
        gone: list[dict] = []
        todo = list(direct)
        while todo:                                   # and whatever hangs off those
            ln = todo.pop()
            if any(ln is g for g in gone):
                continue
            gone.append(ln)
            todo += [q for q in self.links if (q.get("tap_of") is ln or q.get("boost_into") is ln)
                     and not any(q is g for g in gone)]
        note = (f"{len(kept)} belt link(s) out of it stay standing and get a new source fed in" if kept else "")
        if not gone:
            return note
        relink = sorted({ln["to_site"] for ln in gone if ln.get("to_site") != site.id})
        for ln in reversed(gone):                     # the hangers-on first, then the links themselves
            if ln.get("to_site") == site.id and ln.get("kind") != "trunk":
                self._unregister(ln)                  # its belts are the site's own and come up with it
                gone_ents = set(ln.get("entities", []))
                site.entities.extend(e for e in gone_ents if e not in site.entities)
            else:
                self._unlink(ln)
        return (f"took up {len(gone)} belt link(s)" + (f"; sites {relink} get linked again" if relink else "")
                + (f"; {note}" if note else ""))

    # A kept belt whose source came down gets a new source fed into its side within this long; if none can be
    # (no spare source, no side to join), it is taken up as before and its ports linked afresh.
    SPLICE_WAIT_MIN = 10.0

    def gone_roots(self) -> dict:
        """{source key: game minute it came down} of kept belts whose source was removed (see _keep_outbound)."""
        g = self.__dict__.get("_gone_roots")
        if not isinstance(g, dict):
            g = self.__dict__["_gone_roots"] = {}
        return g

    def _makes(self, site: Site) -> set:
        """What a site's outputs carry (its chests and its line)."""
        made = {what for *_, what in site.chests if what not in self.TAGS}
        if site.params.get("line"):
            made.add(site.params["line"]["item"])
        return made

    def _keep_outbound(self, site: Site, out: list[dict]) -> list[dict]:
        """The links out of a site about to come down that stay standing: their own inserter at the site comes up
        (nothing else may ever be lifted onto the belt from that tile), and their source is remembered as gone,
        so it counts as giving nothing and the link step feeds a new source into the belt's side."""
        kept = []
        for ln in out:
            if ln.get("kind") == "trunk":
                continue
            # only where a new source can be fed in: a replacement already standing, or another spare source of
            # the item. Run 20261124-015025: 5 of 8 kept belt sets had none, their factories got nothing for the
            # 10 minutes until they were taken up and relinked anyway
            if not any(q[2] is not site for q in self._spare(ln["item"])):
                continue
            if ln.get("ins") and not ln.get("ins_removed"):
                (ix, iy), facing, name = ln["ins"]
                e = next((q for q in ln.get("entities", []) if q[0] == name
                          and (math.floor(q[1]), math.floor(q[2])) == (ix, iy)), None)
                if e is not None:
                    try:
                        self.w.pickup(*e)
                    except BuildError:
                        pass
                    ln["entities"] = [q for q in ln["entities"] if q is not e]
                ln["ins_removed"] = True
            self.gone_roots().setdefault(tuple(ln["source"]), self._minute())
            kept.append(ln)
        return kept

    def spliced(self, root) -> bool:
        """Has a new source been fed into the belts of this (gone) source?"""
        root = tuple(root)
        return any(tuple(ln.get("boost_of") or ()) == root for ln in self.links)

    def _gone_chain(self, root) -> list[dict]:
        root = tuple(root)
        return [ln for ln in self.links if ln.get("boost_into") is None and ln.get("kind") not in ("trunk", "science")
                and self._coal_root(ln) == root]

    def expire_gone(self) -> list[str]:
        """Kept belts whose source is gone: alive again if a source stands there now; spliced ones stay; ones still
        unspliced after SPLICE_WAIT_MIN are taken up (with what hangs off them) so their ports get linked afresh."""
        notes = []
        now = self._minute()
        for root, t in list(self.gone_roots().items()):
            chain = self._gone_chain(root)
            item = chain[0]["item"] if chain else None
            if not chain or (item is not None and self._source_site(root, item) is not None):
                self.gone_roots().pop(root, None)
                continue
            if self.spliced(root) or now - t < self.SPLICE_WAIT_MIN:
                continue
            gone, todo = [], list(chain)
            while todo:
                q = todo.pop()
                if any(q is g for g in gone):
                    continue
                gone.append(q)
                todo += [h for h in self.links if h.get("tap_of") is q or h.get("boost_into") is q]
            for q in reversed(gone):
                self._unlink(q)
                self._link_fails.pop(tuple(q["port"]["end"]), None)
            self.gone_roots().pop(root, None)
            notes.append(f"the {item} belts of a removed source at {list(root)} got no new source in "
                         f"{self.SPLICE_WAIT_MIN:.0f} min: taken up ({len(gone)} link(s)), linked afresh")
        return notes

    def hints(self) -> list[str]:
        """What the general manager should consider: depleted, unpowered, starved or idle sites."""
        out = [f"churn: {c}" for c in self.churn[-2:]]
        blk, drills = self.blocker(), self.burner_drills()
        if blk and blk.get("module") == "burner_mine" and drills:
            out.insert(0, "no coal mine yet: every refuel is chopped or hand-mined. Build a coal mine "
                          f"({blk}) before anything else; automation beats doing it by hand")
        elif blk and drills >= 8:
            what = blk.get("module") or blk.get("tech")
            out.insert(0, f"{drills} burner drills already run: more burner mines unblock nothing. The base is "
                          f"blocked on {what} ({blk}); put the next goals there. A failed build of it can be "
                          "asked again: its chiplet remembers what the ground refused and tries other places")
        for s in self.base.sites:
            h, n = s.health, sum(s.health.values()) or 1
            if _depleted(s):
                out.append(f"site {s.id} ({s.kind} {s.params.get('resource', '')}) is depleted: remove it to get "
                           "its buildings back")
            if s.powered is False:
                out.append(f"site {s.id} ({s.kind}) has no power: connect it")
            idle = sum(v for k, v in h.items() if k in IDLE)
            if s.kind == "factory" and idle >= 0.6 * n and n > 3:
                out.append(f"site {s.id} (factory {s.params.get('item')}) is mostly idle ({idle}/{n} machines "
                           "waiting): its input or output is the bottleneck")
            if s.kind.startswith("unfinished"):
                out.append(f"site {s.id} is an unfinished {s.kind[11:]}: remove it or build it again")
        if self.base.of("labs") and not (self.w.research or {}).get("current"):
            out.append("the labs are idle: queue research (they will pick the next milestone tech on their own)")
        if "electric-mining-drill" in self.kn.unlocked and self.base.of("factory"):
            n = sum(int(s.params.get("count", 0)) for s in self.base.of("burner_mine"))
            if n >= 6:
                out.append(f"{n} burner drills hold ore tiles; once a factory's electric mines run, burner mines "
                           "on iron and copper can be replaced to free the patch (keep one plate source for crafting)")
        return out[:10]

    # ============================================================ unlocking
    def unlock_items(self, items: list[str]) -> None:
        """Make sure every item can be hand-crafted: complete trigger techs, research the rest."""
        for item in items:
            if item in FUEL or item in PLATES or self.kn.recipe_for(item, hand=True):
                continue
            rec = self.kn.recipe_for(item, hand=True, unlocked_only=False)
            if rec is None:
                raise BuildError(f"{item} cannot be hand-crafted at all")
            self.unlock_recipes({rec})

    def unlock_recipes(self, recipes: set[str]) -> None:
        for rec in recipes:
            if rec in self.kn.unlocked:
                continue
            tech = self.kn.unlocking_tech(rec)
            if tech is None:
                raise BuildError(f"nothing unlocks {rec}")
            for t in self.kn.tech_path(tech):
                self.complete_tech(t)

    def complete_tech(self, tech: str) -> None:
        if tech in self.kn.researched:
            return
        trig = self.kn.techs[tech].get("trigger")
        if trig:
            self.log(f"[crew] trigger tech {tech}: {trig}")
            if trig["type"] == "craft-item":
                item, n = trig["item"], int(trig.get("count") or 1)
                made = ((self.w.stats.get("items") or {}).get(item) or {}).get("made", 0)
                if made < n:
                    if item in PLATES:
                        self.plates(item, n - made + self.w.inventory.get(item, 0))
                    else:
                        self.unlock_items([item])
                        self.make({item: max(1, n)}, extra=True)
            elif trig["type"] == "mine-entity" and "crude-oil" in (trig.get("entities") or [trig.get("entity")]):
                # the scenario sees the trigger once crude oil is pumped: a powered pumpjack on a well (its
                # kit unlocks oil-gathering first, which is plain research)
                for t in self.kn.techs[tech].get("prerequisites", []):
                    for p in self.kn.tech_path(t):
                        self.complete_tech(p)
                if not self.base.of("pumpjack"):
                    self.build({"do": "build", "module": "pumpjack", "count": 1})
            else:
                raise BuildError(f"{tech} needs its trigger ({trig['type']} {trig}), which this bot cannot do yet")
            for _ in range(60 if trig["type"] == "mine-entity" else 20):   # triggers are checked every second
                self.w.read_stats(1)
                if tech in self.kn.researched:
                    return
                time.sleep(0.5)
            raise BuildError(f"{tech} did not complete after its trigger"
                             + (": is the pumpjack powered and pumping?" if trig["type"] == "mine-entity" else ""))
        self.research(tech, wait=True)

    def research(self, tech: str, wait: bool = False) -> str:
        for t in self.kn.tech_path(tech):
            if self.kn.is_trigger(t):
                self.complete_tech(t)
        # after the triggers: completing one may have researched part of the path (oil-processing researches
        # oil-gathering and its prerequisites before its pumpjack can be built)
        path = [t for t in self.kn.tech_path(tech) if not self.kn.is_trigger(t)]
        if not path:
            # the planner asked for a finished tech (it did 6 times in a row while the labs sat idle): give the
            # labs the next milestone tech instead of nothing
            if self.base.of("labs") and not (self.w.research or {}).get("current"):
                pick = self.auto_research()
                if pick:
                    return f"{tech} already researched; the labs were idle, so queued {pick}"
            return f"{tech} already researched"
        if not self.base.of("labs"):
            # labs first, like power, plates and feeders first: run 20261126-test (seed 515151) asked for red
            # science before any labs; the factory goal crafted for 5.4 minutes, then failed on this, and three
            # more goals after it
            if getattr(self, "_in_labs_first", False):
                raise BuildError(f"research {tech} needs labs (build labs first)")
            self._in_labs_first = True
            try:
                self.log(f"[crew] labs first: research {tech} needs labs")
                if self.current is not None:
                    self.current["extra_min"] = (float(self.current.get("extra_min") or 0.0)
                                                 + MODULE_BUDGET_MIN.get("labs", GOAL_BUDGET_MIN["build"]))
                if not self.base.of("steam_power"):    # labs draw power: the first station before them (the
                    self.log("[crew] power first: labs need a steam station")   # zones test, seed 626262)
                    self.build({"do": "build", "module": "steam_power", "engines": 1})
                self.build({"do": "build", "module": "labs", "count": 2})
            except BuildError as exc:
                raise BuildError(f"research {tech} needs labs, and building them first failed: {str(exc)[:160]}")                     from None
            finally:
                self._in_labs_first = False
        # every pack the path costs: made by a factory already, automated now, or (only if that is cheap or
        # impossible) hand-crafted within a budget; packs by hand mean the labs need the crew, so it waits
        notes = []
        by_hand = self.ensure_packs(path, notes)
        if by_hand:
            wait = True
        rs = self.w.research or {}
        busy = set(rs.get("queue") or []) | ({rs["current"]} if rs.get("current") else set())
        if not wait:
            todo = [t for t in path if t not in busy]
            if not todo:                              # already on its way: never re-queue (it cancelled progress)
                return f"{tech} is already being researched"
            # append: whatever the labs are on keeps going; replacing made two goals cancel each other forever
            r = self.w.act({"type": "research", "technologies": todo[:16], "mode": "append"})
            if not r["ok"]:
                raise BuildError(f"research {todo}: {r['code']} {r.get('message', '')}")
            self.w.research = r.get("data") or rs
            self.foreman.research_goal = tech
            return f"queued {', '.join(todo)}"
        # the crew needs it now (to unlock a kit): put it first
        r = self.w.act({"type": "research", "technologies": path[:16], "mode": "replace"})
        if not r["ok"]:
            raise BuildError(f"research {path}: {r['code']} {r.get('message', '')}")
        self.foreman.research_goal = tech
        if not wait:
            return f"researching {', '.join(path)}"
        with self.w.step("research"):
            return self._wait_research(tech, path, defer=not by_hand)   # packs by hand need the crew there

    # ===================================================== research station
    def science_sources(self) -> list[tuple[float, float, str]]:
        """Every chest a science pack comes out of (factories, workshop cells), never a lab's own feeder."""
        return [(x, y, what) for s in self.base.sites if s.kind != "labs"
                for x, y, what in s.chests if str(what).endswith("science-pack")]

    def belted_packs(self) -> set[str]:
        """Packs every source of which reaches a research station by belt: the foreman never carries those."""
        src = self.science_sources()
        packs = {p for *_, p in src}
        return {p for p in packs if all((x, y) in self.science_links for x, y, q in src if q == p)}

    def unlinked_sources(self) -> list[tuple[float, float, str]]:
        return [(x, y, p) for x, y, p in self.science_sources() if (x, y) not in self.science_links
                and self._may_retry((x, y))]

    # A link that failed is not tried again the same way: only once the base changed (a site built or removed)
    # or after a back-off that grows with each failure. Each failure keeps its reason, which the supervisor and
    # the strategist see ("stuck_links"), so nobody asks for the same link over and over.
    def _base_sig(self) -> tuple:
        # a count, not len(self.removed): that list stops at 10, and a removal that leaves a pole stub keeps
        # the site count too, so later removals never let a failed link be tried again
        return len(self.base.sites), self.removals

    def _may_retry(self, key: tuple) -> bool:
        f = self._link_fails.get(tuple(key))
        if f is None:
            return True
        # a changed base earns an early retry only for the first failures: any site built or removed anywhere
        # changes it, so a link that cannot work was otherwise retried with every build (run 20261121-140755:
        # 2 recorded failures, 56 attempts). After that only the back-off (or a relink goal) lets it try again.
        if f["base"] != self._base_sig() and f["n"] < self.EARLY_RETRIES:
            return True
        return self._minute() - f["minute"] >= AUTO_RETRY_MIN * min(4, f["n"])

    EARLY_RETRIES = 2

    def _note_link_fail(self, key: tuple, item: str, site: Site | None, reason: str) -> None:
        key = tuple(key)
        f = self._link_fails.get(key) or {"n": 0}
        self._link_fails[key] = {"n": f["n"] + 1, "item": item, "site": site.id if site else None,
                                 "reason": reason[:300], "minute": self._minute(), "base": self._base_sig()}
        self._link_failed[key] = self._minute()

    def _prune_link_fails(self) -> None:
        """Forget failed links nothing wants any more: their consumer site was removed and no port or science
        chest is left at that spot. Without this a removed site's links stayed "stuck" for good, and every link
        goal failed at once on them (run 20261119-202811: "nothing new to try: ... into site 15", 70 minutes
        after site 15 was removed)."""
        if "underground-belt" in (getattr(self.kn, "unlocked", None) or set()):
            # failures for want of underground belts are void once they are researched: the link step lays every
            # such link again (with undergrounds) instead of "nothing new to try" until some site changes. Run
            # 20261125-184025: links failed before logistics stayed failed after it, the planners saw only failing
            # relinks, and rebuilt red science, green science and the research station instead
            for key, f in list(self._link_fails.items()):
                if "research logistics" in str(f.get("reason") or ""):
                    self._link_fails.pop(key, None)
        ends = {tuple(pt["end"]) for _, pt in self.all_ports()}
        sources = {(x, y) for x, y, _ in self.science_sources()}
        for key, f in list(self._link_fails.items()):
            sid = f.get("site")
            if key in ends or key in sources:
                continue
            # a science link's key is its source chest: once that chest is gone (its factory removed) the failure
            # is stale even though the station stands. Run 20261124-120854: removed workshop 64's blue chest kept
            # a stuck link, and the far-link alert sent a trunk for packs nothing made any more
            science = str(f.get("item", "")).endswith("science-pack") and len(key) == 2                 and all(isinstance(v, (int, float)) for v in key)
            if not science and (sid is None or self.base.get(sid) is not None):
                continue
            self._link_fails.pop(key, None)
            self._link_failed.pop(key, None)

    def stuck_links(self) -> list[dict]:
        """Links that failed and are still missing: what, where, how often, why, and what would fix it."""
        self._prune_link_fails()
        out = []
        for key, f in self._link_fails.items():
            fed = any(tuple(pt["end"]) == key and (pt.get("fed") or pt.get("pack")) for _, pt in self.all_ports())
            if fed or key in self.science_links:
                continue
            out.append({"item": f["item"], "into_site": f["site"], "at": list(key), "failed": f["n"],
                        "minute": f["minute"], "why": f["reason"]})
        return sorted(out, key=lambda q: -q["failed"])[:8]

    # ------------------------------------------------------------- links
    TAGS = ("lab-feed", "coal-in", "storage")

    def outputs(self, item: str) -> list[tuple[float, float, Site]]:
        """Chests an item comes out of (factories, electric mines, workshop cells), and the tails of its lines
        (a plate row's or a column's belt): where links start."""
        out = [(x, y, s) for s in self.base.sites for x, y, what in s.chests if what == item and what not in self.TAGS]
        out += [(ln["source"][0], ln["source"][1], s) for s, ln in self.lines(item)]
        return out

    def lines(self, item: str | None = None) -> list[tuple[Site, dict]]:
        """Every line (docs/logistics-plan.md, phase 1): a row's or a column's plates on one belt."""
        return [(s, s.params["line"]) for s in self.base.sites if s.params.get("line")
                and (item is None or s.params["line"]["item"] == item)]

    def line_at(self, key) -> dict | None:
        """The line whose tail is this source key, if any."""
        key = tuple(key)
        return next((ln for _, ln in self.lines() if tuple(ln["source"]) == key), None)

    LINE_FURNACE = {"stone-furnace": 18.75, "steel-furnace": 37.5, "electric-furnace": 37.5}

    def line_rate(self, ln: dict, site: Site | None = None) -> float:
        """What a line carries a minute: what its furnaces make (a row's drill gives 30 ore a minute, so a steel
        furnace there makes 30, a stone one 18.75); 0 once its ore is gone. A furnace upgrade raises it."""
        if site is None:
            return float(ln.get("rate") or 0.0)
        if _depleted(site):
            return 0.0
        if site.kind == "smelter_column" and any(pt.get("item") and not pt.get("fuel") and not pt.get("fed")
                                                 for pt in site.params.get("ports", [])):
            # no ore comes in (its trunk never joined): it carries nothing. Run 20261122-133537: column 33's
            # 281/min counted as spare iron and was offered as the circuit factory's iron source
            return 0.0
        furnaces = [e[0] for e in site.entities if e[0] in self.LINE_FURNACE]
        if not furnaces:
            return float(ln.get("rate") or 0.0)
        cap = self.DRILL_PER_MIN if site.kind == "electric_mine" else 1e9
        return round(sum(min(cap, self.LINE_FURNACE[f]) for f in furnaces), 2)

    def item_ports(self) -> list[tuple[Site, dict]]:
        """Input ports that want one item (factory input cells, workshop cells, the silo's chests)."""
        return [(s, pt) for s in self.base.sites for pt in s.params.get("ports", [])
                if pt.get("item") and not pt.get("trunk")]          # a trunk port is fed by its outpost

    def unfed_ports(self) -> list[tuple[Site, dict]]:
        return [(s, pt) for s, pt in self.item_ports() if not pt.get("fed") and self._may_retry(tuple(pt["end"]))]

    def linkable(self) -> bool:
        """Is there a link the manager could lay now (a source for an unfed port, or science for a station), or a
        hand-fed row or boiler due for its coal belt?"""
        if self.retrofit_due():
            return True
        stations = [s for s in self.base.of("labs") if s.params.get("station")]
        if stations and any(pt.get("pack") is None for s in stations for pt in s.params["ports"]) \
                and self.unlinked_sources():
            return True
        if any(c["spare_sources"] and c["boost_may_retry"] for c in self.short_chains()):
            return True                               # a short belt and a spare source to feed into it
        if any(not self.spliced(r) for r in self.gone_roots()):
            return True                               # kept belts of a removed source: splice in, or link afresh
        return any(self._free_outputs(pt["item"], s, self._need(pt)) for s, pt in self.unfed_ports())

    @staticmethod
    def _manhattan(a, b) -> float:
        return abs(a[0] - b[0]) + abs(a[1] - b[1])

    @staticmethod
    def _need(pt: dict) -> float:
        """What a port takes a minute (its source must have that much left, see _fits)."""
        return float(pt.get("per_minute") or 0)

    def _free_outputs(self, item: str, consumer: Site, need: float = 0.0) -> list[tuple[float, float, Site]]:
        """Sources of an item that can take one more link (a chest has room for a few inserters) and still have
        some of the item to give: an unused source takes any port (a short one gets more fed in later, see
        boost_links), a used one only a port it can feed in full (`need` a minute on top of what it feeds)."""
        used: dict[tuple, int] = {}
        for ln in self.links:
            used[tuple(ln["source"])] = used.get(tuple(ln["source"]), 0) + 1
        # a mine's plate chest gets one drill's plates: one link (one input cell) each; other chests two
        cap = lambda s: 1 if s.kind in ("electric_mine", "smelter_column") and item in self.PLATE_ORE else 2
        out = [(x, y, s) for x, y, s in self.outputs(item) if s is not consumer and used.get((x, y), 0) < cap(s)
               and not (item == "coal" and s.kind == "burner_mine")]   # burner coal mines burn their own coal
        budget = self._budget(item)
        out = [(x, y, s) for x, y, s in out if self._fits(budget, (x, y), need, item)]
        if need > 0.5 and consumer is not None:
            # a source that covers the port in full, within a link's reach, before any short one: links go to the
            # nearest source, and an unused one takes a port of any size (run 20261125-214645: green science's 91
            # iron a minute went to a 56/min line 53 tiles off while a free 112/min line stood 58 tiles off;
            # the factory ran at 55% for 170 game minutes and triage built iron mines meanwhile)
            from . import belts
            c = consumer.centre or (0, 0)
            full = [q for q in out if self._left(budget, (q[0], q[1]), item) >= need - 0.5
                    and self._manhattan(q[:2], c) <= belts.LINK_REACH]
            if full:
                return full
        return out

    def _left(self, budget: dict, root: tuple, item: str) -> float:
        """What a source has left a minute after the ports it already feeds."""
        load, supply = budget.get(tuple(root)) or [0.0, self._source_rate(tuple(root), item)]
        return supply - load

    # A belt carries what its source gets. Run 20261119-154451: three boilers and four furnace rows tapped one coal
    # chest's belt (one drill, 30 a minute) and all ran dry, while each new coal mine stood unlinked (every port
    # counted as fed), so "build more coal drills" was asked again and again and changed nothing. Plates the same:
    # a factory port wanting 40 iron a minute got one drill's 19 and its machines waited on their input.
    COAL_PER_CHEST = 30.0
    DRILL_PER_MIN = 30.0
    FURNACE_PER_MIN = {"stone-furnace": 18.75, "steel-furnace": 37.5,    # iron, copper or bricks a minute
                       "electric-furnace": 37.5}
    STEEL_PER_MIN = {"stone-furnace": 3.75, "steel-furnace": 7.5, "electric-furnace": 7.5}

    def _source_site(self, root: tuple, item: str) -> Site | None:
        return next((s for x, y, s in self.outputs(item) if (x, y) == tuple(root)), None)

    def _source_rate(self, root: tuple, item: str) -> float:
        """What one source chest gets a minute: one drill (or its furnace), one furnace of a column, a share of
        a factory's or a workshop cell's output."""
        site = self._source_site(root, item)
        if site is None:
            if tuple(root) in self.gone_roots():
                return 0.0                            # it came down: what feeds its belts now are the boosts
            return self.COAL_PER_CHEST
        ln = site.params.get("line")
        if ln and tuple(ln["source"]) == tuple(root):
            return self.line_rate(ln, site)
        furnace = next((f for f in ("electric-furnace", "steel-furnace") if any(e[0] == f for e in site.entities)),
                       "stone-furnace")
        chests = max(1, sum(1 for *_, what in site.chests if what == item))
        if site.kind == "electric_mine":
            if site.params.get("into") == "chest":
                return self.DRILL_PER_MIN
            return min(self.DRILL_PER_MIN, self.FURNACE_PER_MIN[furnace])
        if site.kind == "smelter_column":
            if site.params.get("into") == "chest":
                return 45.0                           # an inserter off the trunk
            return self.STEEL_PER_MIN[furnace] if item == "steel-plate" else self.FURNACE_PER_MIN[furnace]
        if site.kind == "factory":
            return float(site.params.get("per_minute") or 5.0) / chests
        if site.kind == "workshop":
            return max(1.0, sum(c.get("item_out", {}).get(item, 0.0) for c in site.cells) / chests)
        if site.kind == "burner_mine":
            return 12.75
        return self.COAL_PER_CHEST

    def _coal_root(self, ln: dict | None) -> tuple | None:
        """The source a link's items really come from (through the belts it taps)."""
        for _ in range(50):
            if ln is None:
                return None
            if isinstance(ln.get("tap_of"), dict):
                ln = ln["tap_of"]
            elif ln.get("tap_site") is not None:      # it taps an electric mine's fuel belt: whatever feeds that
                item = ln["item"]
                ln = next((q for q in self.links if q.get("to_site") == ln["tap_site"] and q["item"] == item
                           and q["port"].get("fuel") and q.get("boost_into") is None), None)
            else:
                return tuple(ln["source"])
        return None

    _root = _coal_root

    def _budget(self, item: str | None = None) -> dict[tuple, list[float]]:
        """{source: [a minute the ports it feeds take, a minute it gets]} for every chain (of one item). A
        source fed into another chain (a boost) gives that chain what it is short of, and keeps the rest: the
        boosted belt backs up once full, and the source's line or chest has the rest for other links (run
        20261125-214645: a 487/min line boosted into a steel column short of 37.5/min was booked whole, and
        stood 43/52 machines blocked while it counted as fully used)."""
        out: dict[tuple, list[float]] = {}

        def row(root: tuple, it: str) -> list[float]:
            if root not in out:
                out[root] = [0.0, self._source_rate(root, it)]
            return out[root]
        boosts = []
        for ln in self.links:
            it = ln.get("item")
            if it is None or (item is not None and it != item) or ln.get("kind") in ("trunk", "science"):
                continue
            if ln.get("boost_into") is not None:
                boosts.append((ln, it))
                continue
            root = self._coal_root(ln)
            if root is not None:
                row(root, it)[0] += float((ln.get("port") or {}).get("per_minute") or 0)
        for ln, it in boosts:                         # after every port is counted: what each chain lacks
            src = row(tuple(ln["source"]), it)
            give = max(0.0, src[1] - src[0])
            if ln.get("boost_of") is not None:
                chain = row(tuple(ln["boost_of"]), it)
                give = min(give, max(0.0, chain[0] - chain[1]))
                chain[1] += give
            src[0] += give
        return out

    def _coal_budget(self) -> dict[tuple, list[float]]:
        return self._budget("coal")

    def _fits(self, budget: dict, root: tuple | None, need: float, item: str) -> bool:
        """May one more port draw on this source? An unused source: always (it is boosted if short). A used
        one: only when what it has left covers the port in full."""
        if root is None:
            return True
        if tuple(root) in (getattr(self, "_avoid_roots", None) or ()):
            return False                              # a relink found this source did not feed its port
        load, supply = budget.get(tuple(root)) or [0.0, self._source_rate(tuple(root), item)]
        left = supply - load
        return left > 0.5 and (load <= 0.5 or need <= left + 0.5)

    def _overloaded(self, item: str | None = None) -> list[tuple]:
        """Chains whose ports take more than their source gets."""
        return [root for root, (load, supply) in self._budget(item).items() if load > supply + 0.5]

    def _item_of_root(self, root: tuple) -> str | None:
        return next((ln["item"] for ln in self.links if ln.get("boost_into") is None
                     and ln.get("kind") not in ("trunk", "science") and self._coal_root(ln) == tuple(root)), None)

    def _spare(self, item: str) -> list[tuple[float, float, Site]]:
        """Sources of the item nothing takes from yet (a mine or factory built after the belts were laid)."""
        used = {tuple(ln["source"]) for ln in self.links}
        return [(x, y, s) for x, y, s in self.outputs(item)
                if (x, y) not in used and not (item == "coal" and s.kind == "burner_mine")]

    def _spare_coal(self) -> list[tuple[float, float, Site]]:
        return self._spare("coal")

    def short_chains(self) -> list[dict]:
        """Every belt chain whose ports take more than its source gets: the source, the item, what it feeds,
        taken vs got, spare sources of the item, and whether feeding one in may be tried now."""
        out = []
        for root, (load, supply) in self._budget().items():
            if load <= supply + 0.5:
                continue
            item = self._item_of_root(root)
            if item is None:
                continue
            sites = sorted({ln["to_site"] for ln in self.links if ln["item"] == item
                            and ln.get("boost_into") is None and self._coal_root(ln) == root})
            spare = len(self._spare(item))
            out.append({"item": item, "chest": list(root), "feeds_sites": sites, "burns_per_min": round(load, 1),
                        "gets_per_min": round(supply, 1), "spare_sources": spare, "spare_coal_chests": spare,
                        "boost_may_retry": self._may_retry(("boost",) + tuple(root))})
        return out

    def coal_chains(self) -> list[dict]:
        """The coal belts that run short (see short_chains)."""
        return [c for c in self.short_chains() if c["item"] == "coal"]

    def patch_full(self, resource: str) -> bool:
        """Did a mine of this resource find no room on its patches (and nothing changed since)?"""
        f = self._patch_full.get(resource)
        return f is not None and f[1] == self._patch_sig(resource) and self._minute() - f[0] < 30

    def _patch_sig(self, resource: str) -> tuple:
        """What changes the room on a resource's patches: its own mines standing or coming down. Run
        20261124-120854: the mark was keyed on every site in the base, so the next unrelated build cleared it,
        the ore outlook said "room for more" again and copper's next patch was never planned."""
        return tuple(sorted(s.id for s in self.base.sites if s.params.get("resource") == resource
                            and s.kind in ("electric_mine", "burner_mine", "outpost")))

    def _sideload_ports(self, chain: list[dict], item: str = "coal") -> list[tuple[dict, dict]]:
        """Where a belt can join a chain from the side: beside a straight belt fed straight from behind (so it
        stays straight), pointing into it. A port-like target for each, with the link it joins. A kept belt whose
        source came down (its inserter taken up) is joined head-on too: a belt running into its first tile from
        behind, where the inserter stood. Run 20261124-015025: kept belts with no straight stretch to join from
        the side were taken up and relinked."""
        from . import belts
        others = {tuple(t) for s, pt in self.all_ports() for t in (pt["end"], pt["approach"])}
        out = []
        for ln in chain:
            st = ln.get("steps", [])
            if ln.get("ins_removed") and st and st[0][0] == "belt":
                vx, vy = belts.VEC[st[0][2]]
                end = (st[0][1][0] - vx, st[0][1][1] - vy)
                appr = (end[0] - vx, end[1] - vy)
                if not ({end, appr} & others) and all(self._belt_free(t) for t in (end, appr)):
                    out.append(({"item": item, "end": list(end), "approach": list(appr), "per_minute": 0,
                                 "boost": True, "head": True}, ln))
            for i in range(1, len(st)):
                # b stays straight when something feeds it from behind in its own direction: a belt facing the
                # same way, or an underground exit. What comes after b does not matter (an underground entrance,
                # a turn, the port). Run 20261127-101219: the bus head's only iron belt was 27 steps of which all
                # but five were underground pairs, no three straight belts in a row, so no spare source could
                # ever join it; triage built four more iron mines instead, none of which could join either.
                a, b = st[i - 1], st[i]
                if b[0] != "belt" or a[2] != b[2]:
                    continue
                if not (a[0] == "belt" or (a[0] == "ug" and len(a) > 3 and a[3] == "output")):
                    continue
                avx, avy = belts.VEC[a[2]]
                if (a[1][0] + avx, a[1][1] + avy) != tuple(b[1]):
                    continue
                for p in (belts.LEFT[b[2]], belts.RIGHT[b[2]]):
                    vx, vy = belts.VEC[p]
                    end = (b[1][0] - vx, b[1][1] - vy)
                    appr = (end[0] - vx, end[1] - vy)
                    if {end, appr} & others or not all(self._belt_free(t) for t in (end, appr)):
                        continue
                    out.append(({"item": item, "end": list(end), "approach": list(appr), "per_minute": 0,
                                 "boost": True}, ln))
        return out

    def _belt_free(self, t) -> bool:
        """May a belt go on this tile (lanes kept for belts count as free, a hand-fed row's coal strip not)?"""
        if t in self._strip_tiles():
            return False
        fb = getattr(self.w, "free_belt", None)
        return fb(t) if fb is not None else self.w.free({t})

    # A furnace row built before logistics is fed by hand until it gets its coal belt behind its chests. Other
    # builds and links took that strip, and five rows stayed hand-fed for good, running dry again and again
    # (run 20261119-202811: "no room for a coal belt"). Their strip is kept free from the start: no building
    # (a lane) and no other belt (_belt_free) until the row has its belt or is gone.
    def _strip_tiles(self) -> set:
        return {tuple(t) for ts in getattr(self, "fuel_strips", {}).values() for t in ts}

    def _row_strip(self, site: Site) -> set | None:
        """The tiles a hand-fed furnace row's coal belt will take (None: not such a row)."""
        if site.kind != "electric_mine" or site.params.get("into") != "furnace" or site.params.get("ports") \
                or site.params.get("fuel") == "electric":
            return None
        em = MODULES["electric_mine"]
        drills = sorted({(math.floor(x), math.floor(y)) for name, x, y, _ in site.entities
                         if name == "electric-mining-drill"})
        if not drills or len({y for _, y in drills}) != 1:
            return None
        cy = drills[0][1]
        xs = [cx for cx, _ in drills]
        units = [em.unit(cx, cy, "furnace", fuel=True) for cx in xs]
        return {u["fuel_inserter"] for u in units} | {t for u in units for t in u["belt"]} | set(em.fuel_port(xs[0], cy))

    def reserve_strips(self) -> None:
        """Keep the coal strip of every hand-fed furnace row free (what of it is still free)."""
        strips = self.__dict__.setdefault("fuel_strips", {})
        lanes = getattr(self.w, "lanes", None)
        for s in self.base.sites:
            if s.id in strips:
                continue
            need = self._row_strip(s)
            if not need:
                continue
            free = {t for t in need if getattr(self.w, "free_belt", lambda q: True)(t)}
            strips[s.id] = sorted(free)
            if lanes is not None:
                lanes |= free

    def release_strip(self, site_id: int) -> None:
        mine = set(map(tuple, getattr(self, "fuel_strips", {}).pop(site_id, [])))
        lanes = getattr(self.w, "lanes", None)
        if lanes is not None and mine:
            keep = {tuple(t) for ts in getattr(self, "site_lanes", {}).values() for t in ts}
            lanes -= mine - keep

    BOOST_TRIES = 6

    def _no_join_before(self, key: tuple) -> bool:
        """Did this boost last fail because its belts had no place to join from the side?"""
        return "join from the side" in ((self._link_fails.get(tuple(key)) or {}).get("reason") or "")

    def boost_ready(self, root: tuple, item: str) -> bool:
        """May a spare source be fed into the chain from root now: its back-off is over, or it failed only for
        want of a join point and its belts have one now."""
        key = ("boost",) + tuple(root)
        if self._may_retry(key):
            return True
        if not self._no_join_before(key):
            return False
        chain = [ln for ln in self.links if ln["item"] == item and ln.get("boost_into") is None
                 and ln.get("kind") not in ("trunk", "science") and self._coal_root(ln) == tuple(root)]
        return bool(chain and self._sideload_ports(chain, item))

    def boost_links(self) -> tuple[list[str], list[str]]:
        """Feed a spare source into every chain whose ports take more than its source gets (any item): a belt
        from the spare chest side-loads onto one of the chain's straight belts. Returns (done, failed)."""
        done, failed = [], []
        roots = list(self._overloaded())
        # belts whose source came down get the first spare source (the replacement, usually) fed in, whatever
        # their ports book: a port that books nothing still gets nothing from a removed mine
        roots = [r for r in self.gone_roots() if not self.spliced(r) and r not in roots] + roots
        for root in roots:
            item = self._item_of_root(root)
            if item is None:
                continue
            key = ("boost",) + tuple(root)
            spare = self._spare(item)
            chain = [ln for ln in self.links if ln["item"] == item and ln.get("boost_into") is None
                     and ln.get("kind") not in ("trunk", "science") and self._coal_root(ln) == root]
            if not spare or not chain:
                continue
            targets = self._sideload_ports(chain, item)
            if not self._may_retry(key) and not (targets and self._no_join_before(key)):
                continue                              # (it failed for want of a join point and now has one)
            site = self.base.get(chain[0]["to_site"])
            near = lambda q: min(math.dist(q[0]["end"], (x, y)) for x, y, _ in spare)
            rec = None
            # the nearest side-joins first; then the three nearest again with a wide detour (run 20261122-133537:
            # "no belt route from a spare ... source to the side of its belts" on every build, mines standing full)
            order = [(t, 40) for t in sorted(targets, key=near)[:self.BOOST_TRIES]] + \
                [(t, 70) for t in sorted(targets, key=near)[:3]]
            for (port, ln), margin in order:
                srcs = sorted(spare, key=lambda q: math.dist(q[:2], port["end"]))[:3]
                plan = self._plan_link(port, srcs, [], margin, False)
                if plan is None:
                    continue
                try:
                    rec = self._build_link(plan, port, site, "item", None, extra={"boost_into": ln,
                                                                                 "boost_of": tuple(root)})
                except BuildError:
                    continue
                break
            if rec is None:
                why = (f"no belt route from a spare {item} source to the side of its belts" if targets else
                       "its belts have no free straight stretch to join from the side")
                self._note_link_fail(key, item, site, why)
                failed.append(f"extra {item} into the chain from {list(root)} (sites "
                              f"{sorted({q['to_site'] for q in chain})}): {why}")
                continue
            self._link_fails.pop(key, None)
            done.append(f"extra {item} from {rec['from']} into the belt of site {rec['to_site']} "
                        f"({rec['belts']} belts)")
        return done, failed

    boost_coal = boost_links

    def link(self) -> str:
        """The manager's link step: science outputs to research stations, then every input port that has a
        source of its item somewhere (fuel ports first: they keep the smelters burning). A port with no link yet
        keeps taking deliveries from the courier. Each port escalates on its own (see _link_port); a call that
        links nothing while links are stuck fails with why and what would unblock them, so a planner that asks
        again learns it will not work until something changes."""
        parts, science_done = [], False
        expired = self.expire_gone()                  # kept belts no new source could be fed into: linked afresh
        if expired:
            parts.append("; ".join(expired))
        for out_site, col in self.pending_trunks():   # an outpost whose trunk failed before
            msg = self._finish_outpost(out_site)
            if out_site.params.get("trunk_to") is None:
                self._note_link_fail(("trunk", out_site.id), out_site.params["resource"], out_site, msg)
            else:
                science_done = True
            parts.append(f"outpost {out_site.id}: {msg}")
        with self.w.step("build"):                    # hand-fed rows and boilers get coal belts once logistics is in
            retro = self.retrofit_fuel()
        if retro:
            parts.append("; ".join(retro))
            science_done = True                       # something was done: not a failed link step
        for row in self.rows_for_lines()[:1]:         # a row of idle plate chests becomes one line (phase 1)
            with self.w.step("build"):
                try:
                    parts.append(self.to_line(row))
                    science_done = True
                except BuildError as exc:
                    self._line_tried[row.id] = self._minute()
                    parts.append(f"site {row.id} stays a chest row for now: {str(exc)[:140]}")
        try:
            if any(s.params.get("station") for s in self.base.of("labs")):
                m = self.link_science()
                science_done = m.startswith("linked ") and not m.startswith("linked nothing")
                parts.append(m)
        except BuildError as exc:
            parts.append(str(exc)[:300])
        done, failed = [], []
        with self.w.step("build"):
            for site, pt in sorted(self.unfed_ports(), key=lambda q: (not q[1].get("fuel"), q[1]["item"])):
                with self.w.step("between"):          # one link stands whole, the next not begun: a safe point
                    self.checkpoint()
                srcs = self._free_outputs(pt["item"], site, self._need(pt))
                taps = self._belt_taps(pt["item"], site, self._need(pt))
                if not srcs and not taps:
                    continue
                try:
                    rec, how = self._link_port(pt, site, srcs, taps, kind="item")
                except BuildError as exc:
                    self._note_link_fail(tuple(pt["end"]), pt["item"], site, str(exc))
                    failed.append(f"{pt['item']} into site {site.id}: {str(exc)[:220]}")
                    continue
                self._link_fails.pop(tuple(pt["end"]), None)
                done.append(f"{pt['item']} from {rec['from']} into site {site.id} ({rec['belts']} belts{how})")
            more, short = self.boost_links()            # belts whose ports take more than their source gets
            done += more
            failed += short
        for c in self.coal_chains():
            if not c["spare_coal_chests"]:
                parts.append(f"the coal belt from the chest at {c['chest']} feeds sites {c['feeds_sites']} that burn "
                             f"{c['burns_per_min']}/min but gets {c['gets_per_min']}/min: build electric_mine coal "
                             "into chest and the next link step feeds it in")
        if done:
            parts.append(f"linked {len(done)}: " + "; ".join(done))
        if failed:
            parts.append(f"not linked ({len(failed)}): " + self._group_fails(failed))
        skipped = [(s, pt) for s, pt in self.unfed_ports() if not pt.get("fuel")
                   and not self._free_outputs(pt["item"], s, self._need(pt))
                   and not self._belt_taps(pt["item"], s, self._need(pt))]
        if skipped:                                   # left out above without a word before (run 20261120-092648)
            parts.append(f"not tried ({len(skipped)}, no source with spare): " + ", ".join(
                f"{pt['item']} into site {s.id}" for s, pt in skipped[:8]) + (" ..." if len(skipped) > 8 else ""))
        # ok or not by the consumer ports: a science output with no free station port is said above, not a
        # failed link step (run 20261122-133537: "link" failed ~20x on one science text)
        packs_at = {(x, y) for x, y, _ in self.science_sources()}
        stuck = [q for q in self.stuck_links() if tuple(q["at"]) not in packs_at]
        if not done and not science_done and (failed or stuck):
            if not failed:
                parts.append("nothing new to try: " + "; ".join(
                    f"{q['item']} into site {q['into_site']} failed {q['failed']}x ({q['why'][:120]})" for q in stuck[:3]))
            raise BuildError(". ".join(p for p in parts if p) + ". " + self.LINK_ADVICE)
        return ". ".join(p for p in parts if p) or "nothing to link"

    @staticmethod
    def _group_fails(failed: list[str]) -> str:
        """Every failed link, grouped by what into which site, then the first reasons in full: a planner sees all
        18 (run 20261120-092648 showed 3 and the rest never reached anyone)."""
        groups: dict[str, int] = {}
        for f in failed:
            head = f.split(":", 1)[0]
            groups[head] = groups.get(head, 0) + 1
        listed = ", ".join(f"{h} x{n}" if n > 1 else h for h, n in groups.items())
        return listed + ". First: " + "; ".join(failed[:2])

    def triaged_relink(self, goal: dict) -> str:
        """A relink, after finding out what is really wrong (triage.py): when the cause is not the belt, the repair
        that fits is queued first in its place and the relink is not done."""
        from . import triage
        from .overseer import same_target
        st = self.base.get(goal.get("site"))
        if st is not None and st.kind == "labs" and st.params.get("station")                 and str(goal.get("item") or "").endswith("science-pack"):
            return self.relink(st.id, goal.get("item"))      # a station's packs: no triage of plate inputs
        try:
            fix, note, avoid = triage.route(self, goal)
        except Exception as exc:  # noqa: BLE001 - a failed diagnosis falls back to the plain relink
            fix, note, avoid = None, f"no diagnosis ({type(exc).__name__}: {str(exc)[:80]})", []
        self.log(f"[crew] relink site {goal.get('site')} {goal.get('item') or ''}: {note}")
        if fix is not None and fix.get("skip"):
            return f"{note}; no relink"
        if fix is not None:
            from .agenda import agenda_of
            queued, why = agenda_of(self).propose(self, {**fix, "reflex": f"triage: {note}"[:160]}, "triage",
                                                  front=True)
            if queued or why.startswith("already"):
                return f"{note}; a relink would not fix that, so {triage._short(fix)} goes first instead"
            # the overseer refused that fix (run 20261125-214645: an iron outpost it dropped as "a routing
            # failure, not a shortage" came back from triage four times): the relink it asked for runs instead
            note = f"{note}; its fix ({triage._short(fix)}) is {why[len('not queued: '):]}: relinking as asked"
            return f"{note}. " + self.relink(goal.get("site"), goal.get("item"), avoid=avoid)
        return f"{note}. " + self.relink(goal.get("site"), goal.get("item"), avoid=avoid)

    def relink(self, site_id, item: str | None = None, avoid=()) -> str:
        """Take up the belts into one site's input ports (of one item, or all) and lay them again: from a line
        that exists now, or around what walled them in. The site itself stays: rework by input, not by whole
        site (docs/logistics-plan.md, phases 4-5)."""
        site = self.base.get(site_id)
        if site is None:
            raise BuildError(f"there is no site {site_id}")
        if site.kind == "labs" and site.params.get("station") and str(item or "").endswith("science-pack"):
            return self._relink_station(site, item)
        ports = [pt for pt in site.params.get("ports", []) if pt.get("item") and not pt.get("trunk")
                 and (item is None or pt["item"] == item)]
        if not ports:
            raise BuildError(f"site {site.id} has no input port" + (f" for {item}" if item else ""))
        ends = {tuple(pt["end"]) for pt in ports}
        mine = [ln for ln in self.links if tuple((ln.get("port") or {}).get("end") or ()) in ends
                and ln.get("kind") != "science"]
        # a belt other factories tap (or that a spare source is fed into) stays: taking it up unplugged them, and
        # they were relinked in turn (run 20261121-140755: site 56 relinked 10 times, its neighbours after it).
        # Such a belt starving is a supply problem: more is fed into it instead.
        shared = [ln for ln in mine if any(h.get("tap_of") is ln or h.get("boost_into") is ln for h in self.links)]
        gone = [ln for ln in mine if not any(ln is s for s in shared)]
        boosted: list[str] = []
        if shared and not gone:
            done, failed = self.boost_links()
            boosted = done or failed
            return (f"site {site.id}: its {item or 'input'} belt is shared with sites "
                    f"{sorted({h['to_site'] for s in shared for h in self.links if h.get('tap_of') is s})}"
                    " and stays where it is; " + ("; ".join(boosted)[:240] if boosted else
                                                  "no spare source to feed into it: more of it must be made"))
        for q in reversed(gone):
            self._unlink(q)
        for e in ends:
            self._link_fails.pop(e, None)
        self._avoid_roots = {tuple(r) for r in avoid}   # sources that already failed this input: not again
        try:
            msg = self.link()
        except BuildError as exc:
            msg = str(exc)[:300]
        finally:
            self._avoid_roots = set()
        fed = sum(1 for pt in ports if pt.get("fed"))
        return (f"site {site.id}: took up {len(gone)} link(s) into its {item or 'input'} ports and laid them "
                f"again: {fed}/{len(ports)} fed. {msg[:240]}")

    def _relink_station(self, site: Site, pack: str) -> str:
        """A research station's ports take packs (port "pack"), not items: its belts of that pack come up and the
        science links are laid again. Run 20261125-214645: "relink site 24 automation-science-pack" failed twice
        with "no input port" and was forbidden, while red science backed up and the labs waited."""
        gone = [ln for ln in self.links if ln.get("kind") == "science" and ln.get("to_site") == site.id
                and ln.get("item") == pack]
        for ln in gone:
            self._unlink(ln)
            self._link_fails.pop(tuple(ln["port"]["end"]), None)
        msg = self.link_science()
        return (f"research station {site.id}: took up {len(gone)} {pack} belt(s) and laid the science links "
                f"again: {str(msg)[:240]}")

    TRUNK_SOURCES = 3              # far sources a trunk goal tries, nearest first

    def _station_trunk_target(self, site: Site, item: str | None) -> tuple[dict | None, list | str]:
        """(free port, unlinked outputs of `item` nearest first) for a trunk into research station `site`, or
        (None, why nothing is needed). Raises when it cannot be done."""
        if not item:
            raise BuildError(f'a trunk into research station {site.id} needs the pack: {{"do": "trunk", "site": '
                             f'{site.id}, "item": "chemical-science-pack"}}')
        ports = site.params.get("ports", [])
        if any(pt.get("pack") == item for pt in ports):
            return None, f"research station {site.id} already gets {item} by belt"
        free = [pt for pt in ports if pt.get("pack") is None]
        if not free:
            raise BuildError(f"research station {site.id} has no free port for {item}")
        srcs = [(x, y, self._source_site((x, y), item)) for x, y, p in self.science_sources()
                if p == item and (x, y) not in self.science_links]
        if not srcs:
            raise BuildError(f"no {item} output without a belt to a research station: nothing to carry")
        port = min(free, key=lambda pt: min(self._manhattan(q[:2], pt["end"]) for q in srcs))
        return port, sorted(srcs, key=lambda q: self._manhattan(q[:2], port["end"]))

    def trunk(self, site_id, item: str | None = None) -> str:
        """One long belt (beyond a link's reach, belts.LINK_REACH) from the nearest spare source of an item into
        one unfed port of a site, as a goal with its own budget: the ground along the way is looked at first and
        the route keeps to a corridor along the straight line (outposts.plan_trunk). Never laid inside another
        goal's link step (run 20261121-140755: a 480-tile copper belt inside a pump build)."""
        from . import belts, outposts
        site = self.base.get(site_id)
        if site is None:
            raise BuildError(f"there is no site {site_id}")
        kind, pack = "item", None
        if site.params.get("station"):
            # a research station's ports take any colour (port["pack"] says which once belted). Run
            # 20261124-120854: "trunk site 50 chemical-science-pack" answered "no unfed input port", so the far
            # blue workshop never reached a lab and a new station was asked for instead
            port, srcs = self._station_trunk_target(site, item)
            if port is None:
                return srcs                               # why nothing is needed
            it, kind, pack = item, "science", item
            if self._manhattan(srcs[0][:2], port["end"]) <= belts.LINK_REACH:
                return f"a {it} output is within a link's reach of station {site.id}: " + self.link_science()
        else:
            ports = [pt for pt in site.params.get("ports", []) if pt.get("item")
                     and not pt.get("fed") and (item is None or pt["item"] == item)]
            if not ports:
                raise BuildError(f"site {site.id} has no unfed input port" + (f" for {item}" if item else ""))
            port = ports[0]
            if port.get("trunk"):
                # a column's ore port is what a trunk is for: its source is the outpost's head. Run 20261122-133537:
                # "trunk site 33" was ordered 6x and each answered "no unfed input port for iron-ore"
                return self._outpost_trunk(site, port)
            it = port["item"]
            srcs = sorted(self._free_outputs(it, site, self._need(port)),
                          key=lambda q: self._manhattan(q[:2], port["end"]))
            if not srcs:
                raise BuildError(f"no source of {it} with any to spare for site {site.id}: build one first")
            if self._manhattan(srcs[0][:2], port["end"]) <= belts.LINK_REACH:
                return f"a {it} source is within a link's reach of site {site.id}: " + self.relink(site.id, it)
        others = {tuple(t) for s, pt in self.all_ports() if pt is not port for t in (pt["end"], pt["approach"])}
        appr = tuple(port["approach"])
        errors: list[str] = []
        for x, y, src in srcs[:self.TRUNK_SOURCES]:
            key = (x, y)
            label = f"site {src.id}" if src is not None else f"({x:.0f}, {y:.0f})"
            c = (math.floor(x), math.floor(y))
            n = max(1, int(math.dist(c, appr) // 40))
            self.w.doing(f"surveying a {it} trunk from {label} to site {site.id}")
            for k in range(n + 1):
                try:
                    self.w.look(c[0] + (appr[0] - c[0]) * k / n, c[1] + (appr[1] - c[1]) * k / n, radius=32)
                except BuildError:
                    pass
            ln = self.line_at(key)
            if ln is not None:                         # a line's tail: its belt runs on, no inserter
                starts = [(tuple(ln["start"]), None, None)]
            else:
                starts = []
                for dx, dy in belts.VEC.values():
                    ins, start = (c[0] + dx, c[1] + dy), (c[0] + 2 * dx, c[1] + 2 * dy)
                    if not all(self._belt_free(t) for t in (ins, start)) or {ins, start} & (others | self.bad_tiles()):
                        continue
                    facing = next(k for k, v in belts.VEC.items() if v == (-dx, -dy))
                    starts.append((start, ins, facing))
            starts.sort(key=lambda q: self._manhattan(q[0], appr))
            for start, ins, facing in starts:
                steps = outposts.plan_trunk(self, start, port, block={ins} if ins is not None else set())
                if not steps:
                    continue
                if ins is not None and not self._can_power(ins, avoid=others | set(belts.tiles_of(steps)) | {ins}):
                    continue
                plan = {"ins": ins, "facing": facing, "steps": steps, "source": key, "from": label,
                        "tap": None, "rip": [], "relay": None}
                try:
                    with self.w.step("build"):
                        rec = self._build_link(plan, port, site, kind, pack, extra={"long": True})
                except BuildError as exc:
                    errors.append(f"from {label}: {str(exc)[:100]}")
                    break
                self._link_fails.pop(key if kind == "science" else tuple(port["end"]), None)
                return (f"a {rec['belts']}-belt trunk carries {it} from {label} into site {site.id} "
                        f"({belts.route_len(steps)} tiles)")
            else:
                errors.append(f"from {label}: no route inside a corridor along the way")
        # a science failure is keyed by its source chest (link_science), an item port's by the port
        fail_key = (srcs[0][0], srcs[0][1]) if kind == "science" else tuple(port["end"])
        self._note_link_fail(fail_key, it, site, "trunk: " + "; ".join(errors)[:200])
        raise BuildError(f"no {it} trunk into site {site.id}: " + "; ".join(errors)[:300]
                         + ". A mine, smelter or factory of it next to the site avoids the long belt")

    LINE_RETRY_MIN = 30.0

    def rows_for_lines(self) -> list[Site]:
        """Plate rows of chests worth turning into a line where they stand: two or more plate chests, and either
        no link at all or at least three chests nobody draws from with at most one link (run 20261120-092648:
        site 25 had six full chests and one link, while factories starved for iron). A row with more links
        stays as it is: taking those links up could leave a fed factory without its belt."""
        if not lines_ready(self.kn):
            return []
        tried = getattr(self, "_line_tried", None)
        if tried is None:
            tried = self._line_tried = {}
        used = Counter(tuple(ln["source"]) for ln in self.links)
        out = []
        for s in self.base.of("electric_mine"):
            if s.params.get("line") or s.params.get("into") != "furnace" or _depleted(s) \
                    or s.params.get("fuel") == "electric":     # its units stand 4 apart: to_line is for burner rows
                continue
            if self._minute() - tried.get(s.id, -1e9) < self.LINE_RETRY_MIN:
                continue
            plate = PLATE_OF.get(s.params.get("resource"))
            chests = [(x, y) for x, y, w in s.chests if w == plate]
            linked = sum(1 for c in chests if used.get(c))
            if len(chests) >= 2 and (linked == 0 or (len(chests) - linked >= 3 and linked <= 1)):
                out.append(s)
        return sorted(out, key=lambda q: -len(q.chests))

    def to_line(self, site: Site) -> str:
        """Turn a row's plate chests into one line on the same tiles: the chests (and the links that drew from
        them) are taken up, a belt is laid along the chest row toward the base, and the link step belts the line
        into the ports that wait. The units' furnaces and inserters stay as they are."""
        plate = PLATE_OF.get(site.params.get("resource"))
        chests = sorted(((x, y) for x, y, w in site.chests if w == plate), key=lambda c: c[0])
        if len(chests) < 2:
            raise BuildError("fewer than two plate chests")
        y = math.floor(chests[0][1])
        xs = [math.floor(x) for x, _ in chests]
        if any(math.floor(cy) != y for _, cy in chests) or any(b - a != MODULES["electric_mine"].UNIT_W
                                                             for a, b in zip(xs, xs[1:])):
            raise BuildError("its chests are not one straight row of units")
        keys = set(chests)
        drained = [ln for ln in self.links if tuple(ln["source"]) in keys]
        gone: list = []
        todo = list(drained)
        while todo:                                   # and what taps them: it would carry nothing either
            q = todo.pop()
            if any(q is g for g in gone):
                continue
            gone.append(q)
            todo += [h for h in self.links if h.get("tap_of") is q or h.get("boost_into") is q]
        theirs = {t for q in gone for name, x, yy, d in q.get("entities", []) for t in self.w.footprint(name, x, yy, d)}
        between = [(x, y) for x in range(xs[0], xs[-1] + 1) if (x, y) not in {(cx, y) for cx in xs}]
        taken = [t for t in between if not (self._belt_free(t) or t in theirs)]
        if taken:
            raise BuildError(f"the tiles between its chests are taken ({taken[:3]})")
        self.unlock_items(["transport-belt"])
        self.make({"transport-belt": len(xs) * 3 + 2}, extra=True)
        for q in reversed(gone):
            self._unlink(q)
            self._link_fails.pop(tuple(q["port"]["end"]), None)
        for x, cy in chests:
            self._empty_container(x, cy)
            ent = next((e for e in site.entities if (e[1], e[2]) == (x, cy)), ("iron-chest", x, cy, "north"))
            self.w.pickup(*ent)
            site.entities = [e for e in site.entities if (e[1], e[2]) != (x, cy)]
        site.chests = [c for c in site.chests if (c[0], c[1]) not in keys]
        ents: list = []
        self.w.recorder = ents
        try:
            units = [(x, y + 5) for x in xs]
            site.params["line"] = MODULES["electric_mine"].lay_line(self.w, self.base, units, plate)
        finally:
            self.w.recorder = None
        site.entities += ents
        site.params["into"] = "line"
        ln = site.params["line"]
        return (f"site {site.id}: {len(chests)} plate chests turned into one {plate} line (~{ln['rate']:.0f}/min, "
                f"flowing {ln['facing']})" + (f"; {len(gone)} link(s) taken up to be laid from the line" if gone else ""))

    LINK_ADVICE = ("A failed link is retried by itself once a site is built or removed; asking for link again "
                   "before that changes nothing. To unblock it: remove the site named as walling it in (if it is "
                   "not needed), build a new source of the item next to the consumer, or replace the consumer: a "
                   "replace is placed only where a route probe finds a belt route for every input, and is refused "
                   "near a spot where the same factory already failed.")

    def link_science(self) -> str:
        """Belt every science output that has none to a free port of a research station: an inserter lifts the
        packs out of the source chest onto a belt that runs head-on into the port (so it never feeds a
        neighbouring port's belt). The labs then run without the crew carrying packs."""
        stations = [s for s in self.base.of("labs") if s.params.get("station")]
        if not stations:
            raise BuildError('no research station to link to: build one first ({"do": "build", "module": '
                             '"research_station", "count": 6})')
        todo = self.unlinked_sources()
        if not todo:
            with self.w.step("build"):
                tapped = self._tap_short_stations(stations)
            if tapped:
                return "linked by tapping a science belt: " + "; ".join(tapped)
            # said from what stands, not from the retry list: run 20261121-140755 said "every science output
            # already reaches a research station" while red and green had failed 14x into site 53
            waiting = self.unreached_science()
            if not waiting:
                return "every science output already reaches a research station by belt"
            self._move_labs(waiting)
            raise BuildError(f"{len(waiting)} science output(s) reach no research station: " + "; ".join(
                f"{p} at ({x:.0f}, {y:.0f}) failed {(self._link_fails.get((x, y)) or {}).get('n', 0)}x"
                for x, y, p in waiting[:3]) + " (each retries after its back-off)")
        done, failed, spare_now = [], [], []
        if not isinstance(getattr(self, "_spare_science", None), dict):
            self._spare_science = {}
        with self.w.step("build"):
            for x, y, pack in sorted(todo, key=lambda q: min(math.dist(q[:2], s.centre) for s in stations)):
                # a station runs only with every colour the research needs: fill the one that lacks this
                # colour and already has the most others, nearest first (run 20261119-154451: one station got
                # only red, others nothing, and none of them researched)
                def packs(s):
                    return {pt.get("pack") for pt in s.params["ports"] if pt.get("pack")}
                # a port no belt reaches comes last whatever colours its station has: run 20261124-120854 sent
                # blue (handled first, while station 65 sixty tiles off had no colour yet) to station 50's ports
                # 233+ tiles away, all three tries failed, and the back-off kept it from station 65 for good
                from .belts import LINK_REACH
                ports = sorted(((self._manhattan((x, y), pt["end"]) > LINK_REACH, pack in packs(s), -len(packs(s)),
                                 math.dist((x, y), s.centre), s, pt)
                                for s in stations for pt in s.params["ports"] if pt["pack"] is None),
                               key=lambda q: q[:4])
                ports = [(q[3], q[4], q[5]) for q in ports]
                if not ports:
                    if self._pack_everywhere(pack, stations):
                        # every station already gets this colour from another output: this one is spare, not a
                        # failed link (run 20261122-133537: "link" failed ~20x on "every port is taken", which fed
                        # replans and a fixer's guess, research station 47 that only ever got green)
                        spare_now.append(f"{pack} at ({x:.0f}, {y:.0f})")
                        self._spare_science[(x, y)] = pack
                        continue
                    failed.append(f"{pack} at ({x:.0f}, {y:.0f}): every port is taken (build another station)")
                    continue
                rec, err = None, None
                for _, st, port in ports[:3]:          # the nearest free port first, then the next ones
                    try:
                        rec, how = self._link_port(port, st, [(x, y, None)], [], kind="science", pack=pack)
                        break
                    except BuildError as exc:
                        err = exc
                if rec is None:
                    # kept against the nearest station with a free port: a trunk goal (assess, far links) runs there
                    near = min((q[1] for q in ports), key=lambda s: math.dist((x, y), s.centre))
                    self._note_link_fail((x, y), pack, near, str(err))
                    failed.append(f"{pack} at ({x:.0f}, {y:.0f}): {str(err)[:200]}")
                    continue
                self._link_fails.pop((x, y), None)
                done.append(f"{pack} from ({x:.0f}, {y:.0f}) to station site {rec['to_site']}, {rec['belts']} belts{how}")
            done += self._tap_short_stations(stations)
        moved = self._move_labs(self.unreached_science())
        if moved:
            failed.append(moved)
        msg = (f"linked {len(done)}: " + "; ".join(done)) if done else "linked nothing"
        said = self.__dict__.setdefault("_spare_said", set())
        new_spare = [s for s in spare_now if s not in said]
        if new_spare:                                  # said once: it is no failure and needs nothing
            said.update(new_spare)
            msg += (f". Spare ({len(new_spare)}): " + ", ".join(new_spare[:4])
                    + " (every station already gets that colour by belt from another output)")
        if failed:
            msg += f". Not linked ({len(failed)}): " + "; ".join(failed[:3])
            if not done:
                raise BuildError(msg)
        return msg

    def _tap_short_stations(self, stations: list) -> list[str]:
        """A station that lacks a colour the base makes, with no source chest of it free or routable, takes it
        from a belt that already carries that colour to another station: an inserter lifts packs off it onto a
        belt into a free port. Run 20261124-120854: station 41 got red only; the one free green chest was boxed in
        by other belts, the router was never offered the three green belts into site 26, and 41's labs sat idle
        for 45 minutes. Returns a note per link laid."""
        made = {p for *_, p in self.science_sources()}
        notes = []
        for st in stations:
            have = {pt.get("pack") for pt in st.params.get("ports", []) if pt.get("pack")}
            free = [pt for pt in st.params.get("ports", []) if pt.get("pack") is None]
            for pack in sorted(made - have):
                if not free:
                    break
                key = ("tap", st.id, pack)
                if not self._may_retry(key):
                    continue
                taps = self._belt_taps(pack, st)
                if not taps:
                    continue
                port = min(free, key=lambda pt: min(self._manhattan(t, pt["end"]) for t, _ in taps))
                try:
                    rec, how = self._link_port(port, st, [], taps, kind="science", pack=pack)
                except BuildError as exc:
                    self._note_link_fail(key, pack, st, str(exc))
                    continue
                self._link_fails.pop(key, None)
                free.remove(port)
                notes.append(f"{pack} into station site {st.id}, {rec['belts']} belts{how}")
        return notes

    @staticmethod
    def _pack_everywhere(pack: str, stations: list) -> bool:
        """Does every research station already get this colour by belt?"""
        return bool(stations) and all(any(pt.get("pack") == pack for pt in s.params.get("ports", []))
                                      for s in stations)

    def unreached_science(self) -> list[tuple[float, float, str]]:
        """Science outputs no station port takes by belt, whether or not their next retry is due yet. A spare
        output (link_science: every station already gets its colour) is not waiting for anything."""
        stations = [s for s in self.base.of("labs") if s.params.get("station")]
        spare = getattr(self, "_spare_science", None) or {}
        return [(x, y, p) for x, y, p in self.science_sources() if (x, y) not in self.science_links
                and not ((x, y) in spare and self._pack_everywhere(p, stations))]

    LAB_MOVE_FAILS = 3             # failures of one science link before the labs come to the science instead
    LAB_MOVE_GAP_MIN = 60.0

    def _move_labs(self, waiting: list) -> str:
        """A science link that failed LAB_MOVE_FAILS times is not retried into the same crowded station for
        ever: a research station is queued beside the science factories (build places it at their centre), and
        the old labs stay until the new ones are fed. Run 20261121-140755: red and green into site 53 failed 14x,
        retried every hour, and research stood still. Returns a note when it queued one."""
        from .modules import ResearchStation
        if ResearchStation.slot_labs_refusal(self.base):
            return ""                         # the floorplan's labs take the packs by bus: no station elsewhere
        if "underground-belt" in (getattr(self.kn, "unlocked", None) or set()):
            # failures from before belts could cross count for nothing now: those links are laid again with
            # undergrounds. Run 20261125-184025: three pre-logistics failures of red science's links built a third
            # research station (6 labs) beside two that stood already
            for x, y, p in waiting:
                f = self._link_fails.get((x, y)) or {}
                if "research logistics" in str(f.get("reason") or ""):
                    self._link_fails.pop((x, y), None)
        stuck = [(x, y, p) for x, y, p in waiting
                 if (self._link_fails.get((x, y)) or {}).get("n", 0) >= self.LAB_MOVE_FAILS]
        if not stuck or self._minute() - getattr(self, "_labs_moved_at", -1e9) < self.LAB_MOVE_GAP_MIN:
            return ""
        queued = list(self.urgent) + list(getattr(self, "plan_queue", []) or []) \
            + [((self.current or {}).get("goal") or {})]
        if any((g.get("module") or (g.get("with") or {}).get("module")) == "research_station" for g in queued):
            return ""
        self._labs_moved_at = self._minute()
        packs = sorted({p for *_, p in stuck})
        goal = {"do": "build", "module": "research_station", "count": 6,
                "reflex": f"labs: {', '.join(packs)} failed {self.LAB_MOVE_FAILS}+x into the labs: labs beside "
                          "the science"}
        building = [g for g in self.urgent + self.plan_queue + ([self.current["goal"]] if self.current else [])
                    if g.get("module") == "research_station" or (g.get("with") or {}).get("module") == "research_station"]
        if building:
            # run 20261125-214645: stations 21, 24, 25, 38, 47, 50 and 66, ordered by the labs, the rule floor and
            # the overseer at once; most were removed again
            return (f"{', '.join(packs)} failed {self.LAB_MOVE_FAILS}+x into the labs, but a research station is "
                    "already queued: no second one")
        from .agenda import agenda_of
        queued, why = agenda_of(self).propose(self, goal, "labs")
        if not queued:
            return f"{', '.join(packs)} failed {self.LAB_MOVE_FAILS}+x into the labs; no new station: {why}"
        self.log(f"[crew] {', '.join(packs)} link(s) failed {self.LAB_MOVE_FAILS}+ times: a research station "
                 "beside the science factories is queued instead of retrying the same port")
        return (f"{', '.join(packs)} failed {self.LAB_MOVE_FAILS}+x into the labs: a research station beside the "
                "science factories is queued")

    # ------------------------------------------------------- the link router
    # A link is an inserter that loads a belt (beside a source chest, or beside a belt that already carries the
    # item: a tap, so links grow into a shared bus) and the belt to a port. When the plain route fails the
    # router escalates instead of failing the same way every time:
    #   1. the nearest sources, 30 tiles of detour around them
    #   2. every free source and every tap on a belt of the item, 70 tiles of detour
    #   3. the same, but other links' belts may be crossed: those links are taken up, the new belt is laid, and
    #      they are laid again around it; if one of them fits nowhere, everything is put back as it was
    # A belt the game refuses half-way is taken up again (no half-built links narrowing the next try) and its
    # tile is avoided from then on.
    LINK_TAP_COST = 6.0            # a tap shares another link's flow: a chest of its own is preferred
    LINK_SPLIT_COST = 2.0          # a splitter shares half of it: well ahead of an inserter tap, still not a chest
    LINK_RIP_COST = 15.0           # per tile of another link a route crosses by moving that link

    def _lay_link(self, src: tuple[float, float], port: dict, site: Site) -> int:
        """One link from one source chest to a port (with every escalation step). Returns its belt count."""
        rec, _ = self._link_port(port, site, [(src[0], src[1], None)], [], kind="item")
        return rec["belts"]

    def _link_port(self, port: dict, site: Site, srcs: list, taps: list, kind: str = "item",
                 pack: str | None = None) -> tuple[dict, str]:
        """Lay a link into `port`, escalating step by step. Returns (the link, how it was done). Sources and taps
        farther than belts.LINK_REACH are not tried: such a belt is a trunk goal of its own (see trunk)."""
        from . import belts
        far = [q for q in srcs if self._manhattan(q[:2], port["end"]) > belts.LINK_REACH]
        srcs = [q for q in srcs if self._manhattan(q[:2], port["end"]) <= belts.LINK_REACH]
        taps = [q for q in taps if self._manhattan(q[0], port["end"]) <= belts.LINK_REACH]
        if far and not srcs and not taps:
            gap = min(self._manhattan(q[:2], port["end"]) for q in far)
            item = pack or port.get("item")
            fix = ('a research station next to the science factories ({"do": "build", "module": '
                   '"research_station", "count": 6})' if kind == "science" else
                   f'its own goal ({{"do": "trunk", "site": {site.id}, "item": "{item}"}}); a mine, smelter or '
                   "factory of it next to the site is often better")
            raise BuildError(f"the nearest spare {item} source is {gap:.0f} tiles from site {site.id}'s port: a link "
                             f"reaches {belts.LINK_REACH} tiles at most. A far source needs {fix}")
        self._too_long = None
        near = sorted(srcs, key=lambda q: math.dist(q[:2], port["end"]))
        levels = [("", near[:3], [], 30, False),
                  (", a wide detour", near[:8], taps, 70, False),
                  (", moving other links out of the way", near[:8], taps, 70, True)]
        err: BuildError | None = None
        for how, ss, tt, margin, rip in levels:
            if rip and not self.links:
                break
            if not ss and not tt:
                continue
            plan = self._plan_link(port, ss, tt, margin, rip)
            if plan is None:
                continue
            try:
                rec = self._lay_plan(plan, port, site, kind, pack)
            except BuildError as exc:      # the game refused a tile: it is avoided from now on, try the next step
                err = exc
                continue
            if plan["rip"]:
                how += f" ({len(plan['rip'])} link(s) moved)"
            if plan["tap"]:
                how += ", tapping a belt that already carries it"
            return rec, how
        if err is not None:
            raise BuildError(f"{str(err)[:160]}; {self._walls_note(port, site)}")
        if getattr(self, "_too_long", None):
            raise BuildError(f"the only belt routes from the sources run {self._too_long} tiles, more than a link's "
                             f"{belts.LINK_ROUTE_MAX}: build a source next to site {site.id}, or a trunk goal "
                             f'({{"do": "trunk", "site": {site.id}, "item": "{pack or port.get("item")}"}})')
        ug = "underground-belt" in self.kn.unlocked
        raise BuildError("no belt route from the source to the port even with a wide detour, taps on existing "
                         "belts and moving other links (" + self._walls_note(port, site)
                         + ("" if ug else "; underground belts would help: research logistics") + ")")

    LINE_TAPS = 6                  # inserter taps one line may lend to (its tail feeds one link head-on)

    def _split_spots(self, t, owner: dict) -> list[dict]:
        """Where a splitter can be cut into the belt at tile t of a line or link (owner) for a new branch: t sits
        in a straight run (the belt behind and the belt ahead face the same way, so the splitter's input and its
        main output stay on the owner's belt), and beside it, on the left or right, the splitter's second tile
        and the branch's first tile ahead of it are free, with no belt behind that second tile (it would feed the
        splitter). Each spot: {t, side, side_behind, start, dir, behind, ahead, centre}."""
        from . import belts
        t = (int(t[0]), int(t[1]))
        if owner.get("kind") == "trunk":
            return []
        if "tiles" in owner and "facing" in owner:                   # a site's line, in flow order
            tiles = [tuple(q) for q in owner["tiles"]]
            plain = set(tiles)
        else:                                                        # a link: its plain belts only
            steps = owner.get("steps") or []
            tiles = [tuple(st[1]) for st in steps]
            plain = {tuple(st[1]) for st in steps if st[0] == "belt"}
        if t not in tiles or list(t) in owner.get("splitters", []):
            return []
        i = tiles.index(t)
        if i == 0 or i >= len(tiles) - 1:
            return []
        behind, ahead = tiles[i - 1], tiles[i + 1]
        v = (t[0] - behind[0], t[1] - behind[1])
        # three plain belts in a straight run, in the owner's own flow order
        if (ahead[0] - t[0], ahead[1] - t[1]) != v or not {behind, t, ahead} <= plain:
            return []
        d = next((k for k, q in belts.VEC.items() if q == v), None)
        if d is None:
            return []
        vx, vy = v
        belt_at = getattr(self.w, "belt_at", lambda q: False)
        out = []
        for turn in (belts.LEFT[d], belts.RIGHT[d]):
            sx, sy = belts.VEC[turn]
            side = (t[0] + sx, t[1] + sy)
            side_behind, start = (side[0] - vx, side[1] - vy), (side[0] + vx, side[1] + vy)
            if not (self._belt_free(side) and self._belt_free(start)) or belt_at(side_behind):
                continue
            centre = ((t[0] + side[0]) / 2 + 0.5, (t[1] + side[1]) / 2 + 0.5)
            out.append({"t": t, "side": side, "side_behind": side_behind, "start": start, "dir": d,
                        "behind": behind, "ahead": ahead, "centre": centre})
        return out

    @staticmethod
    def _owner_lists(crew, owner) -> list:
        """The entity lists the owner's belt is recorded in: a link's own and its site's, or a line's site's."""
        lists = []
        if isinstance(owner, dict) and "entities" in owner:
            lists.append(owner["entities"])
            site = crew.base.get(owner.get("to_site"))
            if site is not None:
                lists.append(site.entities)
        else:
            site = next((s for s, ln in crew.lines() if ln is owner), None)
            if site is not None:
                lists.append(site.entities)
        return lists

    def _cut_splitter(self, sp: dict, item: str, owner) -> tuple:
        """Take the owner's belt at sp["t"] up and put a splitter in its place (facing the same way, its second
        tile beside it). The splitter belongs to the owner's belt from then on: its main output carries on as
        before, and with the branch gone it passes everything on. Returns the splitter's entity tuple. On a
        refusal the belt goes back and BuildError is raised."""
        from . import belts
        t, d = sp["t"], sp["dir"]
        bx, by = t[0] + 0.5, t[1] + 0.5
        self.w.pickup("transport-belt", bx, by, d)
        try:
            with belts.job_for(self.w, [item], [t, sp["side"]], [sp["behind"], sp["ahead"]]):
                self.w.place("splitter", sp["centre"][0], sp["centre"][1], d, {t, sp["side"]})
        except BuildError:
            with belts.job_for(self.w, [item], [t], [sp["behind"], sp["ahead"]]):
                self.w.place("transport-belt", bx, by, d, {t})
            raise
        ent = ("splitter", sp["centre"][0], sp["centre"][1], d)
        for lst in self._owner_lists(self, owner):
            for k, e in enumerate(lst):
                if e[0] == "transport-belt" and (math.floor(e[1]), math.floor(e[2])) == t:
                    lst[k] = ent
                    break
        owner.setdefault("splitters", []).append(list(t))
        return ent

    def _uncut_splitter(self, sp: dict, ent: tuple, item: str, owner) -> None:
        """Undo _cut_splitter: the splitter up, the owner's belt back on its tile and in its lists."""
        from . import belts
        t, d = sp["t"], sp["dir"]
        belt = ("transport-belt", t[0] + 0.5, t[1] + 0.5, d)
        try:
            self.w.pickup(*ent)
            with belts.job_for(self.w, [item], [t], [sp["behind"], sp["ahead"]]):
                self.w.place("transport-belt", belt[1], belt[2], d, {t})
        except BuildError as exc:
            self.foreman.note(f"the splitter at {t} could not be swapped back for its belt: {str(exc)[:80]}")
            return
        for lst in self._owner_lists(self, owner):
            for k, e in enumerate(lst):
                if e == ent:
                    lst[k] = belt
        if list(t) in owner.get("splitters", []):
            owner["splitters"].remove(list(t))

    def _belt_taps(self, item: str, consumer: Site | None,
                   need: float = 0.0) -> list[tuple[tuple[int, int], dict | Site]]:
        """Belt tiles that already carry `item` and may lend some of it to another port: other links of the
        item, and (for coal) electric-mine fuel belts whose coal is linked. At most two taps per belt; a coal
        belt only while its chest gets the coal for one more port (`need` a minute)."""
        out = []
        budget = self._budget(item)
        for ln in self.links:
            if (ln["item"] != item or ln.get("to_site") == getattr(consumer, "id", None) or ln.get("taps", 0) >= 2
                    or ln.get("boost_into") is not None or ln.get("kind") == "trunk"):
                continue
            if not self._fits(budget, self._coal_root(ln), need, item):
                continue
            out += [(s[1], ln) for s in ln.get("steps", [])[:-1] if s[0] == "belt"]
        for site, ln in self.lines(item):             # a line lends an inserter's worth to each tap
            if site is consumer or ln.get("taps", 0) >= self.LINE_TAPS:
                continue
            if not self._fits(budget, tuple(ln["source"]), need, item):
                continue
            out += [(t, ln) for t in self.line_taps(site, ln)]
        if item == "coal":
            for s in self.base.of("electric_mine"):
                if s is consumer or s.params.get("fuel") != "belt" or not any(
                        pt.get("fed") for pt in s.params.get("ports", [])):
                    continue
                feed = next((q for q in self.links if q.get("to_site") == s.id and q["item"] == "coal"
                             and q["port"].get("fuel") and q.get("boost_into") is None), None)
                if feed is not None and not self._fits(budget, self._coal_root(feed), need, item):
                    continue
                out += [((math.floor(x), math.floor(y)), s) for name, x, y, _ in s.entities if name == "transport-belt"]
        return [(t, owner) for t, owner in out if self._may_tap(t, item)]

    @staticmethod
    def _line_drops(site: Site, ln: dict) -> int:
        """The index (in flow order) of the last tile of a line its own inserters drop plates onto; -1: none
        found."""
        from . import belts
        idx = {tuple(t): i for i, t in enumerate(ln.get("tiles") or [])}
        last = -1
        for name, x, y, d in site.entities:
            if not name.endswith("inserter") or d not in belts.VEC:
                continue
            dx, dy = belts.VEC[d]                     # an inserter faces where it picks up: it drops behind it
            k = 2 if "long-handed" in name else 1
            drop = (math.floor(x) - k * dx, math.floor(y) - k * dy)
            if drop in idx:
                last = max(last, idx[drop])
        return last

    def line_taps(self, site: Site, ln: dict) -> list[tuple[int, int]]:
        """The tiles of a line a tap's inserter may pick from: only at or past the last furnace that drops onto
        it, where the whole row's plates go by. A tile upstream sees only the furnaces before it (run
        20261125-214645: three copper links picked from the first tile of site 19's line, where one furnace of
        six drops; the other five backed up while the factories went short). The tail is left to the link that
        runs on from it."""
        tiles = [tuple(t) for t in ln.get("tiles") or []]
        last = self._line_drops(site, ln)
        if last < 0:
            last = len(tiles) * 2 // 3               # no inserters known: the last third
        return tiles[last:-1] or tiles[-1:]

    def starved_taps(self) -> list[dict]:
        """Links whose inserter picks from a line upstream of its last furnace (laid before line_taps)."""
        from . import belts
        out = []
        for site, ln in self.lines():
            tiles = [tuple(t) for t in ln.get("tiles") or []]
            idx = {t: i for i, t in enumerate(tiles)}
            last = self._line_drops(site, ln)
            if last <= 0:
                continue
            for rec in self.links:
                ins = rec.get("ins")
                if rec.get("item") != ln.get("item") or not ins or not ins[0] or ins[1] not in belts.VEC:
                    continue
                dx, dy = belts.VEC[ins[1]]
                pick = (math.floor(ins[0][0]) + dx, math.floor(ins[0][1]) + dy)
                if pick in idx and idx[pick] < last:
                    out.append({"link": rec, "site": site, "line": ln, "at": idx[pick], "last": last,
                                "of": len(tiles)})
        return out

    def _may_tap(self, t: tuple[int, int], item: str) -> bool:
        """Does the belt on tile t carry `item`? Not when it is another item's line, when its tag says it carries
        something else, or when a purge found a tap there feeding the wrong items. Run 20261121-140755: an
        electric mine's belts all counted as its coal belt, so a coal link tapped the copper-plate line beside the
        fuel belt; its inserter lifted copper onto site 83's coal belt, and six purges cleaned it in vain."""
        t = tuple(t)
        if t in (getattr(self, "_bad_taps", None) or set()):
            return False
        if any(t in {tuple(q) for q in ln.get("tiles", [])} for _, ln in self.lines() if ln.get("item") != item):
            return False
        e = (getattr(self.w, "entities", None) or {}).get((t[0] + 0.5, t[1] + 0.5)) or {}
        carries = e.get("carries")
        return not carries or item in carries

    def _plan_link(self, port: dict, srcs: list, taps: list, margin: int, rip: bool,
                   block: set = frozenset(), unblock: set = frozenset(), reach: bool = True) -> dict | None:
        """The cheapest route into the port from any of the sources or taps (None: there is none). block and
        unblock plan against a base that does not stand yet: tiles a planned belt will take, tiles a link that
        is to be moved will give up. With reach (a new link): only sources within belts.LINK_REACH, only routes
        up to LINK_ROUTE_MAX (longer belts are trunk goals)."""
        from . import belts
        end, appr = tuple(port["end"]), tuple(port["approach"])
        if reach:
            srcs = [q for q in srcs if self._manhattan(q[:2], end) <= belts.LINK_REACH]
            taps = [q for q in taps if self._manhattan(q[0], end) <= belts.LINK_REACH]
        others = {tuple(t) for s, pt in self.all_ports() if pt is not port for t in (pt["end"], pt["approach"])}
        mine = {end, appr}
        cands = [((math.floor(x), math.floor(y)), (x, y), f"site {s.id}" if s is not None else f"({x:.0f}, {y:.0f})",
                  0.0, None) for x, y, s in srcs]
        taps = sorted(taps, key=lambda q: math.dist(q[0], appr))[:40]
        cands += [(t, t, "a tap on a belt of it", self.LINK_TAP_COST, owner) for t, owner in taps]
        if not cands:
            return None
        xs, ys = [c[0][0] for c in cands] + [appr[0]], [c[0][1] for c in cands] + [appr[1]]
        half = min(80, int(max(max(xs) - min(xs), max(ys) - min(ys)) / 2) + 10)
        self.w.survey_area((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, half=half, step=32)
        bad = self.bad_tiles()
        resting = self._resting_starts()               # starts whose links kept rolling back (_note_rollback)
        heads = {tuple(l["start"]) for _, l in self.lines()}   # a tap's inserter never takes a line's head-on start
        starts: dict = {}
        for c, key, label, cost, owner in cands:
            ln = self.line_at(key) if owner is None else None
            if ln is not None:                         # a line's tail: its belt runs on into the link, no inserter
                st = tuple(ln["start"])
                if (st in unblock or self._belt_free(st)) and st not in (others | mine | bad | set(block) | resting):
                    starts[st] = (cost - 1.0, None, None, key, label + "'s line", None)
                continue
            for dx, dy in belts.VEC.values():          # the inserter beside the source, the belt starts beyond it
                ins, start = (c[0] + dx, c[1] + dy), (c[0] + 2 * dx, c[1] + 2 * dy)
                if not all(t in unblock or self._belt_free(t) for t in (ins, start)) \
                        or {ins, start} & (others | mine | bad | set(block) | heads) or ins in resting:
                    continue
                facing = next(k for k, v in belts.VEC.items() if v == (-dx, -dy))   # it picks up from the source
                if start not in starts or cost < starts[start][0]:
                    starts[start] = (cost, ins, facing, key, label, owner)
        splits: dict = {}                             # a splitter cut into a belt of it: half its flow, not an
        if "splitter" in (getattr(self.kn, "unlocked", None) or set()):   # inserter's 50 a minute
            for c, key, label, cost, owner in cands:
                if not isinstance(owner, dict):
                    continue
                for sp in self._split_spots(c, owner):
                    st = sp["start"]
                    if {st, sp["side"], sp["side_behind"]} & (others | mine | bad | set(block)) or st in resting:
                        continue
                    if st not in starts or self.LINK_SPLIT_COST < starts[st][0]:
                        starts[st] = (self.LINK_SPLIT_COST, None, None, key, "a splitter on a belt of it", owner)
                        splits[st] = sp
        if not starts:
            return None
        split_side = {sp["side"] for sp in splits.values()}
        soft, owner_of = {}, {}
        if rip:                                       # another link may be crossed by moving it (see _movable)
            for ln in self.links:
                if ln.get("port") is port or not self._movable(ln):
                    continue
                for name, x, y, d in ln.get("entities", []):
                    for t in self.w.footprint(name, x, y, d):
                        if t not in others and t not in mine:
                            soft[t], owner_of[t] = self.LINK_RIP_COST, ln
        free = lambda t: (t in mine or t in unblock or self._belt_free(t)) and t not in bad and t not in block \
            and t not in split_side
        ug = "underground-belt" in self.kn.unlocked
        skip: set = set()
        for _ in range(4):
            opts = {st: v[0] for st, v in starts.items() if st not in skip}
            if not opts:
                return None
            steps = belts.route_belt(free, opts, appr, end, avoid=others, underground=ug, margin=margin, soft=soft,
                                     ug_block=self.ug_cover() if ug else None)
            if not steps:
                return None
            if reach and belts.route_len(steps) > belts.LINK_ROUTE_MAX:
                self._too_long = belts.route_len(steps)  # a detour this long is a trunk's job, not a link's
                return None
            cost, ins, facing, key, label, owner = starts[steps[0][1]]
            tiles = set(belts.tiles_of(steps))
            if ins is not None and (ins in tiles or ins in soft):   # over its own inserter's tile: another start
                skip.add(steps[0][1])
                continue
            if ins is not None and not self._can_power(ins, avoid=others | tiles | {ins}):
                skip.add(steps[0][1])                 # an inserter no pole can reach: another side of the source
                continue
            moved = []
            for t in tiles:
                ln = owner_of.get(t)
                if ln is not None and ln not in moved:
                    moved.append(ln)
            if len(moved) > 1:                        # moving one link is a repair; moving several is churn
                return None
            split = splits.get(steps[0][1]) if cost == self.LINK_SPLIT_COST else None
            if split is not None and len(steps) > 1 and steps[1][1] in (split["side"], split["behind"]):
                skip.add(steps[0][1])                 # it would run back into the splitter: another start
                continue
            plan = {"ins": ins, "facing": facing, "steps": steps, "source": key, "from": label,
                    "tap": owner if cost in (self.LINK_TAP_COST, self.LINK_SPLIT_COST) else None, "rip": moved,
                    "relay": None, "split": split}
            if moved:                                 # where the moved link goes, decided before anything is touched
                ln = moved[0]
                taken = tiles | ({ins} if ins is not None else set())
                given = {t for name, x, y, d in ln["entities"] for t in self.w.footprint(name, x, y, d)}
                plan["relay"] = self._plan_relink(ln, block=taken, unblock=given - taken)
                if plan["relay"] is None:
                    return None
            return plan
        return None

    # Moving a link is allowed only when it is cheap and cannot cascade (run 20261119-142858: 948 belts taken up
    # in 100 game minutes, a long coal link moved out of the way, failing to fit, and put back over and over).
    LINK_MOVE_MAX_BELTS = 40
    LINK_MOVE_GAP_MIN = 15.0

    def _movable(self, ln: dict) -> bool:
        """A link that may be moved: short, nothing taps it, and not laid or moved in the last 15 minutes."""
        sealed = getattr(self, "sealed", {})
        src = next((s for x, y, s in self.outputs(ln["item"]) if (x, y) == tuple(ln["source"])), None) \
            if sealed else None
        if ln.get("to_site") in sealed or (src is not None and src.id in sealed) or ln.get("kind") == "trunk":
            return False                              # a sealed site's belts (and trunks) stay where they are
        return (ln.get("belts", 0) <= self.LINK_MOVE_MAX_BELTS and not ln.get("taps")
                and ln.get("boost_into") is None and self._minute() - ln.get("minute", -1e9) >= self.LINK_MOVE_GAP_MIN)

    def _plan_relink(self, ln: dict, block: set, unblock: set) -> dict | None:
        """The route a link to be moved will take, from its own source into its own port, around `block`."""
        if ln.get("tap_of") is not None or ln.get("tap_site") is not None:
            owner = ln.get("tap_of") or self.base.get(ln["tap_site"])
            srcs, taps = [], [(tuple(ln["source"]), owner)]
        else:
            srcs, taps = [(ln["source"][0], ln["source"][1], None)], []
        return self._plan_link(ln["port"], srcs, taps, 70, False, block=block, unblock=unblock,
                               reach=False)           # it stands already: moving it adds no length to cap

    def _lay_plan(self, plan: dict, port: dict, site: Site, kind: str, pack: str | None) -> dict:
        """Build a planned link. A link in its way (at most one, its new route already planned) is taken up
        first and laid along that route afterwards; if the game refuses that, the new link is taken up and the
        moved one goes back exactly where it was."""
        moved = list(plan["rip"])
        for ln in moved:
            self._unlink(ln)
        try:
            rec = self._build_link(plan, port, site, kind, pack)
        except BuildError:
            for ln in moved:
                self._restore(ln)
            raise
        for ln in moved:
            try:
                new = self._build_link(plan["relay"], ln["port"], self.base.get(ln["to_site"]), ln["kind"],
                                       ln["item"] if ln["kind"] == "science" else None)
                new["minute"] = self._minute()           # just moved: not moved again for a while
            except BuildError as exc:
                self._unlink(rec)
                self._restore(ln)
                raise BuildError(f"the link of {ln['item']} into site {ln['to_site']} that was in the way could "
                                 f"not be laid again ({str(exc)[:100]}); it was put back as it was") from None
        return rec

    # Every rolled-back link must change the next plan. A tile the game refused does (it is avoided from then on);
    # any other failure (no pole, a walk that failed, a refusal off the route) would be planned again the same
    # way, so such rollbacks are counted per start, and a start that rolled back ROLLBACK_LIMIT times is not
    # used for ROLLBACK_REST_MIN. Run 20261121-140755: one start was laid and taken up 56 times in two hours.
    ROLLBACK_LIMIT = 2
    ROLLBACK_REST_MIN = 60.0

    def _note_rollback(self, plan: dict, port: dict, exc: BuildError) -> None:
        start = tuple(plan["ins"] if plan.get("ins") is not None else plan["steps"][0][1])
        counts = getattr(self, "_rollbacks", None)
        if counts is None:                            # a crew from an older save has neither
            counts = self._rollbacks = {}
        n = counts[start] = counts.get(start, 0) + 1
        if n >= self.ROLLBACK_LIMIT:
            rested = getattr(self, "_rested_starts", None)
            if rested is None:
                rested = self._rested_starts = {}
            rested[start] = self._minute()
            counts.pop(start, None)
            self.foreman.note(f"build loop stopped: the {port.get('pack') or port.get('item')} link from "
                              f"{plan.get('from')} was laid and taken up {n}x ({str(exc)[:90]}); its start at "
                              f"{start} rests for {self.ROLLBACK_REST_MIN:.0f} min")

    def _resting_starts(self) -> set:
        rested = getattr(self, "_rested_starts", None) or {}
        now = self._minute()
        return {t for t, m in rested.items() if now - m < self.ROLLBACK_REST_MIN}

    def link_tail(self, plan: dict) -> tuple[int, int] | None:
        """The belt a link with no inserter runs on from: its line's tail, or an outpost's head for a trunk
        (None: neither)."""
        if plan.get("kind") == "trunk" and plan.get("source") is not None:
            return tuple(int(v) for v in plan["source"])
        ln = self.line_at(plan.get("source") or ())
        return tuple(ln["tail"]) if ln is not None and ln.get("tail") is not None else None

    # A tile the game refused a link's belt on is avoided, but not for good. Run 20261122-133537: refusals caused
    # by the bot itself (a wrongly declared join) and passing ones (cannot_place twice on an open tile) banned the
    # start of 7 plate lines and both ends of the circuit factory's iron port for the rest of the run, so no link
    # could ever be laid into it. A banned tile is tried again after BAD_BELT_REST_MIN.
    BAD_BELT_REST_MIN = 30.0

    def ban_tile(self, t) -> None:
        t = (int(t[0]), int(t[1]))
        if t in self.line_heads():
            # a line's start is the one tile its plates can leave by: a refusal there (the character in the way,
            # an item on the ground) is never banned (the zones test, seed 626262: the iron line's start was the only
            # banned tile, and every iron link and factory design was refused for it)
            self.log(f"[crew] belt refused on {t}, the start of a line: not banned, tried again next time")
            return
        self._bad_belt.add(t)
        at = getattr(self, "_bad_belt_at", None)
        if not isinstance(at, dict):
            at = self._bad_belt_at = {}
        at[t] = self._minute()

    def bad_tiles(self) -> set:
        """Tiles a link's belt keeps off now: refused within the last BAD_BELT_REST_MIN. A ban with no time yet
        (an older save, or code that adds to the set directly) rests from the first time it is seen here."""
        at = getattr(self, "_bad_belt_at", None)
        if not isinstance(at, dict):
            at = self._bad_belt_at = {}
        now = self._minute()
        for t in list(self._bad_belt):
            if now - at.setdefault(t, now) >= self.BAD_BELT_REST_MIN:
                self._bad_belt.discard(t)
                at.pop(t, None)
        return self._bad_belt

    def _build_link(self, plan: dict, port: dict, site: Site, kind: str, pack: str | None,
                    extra: dict | None = None) -> dict:
        from . import belts
        steps, ins, facing = plan["steps"], plan["ins"], plan["facing"]
        others = {tuple(t) for s, pt in self.all_ports() if pt is not port for t in (pt["end"], pt["approach"])}
        need = belts.count(steps)
        # the inserter's power is worked out before anything is crafted or laid. Run 20261121-140755: the green
        # science link into site 53 laid its whole 45-tile belt, found no room for the inserter's pole, took it
        # all up again, and did that 56 times in two hours (every port, every escalation level, every goal).
        power = (self._pole_plan(ins, avoid=others | set(belts.tiles_of(steps)) | {ins})
                 if ins is not None else None)
        ins_kind = "fast-inserter" if "fast-inserter" in self.kn.unlocked else "inserter"
        lift = {ins_kind: 1} if ins is not None else {}  # a line's own belt runs on: nothing lifts the items
        split = plan.get("split")
        if split is not None:
            lift = {"splitter": 1}
        self.unlock_items([k for k, v in need.items() if v] + list(lift))
        self.make_for({**{k: v for k, v in need.items() if v}, **lift})
        ents: list = []
        cut = None
        if split is not None:                         # the splitter first: the branch runs on from its output
            cut = self._cut_splitter(split, pack or port["item"], plan.get("tap"))
        self.w.recorder = ents
        try:
            if ins is not None:
                self.w.place(ins_kind, ins[0] + 0.5, ins[1] + 0.5, facing, {ins})
            joins = belts.link_joins(steps, from_belt=ins is None,
                                     tail=self.link_tail(plan) if ins is None and split is None else None)
            if split is not None:                     # fed by the splitter (its tile is the side or the cut one)
                joins = joins[:1] + [split["side"], split["t"]]
            with belts.job_for(self.w, [pack or port["item"]], belts.tiles_of(steps), joins):
                belts.lay_belt(self.w, steps)
            if ins is not None:
                self._power_tile(ins, avoid=others | set(belts.tiles_of(steps)), plan=power)
        except BuildError as exc:
            self.w.recorder = None
            bad = getattr(exc, "tile", None)
            if bad is not None:
                self.ban_tile(bad)
            self._undo(ents)
            if cut is not None:                       # the owner's belt as it was: splitter out, belt back
                self._uncut_splitter(split, cut, pack or port["item"], plan.get("tap"))
            if ents and bad is None:
                self._note_rollback(plan, port, exc)
            raise
        finally:
            self.w.recorder = None
        rec = {"source": plan["source"], "to_site": site.id, "item": pack or port["item"], "belts": len(steps),
               "kind": kind, "port": port, "steps": steps, "entities": ents,
               "ins": (ins, facing, ins_kind) if ins is not None else None,
               "from": plan["from"], "minute": self._minute(), **(extra or {})}
        if isinstance(plan.get("tap"), dict):
            rec["tap_of"] = plan["tap"]
        elif plan.get("tap") is not None:             # an electric mine's fuel belt
            rec["tap_site"] = plan["tap"].id
        self._register(rec)
        return rec

    def _register(self, rec: dict) -> None:
        site, port = self.base.get(rec["to_site"]), rec["port"]
        self.links.append(rec)
        for held in (rec.get("tap_of"), rec.get("boost_into")):   # a belt others draw on or feed stays put
            if isinstance(held, dict):
                held["taps"] = held.get("taps", 0) + 1
        if site is not None:
            site.entities.extend(rec["entities"])
        if rec["kind"] == "science":
            port["pack"] = rec["item"]
            self.science_links[rec["source"]] = {"station": rec["to_site"], "pack": rec["item"],
                                                 "port": port["end"], "belts": rec["belts"]}
            return
        port["fed"] = list(rec["source"])
        for c in (site.cells if site is not None else []):    # the courier stops carrying what the belt brings
            if c.get("port") is port or (c.get("in") and tuple(port.get("chest") or ()) in {tuple(q) for q in c["in"]}):
                if rec["item"] not in c.setdefault("belted", []):
                    c["belted"].append(rec["item"])

    # A link can stop carrying without anyone taking it up: a later link's underground paired with one of its
    # undergrounds and turned it round (run 20261119-202811: the coal belt of steam site 21 ran into the back of
    # a flipped exit for 60 minutes while the bot counted the boiler as belted), or a piece went missing. The
    # audit compares what stands with what was planned and takes a broken link up, so the link step lays it
    # again (around what broke it) and the foreman hand-feeds a dry boiler meanwhile.
    AUDIT_GAP_MIN = 10.0
    AUDIT_RADIUS = 16

    def audit_links(self) -> list[str]:
        """Take up every link whose belts no longer match their plan. Returns a note per link taken up."""
        want: dict[tuple, tuple] = {}
        for ln in self.links:
            placed = {(round(x, 2), round(y, 2)) for name, x, y, d in ln.get("entities", [])
                      if name.endswith(("transport-belt", "underground-belt"))}
            for st in ln.get("steps") or []:
                key = (round(st[1][0] + 0.5, 2), round(st[1][1] + 0.5, 2))
                if key in placed:
                    want[key] = (ln, "underground-belt" if st[0] == "ug" else "transport-belt", st[2],
                                 st[3] if st[0] == "ug" else None)
        notes = self._free_line_heads() + self._retake_starved()
        if not want:
            return notes
        have: dict[tuple, dict] = {}
        todo = set(want)
        while todo:
            cx, cy = min(todo)
            d = self.w.observe(self.AUDIT_RADIUS, x=cx, y=cy, include=["entities"],
                               names=["transport-belt", "underground-belt", "fast-transport-belt",
                                      "fast-underground-belt", "express-transport-belt", "express-underground-belt"])
            for e in d.get("entities") or []:
                have[(round(e["x"], 2), round(e["y"], 2))] = e
            reach = min(self.AUDIT_RADIUS, int(d.get("radius") or self.AUDIT_RADIUS)) - 1
            todo -= {k for k in todo if math.dist(k, (cx, cy)) <= reach} | {(cx, cy)}
        broken: list[tuple] = []
        for key, (ln, name, facing, typ) in want.items():
            e = have.get(key)
            if e is not None and e["name"].endswith(name) and e.get("direction") == facing                     and (typ is None or e.get("belt_type") == typ):
                continue
            if not any(b[0] is ln for b in broken):
                what = "missing" if e is None else f"{e.get('belt_type') or ''} facing {e.get('direction')}".strip()
                broken.append((ln, key, what, name, facing, typ))
        for ln, key, what, name, facing, typ in broken:
            todo_ln, gone = [ln], []
            while todo_ln:                            # and what hangs off it: it would carry nothing either
                q = todo_ln.pop()
                if any(q is g for g in gone) or not any(q is l for l in self.links):
                    continue
                gone.append(q)
                todo_ln += [h for h in self.links if h.get("tap_of") is q or h.get("boost_into") is q]
            for q in reversed(gone):
                self._unlink(q)
                self._link_fails.pop(tuple(q["port"]["end"]), None)
            notes.append(f"the {ln['item']} link into site {ln['to_site']} was broken at {key} ({name} "
                         f"{'wanted ' + (typ + ' ' if typ else '') + 'facing ' + facing}, found {what}): taken up "
                         f"({len(gone)} link(s)), to be laid again")
        return notes

    def _free_line_heads(self) -> list[str]:
        """A pole of ours on a line's start blocks the belt its plates run on from: a pole goes on a free tile next
        to it first (within wire reach of the poles it was wired to), then it comes up and the power nearby is
        checked. Once per line. Run 20261125-214645: site 19's own pole stood on its start; its copper had no
        way out but taps upstream of five of six furnaces, and copper plates ran at 19 a minute."""
        tried = self.__dict__.setdefault("_heads_freed", set())
        notes = []
        heads = self.line_heads()
        tile = lambda e: (math.floor(e[1]), math.floor(e[2]))
        for site, ln in self.lines():
            st = tuple(ln["start"])
            if (site.id, st) in tried:
                continue
            hit = next(((s, e) for s in self.base.sites for e in s.entities if e[0] in POLES and tile(e) == st), None)
            if hit is None:
                continue
            tried.add((site.id, st))
            owner, e = hit
            near = [q for q in self.existing_poles() if q != st and math.dist(q, st) <= self.WIRE_REACH]
            line = {tuple(t) for _, l in self.lines() for t in l.get("tiles") or []}
            ok = self._pole_freer()
            spots = sorted(((st[0] + dx, st[1] + dy) for dx in (-2, -1, 0, 1, 2) for dy in (-2, -1, 0, 1, 2)
                            if (dx, dy) != (0, 0)), key=lambda q: math.dist(q, st))
            spot = next((q for q in spots if q not in heads and q not in line and ok(q) and self.w.free({q})
                         and all(math.dist(q, n) <= self.WIRE_REACH - 0.5 for n in near)), None)
            if spot is None:
                notes.append(f"site {owner.id}'s pole stands on the start of site {site.id}'s {ln['item']} line and "
                             "no tile next to it keeps its wires: left standing")
                continue
            try:
                self.make({e[0]: 1}, extra=True)
                self.w.place(e[0], spot[0] + 0.5, spot[1] + 0.5, "north", {spot})
            except BuildError as exc:
                notes.append(f"the pole on site {site.id}'s line start could not be moved: {str(exc)[:80]}")
                continue
            new = (e[0], spot[0] + 0.5, spot[1] + 0.5, "north")
            owner.entities.append(new)
            owner.poles.append(spot)
            try:
                if self.w.pickup(*e):
                    owner.entities.remove(e)
                    owner.poles = [q for q in owner.poles if tuple(q) != st]
            except BuildError as exc:
                notes.append(f"the pole on site {site.id}'s line start stays (could not be picked up: "
                             f"{str(exc)[:80]})")
                continue
            self._recheck_power_near([(e[1], e[2]), (new[1], new[2])])
            notes.append(f"moved site {owner.id}'s pole off the start of site {site.id}'s {ln['item']} line to "
                         f"{spot}: its plates can run on into a link now")
        return notes

    CREW_BOX = "wooden-chest"      # small (16 stacks): it fills from what passes, then takes nothing more

    def crew_boxes(self) -> list[str]:
        """A crew box on every plate line: an inserter past the line's last furnace into a small chest the crew
        takes construction plates from (Crew._plates_loop looks there first), instead of mining ore and smelting
        by hand while the plates ride belts out of its reach (the game lets it pick up from chests and machines,
        never from a belt). Kept apart from the site's chests, so no link ever starts there. Once per line."""
        from . import belts
        notes = []
        done = self.__dict__.setdefault("_boxed_lines", {})
        heads = self.line_heads()
        for site, ln in self.lines():
            item = ln.get("item")
            if item not in PLATES or site.id in done or (site.params.get("crew_boxes") or {}).get(item):
                continue
            done[site.id] = self._minute()
            spot = None
            taps = list(reversed(self.line_taps(site, ln)))
            # past the last furnace first (the whole row's plates); else anywhere on the line: a full box takes
            # nothing more, so upstream it costs the row only its first 16 stacks (the zones test, seed 737373:
            # three lines had one or two tiles past their last furnace, all taken by links' inserters)
            rest = [tuple(t) for t in reversed(ln.get("tiles") or []) if tuple(t) not in taps]
            for t in taps + rest:
                for dx, dy in belts.VEC.values():
                    ins, box = (t[0] + dx, t[1] + dy), (t[0] + 2 * dx, t[1] + 2 * dy)
                    if {ins, box} & heads or not (self.w.free({ins}) and self.w.free({box})):
                        continue
                    spot = (t, ins, box, next(k for k, v in belts.VEC.items() if v == (-dx, -dy)))
                    break
                if spot:
                    break
            if spot is None:
                notes.append(f"site {site.id}'s {item} line has no room for a crew box")
                continue
            t, ins, box, facing = spot
            try:
                self.make({"inserter": 1, self.CREW_BOX: 1}, extra=True)
                rec: list = []
                self.w.recorder = rec
                try:
                    self.w.place("inserter", ins[0] + 0.5, ins[1] + 0.5, facing, {ins})
                    self.w.place(self.CREW_BOX, box[0] + 0.5, box[1] + 0.5, "north", {box})
                finally:
                    self.w.recorder = None
                site.entities += rec
                site.params.setdefault("crew_boxes", {}).setdefault(item, []).append([box[0] + 0.5, box[1] + 0.5])
                self._power_tile(ins)
                notes.append(f"crew box on site {site.id}'s {item} line at {box}: construction plates from it, "
                             "not by hand")
            except BuildError as exc:
                notes.append(f"crew box on site {site.id}'s {item} line not built: {str(exc)[:100]}")
        return notes

    def _line_open(self, site: Site, ln: dict) -> bool:
        """Can a link take this line's whole flow: its start free for a belt, or a tap past its last furnace with
        room for an inserter and the belt beyond?"""
        from . import belts
        if self._belt_free(tuple(ln["start"])):
            return True
        heads = self.line_heads()
        for t in self.line_taps(site, ln):
            for dx, dy in belts.VEC.values():
                ins, start = (t[0] + dx, t[1] + dy), (t[0] + 2 * dx, t[1] + 2 * dy)
                if ins not in heads and start not in heads and self._belt_free(ins) and self._belt_free(start):
                    return True
        return False

    def _retake_starved(self) -> list[str]:
        """Take up (once each) a link that picks from a line where only its first furnaces drop: laid again, it
        taps the line past its last furnace, or a belt that carries the whole row."""
        done = self.__dict__.setdefault("_starved_retaken", set())
        notes = []
        for q in self.starved_taps():
            rec = q["link"]
            key = tuple(rec["port"]["end"])
            if key in done or not any(rec is l for l in self.links):
                continue
            if not self._line_open(q["site"], q["line"]):
                continue                              # nowhere better yet: a starved tap beats none (tried again later)
            done.add(key)
            hang = [h for h in self.links if h.get("tap_of") is rec or h.get("boost_into") is rec]
            for h in hang + [rec]:
                self._unlink(h)
                self._link_fails.pop(tuple(h["port"]["end"]), None)
            notes.append(f"the {rec['item']} link into site {rec['to_site']} picked from tile {q['at'] + 1} of "
                         f"{q['of']} of site {q['site'].id}'s line, where only the first furnaces drop (the last "
                         f"drops on tile {q['last'] + 1}): taken up, to be laid again past the last furnace")
        return notes

    def _unregister(self, rec: dict) -> None:
        site, port = self.base.get(rec["to_site"]), rec["port"]
        if rec in self.links:
            self.links.remove(rec)
        for held in (rec.get("tap_of"), rec.get("boost_into")):
            if isinstance(held, dict):
                held["taps"] = max(0, held.get("taps", 1) - 1)
        if site is not None:
            gone = set(rec["entities"])
            site.entities[:] = [e for e in site.entities if e not in gone]
        if rec["kind"] == "science":
            port["pack"] = None
            self.science_links.pop(rec["source"], None)
            return
        port["fed"] = None
        for c in (site.cells if site is not None else []):
            if rec["item"] in c.get("belted", []) and (c.get("port") is port or c.get("in")):
                c["belted"].remove(rec["item"])

    def _undo(self, ents: list) -> None:
        for name, x, y, d in reversed(ents):
            try:
                self.w.pickup(name, x, y, d)
            except BuildError:
                pass

    def _unlink(self, rec: dict) -> None:
        """Take a link up (its inserter, belts and poles back into the inventory) and forget it. A pole of the link
        that another site's power runs through stays standing and becomes that site's (run 20261122-133537: a
        relink of site 15 took up a pole the 75/min iron line of site 19 was wired over; it stood dark for 25
        game minutes). Sites near the poles that did come up are checked against the game's network after."""
        self._unregister(rec)
        poles = [e for e in rec["entities"] if e[0] in POLES]
        try:
            keep = self._pole_bridges(poles, rec.get("to_site"))
        except Exception as exc:  # noqa: BLE001 - a bookkeeping check never stops a link step
            keep, _ = [], self.log(f"[crew] pole check failed: {str(exc)[:100]}")
        rec["kept_poles"] = keep
        with self.w.step("remove"):
            self._undo([e for e in rec["entities"] if e not in keep])
        taken = [e for e in poles if e not in keep]
        if taken:
            self._recheck_power_near([(e[1], e[2]) for e in taken])

    WIRE_REACH = 7.5               # a small pole's wire (the shortest kind: a cut is never missed)

    def _pole_bridges(self, poles: list, own_site=None) -> list:
        """Of a link's poles about to be taken up, those some other site's power runs through: without them a pole
        of that site loses every wire path (as far as the poles we know go) to the grid's first pole. Returns the
        poles to leave standing, each added to the nearest site it powers."""
        grid = self.base.power_pole
        if grid is None or not poles:
            return []
        tile = lambda e: (math.floor(e[1]), math.floor(e[2]))
        mine = {tile(e): e for e in poles}
        nodes = set(self.existing_poles()) | {tuple(grid)}
        owners = {}
        for s in self.base.sites:
            if str(s.id) == str(own_site):
                continue
            for q in s.poles:
                owners.setdefault(tuple(q), s)
        nodes |= set(owners)

        def reached(ns: set) -> set:
            start = tuple(grid)
            if start not in ns:
                return set()
            seen, stack = {start}, [start]
            while stack:
                p = stack.pop()
                for q in ns:
                    if q not in seen and abs(q[0] - p[0]) <= self.WIRE_REACH and abs(q[1] - p[1]) <= self.WIRE_REACH \
                            and math.dist(p, q) <= self.WIRE_REACH:
                        seen.add(q)
                        stack.append(q)
            return seen

        before = reached(nodes)
        base_ns = nodes - set(mine)
        lost = lambda ns: {q for q in owners if q in before and q not in reached(ns)}
        cut = lost(base_ns)
        if not cut:
            return []
        hurt = {id(owners[q]): owners[q] for q in cut}      # the sites whose power runs through these poles
        keep: list = []
        ns = set(base_ns)
        for t in sorted(mine, key=lambda t: min(math.dist(t, q) for q in cut)):
            ns.add(t)
            keep.append(mine[t])
            cut = lost(ns)
            if not cut:
                break
        for e in keep:                            # it is that site's pole now: removals and routes count it
            t = tile(e)
            s = min(hurt.values(), key=lambda s: min(math.dist(tuple(q), t) for q in s.poles), default=None)
            if s is not None:
                s.poles.append(t)
                s.entities.append(tuple(e))
                self.log(f"[crew] the link's pole at {t} stays: site {s.id}'s power runs through it")
        return keep

    def _recheck_power_near(self, spots: list, radius: float = 10.0) -> None:
        """After poles came up: sites with poles within `radius` of them that the game now puts off the grid are
        marked unpowered and get a connect goal (the foreman's rewire does the same within minutes)."""
        for s in self.base.sites:
            if not s.powered or not s.poles or s.kind == "poles":
                continue
            if not any(math.dist((q[0] + 0.5, q[1] + 0.5), p) <= radius for q in s.poles for p in spots):
                continue
            try:
                og = self.on_grid(s.poles)
            except BuildError:
                continue
            if og is False:
                s.powered = False
                self.foreman.note(f"site {s.id}: cut off from the grid after poles were taken up nearby")
                g = {"do": "connect", "site": s.id, "reflex": "rules: power cut by a pole taken up nearby"}
                from .agenda import agenda_of
                agenda_of(self).propose(self, g, "rules", front=True)

    def _restore(self, rec: dict) -> None:
        """Put a taken-up link back exactly where it was."""
        from . import belts
        ins, facing, ins_kind = rec["ins"] or (None, None, None)     # a trunk has no inserter
        stayed = rec.get("kept_poles") or []           # still standing (another site's power runs through them)
        poles = [e for e in rec["entities"] if e[0] in POLES and e not in stayed]
        need = belts.count(rec["steps"])
        ents: list = []
        try:
            self.make_for({**{k: v for k, v in need.items() if v}, **({ins_kind: 1} if ins_kind else {}),
                           **Counter(e[0] for e in poles)})
            self.w.recorder = ents
            try:
                if ins is not None:
                    self.w.place(ins_kind, ins[0] + 0.5, ins[1] + 0.5, facing, {ins})
                with belts.job_for(self.w, [rec["item"]], belts.tiles_of(rec["steps"]),
                                   belts.link_joins(rec["steps"], from_belt=ins is None,
                                                    tail=self.link_tail(rec) if ins is None else None)):
                    belts.lay_belt(self.w, rec["steps"])
                for name, x, y, d in poles:
                    self.w.place(name, x, y, d, self.w.footprint(name, x, y, d))
            finally:
                self.w.recorder = None
        except BuildError as exc:            # the courier carries for it again until the next link step
            self.foreman.note(f"could not put back the {rec['item']} link into site {rec['to_site']}: {str(exc)[:80]}")
            self._undo(ents)
            return
        self._register({**rec, "entities": ents})

    def _walls_note(self, port: dict, site: Site) -> str:
        """What hems the port in: the sites (and water) on the tiles around where its belt must arrive."""
        appr = tuple(port["approach"])
        owner: dict = {}
        for s in self.base.sites:
            for name, x, y, d in s.entities:
                for t in self.w.footprint(name, x, y, d):
                    owner[t] = s
        count: Counter = Counter()
        for dx in range(-4, 5):
            for dy in range(-4, 5):
                t = (appr[0] + dx, appr[1] + dy)
                if t in owner:
                    count[f"site {owner[t].id} ({owner[t].kind})"] += 1
                elif t in self.w.water:
                    count["water"] += 1
                elif t in self.w.cliffs:
                    count["cliffs"] += 1
        if not count:
            return f"port at {list(appr)} of site {site.id}: open around it, so the sources are what is cut off"
        walls = ", ".join(f"{k}: {v} tiles" for k, v in count.most_common(3))
        return f"around the port at {list(appr)} of site {site.id}: {walls}"

    def ug_cover(self) -> dict:
        """Where a new underground pair may not go (belts.ug_cover): every underground belt we placed."""
        from . import belts
        ugs = {((math.floor(x), math.floor(y)), d) for s in self.base.sites for name, x, y, d in s.entities
               if name == "underground-belt"}
        ugs |= {((math.floor(x), math.floor(y)), d) for ln in self.links for name, x, y, d in ln.get("entities", [])
                if name == "underground-belt"}
        return belts.ug_cover(ugs)

    def all_ports(self) -> list[tuple[Site, dict]]:
        return [(s, pt) for s in self.base.sites for pt in s.params.get("ports", [])]

    def can_obtain(self, item: str, n: int = 1) -> bool:
        """Can the crew hold n of an item now: on hand or in storage, or hand-craftable from what it can get (what
        our cells and factories make counts as got). Engine units, for one, are machine-only."""
        have = self.w.inventory.get(item, 0) + self.stored(item)
        if have >= n:
            return True
        try:
            return not self.kn.kit_plan({item: n - have}, self.w.inventory, supplied=self.ready_items()).blocked
        except Exception:  # noqa: BLE001 - cannot tell: say no rather than fail a build on it
            return False

    def _power_pumps(self, site: Site) -> list[str]:
        """Power every pump on a site's long pipe line (an unpowered pump passes no fluid at all). Returns what
        failed."""
        line = {(int(x - 0.5), int(y - 0.5)) for n, x, y, _ in site.entities if n in ("pipe", "pipe-to-ground", "pump")}
        failed = []
        for t in site.params.get("pumps", []):
            try:
                powered = self.base.powered_poles()
                to = min(powered, key=lambda q: math.dist(q, t), default=None)
                if to is not None and math.dist(to, t) > 20 and hasattr(self.w, "look"):
                    # the pole bridge goes only over tiles seen: look along the way to the nearest powered pole
                    n = max(1, int(math.dist(to, t) // 30))
                    for k in range(n + 1):
                        try:
                            self.w.look(t[0] + (to[0] - t[0]) * k / n, t[1] + (to[1] - t[1]) * k / n, radius=20)
                        except BuildError:
                            pass
                self._power_tile(tuple(t), avoid=line)
            except BuildError as exc:
                failed.append(f"pump at {tuple(t)}: {str(exc)[:80]}")
                self.foreman.note(f"site {site.id}: pump at {tuple(t)} has no power ({str(exc)[:80]})")
        return failed

    def split_pipelines(self) -> list[str]:
        """Pumps on pipe lines laid before the bot knew the 320-tile segment limit (pipes.PIPELINE_EXTENT): each
        site's longest line, if it overextends, gets a pump every pipes.PUMP_EVERY tiles, powered. Run once per site
        (params pumps_checked). Run 20261120-211555: a 460-tile crude line fed the blue science workshop nothing."""
        from . import pipes
        notes = []
        for s in list(self.base.sites):
            if s.params.get("pumps_checked"):
                continue
            ents = [e for e in s.entities if e[0] in ("pipe", "pipe-to-ground")]
            steps = pipes.line_from_entities(ents, s.centre) if len(ents) >= 20 else None
            if not steps or pipes.route_length(steps) <= pipes.PIPELINE_EXTENT - 40:
                s.params["pumps_checked"] = True
                continue
            line = {st[1] for st in steps}
            _, changes = pipes.add_pumps(steps, lambda q: q not in line and not self.w.blocked(q), flow_to_start=True)
            if not changes:
                s.params["pumps_checked"] = True
                notes.append(f"site {s.id}: a {pipes.route_length(steps)}-tile pipe line with no place for a pump")
                continue
            if not self.can_obtain("pump", len(changes)):
                # a pump needs an engine unit, which only a machine makes: tried again once something makes them
                s.params["needs_pumps"] = True
                if not s.params.get("pumps_noted"):
                    s.params["pumps_noted"] = True
                    notes.append(f"site {s.id}: its {pipes.route_length(steps)}-tile pipe line overextends; "
                                 f"{len(changes)} pump(s) go in once engine units are made (no fluid flows till then)")
                continue
            self.unlock_items(["pump"])
            self.make({"pump": len(changes)}, extra=True)
            self.w.recorder = s.entities
            done = 0
            try:
                for c in changes:
                    a, b = c["pump"]
                    if c["move"] is not None:                  # the upstream end of an underground pair moves on
                        old, new, side = c["move"]
                        self.w.pickup("pipe-to-ground", old[0] + 0.5, old[1] + 0.5, side)
                        s.entities[:] = [e for e in s.entities if not (e[0] == "pipe-to-ground" and
                                                                     (int(e[1] - 0.5), int(e[2] - 0.5)) == old)]
                        self.w.place("pipe-to-ground", new[0] + 0.5, new[1] + 0.5, side, {new})
                    else:
                        for q in (a, b):
                            self.w.pickup("pipe", q[0] + 0.5, q[1] + 0.5)
                        s.entities[:] = [e for e in s.entities if not (e[0] == "pipe" and
                                                                     (int(e[1] - 0.5), int(e[2] - 0.5)) in (a, b))]
                    self.w.place("pump", (a[0] + b[0]) / 2 + 0.5, (a[1] + b[1]) / 2 + 0.5, c["facing"], {a, b})
                    s.params.setdefault("pumps", []).append(list(a))
                    done += 1
            finally:
                self.w.recorder = None
            failed = self._power_pumps(s)
            s.params["pumps_checked"] = True
            s.params.pop("needs_pumps", None)
            notes.append(f"site {s.id}: {done} pump(s) on its {pipes.route_length(steps)}-tile pipe line"
                         + (f", {len(failed)} without power" if failed else ""))
        return notes

    # supply reach of each pole kind: a small pole powers the 5x5 around it, a medium one the 7x7
    POLE_SUPPLY = {POLE: 2, MEDIUM_POLE: 3}

    def _pole_plan(self, t: tuple[int, int], avoid: set = frozenset(),
                   half: int = 0) -> list[tuple[str, tuple[int, int]]]:
        """The poles that would power a lone electric building (a link's inserter) at t, as (kind, tile): [] when
        a pole already covers it, else one pole anywhere its supply area reaches t (a small pole up to 2 tiles
        away, a medium one up to 3 once unlocked) and the cheapest bridge of small poles to the network. Places
        nothing; raises BuildError when there is no room. Run 20261121-140755: only the 8 tiles touching the
        inserter were tried, all were taken, and the green science link never got built."""
        poles = self.existing_poles()
        medium = {(math.floor(x), math.floor(y)) for (x, y), e in self.w.entities.items()
                  if e.get("name") == MEDIUM_POLE} if hasattr(self.w, "entities") else set()
        # half: tiles from the building's centre to its edge (a 3x3 drill: 1), so a pole reaching its edge counts
        if any(abs(p[0] - t[0]) <= self.POLE_SUPPLY[MEDIUM_POLE if p in medium else POLE] + half
               and abs(p[1] - t[1]) <= self.POLE_SUPPLY[MEDIUM_POLE if p in medium else POLE] + half for p in poles):
            return []
        kinds = [POLE] + ([MEDIUM_POLE] if MEDIUM_POLE in (getattr(self.kn, "unlocked", None) or set()) else [])
        pole_ok = self._pole_freer()
        free = lambda q: pole_ok(q) and q not in avoid
        anchors = self.base.powered_poles()
        lanes = getattr(self.w, "lanes", None) or set()
        for kind in kinds:                            # a cheap small pole first, a medium one where none fits
            r = self.POLE_SUPPLY[kind] + half
            spots = sorted(((t[0] + dx, t[1] + dy) for dx in range(-r, r + 1) for dy in range(-r, r + 1)),
                           key=lambda q: abs(q[0] - t[0]) + abs(q[1] - t[1]))
            open_spots = [q for q in spots if q != t and q not in avoid and self.w.free({q}) and pole_ok(q)]
            if not open_spots and half:               # a big machine boxed in: a belt lane may take a pole
                open_spots = [q for q in spots if q in lanes and q not in avoid and pole_ok(q)
                              and getattr(self.w, "free_belt", lambda x: False)(q)]
            for spot in open_spots[:3]:               # the nearest few: each bridge search is a full route
                bridge = route_poles([spot], anchors, poles, free, margin=16)
                if bridge is not None:
                    return [(kind, spot)] + [(POLE, q) for q in bridge]
        raise BuildError(f"no room for a pole (or no pole route) to power the link's inserter at {t}")

    def line_heads(self) -> set[tuple[int, int]]:
        """Each line's start and the two tiles beyond it: where its plates run on into a link, belts only."""
        from . import belts
        out = set()
        for _, ln in self.lines():
            st, (dx, dy) = tuple(ln["start"]), belts.VEC.get(ln.get("facing"), (0, 0))
            out |= {(st[0] + k * dx, st[1] + k * dy) for k in range(3)}
        return out

    def _pole_freer(self, allow=frozenset()):
        """A test for tiles a pole may go on (route_poles calls it per tile: the heads are looked up once). Never on
        a line's head (run 20261125-214645: a pole on the start of site 19's copper line left its plates no way
        out but taps upstream of five of its six furnaces). `allow`: reserved tiles this site may use anyway (a
        slot factory's own slot)."""
        heads = self.line_heads()
        z = getattr(self.w, "zones", None)
        if z is not None and z.refused_slots():          # poles may cross the zones' corridors, never their slots
            heads = set(heads) | (z.refused_slots() - set(allow))
        return lambda t: t in self.w.seen and not self.w.blocked(t) and t not in self.w.keepout and t not in heads

    def _own_slot(self, site) -> set:
        """The tiles of the zone slot a slot factory stands in: its own poles may bridge across them (the zones test,
        seed 626262: green science in slot make-3 was built, then 'no pole route from site 25 to the network',
        because the bridge between its pole groups had to stand inside its own slot, which poles kept off)."""
        z = getattr(self.w, "zones", None)
        sid = (getattr(site, "params", None) or {}).get("slot")
        if z is None or not sid:
            return set()
        sl = next((q for q in getattr(z, "slots", []) or [] if q.id == sid), None)
        return set(sl.tiles) if sl is not None else set()

    def _can_power(self, t: tuple[int, int], avoid: set = frozenset()) -> bool:
        try:
            self._pole_plan(t, avoid)
        except BuildError:
            return False
        return True

    def _power_tile(self, t: tuple[int, int], avoid: set = frozenset(),
                    plan: list[tuple[str, tuple[int, int]]] | None = None) -> None:
        """Make sure a lone electric building (a link's inserter) is inside a pole's supply area and that pole is
        on the network (`plan`: poles worked out before anything was laid, see _build_link)."""
        todo = self._pole_plan(t, avoid) if plan is None else plan
        if not todo:
            return
        kit = Counter(kind for kind, _ in todo)
        self.unlock_items(list(kit))
        self.make_for(dict(kit))
        for kind, q in todo:
            self.w.place(kind, q[0] + 0.5, q[1] + 0.5, "north", {q})

    # ======================================================= hand or automate
    def hand_seconds(self, item: str, n: int) -> float | None:
        """Game seconds the character would spend crafting n of an item by hand (None: it cannot)."""
        plan = self.kn.kit_plan({item: n}, {}, extra=True)
        return None if plan.blocked else plan.seconds

    def automation_goal(self, item: str) -> dict | None:
        """The goal that makes `item` automatically, or None while nothing can (no power, no automation, no
        module that makes it). Science gets the road's rate, so the factory serves the whole game."""
        if not self.base.powered_poles() or "automation" not in self.kn.researched:
            return None
        from .roadmap import rate_targets
        rate = float(rate_targets(self).get(item, 10))
        if item in MODULES["factory"].can_make(self.kn):
            # Real automation only: drills, furnaces and belts into assemblers. A workshop is courier-fed (the
            # character carries every plate), so it never stands in for a belt factory; until electric drills
            # are researched, or while its layout keeps failing, nothing automates this item yet.
            belts_failed = self._factory_failed.get(item)
            belts_ok = belts_failed is None or self._minute() - belts_failed >= AUTO_RETRY_MIN
            if belts_ok and "electric-mining-drill" in self.kn.researched:
                return {"do": "build", "module": "factory", "item": item, "per_minute": rate}
            return None
        from .production import Planner
        pl = Planner(self.kn).plan(item, rate)
        if not pl.blocked and (not pl.fluids.get("crude-oil") or self.base.of("pumpjack")):
            return {"do": "build", "module": "workshop", "item": item, "per_minute": rate}
        return None

    def hand_or_automate(self, item: str, n: int) -> tuple[str, dict | None, str]:
        """("auto", None) when a factory makes it already; ("automate", goal) to build one first; ("hand", None)
        only when crafting n by hand costs under HAND_LIMIT_S, or nothing can automate it now (or its factory
        failed to build within the last AUTO_RETRY_MIN game minutes). The third value says why."""
        if item in {what for s in self.base.sites for *_, what in s.chests}:
            return "auto", None, f"{item} is made by our factories"
        secs = self.hand_seconds(item, n)
        if secs is not None and secs <= HAND_LIMIT_S:
            return "hand", None, f"{n} {item} take {secs:.0f} s by hand: cheaper than a factory"
        goal = self.automation_goal(item)
        if goal is None:
            return "hand", None, f"nothing can automate {item} yet (power, automation research or its module)"
        failed = self._auto_failed.get(item)
        if failed is not None and self._minute() - failed < AUTO_RETRY_MIN:
            return "hand", None, f"its factory failed {self._minute() - failed:.0f} min ago: by hand meanwhile"
        took = f"{secs:.0f} s" if secs is not None else "impossible"
        return "automate", goal, f"{n} {item} by hand: {took}; a factory first"

    def ensure_packs(self, path: list[str], notes: list[str]) -> bool:
        """Before research: automate every pack it lacks unless hand-crafting is the cheaper or only way. Sets
        the foreman's hand budget (what it may craft for the labs). True when some packs come by hand."""
        cost: dict[str, int] = {}
        for t in path:
            for k, v in self.kn.research_cost(t).items():
                cost[k] = cost.get(k, 0) + v
        by_hand: dict[str, int] = {}
        for pack, n in cost.items():
            choice, goal, why = self.hand_or_automate(pack, n)
            if choice == "automate":
                self.log(f"[crew] automation first: {why}")
                built = False
                for _ in range(2):                    # a belt factory that does not fit: the workshop next
                    try:
                        notes.append(self.build(goal))
                        built = True
                        break
                    except BuildError as exc:
                        why = f"its factory failed ({str(exc)[:80]}): by hand for now"
                        nxt = self.automation_goal(pack)
                        if nxt is None or nxt.get("module") == goal.get("module"):
                            break
                        self.log(f"[crew] {goal['module']} for {pack} failed; trying a {nxt['module']}")
                        goal = nxt
                if built:
                    continue
                self._auto_failed[pack] = self._minute()
                choice = "hand"
            if choice == "hand":
                by_hand[pack] = n
                notes.append(f"{pack} by hand: {why}")
        self.hand_allow = dict(by_hand)
        if by_hand:
            self.foreman.note(f"research by hand-crafted packs: {', '.join(f'{k} x{v}' for k, v in by_hand.items())}")
        return bool(by_hand)

    # Run 20261122-133537: "build pumpjack" waited 15.6 and then 15.1 game minutes on fluid-handling stuck at 36%
    # (the labs had red packs and no green), each time until its budget ran out. Research that has not moved for
    # this long while the foreman fed the labs will not move by waiting: the goal fails and says which packs.
    RESEARCH_STALL_MIN = 3.0

    def _wait_research(self, tech: str, path: list[str], defer: bool = True) -> str:
        secs = sum(self.kn.research_seconds(t, self.base.count("labs")) for t in path)
        deadline = time.monotonic() + secs * 4 / self.w.speed + 120
        mark, since = None, self._minute()
        rs0 = self.w.research or {}
        first = (self._minute(), rs0.get("current"), float(rs0.get("progress") or 0))
        topped: set = set()
        while time.monotonic() < deadline and self.w.minutes_left > 0.5:
            self.checkpoint()
            rs = self.w.research or {}
            self.w.doing(f"waiting for research: {tech} ({rs.get('progress', 0):.0%})")
            self.foreman.patrol(force=True, feed_hard=True)
            self.w.read_stats(1)
            if tech in self.kn.researched:
                return f"researched {tech}"
            rs = self.w.research or {}
            now_mark = (rs.get("current"), round(float(rs.get("progress") or 0), 4),
                        sum(1 for t in path if t in self.kn.researched))
            if now_mark != mark:
                mark, since = now_mark, self._minute()
            elif self._minute() - since >= self.RESEARCH_STALL_MIN:
                if rs.get("current") not in topped and self._top_up_stalled(rs):
                    topped.add(rs.get("current"))      # once per tech: the foreman crafts the few that are left
                    since = self._minute()
                    continue
                raise BuildError(self._research_stalled(tech, rs, self._minute() - since))
            if rs.get("current") and rs.get("current") != first[1]:
                # the next tech of the path is on now: the pace is judged from here (run 20261125-214645: the
                # path moved from fluid-handling to oil-gathering, the ETA stayed None for good, and the pumpjack
                # goal waited out its budget twice)
                first = (self._minute(), rs.get("current"), float(rs.get("progress") or 0))
            eta = self._research_eta(path, rs, first)
            if defer and eta is not None and eta > self.RESEARCH_DEFER_MIN:
                raise BuildError(f"research for {tech} needs about {eta:.0f} more game minutes at this pace: the "
                                 "crew does other work meanwhile (the research goes on); build this once "
                                 f"{tech} is researched")
            time.sleep(2)
        raise BuildError(f"research {tech} did not finish in time")

    # Run 20261124-120854: "build pumpjack" waited 16 game minutes on oil-gathering creeping from 0 to 53% and was
    # dropped over budget, while science links and copper belts waited for the crew. Research that moves but
    # will take longer than this is left to run: the goal fails with the time it needs and comes back later.
    RESEARCH_DEFER_MIN = 8.0
    RESEARCH_ETA_AFTER_MIN = 1.5

    def _research_eta(self, path: list[str], rs: dict, first: tuple) -> float | None:
        """Game minutes until the last tech of `path` is done at the pace seen since `first` (minute, tech,
        progress), or None while there is too little to judge (under RESEARCH_ETA_AFTER_MIN, or another tech)."""
        t0, cur0, p0 = first
        cur, p = rs.get("current"), float(rs.get("progress") or 0)
        span = self._minute() - t0
        if span < self.RESEARCH_ETA_AFTER_MIN or cur is None or cur != cur0 or p <= p0:
            return None
        rate = (p - p0) / span                         # of the current tech, a game minute
        labs = max(1, self.base.count("labs"))
        nominal = self.kn.research_seconds(cur, labs) / 60.0
        slow = (1.0 / rate) / nominal if nominal > 0 else 1.0
        rest = sum(self.kn.research_seconds(t, labs) / 60.0 for t in path
                   if t != cur and t not in self.kn.researched)
        return (1.0 - p) / rate + slow * rest

    # The overnight zones test (seed 737373): automation stopped at 80% with the labs out of packs; it needed 2 more
    # red packs (10 s of crafting), the wait gave up instead, and the base sat 25 game minutes until the overseer
    # ordered 50 packs. A stalled tech whose remaining packs are this few, all hand-craftable, gets them by hand.
    STALL_TOPUP_MAX = 30

    def _top_up_stalled(self, rs: dict, dry: bool = False) -> bool:
        """Let the foreman hand-craft what the stalled current tech still needs, when that is a handful (True if
        it may). The wait's patrol (feed_hard) or a lab feed crafts and carries them. `dry`: only say whether."""
        cur = rs.get("current")
        t = self.kn.techs.get(cur) if cur else None
        if not t or not t.get("unit_count") or not hasattr(self.kn, "recipe_for"):
            return False                               # an unknown cost: never guessed
        left = math.ceil((1.0 - float(rs.get("progress") or 0)) * t["unit_count"]) + 1
        need = {i["name"]: int(i["amount"]) * left for i in t.get("unit_ingredients") or []}
        if not need or any(n > self.STALL_TOPUP_MAX or not self.kn.recipe_for(k, hand=True)
                           for k, n in need.items()):
            return False
        if dry:
            return True
        for k, n in need.items():
            self.hand_allow[k] = max(int(self.hand_allow.get(k, 0) or 0), n)
        self.log(f"[crew] research {cur} stalled with its labs out of packs: crafting the last "
                 f"{', '.join(f'{n} {k}' for k, n in need.items())} by hand")
        return True

    def _research_stalled(self, tech: str, rs: dict, minutes: float) -> str:
        cur = rs.get("current") or tech
        packs = [i["name"] for i in (self.kn.techs.get(cur) or {}).get("unit_ingredients") or []]
        return (f"research stalled: {cur} has not moved from {float(rs.get('progress') or 0):.0%} in "
                f"{minutes:.0f} game minutes, so {tech} will not finish by waiting. Its labs need "
                f"{', '.join(packs) or 'science packs'} flowing: get those science factories fed and linked to the "
                "labs first (or stock the packs), then build what needs the research")

    # ============================================================== making
    @contextmanager
    def hold(self, items: dict[str, int]):
        """Reserve these items while the block runs (nested holds add up)."""
        items = {k: int(v) for k, v in items.items() if v and v > 0}
        for k, v in items.items():
            self.reserved[k] = self.reserved.get(k, 0) + v
        try:
            yield
        finally:
            for k, v in items.items():
                left = self.reserved.get(k, 0) - v
                if left > 0:
                    self.reserved[k] = left
                else:
                    self.reserved.pop(k, None)

    def _kit_raw(self, kit: dict[str, int]) -> dict[str, int]:
        """Everything a kit is made from, in total (steel counted as the iron it is smelted from too)."""
        try:
            raw = dict(self.kn.kit_plan(kit, {}, supplied=self.ready_items()).raw)
        except Exception:  # noqa: BLE001 - an estimate for the courier only
            return dict(kit)
        for item, n in list(raw.items()):
            if item in SMELTED:
                src, per = SMELTED[item]
                raw[src] = raw.get(src, 0) + per * n
        return {**raw, **{k: v for k, v in kit.items() if k not in raw}}

    def make(self, kit: dict[str, int], extra: bool = False) -> None:
        """Hold everything in `kit` (in total; extra=True: on top of what we hold). What it is made from stays
        reserved meanwhile: the courier once carried off the iron a pumpjack's steel was to be smelted from, for
        34 game minutes (run 20261119-202811)."""
        raw = self._kit_raw(kit)
        if raw.get("steel-plate", 0) > self.w.inventory.get("steel-plate", 0) + self.stored("steel-plate"):
            self._steel_first()
        with self.hold(raw):
            return self._make(kit, extra)

    def make_for(self, need: dict[str, int]) -> None:
        """Hold enough for one more job: `need` plus only what another step of the goal in hand still keeps
        (the build's kit, items held while making), never on top of a whole hoard. make(need, extra=True) here
        crafted every link's belts anew beside what was carried: run 20261121-140755 hand-made 1924 underground
        belts, held 1489 of them, and one pump build spent ~15 game minutes and ~4500 iron plates crafting 172
        undergrounds three times while 800 sat in the inventory."""
        inv = getattr(self.w, "inventory", None) or {}
        hold, res = getattr(self, "kit_hold", None) or {}, getattr(self, "reserved", None) or {}
        kept = {k: min(inv.get(k, 0), hold.get(k, 0) + res.get(k, 0)) for k in need}
        self.make({k: v + kept[k] for k, v in need.items() if v})

    STEEL_FIRST_PER_MIN = 15.0         # steel a minute of the first column: blue science's engines and then some

    def steel_column(self) -> int:
        """Furnaces of the first steel column: STEEL_FIRST_PER_MIN at the furnace it gets (3.75 a minute in stone,
        7.5 in steel). Run 20261125-214645: 4 steel furnaces made 22-28 steel a minute for 2.2 used, and booked
        150 iron a minute."""
        from .outposts import SmelterColumn
        fur = SmelterColumn.furnace(self.w)
        return max(1, math.ceil(self.STEEL_FIRST_PER_MIN / self.STEEL_PER_MIN.get(fur, 3.75)))

    def _steel_first(self) -> str:
        """Steel by belt before steel by hand: once steel-processing is in and nothing of ours makes steel, a
        smelting column on iron plates comes first (its iron is linked in like any input; kits then take steel
        from its chests). Hand-smelting is the fallback for a few plates only (run 20261119-202811: a pumpjack's
        5 steel took 34+ game minutes by hand). Returns a note."""
        if getattr(self, "_in_steel_first", False) or "steel-processing" not in self.kn.researched                 or "steel-plate" in self.ready_items() or not self.base.powered_poles()                 or self._minute() - self.failed_suppliers.get("steel-plate", -1e9) < 15:
            return ""
        self._in_steel_first = True
        try:
            self.log("[crew] steel first: a smelting column on iron plates, so kits take steel from a chest")
            return self.build({"do": "build", "module": "smelter_column", "resource": "iron-plate",
                               "count": self.steel_column()})
        except BuildError as exc:            # the kit goes on; its steel is smelted by hand this time
            self.failed_suppliers["steel-plate"] = self._minute()
            self.foreman.note(f"steel first (smelting column) failed: {str(exc)[:100]}")
            return ""
        finally:
            self._in_steel_first = False

    def _make(self, kit: dict[str, int], extra: bool = False) -> None:
        self.w.refresh()
        made = self.ready_items()
        if made:                       # automation first: what our cells and factories made comes out of their chests
            if extra:
                kit = {k: v + self.w.inventory.get(k, 0) for k, v in kit.items()}
                extra = False
            for item, n in kit.items():
                short = n - self.w.inventory.get(item, 0)
                if short > 0 and item in made and item not in PLATES:
                    self._grab(item, short)
        if self.storage_held:          # left in storage earlier (a removal, a full inventory): take it back out
            want = {k: v + (self.w.inventory.get(k, 0) if extra else 0) for k, v in kit.items()}
            pulls = {k: v - self.w.inventory.get(k, 0) for k, v in want.items()
                     if v > self.w.inventory.get(k, 0) and self.stored(k) > 0}
            if pulls:
                kit, extra = want, False
                for item, short in pulls.items():
                    self.from_storage(item, short)
        for attempt in range(4):
            plan = self.kn.kit_plan(kit, self.w.inventory, extra=extra and attempt == 0, supplied=self.ready_items())
            if plan.blocked:
                raise BuildError(f"cannot hand-craft {', '.join(plan.blocked)}")
            if not plan.raw:
                for rec, times in plan.steps:
                    self.w.craft(rec, times)
                return
            for item, n in sorted(plan.raw.items(), key=lambda kv: kv[0] not in PLATES):
                self.gather(item, n + self.w.inventory.get(item, 0))
            if extra and attempt == 0:
                kit = {k: v + self.w.inventory.get(k, 0) for k, v in kit.items()}   # now a total
            self.w.refresh()
        raise BuildError(f"could not gather everything for {kit}")

    def gather(self, item: str, total: int) -> None:
        """Hold `total` of a raw item."""
        if item in PLATES:
            return self.plates(item, total)
        if item in SMELTED:
            return self.smelted(item, total)
        short = total - self.w.inventory.get(item, 0)
        if short <= 0:
            return
        if item == "wood":
            self.w.find_trees(walk=not self.w.trees_near(TREES_NEAR))   # wood is asked for by name: chests
            self.w.chop(short)
            return
        short -= self.from_storage(item, short)      # storage, then our own mines (waiting for them if they
        if short > 0:                                # make it fast enough), and only then by hand
            short -= self.from_mines(item, short)
        if short > 0:
            if item in ("coal", "stone", "iron-ore", "copper-ore"):
                try:
                    self.w.mine(item, short)
                except BuildError as exc:
                    if "inventory full" not in str(exc) or not self.ensure_space(12, force=True):
                        raise
                    self.w.mine(item, total - self.w.inventory.get(item, 0))
            elif self.cell_rate(item):
                raise BuildError(f"still {short} {item} short: our cells make {self.cell_rate(item):g}/min and the "
                                 f"chests hold less; wait, or build a workshop for more {item}")
            elif any(s.params.get("item") == item and s.chests for s in self.base.of("factory")):
                short -= self.from_factory(item, short)
                if short > 0:
                    fac = next(s for s in self.base.of("factory") if s.params.get("item") == item and s.chests)
                    raise BuildError(f"still {short} {item} short: factory site {fac.id} makes it but too little came "
                                     f"out ({fac.health or 'no reading'}); its inputs must reach it (relink it)")
            else:
                raise BuildError(f"no way to get {item}: nothing we built makes it (build a workshop for it)")

    FACTORY_WAIT_MIN = 6.0             # game minutes a kit waits on a factory it just stocked by hand

    def from_factory(self, item: str, want: int) -> int:
        """`want` of an item one of our factories makes (engine units for pumps): its output chest first; short of
        that, the inputs its chest ports lack are carried in by hand (only what this batch needs) and the crew
        waits for the factory. Run 20261121-140755: the engine-unit factory's steel belt carried nothing, 5 engines
        in 3 hours, and "stock pump" failed with "nothing we built makes it" while 4000 steel sat in a chest."""
        facs = [s for s in self.base.of("factory") if s.params.get("item") == item and s.chests]
        got = self._grab(item, want)
        if got >= want or not facs:
            return got
        rec = self.kn.recipes.get(item) or {}
        per = {i["name"]: float(i["amount"]) / max(1.0, float(sum(p["amount"] for p in rec.get("products", [])
                                                                 if p["name"] == item) or 1))
               for i in rec.get("ingredients", []) if i.get("type") != "fluid"}
        stocked = False
        for s in facs:
            for pt in s.params.get("ports") or []:
                inp, chest = pt.get("item"), pt.get("chest")
                if not chest or inp not in per:
                    continue
                n = math.ceil(per[inp] * (want - got))
                try:
                    self.gather(inp, n + self.w.inventory.get(inp, 0))
                    put = self.w.insert(chest[0] + 0.5, chest[1] + 0.5, inp, min(n, self.w.inventory.get(inp, 0)))
                except BuildError as exc:
                    self.foreman.note(f"factory site {s.id}: could not carry {inp} in: {str(exc)[:80]}")
                    continue
                stocked = stocked or put > 0
                self.log(f"[crew] carried {put} {inp} into factory site {s.id} for {want - got} {item}")
        if not stocked:
            return got
        deadline = time.monotonic() + self.FACTORY_WAIT_MIN * 60 / self.w.speed
        while got < want and time.monotonic() < deadline:
            self.checkpoint()
            self.w.doing(f"waiting for {want - got} {item} from our factory")
            self.foreman.patrol(force=True)
            time.sleep(max(3.0, min(20.0, 30.0 / self.w.speed)))
            got += self._grab(item, want - got)
        return got

    def from_mines(self, item: str, want: int) -> int:
        """Take `want` of an item out of our mines' chests. When drills make it, wait for them (the foreman keeps
        the base running) as long as they deliver within MINE_WAIT_MIN game minutes: automation over hands.
        Returns how many we got."""
        chests = [(x, y) for s in self.base.sites for x, y, what in s.chests if what == item]
        if not chests or want <= 0:
            return 0
        # burner drills out of fuel deliver nothing: never waited for. Run 20261125-184025: two coal burners stood
        # dry, the crew had no coal, and the coal wait (whose patrol fetched coal through this wait again) held the
        # bot from game minute 36 to 122 with no goal running
        drills = sum(max(0, int(s.params.get("count", 0)) - int((s.health or {}).get("no_fuel", 0)))
                     for s in self.base.of("burner_mine")
                     if s.params.get("resource") == item and not _depleted(s))
        if getattr(self, "_mine_wait", False):        # inside a wait already (its patrol came back here): no
            drills, nested = 0, True                  # second wait, take what the chests hold
        else:
            nested = False
        per_min = 15.0 * drills - (2.25 * drills if item == "coal" else 0)   # coal drills burn some of their own
        per_min += 30.0 * sum(int(s.params.get("count", 0)) for s in self.base.of("electric_mine")
                              if s.params.get("resource") == item and s.params.get("into") == "chest")
        per_min += self.cell_rate(item)                 # workshop cells: kits wait for them (automation first)
        if any(s.kind == "smelter_column" and any(w == item for *_, w in s.chests) for s in self.base.sites):
            per_min += sum(self._source_rate((x, y), item) for s in self.base.of("smelter_column")
                           for x, y, w in s.chests if w == item)       # smelting columns (steel, plates)
        wait_min = CELL_WAIT_MIN if self.cell_rate(item) else MINE_WAIT_MIN
        got = 0
        if self.no_room(item):
            return 0
        if nested:
            per_min = 0.0                             # one pass over the chests, no waiting
        deadline = time.monotonic() + (wait_min * 60 / self.w.speed if per_min > 0 else 0)
        while True:
            for x, y in sorted(chests, key=lambda q: math.dist(q, self.w.pos)):
                if got >= want:
                    break
                got += self.w.extract(x, y, item, want - got)
            left = want - got
            if left <= 0 or per_min <= 0 or left / per_min > wait_min or time.monotonic() > deadline:
                return got
            if self.w.now_tick() - getattr(self.w, "last_full_tick", -1e9) < 120 and self.no_room(item):
                return got                            # refused for room just now, and no room could be made
            self.checkpoint()
            self.w.doing(f"waiting for {left} {item} from our {drills} drill(s)")
            self._mine_wait = True
            try:
                self.foreman.patrol(force=True)
            finally:
                self._mine_wait = False
            time.sleep(max(3.0, min(20.0, left / per_min * 60 / self.w.speed / 3)))

    def plates(self, plate: str, total: int) -> None:
        """Hold `total` plates: collect from our furnaces; if production is too slow, hand-mine and smelt."""
        with self.w.step("plates"):
            return self._plates(plate, total)

    BURNER_PLATES_MIN = 15.0           # plates a minute one burner drill's furnace gives
    ELECTRIC_PLATES_MIN = 18.0         # ... one electric drill's furnace
    PLATE_BORROW_MAX = 3               # link inserters lifted at once while the crew collects construction plates

    def _plate_sources(self, plate: str, lifted: tuple = ()) -> list[tuple[float, float, float, dict | None]]:
        """Every spot our plates come out of: (x, y, plates a minute the crew can take there, the link that belts
        them away or None). A unit whose chest a link takes from (the inserter out of it moves ~50 a minute, its
        furnace makes ~19) gives the crew only what the link's ports leave over: run 20261120-084046 waited for
        "66 iron a minute" that all went down the belt to the red-science factory, and builds starved for plates.
        A link in `lifted` has its inserter taken up for now: its unit gives everything again."""
        drained = {tuple(ln["source"]): ln for ln in self.links
                   if ln.get("item") == plate and ln.get("kind") not in ("trunk", "science") and ln.get("ins")
                   and ln.get("boost_into") is None and not any(ln is q for q in lifted)}
        budget = self._budget(plate) if drained else {}

        def left(root: tuple) -> float:                 # what a drained chest still gets after its ports
            load, supply = budget.get(root) or [0.0, self._source_rate(root, plate)]
            return max(0.0, supply - load)

        out: list[tuple[float, float, float, dict | None]] = []
        for s in self.base.sites:
            mine_chests = [(x, y) for x, y, what in s.chests if what == plate]
            for x, y, p in s.plate_furnaces:
                if p != plate:
                    continue
                if s.kind == "burner_mine":
                    out.append((x, y, 0.0 if _depleted(s) else self.BURNER_PLATES_MIN, None))
                elif s.kind == "electric_mine" and s.params.get("line"):
                    # the line takes every plate the furnaces make; what its links do not draw backs up into
                    # the furnaces, where the crew picks it up
                    ln = s.params["line"]
                    load, supply = (self._budget(plate).get(tuple(ln["source"]))
                                    or [0.0, self.line_rate(ln, s)])
                    share = max(0.0, 1.0 - load / supply) if supply > 0 else 0.0
                    out.append((x, y, 0.0 if _depleted(s) else self.ELECTRIC_PLATES_MIN * share, None))
                elif s.kind == "electric_mine":
                    near = min(mine_chests, key=lambda c: math.dist(c, (x, y)), default=None)
                    ln = drained.get(near) if near is not None and math.dist(near, (x, y)) <= 3 else None
                    rate = 0.0 if _depleted(s) else self.ELECTRIC_PLATES_MIN
                    out.append((x, y, min(rate, left(near)) if ln is not None else rate, ln))
                else:                                     # hand-fed smelters: what is inside, nothing more
                    out.append((x, y, 0.0, None))
            for x, y in mine_chests:                      # its rate is its furnace's (counted above)
                out.append((x, y, 0.0, drained.get((x, y))))
        return out

    def _lift_link(self, ln: dict) -> bool:
        """Take a link's inserter up for a while: its source fills its own chest for the crew again."""
        tile, facing, kind = ln["ins"]
        try:
            ok = self.w.pickup(kind, tile[0] + 0.5, tile[1] + 0.5, facing)
        except BuildError:
            return False
        if ok:
            ln["lifted"] = self._minute()
            self.lifted_links = getattr(self, "lifted_links", []) + [ln]
        return bool(ok)

    def restore_lifted(self) -> None:
        """Put back every link inserter taken up for plates (also after an interrupt: called in a finally)."""
        for ln in list(getattr(self, "lifted_links", [])):
            tile, facing, kind = ln["ins"]
            try:
                self.w.place(kind, tile[0] + 0.5, tile[1] + 0.5, facing, {tuple(tile)})
            except BuildError as exc:
                if "occupied" not in str(exc) and "exists" not in str(exc):
                    self.foreman.note(f"could not put back the inserter of the {ln.get('item')} link into site "
                                      f"{ln.get('to_site')}: {str(exc)[:80]}; trying again later")
                    continue
            ln.pop("lifted", None)
            self.lifted_links = [q for q in self.lifted_links if q is not ln]

    def _plates(self, plate: str, total: int) -> None:
        try:
            return self._plates_loop(plate, total)
        finally:
            self.restore_lifted()                     # the factories get their belts back, come what may

    def _plates_loop(self, plate: str, total: int) -> None:
        ore = PLATES[plate]
        deadline = time.monotonic() + 600 / self.w.speed
        self.restore_lifted()                         # anything left lifted from before goes back first
        lifted: list[dict] = []
        waited: float | None = None                   # plates the last wait for our drills should have brought
        distrust = False                              # a wait brought far less: smelt in the crew's furnaces
        dry: set = set()                              # spots that gave nothing (run 20261121-140755: 61 empty visits)

        def smelters():                  # furnaces we smelt in, and the chests electric mines and cells fill
            return ([tuple(b) for s in self.base.sites for b in (s.params.get("crew_boxes") or {}).get(plate, [])]
                    + [(x, y) for s in self.base.sites for x, y, p in s.plate_furnaces if p == plate]
                    + [(x, y) for s in self.base.sites for x, y, what in s.chests if what == plate])
        while time.monotonic() < deadline:
            self.w.refresh()
            short = total - self.w.inventory.get(plate, 0)
            if short <= 0:
                return
            got = self.from_storage(plate, short)
            self.checkpoint()
            self.w.doing(f"collecting {plate} ({self.w.inventory.get(plate, 0)}/{total})")
            own = set(self.hand_furnaces_of(ore))
            for x, y in sorted(smelters(), key=lambda q: math.dist(q, self.w.pos)):
                if (x, y) in dry and (x, y) not in own:
                    continue                          # empty last time: a belt drains it, do not walk there again
                n = self.w.extract(x, y, plate, short - got)
                if not n:
                    dry.add((x, y))
                got += n
                if got >= short:
                    break
            short -= got
            if short <= 0:
                return
            sources = self._plate_sources(plate, tuple(lifted))
            per_min = sum(r for *_, r, _ in sources) + self.cell_rate(plate)
            if not lifted and short > per_min * MINE_WAIT_MIN:
                # what the crew can take is too slow, but links belt plates away: lift the nearest links'
                # inserters while it collects (their factories wait a few minutes; builds do not wait forever)
                drained = sorted({id(ln): (x, y, ln) for x, y, r, ln in sources if ln is not None}.values(),
                                 key=lambda q: math.dist(q[:2], self.w.pos))
                gain = 0.0
                for x, y, ln in drained:
                    if short <= (per_min + gain) * MINE_WAIT_MIN or len(lifted) >= self.PLATE_BORROW_MAX:
                        break
                    if self._lift_link(ln):
                        lifted.append(ln)
                        gain = sum(r for *_, r, _ in self._plate_sources(plate, tuple(lifted))) \
                            + self.cell_rate(plate) - per_min
                if lifted:
                    self.foreman.note(f"short of {short} {plate} for a build: took up {len(lifted)} link inserter(s) "
                                      f"(into site(s) {sorted({ln.get('to_site') for ln in lifted}, key=str)}) "
                                      "while collecting; they go back right after")
                    per_min += gain
                    dry.clear()                       # the lifted links' chests fill again
            if waited is not None:                    # what the last wait brought against what was expected
                if got < 0.25 * waited:
                    distrust = True                   # the estimate is the plan, not what arrives: smelt instead
                waited = None
            if not distrust and per_min > 0 and short / per_min <= MINE_WAIT_MIN:   # our drills beat smelting
                self.w.doing(f"waiting for {short} {plate} from our furnaces ({per_min:.0f}/min for us"
                             + (f", {len(lifted)} link(s) paused" if lifted else "") + ")")
                self.foreman.patrol(force=True)
                pause = max(3.0, min(20.0, short / per_min * 60 / self.w.speed / 3))
                t_wait = time.monotonic()
                time.sleep(pause)
                waited = per_min * (time.monotonic() - t_wait) * self.w.speed / 60   # plates it should bring
                continue
            # too slow (or no furnaces yet): hand-mine ore and smelt it ourselves
            self.hand_smelt(plate, ore, short)
        raise BuildError(f"could not get {total} {plate} in time")

    SMELTER_FURNACES = 4

    def smelted(self, item: str, total: int) -> None:
        """Hold `total` of a furnace product made from plates (steel): out of storage and our smelter's furnaces,
        else feed the smelter plates and fuel and wait for it. Unlocks the recipe first (steel-processing)."""
        src, per = SMELTED[item]
        self.unlock_recipes({self.kn.recipe_for(item, unlocked_only=False) or item})
        energy = float((self.kn.recipes.get(item) or {}).get("energy") or 16)
        speed = float((self.kn.entities.get("stone-furnace") or {}).get("crafting_speed") or 1)
        with self.w.step("smelt"):
            deadline = time.monotonic() + (900 + 2 * energy / speed * total) / self.w.speed
            while time.monotonic() < deadline:
                self.w.refresh()
                short = total - self.w.inventory.get(item, 0)
                if short <= 0:
                    return
                short -= self.from_storage(item, short)
                columns = any(s.kind == "smelter_column" and any(w == item for *_, w in s.chests)
                              for s in self.base.sites)
                if (self.cell_rate(item) or columns) and short > 0:   # steel cells and columns come first
                    short -= self.from_mines(item, short)
                    if short <= 0:
                        return
                furnaces = self.smelter(item)
                for x, y in furnaces:
                    if short > 0:
                        short -= self.w.extract(x, y, item, short)
                if short <= 0:
                    return
                # plates for what is still short, less what the furnaces already hold
                inside = self._furnace_input(furnaces, src)
                feed = max(0, per * short - inside)
                if feed:
                    self.plates(src, feed + self.w.inventory.get(src, 0))
                    if self.w.fuel_units() < 2 * len(furnaces):
                        self.get_fuel(4 * len(furnaces))
                    each = math.ceil(feed / len(furnaces) / per) * per
                    for x, y in furnaces:
                        self.w.fuel(x, y, 3)
                        self.w.insert(x, y, src, each)
                self.w.doing(f"smelting {short} {item} in {len(furnaces)} furnaces")
                rounds = math.ceil(short / len(furnaces))
                left = min(60.0, rounds * energy / speed + 1) / self.w.speed
                while left > 0:                            # let the base run meanwhile
                    self.checkpoint()
                    self.foreman.patrol(force=True)
                    time.sleep(min(left, 5.0))
                    left -= 5.0
        raise BuildError(f"could not smelt {total} {item} in time")

    def _furnace_input(self, furnaces: list, item: str) -> int:
        """How much of `item` waits in these furnaces' input slots (one remote look)."""
        if not furnaces:
            return 0
        cx = sum(f[0] for f in furnaces) / len(furnaces)
        cy = sum(f[1] for f in furnaces) / len(furnaces)
        try:
            d = self.w.observe(8, x=cx, y=cy, include=["entities"], names=["stone-furnace"])
        except BuildError:
            return 0
        spots = set(furnaces)
        return sum(((e.get("inventories") or {}).get("source") or {}).get(item, 0)
                   for e in d.get("entities") or [] if (e["x"], e["y"]) in spots)

    def smelter(self, item: str) -> list[tuple[float, float]]:
        """The furnaces that make `item` from plates (built on first use: a few stone furnaces in a row beside
        the character, which is reachable by definition). The foreman keeps them fuelled and emptied."""
        key = f"smelter-{item}"
        for s in self.base.sites:
            if s.kind == key and s.plate_furnaces:
                return [(x, y) for x, y, _ in s.plate_furnaces]
        n = self.SMELTER_FURNACES
        have = self.w.inventory.get("stone-furnace", 0)
        if have < n:
            self.make({"stone-furnace": n})
        self.w.observe(16)
        hx, hy = math.floor(self.w.pos[0]), math.floor(self.w.pos[1])
        spots: list[tuple[int, int]] = []
        for r in range(3, 14):
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    x0, y0 = hx + dx, hy + dy
                    if (len(spots) < n and max(abs(dx), abs(dy)) == r and self.w.free_box(x0 - 1, y0 - 1, 2, 2)
                            and all(abs(x0 - s[0]) >= 3 or abs(y0 - s[1]) >= 3 for s in spots)):
                        spots.append((x0, y0))
            if len(spots) >= n:
                break
        if not spots:
            raise BuildError(f"no room for a {item} smelter near ({hx}, {hy})")
        site = Site(key, {"item": item, "count": len(spots)}, (hx, hy), built_minute=self._minute())
        rec: list = []
        self.w.recorder = rec
        last = ""
        try:
            for x0, y0 in spots:
                try:
                    self.w.place("stone-furnace", x0, y0, "north",
                                 {(x0 - 1, y0 - 1), (x0, y0 - 1), (x0 - 1, y0), (x0, y0)})
                except BuildError as exc:            # one refused spot: the others still make a smelter
                    last = str(exc)
                    continue
                site.plate_furnaces.append((x0, y0, item))
                site.burners.append((x0, y0))
        finally:
            self.w.recorder = None
        if not site.plate_furnaces:
            raise BuildError(f"no {item} smelter furnace could be placed ({last[:120]})")
        site.params["count"] = len(site.plate_furnaces)
        site.entities = rec
        site.centre = (sum(x for x, _, _ in site.plate_furnaces) / len(site.plate_furnaces),
                       sum(y for _, y, _ in site.plate_furnaces) / len(site.plate_furnaces))
        self.base.add(site)
        return [(x, y) for x, y, _ in site.plate_furnaces]

    # The crew smelts what its builds need in furnaces of its own. Run 20261121-140755: the base made 226 iron a
    # minute, all of it belted to factories; the crew got 14 a minute, hand-mined 40 ore at a time, and put it into
    # belted furnaces (whose plates went down the belt) and plate chests (ore onto a plate belt) while three ore
    # rows sat with full chests. A plastics build waited 19 minutes for 801 iron and failed.
    CREW_FURNACES_MAX = 8          # the crew's smelter grows to this many furnaces when builds wait on plates
    FURNACE_PLATES_MIN = 18.75     # plates a minute one stone furnace makes
    ORE_BATCH = 50                 # ore per crew furnace per round (one stack)

    def from_mine_chests(self, item: str, want: int) -> int:
        """Up to `want` of an item (ore, stone) out of our mines' chests at once, no waiting: an ore row "into
        chest" fills its chests while nothing draws on them."""
        got = 0
        chests = [(x, y) for s in self.base.sites for x, y, what in s.chests if what == item]
        for x, y in sorted(chests, key=lambda q: math.dist(q, self.w.pos)):
            if got >= want:
                break
            got += self.w.extract(x, y, item, want - got)
        return got

    def _gather(self, item: str, want: int) -> int:
        """Hold `want` of a raw item: storage, then our mines' chests, then (a small batch) by hand."""
        have = self.w.inventory.get(item, 0)
        if have < want:
            have += self.from_storage(item, want - have)
        if have < want:
            have += self.from_mine_chests(item, want - have)
        if have < min(want, 40):                      # hand-mining is slow: only a small batch, as a last resort
            self.w.mine(item, min(want, 40) - have)
        return self.w.inventory.get(item, 0)

    def hand_smelt(self, plate: str, ore: str, n: int) -> None:
        """Smelt up to `n` plates for the crew, in the crew's own furnaces only: never a mine's, line's or
        column's furnace (their plates are belted away) and never a chest. The smelter grows with the shortfall."""
        want = max(2, min(self.CREW_FURNACES_MAX, math.ceil(n / (self.FURNACE_PLATES_MIN * MINE_WAIT_MIN))))
        furnaces = self.hand_furnaces(ore, want)
        if not furnaces:
            raise BuildError(f"no room for the crew's {plate} furnaces")
        batch = min(n, self.ORE_BATCH * len(furnaces))
        self.w.doing(f"smelting {batch} {plate} in the crew's {len(furnaces)} furnaces")
        have = self._gather(ore, batch)
        if have <= 0:
            return
        if self.w.fuel_units() < 2 * len(furnaces):
            self.get_fuel(4 * len(furnaces))
        per = max(1, min(have, batch) // len(furnaces))
        for x, y in furnaces:
            self.w.fuel(x, y, 3)
            self.w.insert(x, y, ore, per)
        self.foreman.patrol(force=False)
        time.sleep(min(30.0, per * 3.2 / self.w.speed))

    def get_fuel(self, units: int) -> None:
        """Fuel (coal-equivalents): coal from storage and our coal mines (waiting for them if they deliver soon);
        before any coal mine, wood from trees close by; else coal mined by hand. Never a long walk for trees."""
        if self.no_room("coal"):                     # every extract would be refused: do not walk for nothing
            self.foreman.note("the inventory is full and nothing more could go into storage: no room for coal")
            return
        for x, y in self.coal_chests():
            if self.w.fuel_units() >= units:
                return
            self.w.extract(x, y, "coal", 400)
        short = units - self.w.fuel_units()
        if short > 0:
            short -= self.from_storage("coal", short)
        if short > 0 and self.coal_mines():
            short -= self.from_mines("coal", short)
        if short <= 0:
            return
        if not self.coal_mines() and self.fuel_item() == "wood":
            try:
                self.w.chop(2 * short, within=TREES_NEAR)
            except BuildError:
                pass
        if self.w.fuel_units() < units:
            self.w.mine("coal", units - self.w.fuel_units())

    def hand_furnaces_of(self, ore: str) -> list[tuple[float, float]]:
        """The crew's own furnaces for an ore, as they stand (none built here)."""
        return [(x, y) for s in self.base.sites if s.kind == f"smelter-{ore}" for x, y, _ in s.plate_furnaces]

    def hand_furnaces(self, ore: str, want: int = 2) -> list[tuple[float, float]]:
        """The crew's own stone furnaces for an ore (a smelter-<ore> site, kept for good): built on first use
        beside our ore chests (else by the ore patch), and grown to `want` (at most CREW_FURNACES_MAX) when builds
        wait on plates."""
        key = f"smelter-{ore}"
        site = next((s for s in self.base.sites if s.kind == key), None)
        have = len(site.plate_furnaces) if site is not None else 0
        want = min(max(want, 1), self.CREW_FURNACES_MAX)
        if site is not None and have >= want:
            return [(x, y) for x, y, _ in site.plate_furnaces]
        need = want - have
        if self.w.inventory.get("stone-furnace", 0) < need:
            short = need - self.w.inventory.get("stone-furnace", 0)
            self._gather("stone", 5 * short)
            self.w.craft("stone-furnace", min(short, self.w.inventory.get("stone", 0) // 5))
        need = min(need, self.w.inventory.get("stone-furnace", 0))
        if site is not None:
            here = site.centre
        else:
            ore_chests = [(x, y) for s in self.base.sites for x, y, what in s.chests if what == ore]
            p = None if ore_chests else self.w.patch(ore)
            here = (min(ore_chests, key=lambda q: math.dist(q, self.w.pos)) if ore_chests
                    else (p["nearest"]["x"], p["nearest"]["y"]) if p else self.w.pos)
        if need > 0:
            self.w.go(*here)
            self.w.observe(16)
        taken = [(x, y) for x, y, _ in (site.plate_furnaces if site is not None else [])]
        spots: list[tuple[int, int]] = []
        hx, hy = int(here[0]), int(here[1])
        for r in range(2, 14):
            if len(spots) >= need:
                break
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    x0, y0 = hx + dx, hy + dy
                    if (len(spots) < need and max(abs(dx), abs(dy)) == r and self.w.free_box(x0 - 1, y0 - 1, 2, 2)
                            and all(math.dist((x0, y0), s) >= 3 for s in spots + taken)):
                        spots.append((x0, y0))
        if site is None:
            site = Site(key, {"ore": ore}, here, built_minute=self._minute())
            site.entities = []
            self.base.add(site)
        plate = {v: k for k, v in PLATES.items()}[ore]
        rec: list = []
        self.w.recorder = rec
        try:
            for x0, y0 in spots:
                self.w.place("stone-furnace", x0, y0, "north", {(x0 - 1, y0 - 1), (x0, y0 - 1), (x0 - 1, y0), (x0, y0)})
                site.plate_furnaces.append((x0, y0, plate))
                site.burners.append((x0, y0))
        finally:
            self.w.recorder = None
        site.entities = list(site.entities or []) + rec
        return [(x, y) for x, y, _ in site.plate_furnaces]


def heal_powered(s: Site) -> None:
    """A site the records call unpowered whose machines the game shows with power (later poles reached them) is
    powered again: the records and the alerts heal, and the rewire stops connecting it (run 20261122-133537: sites
    19 and 22 ran again over other builds' poles and still read unpowered, so connect after connect failed)."""
    h = s.health or {}
    if s.powered is False and s.electric and s.kind != "steam_power" and sum(h.values()) and not h.get("no_power"):
        s.powered = True


def _pole_groups(poles, reach: float = 7.0) -> list[list[tuple[int, int]]]:
    """A site's poles split into groups that wire to each other (small pole reach 7.5)."""
    left = [tuple(p) for p in dict.fromkeys(tuple(q) for q in poles)]
    out = []
    while left:
        group = [left.pop(0)]
        k = 0
        while k < len(group):
            near = [q for q in left if math.dist(q, group[k]) <= reach]
            for q in near:
                left.remove(q)
            group += near
            k += 1
        out.append(group)
    return out


def _site_key(kind: str, params: dict) -> str:
    """What a site is, for spotting remove-then-rebuild churn: kind plus what it makes."""
    what = params.get("resource") or params.get("item") or params.get("ore") or ""
    return f"{kind} {what}".strip()


def _depleted(site: Site) -> bool:
    drills = sum(1 for e in site.entities if e[0] in ("burner-mining-drill", "electric-mining-drill"))
    return drills > 0 and site.health.get("no_minable_resources", 0) >= max(1, math.ceil(drills * 0.75))


def _centre(rec: list) -> tuple[float, float]:
    return (sum(e[1] for e in rec) / len(rec), sum(e[2] for e in rec) / len(rec))


class Foreman:
    """Keeps the base running between goals, and keeps every site's health current for the strategist."""

    def __init__(self, crew: Crew):
        self.crew = crew
        self.last = 0.0
        self.problems: list[str] = []
        self.fuel_fixes = 0
        self.research_goal: str | None = None
        self.hand_packs = 0
        self.collected = 0                    # plates taken out of full furnaces
        self._rewired: dict[int, float] = {}
        self.busy: str | None = None          # a build goal is running: only urgent upkeep, near the character
        self._visited: dict[int, int] = {}    # site id -> tick of the last look
        self.priority: list[str] = []         # site ids, kinds, resources or items the strategist puts first
        self.boiler_coal: dict[tuple[float, float], int] = {}   # coal in each boiler's chest, last seen
        self.rationed = 0                     # refuels skipped because coal was short
        self.last_courier: str | None = None  # what the courier did on its last round
        self.fed_feeders = 0                   # packs put into lab feeder chests
        self._feeder_tried: set[int] = set()   # lab sites a feeder was tried on

    def patrol(self, force: bool = False, feed_hard: bool = False) -> None:
        w = self.crew.w
        lean = self.busy is not None and not feed_hard
        since = time.monotonic() - self.last
        if lean and since < LEAN_GAP_S / w.speed:   # building comes first: force does not override this
            return
        if not force and since < 25:
            return
        if since < 6:
            return
        self.last = time.monotonic()
        before = w.activity
        w.doing(f"foreman: urgent upkeep while building {self.busy}" if lean
                else "foreman patrol: coal, fuel, power, labs")
        try:
            w.foreman("coal")
            self._coal(lean)
            self.crew.ensure_space()
            w.foreman("sites")
            self._sites(lean)
            if lean:
                # one call, no walking: labs left idle through back-to-back builds sat empty for 35+ game
                # minutes (run 20261119-202811), since only the full patrol below queued research
                w.foreman("labs")
                self._keep_labs_busy(quiet=True)
                if self._labs_starving():
                    # labs out of packs while the crew holds some: the trip is worth it even mid-build. Run
                    # 20261125-184025: 20 red packs on hand, logistics at 0% for 15 minutes, every patrol lean
                    self._labs(False)
            else:
                w.foreman("power")
                self._rewire()
                w.foreman("labs")
                self._labs(feed_hard)
                w.foreman("links")
                self._audit_links()
                self._crew_boxes()
                w.foreman("courier")
                done = self.crew.courier()
                if done:
                    self.last_courier = done
                w.foreman("spares")
                self._spares()
        except Preempted:                     # the supervisor's pause: let it reach the main loop, not a "crash"
            raise
        except BuildError as exc:
            self.note(f"foreman: {str(exc)[:120]}")
        except ArenaError as exc:
            if exc.code in DEAD_RUN:
                raise
            self.note(f"foreman refused: {exc.code}")
        except Exception as exc:  # noqa: BLE001 - upkeep must never stop the crew
            self.note(f"foreman crashed: {type(exc).__name__}: {str(exc)[:100]}")
        finally:
            w.foreman(None)
        w.doing(before)
        self.crew.checkpoint()

    def _spares(self) -> str | None:
        """Top up one spare part (SPARES) from what is on hand or in storage. Returns what it made, if anything."""
        crew = self.crew
        now = crew._minute()
        if now - self.__dict__.get("_spares_at", -1e9) < SPARES_EVERY_MIN:
            return None
        self._spares_at = now
        unlocked = getattr(crew.kn, "unlocked", set()) or set()
        inv = crew.w.inventory
        held = {**(getattr(crew, "reserved", None) or {})}
        holds = [getattr(crew, "kit_hold", None) or {}, getattr(crew, "outpost_hold", None) or {}]
        for k, v in [kv for h in holds for kv in h.items()]:
            held[k] = max(held.get(k, 0), v)
        for item, want in SPARES.items():            # a hoard on hand goes to storage (kits draw on it there)
            keep = max(SPARES_CAP * want, held.get(item, 0))
            if inv.get(item, 0) > keep:
                try:
                    put = crew._stash(item, inv[item] - keep)
                except BuildError as exc:
                    self.note(f"storage: {str(exc)[:100]}")
                    break
                if put:
                    return f"{put} spare {item} into storage"
        for item, want in SPARES.items():
            if item not in unlocked:
                continue
            have = inv.get(item, 0) + crew.stored(item)
            if have >= want // 2:
                continue
            try:
                raw = dict(crew.kn.kit_plan({item: want - have}, {}, supplied=crew.ready_items()).raw)
            except Exception:  # noqa: BLE001 - no recipe data (a stub): nothing to top up
                return None
            if not raw or any(inv.get(k, 0) + crew.stored(k) < v for k, v in raw.items()):
                continue                              # not without gathering: the next build gathers anyway
            crew.make({item: want})
            return f"{want} {item}"
        return None

    def note(self, p: str) -> None:
        if p not in self.problems:
            self.problems.append(p)
            self.problems = self.problems[-12:]

    def forget(self, site: Site) -> None:
        self.problems = [p for p in self.problems if not p.startswith(f"site {site.id} ")]

    COAL_ROUND = 300               # coal the foreman carries for a round of refuelling

    def _coal(self, lean: bool = False) -> None:
        w = self.crew.w
        if w.inventory.get("coal", 0) >= 150:             # enough to top up several burners by nearly a stack
            return
        if self.crew.no_room("coal"):                     # a full inventory: walking to every chest gets nothing
            self.note("the inventory is full and nothing more could go into storage: no room for coal")
            return
        # enough for the round, not everything: chests a coal belt starts at last (their coal is the belts'),
        # stop once it is on hand (run 20261125-214645: 35,508 coal taken out of the chests feeding four coal
        # belts, 27,000 of it put into storage)
        sources = {tuple(ln["source"]) for ln in self.crew.links if ln.get("item") == "coal"}
        for x, y in sorted(self.crew.coal_chests(), key=lambda c: tuple(c) in sources):
            if w.inventory.get("coal", 0) >= self.COAL_ROUND:
                break
            if not lean or math.dist((x, y), w.pos) <= LEAN_REACH:
                w.extract(x, y, "coal", self.COAL_ROUND - w.inventory.get("coal", 0))
        # while building, gather fuel by hand only when it is nearly gone (the build itself is the priority)
        if w.fuel_units() < (5 if lean else 15) and any(s.burners for s in self.crew.base.sites):
            self.crew.get_fuel(30)

    # Priority: what keeps everything else running comes first. Coal mines feed every burner (and refuel
    # themselves from their own chests), the power station runs the labs and every electric machine, then
    # whatever the strategist asked to put first, then electric sites, plate mines, the rest.
    TIERS = ("coal mines", "power", "strategist's priority", "labs and factories", "plate mines", "other")

    def tier(self, s: Site) -> int:
        if s.kind == "burner_mine" and s.params.get("resource") == "coal":
            return 0
        if s.kind == "steam_power":
            return 1
        if str(s.id) in self.priority or s.kind in self.priority or s.params.get("resource") in self.priority \
            or s.params.get("item") in self.priority:
            return 2
        if s.kind in ("labs", "factory"):
            return 3
        if s.plate_furnaces:
            return 4
        return 5

    @staticmethod
    def needs_hands(s: Site) -> bool:
        """Might the patrol have to do something there: refuel burners, top up a boiler chest, empty a burner
        mine's furnaces, take coal from a coal mine's chests. Not when coal comes by belt (a fuel port that is fed):
        a belted furnace row, pumpjacks and every other electric site run without hands."""
        belted = any(pt.get("fuel") and pt.get("fed") for pt in s.params.get("ports", []) or [])
        if s.kind == "burner_mine":
            return True
        if s.kind == "steam_power" or s.burners:
            return not belted
        return False

    def revisit(self, s: Site) -> float:
        """Game ticks between looks at a site: every round where hands help; else only for its health."""
        if self.needs_hands(s):
            return 0.0
        fine = bool(s.health) and all(k in FINE for k in s.health)
        return SEALED_REVISIT if fine and s.id in getattr(self.crew, "sealed", {}) else AUTOMATED_REVISIT

    def route(self, sites: list[Site]) -> list[Site]:
        """Sites in priority order; within a tier, always the nearest next (less walking, a faster patrol)."""
        out: list[Site] = []
        here = self.crew.w.pos
        for t in range(len(self.TIERS)):
            todo = [s for s in sites if self.tier(s) == t]
            while todo:
                s = min(todo, key=lambda q: math.dist(q.centre, here))
                todo.remove(s)
                out.append(s)
                here = s.centre
        return out

    def _sites(self, lean: bool = False) -> None:
        """One look per site, most important first: health (status counts), refuel its burners, top up its
        boiler chest. When coal is short it goes to coal mines and the power station first, and the least
        important sites wait. lean (a build is running): far sites only when not looked at for a while
        (power and coal mines more often than the rest)."""
        w = self.crew.w
        now = w.now_tick()
        for s in self.route(list(self.crew.base.sites)):
            t = self.tier(s)
            names = sorted({e[0] for e in s.entities if e[0] not in POLES and e[0] not in (
                "transport-belt", "underground-belt", "pipe", "wooden-chest", "splitter")
                and (e[0] != "iron-chest" or s.kind == "steam_power")})
            if not names:
                continue
            revisit = LEAN_REVISIT_CRITICAL if t <= 1 else LEAN_REVISIT
            if lean and math.dist(s.centre, w.pos) > LEAN_REACH and now - self._visited.get(s.id, -1e9) < revisit:
                continue
            # look from here when the level's observe range reaches it; walk only to a site that may need hands
            # (fuel, a boiler chest, plates to collect, a coal mine's chests). Run 20261120-211555: every 3 game
            # minutes of a workshop build the patrol walked ~500 tiles to look at two pumpjacks and back.
            far = math.dist(s.centre, w.pos) > max(0, getattr(w, "observe_range", 60) - 2)
            if far and not self.needs_hands(s):
                continue                                  # looked at again whenever the character is in range
            gap = max(self.revisit(s), FAR_REVISIT if far else 0)
            if now - self._visited.get(s.id, -1e9) < gap:
                continue
            self._visited[s.id] = now
            try:
                cx, cy = s.centre
                own = {(e[1], e[2]) for e in s.entities}
                radius = min(32, max(6, int(max((math.dist((x, y), (cx, cy)) for x, y in own), default=0)) + 3))
                if far:
                    w.near(cx, cy, dist=20)
                # a mine's ground too, now and then: the ore left under its drills says when to rebuild it
                ground = s.kind in ("electric_mine", "burner_mine", "outpost") and                     self.crew._minute() - s.params.get("ore_look", -1e9) >= ORE_LOOK_MIN
                d = w.observe(radius, x=cx, y=cy, include=["entities"] + (["resources"] if ground else []),
                              names=names[:20])
                if ground and "resources" in d:
                    s.params["ore_look"] = round(self.crew._minute(), 1)
            except BuildError:
                continue
            ents = [e for e in d.get("entities") or [] if (e["x"], e["y"]) in own]
            if t == 0:                                    # a coal mine feeds itself from its own chests first
                low = sum(1 for e in ents if (e.get("inventories") or {}).get("fuel") is not None
                          and sum(((e.get("inventories") or {}).get("fuel") or {}).values()) < 5)
                for x, y, what in s.chests:
                    if low and what == "coal":
                        self.crew.w.extract(x, y, "coal", 30 * low)
            if getattr(s, "cells", None):              # which cells work (Workshop.stalled): seeded at the first
                seen = s.__dict__.setdefault("cell_working", {})   # look, so a cell gets its full grace from then
                at = {(e["x"], e["y"]): e.get("status", "") for e in ents}
                for c in s.cells:
                    key = (c.get("x"), c.get("y"))
                    if key not in seen or at.get(key) in ("working", "waiting_for_space_in_destination", "full_output"):
                        seen[key] = now / 3600
            health: Counter = Counter()
            census: dict = defaultdict(Counter)        # building name -> status counts
            plate_spots = {(x, y) for x, y, _ in s.plate_furnaces}
            scarce = w.fuel_units() < SCARCE_FUEL
            belted = any(pt.get("fuel") and pt.get("fed") for pt in s.params.get("ports", []))
            for e in ents:
                if e["name"] == "iron-chest":                 # the boiler's coal chest
                    if s.kind != "steam_power":               # a mine's or a column's chest: not a boiler's (run
                        continue                              # 20261124-015025: "feed site None" six times)
                    have = ((e.get("inventories") or {}).get("contents") or {}).get("coal", 0)
                    self.boiler_coal[(e["x"], e["y"])] = have
                    if belted and have >= 5:                  # a coal belt fills it: hands only if it runs dry
                        continue
                    # a big base: fill the chest for a long while (fewer visits), when coal is plentiful
                    target = BOILER_CHEST_TARGET * (6 if w.inventory.get("coal", 0) >= 400 else 1)
                    give = min(target - have, w.inventory.get("coal", 0) - (2 if scarce else 10))
                    if have < target // 2 and give > 0:
                        self.boiler_coal[(e["x"], e["y"])] += w.insert(e["x"], e["y"], "coal", give)
                    continue
                health[e.get("status", "")] += 1
                census[e["name"]][e.get("status", "")] += 1
                fuel = (e.get("inventories") or {}).get("fuel")
                if fuel is not None and sum(fuel.values()) < 3 and w.fuel_units() > 0:
                    if scarce and t >= 5:                     # coal is short: the least important sites wait
                        self.rationed += 1
                        continue
                    # plenty on hand: nearly a stack, as players do before coal belts (40 coal runs a stone
                    # furnace ~30 min, a burner drill ~18), so each burner needs a visit rarely
                    much = w.fuel_units() >= 100
                    amount = (40 if much else 8) if e["name"] != "boiler" else (40 if much else 20)
                    if scarce and t >= 3:
                        amount = 4
                    if w.fuel(e["x"], e["y"], amount, current=fuel):
                        self.fuel_fixes += 1
                out = (e.get("inventories") or {}).get("output") or {}
                for item, n in out.items():               # a full furnace stalls its drill: empty it into the stock
                    if (e["x"], e["y"]) in plate_spots and n >= 40 and w.inventory.get(item, 0) < 800:
                        self.collected += w.extract(e["x"], e["y"], item, n)
            s.health = dict(health)
            s.census = {n: dict(c) for n, c in census.items()}     # by building: which ones stand still
            if health.get("no_power") and s.powered is not False and health["no_power"] == sum(health.values()):
                s.powered = False
            else:
                heal_powered(s)
            if s.kind == "steam_power" and health.get(UNPLUGGED) and s.powered:
                s.powered = False                         # an engine powers nothing: _rewire plugs it in
            if _depleted(s):
                self.note(f"site {s.id} ({s.kind}) is depleted")

    def _rewire(self) -> None:
        """Sites that lost (or never had) power: try to connect them again, at most every 3 minutes each."""
        for s in self.crew.base.sites:
            if s.powered is False and (s.electric or s.kind == "steam_power") and time.monotonic() - self._rewired.get(s.id, 0) > 180 / self.crew.w.speed:
                self._rewired[s.id] = time.monotonic()
                try:
                    self.crew.connect(s)
                except BuildError as exc:
                    self.note(f"site {s.id} ({s.kind}): {str(exc)[:100]}")

    def _audit_links(self) -> None:
        """Every AUDIT_GAP_MIN game minutes: belt links that stopped carrying are taken up and laid again."""
        crew = self.crew
        now = crew._minute()
        if now - self.__dict__.get("_audited", -1e9) < crew.AUDIT_GAP_MIN or not crew.links:
            return
        self._audited = now
        notes = crew.audit_links()
        for n in notes:
            self.note(n)
            crew.log(f"[foreman] {n}")
        if notes and not any(g.get("do") == "link" for g in crew.urgent):
            from .agenda import agenda_of
            agenda_of(crew).propose(crew, {"do": "link", "reflex": "foreman: a broken belt link was taken up"},
                                    "foreman")

    CREW_BOX_EVERY_MIN = 10.0      # game minutes between the foreman's looks for lines with no crew box

    def _crew_boxes(self) -> None:
        crew = self.crew
        now = crew._minute()
        if now - self.__dict__.get("_boxes_at", -1e9) < self.CREW_BOX_EVERY_MIN:
            return
        self._boxes_at = now
        try:
            for n in crew.crew_boxes():
                self.note(n)
                crew.log(f"[foreman] {n}")
        except BuildError as exc:
            self.note(f"crew box: {str(exc)[:100]}")

    def _labs_starving(self) -> bool:
        """Do labs report missing packs while the crew holds (or its chests hold) packs the research takes?"""
        base, w = self.crew.base, self.crew.w
        labs = [s for s in base.of("labs") if not s.params.get("bus_fed")]    # the slot's labs: the bus feeds them
        if not labs or not any(s.health.get("missing_science_packs", 0) for s in labs):
            return False
        cur = (w.research or {}).get("current")
        need = {i["name"] for i in (self.crew.kn.techs.get(cur, {}) or {}).get("unit_ingredients", [])} if cur else set()
        have = {k for k, v in w.inventory.items() if v > 0 and k.endswith("science-pack")}
        # a chest a science belt starts at belongs to its station: hand-fed labs never count on it
        linked = self.crew.science_links
        stored = {what for s in base.sites for x, y, what in s.chests
                  if what.endswith("science-pack") and (x, y) not in linked}
        return bool(need & (have | stored)) if need else bool(have)

    def _keep_labs_busy(self, quiet: bool = False) -> str | None:
        """The technology the labs are on, after queueing the next one if they had none. None: nothing to do."""
        w = self.crew.w
        if not self.crew.base.of("labs"):
            return None
        cur = (w.research or {}).get("current")
        if not cur:
            w.read_stats(1)                                  # make sure: the queue may have just moved on
            cur = (w.research or {}).get("current")
        if not cur:
            cur = self.crew.auto_research()
            if cur:
                self.note(f"labs were idle: queued {cur}")
            elif not quiet:
                self.note("labs idle: nothing researchable with the science packs we make")
        # keep RESEARCH_AHEAD technologies queued, so the labs roll on to the next one between patrols. Run
        # 20261121-140755: one tech at a time, queued only once a patrol saw the labs idle; they stood idle eight
        # times, and the science factories backed up meanwhile
        queued = [t for t in ((w.research or {}).get("queue") or []) if t != cur]
        taken = {cur, *queued}
        for _ in range(max(0, self.RESEARCH_AHEAD - len(queued))):
            pick = self.crew.auto_research(exclude=taken) if cur else None
            if not pick:
                break
            taken.add(pick)
        if cur and (w.research or {}).get("current") != cur:
            w.research = {**(w.research or {}), "current": cur}   # an append does not change the one in hand
        return cur

    RESEARCH_AHEAD = 2             # technologies queued behind the one in hand

    def _labs(self, feed_hard: bool) -> None:
        w, kn, base = self.crew.w, self.crew.kn, self.crew.base
        labs = [p for s in base.of("labs") for p in s.labs]
        if not labs:
            return
        cur = self._keep_labs_busy()
        if not cur:
            return
        # every pack the research in hand and the next few in the queue use: a feeder full of them carries the
        # labs through several technologies without a visit
        techs = [cur] + [t for t in ((w.research or {}).get("queue") or []) if t != cur][:3]
        need = list(dict.fromkeys(i["name"] for t in techs for i in kn.techs.get(t, {}).get("unit_ingredients", [])))
        self._retrofit_feeders()
        # labs in the floorplan's science slot get their packs by bus (zones.build_labs_slot): never fed by hand
        plain = [s for s in base.of("labs") if not s.params.get("station") and not s.params.get("bus_fed")]
        heads = [s.labs[0] for s in base.of("labs") if s.params.get("station") and s.labs]
        belted = self.crew.belted_packs() if heads else set()   # these reach the stations by belt on their own
        feeders = [(x, y) for s in plain for x, y, what in s.chests if what == "lab-feed"]
        bare = [p for s in plain if not any(what == "lab-feed" for *_, what in s.chests) for p in s.labs]
        held = self._feeder_contents(feeders)
        for pack in need:
            want = sum(max(0, FEEDER_TARGET - held.get(f, {}).get(pack, 0)) for f in feeders
                       if held.get(f, {}).get(pack, 0) < FEEDER_TARGET // 2)
            want += 40 if bare else 0
            want += 40 if heads and pack not in belted else 0   # a pack no belt brings: into the head lab
            if want <= 0:
                continue                              # every feeder has plenty: no trip to the labs at all
            for s in base.sites:                      # automated packs first
                for x, y, what in s.chests:
                    # never out of a chest a science belt starts at: those packs go to the station on their own
                    # (run 20261125-214645: carried to the spawn labs while the station's belt ran dry)
                    if (x, y) in self.crew.science_links:
                        continue
                    if what == pack and w.inventory.get(pack, 0) < want:
                        w.extract(x, y, pack, want - w.inventory.get(pack, 0))
            allow = self.crew.hand_allow.get(pack, 0)     # only what hand_or_automate chose to do by hand
            if w.inventory.get(pack, 0) < 2 and feed_hard and allow > 0 and kn.recipe_for(pack, hand=True):
                n = min(5, allow)
                try:
                    self.crew.make({pack: n}, extra=True)
                    self.hand_packs += n
                    self.crew.hand_allow[pack] = allow - n
                except BuildError as exc:
                    self.note(f"cannot hand-craft {pack}: {str(exc)[:80]}")
            for f in feeders:                         # bulk into each feeder that runs low
                have = held.get(f, {}).get(pack, 0)
                if have < FEEDER_TARGET // 2 and w.inventory.get(pack, 0) > 0:
                    self.fed_feeders += w.insert(f[0], f[1], pack, FEEDER_TARGET - have)
            if bare and w.inventory.get(pack, 0) > 0:     # rows without a feeder (none could be added): by hand
                per = max(1, w.inventory[pack] // len(bare))
                for x, y in bare:
                    w.insert(x, y, pack, per)
            if heads and w.inventory.get(pack, 0) > 0 and (pack not in belted or not plain):
                # the station passes it down its row; packs carried anyway (out of labs taken down, a round
                # before) go in too rather than riding along (run 20261125-214645: every lab round ended
                # "still carrying 35 automation-science-pack")
                per = max(1, w.inventory[pack] // len(heads))
                for x, y in heads:
                    w.insert(x, y, pack, per)

    def _feeder_contents(self, feeders: list) -> dict:
        """What each feeder chest holds (remote looks, no walking)."""
        w, out = self.crew.w, {}
        for x, y in feeders:
            if (x, y) in out:
                continue
            try:
                d = w.observe(6, x=x, y=y, include=["entities"], names=["iron-chest"])
            except BuildError:
                continue
            for e in d.get("entities") or []:
                if (e["x"], e["y"]) in feeders:
                    out[(e["x"], e["y"])] = dict(((e.get("inventories") or {}).get("contents") or {}))
        return out

    def _retrofit_feeders(self) -> None:
        """Lab rows built before feeders existed get one (once each; skipped where the ground is taken)."""
        from .modules import Labs
        for s in self.crew.base.of("labs"):
            if s.params.get("station") or s.params.get("bus_fed"):   # fed by belts (ports, the bus), never a feeder
                continue
            if s.id in self._feeder_tried or any(what == "lab-feed" for *_, what in s.chests):
                continue
            self._feeder_tried.add(s.id)
            t = Labs.feeder_tiles(s)
            if not self.crew.w.free({t["chest"], t["inserter"]}):
                self.note(f"site {s.id} (labs): no room for a feeder chest; the foreman feeds each lab")
                continue
            try:
                self.crew.make({"iron-chest": 1, "inserter": 1, POLE: 1}, extra=True)
                rec: list = []
                self.crew.w.recorder = rec
                try:
                    Labs.add_feeder(self.crew.w, s)
                finally:
                    self.crew.w.recorder = None
                s.entities += rec
                self.crew.connect(s)                  # the feeder's inserter is on the row's pole line
            except BuildError as exc:
                self.note(f"site {s.id} (labs): feeder not added: {str(exc)[:80]}")
