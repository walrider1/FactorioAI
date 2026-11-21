"""Remote mining: find the next patch before the near ones run out, mine it, and belt it home.

There are no trains or cars in this game's API, so an outpost is what players build without trains: electric
drills on a far patch dropping onto one collector belt, a pole line home (crew.connect lays it, as it does for
oil), one long trunk belt, and at the base end a smelting column (or, for coal, a row of depot chests) whose
chests are sources like any mine's.

    outlook(crew)         per ore: how much is left on the patches we work, how fast it goes, whether there is
                          room for more drills, the next patch known, and what to do about it (think ahead)
    scout(w, ...)         walk out in rings until a patch of the ore that we do not work yet shows up
    Outpost               the far end: a row of electric drills on one side of a collector belt (so the ore rides
                          on one lane), poles over the drills, the trunk's first tile kept free
    SmelterColumn         the near end: the trunk runs down a column; an inserter per furnace takes ore from it,
                          a long-handed inserter takes coal from a coal belt of its own (a coal port the link step
                          feeds like any furnace row), plates go into a chest per furnace. For coal (and with
                          into: chest) inserters fill depot chests instead
    lay_trunk(crew, ...)  the long belt between them, routed in a corridor along the straight line (so a
                          400-tile route stays a small search) with undergrounds once logistics is in
"""

from __future__ import annotations

import math

from .modules import Module, Site, coal_belts_ready, foreign_ore, line_record, lines_ready, mixes_ore
from .world import BuildError, _same_patch

ORES = ("iron-ore", "copper-ore", "coal", "stone")
PRODUCT = {"iron-ore": "iron-plate", "copper-ore": "copper-plate", "stone": "stone-brick",
           "iron-plate": "steel-plate"}
# what one furnace takes in a minute (ore, or plates for steel: five a steel, 16 s at speed 1)
FURNACE_IN = {"stone-furnace": 18.75, "steel-furnace": 37.5}
RUNWAY_MIN = 240.0              # game minutes of ore left on worked patches before the next patch is prepared
DRILL_PER_MIN = 30.0            # an electric drill's ore a minute
POLE = "small-electric-pole"


# ------------------------------------------------------------------ the patches we work, and the next ones
def _box_near(box, pt, margin: float = 8.0) -> bool:
    return box[0] - margin <= pt[0] <= box[2] + margin and box[1] - margin <= pt[1] <= box[3] + margin


def worked(base, patch: dict) -> bool:
    """Do we mine this patch already (a mine of its ore stands on it)?"""
    return any(s.kind in ("electric_mine", "burner_mine", "outpost") and s.params.get("resource") == patch["name"]
               and _box_near(patch["box"], s.centre) for s in base.sites)


def spare_patches(w, base, resource: str, min_tiles: int = 40) -> list[dict]:
    """Patches of the ore nobody works yet, nearest the base first."""
    home = base.centre or w.pos
    out = [p for p in w.patches if p["name"] == resource and p.get("tiles", 0) >= min_tiles and not worked(base, p)]
    return sorted(out, key=lambda p: math.dist((p["centre"]["x"], p["centre"]["y"]) if p.get("centre")
                                               else (p["nearest"]["x"], p["nearest"]["y"]), home))


def outlook(crew) -> dict:
    """Per ore: what is left where we mine, how fast it goes, room for more, the next patch, and the move."""
    w, base = crew.w, crew.base
    home = base.centre or w.pos
    out = {}
    for r in ORES:
        mine = [p for p in w.patches if p["name"] == r and worked(base, p)]
        if not mine and not any(s.params.get("resource") == r for s in base.sites):
            continue                                  # not mined at all yet: nothing to look ahead for
        amount = sum(int(p.get("amount") or 0) for p in mine)
        rate = w.rate(r) if hasattr(w, "rate") else 0.0
        left = amount / rate if rate > 0 and amount else None
        full = crew.patch_full(r) if hasattr(crew, "patch_full") else False
        spare = spare_patches(w, base, r)
        nxt = None
        if spare:
            p = spare[0]
            c = (p["centre"]["x"], p["centre"]["y"]) if p.get("centre") else (p["nearest"]["x"], p["nearest"]["y"])
            nxt = {"distance": round(math.dist(c, home)), "tiles": p.get("tiles"), "amount": p.get("amount")}
        short = full or (left is not None and left < RUNWAY_MIN)
        fix = None
        if short:
            fix = {"do": "build", "module": "outpost", "resource": r, "count": 8} if spare \
                else {"do": "scout", "resource": r}
        out[r] = {"worked_patches": len(mine), "amount_left": amount, "mined_per_min": round(rate, 1),
                  "minutes_left": round(left) if left is not None else None, "room_for_more": not full,
                  "next_patch": nxt, "fix": fix}
    return out


