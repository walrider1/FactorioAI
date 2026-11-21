"""The game's own data: every item, fluid, recipe, technology and buildable entity (config/catalog.json).

The organizer generates the file once per game version with ``arena catalog`` (it boots a game and asks the
scenario to dump its prototypes). The server uses it to:

- validate names: a name a team sends must be in the catalog (and match NAME_RE), so free text still never
  reaches Lua, without hand-maintaining whitelists for the hundreds of names a whole game has;
- snap building positions for any entity size;
- publish it to bots (GET /v1/catalog), so a planner works from the real recipe numbers.

Without the file the server falls back to the small built-in lists in ``rules`` (the original levels).
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "catalog.json"


class Catalog:
    def __init__(self, data: dict):
        self.data = data
        self.items: dict[str, dict] = data.get("items", {})
        self.fluids: dict[str, dict] = data.get("fluids", {})
        self.recipes: dict[str, dict] = data.get("recipes", {})
        self.technologies: dict[str, dict] = data.get("technologies", {})
        self.entities: dict[str, dict] = data.get("entities", {})
        self.resources: dict[str, dict] = data.get("resources", {})
        self.hand_categories: set[str] = set((data.get("character") or {}).get("crafting_categories", ["crafting"]))

    @property
    def empty(self) -> bool:
        return not self.items

    def dims(self, entity: str) -> tuple[int, int] | None:
        e = self.entities.get(entity)
        return (int(e["width"]), int(e["height"])) if e else None

    @staticmethod
    def categories(r: dict) -> list[str]:
        return list(r.get("categories") or ([r["category"]] if r.get("category") else []))

    def hand_craftable(self, recipe: str) -> bool:
        r = self.recipes.get(recipe)
        if not r or not set(self.categories(r)) & self.hand_categories:
            return False
        return all(i["type"] == "item" for i in r["ingredients"]) and all(p["type"] == "item" for p in r["products"])

    def machine_recipes(self) -> set[str]:
        """Recipes some buildable machine can be set to (assemblers, chemical plants, refineries...)."""
        cats = {c for e in self.entities.values() if e["type"] == "assembling-machine"
                for c in (e.get("crafting_categories") or [])}
        return {n for n, r in self.recipes.items() if set(self.categories(r)) & cats}


@lru_cache(maxsize=4)
def load(path: str | None = None) -> Catalog:
    p = Path(path) if path else DEFAULT_PATH
    if not p.exists():
        return Catalog({})
    return Catalog(json.loads(p.read_text(encoding="utf-8")))


def get() -> Catalog:
    return load(None)
