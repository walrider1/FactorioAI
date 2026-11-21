"""Public API schemas.

Every field a team can send is either a bounded number or a value from a fixed
whitelist (``Literal``). Nothing a team sends is ever free text, so nothing a team
sends can turn into Lua code on the game server.
"""

from __future__ import annotations

import math
from typing import Annotated, Literal, Union

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from . import catalog, rules

# The original levels' names. With a catalog (config/catalog.json, see arena/catalog.py) any name the game
# knows is accepted too; names are still checked against a fixed set, so free text never reaches Lua.
_STATIC_ENTITIES = {
    "burner-mining-drill", "stone-furnace", "wooden-chest", "iron-chest",
    "burner-inserter", "transport-belt",
    "electric-mining-drill", "assembling-machine-1", "inserter", "long-handed-inserter",
    "small-electric-pole", "offshore-pump", "boiler", "steam-engine", "pipe",
    "underground-belt", "splitter",
}
_STATIC_ITEMS = {
    "iron-ore", "copper-ore", "coal", "stone", "wood",
    "iron-plate", "copper-plate", "stone-brick", "iron-gear-wheel",
    "copper-cable", "electronic-circuit", "automation-science-pack", "logistic-science-pack",
} | _STATIC_ENTITIES
_STATIC_MACHINE_RECIPES = {
    "iron-gear-wheel", "copper-cable", "electronic-circuit", "inserter", "transport-belt",
    "automation-science-pack", "logistic-science-pack",
}
_STATIC_RECIPES = {
    "iron-gear-wheel", "stone-furnace", "burner-mining-drill", "wooden-chest",
    "iron-chest", "burner-inserter", "transport-belt",
    "copper-cable", "electronic-circuit", "inserter", "pipe", "small-electric-pole",
    "assembling-machine-1", "electric-mining-drill", "underground-belt", "splitter",
}


def _known(kind: str) -> set[str]:
    cat = catalog.get()
    if kind == "entity":
        return _STATIC_ENTITIES | set(cat.entities)
    if kind == "item":
        return _STATIC_ITEMS | set(cat.items)
    if kind == "item_or_fluid":
        return _STATIC_ITEMS | set(cat.items) | set(cat.fluids)
    if kind == "recipe":
        return _STATIC_RECIPES | _STATIC_MACHINE_RECIPES | set(cat.recipes)
    if kind == "technology":
        return set(cat.technologies)
    if kind == "resource":
        return {"iron-ore", "copper-ore", "coal", "stone"} | set(cat.resources)
    raise ValueError(kind)


def _name(kind: str):
    label = kind.replace("_", " ")

    def check(v: str) -> str:
        if not catalog.NAME_RE.match(v) or v not in _known(kind):
            raise ValueError(f"unknown {label} name")
        return v
    return AfterValidator(check)


EntityName = Annotated[str, _name("entity")]
ItemName = Annotated[str, _name("item")]
ItemOrFluidName = Annotated[str, _name("item_or_fluid")]
RecipeName = Annotated[str, _name("recipe")]
AssemblerRecipe = RecipeName
TechName = Annotated[str, _name("technology")]
ResourceName = Annotated[str, _name("resource")]
Direction = Literal["north", "east", "south", "west"]
Coord = Annotated[float, Field(ge=-5000, le=5000, allow_inf_nan=False)]

VERBS = ("observe", "move", "place", "place_line", "insert", "extract",
         "advance", "craft", "mine", "rotate", "pickup", "set_recipe",
         "research", "stats", "scan", "batch")
READ_ONLY_VERBS = ("observe", "stats", "scan")     # never change the game; they use the observe budget


# Belts (place and place_line). Every belt placement reports `merges`: each join it makes with a belt that is
# not part of it. carries: what the belt is for; observe then flags anything else on it as foreign_items.
# isolated: refuse (would_merge, nothing left placed) any join except with a tile listed in allow.


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Observe(_Strict):
    """What is around the character (or around x, y on levels with an observe range)."""
    type: Literal["observe"]
    radius: int = Field(16, ge=1, le=rules.MAX_OBSERVE_RADIUS)
    x: Coord | None = None
    y: Coord | None = None
    include: list[Literal["entities", "resources", "summary", "obstacles", "water"]] | None = Field(
        None, min_length=1, max_length=5)
    detail: Literal["full", "brief"] = "full"
    only_problems: bool = False
    names: list[EntityName] | None = Field(None, min_length=1, max_length=20)
    limit: int = Field(2000, ge=1, le=5000)
    # belts: each belt's two lanes ({item: count} each) and the belts it feeds and is fed by
    belts: bool = False

    @model_validator(mode="after")
    def _centre(self) -> "Observe":
        if (self.x is None) != (self.y is None):
            raise ValueError("give both x and y, or neither")
        return self


class Move(_Strict):
    type: Literal["move"]
    x: Coord
    y: Coord


class Place(_Strict):
    type: Literal["place"]
    entity: EntityName
    x: Coord
    y: Coord
    direction: Direction = "north"
    # underground-belt only: "input" goes down, "output" comes back up; both face the way items travel
    belt_type: Literal["input", "output"] | None = None
    carries: list[ItemName] | None = Field(None, min_length=1, max_length=4)
    isolated: bool = False
    allow: list[tuple[Coord, Coord]] | None = Field(None, max_length=600)

    @model_validator(mode="after")
    def _belt_type(self) -> "Place":
        if self.entity == "underground-belt" and self.belt_type is None:
            raise ValueError("an underground-belt needs belt_type: input or output")
        if self.entity != "underground-belt" and self.belt_type is not None:
            raise ValueError("belt_type is only for underground-belt")
        return self


