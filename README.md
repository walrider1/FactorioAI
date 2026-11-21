# FactorioAI

An AI that plays Factorio on its own, from an empty inventory toward a rocket launch, live on YouTube as
**"AI Attempts to Beat Factorio"**. No human touches the game. The AI never sees the screen: it reads the map as
text, plans, builds through a game API and repairs its own factory.

When it gets truly stuck, the stream switches to a "Self-upgrading" screen. The problem is analysed, the fix is
approved by a human, and the bot's code is improved here. Every self-upgrade is a commit in this repository, and the
run either continues from its save or restarts, whichever makes for steadier progress.

## How it plays

- **Brain bot** (`bots/brain/`): a strategist (an LLM) sets goals from a roadmap toward the rocket. An overseer and
  an assessor (LLMs) audit the base and correct the plan. A scripted crew turns each goal into game actions at full
  speed, and a rule-based foreman keeps buildings fuelled and reports what it can't fix.
- **Layout engine** (`bots/fab/`): designs factories like chips. Machines are cells, simulated annealing places
  them, A* routes the belts (with undergrounds and rip-up/reroute), and a design-rule check verifies the result
  before anything is built. An LLM can draw the floor plan instead (`--placer llm`), from a text map of the ground.
- **Server** (`arena/`, `lua/`): the game API it plays through, a fork of Tesserac's
  [factorio-ai-arena](https://github.com/tesserac-ai/factorio-ai-arena) extended for the whole game (real tech tree,
  milestones up to a rocket launch, save and resume of long runs).
- **Stream** (`stream/`): OBS scenes, the overlay and screens, a multistream relay with a vertical cut for phones, and
  the watchdog: every hour Claude reviews the run and only alerts the phone when the bot is stuck for good. See
  [stream/README.md](stream/README.md).

## Run it

Needs Docker, Python 3.11+, uv and an OpenAI API key for the LLM roles (in `.env`, see `.env.example`).

```bash
uv sync --extra dev --extra openai
docker pull factoriotools/factorio:2.1.24
uv run arena local-run --backend factorio --task freeplay-open --bot brain --strategist openai     --placer llm --designer-model gpt-6.1-sol --clock-speed 2 --designs ai --max-cost 5 --overseer-max-cost 10
uv run arena monitor            # live page and the OBS overlay on http://127.0.0.1:8730
```

Watch it in the game: join as a spectator (Factorio 2.1.24, Multiplayer > Connect to `127.0.0.1:34300`, after
adding your Factorio username to `docker/config/server-whitelist.json`).

## The server it plays on (freeplay fork)

A copy of the arena stack (forked from `../` on 2026-11-18) extended for open-ended play: the base game's
real tech tree, an empty inventory, and runs from "make green science" to "launch a rocket". The
competition server in `../` is untouched; both can run side by side (this one uses gateway port 8720, RCON
27200+, spectators 34300+, containers named `freeplay-*`).

All original levels still run here unchanged. The new levels:

| Task | What |
|---|---|
| `freeplay-green` | From nothing to automated green science. 1 hour, milestones with a time bonus, ends when done. The character walks around water and buildings like a player (`walking: path`) |
| `freeplay-open` | From nothing, as far as you can get: 21 milestones up to a rocket launch. No practical time limit (30 days); ends when you stop it (Ctrl+C scores it) or a rocket launches |

## What changed in the server

**The game's own data.** `arena catalog` boots a game and writes `config/catalog.json`: every item, fluid,
recipe, technology (prerequisites, cost, unlocks, triggers), buildable entity (size, speeds, power) and
resource. The server validates names against it (still a fixed set, so free text never reaches Lua; no
hand-kept whitelists for hundreds of names), snaps any entity size with it, and serves it at
`GET /v1/catalog` so bots plan from real numbers. Regenerate it when the game version changes.

**The real tech tree** (`tech: real` in a task). Recipes start as in a new game. Research runs in labs
(`research` verb: replace or append to the queue). Factorio 2.1 trigger technologies complete when their
trigger happens (steam power: 50 iron plates made; electronics: 10 copper plates; red science: a lab
crafted; oil processing: crude oil mined). The scenario's verbs act through scripts, so the game's own
trigger events never fire; the scenario checks triggers every second instead. Crafting and `set_recipe`
refuse locked recipes, and hand crafting refuses machine-only recipes, as the game does.