def scout(w, resource: str, worked_boxes: list, min_tiles: int = 60, max_rings: int = 2,
          home: tuple[float, float] | None = None) -> dict | None:
    """Walk out like a player leaving the start area (8 directions a ring, around `home`: the base) until a patch
    of the ore shows up that is none of `worked_boxes`. Returns it, or None. Crude oil never forms such a patch
    (single-tile wells): Crew.scout_oil handles it."""
    def new() -> dict | None:
        ps = [p for p in w.patches if p["name"] == resource and p.get("tiles", 0) >= min_tiles
              and not any(_same_patch(p["box"], b) for b in worked_boxes)]
        return min(ps, key=lambda p: p.get("distance", 1e9)) if ps else None
    found = new()
    if found is not None:
        return found
    home = tuple(home) if home else w.pos
    step = w.oil_step() if hasattr(w, "oil_step") else 150.0
    for ring in range(1, max_rings + 1):
        for a in range(8):
            ang = a * math.pi / 4
            w.doing(f"scouting for {resource} (ring {ring}, direction {a + 1}/8)")
            try:
                w.go(home[0] + ring * step * math.cos(ang), home[1] + ring * step * math.sin(ang))
            except BuildError:
                continue
            w.sleep_ticks(720)                         # the map is generated and charted around us every 10 s
            w.scan()
            found = new()
            if found is not None:
                return found
    return None


