# Hybrid plan: the brain's game, Atlas's space

Status: plan, written 2026-11-26 after reviewing run 20261125-214645 (game minutes 104 to 590).

## What run 20261125-214645 showed

| Measure | Value |
|---|---|
| Crew time logged since minute 104 | 492 game minutes |
| Failed or paused goals | 140 minutes (28%) |
| Advanced-circuit factory | 175 minutes; 3 of 35 designs worked over three days of runs |
| Factory design while the crew waited | about 115 real minutes (about 230 game minutes) |
| Sites "nowhere to put output" while green science ran at 0/min | 15+ |
| Link-stuck or no-belt-route alerts | 30+; up to 5 research stations at once |
| Refuels by hand | 301; 4 sites out of coal at minute 590 |
| Queued goals dropped by another decider | 74 |
| Latest failures | sulfur workshop: "no pipe route for crude" (58 tiles); circuit feeder delivers 8 of the 40/min its consumer needs |

The root causes, most important first:

1. **No floorplan.** Each factory goes wherever there is room, then a belt is routed to it from each
   source. Nothing keeps paths free, so every build threads through the earlier ones, and it gets worse
   with every tier.
2. **Point-to-point links from chests, counted on paper.** Lines back up next to factories that starve.
   Factories output into several chests, and a link draws from one of them.
3. **Every factory designed from scratch, while the crew waits.**
4. **The deciders fight.** Four queues (plan, urgent, close-out, resume) plus the overseer's plan and the
   fixer's retries. They add, drop, forbid and re-add each other's goals, from text summaries.
5. **Coal and one oil well.** Burner smelting all game long, and nothing scouts for more crude early.

## Why not just Atlas

Atlas is built for causes 1 to 3: a floorplan, a ledger, a bus and blocks designed into slots, all proven
offline on 22 maps and block by block in the real game. It was poor in the early and mid game because it
sized and built for the late game straight away. The brain is the opposite. Its opening, burner phase,
first electric mines and red and green science are tuned by 28 self-upgrades, and it falls apart only as
the base grows.

**The hybrid:** the brain keeps playing, with its opening, deciders, foreman and repairs. Space comes from
Atlas: a ledger, zones, and a bus that grows **one era segment at a time**, sized to the era in hand, never
to the rocket.

## 1. Zones and era segments

```
 era          zone built                     what decides placement
 ---------    ---------------------------    ---------------------------------------------
 opening      START YARD (organic)           brain modules, as today
 red/green    START YARD, plus reservations  brain modules; the ledger keeps reserved tiles free
 green+ (*)   BUS SEGMENT 1 + smelting       slots and taps for this era's blocks, sized x1.2
 blue         SEGMENT 2 + OIL ZONE           spine extended, lanes added (relay, old lanes pinned)
 purple       SEGMENT 3                      the same
 yellow       SEGMENT 4                      the same
 rocket       SILO SEGMENT                   the same (the silo spot is reserved from segment 1)
```
(*) Segment 1 opens once logistics (underground belts), electric mining and the first steam station are
all in. That is the point where the brain starts to struggle today.

- **Start yard.** A box around spawn, sized to the brain's opening: burner mines, stone furnaces, the first
  steam station, labs and red science. Organic as now. Nothing in it is ever forced to move, and its sites
  are replaced in place when they wear out.
- **Reservations (from the first electric build).** The floorplan is computed early, but only *reserves*:
  the spine path out of the start yard, the corridor to the oil, and the silo spot. Reserving costs nothing.
  `world.free()` already refuses `lanes` and `keepout`; it also refuses ledger-reserved tiles, so an organic
  build can never land on the future bus.
- **Segments.** Each era's demand comes from `atlas.plan.graph` for the **era's targets** (for example red
  and green at 30/min), not the rocket's. The floorplan grows the spine just enough for that era's slots
  (`floorplan.place_slots`), and the bus is laid for those terminals only (`bus.relay`, with existing lanes
  pinned). The next era appends. Nothing already built is rearranged.
