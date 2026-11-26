# The brain bot

An AI that plays Factorio from an empty inventory, in real time, through the arena API. The language model
is the brain: it thinks ahead and decides *what* the base needs. Code does everything that has to be fast
or exact, in the background, while the model thinks.

```
  STRATEGIST   LLM (or the free book)       rare: on milestones, when goals run out, after failures
     |  goals + doctrine, read against the computed ROADMAP to the rocket
     v
  SCHEDULER    code (crew.py)       kit -> unlock (trigger techs, research) -> craft -> gather -> build
     v
  MODULES      code                 burner_mine, steam_power, labs, factory (fab), pumpjack, electric_mine,
                                    workshop (any item: cells + fluid nets), silo
     v
  CREW         code (world.py)      walks, mines, crafts, places, at the rate limit, waiting for its hands
  FOREMAN      code (crew.py)       coal, refuelling, boiler chests, labs fed, and the COURIER for the workshop
  SUPERVISOR   rules + LLM          watches the base between plans, queues urgent fixes, pauses a goal
  FIXER        rules + LLM          troubleshoots failures that repeat: digs out, retries changed, sets aside
```

| File | What |
|---|---|
| `knowledge.py` | The game from `GET /v1/catalog`: recipes, what unlocks what, hand-crafting plans, tech paths, pipe ports |
| `world.py` | Map memory (scan, observes, trees, oil wells), inventory, research, the production panel, primitive moves |
| `purity.py` | Belts carry what they were laid for: links whose last belts hold foreign items are an urgent alert; `purge` stops the leak and cleans the belts. Prevention is `world.belt_job` (belts laid tagged and isolated) |
| `modules.py` | The early chiplets and the `Base` (every built module as a `Site`) |
| `production.py` | The planner: any item at a rate -> recipes, machines, counts, raw inputs, fluids; cracking and a petroleum sink so a refinery never stalls |
| `pipes.py` | The fluid router: one pipe net per fluid, crossing others with pipe-to-ground, never touching another fluid |
| `workshop.py` | The workshop floor (cells, corridors, walkways, fluid nets, the water pump and crude lines) and the silo |
| `roadmap.py` | The road to the rocket, computed: stages, research left, science rates, power, plates, and the next step |
| `crew.py` | Scheduler + crew + foreman + courier: one character, one thread, taking turns |
| `brain.py` | The one-page report, the free book (opening + rocket book) and the `LLMStrategist` |
| `assess.py` | The supervisor (rules + an optional fast model) |
| `fixer.py` | The fixer (rules + an optional model) |
| `bot.py` | The main loop; never lets one failed or crashed goal end the run |

```bash
uv run arena local-run --backend factorio --task freeplay-green --bot brain                     # free
uv run --extra openai arena local-run --backend factorio --task freeplay-green --bot brain \
    --strategist openai --model gpt-6.1-terra --max-cost 2
uv run arena local-run --backend factorio --task freeplay-open --bot brain --clock-speed 2      # twice real time
```

## How the LLM is used

- **Rarely.** The strategist is asked when its goals run out, when a milestone is reached, after two failed
  goals in a row, and at least every 8 game minutes. A one-hour game is about 10-25 calls.
- **In the background.** It plans on a worker thread; the crew keeps building and the foreman keeps tending
  the base, so thinking time costs nothing in game.
- **With a doctrine.** Each reply carries the model's standing plan in a few lines, which it gets back next
  time. A stateless model still plays one consistent game, and prompts stay small (one page).
- **At the right altitude.** The model never sees coordinates or single actions. It picks modules and
  research; the code turns that into hundreds of exact actions. A wrong idea costs one failed goal, which
  comes back in the next report with the reason.
