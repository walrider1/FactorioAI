# Logistics that scale: lines, a bus, and inputs as nodes

Status: phases 0–5 built 2026-11-20 (unit-tested, checked against the minute-334 save; not yet run in the real
game). The splitter bus in phase 3 is deferred; see below.
Written 2026-11-20 after run `20261120-092648-dead3d` stalled.

## Built 2026-11-24: belts stay, sources change (less upkeep)

In the five long runs, 60–100% of goal time after game minute 120 went into replacing mines and factories and
relinking. Belts were tied to both their ends, so every mined-out 2-drill mine pulled up its belts, and every
relink pulled up the belts other factories tapped. Now:

- **Removing a source keeps its outgoing belts** (`crew._detach` → `_keep_outbound`). The source key goes into
  `crew.gone_roots()` and counts as giving 0/min. `boost_links` then feeds a new source into the belt's side
  (a splice). A `replace` holds its new source back for this (`crew._replacing`), so the replacement mine feeds
  the old belts and no consumer is touched. If no source can be spliced in within `SPLICE_WAIT_MIN` (10), the
  belts come up as before (`expire_gone`).
- **A relink never takes up a belt that others tap or feed into.** It feeds more into it instead (`boost_links`).
- **Triage:** a starving input on a kept belt is fixed with `link` (splice), never with a relink. Once spliced,
  it is judged by what the belt gets.
- **Fewer, bigger mines:** iron, copper and coal rows are at least 6 units (4 into chests; `supply.MIN_UNITS`).
  Rebuilds of mined-out rows are at least that big. A row is rebuilt once a third of its drills stand dry (not
  the first end drill) or its ore runway is under 45 minutes.
- **Not built:** placing factories along a line with inserter taps. The splice removes the main reason a factory
  needed a new route (its source moving), so this waits until runs show walled-in factories are still common.

## What was built (phases 0–2)

- **Phase 0**:
  - `brain/nodes.py` (port report, plate ledger, consumer gate). The gate lets a held build through after 30 game
    minutes, so nothing deadlocks for good.
  - Hooked into the main loop, the assessor, `supply.py`, alerts (`plate-routing-*`) and all three planners'
    reports and prompts.
  - Removal empties chests into storage first and may make up to 40 trips to storage (it was 4).
  - The link step lists every failure and every port it skipped.
  - On the minute-334 save it reports the 16 blocked iron ports and site 25's six idle chests, and holds back
    another green factory.
- **Phase 1**:
  - `ElectricMine` and ore `SmelterColumn`s lay a line once underground belts and long-handed inserters exist
    (`modules.PLATE_LINES`; `line: false` opts out).
  - A line lives in `site.params["line"]`. Its tail is a source in `crew.outputs()`; its first link runs on from
    the tail with no inserter; up to 6 more ports tap it with inserters.
  - Chest rows with idle chests are turned into lines where they stand (`crew.rows_for_lines`, `to_line`), at
    most one per link step. Rows with two or more links are left alone.
  - `_plates_first` sizes new rows by rate.
- **Differences from the plan below**:
  - No tail chest: the crew takes plates from a line row's furnaces, which back up with whatever its links don't
    draw.
  - Steel columns keep their chests, because the crew crafts from them.
  - Merging a second row into the first row's line is not built yet.
- **Phase 2**:
  - `fab` has a `feed` cell, `recipes.plan(feeds=...)` and `place_sources(side=...)`. The router keeps each feed's
    two tiles for its own item.
  - `Factory` takes plates as feeds facing the nearest line, and falls back to chest inputs when a feed design
    fails (`Factory.FEEDS`).
  - On open ground, green science at 20/min goes from failing with 9 chest ports to designing with 2 feeds on one
    side.
  - On 54 saved plate-fed terrain cases: chests designed 38, feeds 41 with half the ports, and feeds with the
    chest fallback 45.
  - A feed has no chest, so the courier can't stock it: a feed factory runs only once its belts arrive.

## What was built (phases 3–5)