- **Smelting.** It stays where the brain's dense fields and smelter columns are good (#14-#27). A field's
  line feeds a spine-head terminal through a trunk (the brain's trunk goal), instead of every factory
  linking to it.
- **Factories in a segment** are designed into their slot by Atlas's strip designer (`design.block`), ports
  on the spine side, inputs from spine taps (splitters). The brain's free-form `fab` designer stays for
  start-yard builds, and as a fallback.
- **Links.** Inside segments a link is "tap lane X". The brain's point-to-point links stay for the start
  yard and the outposts.

**Maintainability:** one module, `bots/brain/zones.py`, owns the ledger, the era segments and slot
assignment, and imports Atlas's `layout`, `design.block` and `plan.graph` as a library (Atlas is not run).
Brain modules ask it two things: "may I build on these tiles" (through `world.free`) and "which slot and
ports for this block". Everything else in the brain stays as it is.

## 2. Saved factories (design memory)

- Every factory design that gets built is saved, with its layout relative to its anchor, its ports, and
  its signature (item, rate bucket, inputs, kit, slot shape or "free"). It goes to
  `runs/local/designs/<item>/<signature>.json`, so it persists across runs.
- A new factory goal first tries the saved designs that match. A design is reused only after the DRC, the
  route probe and a free-tile check pass on today's ground. A slot design almost always fits, because the
  slots are standard.
- It covers both designers (Atlas's `design.memory` for slot blocks, and the same format for free-form
  designs). #29's split memory becomes part of it.

## 3. The deciders, synced: one agenda

Today: `goals` (strategist), `crew.urgent` (rules, assessor, triage, overseer orders), `closeout_queue`
(upkeep), `resume`/`parked`, `crew.plan_queue` replaced by the overseer, and the fixer's retries, all merged
by `upkeep.next_goal` with aging and "protected" goals.

New: **one Agenda (a blackboard) and one arbiter.**

- **Entry:** the goal, its target key (`same_target`), its owner (the decider that proposed it), its class
  (safety > upkeep > plan > improve), the reasons from every decider that asked for it, its status
  (queued, designing, ready, running, paused, done, failed, vetoed) and its dependencies (research, design,
  inputs).
- **Writes:** `propose`, `amend` and `veto`, never a private queue.
  - `propose` merges with the entry for the same target: reasons are added, and the higher class wins. It
    is refused if that target is vetoed or was just done.
  - `amend` changes an entry and records who changed it and why, so triage's diagnosis amends the relink
    it came from rather than inserting an outpost in front of it.
  - Only the **overseer** vetoes or reorders (`veto(target, why, minutes)`). A veto holds against everyone,
    including the fixer, triage and the close-out.
- **Roles:**
  - **Strategist:** the era plan (rate targets and blocks). It sees the agenda, the active vetoes, the
    recent failures and who proposed what, so it does not re-propose what was vetoed.
  - **Overseer:** reviews the agenda every 10 minutes. It can approve, veto, reorder, or add an order.
  - **Rule floor and assessor:** safety and upkeep proposals.
  - **Triage and fixer:** services. They attach diagnoses to entries and amend them; they do not spawn
    goals of their own.
  - **Close-out:** becomes the planner's checklist at the end of an era (proposals of class "improve").
- **Arbiter (deterministic):** picks the next *ready* entry by class, then age. "Ready" means its research
  is done, its design is prepared and its inputs exist. There is no "protected": long goals are resumable
  (section 4).
- **One shared picture:** every LLM decider gets the same situation, the agenda, and the last decisions
  with their authors. The overlay shows who proposed, amended or vetoed each goal.

## 4. Two lanes, and design ahead

- **Upkeep lane:** fuel, power, lab feed, connect and short repairs. It runs at the build's safe points
  (`crew.checkpoint`) within a time slice (at most about 20% of the time) whenever an upkeep entry is due,
  even in the middle of a long build. Run 20261125-214645 let four sites run out of coal behind a 96-minute
  build.
- **Design ahead:** every factory or workshop entry is designed in the background as soon as it enters the
  agenda (two workers, on a ledger snapshot). The arbiter starts a build only once its design is ready, and
  the crew does other entries meanwhile. Design failures (split, another slot) are handled in the
  background without crew time.
- **Resumable builds:** what was placed is recorded (as the unfinished-site mechanism does today), so a
  pause costs nothing and the rest is placed later.

## 5. Electric smelting, power, oil

- **Electric smelting:** right after blue science, research `advanced-material-processing-2`. Then swap
  smelter columns and dense-field smelters to electric furnaces, column by column, while the old ones run,
  and take up the coal belts. This removes most fuel hauling.
- **Power planner:** keep about 30% headroom over measured demand. Each swap's demand (180 kW per furnace)
  is booked before the swap. Steam now; solar once it is researched; inland plants by water pipe (Atlas's
  rocket blocker).
- **Oil:** scout for at least three crude wells as soon as oil processing is queued. Oil zone slots are
  reserved with the blue segment, and the pipe trunks are reserved with taps (Atlas's oil zone).

## 6. Order of work, and how each step is proven

| Step | Change | Proven when |
|---|---|---|
| 0 | Commit Atlas (13k lines, never committed) on its own commit | pushed |
| 1 | Agenda and arbiter; deciders write through it; vetoes hold for everyone | unit tests; replaying the run 20261125-214645 decisions shows no drop/re-add loops |
| 2 | Upkeep lane at safe points; design ahead; resumable builds | unit tests; a scripted long build with a coal outage feeds the coal mid-build |
| 3 | Design memory for both designers | a second identical factory designs instantly in tests |
| 4 | `zones.py`: the ledger behind `world.free`, reservations from the electric stage, start yard | the 22 offline maps and this run's map: no early build on reserved tiles |
| 5 | Era segment 1: slots and bus for red/green, strip-designed blocks, trunks into the spine head | offline on the 22 maps; a short real-game test places segment 1 and its taps deliver |
| 6 | Segments 2+ (blue, oil zone), electric smelting swap, power planner, oil scouting | offline; short real-game tests per block |
| 7 | Full run from minute 0 | the organizer starts it (command given) |

Steps 1 to 3 fix the deciders and the waiting, and help the current world too. Steps 4 to 6 are the
floorplan, and pay off from a fresh world.
