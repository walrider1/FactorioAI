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

from .modules import (DRILL_REACH, Module, Site, coal_belts_ready, foreign_ore, line_record, lines_ready,
                      mixes_ore, reserved, thin_ground)
from .world import BuildError, _same_patch

ORES = ("iron-ore", "copper-ore", "coal", "stone")
PRODUCT = {"iron-ore": "iron-plate", "copper-ore": "copper-plate", "stone": "stone-brick",
           "iron-plate": "steel-plate"}
# what one furnace takes in a minute (ore, or plates for steel: five a steel, 16 s at speed 1)
FURNACE_IN = {"stone-furnace": 18.75, "steel-furnace": 37.5}
RUNWAY_MIN = 240.0              # game minutes of ore left on worked patches before the next patch is prepared
DRILL_PER_MIN = 30.0            # an electric drill's ore a minute
POLE = "small-electric-pole"
# Smelting next to the ore (the plates ride the belt out, the base keeps its room) when the field is this close to
# the base; farther out the column stays at the base end, where its coal belt is short.
FIELD_SMELT_MAX = 130.0
FIELD_ORE_MIN = 8               # ore tiles of its 5x5 mining area a drill of a dense field needs
FIELD_DRILLS = 16               # a dense field's drills by default: ~480 ore a minute, one belt's worth and change


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