- **Phase 3, districts instead of a splitter bus**:
  - `crew.supply_spots` finds where a fed factory's plates can come from: lines with spare first, then free
    plate chests, for the plate it needs most that has a source. `crew.build` and prefetch pass them as
    `supply_at`.
  - `Factory._fed_places` tries open ground 16–24 tiles from those spots before the base's open ground, and
    the feeds face the nearest line. Each factory sits by its own supply: mine → line → feed.
  - `supply.shortfalls` subtracts stranded plates, so no more mines are asked for while made plates sit
    unbelted.
  - A new row's line is a spare source, so `boost_links` side-loads it into a chain that runs short. That is
    how rows merge, on demand.
- **Phase 3, deferred**:
  - The splitter bus. The API can place splitters (2×1), but the link router has no splitter routing, so taps
    off a bus would be inserters (~45/min each), the same bottleneck as chests.
  - Building the bus means splitter-aware routing in `belts.route_belt` and `crew._plan_link`, plus a reserved
    corridor. It's worth doing once a single line can't carry a district's plates (over ~450/min per lane).
- **Phase 4**:
  - `nodes.ledger` lists every line: what it carries, what is drawn, what is spare, the sites it feeds, its
    start tile. It is in the strategist, assessor and overseer reports as `lines`, with prompt text saying to
    reason in lines and ports.
  - New verb `{"do": "relink", "site": id, "item"?}`, accepted by all three planners. The strategist also may
    now send `upgrade`, `scout` and `repair`, which its goal filter dropped before.
- **Phase 5**:
  - `relink` re-lays one site's inputs without touching the site.
  - `crew.line_rate` follows the furnaces: a steel-furnace upgrade raises a row's line from 18.75 to 30 per
    unit, so `upgrade` now shows up in the ledger and the budgets.
  - Removal in batches came with phase 0, build-new-first replace already existed, and idle factories are
    already retired by `upgrade.py`.
  - Not built: moving a single feed, rebuilding one section of a factory, and yellow → red belts on lines.
    Lines carry at most ~225/min (12 units) for now, under half a lane.

## Why (what the run showed)

At game minute 334 the base made ~150 iron plates/min, yet every green-science factory starved:

- **Links:** 45 point-to-point belt links, 1,489 belts in total (33 per link on average, up to 110).
- **Iron ports:** 26 asked for, 16 unfed. Every factory that needs 1 iron port is fed; the ones that need 4–5
  are not (green 37/56/72, gears 58, inserters 59).
- **Idle iron:** iron mine site 25 has 6 full plate chests nothing draws from.
- **Planners:** the overseer, assessor and strategist see "iron 169 made vs 148 used", so they keep building new
  consumers, each with more unfed ports.
- **Replacing big sites:** it takes 30+ minutes and ends in "inventory full" (site 37: 325 buildings; site 56:
  444).

Run `20261119-202811` failed the same way: 7 factories (~1,900 buildings) were torn down. The fix then only
stopped a factory from walling in its own ports; the cause stayed.

## The weak link: everything is point-to-point

| Layer | Today | Where |
|---|---|---|
| Supply | One chest per drill (or per column furnace), ~18.75/min, **one link max** | `modules.ElectricMine.build`, `outposts.SmelterColumn.build`, `crew._free_outputs` (`cap`) |
| Demand | **One input cell per 18.75/min** of plates, ports facing every way | `fab/recipes.plan` (line ~111), `fab/place.place_sources` |
| Wiring | One A* belt per port, laid afterwards through leftover ground; only a 6-tile lane per port is reserved | `crew._link_port`, `_plan_link`, `fab/design.port_escapes` |
| Placement | The most open ground near the base centre, blind to where inputs come from | `modules.Factory._open_places` |
| Visibility | Totals only; a port with no usable source is skipped without a word; only 3 failures reported | `crew.link`, `assess.situation` |
| Rework | Replace = tear down the whole site, then rebuild | `crew._remove`, `replace` |

