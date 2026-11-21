"""Competition, task and secrets configuration (YAML files)."""

from __future__ import annotations

import hashlib
import secrets as pysecrets
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import catalog, rules
from .models import VERBS


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Budgets(_Cfg):
    # place, move, insert, extract, craft, mine, rotate, pickup. None = no action limit (time-only levels).
    actions: int | None = Field(default=None, ge=1)
    # Minimum game time per action: the world runs at least this many ticks after each action
    # (move/mine/craft already spend their own ticks and only top up to this). 0 = actions are instant.
    action_ticks: int = Field(default=0, ge=0, le=3600)
    observes: int = Field(ge=0)               # observe has its own budget
    ticks: int = Field(ge=1)                  # game time you may spend (advance, move, mine, craft...)
    idle_tail_ticks: int = Field(ge=0)        # scorer runs this many ticks after your run, no actions
    wall_clock_seconds: int = Field(ge=10)    # real-time limit for the whole run
    idle_timeout_seconds: int = Field(ge=10)  # run ends if no request arrives for this long
    # realtime clock only: at most one game action (not observe) per this many ticks (12 = 5 a second)
    min_action_gap_ticks: int = Field(default=0, ge=0, le=600)

    @model_validator(mode="after")
    def _limited(self) -> "Budgets":
        if self.actions is None and self.action_ticks == 0 and self.min_action_gap_ticks == 0:
            raise ValueError("a task without an action limit needs action_ticks > 0 (paused clock) or "
                             "min_action_gap_ticks > 0 (realtime clock), or actions would be free")
        return self


class Tier(_Cfg):
    """One automation tier: the factory alone must make `item` at `per_minute` during the hands-off window."""
    name: str
    item: str
    per_minute: float = Field(gt=0)
    # intermediates the factory itself must keep up with during the window: made >= balance x used
    chain: list[str] = []


class Milestone(_Cfg):
    """One step of a progression ladder (score type "milestones"). Exactly one condition:

    - tech: the technology is researched
    - item + per_minute (+ window minutes): made at that rate over the last `window` minutes (and every item in
      `also` at the same rate: "red and green at 30/min each")
    - item + produced: made at least this many in total
    - working (+ count): at least `count` of that entity are working at once
    - rockets: rockets launched

    automated (default true): hand-crafted items never count toward item conditions.
    The game checks every 10 s and records the tick each milestone is first reached.
    """
    id: str = Field(pattern=r"^[a-z0-9-]{1,40}$")
    name: str
    points: int = Field(ge=0)
    tech: str | None = None
    item: str | None = None
    fluid: bool = False
    per_minute: float | None = Field(None, gt=0)
    also: list[str] | None = Field(None, min_length=1, max_length=6)
    window: float = Field(5, gt=0, le=60)
    produced: int | None = Field(None, ge=1)
    automated: bool = True
    working: str | None = None
    count: int = Field(1, ge=1)
    rockets: int | None = Field(None, ge=1)

    @model_validator(mode="after")
    def _one(self) -> "Milestone":
        kinds = [self.tech is not None, self.working is not None, self.rockets is not None,
                 self.item is not None]
        if sum(kinds) != 1:
            raise ValueError(f"milestone {self.id}: give exactly one of tech, item, working, rockets")
        if self.item is not None and (self.per_minute is None) == (self.produced is None):
            raise ValueError(f"milestone {self.id}: an item milestone needs per_minute or produced")
        if self.also and self.per_minute is None:
            raise ValueError(f"milestone {self.id}: also is for rate milestones (item + per_minute)")
        cat = catalog.get()
        if not cat.empty:
            if self.tech and self.tech not in cat.technologies:
                raise ValueError(f"milestone {self.id}: unknown technology {self.tech}")
            for it in [self.item] + list(self.also or []) if self.item else []:
                if it not in (cat.fluids if self.fluid else cat.items):
                    raise ValueError(f"milestone {self.id}: unknown {'fluid' if self.fluid else 'item'} {it}")
            if self.working and self.working not in cat.entities:
                raise ValueError(f"milestone {self.id}: unknown entity {self.working}")
        return self

    def lua(self) -> dict:
        return {k: v for k, v in self.model_dump().items() if v is not None and k not in ("name", "points")}