**New verbs.**

| Verb | Kind | What |
|---|---|---|
| `stats` | read-only | The production panel: made/used totals and per-minute rates for every item and fluid over the last N minutes, hand-crafted counts, research, milestones |
| `scan` | read-only | Every resource patch (clustered: size, amount, box, centre, nearest tile) and the nearest water within `limits.scan_radius` |
| `research` | action | Set the research queue |
| `batch` | action | Up to `limits.batch_max_ops` instant ops (place, place_line, insert, extract, rotate, set_recipe) on one tick; each op costs `batch_op_gap_ticks` of the rate limit |
| `observe` (extended) | read-only | Centre anywhere within `limits.observe_range` of the character, `include` only some parts, `detail: brief`, `only_problems`, `names` filter, `limit`; fluids, labs and silos described |
| `place`, `place_line` (belts) | action | Every belt placement reports `merges`: each join it makes with a belt that is not part of it (side-loads, a belt run on from behind, an old belt's dead end pointing into the new line anywhere along it). `carries: [items]` tags what the belt is for. `isolated: true` refuses any join except with the tiles in `allow` (`would_merge`, nothing left placed) |
| `observe` `belts: true` | read-only | Each belt's two lanes (`{item: count}` each) and the belts it `feeds` and is `fed_by`. A belt placed with `carries` that holds anything else has status `foreign_items` (also under `only_problems`). Mixing is never judged as such, only against what was declared |

Read-only verbs use the observe budget and are never rate-limited, so a bot can watch a big base without
slowing its building.

**Scale knobs per task** (`limits`): observe radius (hard cap 64), observe range, scan radius (cap 512),
batch size (cap 100) and cost, chart radius (the map is generated and charted around the character as it
explores). `clock_speed` runs a realtime level faster than real time (budgets stay in game ticks), for
quick test runs of long games. Gateway limits are raised in `config/competition.yaml` (40 requests a second,
64 KB bodies).

**Rockets launch on their own.** A script character has no silo window to press "Launch" in and there is no
verb for it, so the scenario launches any silo whose rocket is ready (checked every 10 s), like the old silo's
"auto launch" checkbox. Without it the "A rocket launched" milestone could not be reached through the API.

**Belts carry what they were laid for.** Run 20261120-211555: coal fuel strips were laid with their last belt
pointing into the side of iron belts laid earlier, and their spare coal ran onto the iron feeds of two gear
factories and red science for the rest of the run. The server's old note only looked at a new line's first and
last tile, and nothing could see belt contents. Now the game's own belt connections are reported on every
placement, a bot can make the server refuse joins it did not plan, and observe shows lanes and foreign items.
A resumed run gets the scenario's current script (the backend swaps it into its copy of the save), so server
fixes reach saved games too.

**Rate milestones for several items.** `also: [items]` on an item + `per_minute` milestone: every one must make
that rate. "Red and green at 30/min each" checked only green before; it now asks both.

**Milestone scoring** (`score.type: milestones`). A ladder of conditions: a technology researched, an item
or fluid made at a rate over a window (automated: hand-crafted items never count), a total made, N of an
entity working at once, rockets launched. The game checks every 10 s and records the tick each is first
reached. Score = points of reached milestones + up to `time_bonus` x points for reaching each early. With
`end_when_complete` the run ends as soon as the last is reached. The live per-action score is cheap (no
scan of every entity), so it stays fast on big bases.

## Run it

```bash
uv sync --extra dev
uv run arena catalog                                       # once per game version (already done for 2.1.24)
uv run python -m pytest                                    # offline tests
ARENA_FACTORIO=1 uv run python -m pytest tests/test_freeplay_factorio.py   # the new verbs on real Factorio
uv run arena local-run --backend factorio --task freeplay-green --bot brain
uv run arena serve                                         # gateway on 127.0.0.1:8720
```

## Watching a run

`arena local-run` serves a live monitor at http://127.0.0.1:8730 while it plays (`--monitor 0` turns it
off); `arena monitor` serves the same page for every run under `runs/`, including ones played through
`arena serve`. It reads only the run folders: the server's action log (every API call, budgets, score,
stats) and, for the brain bot, `bot_live.json` and `bot_events.jsonl`. It binds to 127.0.0.1 and never
touches a game. A strip of five numbers stays on top (clock, score, stages of the road to the rocket, power
against demand, AI spend), and the rest is in tabs:

