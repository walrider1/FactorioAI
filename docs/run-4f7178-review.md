# Run 4f7178 review: why the floorplan wasn't followed, and how to get through faster

Seed 681265899, 2026-11-27, 690 game minutes (clock x2), brain bot on main 1c35dbb, resumed twice late on
(2b12ff6, d864912). AI spend for the run: about $9.80.

## 1. What happened

| Game min | Event |
|---:|---|
| 16 | Steam power on. **Floorplan reserved**: spine of 11 cells, 15 slots, no oil corridor (crude was 369 tiles out) |
| 13 → 111 | Red science factory at 30/min tried three times without undergrounds (65 game min lost), then built at 15/min outside the zones (site 16) |
| 63 | First research (automation), packs carried by hand |
| 111 → 150 | Green science factory built outside the zones (site 19, with a circuit feeder, site 17) |
| 150 → 250 | Coal crisis: the only near coal patch had 89 tiles. Coal came from site 20, 300+ tiles out, over seven coal trunks of 366–490 tiles each |
| 251 | **Segment 1 opens** (it waited for green, logistics, electric mining and steam). Bus laid by 259 |
| 290 → 356 | Slot factories built: circuits, gears, labs, red. The organic red (16) and green (19) sites stay up too, so both sets share the same iron |
| 254 | Green science automated (just past the 4-hour bonus window at 240) |
| 292 → 640 | **The bus's iron input is fed by one 37.5→60/min source and takes 130/min.** Green science runs at 2.7/min for ~350 min. 96/min of spare iron sat 15 tiles away |
| 409 | Pumpjacks; plastics 443, plastic automated 500 |
| 500 → 650 | Sulfur and solid-fuel workshops fail 6 times: the oil floor is too crowded for a crude pipe (≈35 min) |
| 562 | Blue science researched. **Segment 2 stays shut** (it waited for sulfur); blue parts start going up outside the zones (advanced circuits at site 87, a blue science workshop) |
| 632 | The bus's only iron source is mined out; science stops |
| 654 | Segment 2 opens on the new code, but tears down all 5 working slot factories to rebuild them |
| 690 | Iron trunk from the far mines (390 tiles) into the bus laid. Run stopped for the night |

End state: red science 18/min, green 0, blue 0. 244 iron/min mined, but most of it never reached a consumer.

## 2. Where the game time went (646 of 690 min inside goals)

- **Long belts: 126 min** (27 trunks, 13 of them failed: 60 min wasted). The coal trunk into steam site 21 alone failed 10 times.
- **Factories: 152 min**, of which ~65 min were the pre-underground red science attempts.
- **Replacements: 115 min**, mostly rebuilding smelter columns and mines that couldn't be fed.
- **Workshops: 91 min**, of which 56 min failed (crowded oil floor, research not done yet).
- **Waiting on research.** Research was the binding limit all game: one build was refused because fluid-handling research "needs ~127 more game minutes at this pace". Pace = science packs, and science packs = iron at the bus.
- **Decision churn**: 43 relinks (many "diagnosed: unknown cause, relinked once"), 8 link steps that did nothing, 17 hand-ordered "act" goals.

## 3. Why it didn't follow the floorplan

1. **The floorplan opened after the base was built.** Segment 1 waited for green science, logistics, electric mining and steam (opened at minute 251). By then red, green, circuits, mines and smelters all stood organically round the start yard. The slots got *second* copies, and both copies competed for the same scarce iron.
2. **The blue era was gated on sulfur, silently.** Segment 2 needed "something makes sulfur" before it would open, and sulfur kept failing on the oil floor. Nothing logged why. *(Fixed in 2b12ff6: the era opens on its recipes, missing head inputs get queued, a held era says why.)*
3. **Items of a closed era went free-form.** By design ("organic until its era opens"), so blue parts went up wherever they fit. A "build workshop chemical-science-pack" goal also sidestepped the slots entirely. *(Fixed in 5da7a31 and d864912: later-era parts wait up to 45 min for their slot; workshop goals for slot items go to the slot.)*
4. **There were no later eras.** Purple, yellow and the rocket had no floorplan at all. *(Fixed in 5da7a31: eras 3–5, plus the spine grows new slots when an era needs more.)*
5. **An era switch tore down working factories.** Blue's take-over reworked red and green science (15/min in both eras) only because the kit now had faster assemblers. *(Fixed in 5da7a31 for eras opened from now on: a block that still covers the rate stays built.)*

## 4. Why it should follow the floorplan

The single biggest loss this run was **getting items from where they are made to where they are used**: 60 min of failed trunks, 43 relinks, a 350-minute iron starvation with spare iron 15 tiles away, and copper columns standing full ("nowhere to put output") the whole game. Every one of those happened in the crowded start yard, where each new factory needs its own hand-routed belts through what is already there.

The floorplan removes that whole class of problem. Every slot factory takes its inputs from the bus and puts its output back on the bus. The routing is planned once, on reserved land, and checked before anything is built (the blue era laid out with zero problems on this map). The only routing left is **getting plates into the bus head**, and that is exactly where this run broke. So the plan below is mostly about the head.

