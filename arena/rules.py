"""Game constants shared by validation, the simulator and the docs.

Numbers match vanilla Factorio 2.0 so the simulator and the real server agree.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

TICKS_PER_SECOND = 60

# Character
CHARACTER_SPEED = 0.15          # tiles per tick (running speed, no exoskeleton)
REACH = 10.0                    # build / interact reach in tiles
RESOURCE_REACH = 3.0            # hand-mining reach in tiles (vanilla is 2.7)
HAND_MINING_SPEED = 0.5
HAND_CRAFTING_SPEED = 1.0
CHARACTER_SLOTS = 80
MAX_MOVE_DISTANCE = 200.0
MAX_OBSERVE_RADIUS = 64         # hard cap; each task sets its own limit (limits.observe_radius, default 20)
MAX_LINE_LENGTH = 32
MAX_SCAN_RADIUS = 512           # hard cap; each task sets limits.scan_radius (0 = no scan)
MAX_BATCH_OPS = 100             # hard cap; each task sets limits.batch_max_ops (0 = no batch)

DIRECTIONS = ("north", "east", "south", "west")

STACK_SIZE: dict[str, int] = {
    "iron-ore": 50, "copper-ore": 50, "coal": 50, "stone": 50, "wood": 100,
    "iron-plate": 100, "copper-plate": 100, "stone-brick": 100,
    "iron-gear-wheel": 100,
    "burner-mining-drill": 50, "stone-furnace": 50, "wooden-chest": 50,
    "iron-chest": 50, "burner-inserter": 50, "transport-belt": 100,
    # automation level
    "copper-cable": 200, "electronic-circuit": 200, "automation-science-pack": 200,
    "logistic-science-pack": 200, "electric-mining-drill": 50, "assembling-machine-1": 50,
    "inserter": 50, "long-handed-inserter": 50, "small-electric-pole": 50, "offshore-pump": 20,
    "boiler": 50, "steam-engine": 10, "pipe": 100,
    "underground-belt": 50, "splitter": 50,
}

FUEL_VALUE: dict[str, float] = {"coal": 4_000_000.0, "wood": 2_000_000.0}

RESOURCES: dict[str, float] = {   # resource -> mining time
    "iron-ore": 1.0, "copper-ore": 1.0, "coal": 1.0, "stone": 1.0,
}


@dataclass(frozen=True)
class Smelt:
    ingredient: str
    ingredient_count: int
    product: str
    seconds: float


SMELTING: dict[str, Smelt] = {
    "iron-ore": Smelt("iron-ore", 1, "iron-plate", 3.2),
    "copper-ore": Smelt("copper-ore", 1, "copper-plate", 3.2),
    "stone": Smelt("stone", 2, "stone-brick", 3.2),
}


@dataclass(frozen=True)
class Recipe:
    ingredients: dict[str, int]
    product: str
    product_count: int
    seconds: float


HAND_RECIPES: dict[str, Recipe] = {
    "iron-gear-wheel": Recipe({"iron-plate": 2}, "iron-gear-wheel", 1, 0.5),
    "stone-furnace": Recipe({"stone": 5}, "stone-furnace", 1, 0.5),
    "burner-mining-drill": Recipe(
        {"iron-gear-wheel": 3, "stone-furnace": 1, "iron-plate": 3}, "burner-mining-drill", 1, 2.0),
    "wooden-chest": Recipe({"wood": 2}, "wooden-chest", 1, 0.5),
    "iron-chest": Recipe({"iron-plate": 8}, "iron-chest", 1, 0.5),
    "burner-inserter": Recipe({"iron-plate": 1, "iron-gear-wheel": 1}, "burner-inserter", 1, 0.5),
    "transport-belt": Recipe({"iron-plate": 1, "iron-gear-wheel": 1}, "transport-belt", 2, 0.5),
    # real-time level: hand-craft more buildings from plates you take out of your own factory
    "copper-cable": Recipe({"copper-plate": 1}, "copper-cable", 2, 0.5),
    "electronic-circuit": Recipe({"iron-plate": 1, "copper-cable": 3}, "electronic-circuit", 1, 0.5),
    "inserter": Recipe({"electronic-circuit": 1, "iron-gear-wheel": 1, "iron-plate": 1}, "inserter", 1, 0.5),
    "pipe": Recipe({"iron-plate": 1}, "pipe", 1, 0.5),
    "small-electric-pole": Recipe({"wood": 1, "copper-cable": 2}, "small-electric-pole", 2, 0.5),
    "assembling-machine-1": Recipe({"electronic-circuit": 3, "iron-gear-wheel": 5, "iron-plate": 9},
                                   "assembling-machine-1", 1, 0.5),
    "electric-mining-drill": Recipe({"electronic-circuit": 3, "iron-gear-wheel": 5, "iron-plate": 10},
                                    "electric-mining-drill", 1, 2.0),
    "underground-belt": Recipe({"iron-plate": 10, "transport-belt": 5}, "underground-belt", 2, 1.0),
    "splitter": Recipe({"electronic-circuit": 5, "iron-plate": 5, "transport-belt": 4}, "splitter", 1, 1.0),
}


# Recipes an assembling machine may be set to (automation level). seconds = recipe time at speed 1;
# assembling-machine-1 runs at speed 0.5, so it takes twice as long.
ASSEMBLER_RECIPES: dict[str, Recipe] = {
    "iron-gear-wheel": Recipe({"iron-plate": 2}, "iron-gear-wheel", 1, 0.5),
    "copper-cable": Recipe({"copper-plate": 1}, "copper-cable", 2, 0.5),
    "electronic-circuit": Recipe({"iron-plate": 1, "copper-cable": 3}, "electronic-circuit", 1, 0.5),
    "inserter": Recipe({"electronic-circuit": 1, "iron-gear-wheel": 1, "iron-plate": 1}, "inserter", 1, 0.5),
    "transport-belt": Recipe({"iron-plate": 1, "iron-gear-wheel": 1}, "transport-belt", 2, 0.5),
    "automation-science-pack": Recipe({"copper-plate": 1, "iron-gear-wheel": 1}, "automation-science-pack", 1, 5.0),
    "logistic-science-pack": Recipe({"inserter": 1, "transport-belt": 1}, "logistic-science-pack", 1, 6.0),
}


@dataclass(frozen=True)
class EntitySpec:
    name: str
    size: int                    # footprint in tiles (square), or the width when height is set
    kind: str                    # drill | furnace | chest | inserter | belt
    energy_watts: float = 0.0
    speed: float = 0.0           # mining speed (drill) or crafting speed (furnace)
    chest_slots: int = 0
    mining_time: float = 0.2     # time to pick the entity back up
    sim: bool = True             # supported by the simulator backend
    drop_vector_north: tuple[float, float] | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)
    height: int = 0              # non-square entities: footprint height when facing north (0 = size)

    def dims(self, direction: str = "north") -> tuple[int, int]:
        """(width, height) of the footprint for this facing."""
        w, h = self.size, self.height or self.size
        return (h, w) if direction in ("east", "west") else (w, h)


ENTITIES: dict[str, EntitySpec] = {
    "burner-mining-drill": EntitySpec(
        "burner-mining-drill", 2, "drill", energy_watts=150_000, speed=0.25,
        mining_time=0.3, drop_vector_north=(-0.5, -1.3)),
    "stone-furnace": EntitySpec(
        "stone-furnace", 2, "furnace", energy_watts=90_000, speed=1.0, mining_time=0.2),
    "wooden-chest": EntitySpec("wooden-chest", 1, "chest", chest_slots=16, mining_time=0.1),
    "iron-chest": EntitySpec("iron-chest", 1, "chest", chest_slots=32, mining_time=0.2),
    "burner-inserter": EntitySpec("burner-inserter", 1, "inserter", mining_time=0.1, sim=False),
    "transport-belt": EntitySpec("transport-belt", 1, "belt", mining_time=0.1, sim=False),
    # automation level (real game only)
    "electric-mining-drill": EntitySpec("electric-mining-drill", 3, "drill", energy_watts=90_000, speed=0.5,
                                        mining_time=0.3, sim=False),
    "assembling-machine-1": EntitySpec("assembling-machine-1", 3, "assembler", energy_watts=75_000, speed=0.5,
                                       mining_time=0.2, sim=False),
    "inserter": EntitySpec("inserter", 1, "inserter", mining_time=0.1, sim=False),
    "long-handed-inserter": EntitySpec("long-handed-inserter", 1, "inserter", mining_time=0.1, sim=False),
    "small-electric-pole": EntitySpec("small-electric-pole", 1, "pole", mining_time=0.1, sim=False),
    "offshore-pump": EntitySpec("offshore-pump", 1, "pump", mining_time=0.1, sim=False),
    "boiler": EntitySpec("boiler", 3, "boiler", height=2, mining_time=0.2, sim=False),
    "steam-engine": EntitySpec("steam-engine", 3, "generator", height=5, mining_time=0.3, sim=False),
    "pipe": EntitySpec("pipe", 1, "pipe", mining_time=0.1, sim=False),
    "underground-belt": EntitySpec("underground-belt", 1, "belt", mining_time=0.1, sim=False),
    "splitter": EntitySpec("splitter", 2, "splitter", height=1, mining_time=0.1, sim=False),
}

LINE_ENTITIES = ("transport-belt", "pipe")      # what place_line can lay

FURNACE_OUTPUT_CAP = 100         # one output slot = one stack of plates
FURNACE_SOURCE_CAP = 50          # one source slot = one stack of ore
FUEL_SLOT_CAP = 50               # one fuel slot = one stack of coal

ALL_ITEMS = tuple(sorted(STACK_SIZE))


def _snap_axis(v: float, n: int) -> float:
    return float(round(v)) if n % 2 == 0 else math.floor(v) + 0.5


def dims(entity: str, direction: str = "north") -> tuple[int, int]:
    """(width, height) of an entity's footprint for this facing: built-in specs, else the game catalog."""
    if entity in ENTITIES:
        return ENTITIES[entity].dims(direction)
    from . import catalog
    wh = catalog.get().dims(entity) or (1, 1)
    return (wh[1], wh[0]) if direction in ("east", "west") else wh


def snap(entity: str, x: float, y: float, direction: str = "north") -> tuple[float, float]:
    """Snap a position to the grid the way Factorio does for buildings (per axis, rotation aware)."""
    w, h = dims(entity, direction)
    return _snap_axis(x, w), _snap_axis(y, h)


def rotate_vector(vec: tuple[float, float], direction: str) -> tuple[float, float]:
    x, y = vec
    for _ in range(DIRECTIONS.index(direction)):
        x, y = -y, x
    return x, y


def move_ticks(distance: float) -> int:
    return math.ceil(distance / CHARACTER_SPEED)


def hand_mine_ticks(resource: str, count: int) -> int:
    per = math.ceil(RESOURCES[resource] / HAND_MINING_SPEED * TICKS_PER_SECOND)
    return per * count


def craft_ticks(recipe: str, count: int) -> int:
    return math.ceil(HAND_RECIPES[recipe].seconds * TICKS_PER_SECOND / HAND_CRAFTING_SPEED * count)


def pickup_ticks(entity: str) -> int:
    return math.ceil(ENTITIES[entity].mining_time / HAND_MINING_SPEED * TICKS_PER_SECOND)
