"""Productivity modules: more out of the same inputs, where it counts most.

The rocket silo's research needs productivity-module-3 anyway (and speed-module-3), so by the silo the modules are
unlocked for free. A productivity module in the silo makes 10% (tier 1) to 40% (tier 3, four slots) more rocket
parts from the same parts; in science and rocket-intermediate assemblers it does the same for research and the
rocket's ingredients. Players fill the silo first, then science, then the rocket's intermediates.

    module_item(kn)        the best productivity module researched, or None
    targets(crew)          (site, machines, slots each): what still takes modules, best payoff first
    alerts(crew)           one rule-floor alert at a time (key modules-<site>): the next site to fill, once modules
                           can be had (on hand, or advanced circuits are made in the base)
    Crew.fit_modules       makes the modules (crafting from advanced circuits) and inserts them

Productivity is allowed on intermediates, science packs and rocket parts only (the game refuses it elsewhere: an
insert into a machine on another recipe is refused and counted as done).
"""

from __future__ import annotations

MODULES = ("productivity-module-3", "productivity-module-2", "productivity-module")
SLOTS = {"rocket-silo": 4, "assembling-machine-2": 2, "assembling-machine-3": 4}
SCIENCE = ("automation-science-pack", "logistic-science-pack", "military-science-pack", "chemical-science-pack",
           "production-science-pack", "utility-science-pack")
ROCKET = ("processing-unit", "low-density-structure", "rocket-fuel", "rocket-control-unit")
INTERMEDIATE = ("advanced-circuit", "electronic-circuit", "engine-unit", "electric-engine-unit", "plastic-bar",
                "sulfur", "battery", "iron-gear-wheel", "copper-cable", "steel-plate", "solid-fuel", "explosives")
# what a factory makes, best payoff first (its rank): the silo comes before everything
RANK = {**{k: 1 for k in SCIENCE[3:]}, **{k: 2 for k in ROCKET}, **{k: 3 for k in SCIENCE[:3]},
        **{k: 4 for k in INTERMEDIATE}}
MAX_PER_GOAL = 12               # modules one goal makes and fits at most (each takes 5 advanced circuits)


def module_item(kn) -> str | None:
    unlocked = getattr(kn, "unlocked", None) or set()
    return next((m for m in MODULES if m in unlocked), None)


def _machines(site) -> list[tuple]:
    return [e for e in site.entities if e[0] in SLOTS]


def targets(crew) -> list[tuple]:
    """Sites that still take modules: the silo, then factories by RANK. [(site, machines, slots each)]."""
    out = []
    for s in crew.base.sites:
        if s.params.get("modules_done"):
            continue
        machines = _machines(s)
        if not machines:
            continue
        if s.kind == "silo" or any(e[0] == "rocket-silo" for e in machines):
            rank = 0
        elif s.kind == "factory" and s.params.get("item") in RANK:
            rank = RANK[s.params["item"]]
        else:
            continue
        out.append((rank, s.id, s, machines))
    return [(s, m, [SLOTS[e[0]] for e in m]) for _, _, s, m in sorted(out, key=lambda q: (q[0], q[1]))]


def _can_have(crew, item: str, need: int) -> bool:
    """Modules on hand, or the base makes advanced circuits (the crew crafts the modules from them)."""
    if crew.w.inventory.get(item, 0) >= min(need, 4):
        return True
    items = (getattr(crew.w, "stats", None) or {}).get("items") or {}
    return float((items.get("advanced-circuit") or {}).get("made_per_min") or 0) > 0


def alerts(crew) -> list[dict]:
    try:
        item = module_item(crew.kn)
    except Exception:  # noqa: BLE001 - stub crews in tests
        return []
    if item is None:
        return []
    for site, machines, slots in targets(crew):
        need = min(MAX_PER_GOAL, sum(slots))
        if not _can_have(crew, item, need):
            return []
        what = "the rocket silo" if any(e[0] == "rocket-silo" for e in machines) else \
            f"factory site {site.id} ({site.params.get('item')})"
        return [{"level": "urgent", "key": f"modules-{site.id}",
                 "what": f"{item} is researched and {what} has none: {need} module(s) give "
                         f"{'10' if item == 'productivity-module' else '40' if item.endswith('3') else '24'}% more "
                         "out of the same inputs per filled machine", "fix": {"do": "modules", "site": site.id}}]
    return []