## 5. What still stops it (not fixed yet)

1. **The bus head is fed by leftovers.** Each head input has one port, fed by whatever source happens to be spare, with no sizing. The bus took 130 iron/min from a 37.5/min mine for 350 minutes. Today's fixes (taps of belts with spare, second trunks) help, but the head still has no supply of its own.
2. **Organic and zoned factories run side by side.** Once segment 1's red and green slot factories run, sites 16 and 19 kept drawing iron from the start-yard lines.
3. **Power is outside the floorplan.** Hand-fed boilers ran all game (sites 24 and 60). Steam site 21's coal belt failed 12 times. Two stations were unreachable at the end ("no path"). The zones drop Atlas's power slots (`zones.apply` keeps everything but "power").
4. **The oil floor is not zoned.** No oil corridor was ever reserved (crude 369 tiles out, OIL_LAZY), so workshops crowd one floor until pipes no longer fit.
5. **Research doesn't follow the era.** During blue the queue was modules, battery, concrete. The overseer had to drop advanced-material-processing-2 as premature.
6. **The map was hard and nothing checked it.** Coal 89 tiles near home, crude 369 tiles out, near iron patches full by minute ~390. `pick_seed` didn't reject it.

## 6. Plan

Targets (game minutes): red automated by 45, **green automated by 150** (inside the 4-hour bonus, 80 min faster), blue automated by 360, purple by 600. Each step below is one change with a short test, tried on a fresh map before the stream.

### Step 1: the bus head gets its own supply (biggest win)
- Size every head input from the live segment's demand (`Segment.demand`), as today.
- **Give the head smelting of its own.** Use Atlas's smelt blocks (already in the era decomposition: `smelt-iron-plate`, `smelt-copper-plate`, `smelt-steel-plate`, `smelt-stone-brick`) in smelt slots just upstream of the head. Ore comes in by trunk from a dense field or outpost sized to that column. Add "smelt" to the slots `slot_demand` asks for, sized from `era_blocks`.
- Until that column runs: let a head port take several feeds (today's taps and second trunks). Never count a head input as "fed" while its feed gives under HEAD_SHORT of demand.
- Test: replay this save. The head's iron gets ≥130/min planned from its own column.

### Step 2: one set of factories, not two
- When a slot factory for an item runs flat out (or meets the bus demand), retire the organic factory for the same item. Its inputs go back to the pool. `rework_old` already knows how to take sites down.
- Test: segment-1 fixture plus an organic red site → the organic one is listed for removal once the slot one is built.

### Step 3: open segment 1 earlier, or open with what's standing
- Option A: open segment 1 as soon as logistics and electric mining are in (drop the green-science gate). Green and circuits go straight into slots. Red stays organic as the opening.
- Option B (after step 2): keep the gate, but have the slots replace the organic sites instead of duplicating them.
- Measure both on two fresh maps: minutes to green-auto.

### Step 4: power in the floorplan
- Keep Atlas's power slots (`place_power`) instead of dropping them. Steam stations go there, coal comes by belt from a coal field into a coal line on the bus. No hand-fed boilers after logistics.
- Test: a fixture with water near the spine gets power slots; boilers are belt-fed in the design.

### Step 5: oil in the floorplan
- Reserve the oil corridor even when crude is far: a pipe trunk to oil slots near the spine head, sized for 2 workshops per era. Sulfur, plastic, batteries and processing units go into oil slots, each with its own crude, water and product ports.
- Test: the crowded-floor case ("no pipe route for crude-oil ... 13 tiles away") cannot happen in an oil slot.

### Step 6: research follows the era
- While an era's science isn't automated, the research queue holds only that era's and the next era's techs, plus the tier's essentials (logistics-2, electric mining, steel, advanced material processing when furnaces are short). Strategist picks outside that list get dropped by a rule, not the overseer.

### Step 7: fewer wasted goals
- Same goal, same failure reason, 3 times → forbidden until something it depends on changes (a site built or removed nearby). Site 21's coal trunk would have stopped after 3 tries instead of 12.
- A relink that ends "diagnosed: unknown, relinked once" twice for the same port → no more relinks; the port goes to the boost/second-trunk path.

### Step 8: map vetting
- `pick_seed` rejects maps whose nearest coal patch is under 200 tiles, whose nearest crude is over 250 tiles, or whose iron within 150 tiles is under ~1M ore.

### Already done today (for reference)
- c06b470: short belts get taps from belts with spare; overseer underground placements work.
- 114b12d: outposts never overfill one trunk belt; short far ports get a second trunk.
- 2b12ff6: blue opens on its recipes; missing head inputs queued; held eras say why.
- 5da7a31: purple, yellow and rocket eras; the spine grows; later parts wait for their slot; working factories survive era switches; the silo goes in its slot.
- d864912: workshop goals for slot items go to the slot.
- 2f55947: small factories until undergrounds (the 65-minute red science loop); low coal is urgent.

Order of work: steps 1 and 2 first (they decide whether the bus is "good"), then 7 (cheap), then 3, 4, 5 and 6, then 8.