So the number of belts grows with ports × distance, every belt blocks the next one, and nothing reserves room.
A typical player avoids this by collecting first (many drills onto one belt), running a bus, and putting
consumers on the bus.

## Target model

- **Line**: one belt carrying one item, with a head (where producers load it), a path, taps, and a ledger:
  `supply/min` (what its loaders give), `booked/min` (what its taps take), `capacity` (yellow 900/min, 450 per
  lane), `spare`. Lines are first-class records (`crew.lines`), not anonymous link belts.
- **Port**: one input of one site: item, rate, which line or link feeds it, and its state: `fed`, `short`, `unfed`
  (no source with spare), `blocked` (no route), or `walled`, plus why. Ports are planned, fixed and reported one at
  a time, not as a whole factory.
- **Bus**: a corridor of parallel lines (iron, copper; later steel, circuits) from the smelting area into open
  ground, kept clear with a margin. Factories sit along it, each taking one tap per input item on the side facing
  the bus.
- **District**: a self-contained group: mine → column → line → factory. Used when the bus is full or the inputs are
  far away. Only the product goes home: a green factory takes ~125 iron/min and makes ~20 packs/min.
- **Remote smelting fuel:** electric furnaces need advanced-material-processing-2 (blue science), so they come
  late. Until then a far district either belts ore home (what the outposts already do) or smelts on site with
  **coal on the second lane of the ore belt**. One belt carries both, so there's no separate coal route and no
  supply runs by the character.

## Phases

Each phase ships on its own, behind a feature flag where it changes the build. Old links keep working: lines are
added alongside them, never swapped in all at once mid-run.

### Phase 0: stop the bleeding (small, low risk)

Makes the problem visible and stops the planners digging deeper. No new building types.

1. **Port report.** `crew.port_report()` lists every unfed port with a reason, including ports `link()` skips
   today (`not srcs and not taps` → `unfed: no source with spare`). It also lists **stranded sources**: chests
   that are full or nearly full with no link. Feeds `assess.situation`, the overseer report and `alerts()`.
2. **Delivered vs made, per plate.** Made = source rates. Delivered = sum of the rates of fed ports. Stranded =
   rates of unlinked full chests. The planners see "iron: 169 made, ~70 reaches factories, ~95 stranded at site 25"
   instead of a single surplus number.
3. **Consumer gate.** While an item has unfed ports, no new build that consumes that item is allowed. This covers
   factory, workshop and replace-to-bigger, from any planner: the goal is refused with the port report and the
   fix ("connect the stranded supply first"). It goes in `bot.py` next to `forbidden()`, and `supply.py` respects it.
4. **Removal that fits.** Before a remove or replace, estimate the stack slots of what comes back. If it doesn't
   fit, empty into storage chests first and remove in batches (build-new-first replaces cut over, then remove the
   old one in batches). No more half-removed sites ending in "inventory full".
5. **Show every link failure.** `crew.link` reports the count and all ports in a compact form, not the first 3.

Tests: unit tests on a fake base built from the minute-334 save's sites/links (pickle →
fixture). Check the gate refuses factory 72, and the report names site 25's 6 chests.

### Phase 1: plate lines (aggregate the supply side)

1. **`ElectricMine` `into: "line"`** (default once belts are available). The per-unit chest tile `(cx, cy-5)`
   becomes a belt flowing toward the row end nearest the base, so every unit's inserter drops onto one belt. The
   footprint is the same: the coal belt stays on `cy-6`, and the long-handed fuel inserter still reaches over.
   The row end continues as the line head. Rate = units × furnace rate.
2. **Merge rows.** A second row on the same patch side-loads into the first row's line (up to lane capacity)
   instead of starting a new source.
3. **`SmelterColumn` `into: "line"`.** The plate chest column (`bx-5`) becomes an output belt down the column. This
   is the same change for outposts and steel.
4. **`crew.lines` + ledger**: create, extend, tap, book, release. `_budget`/`_fits` read line spare instead of
   per-chest rates. A line is one source with real capacity, not `cap = 1`.