# ------------------------------------------------------------------ the far end
class Outpost(Module):
    name = "outpost"
    describe = ("remote mining for when the patches near the base are full or running low (see ore_outlook): "
                "a row of electric drills on the nearest patch of `resource` nobody works yet, dropping onto one "
                "collector belt, a pole line home, a trunk belt to a smelting column built at the base edge "
                "(plates into chests there, its furnaces fed coal by belt; coal goes into depot chests instead). "
                "The crew builds all three in one goal. Scout first if no spare patch is known. "
                "args: {resource, count (drills, 2-14)}")

    def kit(self, w, base, p):
        n = max(2, min(14, int(p.get("count", 8))))
        return {"electric-mining-drill": n, "transport-belt": 3 * n + 2, POLE: n // 2 + 2}

    @staticmethod
    def unit(cx: int, cy: int) -> dict:
        """A north-facing drill whose ore lands on the collector belt 2 rows up (on its south lane)."""
        return {"drill": {(cx + dx, cy + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)},
                "belt": [(cx - 1, cy - 2), (cx, cy - 2), (cx + 1, cy - 2)], "pole": (cx + 1, cy + 2)}

    def _row(self, w, resource: str, n: int, flow: int, box=None) -> list[tuple[int, int]]:
        ore = w.resources.get(resource, set())
        if box is not None:                            # this patch only, not every tile of the ore we know
            ore = {t for t in ore if box[0] - 1 <= t[0] <= box[2] + 1 and box[1] - 1 <= t[1] <= box[3] + 1}
        foreign = foreign_ore(w, resource)
        best: list = []
        for sx, sy in sorted(ore)[:4000]:
            run, cx = [], sx
            while len(run) < n:
                u = self.unit(cx, sy)
                need = u["drill"] | set(u["belt"]) | ({u["pole"]} if len(run) % 2 == 0 else set())
                if sum(t in ore for t in u["drill"]) < 7 or mixes_ore(cx, sy, foreign) or not w.free(need):
                    break
                run.append((cx, sy))
                cx += 3
            if run:                                    # the trunk's first tile past the head must be free too
                head_x = run[-1][0] + 2 if flow > 0 else run[0][0] - 2
                if not w.free({(head_x, sy - 2)}):
                    run = run[:-1] if flow > 0 else run[1:]
            if len(run) > len(best):
                best = run
                if len(best) >= n:
                    break
        return best

    def build(self, w, base, p):
        r, n = p["resource"], max(2, min(14, int(p.get("count", 8))))
        if not base.powered_poles():
            raise BuildError("an outpost needs the power network to reach it: build steam_power first")
        cands = spare_patches(w, base, r)
        if not cands:
            raise BuildError(f"no {r} patch known that nobody works yet: scout first "
                             f'({{"do": "scout", "resource": "{r}"}})')
        home = base.centre or w.pos
        for q in cands[:3]:
            c = (q["centre"]["x"], q["centre"]["y"]) if q.get("centre") else (q["nearest"]["x"], q["nearest"]["y"])
            flow = -1 if home[0] < c[0] else 1         # the collector runs toward the base
            w.survey_area(*c, half=min(48, max(16, int(math.sqrt(q.get("tiles", 100))))), step=32)
            units = self._row(w, r, n, flow, q.get("box"))
            if len(units) >= 2:
                break
        else:
            raise BuildError(f"no room for a row of 2+ drills on the spare {r} patches known")
        cy = units[0][1]
        site = Site(self.name, {"resource": r, "count": 0, "outpost": True}, c)
        d = "east" if flow > 0 else "west"
        for k, (cx, _) in enumerate(units):
            u = self.unit(cx, cy)
            for t in (u["belt"] if flow > 0 else reversed(u["belt"])):
                w.place("transport-belt", t[0] + 0.5, t[1] + 0.5, d, {t})
            w.place("electric-mining-drill", cx + 0.5, cy + 0.5, "north", u["drill"])
            if k % 2 == 0 and w.free({u["pole"]}):
                w.place(POLE, u["pole"][0] + 0.5, u["pole"][1] + 0.5, "north", {u["pole"]})
                site.poles.append(u["pole"])
            site.params["count"] += 1
        head = (units[-1][0] + 1, cy - 2) if flow > 0 else (units[0][0] - 1, cy - 2)
        start = (head[0] + flow, head[1])
        site.params.update({"head": list(head), "head_dir": d, "trunk_to": None,
                            "per_minute": DRILL_PER_MIN * site.params["count"]})
        site.centre = (sum(u[0] for u in units) / len(units) + 0.5, cy + 0.5)
        w.keepout |= {start}
        return site


# ------------------------------------------------------------------ the near end
class SmelterColumn(Module):
    name = "smelter_column"
    describe = ("a column of furnaces fed from one belt, coal from a belt of their own, products into chests. "
                "With resource iron-plate it makes STEEL by belt (iron plates linked in like any input, steel "
                "into chests kits and links take from): the way to the silo's 1000 steel and purple science's "
                "rails. With an ore it is the base end of an outpost's trunk (the outpost goal builds that one). "
                "args: {resource, count (furnaces), toward: [x, y]}")

    @staticmethod
    def _into(p: dict) -> str:
        return p.get("into") or ("chest" if p["resource"] == "coal" else "furnace")

    @staticmethod
    def furnace(w) -> str:
        unlocked = getattr(w.kn, "unlocked", set()) or set()
        return "steel-furnace" if "steel-furnace" in unlocked else "stone-furnace"

    def layout(self, w, p: dict) -> str:
        """chest (depot chests off the trunk), furnace (a product chest per furnace) or line (every furnace's
        product onto one belt down the column): furnace columns are lines once belts can cross."""
        into = self._into(p)
        # steel stays in chests: the crew takes it from them for its own crafting (a line would carry it away)
        if into == "furnace" and p["resource"] in ORES and p.get("line", True) \
                and lines_ready(getattr(w, "kn", None)):
            return "line"
        return into

    def kit(self, w, base, p):
        n = max(1, min(24, int(p.get("count", 8))))
        lay = self.layout(w, p)
        if lay == "chest":
            return {"iron-chest": n, "inserter": n, "transport-belt": n + 2, POLE: n // 4 + 2}
        out = {self.furnace(w): n, "inserter": 2 * n, "long-handed-inserter": n, "iron-chest": n,
               "transport-belt": 4 * n + 4, POLE: n + 2}
        if lay == "line":
            out.pop("iron-chest")
            out["transport-belt"] += 2 * n + 3
        return out

    def tiles(self, bx: int, by: int, n: int, into: str) -> set:
        rows = n if into == "chest" else 2 * n
        xs = range(bx - 2, bx + 1) if into == "chest" else range(bx - 6, bx + 1)
        out = {(x, y) for x in xs for y in range(by, by + rows)}
        out |= {(bx, by - 1), (bx, by - 2)}
        if into != "chest":
            out |= {(bx - 6, by - 1), (bx - 6, by - 2)}
        if into == "line":                       # where the line's first link continues on
            out |= {(bx - 5, by + rows), (bx - 5, by + rows + 1)}
        return out

    def build(self, w, base, p):
        r, into = p["resource"], self.layout(w, p)
        n = max(1, min(24, int(p.get("count", 8))))
        if not base.powered_poles():
            raise BuildError("a smelting column needs power: build steam_power first")
        home = tuple(p.get("near") or base.centre or w.pos)     # where its inputs are, when known
        toward = tuple(p.get("toward") or home)
        trunk = bool(p.get("trunk", r in ORES))               # fed by an outpost, not by the link step
        spot = None
        for size in range(n, 0, -1):
            for ring in range(1, 9):
                rr = 12 * ring
                cands = [(int(home[0] + dx), int(home[1] + dy)) for dx in range(-rr, rr + 1, 12)
                         for dy in range(-rr, rr + 1, 12) if max(abs(dx), abs(dy)) == rr]
                for bx, by in sorted(cands, key=lambda q: (math.dist(q, home) * 0.6 + math.dist(q, toward) * 0.4)):
                    area = self.tiles(bx, by, size, into)
                    w.survey_area(bx - 3, by + size, half=max(12, size + 6), step=32)
                    if w.free(area):
                        spot = (bx, by)
                        break
                if spot:
                    break
            if spot:
                n = size
                break
        if spot is None:
            raise BuildError(f"no clear ground near the base for a smelting column of {n}")
        bx, by = spot
        rows = n if into == "chest" else 2 * n
        site = Site(self.name, {"resource": r, "count": n, "into": into}, (bx - 2.5, by + rows / 2))
        for t in range(rows):                          # the trunk's last stretch: down the column
            w.place("transport-belt", bx + 0.5, by + t + 0.5, "south", {(bx, by + t)})
        product = PRODUCT.get(r, r) if into != "chest" else r
        fur = self.furnace(w)
        for k in range(n):
            if into == "chest":
                y = by + k
                w.place("inserter", bx - 0.5, y + 0.5, "east", {(bx - 1, y)})         # picks from the belt
                w.place("iron-chest", bx - 1.5, y + 0.5, "north", {(bx - 2, y)})
                site.chests.append((bx - 1.5, y + 0.5, product))
                if k % 4 == 0 and w.free({(bx - 3, y)}):
                    w.place(POLE, bx - 2.5, y + 0.5, "north", {(bx - 3, y)})
                    site.poles.append((bx - 3, y))
                continue
            y = by + 2 * k
            ftiles = {(bx - 3, y), (bx - 2, y), (bx - 3, y + 1), (bx - 2, y + 1)}
            w.place(fur, bx - 2.0, y + 1.0, "north", ftiles)
            site.burners.append((bx - 2.0, y + 1.0))
            site.plate_furnaces.append((bx - 2.0, y + 1.0, product))
            w.fuel(bx - 2.0, y + 1.0, 5)
            w.place("inserter", bx - 0.5, y + 0.5, "east", {(bx - 1, y)})             # ore off the trunk
            w.place("inserter", bx - 3.5, y + 0.5, "east", {(bx - 4, y)})             # plates out of the furnace
            if into == "line":                      # onto the column's line, two tiles per furnace
                for t in ((bx - 5, y), (bx - 5, y + 1)):
                    w.place("transport-belt", t[0] + 0.5, t[1] + 0.5, "south", {t})
            else:
                w.place("iron-chest", bx - 4.5, y + 0.5, "north", {(bx - 5, y)})
                site.chests.append((bx - 4.5, y + 0.5, product))
            w.place("long-handed-inserter", bx - 3.5, y + 1.5, "west", {(bx - 4, y + 1)})   # coal from 2 west
            for t in ((bx - 6, y), (bx - 6, y + 1)):
                w.place("transport-belt", t[0] + 0.5, t[1] + 0.5, "south", {t})
            if k % 2 == 0:
                for q in ((bx - 1, y + 1),) + (((bx - 5, y + 1),) if into != "line" else ()):
                    if w.free({q}):
                        w.place(POLE, q[0] + 0.5, q[1] + 0.5, "north", {q})
                        site.poles.append(q)
        if into == "line":
            tiles = [(bx - 5, by + t) for t in range(2 * n)]
            start = (bx - 5, by + 2 * n)
            lanes = getattr(w, "lanes", None)
            if lanes is not None:
                lanes |= {(bx - 5, by + 2 * n + k) for k in range(3)}
            each = (FURNACE_IN.get(fur, 18.75) / 5) if r == "iron-plate" else FURNACE_IN.get(fur, 18.75)
            site.params["line"] = line_record(product, tiles, "south", start, each * n)
        per_in = 0.0 if trunk else round(FURNACE_IN.get(fur, 18.75) * n, 1)
        ports = [{"item": r, "end": [bx, by - 1], "approach": [bx, by - 2], "fed": None, "per_minute": per_in,
                  **({"trunk": True} if trunk else {})}]
        if into != "chest":
            ports.append({"item": "coal", "end": [bx - 6, by - 1], "approach": [bx - 6, by - 2], "fuel": True,
                          "per_minute": round(1.4 * n, 1), "fed": None})
            site.params["fuel"] = "belt" if coal_belts_ready(getattr(w, "kn", None)) else "hand"
        site.params["ports"] = ports
        for pt in ports:
            w.keepout |= {tuple(pt["end"]), tuple(pt["approach"])}
        return site


# ------------------------------------------------------------------ the long belt between them
def _near_line(t, a, b, width: float) -> bool:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    u = 0.0 if L2 == 0 else max(0.0, min(1.0, ((t[0] - ax) * dx + (t[1] - ay) * dy) / L2))
    return math.dist(t, (ax + u * dx, ay + u * dy)) <= width


TRUNK_WIDTHS = (12, 28, 56)     # corridor half-widths tried in turn


def plan_trunk(crew, start: tuple, port: dict) -> list | None:
    """The trunk's route: belts from `start` into the port, inside a corridor along the straight line."""
    from . import belts
    end, appr = tuple(port["end"]), tuple(port["approach"])
    others = {tuple(t) for s, pt in crew.all_ports() if pt is not port for t in (pt["end"], pt["approach"])}
    mine = {start, end, appr}
    ug = "underground-belt" in crew.kn.unlocked
    for width in TRUNK_WIDTHS:
        free = lambda t, wd=width: ((t in mine or crew._belt_free(t)) and t not in crew._bad_belt
                                    and _near_line(t, start, appr, wd))
        steps = belts.route_belt(free, start, appr, end, avoid=others - mine, underground=ug, margin=width,
                                 limit=600_000, ug_block=crew.ug_cover() if ug and hasattr(crew, "ug_cover") else None)
        if steps:
            return steps
    return None


def lay_trunk(crew, outpost: Site, column: Site) -> dict:
    """Survey along the line, route the trunk, lay it (taken up again if the game refuses a tile), and record
    it as a link of kind trunk into the column's ore port."""
    from . import belts
    head, d = tuple(outpost.params["head"]), outpost.params["head_dir"]
    vx, vy = belts.VEC[d]
    start = (head[0] + vx, head[1] + vy)
    port = next(pt for pt in column.params.get("ports", []) if pt.get("trunk"))
    appr = tuple(port["approach"])
    n = max(1, int(math.dist(start, appr) // 40))
    crew.w.doing(f"surveying a trunk belt from outpost {outpost.id} to site {column.id}")
    for k in range(n + 1):
        x, y = start[0] + (appr[0] - start[0]) * k / n, start[1] + (appr[1] - start[1]) * k / n
        try:
            crew.w.look(x, y, radius=32)
        except BuildError:
            pass
    steps = plan_trunk(crew, start, port)
    if not steps:
        raise BuildError(f"no trunk route from outpost {outpost.id} to site {column.id} "
                         f"({round(math.dist(start, appr))} tiles"
                         + ("" if "underground-belt" in crew.kn.unlocked else "; research logistics for undergrounds")
                         + ")")
    need = belts.count(steps)
    crew.unlock_items([k for k, v in need.items() if v])
    crew.make({k: v for k, v in need.items() if v}, extra=True)
    ents: list = []
    crew.w.recorder = ents
    try:
        with belts.job_for(crew.w, [port.get("item") or outpost.params.get("resource")], belts.tiles_of(steps),
                           belts.link_joins(steps, from_belt=True)):     # it runs on from the outpost's head
            belts.lay_belt(crew.w, steps)
    except BuildError as exc:
        crew.w.recorder = None
        bad = getattr(exc, "tile", None)
        if bad is not None:
            crew._bad_belt.add(tuple(bad))
        crew._undo(ents)
        raise
    finally:
        crew.w.recorder = None
    rec = {"source": head, "to_site": column.id, "item": port["item"], "belts": len(steps), "kind": "trunk",
           "port": port, "steps": steps, "ins": None, "entities": ents, "from": f"outpost site {outpost.id}",
           "minute": crew._minute(), "outpost": outpost.id}
    crew._register(rec)
    outpost.params["trunk_to"] = column.id
    column.params["outpost"] = outpost.id
    tiles = set(belts.tiles_of(steps))
    side = {(tx + dx, ty + dy) for tx, ty in tiles for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))} - tiles
    if hasattr(crew, "_reserve_margin"):
        crew._reserve_margin(outpost, side | set(getattr(crew, "site_lanes", {}).get(outpost.id, [])))
    return rec


def _register() -> None:
    from .modules import MODULES
    for m in (Outpost(), SmelterColumn()):
        MODULES.setdefault(m.name, m)


_register()