class Limits(_Cfg):
    """What one call may see or do. The defaults are the original levels."""
    observe_radius: int = Field(20, ge=1, le=rules.MAX_OBSERVE_RADIUS)
    observe_range: int = Field(0, ge=0, le=5000)     # an observe may be centred this far from the character
    scan_radius: int = Field(0, ge=0, le=rules.MAX_SCAN_RADIUS)    # 0 = no scan verb
    batch_max_ops: int = Field(0, ge=0, le=rules.MAX_BATCH_OPS)     # 0 = no batch verb
    batch_op_gap_ticks: int | None = Field(None, ge=1, le=600)      # rate-limit cost per op (default: the gap)
    chart_radius: int = Field(0, ge=0, le=1024)      # the map is charted and generated this far around the AI


class Score(_Cfg):
    type: Literal["production", "delivered", "automation", "milestones"]
    item: str = ""
    # delivered only: organizer places a locked iron chest at spawn + offset
    delivery_offset: tuple[int, int] = (24, 0)
    # automation only: tiers from lowest to highest, points per item made during the window
    tiers: list[Tier] = []
    points: dict[str, int] = {}
    balance: float = Field(0.8, gt=0, le=1)
    # automation: the window starts when the build time is used up, however early the run ended;
    # the scorer fills the rest of the build time first, so ending early changes nothing
    window_at_build_end: bool = False
    # milestones only: the ladder; score = sum of reached milestones' points, each plus up to
    # time_bonus x its points for reaching it early; the run ends once all are reached if end_when_complete
    milestones: list[Milestone] = []
    time_bonus: float = Field(0.5, ge=0, le=10)
    # the time bonus shrinks to nothing over this many ticks (default: the whole build time). Long or open-ended
    # levels set it shorter, so reaching a milestone in hour 2 still beats hour 20.
    bonus_horizon_ticks: int | None = Field(None, ge=1)
    end_when_complete: bool = False

    @model_validator(mode="after")
    def _automation(self) -> "Score":
        if self.type == "milestones":
            if not self.milestones:
                raise ValueError("milestones scoring needs milestones")
            ids = [m.id for m in self.milestones]
            if len(set(ids)) != len(ids):
                raise ValueError("milestone ids must be unique")
            return self
        if not self.item:
            raise ValueError(f"{self.type} scoring needs an item")
        if self.type != "automation":
            return self
        if not self.tiers or not self.points:
            raise ValueError("automation scoring needs tiers and points")
        for name in [self.item, *self.points, *(t.item for t in self.tiers),
                     *(c for t in self.tiers for c in t.chain)]:
            if not _known_item(name):
                raise ValueError(f"unknown item {name} in automation score")
        return self


def _known_item(name: str) -> bool:
    return name in rules.STACK_SIZE or name in catalog.get().items


NameList = list[str] | Literal["all"]