5. **Retrofit** existing rows to lines in place: pick up the chests (plates go to storage) and lay belts on the
   same tiles. This is cheap and runs as a close-out step.
6. **Crew pickups.** `_plate_sources`, the courier and hand-plates read from a **tail chest** at each line's end
   (an inserter off the line into a chest), not from mine chests.

Tests: a mine-row unit test for the new footprint, merging rows, the ledger math, the retrofit, and crew pickups
from the tail chest.

### Phase 2: one feed per input (aggregate the demand side, `fab/`)

1. **New cell kind `feed`**: an input that is a belt entering the design box on one edge tile, routed inside like
   any net. The entry tile is the net's producer pin. There's no chest and no inserter bottleneck, so one feed
   carries a lane (450/min), enough for any single factory's plates.
2. **`recipes.plan`**: plates (and any bulk input) get **one feed per item**, sized to the belt, not one cell per
   furnace. Chest `src` cells stay for low-volume inputs that the courier may stock (steel, plastic).
3. **Feed side.** `design_factory(..., feed_side="west")` puts every feed on one edge, in a fixed item order. The
   tiles from the box edge to the line are reserved in the design (keepout), so they can't be walled in later.
4. **Output port.** The product goes to a chest as now, plus an optional output belt port on a stated side (labs,
   the next factory).
5. **Bench first.** Run `fab/bench.py` over every saved terrain case (`runs/*/chiplets/`). Acceptance: green
   science at 20/min and circuits at 30/min design with ≤ 1 feed per item on most cases. Before swapping the
   default, compare against today's success rate.

### Phase 3: placement by supply: the bus and districts

1. **Lay a bus** when the first two plate lines exist (logistics researched). Iron and copper lines run in parallel
   from the smelting area in a straight line into open ground. The corridor plus 1 tile each side goes into
   `world.keepout`/`lanes` so nothing is built on it. Splitters (from logistics) make taps; an inserter tap is the
   fallback for small rates.
2. **`Factory` placement**: `_places_on_bus(inputs)` gives the next free slots along the bus where every input line
   passes with enough spare, feeds facing the bus. This replaces `_open_places` for fed factories.
3. **District fallback.** No bus slot, or the inputs exceed the bus's spare → build a district at the nearest
   spare patch: outpost (or mine row) → column into line → the factory next to it. Its product goes home over one
   belt, or by courier at low rates. Fuel: a coal lane on the ore belt.
4. **Grow at the head.** When a bus line runs short, new supply (row, outpost trunk) joins that line's head.
   `supply.py` sizes it against line spare, not totals.

### Phase 4: the node ledger in the planners

1. **`crew.ledger()`**: per line (item, supply, booked, spare, taps) and per site (each input and output
   separately, with state and why). It replaces `stuck_links`/`short_belts` in `assess.situation`, the overseer
   report and the strategist report.
2. **New verbs**: `extend_line {item, toward}`, `tap {site, item}`, `relink {site, port}`,
   `move_input {site, port}`, `add_supply {line}`. Free-form `link` becomes internal (the crew does it after
   builds).
3. **Prompts** (brain strategist, `assess.ASSESS_PROMPT`, `overseer` prompt): reason in lines and ports; never build
   a consumer onto a line without spare; connect stranded supply before building new supply.

### Phase 5: rework and upgrades by segment

1. **Partial operations**: re-tap one input, move one feed, rebuild one section of a factory.
2. **Upgrade lines**: yellow → red belts (logistics-2) when booked > 450/lane, and stone → steel furnaces on line
   rows (`upgrade.py` already swaps machines in place).
3. **Replace = build new on the bus, move the taps over, remove the old in inventory-sized batches.** The base
   keeps running throughout.
4. **Retire** idle factories and lines; their parts go into the next build.

## Order and size

| Phase | Size | Depends on | Payoff |
|---|---|---|---|
| 0 | small | — | stops the "build more, starve more" loop at once |
| 1 | medium | — | 6–12 drills become one source; links drop sharply |
| 2 | large (`fab/`) | — (bench-gated) | a factory needs 1 belt per input, not 5 |
| 3 | large | 1, 2 | factories land where their inputs are; space stays reserved |
| 4 | medium | 1 (ideally 3) | the planners stop fighting routing they can't see |
| 5 | medium | 1–3 | the base can be reworked without stalling |