def plan_field(crew, resource: str, drills: int = FIELD_DRILLS) -> dict | None:
    """Set a clean patch aside for a big build ahead of time: the largest spare patch of the ore near the base
    (nobody works it, no other ore mixed in at its centre), reserved so ordinary mines keep off it until the
    field stands. Returns the build goal for it, or None (no such patch known: the ore outlook scouts)."""
    w, base = crew.w, crew.base
    home = base.centre or w.pos
    key = f"plan-{resource}"
    fields = w.__dict__.setdefault("ore_fields", {})
    spare = [q for q in spare_patches(w, base, resource, min_tiles=120) if q.get("box")]
    if not spare:
        fields.pop(key, None)
        return None

    def far(q):
        c = (q["centre"]["x"], q["centre"]["y"]) if q.get("centre") else (q["nearest"]["x"], q["nearest"]["y"])
        return math.dist(c, home)
    near = [q for q in spare if far(q) <= 2 * FIELD_SMELT_MAX] or spare
    q = max(near, key=lambda q: (int(q.get("amount") or 0) or q.get("tiles", 0)) / (1.0 + far(q) / 100.0))
    fields[key] = list(q["box"])
    return {"do": "build", "module": "outpost", "resource": resource, "count": drills, "dense": True,
            "field": True, "box": list(q["box"]), "field_key": key}


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
    describe = ("mining a whole patch nobody works yet, for when the patches near the base are full or running "
                "low (see ore_outlook), and for big builds (purple and yellow science want hundreds of plates a "
                "minute): electric drills on BOTH sides of one collector belt (twice as dense as a mine row, "
                "every tile of the band mined), a pole line home, the ore smelted in a furnace column right beside "
                "the field when it is near the base (else at the base end of a trunk belt), its plates on one line "
                "out. The patch is set aside: ordinary mines keep off it. The crew builds it all in one goal. "
                "Scout first if no spare patch is known. args: {resource, count (drills, 4-30, default 16)}")

    @staticmethod
    def _n(p: dict) -> int:
        return max(2, min(30 if p.get("dense", True) else 14, int(p.get("count", FIELD_DRILLS))))

    def kit(self, w, base, p):
        n = self._n(p)
        if p.get("dense", True):
            return {"electric-mining-drill": n, "transport-belt": 2 * n + 6, POLE: n // 4 + 3}
        return {"electric-mining-drill": n, "transport-belt": 3 * n + 2, POLE: n // 2 + 2}

    # A dense band, seen from its collector belt at row y + 2 flowing toward the base: drills of the north row are
    # centred on row y and face south, drills of the south row on y + 4 and face north, so both drop onto the
    # belt (one lane each). Along the belt drills go in pairs, 3 tiles apart, then a free column for a pole
    # (x + 5 from a pair's first drill): pitch 7 per pair. The 5x5 mining areas overlap, so every ore tile of the
    # band (rows y - 2 .. y + 6) is mined. A pole in each free column, on row y + 1, powers both rows (its 5x5
    # supply area reaches the north drills and the top row of the south ones) and the next pole is 7 tiles on.
    @staticmethod
    def drill_x(sx: int, k: int) -> int:
        return sx + 7 * (k // 2) + 3 * (k % 2)

    @staticmethod
    def drill_tiles(cx: int, cy: int) -> set:
        return {(cx + dx, cy + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)}

    def _dense_ok(self, w, ore: set, foreign: set, cx: int, cy: int, own) -> bool:
        area = sum((cx + dx, cy + dy) in ore for dx in range(-DRILL_REACH, DRILL_REACH + 1)
                   for dy in range(-DRILL_REACH, DRILL_REACH + 1))
        tiles = self.drill_tiles(cx, cy)
        return area >= FIELD_ORE_MIN and not mixes_ore(cx, cy, foreign) and not thin_ground(w, cx, cy) \
            and not reserved(w, tiles, own) and w.free(tiles)

    def _dense(self, w, resource: str, n: int, flow: int, box=None, own=None, home=None) -> dict | None:
        """The best dense band of up to n drills on the patch: {"y", "sx", "drills": [(cx, cy, facing)],
        "belt": [tiles in flow order], "poles": [tiles]} or None."""
        ore = w.resources.get(resource, set())
        if box is not None:
            ore = {t for t in ore if box[0] - 1 <= t[0] <= box[2] + 1 and box[1] - 1 <= t[1] <= box[3] + 1}
        if not ore:
            return None
        foreign = foreign_ore(w, resource)
        xs = sorted({t[0] for t in ore})
        ys = sorted({t[1] for t in ore})
        best, best_score = None, None
        starts = [(sx, sy) for sy in range(ys[0], ys[-1] - 2) for sx in range(xs[0] - 1, xs[-1] - 1)]
        if len(starts) > 6000:                         # a huge patch: every other start is plenty
            starts = starts[::max(2, len(starts) // 6000)]
        for sx, sy in starts:
            drills, belt, poles = [], [], []
            pole0 = (sx - 2, sy + 1)
            if w.free({pole0}):
                poles.append(pole0)
            k = 0
            while len(drills) < n:
                cx = self.drill_x(sx, k)
                span = [(cx + dx, sy + 2) for dx in (-1, 0, 1)]
                gap = [(cx + 2, sy + 2)] if k % 2 else []
                if not w.free(set(span + gap)):
                    break
                here = []
                if self._dense_ok(w, ore, foreign, cx, sy, own):
                    here.append((cx, sy, "south"))
                if len(drills) + len(here) < n and self._dense_ok(w, ore, foreign, cx, sy + 4, own):
                    here.append((cx, sy + 4, "north"))
                if not here:
                    break
                drills += here
                belt += span + gap
                if k % 2 and w.free({(cx + 2, sy + 1)}):
                    poles.append((cx + 2, sy + 1))
                k += 1
            if len(drills) < 2:
                continue
            x0, x1 = belt[0][0], belt[-1][0]
            head_x = x1 + 1 if flow > 0 else x0 - 1    # the trunk (or the column's short link) starts past the end
            if not w.free({(head_x, sy + 2)}):
                continue
            mid = ((x0 + x1) / 2, sy + 2)
            score = (-len(drills), math.dist(mid, home) if home else 0)
            if best_score is None or score < best_score:
                ordered = sorted(belt, key=lambda t: t[0] * flow)
                best, best_score = {"y": sy, "sx": sx, "drills": drills, "belt": ordered, "poles": poles}, score
                if len(drills) >= n:
                    break
        return best

    def _build_dense(self, w, base, p, r: str, n: int, cands: list, home) -> Site:
        own = p.get("field_key")
        lay, c, q = None, None, None
        for q in cands[:3]:
            c = (q["centre"]["x"], q["centre"]["y"]) if q.get("centre") else (q["nearest"]["x"], q["nearest"]["y"])
            flow = -1 if home[0] < c[0] else 1         # the collector runs toward the base
            w.survey_area(*c, half=min(48, max(16, int(math.sqrt(q.get("tiles", 100))))), step=32)
            lay = self._dense(w, r, n, flow, q.get("box"), own, home)
            if lay is not None:
                break
        if lay is None:
            raise BuildError(f"no room for a dense band of 2+ drills on the spare {r} patches known")
        d = "east" if flow > 0 else "west"
        site = Site(self.name, {"resource": r, "count": 0, "outpost": True, "dense": True}, c)
        for t in lay["belt"]:
            w.place("transport-belt", t[0] + 0.5, t[1] + 0.5, d, {t})
        for cx, cy, facing in lay["drills"]:
            w.place("electric-mining-drill", cx + 0.5, cy + 0.5, facing, self.drill_tiles(cx, cy))
            site.params["count"] += 1
        for t in lay["poles"]:
            if w.free({t}):
                w.place(POLE, t[0] + 0.5, t[1] + 0.5, "north", {t})
                site.poles.append(t)
        head = lay["belt"][-1]
        start = (head[0] + flow, head[1])
        xs = [t[0] for t in lay["belt"]]
        box = [min(xs) - 2, lay["y"] - 2, max(xs) + 2, lay["y"] + 6]
        site.params.update({"head": list(head), "head_dir": d, "trunk_to": None, "box": box,
                            "per_minute": DRILL_PER_MIN * site.params["count"],
                            **({"field": True} if p.get("field") else {})})
        site.centre = (sum(xs) / len(xs) + 0.5, lay["y"] + 2.5)
        w.keepout |= {start}
        fields = w.__dict__.setdefault("ore_fields", {})
        fields.pop(own, None)                          # the plan's reservation becomes the field's own ground
        fields[f"field-{r}-{head[0]},{head[1]}"] = box
        return site

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
        r, n = p["resource"], self._n(p)
        if not base.powered_poles():
            raise BuildError("an outpost needs the power network to reach it: build steam_power first")
        cands = spare_patches(w, base, r)
        if p.get("box"):                               # the patch the plan set aside for it, if still known
            planned = [q for q in w.patches if q["name"] == r and q.get("box")
                       and _same_patch(q["box"], p["box"])]
            cands = planned + [q for q in cands if q not in planned]
        if not cands:
            raise BuildError(f"no {r} patch known that nobody works yet: scout first "
                             f'({{"do": "scout", "resource": "{r}"}})')
        home = base.centre or w.pos
        if p.get("dense", True):
            return self._build_dense(w, base, p, r, n, cands, home)
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
        # never on ore: a column beside a field (or anywhere) would sit on the vein it is meant to serve
        ore = set().union(*[t for k, t in (getattr(w, "resources", {}) or {}).items() if k != "crude-oil"])             if getattr(w, "resources", None) else set()
        for size in range(n, 0, -1):
            for ring in range(1, 9):
                rr = 12 * ring
                cands = [(int(home[0] + dx), int(home[1] + dy)) for dx in range(-rr, rr + 1, 12)
                         for dy in range(-rr, rr + 1, 12) if max(abs(dx), abs(dy)) == rr]
                for bx, by in sorted(cands, key=lambda q: (math.dist(q, home) * 0.6 + math.dist(q, toward) * 0.4)):
                    area = self.tiles(bx, by, size, into)
                    w.survey_area(bx - 3, by + size, half=max(12, size + 6), step=32)
                    if w.free(area) and not (area & ore) and not reserved(w, area):
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