class PlaceLine(_Strict):
    """Place a straight run of belts (facing the direction of travel) or pipes from (x1, y1) to (x2, y2)."""
    type: Literal["place_line"]
    entity: EntityName

    @field_validator("entity")
    @classmethod
    def _line_entity(cls, v: str) -> str:
        kind = (catalog.get().entities.get(v) or {}).get("type")
        if v not in rules.LINE_ENTITIES and kind not in ("transport-belt", "pipe"):
            raise ValueError("place_line lays belts or pipes")
        return v
    x1: Coord
    y1: Coord
    x2: Coord
    y2: Coord
    carries: list[ItemName] | None = Field(None, min_length=1, max_length=4)
    isolated: bool = False
    allow: list[tuple[Coord, Coord]] | None = Field(None, max_length=600)

    @model_validator(mode="after")
    def _straight(self) -> "PlaceLine":
        dx = math.floor(self.x2) - math.floor(self.x1)
        dy = math.floor(self.y2) - math.floor(self.y1)
        if dx != 0 and dy != 0:
            raise ValueError("line must be horizontal or vertical")
        if max(abs(dx), abs(dy)) + 1 > rules.MAX_LINE_LENGTH:
            raise ValueError(f"line longer than {rules.MAX_LINE_LENGTH} tiles")
        return self


class Insert(_Strict):
    type: Literal["insert"]
    x: Coord
    y: Coord
    item: ItemName
    count: int = Field(ge=1, le=5000)


class Extract(_Strict):
    type: Literal["extract"]
    x: Coord
    y: Coord
    item: ItemName
    count: int = Field(ge=1, le=5000)


class Advance(_Strict):
    type: Literal["advance"]
    ticks: int = Field(ge=1, le=216_000)


class Craft(_Strict):
    type: Literal["craft"]
    recipe: RecipeName
    count: int = Field(1, ge=1, le=500)


class Mine(_Strict):
    type: Literal["mine"]
    x: Coord
    y: Coord
    count: int = Field(1, ge=1, le=100)


class Rotate(_Strict):
    type: Literal["rotate"]
    x: Coord
    y: Coord
    direction: Direction


class Pickup(_Strict):
    type: Literal["pickup"]
    x: Coord
    y: Coord


class SetRecipe(_Strict):
    """Set the recipe of the assembling machine at x, y (automation level)."""
    type: Literal["set_recipe"]
    x: Coord
    y: Coord
    recipe: AssemblerRecipe


class Research(_Strict):
    """Set what the labs research (tech-tree levels): replace the queue, or append to it."""
    type: Literal["research"]
    technologies: list[TechName] = Field(min_length=1, max_length=16)
    mode: Literal["replace", "append"] = "replace"


class Stats(_Strict):
    """The production panel: totals and per-minute rates of every item and fluid, research, milestones."""
    type: Literal["stats"]
    items: list[ItemOrFluidName] | None = Field(None, min_length=1, max_length=200)
    minutes: float = Field(5, ge=0.5, le=60)


class Scan(_Strict):
    """Every resource patch and the nearest water within radius of the character (levels with a scan radius)."""
    type: Literal["scan"]
    radius: int = Field(128, ge=8, le=rules.MAX_SCAN_RADIUS)
    resources: list[ResourceName] | None = Field(None, min_length=1, max_length=10)
    min_tiles: int = Field(4, ge=1, le=1000)


BatchOp = Annotated[Union[Place, PlaceLine, Insert, Extract, Rotate, SetRecipe], Field(discriminator="type")]


class Batch(_Strict):
    """Several instant actions applied in order on one tick. Each op costs the level's batch_op_gap ticks of
    the rate limit; by default the batch stops at the first op that fails."""
    type: Literal["batch"]
    ops: list[BatchOp] = Field(min_length=1, max_length=rules.MAX_BATCH_OPS)
    stop_on_error: bool = True


Action = Annotated[
    Union[Observe, Move, Place, PlaceLine, Insert, Extract, Advance, Craft, Mine, Rotate, Pickup, SetRecipe,
          Research, Stats, Scan, Batch],
    Field(discriminator="type"),
]


class ActRequest(_Strict):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$",
                            description="Client-chosen unique id; resending the same id never runs the action twice.")
    action: Action


class StartRunRequest(_Strict):
    task: str = Field(pattern=r"^[a-z0-9-]{1,40}$")
    mode: Literal["practice", "eval"] = "practice"

    @field_validator("task")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.lower()


class Outcome(BaseModel):
    """What a backend reports for one action."""
    ok: bool
    code: str | None = None          # machine-readable rejection reason
    message: str = ""
    data: dict = Field(default_factory=dict)
    tick: int | None = None          # realtime: the game tick the action was applied at

    @classmethod
    def success(cls, **data) -> "Outcome":
        return cls(ok=True, data=data)

    @classmethod
    def fail(cls, code: str, message: str = "") -> "Outcome":
        return cls(ok=False, code=code, message=message or code.replace("_", " "))


# Game-level rejection codes (the action reached the game and the game said no).
REJECTION_CODES = {
    "not_in_inventory", "out_of_reach", "cannot_place", "no_entity", "no_resource",
    "cannot_insert", "nothing_to_extract", "inventory_full", "not_enough_ingredients",
    "insufficient_ticks", "too_far", "blocked", "forbidden_target", "cannot_rotate",
    "budget_exhausted", "no_recipe",
    # tech-tree levels
    "recipe_locked", "not_hand_craftable", "no_research", "unknown_technology", "already_researched",
    "technology_disabled", "trigger_technology", "cannot_research", "out_of_range", "no_scan", "not_batchable",
    "no_path",
}