Phases 1 and 2 are independent and can be built side by side. 0 goes first.

## Decisions taken (change here if wrong)

- No trains or cars in this API, so belts are the only long-haul transport.
- Yellow belts until logistics-2; lane accounting at 450/min (side-loading fills one lane).
- Tail chests keep a small buffer for the crew's own crafting; lines themselves have no buffer.
- Old point-to-point links are never torn down to migrate: they are retrofitted where they stand, or left until
  their factory is reworked.

## Verifying

Unit tests and the fab bench per phase. Real-game checks are run by the user (never started from a session):

```
cd freeplay && .venv/Scripts/arena local-run --backend factorio --task freeplay-open --bot brain \
  --strategist openai --placer llm --designs ai --clock-speed 2 \
  --resume 20261120-092648-dead3d:m00334-auto-133703
```

Phase 2's bench runs offline with `arena chiplet-bench` on the saved terrain cases.

Resuming the minute-334 save of `20261120-092648-dead3d` is the before/after case for phases 0–1.

## Built 2026-11-24 (second patch): no bans on short supply, real outposts, oil always found

From run 20261124-015025 (13 of 21 milestones; stalled on iron and oil):

- **The overseer cannot ban a shortage fix.** A goal that adds supply of an item made under 90% of what its ports
  want (or with ports that have no source, or coal burned faster than mined), or that replaces a mine running dry,
  runs whatever is forbidden (`overseer.relieves`); such a forbid is refused when given, and the overseer sees
  `short_supply` in its report. The run had iron banned three times while it made 38/min against 207 wanted.
- **No hand-fed burners after electric drills** (`supply.mining_options`); every electric row of iron, copper or
  coal is at least 6 drills (4 into chests) whoever asks (`ElectricMine._count`).
- **Outposts are big and go out with their whole kit.** Iron and copper outposts are 16+ drills, big patches
  first, a band under 10 drills only when nothing bigger is known. The smelting column, the trunk home and the
  pole line are made before the walk (`Crew._outpost_extra`, held in `outpost_hold`). Iron or copper with under
  150 game minutes of ore left (or a full patch while short) queues the outpost by the rule floor at once
  (`ore-soon-*` alerts).
- **Oil is always found.** A pumpjack goal searches farther out when every known well is taken
  (`world.oil_wells(more=True)`), so does a crude scout; a workshop that finds no crude for it builds its
  pumpjacks inside the same goal (`Crew._crude_first`), and an oil workshop shares a floor that already has a
  crude line instead of failing on "every pumpjack already feeds another floor".
- **Splices:** kept belts are joined head-on where their inserter stood, and belts are only kept when a source to
  feed in exists (else relinked at once, not after 10 starved minutes).
- **Small:** mine and column chests no longer count as boiler chests (the "feed site None" loop); research goals
  have a 25-minute budget (the labs go on regardless).

## Built 2026-11-24 (third patch, mid-run in 20261124-120854): labs fed, copper read right, no long waits

- **A lab block short of a colour taps a belt that carries it** (`Crew._tap_short_stations`). Station 41 got red only;
  the one free green chest was boxed in by other belts, and the science linker never offered the router the three
  green belts into site 26, so 41's labs sat idle. Every station now takes each colour the base makes, from a free
  chest if one routes, else from a tap on another station's belt of it.
- **"No room on the patch" is keyed on that ore's own mines** (`Crew._patch_sig`). It was keyed on every site, so the
  next unrelated build cleared it, the ore outlook said "room for more" and copper's next patch never came up.
- **Stranded ore is not sent to a far patch** (`assess._stranded`). When 30%+ of an ore's drills have nowhere to put
  their output, the plate is short for want of belts: the ore-soon outpost rule stays out of it.