| Tab | What |
|---|---|
| Overview | The road to the rocket (12 stages, the book's next step), science made a minute against what the road needs, research left (techs, packs, lab hours), milestones, what the crew is doing now |
| Base | The map with the workshop's cells, the workshop's shortages and stock, plate supply against demand, the foreman and courier, every site, production, inventory |
| AI | The strategist's last plan and doctrine, the supervisor's alerts and fixes, the fixer's diagnoses, a filterable timeline |
| Control loop | The live loop graph and its trace |
| Commands | The raw API log |

## Recording a run

1. Start the run as usual. The monitor's top bar shows `spectate 127.0.0.1:34300` (34300 + the slot).
2. Open Factorio **2.1.24** (the server's version), Multiplayer → Connect to address → `127.0.0.1:34300`. Your
   Factorio name must be in `docker/config/server-whitelist.json`. You join as a spectator next to the bot.
3. In chat, type `/cam on`. The camera follows the bot loosely: it aims at a running average of where the bot
   has been (so back-and-forth trips cancel out), holds still inside a dead zone, eases toward it outside it,
   and keeps up on long walks. Tune it live: `/cam zoom 0.4` (smaller sees more), `/cam dead 15` (tiles of slack),
   `/cam ease 0.02` (slower glide). `/cam off` gives you the camera back. `/cam` alone prints the settings.
4. Record, either way:
   - **Real time:** OBS Studio, Game Capture of the Factorio window. Add the monitor page
     (http://127.0.0.1:8730) as a Browser Source for a panel showing what the AI is thinking.
   - **Timelapse, unattended:** `/cam frames 120` makes the game render a 1920x1080 frame from the camera every
     120 game ticks (in daylight, without the GUI; `/cam day off` keeps night, `/cam size 2560 1440` changes the
     size) into `%APPDATA%\Factorio\script-output\arena-cam\<name>-t<tick>\`. Keep the game window open (it may
     sit behind others; minimised windows may stop rendering). One game hour at 120 ticks is 1,800 frames, about
     1 GB, a 60 s video at 30 fps:
     `ffmpeg -framerate 30 -i %07d.jpg -c:v libx264 -crf 18 -pix_fmt yuv420p timelapse.mp4`
   If the client disconnects, rejoin and `/cam on` again (frames go to a new folder).

**A developer sandbox.** `sandbox-workshop` has every technology researched and a kit of machines, for checking
modules in a real game in minutes: `ARENA_FACTORIO=1 uv run python -m pytest tests/test_workshop_factorio.py -s`.

## Saving and carrying on

A long run does not have to start from scratch. The brain bot on the factorio backend saves at safe points
(between goals, never with a build half placed) into `runs/local/<id>/saves/`:

- `<name>.zip`: the game itself (`/server-save`, copied out of the container).
- `<name>.session.json`: the arena session's clock, budgets and counters, so the score's time bonus keeps
  counting game time from the first start.
- `<name>.bot.pkl` and `.bot.json`: the bot's memory, which the game does not tell it again. That is its sites
  and what they are for, belt links and their ports, sealed sites, the foreman's book, the planners' doctrine
  and notes, the goal queue and results, and the map it has learned.

It saves every `--autosave` game minutes (20 by default; 0 turns it off). On the monitor's **Saves** tab you
can save now, or save and stop. After a stop the page stays up, and **Resume** on any complete save starts the
run again in the same run folder with the flags it was started with. From a terminal:

```bash
uv run arena local-run --resume 20261120-101500-a1b2c3          # the newest save, the original flags
uv run arena local-run --resume 20261120-101500-a1b2c3:m00240-manual-153012 --clock-speed 3
```

Flags given with `--resume` override the stored ones. A run started before this feature (or on the sim
backend) cannot save, and its Save buttons stay off. The save carries the scenario script it was made with, so
changes to `lua/` apply to new runs only. Changes to the bot's Python apply on resume.

## The brain bot

`bots/brain/` plays these levels: an LLM strategist decides what to build and research, and a scripted
crew, scheduler and foreman do it in the background. See [bots/brain/README.md](bots/brain/README.md).