class Task(_Cfg):
    id: str
    title: str
    summary: str
    verbs: list[str]
    entities: NameList                          # "all": anything the game can build (needs the catalog)
    recipes: NameList = []                      # hand-craft recipes (craft verb); "all" = any hand-craftable
    assembler_recipes: NameList = []            # set_recipe verb; "all" = any machine recipe
    insert_items: list[str] | None = None       # items the player may insert by hand (None = any)
    clear_obstacles: bool = False               # pickup may also mine trees and rocks
    # paused: the world waits while the AI thinks. realtime: the game runs at normal speed from the
    # team's first call, actions apply when they arrive, walking takes game time.
    clock: Literal["paused", "realtime"] = "paused"
    # realtime only: game speed while the clock runs (1 = normal). Budgets stay in game ticks, so 2x halves
    # the real time a run takes, and the AI gets half the real time per game second.
    clock_speed: float = Field(1.0, ge=0.25, le=8)
    # unlock: assembler_recipes are unlocked from the start, no research (the original levels).
    # real: the base game's tech tree; research in labs, trigger technologies complete on their trigger.
    # all: everything researched from the start.
    tech: Literal["unlock", "real", "all"] = "unlock"
    # realtime only. straight: the character walks a straight line (the original levels). path: like a player,
    # around water, cliffs, rocks and buildings; a move with no way there fails with no_path.
    walking: Literal["straight", "path"] = "straight"
    limits: Limits = Limits()
    water_summary: bool = False                 # observes report the nearest water (on by default with unlock)
    budgets: Budgets
    start_inventory: dict[str, int]
    score: Score
    backends: list[Literal["sim", "factorio"]]
    practice_seeds: list[int]

    # ------------------------------------------------------------ allowed names
    def entity_allowed(self, name: str) -> bool:
        return self.entities == "all" or name in self.entities

    def recipe_allowed(self, name: str) -> bool:
        if self.recipes == "all":
            cat = catalog.get()
            return cat.hand_craftable(name) if not cat.empty else name in rules.HAND_RECIPES
        return name in self.recipes

    def machine_recipe_allowed(self, name: str) -> bool:
        return self.assembler_recipes == "all" or name in self.assembler_recipes

    @model_validator(mode="after")
    def _check(self) -> "Task":
        cat = catalog.get()
        for v in self.verbs:
            if v not in VERBS:
                raise ValueError(f"unknown verb {v}")
        for field in ("entities", "recipes", "assembler_recipes"):
            if getattr(self, field) == "all" and cat.empty:
                raise ValueError(f"{field}: all needs the game catalog (run: arena catalog)")
        if self.entities != "all":
            for e in self.entities:
                if e not in rules.ENTITIES and e not in cat.entities:
                    raise ValueError(f"unknown entity {e}")
        if self.recipes != "all":
            for r in self.recipes:
                if r not in rules.HAND_RECIPES and r not in cat.recipes:
                    raise ValueError(f"unknown recipe {r}")
        if self.assembler_recipes != "all":
            for r in self.assembler_recipes:
                if r not in rules.ASSEMBLER_RECIPES and r not in cat.recipes:
                    raise ValueError(f"unknown assembler recipe {r}")
        if "set_recipe" in self.verbs and not self.assembler_recipes:
            raise ValueError("set_recipe needs assembler_recipes")
        for item in [*self.start_inventory, *(self.insert_items or [])]:
            if not _known_item(item):
                raise ValueError(f"unknown item {item}")
        if "research" in self.verbs and self.tech != "real":
            raise ValueError("the research verb needs tech: real")
        if "scan" in self.verbs and not self.limits.scan_radius:
            raise ValueError("the scan verb needs limits.scan_radius")
        if "batch" in self.verbs and not self.limits.batch_max_ops:
            raise ValueError("the batch verb needs limits.batch_max_ops")
        new_features = (self.tech != "unlock" or any(v in self.verbs for v in ("research", "stats", "scan", "batch"))
                        or self.score.type == "milestones" or self.entities == "all")
        if "sim" in self.backends and new_features:
            raise ValueError("tech trees, milestones and the stats, scan, research and batch verbs need the real game")
        if "sim" in self.backends and any(e not in rules.ENTITIES or not rules.ENTITIES[e].sim for e in self.entities):
            raise ValueError(f"task {self.id} uses entities the simulator cannot run")
        if self.score.type in ("delivered", "automation") and "sim" in self.backends:
            raise ValueError(f"{self.score.type} scoring needs the real game")
        if self.clear_obstacles and "sim" in self.backends:
            raise ValueError("clearing trees and rocks needs the real game")
        if self.clock == "realtime":
            if "sim" in self.backends:
                raise ValueError("the realtime clock needs the real game")
            if "advance" in self.verbs:
                raise ValueError("realtime levels have no advance: time runs on its own")
            if self.budgets.action_ticks:
                raise ValueError("action_ticks is for the paused clock; realtime uses min_action_gap_ticks")
        elif self.budgets.min_action_gap_ticks:
            raise ValueError("min_action_gap_ticks is for the realtime clock")
        if self.clock_speed != 1 and self.clock != "realtime":
            raise ValueError("clock_speed is for the realtime clock")
        return self

    def public(self) -> dict:
        """What teams may see. Practice seeds are public; eval seeds never appear here."""
        return self.model_dump()


class GatewayCfg(_Cfg):
    host: str = "127.0.0.1"
    port: int = 8710
    rate_limit_rps: float = 10.0
    rate_limit_burst: int = 30
    max_body_bytes: int = 16_384