- **A goal steps aside when its research is far off** (`Crew._research_eta`, `RESEARCH_DEFER_MIN` = 8). A pumpjack goal
  waited 16 game minutes on oil-gathering creeping to 53%. Research that moves but needs more than 8 more minutes at
  the pace seen keeps running; the goal fails with the time it needs and comes back later. Packs made by hand still
  keep the crew there.
- Stream: the world map under the thoughts (ore patches, oil wells, sites, the character; far patches as arrows on
  the edge with their distance), from `world` in the overlay data (`oil_wells` added to bot_live.json). Alt view (the
  game's "show details") is on for every spectator who joins (scenario `on_player_joined_game`, display only).

## Built 2026-11-24 (fourth patch, mid-run in 20261124-120854): blue science not thrown away

- **A science output goes to a station a belt reaches** (`Crew.link_science`). Ports were ranked by "has the most
  other colours" before distance, with no reach check: blue from workshop 64 was handled first, while station 65
  (60 tiles off) had no colour yet, so all three tries went to station 50's ports 233+ tiles away, failed, and the
  back-off kept blue from station 65 for good. Ports beyond `belts.LINK_REACH` now come last.
- **Furnaces get no set_recipe** (`Workshop.build`, `gaps.repair`). The game takes set_recipe on assemblers only (a
  furnace answers `no_entity`), so workshop 64's two steel furnaces counted as never placed and every repair
  retried the same refused call.
- **Courier-fed workshop ports are not "starving"** (`nodes.starving`). Workshop 64's 23 ports have no belt by
  design (the courier carries), yet they held the engine, circuit and blue factories back, and the overseer had the
  workshop torn down "to unblock construction".
- **The only working maker of an item is replaced, not removed** (`Crew.removal_blocker`, sole maker). The overseer
  removed workshop 64 (26 machines working, its first blue packs out at minute 400, research at 35% from hand-carried
  packs) on "blue science is still zero". A plain remove of the last factory/workshop making an item, with machines
  working, is refused with "replace it instead" (make before break); a replace whose new site stands may remove it.

## Built 2026-11-24 (fifth patch, mid-run in 20261124-120854): far sources get their trunk by themselves

- **Link reach raised to 220 tiles (routes up to 300 belts)** (`belts.LINK_REACH`, `LINK_ROUTE_MAX`), from 150/200.
  Links still run inside other goals, so the cap stays: the 460-tile walks of run 20261121-140755 stay out.
- **A link stopped by distance alone queues its trunk** (`assess.far_link_fix`, alert key `trunk-…`, queued by the
  rule floor without pausing the goal in hand). Before, the failure only said "needs a trunk goal" and waited for a
  planner to read it. A failed trunk records its own reason, so it is not queued again until the link step has
  failed on reach once more (its back-off); sources over 600 tiles off get no automatic trunk (make it there).
- **A trunk can run into a research station** (`Crew._station_trunk_target`): any free port takes the colour. Before,
  "trunk site 50 chemical-science-pack" answered "no unfed input port" and the far blue workshop never reached a lab.
  A science link that fails is kept against the nearest station with a free port, so the trunk goes there.
- Trunk goal budget 30 → 40 game minutes (longer belts now come this way).

## Built 2026-11-24 (sixth patch, end of 20261124-120854): a factory too big for one block is split

- **A belt factory that fits nowhere as one block splits off its biggest intermediate** (`Crew.build`,
  `Crew._split_candidate`, at most `SPLIT_MAX` = 2 times). Advanced circuits from plates (4 circuit and 6
  advanced-circuit assemblers plus three input belts) found no belt route in 8 designs at 4 places, and blue science
  failed after 26 minutes. Now the circuits get a factory of their own first (`_intermediates_first`) and the
  advanced-circuit factory takes them by belt. Only a failure naming this item's own layout splits it.
- **A science link failure whose source chest is gone is dropped** (`Crew._prune_link_fails`): removed workshop 64's
  blue chest kept a stuck link, and the far-link alert sent a trunk for packs nothing made any more.