- **With a floor.** If the model fails or the budget runs out, the free book plans instead.
- **Three roles, each with a rule floor.** The strategist plans; the supervisor (`--assessor-model`) watches
  between plans; the fixer (`--fixer-model`, default the assessor's model) is asked only when one error fails
  several goals or one goal keeps failing. It sees what the planner cannot: the error, the actions the game
  refused, and the buildings around the character, and answers with fixes from a menu (pick up a building,
  walk, remove a site, retry the goal changed, set it aside, wait). Each has its own cost cap; `--no-assessor`
  and `--no-fixer` keep the rules only.
- **With the road in view.** Every report carries the computed roadmap (`roadmap.py`): the stages to the rocket,
  techs and packs left, lab hours, each science colour's rate against what the road needs, power headroom,
  plates against what the workshop takes, and the step the free book would take next.

## The road to the rocket

Past the opening (red and green science, steel, a pumpjack), the **rocket book** plays the roadmap's next step
one at a time: electric mines when the workshop needs more plates, a steam station whenever power headroom
drops under 1.2 MW, 12 labs, a **workshop** for each science colour as its technology comes in (red and green
15/min, blue 10, purple and yellow 6; pumpjacks first for anything with oil), then, once `rocket-silo` is
researched, workshops for the silo's kit (concrete, processing units, electric engines), the **silo** when the
kit sits in chests, and workshops for the parts (processing units, low density structures, rocket fuel). The
courier keeps the silo fed; the scenario launches a finished rocket by itself (100 parts in 2.1).

**The workshop** makes any item: `production.py` plans every intermediate from the game's recipes, one cell
per machine (input chest, inserter, machine, inserter, output chest; refineries only pipes) on a growing floor
of bands and pipe corridors with walkways that are never built on. Slots not yet used stay clear of pipes,
every port keeps a one-tile exit lane, and no underground end sits on a port or in the gaps between cells. When
a floor can no longer grow, the next workshop starts a new floor beside it: items still cross between floors
(the courier), fluids do not, so the planner gives the new floor its own pump, refinery and crude line. Offline,
the whole rocket factory (five sciences, processing units, low density structures, rocket fuel, concrete: 147
cells) designs on one floor with every fluid net routed. Each fluid gets its own net, routed from an
offshore pump (water), the pumpjacks (crude) and the cells that make it; pipe ports come from the catalog
(`fluid_ports`, dumped from the game's prototypes, since 2.1 has no runtime fluidbox API). The **courier**
(part of the foreman's patrol, every 45 game seconds) empties output chests of whatever another cell needs and
tops each input chest up to 8 minutes of inputs, bringing plates, coal, stone and ore from the mines.

**Factories feed factories.** A belt factory takes what assemblers cannot make from plates (steel, plastic,
sulfur, processing units, batteries) through **input cells**: a chest, an inserter onto its belts, and a port
facing outward. A chain over 12 assemblers is split: its biggest intermediate becomes its own factory feeding the
first (blue science = an advanced-circuit factory and others feeding a small blue factory). Every output chest is
a source and every port (factory inputs, workshop cells, research stations, the silo) a sink; the manager's
`link` step belts sources to ports (`belts.py`, under other belts and pipes with underground belts) and builds a
supplier for any port nothing makes yet (a belt factory if belts can, else a workshop). Until a link arrives, the
courier keeps the port's chest stocked. Workshops are for fluids only, and their cells take their main input by
belt too.

**Energy first, then electric mines.** The book keeps 1.5 MW spare before anything else, every electric build
adds the stations it needs first, and the supervisor treats low power as urgent. From the third station on, each
gets its own offshore pump. Once electric drills are researched, burner mines are replaced by electric ones
(built first, then the old one picked up), so nothing needs refuelling every few minutes.

## A base that runs on its own, then the next tier

Players finish a block, make it run on its own, and leave it alone while they move on. They come back on
purpose a few times a game, not every time one furnace hiccups. The bot works the same way (`upkeep.py`):

- **What hands are still fine** depends on where the game is. Fuel by hand is fine until logistics, then coal
  moves by belt. Burner drills are fine until electric drills and logistics. A courier-fed workshop is fine
  always: small amounts of complicated items.
- **Sealed sites.** A site that runs on its own for 10 game minutes is sealed: inputs by belt, fuel by belt or
  none, power. A sealed site is not removed, replaced or re-routed while it works, and its belts are never
  moved out of another link's way. If it breaks, repairs may still replace it.
- **The close-out.** When a roadmap stage is done, or logistics comes in, the crew makes one planned trip back
  before the planner's next goals. It fixes what is broken, then what this tier no longer accepts by hand,
  then adds headroom (power at 1.3x what the network draws, coal at 1.25x what burns) and prepares the next ore
  patch if one runs low. It does at most 8 goals; the rest is listed in `base_status` for the planner.
- **The frontier shield.** While a next-tier build runs (pumpjacks, factories, workshops, the silo,
  outposts), only base-wide trouble pauses it: power short, coal running out, the character stuck.
  Site-level fixes wait in the urgent queue and run right after it. Other goals may be paused at most once
  every 10 minutes.

## Changing what stands

Players change factories in three ways, and the bot handles each (`upgrade.py`):

- **Upgrade in place** (the most common). `{"do": "upgrade", "site": id}` swaps assembling machine 1 -> 2 ->
  3, stone -> steel furnaces, and basic -> fast inserters, all the same size. Each machine is picked up (it
  comes back with what it held), the better one goes on the same tiles facing the same way, and it gets its
  recipe (read from the game) or its coal back. A machine the game refuses goes back as it was. Belts are left
  alone: the links run far under a yellow belt's 15 items a second. Only a site that waits on its machines
  gains (a furnace row whose drills back up, busy assemblers), not one waiting on its inputs. Whenever a better
  machine is unlocked, the close-out upgrades the two sites that gain most, plate rows first. Upgrading is
  allowed on sealed sites.
- **Rebuild bigger.** A sealed site may be replaced by a bigger build of the same product (a higher
  `per_minute` or `count`), or a burner mine by an electric one on the same ore. The new one is built first.
  Replacing it with something else is refused.
- **Repurpose: build new, retire the old.** A factory or workshop whose machines wait on a full output, whose
  chests feed no link, and whose product neither the builder nor the labs take, goes idle after 45 game
  minutes. It loses its seal, and `base_status.idle` offers it for removal, so its machines go into the next
  build. Science packs and buildings are never counted idle: labs and the builder take those out by hand.

## Keeping the books straight as the base changes

- **A removed site takes its links with it.** Before its buildings come up, every belt link into or out of it
  is taken up, along with the links that tap or feed those. The ports they fed count as unfed again, so the
  link step connects them to a replacement. Its belt margin is released, and an outpost whose column went gets
  a new one.
- **Inputs are matched by rate**, for every item, not just coal. Each source has a rate: one drill (or its
  furnace) for a mine chest, one furnace for a column, a share of the output for a factory or workshop cell.
  Each port has a demand. An unused source may take any port. A used one takes a port only if what it has
  left covers it. A belt that feeds more than its source makes is listed in `short_belts` (alert
  `input-chain-*`), and the link step side-loads an unused source of the item onto it.
- **After an upgrade**, the site's designed rate, its input ports and its cells grow with the machines' speed.
  Plate supply counts steel-furnace rows at 30 a minute (one drill's ore).

## Growing out of hands

- **The mall.** Kits take items from belt factories' and smelting columns' chests, not only workshop cells.
  Once the crew has hand-crafted 40 of a building (belts, inserters, gears, circuits, drills, assemblers,
  furnaces, poles...), a small factory makes it.
- **Busy workshop items move to belts.** A workshop item made at 8+ a minute that belt factories can make
  (advanced circuits, engines, low density structures, blue, purple and yellow science...) gets a belt factory.
  Its workshop-made inputs, like plastic, come by belt from the cell's chest. Once the factory runs, the old
  cell is retired: the courier stops feeding it. Fluid recipes (plastic, sulfuric acid, processing units,
  rocket fuel) stay in the workshop.
- **Steel by belt.** `smelter_column` with `resource: iron-plate` takes plates in through a port the link step
  feeds and puts steel into chests that kits and links take from. The roadmap builds one when the workshop
  wants steel the base does not make.
- **Room for belts.** Every factory, workshop, station, column, silo and outpost keeps a one-tile margin free
  of buildings (belts may use it), and every trunk keeps its sides free. Research stations and steel columns
  are built where their inputs are. A consumer whose links fail 3 times because it is walled in gets an alert
  whose fix rebuilds it where its inputs are (the new one first). Sealed sites are never moved this way.
- **Oil balance.** A refinery with a full output stops the whole oil chain. The `oil-balance` alert proposes a
  solid-fuel cell, which takes any of the three oils and which rocket fuel needs anyway.

## Outposts: ore from farther away

The API has no trains or cars, so remote ore comes home by belt (`outposts.py`):

- **Looking ahead.** `ore_outlook` in the report gives, per ore, what is left on the patches we mine, how fast
  it goes, whether there is room for more drills, and the next patch known. When a patch is full or has less
  than about 4 game hours left, the roadmap, the close-out and a warning all propose an outpost on the next
  patch, or `{"do": "scout", "resource": ...}` when none is known. Scouting walks out in rings until a patch
  nobody works shows up.
- **One goal builds it all.** `{"do": "build", "module": "outpost", "resource": "iron-ore", "count": 8}` lays
  a row of electric drills on one side of a collector belt (running toward the base) and a pole line home.
  It then builds a smelting column at the base edge, sized to what the drills mine, and the trunk belt
  between them.
- **The trunk** is routed inside a corridor along the straight line (12, then 28, then 56 tiles each side), so
  a 400-tile route stays a small search. It uses undergrounds once logistics is in. It is recorded as a link
  of kind `trunk`: never tapped, never moved. A failed trunk is retried by the link step.
- **The column** runs the trunk down a line of furnaces. An inserter per furnace takes ore off the trunk, and
  a long-handed inserter takes coal from the column's own coal belt (a fuel port the link step feeds like any
  furnace row). Plates go into a chest per furnace, which links take on like any mine's chests. For coal, the
  column is a row of depot chests instead.

**Scattered patches** are fine: only electricity goes out to an outpost, and its ore or coal comes home, so
nothing needs bringing together far from the base. Long power lines (40+ tiles) use medium poles (9-tile wires)
once their recipe is in and steel is at hand or made by belt. Where a new medium pole meets one that already
stands, the hop stays within the small pole's 7.5 tiles.

**One ore per drill.** An electric drill mines a 5x5 area. A drill spot whose 5x5 area touches another ore is
skipped, in furnace rows and outposts alike, so copper never ends up in an iron chest and on a factory's iron
belt.

## Zones (hybrid)

`zones.py` adds the hybrid floorplan from `docs/hybrid-plan.md`. The brain keeps its organic opening in a
**start yard**. From the first electric build, an Atlas floorplan only reserves land outside the yard. From
**segment 1** on, the era's blocks go into spine slots. Atlas (`bots/atlas`, a submodule) is used as a library
(ledger, floorplan, bus, strip designer, production graph). Atlas itself is not run.

- **When.** `zones.after_build(crew, site)` runs after every build and never raises. On the first electric
  build (or steam station) it does a remote survey, plans on a snapshot (`w.think`) and reserves the land.
  Segment 1 opens on a later build, once all of these hold:
  - underground belts are unlocked;
  - an `electric_mine` or `outpost` exists;
  - a `steam_power` station exists.
- **Start yard.** A box around the base centre that holds the opening's buildings (those within 90 tiles of
  the centre) plus 10 tiles, between 36 and 90 tiles from the centre each way. It is never reserved.
- **Reservations** (`Zones.reserve` = `plan` then `apply`):
  - the spine, starting at the yard's edge and heading away from it;
  - the oil corridor;
  - the silo slot;
  - the next era's slots (make, science, two spare).

  Nothing is reserved on any ore tile, on or within 2 tiles of our buildings, lanes or keepouts, or on unseen
  land. `FreeWorld.free` refuses reserved tiles for buildings. `free_belt` and pole routing allow corridor
  tiles but not slot tiles. fab's `Grid`, the workshop floor and the storage yard keep off all reserved land.
- **Segments.** `Zones.segment(n)` / `open_segment(n)` work from `SEGMENTS[n]`. Segment 1 is red and green at
  30/min, sized by `atlas.plan.graph`, which adds the ×1.2 `SUPPLY_MARGIN` to every intermediate. Each copy of
  each make block (gears, circuits, red, green) and the labs gets a slot. Each is designed with Atlas's strip
  designer, with its ports on the spine edge. The bus (`atlas.layout.bus.design_bus`) is laid for these
  terminals only, plus **head terminals** at the spine's start for the plates the brain's lines bring in.
  - The result is a `Segment`. Each `SegBlock` holds its slot rect, `ports` (spine edge tiles), `taps`
    (bus line, lane, splitter per input) and `problems`.
  - `Segment.head_ports(spine)` gives the head terminals as feed ports for trunks.
  - The whole segment passes Atlas's world check: no clashes, one item per lane, every input port gets its item.
- **Factories.** When a segment is open, `Factory._design` first asks `slot_block(item)`. If it gets a block,
  `zones.slot_build` turns the Atlas design into placement steps:
  - inserters face their pickup side, and undergrounds carry their belt type;
  - each in port becomes a feed port whose last link belt stands on the spine edge;
  - each out port gets an inserter and a chest on the spine edge (the product's stand-in until the bus exists).

  `supplied_for` returns the block's bus inputs at the block's rate (`SlotPlan`), so the crew's "intermediates
  first" and "plates first" size the feeders to the slot. Free-form fab stays for everything else: start-yard
  builds, items not in the segment, a block already built, `plates: "mine"`, and a slot whose design fails.
- **Saved** with the crew (`crew.zones`, the same object as `world.zones`). Only the plain state and the Atlas
  corridor and slot records are saved. The floorplan's ledger and the designs are rebuilt after a resume.
- **Off switch:** `zones.ENABLED = False`. Nothing is reserved, `free` works as before, and factories are
  free-form.
- **Not wired yet (stubs):**
  - the bus pieces and taps are planned and checked but not built (`Segment.bus`; `atlas.build.compile.bus_jobs`
    turns them into jobs);
  - no trunk goal targets the head ports yet;
  - until both exist, a slot factory's inputs are point-to-point links into its feed ports, and its output
    sits in the stand-in chest;
  - segments 2+ (blue, the oil zone) are hybrid-plan step 6.
- **Tests:** `tests/test_zones.py` runs on the Atlas fixture map. `ZONES_ALL_MAPS=1` also runs it on the 22
  generated maps (all pass, 3 to 11 s each).

## How far it gets today

The opening book goes: burner mines on iron, coal, stone and copper, then steam power (unlocked by the 50th
iron plate) and labs (unlocked by the 10th copper plate). Next it researches automation and electric mining
with hand-fed red science, builds an automated red science factory with the fab engine, researches green
science, adds a second steam engine pair, builds an automated green science factory, and moves on to steel.
Then oil: `oil-processing` is a trigger technology (crude oil mined), so the crew researches oil-gathering,
smelts the 5 steel a pumpjack needs in a few stone furnaces (`Crew.smelted`), builds a pumpjack on the nearest
well and wires it up. Plastics and chemical science follow as research (red and green packs only).
Past that, the rocket book (above) or the LLM decides.

**Verified in a real game (seed 1008):** from an empty inventory to 7 burner mines and a two-engine steam
station by game minute ~11. In the sandbox task (`sandbox-workshop`: everything researched, a kit of machines;
`tests/test_workshop_factorio.py`, about two minutes): a two-cell workshop making batteries from water piped
from its own offshore pump into a sulfuric-acid plant and on into a battery plant, fed by the courier; two
pumpjacks on wells ~230 tiles away, wired back with a surveyed 36-pole line, their crude piped to a second
workshop on the same floor (advanced oil processing, heavy and light cracking, a solid-fuel sink) that puts
plastic in its chest; a row of electric drills putting plates in chests; the silo crafting rocket parts from
its three input chests (about 8 minutes, most of it walking to the oil).
Checked offline only: whole floors for every science colour (designed and routed in under a second), the
roadmap and the rocket book. **Not yet played:** a full game from nothing to a launch. Expect the first long
run to show what breaks next; the fixer, the circuit breaker and the watchdog are there for that.

## Where it goes next

1. **One full game to the rocket**, at `--clock-speed 4` or more, reading what the fixer notes say.
2. **Courier throughput.** One character carries everything between cells; at rocket scale (copper cable for
   processing units) that is the bottleneck. Direct insertion between neighbouring cells (cable straight into
   the circuit assembler) or a few belts inside the floor would remove most trips.
3. **Fuel for the boilers.** Each steam station's chest is hand-filled with coal (400 at a time when coal is
   plentiful). A solar field (solar-energy is researched on the way anyway) would take that off the crew.
4. **A module library that persists**: floors that worked, saved and reused on the next run.

## Running on its own

The bot is meant to be started and left alone. Three things keep it moving:

- **Chiplets fit the ground.** Burner mines pick spots facing any direction on the patch. The power station
  tries all four rotations around every shore spot, then a one-engine size, and routes its water pipe with A*.
  A factory is laid out on demand for any item made in assemblers from iron and copper plates (42 items from
  the game catalog, including buildings for a mall). The `design` goal previews one without building it.
  Nothing is designed around something that does not exist: no water means no steam power.
- **Failures say why.** Messages name the cause, and say whether one of our sites is in the way. The
  strategist sees `repeated_failures`, `recent_removals` and churn hints. Working sites younger than 10 game
  minutes can't be removed without `force`. The opening book retries a failing step smaller, then drops it.
- **A circuit breaker** (`bot.py`): the same error on different goals is a crew fault (walled in, an
  unreachable spot), not a plan fault. After three such failures the crew digs the character out, rescans and
  backs off (1, 2, 4, 5 min) instead of asking the strategist again; a run once asked it ~100 times about one
  unreachable chest. A tile the game refused a storage chest on is never tried again; hand-mining skips ore it
  cannot reach.
- **Trap watch** (`world.py`): a walk the game refuses checks whether the character is shut in, two ways: probes
  (7 tiles, then 24: a pocket longer than the short probe still counts) and the record (4 far walks refused in
  3 game minutes with no real walk away between them; short walks inside a pocket do not reset it). Trapped:
  `escape` re-reads the buildings around, finds the fewest between the character and open ground (a
  cheapest-path search, `dig_plan`), picks those up and keeps that door free of new buildings for good; the
  crew forgets them in their sites. If the map was wrong, it digs by hand (nearest buildings until a way opens,
  put back from outside). When that fails too, or the third dig-out comes within 20 game minutes, the fixer (an
  LLM) is asked with a text map of the ground around the character and can order `escape`, pick-ups, walks or
  site removals. Placing a building that would shut the character in looks 12 tiles out and steps out first.
- **Modular factories** (`modules.py` Factory, `fab/`): a factory goal names one product, a rate
  (`per_minute` or `per_second`) and `inputs`, any items that come in through input ports; the factory makes the
  rest of its chain. Ore is for drills only: plates are inputs by default, so the factory is designed on open
  ground near the base with every ore tile kept out (`_open_places`), one input cell per furnace's worth of
  plates. Before it is built, `_plates_first` adds electric_mine rows until there is one free mine plate chest
  per plate input cell (a mine chest feeds one link, a factory chest two); after it, `link` belts chests to
  ports, and the courier stocks unlinked ports. `plates: "mine"` asks for the older design with its own drills
  and furnaces on the ore; a default plate-fed design that fits nowhere falls back to it.
- **Storage** (`crew.py`): there is no drop action, so whatever the crew cannot carry goes into storage
  chests. They stand together in a 4x2 yard in a clearing near the base's middle (2 free tiles around it),
  beside the character only when no clearing is known. A full inventory leaves surplus first, then anything
  over a stack, then whole stacks; fuel, chests, poles and the kit of the build in hand stay on hand. A removal
  or a hand-mine that hits a full inventory makes room and goes on, and kits come back out of storage before
  anything is crafted again. Layouts may plan over a storage chest (`FreeWorld.free`): clearing the tile empties
  the chest into the yard and picks it up (`make_way`), and a build that takes the yard itself gets a new yard
  at least 12 tiles from it. The storage site is never removed.
- **A watchdog** (`watchdog.py`) acts when nothing progresses: no milestone, research, build, removal or power
  connection. At 6 game minutes it refreshes (rescan, re-survey, refuel, reconnect, free the inventory) and
  replans. At 10 it switches planner, at 15 it adds supply where plates are scarcest, and at 22 it notes the
  stall and starts the ladder again. Progress resets it. Surplus plates and ore go into storage chests, so a
  full inventory never stops mining or crafting.