class FactorioCfg(_Cfg):
    image: str = "factoriotools/factorio:2.1.24"
    slots: int = 2
    rcon_base_port: int = 27100
    game_base_port: int = 34200
    advance_speed: float = 60.0           # game.speed while ticks run (organizer only)
    boot_timeout_seconds: int = 180
    scenario_dir: str = "lua/scenario/arena"
    server_config_dir: str = "docker/config"
    container_prefix: str = Field("arena", pattern=r"^[a-z0-9-]{1,20}$")   # tells servers on one PC apart


class PracticeCfg(_Cfg):
    runs_per_day: int = 30


class EvalCfg(_Cfg):
    open: bool = False
    reveal_scores: bool = False


class Paths(_Cfg):
    runs: str = "runs"
    secrets: str = "secrets"
    reports: str = "reports"


class Competition(_Cfg):
    name: str
    backend: Literal["sim", "factorio"] = "sim"
    gateway: GatewayCfg = GatewayCfg()
    factorio: FactorioCfg = FactorioCfg()
    practice: PracticeCfg = PracticeCfg()
    eval: EvalCfg = EvalCfg()
    paths: Paths = Paths()
    tasks: list[str]


class Settings:
    """Loaded configuration plus resolved paths and secrets."""

    def __init__(self, competition: Competition, tasks: dict[str, Task], root: Path):
        self.competition = competition
        self.tasks = tasks
        self.root = root
        self.runs_dir = root / competition.paths.runs
        self.secrets_dir = root / competition.paths.secrets
        self.reports_dir = root / competition.paths.reports

    # ---- teams -------------------------------------------------------------
    @property
    def teams_file(self) -> Path:
        return self.secrets_dir / "teams.yaml"

    def load_teams(self) -> dict[str, str]:
        """token sha256 -> team name"""
        if not self.teams_file.exists():
            return {}
        data = yaml.safe_load(self.teams_file.read_text(encoding="utf-8")) or {}
        return {t["token_sha256"]: t["name"] for t in data.get("teams", [])}

    def add_team(self, name: str) -> str:
        token = "fa_" + pysecrets.token_urlsafe(32)
        data = {"teams": []}
        if self.teams_file.exists():
            data = yaml.safe_load(self.teams_file.read_text(encoding="utf-8")) or {"teams": []}
        if any(t["name"] == name for t in data["teams"]):
            raise ValueError(f"team {name!r} already exists")
        data["teams"].append({"name": name, "token_sha256": hash_token(token)})
        self.secrets_dir.mkdir(parents=True, exist_ok=True)
        self.teams_file.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return token

    # ---- eval seeds --------------------------------------------------------
    @property
    def eval_seeds_file(self) -> Path:
        return self.secrets_dir / "eval_seeds.yaml"

    def load_eval_seeds(self) -> dict[str, list[int]]:
        if not self.eval_seeds_file.exists():
            return {}
        return yaml.safe_load(self.eval_seeds_file.read_text(encoding="utf-8")) or {}

    def generate_eval_seeds(self, task_id: str, count: int) -> list[int]:
        seeds = self.load_eval_seeds()
        public = set(self.tasks[task_id].practice_seeds)
        new: list[int] = []
        while len(new) < count:
            s = pysecrets.randbelow(2**31 - 1) + 1
            if s not in public and s not in new:
                new.append(s)
        seeds[task_id] = new
        self.secrets_dir.mkdir(parents=True, exist_ok=True)
        self.eval_seeds_file.write_text(yaml.safe_dump(seeds), encoding="utf-8")
        return new


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def load_settings(config_path: str | Path = "config/competition.yaml", load_tasks: bool = True) -> Settings:
    """load_tasks=False: the server settings only (the catalog command runs before tasks that need it load)."""
    config_path = Path(config_path).resolve()
    root = config_path.parent.parent
    comp = Competition.model_validate(yaml.safe_load(config_path.read_text(encoding="utf-8")))
    tasks: dict[str, Task] = {}
    for rel in comp.tasks if load_tasks else []:
        t = Task.model_validate(yaml.safe_load((root / rel).read_text(encoding="utf-8")))
        tasks[t.id] = t
    return Settings(comp, tasks, root)
