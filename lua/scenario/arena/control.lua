-- Factorio AI Arena scenario (Factorio 2.0 / 2.1).
--
-- The organizer's controller drives this script through remote.call("arena", ...)
-- over RCON on 127.0.0.1. Teams never reach RCON; the gateway only forwards
-- schema-validated actions whose fields are numbers or whitelisted names, passed
-- here as JSON data (never as code).
--
-- Two clocks:
--  * paused (default): the world is paused (game.tick_paused) except while an action or
--    `advance` runs ticks via game.ticks_to_run.
--  * realtime: after start_clock the world runs at normal speed (60 ticks a second) and
--    actions apply the moment they arrive. Walking takes real game time (on_tick moves the
--    character), hand jobs (mine, pickup, craft) keep the hands busy, actions are rate-limited,
--    and the world pauses itself exactly when the build time is up.
--    Each action reports the tick it was applied at, so a realtime run replays exactly:
--    run the world (paused) to that tick, then apply the action.

local SURFACE = "arena"
local REACH = 10
local RESOURCE_REACH = 3
local RUN_SPEED = 0.15            -- tiles per tick
local MINING_SPEED = 0.5          -- character hand-mining speed
local MAX_MOVE = 200
local DIR = { north = defines.direction.north, east = defines.direction.east,
              south = defines.direction.south, west = defines.direction.west }
local DIR_NAME = {}
for k, v in pairs(DIR) do DIR_NAME[v] = k end
local STATUS_NAME = nil

-- read-only verbs: they never change the game, are never rate-limited, and use the observe budget
local READ_ONLY = { observe = true, stats = true, scan = true }
local SNAPSHOT_EVERY = 600        -- ticks between production snapshots (rates, milestones)
local SNAPSHOT_KEEP = 360         -- one hour of history at 10 s
local OK_STATUS = { working = true, normal = true }   -- anything else is a problem worth reporting

-- ------------------------------------------------------------------ helpers
local function try(fn)
  local okc, v = pcall(fn)
  if okc then return v end
  return nil
end

-- recipe categories: 2.1 gives recipes a list (categories), 2.0 a single category
local function recipe_cats(r)
  local list = try(function() return r.categories end)
  if list then return list end
  local one = try(function() return r.category end)
  return one and { one } or {}
end

local function any_cat(r, allowed)
  for _, c in pairs(recipe_cats(r)) do
    if allowed[c] then return true end
  end
  return false
end

local function ok(data, ticks)
  return { ok = true, data = data or {}, ticks = ticks or 0 }
end

local function fail(code, message)
  return { ok = false, code = code, message = message or code }
end

local function surface() return game.surfaces[SURFACE] end
local function character() return storage.character end

local function stats()
  return game.forces.player.get_item_production_statistics(surface())
end

local function contents(inv)
  local out = {}
  if not inv then return out end
  for k, v in pairs(inv.get_contents()) do
    if type(v) == "table" then
      out[v.name] = (out[v.name] or 0) + v.count
    else
      out[k] = v
    end
  end
  return out
end

local function status_name(e)
  if not STATUS_NAME then
    STATUS_NAME = {}
    for k, v in pairs(defines.entity_status) do STATUS_NAME[v] = k end
  end
  local s = e.status
  return (s and STATUS_NAME[s]) or "idle"
end

-- 2.1 renamed furnace_source to crafter_input; accept either so the scenario runs on 2.0 and 2.1
local FURNACE_SOURCE = defines.inventory.crafter_input or defines.inventory.furnace_source

-- ------------------------------------------------------------------ belts: contents, neighbours, intent
-- A player sees at a glance what a belt carries and where it joins another. The API says it too: observe
-- {belts = true} gives every belt's two lanes and the belts it feeds and is fed by; a belt placed with
-- carries = {items} is flagged "foreign_items" (also under only_problems) when anything else gets on it.
-- Mixing is never judged as such (lane-split and sushi belts are fine): only against what was declared.
local BELT_TYPES = { ["transport-belt"] = true, ["underground-belt"] = true, ["splitter"] = true }
local BELT_DETAIL = false                        -- set by observe {belts = true} while it describes entities

local function tile_key(x, y) return math.floor(x) .. "," .. math.floor(y) end

local function lanes_of(e)
  local lanes = { {}, {} }
  local n = try(function() return e.get_max_transport_line_index() end) or 0
  for i = 1, n do
    local line = try(function() return e.get_transport_line(i) end)
    if line then
      local lane = lanes[(i - 1) % 2 + 1]
      for k, v in pairs(contents(line)) do lane[k] = (lane[k] or 0) + v end
    end
  end
  return lanes
end

local function belt_neighbours(e)
  local n = try(function() return e.belt_neighbours end)
  if type(n) ~= "table" then return {}, {} end
  return n.inputs or {}, n.outputs or {}
end

local function carried(e)                        -- the items the builder said this belt carries (nil: unsaid)
  local t = storage.belt_carries
  return t and e.unit_number and t[e.unit_number] or nil
end

local function foreign_items(e)
  local want = carried(e)
  if not want then return nil end
  local bad = nil
  for _, lane in ipairs(lanes_of(e)) do
    for item, n in pairs(lane) do
      if not want[item] then
        bad = bad or {}
        bad[item] = (bad[item] or 0) + n
      end
    end
  end
  return bad
end

local function add_belt_detail(e, d)
  if not BELT_TYPES[e.type] then return end
  local want = carried(e)
  if want then
    local list = {}
    for k in pairs(want) do list[#list + 1] = k end
    table.sort(list)
    d.carries = list
    local bad = foreign_items(e)
    if bad then d.status, d.foreign = "foreign_items", bad end
  end
  if BELT_DETAIL then
    d.lanes = lanes_of(e)
    local ins, outs = belt_neighbours(e)
    d.fed_by, d.feeds = {}, {}
    for _, o in pairs(ins) do d.fed_by[#d.fed_by + 1] = { o.position.x, o.position.y } end
    for _, o in pairs(outs) do d.feeds[#d.feeds + 1] = { o.position.x, o.position.y } end
  end
end

local function describe(e)
  local d = { name = e.name, x = e.position.x, y = e.position.y,
              direction = DIR_NAME[e.direction] or "north", status = status_name(e) }
  if e.type == "electric-pole" then d.network = e.electric_network_id end   -- which grid it is on
  local inv = {}
  if e.type == "mining-drill" or e.type == "inserter" or e.type == "boiler" then
    if e.burner then inv.fuel = contents(e.get_fuel_inventory()) end
    if e.type == "inserter" then                  -- what a player sees as the inserter's arrows
      local p, q = e.pickup_position, e.drop_position
      d.pickup = { math.floor(p.x * 10 + 0.5) / 10, math.floor(p.y * 10 + 0.5) / 10 }
      d.drop = { math.floor(q.x * 10 + 0.5) / 10, math.floor(q.y * 10 + 0.5) / 10 }
    elseif e.type == "mining-drill" then          -- where the ore lands
      local q = e.drop_position
      d.drop = { math.floor(q.x * 10 + 0.5) / 10, math.floor(q.y * 10 + 0.5) / 10 }
    end
  elseif e.type == "assembling-machine" then
    local r = e.get_recipe()
    d.recipe = r and r.name or nil
    local input = e.get_inventory(FURNACE_SOURCE)
    inv.input = contents(input)
    inv.output = contents(e.get_output_inventory())
    if r then                                     -- what the machine's window shows as missing
      local missing = {}
      for _, ing in pairs(r.ingredients) do
        local have
        if ing.type == "fluid" then
          have = try(function() return e.get_fluid_count(ing.name) end) or 0
        else
          have = input.get_item_count(ing.name)
        end
        if have < ing.amount then missing[#missing + 1] = ing.name end
      end
      if #missing > 0 then d.missing = missing end
    end
    if e.burner then inv.fuel = contents(e.get_fuel_inventory()) end
  elseif e.type == "furnace" then
    if e.burner then inv.fuel = contents(e.get_fuel_inventory()) end
    inv.source = contents(e.get_inventory(FURNACE_SOURCE))
    inv.output = contents(e.get_output_inventory())
  elseif e.type == "lab" then
    inv.input = contents(e.get_inventory(defines.inventory.lab_input))
  elseif e.type == "rocket-silo" then
    d.rocket_parts = try(function() return e.rocket_parts end)
    inv.input = contents(e.get_inventory(FURNACE_SOURCE))
  elseif e.type == "container" or e.type == "logistic-container" then
    inv.contents = contents(e.get_inventory(defines.inventory.chest))
  elseif e.type == "underground-belt" then        -- which end, and the end it pairs with
    d.belt_type = e.belt_to_ground_type
    for _, key in ipairs({ "underground_belt_neighbour", "neighbours" }) do   -- the name differs between versions
      local okn, n = pcall(function() return e[key] end)
      if okn and n and type(n) ~= "table" or (okn and n and n.valid ~= nil) then
        if n.valid then d.pairs_with = { n.position.x, n.position.y } end
        break
      end
    end
  end
  d.inventories = inv
  -- fluids: what each fluid box holds (pipes, boilers, engines, refineries, chemical plants...)
  local nboxes = try(function() return #e.fluidbox end) or 0
  if nboxes > 0 then
    local fl = {}
    for i = 1, nboxes do
      local f = e.fluidbox[i]
      if f then fl[#fl + 1] = { name = f.name, amount = math.floor(f.amount * 10 + 0.5) / 10,
                                temperature = f.temperature and math.floor(f.temperature) or nil } end
    end
    if #fl > 0 then d.fluids = fl end
  else                                            -- 2.1: no runtime fluidbox; the totals per fluid instead
    local fc = try(function() return e.get_fluid_contents() end)
    if type(fc) == "table" then
      local fl = {}
      for k, v in pairs(fc) do
        if type(v) == "table" then
          fl[#fl + 1] = { name = v.name, amount = math.floor((v.amount or 0) * 10 + 0.5) / 10 }
        else
          fl[#fl + 1] = { name = k, amount = math.floor(v * 10 + 0.5) / 10 }
        end
      end
      if #fl > 0 then d.fluids = fl end
    end
  end
  if storage.delivery and storage.delivery.valid and e == storage.delivery then d.protected = true end
  add_belt_detail(e, d)
  return d
end

-- the short form for big observes: what a player sees at a glance on the map
local function brief(e)
  local d = { name = e.name, x = e.position.x, y = e.position.y,
              direction = DIR_NAME[e.direction] or "north", status = status_name(e) }
  if e.type == "electric-pole" then d.network = e.electric_network_id end   -- which grid it is on
  if e.type == "assembling-machine" then
    local r = e.get_recipe()
    d.recipe = r and r.name or nil
  end
  if BELT_TYPES[e.type] and carried(e) and foreign_items(e) then d.status = "foreign_items" end
  return d
end

local function dist_to_box(p, x0, y0, x1, y1)
  local dx = math.max(x0 - p.x, 0, p.x - x1)
  local dy = math.max(y0 - p.y, 0, p.y - y1)
  return math.sqrt(dx * dx + dy * dy)
end

local function footprint(name, x, y, dir)
  local proto = prototypes.entity[name]
  local w, h = proto.tile_width, proto.tile_height
  if dir == defines.direction.east or dir == defines.direction.west then w, h = h, w end
  return x - w / 2, y - h / 2, x + w / 2, y + h / 2
end

local function in_reach(e)
  local x0, y0, x1, y1 = footprint(e.name, e.position.x, e.position.y, e.direction)
  return dist_to_box(character().position, x0, y0, x1, y1) <= REACH
end

local function entity_at(x, y)
  local found = surface().find_entities_filtered{
    area = { { x - 0.3, y - 0.3 }, { x + 0.3, y + 0.3 } }, force = "player" }
  for _, e in pairs(found) do
    if e.valid and e.type ~= "character" then return e end
  end
  return nil
end

local function resource_at(x, y)
  local tx, ty = math.floor(x), math.floor(y)
  local found = surface().find_entities_filtered{
    area = { { tx + 0.1, ty + 0.1 }, { tx + 0.9, ty + 0.9 } }, type = "resource", limit = 1 }
  return found[1]
end

local function run(ticks)
  if ticks > 0 then game.ticks_to_run = ticks end
  return game.tick + ticks
end

-- ------------------------------------------------------------------- setup
local function setup(args)
  game.tick_paused = true
  local mgs = game.surfaces.nauvis.map_gen_settings
  mgs.seed = args.seed
  if mgs.autoplace_controls and mgs.autoplace_controls["enemy-base"] then
    mgs.autoplace_controls["enemy-base"].size = 0
    mgs.autoplace_controls["enemy-base"].frequency = 0
  end
  local s = game.create_surface(SURFACE, mgs)
  s.request_to_generate_chunks({ 0, 0 }, 4)
  s.force_generate_chunk_requests()
  for _, e in pairs(s.find_entities_filtered{ force = "enemy" }) do e.destroy() end
  -- spectators: chart the area so map view (M) shows it, even while the world is paused
  game.forces.player.chart(s, { { -192, -192 }, { 192, 192 } })

  local pos = s.find_non_colliding_position("character", { 0, 0 }, 64, 0.5)
  local c = s.create_entity{ name = "character", position = pos, force = "player" }
  storage.character = c
  local inv = c.get_main_inventory()
  for name, count in pairs(args.inventory or {}) do
    if count > 0 then inv.insert{ name = name, count = count } end
  end

  storage.score_item = args.score_item
  storage.realtime = args.realtime and true or false
  storage.min_gap = args.min_action_gap or 0
  storage.fast_speed = args.speed or 1
  storage.clear_obstacles = args.clear_obstacles and true or false
  storage.show_water_summary = args.recipes ~= nil or args.water_summary == true
  for _, name in pairs(args.recipes or {}) do          -- automation level: unlocked without research
    local r = game.forces.player.recipes[name]
    if r then r.enabled = true end
  end
  -- tech: "unlock" (the recipes above, no research), "real" (the base game's tech tree: research in labs,
  -- trigger technologies complete when their trigger happens), "all" (everything researched)
  storage.tech = args.tech or "unlock"
  if storage.tech == "all" then game.forces.player.research_all_technologies() end
  storage.trigger_techs = {}
  if storage.tech == "real" then
    for name, t in pairs(game.forces.player.technologies) do
      if t.prototype.research_trigger and not t.researched then storage.trigger_techs[#storage.trigger_techs + 1] = name end
    end
  end
  storage.max_observe_radius = args.max_observe_radius or 20
  storage.observe_range = args.observe_range or 0    -- how far from the character an observe may be centred
  storage.scan_radius = args.scan_radius or 0
  storage.batch_op_gap = args.batch_op_gap or storage.min_gap
  storage.clock_speed = args.clock_speed or 1
  storage.chart_radius = args.chart_radius or 0
  storage.pathing = args.walking == "path"         -- walk around water and buildings, like a player
  storage.milestones = args.milestones or {}
  storage.reached = {}
  storage.hand_made = {}      -- crafted by hand: never counts as automated production
  storage.mined = {}          -- hand-mined, by resource entity name
  storage.snaps = {}          -- production snapshots every SNAPSHOT_EVERY ticks (rates, milestones)
  storage.setup_done = true
  if args.delivery then
    local want = { pos.x + args.delivery[1], pos.y + args.delivery[2] }
    local dpos = s.find_non_colliding_position("iron-chest", want, 20, 1, true)
    local chest = s.create_entity{ name = "iron-chest", position = dpos, force = "player" }
    pcall(function() chest.minable_flag = false end)
    pcall(function() chest.minable = false end)
    chest.destructible = false
    storage.delivery = chest
  end
  storage.baseline = (args.score_item and args.score_item ~= "") and stats().get_input_count(args.score_item) or 0
  game.map_settings.pollution.enabled = false
  game.map_settings.enemy_expansion.enabled = false
  game.map_settings.enemy_evolution.enabled = false
  game.speed = args.speed or 1
  game.tick_paused = true
  return { spawn = c.position, delivery = storage.delivery and storage.delivery.position or nil,
           tick = game.tick }
end

-- ----------------------------------------------------------------- actions
local A = {}
local HAND_JOBS = { mine = true, pickup = true, craft = true }   -- realtime: one at a time

local WATER_TILES = { "water", "deepwater", "water-green", "deepwater-green", "water-shallow", "water-mud" }

-- observe: what is around a point. By default the character; tasks with observe_range let you look
-- anywhere within that distance of the character (like a player's map view). Options keep big
-- factories cheap: include (which parts), detail = brief, only_problems, names, limit.
function A.observe(a)
  local s, c = surface(), character()
  local cp = c.position
  local p = cp
  if a.x and a.y then
    local dx, dy = a.x - cp.x, a.y - cp.y
    if math.sqrt(dx * dx + dy * dy) > (storage.observe_range or 0) + 0.01 then
      return fail("out_of_range", "observe centre must be within " .. (storage.observe_range or 0) ..
        " tiles of the character")
    end
    p = { x = a.x, y = a.y }
  end
  local radius = math.min(a.radius or 16, storage.max_observe_radius or 20)
  local want = {}
  if a.include then
    for _, k in pairs(a.include) do want[k] = true end
  else
    want = { entities = true, resources = true, summary = true, obstacles = true, water = true }
  end
  local limit = a.limit or 2000
  local result = { tick = game.tick, position = { x = cp.x, y = cp.y }, centre = { x = p.x, y = p.y },
                   radius = radius, inventory = contents(c.get_main_inventory()) }
  if want.entities then
    BELT_DETAIL = a.belts and true or false
    local ents, n, more = {}, 0, false
    local filter = { position = p, radius = radius, force = "player" }
    if a.names then filter.name = a.names end
    for _, e in pairs(s.find_entities_filtered(filter)) do
      if e.type ~= "character" then
        local keep = true
        if a.only_problems then
          keep = not OK_STATUS[status_name(e)] or (BELT_TYPES[e.type] and foreign_items(e) ~= nil)
        end
        if keep then
          if n >= limit then more = true; break end
          n = n + 1
          ents[n] = (a.detail == "brief") and brief(e) or describe(e)
        end
      end
    end
    BELT_DETAIL = false
    result.entities = ents
    if more then result.entities_truncated = true end
  end
  if want.resources then
    local resources = {}
    for _, r in pairs(s.find_entities_filtered{ position = p, radius = radius, type = "resource" }) do
      local list = resources[r.name]
      if not list then list = {}; resources[r.name] = list end
      list[#list + 1] = { r.position.x, r.position.y, r.amount }
    end
    result.resources = resources
  end
  if want.summary then
    local summary = {}
    for _, r in pairs(s.find_entities_filtered{ position = p, radius = 64, type = "resource" }) do
      local sm = summary[r.name]
      if not sm then sm = { distance = 1e9, tiles = 0, amount = 0 }; summary[r.name] = sm end
      sm.tiles = sm.tiles + 1
      sm.amount = sm.amount + r.amount
      local dx, dy = r.position.x - cp.x, r.position.y - cp.y
      local d = math.sqrt(dx * dx + dy * dy)
      if d < sm.distance then
        sm.distance = math.floor(d * 100 + 0.5) / 100
        sm.nearest = { x = r.position.x, y = r.position.y }
      end
    end
    result.resource_summary = summary
  end
  if want.obstacles then
    local obstacles = {}
    for _, o in pairs(s.find_entities_filtered{ position = p, radius = radius,
        type = { "tree", "simple-entity", "cliff" }, limit = 12000 }) do
      obstacles[#obstacles + 1] = { o.position.x, o.position.y, o.type }
    end
    result.obstacles = obstacles
  end
  if want.water then
    local water = {}
    for _, t in pairs(s.find_tiles_filtered{ position = p, radius = radius, name = WATER_TILES, limit = 12000 }) do
      water[#water + 1] = { t.position.x + 0.5, t.position.y + 0.5 }
    end
    result.water = water
  end
  if storage.show_water_summary and want.summary then  -- where the nearest water is
    local best, bd, n = nil, 1e18, 0
    for _, t in pairs(s.find_tiles_filtered{ position = cp, radius = 64,
        name = { "water", "deepwater", "water-green", "deepwater-green" } }) do
      n = n + 1
      local dx, dy = t.position.x + 0.5 - cp.x, t.position.y + 0.5 - cp.y
      local d = dx * dx + dy * dy
      if d < bd then bd, best = d, t.position end
    end
    if best then
      result.water_summary = { tiles = n, distance = math.floor(math.sqrt(bd) * 100 + 0.5) / 100,
                               nearest = { x = best.x + 0.5, y = best.y + 0.5 } }
    end
  end
  return ok(result)
end

-- --------------------------------------------------------------- walking
-- Levels with walking = "path" walk like a player: around water, cliffs, rocks and buildings, never through
-- them. A tile grid A* (8 directions, no corner cutting) over what blocks a character. Trees are left
-- passable: a real character slips between them, and a tile grid would wall in every forest.
local WALK_MARGIN = 24
local WALK_MAX_NODES = 60000
local DEEP_WATER = { "water", "deepwater", "water-green", "deepwater-green" }

local function walk_blockers(s, x0, y0, x1, y1)
  local blocked = {}
  local area = { { x0, y0 }, { x1 + 1, y1 + 1 } }
  local tiles = try(function() return s.find_tiles_filtered{ area = area, collision_mask = "player" } end)
  if not tiles then tiles = s.find_tiles_filtered{ area = area, name = DEEP_WATER } end
  for _, t in pairs(tiles) do blocked[t.position.x .. "," .. t.position.y] = true end
  local ents = try(function() return s.find_entities_filtered{ area = area, collision_mask = "player" } end)
  if not ents then
    ents = s.find_entities_filtered{ area = area, type = { "simple-entity", "cliff", "assembling-machine",
      "furnace", "mining-drill", "boiler", "generator", "lab", "container", "inserter", "electric-pole", "pipe",
      "pipe-to-ground", "offshore-pump", "storage-tank", "rocket-silo", "radar", "pump" } }
  end
  for _, e in pairs(ents) do
    if e.type ~= "character" and e.type ~= "tree" then
      local bb = e.bounding_box
      for tx = math.floor(bb.left_top.x + 0.05), math.ceil(bb.right_bottom.x - 0.05) - 1 do
        for ty = math.floor(bb.left_top.y + 0.05), math.ceil(bb.right_bottom.y - 0.05) - 1 do
          blocked[tx .. "," .. ty] = true
        end
      end
    end
  end
  return blocked
end

-- returns a list of points from start to goal (tile centres in between), and its length; nil if no way
local function walk_path(from, to)
  local s = surface()
  local sx, sy = math.floor(from.x), math.floor(from.y)
  local gx, gy = math.floor(to.x), math.floor(to.y)
  local x0, x1 = math.min(sx, gx) - WALK_MARGIN, math.max(sx, gx) + WALK_MARGIN
  local y0, y1 = math.min(sy, gy) - WALK_MARGIN, math.max(sy, gy) + WALK_MARGIN
  local blocked = walk_blockers(s, x0, y0, x1, y1)
  local function free(x, y)
    return x >= x0 and x <= x1 and y >= y0 and y <= y1 and not blocked[x .. "," .. y]
  end
  local function h(x, y)
    local dx, dy = math.abs(x - gx), math.abs(y - gy)
    return math.max(dx, dy) + 0.4142 * math.min(dx, dy)
  end
  local open, gs, prev, closed = {}, {}, {}, {}
  local function push(node, f)                    -- binary heap on f
    open[#open + 1] = { f, node }
    local i = #open
    while i > 1 do
      local p = math.floor(i / 2)
      if open[p][1] <= open[i][1] then break end
      open[p], open[i] = open[i], open[p]
      i = p
    end
  end
  local function pop()
    local top = open[1]
    local last = open[#open]
    open[#open] = nil
    if #open > 0 then
      open[1] = last
      local i = 1
      while true do
        local l, r, m = 2 * i, 2 * i + 1, i
        if l <= #open and open[l][1] < open[m][1] then m = l end
        if r <= #open and open[r][1] < open[m][1] then m = r end
        if m == i then break end
        open[m], open[i] = open[i], open[m]
        i = m
      end
    end
    return top[2]
  end
  local start = sx .. "," .. sy
  local goal = gx .. "," .. gy
  gs[start] = 0
  push({ sx, sy }, h(sx, sy))
  local steps = { { 1, 0, 1 }, { -1, 0, 1 }, { 0, 1, 1 }, { 0, -1, 1 },
                  { 1, 1, 1.4142 }, { 1, -1, 1.4142 }, { -1, 1, 1.4142 }, { -1, -1, 1.4142 } }
  local n = 0
  while #open > 0 do
    local cur = pop()
    local ck = cur[1] .. "," .. cur[2]
    if not closed[ck] then
      closed[ck] = true
      if ck == goal then
        local pts = { { to.x, to.y } }
        local k = prev[ck]
        while k and k ~= start do
          local cx, cy = k:match("^(-?%d+),(-?%d+)$")
          pts[#pts + 1] = { tonumber(cx) + 0.5, tonumber(cy) + 0.5 }
          k = prev[k]
        end
        pts[#pts + 1] = { from.x, from.y }
        local out, len = {}, 0                      -- reverse, and drop points on a straight run
        for i = #pts, 1, -1 do out[#out + 1] = pts[i] end
        local simple = { out[1] }
        for i = 2, #out - 1 do
          local a, b, c = simple[#simple], out[i], out[i + 1]
          local cross = (b[1] - a[1]) * (c[2] - b[2]) - (b[2] - a[2]) * (c[1] - b[1])
          if math.abs(cross) > 1e-6 then simple[#simple + 1] = b end
        end
        simple[#simple + 1] = out[#out]
        for i = 2, #simple do
          local dx, dy = simple[i][1] - simple[i - 1][1], simple[i][2] - simple[i - 1][2]
          len = len + math.sqrt(dx * dx + dy * dy)
        end
        return simple, len
      end
      n = n + 1
      if n > WALK_MAX_NODES then return nil end
      for _, st in ipairs(steps) do
        local nx, ny = cur[1] + st[1], cur[2] + st[2]
        local nk = nx .. "," .. ny
        local okmove = (free(nx, ny) or nk == goal) and not closed[nk]
        if okmove and st[1] ~= 0 and st[2] ~= 0 then     -- no squeezing past a corner
          okmove = free(cur[1] + st[1], cur[2]) and free(cur[1], cur[2] + st[2])
        end
        if okmove then
          local g = gs[ck] + st[3]
          if gs[nk] == nil or g < gs[nk] then
            gs[nk], prev[nk] = g, ck
            push({ nx, ny }, g + h(nx, ny))
          end
        end
      end
    end
  end
  return nil
end

function A.move(a)
  local c = character()
  local p = c.position
  local dx, dy = a.x - p.x, a.y - p.y
  local d = math.sqrt(dx * dx + dy * dy)
  if d > MAX_MOVE then return fail("too_far") end
  local ticks = math.ceil(d / RUN_SPEED)
  if ticks > a.max_ticks then return fail("insufficient_ticks", "move needs " .. ticks .. " ticks") end
  local dest = surface().find_non_colliding_position("character", { a.x, a.y }, 10, 0.5)
  if not dest then return fail("blocked") end
  if storage.realtime then           -- walk there in game time; a new move redirects from where you are
    local pts, len
    if storage.pathing then
      pts, len = walk_path(p, dest)
      if not pts then
        return fail("no_path", "no walkable way to (" .. math.floor(dest.x) .. ", " .. math.floor(dest.y) ..
          "): water, cliffs or buildings are in the way")
      end
    else
      local dx2, dy2 = dest.x - p.x, dest.y - p.y
      pts, len = { { p.x, p.y }, { dest.x, dest.y } }, math.sqrt(dx2 * dx2 + dy2 * dy2)
    end
    local n = math.max(1, math.ceil(len / RUN_SPEED))
    storage.walk = { pts = pts, t0 = game.tick, t1 = game.tick + n }
    return ok({ position = { x = dest.x, y = dest.y }, from = { x = p.x, y = p.y },
                arrives_tick = game.tick + n, ticks = n, path_length = math.floor(len * 10 + 0.5) / 10,
                straight = math.floor(d * 10 + 0.5) / 10 }, 0)
  end
  c.teleport(dest)
  return ok({ position = { x = dest.x, y = dest.y }, ticks = ticks }, ticks)
end

local function place_one(name, x, y, dir, belt_type)
  local s, c = surface(), character()
  local x0, y0, x1, y1 = footprint(name, x, y, dir)
  if dist_to_box(c.position, x0, y0, x1, y1) > REACH then return nil, "out_of_reach" end
  if not s.can_place_entity{ name = name, position = { x, y }, direction = dir, force = "player",
                             build_check_type = defines.build_check_type.manual } then
    return nil, "cannot_place"
  end
  local e = s.create_entity{ name = name, position = { x, y }, direction = dir, force = "player",
                             type = belt_type, raise_built = true }
  if not e then return nil, "cannot_place" end
  return e
end

local function pump_hint(a)
  local fits = {}
  for _, name in ipairs({ "north", "east", "south", "west" }) do
    if surface().can_place_entity{ name = "offshore-pump", position = { a.x, a.y }, direction = DIR[name],
        force = "player", build_check_type = defines.build_check_type.manual } then
      fits[#fits + 1] = name
    end
  end
  if #fits > 0 then return "a pump fits on this tile facing " .. table.concat(fits, " or ") end
  return "no direction fits on this tile: use a land tile right at the water's edge"
end

-- belts: say what a new line connects to, so a missed corner is visible (a player sees it at a glance)
local SIDES = { { "north", 0, -1 }, { "east", 1, 0 }, { "south", 0, 1 }, { "west", -1, 0 } }
local VEC = { north = { 0, -1 }, east = { 1, 0 }, south = { 0, 1 }, west = { -1, 0 } }
local BACK = { north = "south", south = "north", east = "west", west = "east" }

local function belt_at(x, y)
  return surface().find_entities_filtered{ position = { x, y }, type = "transport-belt", limit = 1 }[1]
end

local function at(x, y) return "(" .. x .. ", " .. y .. ")" end

local function belt_links(x1, y1, x2, y2, dir)
  local v = VEC[dir]
  local out = {}
  local ax, ay = x2 + v[1], y2 + v[2]                       -- the tile the last belt moves items onto
  local ahead = belt_at(ax, ay)
  if ahead then
    local d = DIR_NAME[ahead.direction]
    if d == dir then out["end"] = "continues into the belt at " .. at(ax, ay)
    elseif d == BACK[dir] then out["end"] = "runs head-on into the belt at " .. at(ax, ay) .. ": items stop"
    else out["end"] = "feeds into the side of the belt at " .. at(ax, ay) .. " (facing " .. d .. ")" end
  else
    local e = entity_at(ax, ay)
    if e then
      out["end"] = "stops at the " .. e.name .. " at " .. at(ax, ay) .. ": belts never insert, put an inserter there"
    else
      out["end"] = "nothing ahead: items stop on the last belt " .. at(x2, y2)
    end
  end
  local feeder, wrong = nil, nil
  for _, s in ipairs(SIDES) do                              -- belts next to the first tile
    local bx, by = x1 - s[2], y1 - s[3]
    if not (bx == x1 + v[1] and by == y1 + v[2]) and not (x1 == x2 and y1 == y2 and bx == ax and by == ay) then
      local b = belt_at(bx, by)
      if b then
        if DIR_NAME[b.direction] == s[1] then feeder = at(bx, by)
        elseif not wrong then wrong = { bx, by, DIR_NAME[b.direction], s[1] } end
      end
    end
  end
  if feeder then
    out.start = "fed by the belt at " .. feeder
  elseif wrong then
    out.start = "nothing feeds the first belt " .. at(x1, y1) .. ". The belt at " .. at(wrong[1], wrong[2]) ..
      " faces " .. wrong[3] .. "; to turn the corner it must face " .. wrong[4] .. " (rotate it)"
  else
    out.start = "nothing feeds the first belt yet"
  end
  return out
end

-- Every join a placement makes with a belt that is not part of it, from the game's own belt connections
-- (side-loads, a belt run on from behind, an old belt's dead end pointing into a new line anywhere along it):
-- {at = the new belt, other = the belt it joins, way = "into" (feeds it) or "from" (is fed by it), side}
local function merges_of(list)
  local own = {}
  for _, e in pairs(list) do if e.valid then own[tile_key(e.position.x, e.position.y)] = true end end
  local out = {}
  for _, e in pairs(list) do
    if e.valid and BELT_TYPES[e.type] then
      local ins, outs = belt_neighbours(e)
      for _, pair in ipairs({ { "into", outs }, { "from", ins } }) do
        local way, group = pair[1], pair[2]
        for _, o in pairs(group) do
          if not own[tile_key(o.position.x, o.position.y)] then
            local tgt, src = o, e                         -- the belt items go onto, and the one they come from
            if way == "from" then tgt, src = e, o end
            local side = tgt.type == "transport-belt" and tgt.direction ~= src.direction and
              (try(function() return tgt.belt_shape end) or "straight") == "straight"
            out[#out + 1] = { at = { e.position.x, e.position.y }, other = { o.position.x, o.position.y },
                              way = way, side = side }
          end
        end
      end
    end
  end
  return out
end

-- isolated = true: a join is allowed only with a tile in allow (the rest of the same route, a planned
-- side-load); otherwise the placement is taken back and refused with would_merge
local function merge_guard(a, list)
  local m = merges_of(list)
  if not a.isolated then return m, nil end
  local allow = {}
  for _, t in pairs(a.allow or {}) do allow[tile_key(t[1], t[2])] = true end
  for _, g in ipairs(m) do
    if not allow[tile_key(g.other[1], g.other[2])] then return m, g end
  end
  return m, nil
end

local function would_merge(g)
  local how = g.way == "into" and ("would feed into " .. (g.side and "the side of " or "") .. "the belt at ")
    or "would be fed by the belt at "
  return fail("would_merge", "the belt at " .. at(g.at[1], g.at[2]) .. " " .. how .. at(g.other[1], g.other[2]) ..
    ", which is not part of it: route around it, or list that tile in allow if the join is meant")
end

local function tag_carries(a, list)
  if not a.carries then return end
  storage.belt_carries = storage.belt_carries or {}
  for _, e in pairs(list) do
    if e.valid and BELT_TYPES[e.type] and e.unit_number then
      local set = {}
      for _, k in pairs(a.carries) do set[k] = true end
      storage.belt_carries[e.unit_number] = set
    end
  end
end

function A.place(a)
  local inv = character().get_main_inventory()
  if inv.get_item_count(a.entity) < 1 then return fail("not_in_inventory") end
  local e, err = place_one(a.entity, a.x, a.y, DIR[a.direction], a.belt_type)
  if not e and err == "cannot_place" and a.entity == "offshore-pump" then return fail(err, pump_hint(a)) end
  if not e and err == "cannot_place" and a.entity == "transport-belt" and belt_at(a.x, a.y) then
    return fail(err, "a belt is already there: rotate it instead")
  end
  if not e then return fail(err) end
  local merges = nil
  if BELT_TYPES[e.type] then
    local bad
    merges, bad = merge_guard(a, { e })
    if bad then
      e.destroy()
      return would_merge(bad)
    end
    tag_carries(a, { e })
  end
  inv.remove{ name = a.entity, count = 1 }
  if e.type == "transport-belt" then
    local x, y, d = e.position.x, e.position.y, DIR_NAME[e.direction]
    return ok({ entity = describe(e), links = belt_links(x, y, x, y, d), merges = merges })
  end
  return ok({ entity = describe(e), merges = merges })
end

function A.place_line(a)
  local inv = character().get_main_inventory()
  local x1, y1 = math.floor(a.x1) + 0.5, math.floor(a.y1) + 0.5
  local x2, y2 = math.floor(a.x2) + 0.5, math.floor(a.y2) + 0.5
  local dir, sx, sy
  if x2 > x1 then dir, sx, sy = "east", 1, 0
  elseif x2 < x1 then dir, sx, sy = "west", -1, 0
  elseif y2 > y1 then dir, sx, sy = "south", 0, 1
  else dir, sx, sy = "north", 0, -1 end
  local n = math.max(math.abs(x2 - x1), math.abs(y2 - y1)) + 1
  local s, c = surface(), character()
  -- Like dragging belts in the game: a belt (or pipe) already on the line is kept, and a belt is turned
  -- to face along the line, so starting a line on the corner tile makes the corner.
  local existing, need = {}, 0
  for i = 0, n - 1 do                    -- check every tile before placing any
    local x, y = x1 + sx * i, y1 + sy * i
    local bx0, by0, bx1, by1 = footprint(a.entity, x, y, DIR[dir])
    if dist_to_box(c.position, bx0, by0, bx1, by1) > REACH then return fail("out_of_reach") end
    local same = s.find_entities_filtered{ position = { x, y }, name = a.entity, limit = 1 }[1]
    if same then
      existing[i] = same
    elseif s.can_place_entity{ name = a.entity, position = { x, y }, direction = DIR[dir],
        force = "player", build_check_type = defines.build_check_type.manual } then
      need = need + 1
    else
      return fail("cannot_place", "tile " .. i .. " " .. at(x, y) .. " is blocked")
    end
  end
  if inv.get_item_count(a.entity) < need then return fail("not_in_inventory") end
  local placed, turned = {}, {}
  local all, created, was = {}, {}, {}          -- for the merge guard: every belt of the line, the new ones,
  for i = 0, n - 1 do                           -- and the direction each turned one had
    local x, y = x1 + sx * i, y1 + sy * i
    local e = existing[i]
    if e then
      if e.type == "transport-belt" and e.direction ~= DIR[dir] then
        was[#was + 1] = { e, e.direction }
        e.direction = DIR[dir]
        turned[#turned + 1] = { x, y }
      end
    else
      local err
      e, err = place_one(a.entity, x, y, DIR[dir])
      if not e then                      -- never skip a tile silently
        return fail("cannot_place", "tile " .. i .. " " .. at(x, y) .. " could not be built (" .. tostring(err) ..
          "); " .. #placed .. " tiles before it were placed")
      end
      inv.remove{ name = a.entity, count = 1 }
      created[#created + 1] = e
    end
    all[#all + 1] = e
    placed[#placed + 1] = { e.position.x, e.position.y }
  end
  local result = { placed = placed, direction = dir }
  if BELT_TYPES[(prototypes.entity[a.entity] or {}).type or ""] then
    local m, bad = merge_guard(a, all)
    if bad then                                 -- take the whole line back: nothing half-joined stays
      for _, e in pairs(created) do
        if e.valid then
          e.destroy()
          inv.insert{ name = a.entity, count = 1 }
        end
      end
      for _, w in pairs(was) do if w[1].valid then w[1].direction = w[2] end end
      return would_merge(bad)
    end
    tag_carries(a, all)
    result.merges = m
  end
  if #turned > 0 then result.turned = turned end
  if a.entity == "transport-belt" and #placed > 0 then
    result.links = belt_links(placed[1][1], placed[1][2], placed[#placed][1], placed[#placed][2], dir)
  end
  return ok(result)
end

function A.insert(a)
  local e = entity_at(a.x, a.y)
  if not e then return fail("no_entity") end
  if not in_reach(e) then return fail("out_of_reach") end
  if storage.delivery and e == storage.delivery and a.item == storage.score_item then
    return fail("forbidden_target", "you may not insert the score item into the delivery chest")
  end
  local inv = character().get_main_inventory()
  local have = inv.get_item_count(a.item)
  if have < 1 then return fail("not_in_inventory") end
  local n = e.insert{ name = a.item, count = math.min(have, a.count) }
  if n < 1 then return fail("cannot_insert") end
  inv.remove{ name = a.item, count = n }
  return ok({ inserted = n, entity = describe(e) })
end

function A.extract(a)
  local e = entity_at(a.x, a.y)
  if not e then return fail("no_entity") end
  if not in_reach(e) then return fail("out_of_reach") end
  local cinv = character().get_main_inventory()
  local invs = {}
  if e.type == "container" then
    invs = { e.get_inventory(defines.inventory.chest) }
  else
    local crafter = e.type == "furnace" or e.type == "assembling-machine"
    invs = { e.get_output_inventory(), crafter and e.get_inventory(FURNACE_SOURCE) or nil,
             e.burner and e.get_fuel_inventory() or nil }
  end
  local total, present = 0, 0
  for i = 1, 3 do
    local inv = invs[i]
    if inv and total < a.count then
      local have = inv.get_item_count(a.item)
      present = present + have
      local want = math.min(have, a.count - total)
      if want > 0 then
        local removed = inv.remove{ name = a.item, count = want }
        local got = removed > 0 and cinv.insert{ name = a.item, count = removed } or 0
        if got < removed then inv.insert{ name = a.item, count = removed - got } end
        total = total + got
      end
    end
  end
  if total == 0 then
    if present > 0 then return fail("inventory_full") end
    return fail("nothing_to_extract")
  end
  return ok({ extracted = total, entity = describe(e) })
end

function A.advance(a)
  local n = math.min(a.ticks, a.max_ticks)
  return ok({ ticks = n }, n)
end

-- can the character craft this recipe by hand right now? (real tech tree: it must be unlocked)
local function hand_craftable(r)
  if storage.tech == "real" then
    local fr = game.forces.player.recipes[r.name]
    if not (fr and fr.enabled) then return false, "recipe_locked", "research the technology that unlocks it first" end
  end
  local cats = prototypes.entity["character"].crafting_categories or {}
  if not any_cat(r, cats) then
    return false, "not_hand_craftable", "needs a machine (" .. table.concat(recipe_cats(r), ", ") .. ")"
  end
  for _, ing in pairs(r.ingredients) do
    if ing.type == "fluid" then return false, "not_hand_craftable", "needs fluids: use a machine" end
  end
  for _, prod in pairs(r.products) do
    if prod.type == "fluid" then return false, "not_hand_craftable", "makes a fluid: use a machine" end
  end
  return true
end

function A.craft(a)
  local r = prototypes.recipe[a.recipe]
  if not r then return fail("not_enough_ingredients") end
  if storage.tech == "real" then
    local okc, code, msg = hand_craftable(r)
    if not okc then return fail(code, msg) end
  end
  local inv = character().get_main_inventory()
  for _, ing in pairs(r.ingredients) do
    if inv.get_item_count(ing.name) < ing.amount * a.count then return fail("not_enough_ingredients") end
  end
  local ticks = math.ceil(r.energy * 60 * a.count)
  if ticks > a.max_ticks then return fail("insufficient_ticks", "crafting needs " .. ticks .. " ticks") end
  local st = stats()
  for _, ing in pairs(r.ingredients) do
    inv.remove{ name = ing.name, count = ing.amount * a.count }
    st.on_flow(ing.name, -(ing.amount * a.count))
  end
  local made = {}
  for _, prod in pairs(r.products) do
    local n = (prod.amount or 1) * a.count
    local got = inv.insert{ name = prod.name, count = n }
    if got < n then
      surface().spill_item_stack{ position = character().position, stack = { name = prod.name, count = n - got } }
    end
    st.on_flow(prod.name, n)
    made[prod.name] = n
    storage.hand_made[prod.name] = (storage.hand_made[prod.name] or 0) + n
  end
  return ok({ crafted = made, ticks = ticks }, ticks)
end

function A.mine(a)
  local r = resource_at(a.x, a.y)
  if not r then return fail("no_resource") end
  local tx, ty = math.floor(a.x), math.floor(a.y)
  if dist_to_box(character().position, tx, ty, tx + 1, ty + 1) > RESOURCE_REACH then
    return fail("out_of_reach", "hand mining reach is " .. RESOURCE_REACH .. " tiles")
  end
  local props = r.prototype.mineable_properties
  local product = props.products[1].name
  local n = math.min(a.count, r.amount)
  local ticks = math.ceil(props.mining_time / MINING_SPEED * 60) * n
  if ticks > a.max_ticks then return fail("insufficient_ticks", "mining needs " .. ticks .. " ticks") end
  local inv = character().get_main_inventory()
  if not inv.can_insert{ name = product, count = n } then return fail("inventory_full") end
  local got = inv.insert{ name = product, count = n }
  local rname = r.name                       -- read before a mined-out tile is destroyed
  if r.amount <= got then r.destroy() else r.amount = r.amount - got end
  stats().on_flow(product, got)
  storage.mined[rname] = (storage.mined[rname] or 0) + got
  return ok({ mined = { [product] = got }, ticks = ticks }, ticks)
end

function A.rotate(a)
  local e = entity_at(a.x, a.y)
  if not e then return fail("no_entity") end
  if not in_reach(e) then return fail("out_of_reach") end
  if not e.supports_direction then return fail("cannot_rotate") end
  e.direction = DIR[a.direction]
  if e.type == "transport-belt" then
    local x, y = e.position.x, e.position.y
    return ok({ entity = describe(e), links = belt_links(x, y, x, y, a.direction), merges = merges_of({ e }) })
  end
  return ok({ entity = describe(e) })
end

-- automation level: clear a tree or rock (it drops wood or stone into your inventory)
local function pickup_obstacle(a)
  local best, bd = nil, 1e9
  for _, o in pairs(surface().find_entities_filtered{ position = { a.x, a.y }, radius = 1.5,
      type = { "tree", "simple-entity" } }) do
    local dx, dy = o.position.x - a.x, o.position.y - a.y
    local d = dx * dx + dy * dy
    if d < bd then best, bd = o, d end
  end
  if not best then return fail("no_entity") end
  local bb = best.bounding_box
  if dist_to_box(character().position, bb.left_top.x, bb.left_top.y, bb.right_bottom.x, bb.right_bottom.y) > REACH then
    return fail("out_of_reach")
  end
  local props = best.prototype.mineable_properties
  if not (props and props.minable) then return fail("no_entity") end
  local ticks = math.ceil(props.mining_time / MINING_SPEED * 60)
  if ticks > a.max_ticks then return fail("insufficient_ticks") end
  local inv = character().get_main_inventory()
  local before = {}
  for _, prod in pairs(props.products or {}) do before[prod.name] = inv.get_item_count(prod.name) end
  local kind = best.type
  if not character().mine_entity(best, false) then return fail("inventory_full") end
  local got = {}
  for name, n in pairs(before) do
    local d = inv.get_item_count(name) - n
    if d > 0 then got[name] = d end
  end
  return ok({ picked_up = got, cleared = kind, ticks = ticks }, ticks)
end

function A.pickup(a)
  local e = entity_at(a.x, a.y)
  if not e and storage.clear_obstacles then return pickup_obstacle(a) end
  if not e then return fail("no_entity") end
  if not in_reach(e) then return fail("out_of_reach") end
  if storage.delivery and e == storage.delivery then return fail("forbidden_target") end
  local ticks = math.ceil(e.prototype.mineable_properties.mining_time / MINING_SPEED * 60)
  if ticks > a.max_ticks then return fail("insufficient_ticks") end
  local name = e.name
  if not character().mine_entity(e, false) then return fail("inventory_full") end
  return ok({ picked_up = { [name] = 1 }, ticks = ticks }, ticks)
end

function A.set_recipe(a)
  local e = entity_at(a.x, a.y)
  if not e or e.type ~= "assembling-machine" then return fail("no_entity", "no assembling machine there") end
  if not in_reach(e) then return fail("out_of_reach") end
  local current = e.get_recipe()
  if current and current.name == a.recipe then return ok({ entity = describe(e) }) end
  if storage.tech == "real" then
    local fr = game.forces.player.recipes[a.recipe]
    if not (fr and fr.enabled) then return fail("recipe_locked", "research the technology that unlocks it first") end
  end
  local cats = e.prototype.crafting_categories or {}
  local rp = prototypes.recipe[a.recipe]
  if rp and not any_cat(rp, cats) then
    return fail("no_recipe", "this machine cannot make " .. a.recipe .. " (" .. table.concat(recipe_cats(rp), ", ") .. ")")
  end
  local okc, back = pcall(function() return e.set_recipe(a.recipe) end)
  if not okc then return fail("no_recipe", "that recipe cannot be set here") end
  local inv = character().get_main_inventory()
  for k, v in pairs(back or {}) do                 -- items the machine held go back to you
    local name, count = k, v
    if type(v) == "table" then name, count = v.name, v.count end
    if count and count > 0 then
      local got = inv.insert{ name = name, count = count }
      if got < count then
        surface().spill_item_stack{ position = character().position, stack = { name = name, count = count - got } }
      end
    end
  end
  return ok({ entity = describe(e) })
end

-- ------------------------------------------------------------- research
local function research_state()
  local f = game.forces.player
  local cur = f.current_research
  local queue = {}
  for _, t in pairs(try(function() return f.research_queue end) or {}) do
    queue[#queue + 1] = type(t) == "string" and t or t.name
  end
  return { current = cur and cur.name or nil,
           progress = math.floor((f.research_progress or 0) * 1000 + 0.5) / 1000,
           queue = queue }
end

local function researched_list()
  local out = {}
  for name, t in pairs(game.forces.player.technologies) do
    if t.researched then out[#out + 1] = name end
  end
  table.sort(out)
  return out
end

-- research: set what the labs work on (replace the queue, or append to it). Trigger technologies are not
-- researched in labs: they complete on their own when their trigger happens (craft, mine, build).
function A.research(a)
  if storage.tech ~= "real" then return fail("no_research", "this level has no tech tree") end
  local f = game.forces.player
  local wanted = {}
  for _, name in pairs(a.technologies or {}) do
    local t = f.technologies[name]
    if not t then return fail("unknown_technology", name) end
    if t.researched then return fail("already_researched", name) end
    if not t.enabled then return fail("technology_disabled", name) end
    if t.prototype.research_trigger then
      return fail("trigger_technology", name .. " completes by itself when its trigger happens (" ..
        tostring(t.prototype.research_trigger.type) .. "), not in labs")
    end
    wanted[#wanted + 1] = name
  end
  if a.mode ~= "append" then
    try(function() f.cancel_current_research() end)
    try(function() f.research_queue = {} end)
  end
  local added = {}
  for _, name in ipairs(wanted) do
    local okc, res = pcall(function() return f.add_research(name) end)
    if okc and res ~= false then added[#added + 1] = name end
  end
  if #added == 0 and #wanted > 0 then
    return fail("cannot_research", "none could be queued: research their prerequisites first")
  end
  local out = research_state()
  out.added = added
  return ok(out)
end

-- ------------------------------------------------------- production stats
local function copy(t)
  local o = {}
  for k, v in pairs(t or {}) do o[k] = v end
  return o
end

local function fluid_stats()
  return game.forces.player.get_fluid_production_statistics(surface())
end

local function take_snapshot()
  local st, fst = stats(), fluid_stats()
  local snaps = storage.snaps
  snaps[#snaps + 1] = { tick = game.tick, made = copy(st.input_counts), used = copy(st.output_counts),
                        hand = copy(storage.hand_made), fmade = copy(fst.input_counts), fused = copy(fst.output_counts) }
  if #snaps > SNAPSHOT_KEEP then table.remove(snaps, 1) end
end

-- the newest snapshot at least `minutes` old (or the oldest one), and how many minutes back it really is
local function snapshot_ago(minutes)
  local snaps = storage.snaps
  if #snaps == 0 then return nil, 0 end
  local want = game.tick - minutes * 3600
  local best = snaps[1]
  for i = #snaps, 1, -1 do
    if snaps[i].tick <= want then best = snaps[i]; break end
  end
  return best, (game.tick - best.tick) / 3600
end

-- items (or fluids) made per minute over the last `minutes`; automated = without hand crafting
local function rate(name, minutes, automated, fluid)
  local old, span = snapshot_ago(minutes)
  if not old or span <= 0 then return 0, 0 end
  local now_made, old_made
  if fluid then
    now_made, old_made = fluid_stats().get_input_count(name), old.fmade[name] or 0
  else
    now_made, old_made = stats().get_input_count(name), old.made[name] or 0
    if automated then
      now_made = now_made - (storage.hand_made[name] or 0)
      old_made = old_made - (old.hand[name] or 0)
    end
  end
  return (now_made - old_made) / span, span
end

local function round1(v) return math.floor(v * 10 + 0.5) / 10 end

local function flows(st, hand, snap, span, key_made, key_used, filter)
  local out = {}
  local function row(name)
    local made, used = st.get_input_count(name), st.get_output_count(name)
    local r = { made = made, used = used }
    if hand and hand[name] then r.hand_made = hand[name] end
    if snap and span > 0 then
      r.made_per_min = round1((made - (snap[key_made][name] or 0)) / span)
      r.used_per_min = round1((used - (snap[key_used][name] or 0)) / span)
    end
    return r
  end
  for name, _ in pairs(st.input_counts) do
    if not filter or filter[name] then out[name] = row(name) end
  end
  for name, _ in pairs(st.output_counts) do
    if not out[name] and (not filter or filter[name]) then out[name] = row(name) end
  end
  return out
end

-- What the electric networks really carry (the game's electric network panel), in kW: consumption averaged
-- over the last minute and the last 5 seconds, and what the generators delivered. One pole per network.
-- A bot that adds up every machine's maximum draw overbuilds power many times over: inserters and starved
-- machines draw a small part of it (run 20261119-202811: 434 kW drawn, about 15 MW counted).
local function power_stats()
  local seen, nets = {}, {}
  for _, p in pairs(surface().find_entities_filtered{type = "electric-pole", force = "player"}) do
    local id = p.electric_network_id
    if id and not seen[id] then seen[id] = true; nets[#nets + 1] = p end
  end
  local idx = defines.flow_precision_index
  local function sum(st, counts, input, prec)
    local total = 0
    for name, _ in pairs(counts) do
      local v = try(function()
          return st.get_flow_count{name = name, category = input and "input" or "output", precision_index = prec, count = false}
        end)
        or try(function()
          return st.get_flow_count{name = name, input = input, precision_index = prec, count = false}
        end)
      total = total + (v or 0)
    end
    return total * 60 / 1000                              -- joules per tick -> kW
  end
  local out = { networks = 0, consumption_kw = 0, consumption_5s_kw = 0, production_kw = 0 }
  for _, p in pairs(nets) do
    local st = try(function() return p.electric_network_statistics end)
    if st then
      local cin, cout, input = st.input_counts, st.output_counts, true
      if cin["steam-engine"] or cin["solar-panel"] or cin["steam-turbine"] then   -- generators are the output side
        cin, cout, input = cout, cin, false
      end
      out.networks = out.networks + 1
      out.consumption_kw = out.consumption_kw + sum(st, cin, input, idx.one_minute)
      out.consumption_5s_kw = out.consumption_5s_kw + sum(st, cin, input, idx.five_seconds)
      out.production_kw = out.production_kw + sum(st, cout, not input, idx.one_minute)
    end
  end
  for k, v in pairs(out) do if k ~= "networks" then out[k] = math.floor(v * 10 + 0.5) / 10 end end
  return out
end

-- stats: the production panel. Totals and per-minute rates over the last `minutes` for every item and
-- fluid, hand-crafted counts, research, milestones, and the electric networks' load. Read-only.
function A.stats(a)
  local filter = nil
  if a.items then filter = {}; for _, n in pairs(a.items) do filter[n] = true end end
  local snap, span = snapshot_ago(a.minutes or 5)
  local out = { tick = game.tick, window_minutes = math.floor(span * 100 + 0.5) / 100,
                items = flows(stats(), storage.hand_made, snap, span, "made", "used", filter),
                fluids = flows(fluid_stats(), nil, snap, span, "fmade", "fused", filter),
                milestones = storage.reached }
  if storage.tech == "real" then
    out.research = research_state()
    out.researched = researched_list()
  end
  out.rockets_launched = try(function() return game.forces.player.rockets_launched end) or 0
  out.power = try(power_stats)
  return ok(out)
end

-- ---------------------------------------------------------------- scan
-- scan: every resource patch (and the nearest water) within `radius` of the character, clustered from
-- the generated map, like a player reading the map view. Read-only.
function A.scan(a)
  local s, c = surface(), character()
  local p = c.position
  local radius = math.min(a.radius or 128, storage.scan_radius or 0)
  if radius < 1 then return fail("no_scan", "this level has no scan") end
  local area = { { p.x - radius, p.y - radius }, { p.x + radius, p.y + radius } }
  local filter = { area = area, type = "resource" }
  if a.resources then filter.name = a.resources end
  local by = {}
  for _, r in pairs(s.find_entities_filtered(filter)) do
    local x, y = math.floor(r.position.x), math.floor(r.position.y)
    by[x .. "," .. y] = { r.name, x, y, r.amount }
  end
  local seen, patches = {}, {}
  for key, t in pairs(by) do
    if not seen[key] then
      local name = t[1]
      local stack, head = { key }, 1
      seen[key] = true
      local pt = { tiles = 0, amount = 0, x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9, sx = 0, sy = 0, nd = 1e18 }
      while head <= #stack do
        local cur = by[stack[head]]
        head = head + 1
        local x, y = cur[2], cur[3]
        pt.tiles = pt.tiles + 1
        pt.amount = pt.amount + cur[4]
        pt.sx, pt.sy = pt.sx + x, pt.sy + y
        if x < pt.x0 then pt.x0 = x end
        if y < pt.y0 then pt.y0 = y end
        if x > pt.x1 then pt.x1 = x end
        if y > pt.y1 then pt.y1 = y end
        local dx, dy = x + 0.5 - p.x, y + 0.5 - p.y
        local d = dx * dx + dy * dy
        if d < pt.nd then pt.nd, pt.nearest = d, { x = x + 0.5, y = y + 0.5 } end
        for ox = -2, 2 do                       -- gaps of a tile or two still belong to the same patch
          for oy = -2, 2 do
            local k = (x + ox) .. "," .. (y + oy)
            local nb = by[k]
            if nb and not seen[k] and nb[1] == name then
              seen[k] = true
              stack[#stack + 1] = k
            end
          end
        end
      end
      if pt.tiles >= (a.min_tiles or 4) then
        patches[#patches + 1] = { name = name, tiles = pt.tiles, amount = pt.amount,
          box = { pt.x0, pt.y0, pt.x1 + 1, pt.y1 + 1 },
          centre = { x = round1(pt.sx / pt.tiles) + 0.5, y = round1(pt.sy / pt.tiles) + 0.5 },
          nearest = pt.nearest, distance = round1(math.sqrt(pt.nd)) }
      end
    end
  end
  table.sort(patches, function(u, v) return u.distance < v.distance end)
  local water = nil
  local wt = s.find_tiles_filtered{ area = area, name = { "water", "deepwater", "water-green", "deepwater-green" } }
  if #wt > 0 then
    local best, bd = nil, 1e18
    for _, t in pairs(wt) do
      local dx, dy = t.position.x + 0.5 - p.x, t.position.y + 0.5 - p.y
      local d = dx * dx + dy * dy
      if d < bd then bd, best = d, t.position end
    end
    water = { tiles = #wt, nearest = { x = best.x + 0.5, y = best.y + 0.5 }, distance = round1(math.sqrt(bd)) }
  end
  return ok({ tick = game.tick, centre = { x = p.x, y = p.y }, radius = radius, patches = patches, water = water })
end

-- ---------------------------------------------------------------- batch
-- batch: several instant actions in one call, applied in order on the same tick. The rate limit charges
-- batch_op_gap ticks per op, so a batch saves round trips but never allows more ops per second than the
-- level does. Hand jobs (mine, craft, pickup) and walking are not batchable.
local BATCHABLE = { place = true, place_line = true, insert = true, extract = true, rotate = true,
                    set_recipe = true }

function A.batch(a)
  local results, run = {}, 0
  for i, op in ipairs(a.ops or {}) do
    local r
    if not BATCHABLE[op.type] then
      r = fail("not_batchable", op.type)
    else
      op.max_ticks = a.max_ticks
      local okc, res = pcall(A[op.type], op)
      r = okc and res or fail("op_error", "this op could not run")
      run = i
    end
    results[i] = { ok = r.ok, code = r.code, message = r.message, data = r.data }
    if not r.ok and a.stop_on_error ~= false then break end
  end
  local okn = 0
  for _, r in pairs(results) do if r.ok then okn = okn + 1 end end
  return ok({ results = results, ops_run = run, ops_ok = okn })
end

-- ---------------------------------------------------------- milestones
local function working_count(name, need)
  local n = 0
  for _, e in pairs(surface().find_entities_filtered{ name = name, force = "player" }) do
    if status_name(e) == "working" then
      n = n + 1
      if n >= need then return n end
    end
  end
  return n
end

local function milestone_met(m)
  if m.tech then
    local t = game.forces.player.technologies[m.tech]
    return t ~= nil and t.researched
  end
  if m.rockets then
    return (try(function() return game.forces.player.rockets_launched end) or 0) >= m.rockets
  end
  if m.working then return working_count(m.working, m.count or 1) >= (m.count or 1) end
  if m.item and m.per_minute then
    for _, it in ipairs({ m.item, table.unpack(m.also or {}) }) do     -- also: every one at that rate
      local r, span = rate(it, m.window or 5, m.automated, m.fluid)
      if not (span >= (m.window or 5) * 0.95 and r >= m.per_minute) then return false end
    end
    return true
  end
  if m.item and m.produced then
    local made = (m.fluid and fluid_stats() or stats()).get_input_count(m.item)
    if m.automated and not m.fluid then made = made - (storage.hand_made[m.item] or 0) end
    return made >= m.produced
  end
  return false
end

local function check_milestones()
  for _, m in ipairs(storage.milestones or {}) do
    if not storage.reached[m.id] and milestone_met(m) then storage.reached[m.id] = game.tick end
  end
end

-- trigger technologies (Factorio 2.x): craft an item, mine an entity, build an entity, make a fluid.
-- This scenario's verbs act through scripts, so the game's own trigger events never fire; we check here.
local function nm(v) if type(v) == "table" then return v.name end return v end

local function trigger_met(trig)
  if trig.type == "craft-item" then
    return stats().get_input_count(nm(trig.item)) >= (trig.count or 1)
  elseif trig.type == "mine-entity" then
    local list = trig.entities or { trig.entity }      -- 2.1: a list of entities, 2.0: one
    for _, v in pairs(list) do
      local ent = nm(v)
      if (storage.mined[ent] or 0) > 0 then return true end
      local proto = prototypes.entity[ent]
      local prod = proto and proto.mineable_properties and proto.mineable_properties.products
      if prod and prod[1] then
        local st = prod[1].type == "fluid" and fluid_stats() or stats()
        if st.get_input_count(prod[1].name) > 0 then return true end
      end
    end
    return false
  elseif trig.type == "build-entity" then
    return surface().count_entities_filtered{ name = nm(trig.entity), force = "player", limit = 1 } > 0
  elseif trig.type == "craft-fluid" then
    return fluid_stats().get_input_count(nm(trig.fluid)) >= (trig.amount or 1)
  end
  return false
end

local function check_triggers()
  local f = game.forces.player
  local left = {}
  for _, name in ipairs(storage.trigger_techs or {}) do
    local t = f.technologies[name]
    if t and not t.researched then
      local ready = true
      for _, pre in pairs(t.prerequisites) do
        if not pre.researched then ready = false; break end
      end
      if ready and trigger_met(t.prototype.research_trigger) then
        t.researched = true
      else
        left[#left + 1] = name
      end
    end
  end
  storage.trigger_techs = left
end

-- ---------------------------------------------------------------- catalog
-- The game's own data (organizer tool): every item, fluid, recipe, technology and buildable entity of the
-- loaded mods, with the numbers a planner needs. Written to script-output/catalog.json.
local function keys(dict)
  local out = {}
  for k, _ in pairs(dict or {}) do out[#out + 1] = k end
  table.sort(out)
  return out
end

local function catalog()
  local f = game.forces.player
  local out = { base_version = script.active_mods["base"], items = {}, fluids = {}, recipes = {},
                technologies = {}, entities = {}, resources = {},
                character = { crafting_categories = keys(prototypes.entity["character"].crafting_categories) } }
  for name, it in pairs(prototypes.item) do
    if not try(function() return it.hidden end) then
      out.items[name] = { type = it.type, stack_size = it.stack_size,
        subgroup = try(function() return it.subgroup.name end),
        fuel_value = (it.fuel_value or 0) > 0 and it.fuel_value or nil,
        fuel_category = try(function() return it.fuel_category end),
        place_result = try(function() return it.place_result and it.place_result.name end) }
    end
  end
  for name, fl in pairs(prototypes.fluid) do
    if not try(function() return fl.hidden end) then
      out.fluids[name] = { default_temperature = fl.default_temperature,
        fuel_value = (fl.fuel_value or 0) > 0 and fl.fuel_value or nil }
    end
  end
  for name, r in pairs(prototypes.recipe) do
    if not try(function() return r.hidden end) then
      local ings, prods = {}, {}
      for _, i in pairs(r.ingredients) do ings[#ings + 1] = { type = i.type, name = i.name, amount = i.amount } end
      for _, pr in pairs(r.products) do
        prods[#prods + 1] = { type = pr.type, name = pr.name, amount = pr.amount, amount_min = pr.amount_min,
                              amount_max = pr.amount_max, probability = pr.probability }
      end
      local cats = recipe_cats(r)
      out.recipes[name] = { category = cats[1], categories = cats, energy = r.energy, ingredients = ings, products = prods,
        main_product = try(function() return r.main_product and r.main_product.name end),
        enabled_at_start = (f.recipes[name] and f.recipes[name].enabled) and true or false }
    end
  end
  for name, t in pairs(prototypes.technology) do
    if not try(function() return t.hidden end) then
      local unlocks = {}
      for _, e in pairs(t.effects or {}) do
        if e.type == "unlock-recipe" then unlocks[#unlocks + 1] = e.recipe end
      end
      local ings = {}
      for _, i in pairs(t.research_unit_ingredients or {}) do ings[#ings + 1] = { name = i.name, amount = i.amount } end
      local trig = t.research_trigger
      local trigger = nil
      if trig then
        local ents = nil
        if trig.entities then ents = {}; for _, v in pairs(trig.entities) do ents[#ents + 1] = nm(v) end end
        trigger = { type = trig.type, item = nm(trig.item), entity = nm(trig.entity), entities = ents,
                    fluid = nm(trig.fluid), count = trig.count, amount = trig.amount }
      end
      out.technologies[name] = { prerequisites = keys(t.prerequisites), unlocks = unlocks,
        unit_count = try(function() return t.research_unit_count end),
        unit_count_formula = try(function() return t.research_unit_count_formula end),
        unit_ingredients = ings, unit_time = try(function() return t.research_unit_energy / 60 end),
        trigger = trigger, max_level = try(function() return t.max_level end),
        researched_at_start = (f.technologies[name] and f.technologies[name].researched) and true or false }
    end
  end
  local placeable = {}
  for _, it in pairs(prototypes.item) do
    local pr = try(function() return it.place_result end)
    if pr then placeable[pr.name] = true end
  end
  -- pipe connections of every fluid box: where (per direction: north, east, south, west) and which way it faces
  -- when the entity faces north (0, 4, 8, 12); a bot routes pipes to them (2.1 has no runtime fluidbox API)
  local function fluid_ports(e)
    local boxes = {}
    local okp, fbs = pcall(function() return e.fluidbox_prototypes end)
    if not okp or not fbs then return nil end
    for _, fb in pairs(fbs) do
      local cons = {}
      local okc, pcs = pcall(function() return fb.pipe_connections end)
      for _, c in pairs(okc and pcs or {}) do
        local pos = {}
        for i, p in pairs(c.positions or {}) do pos[i] = { p.x, p.y } end
        cons[#cons + 1] = { flow = c.flow_direction, type = c.connection_type, direction = c.direction,
                            positions = pos, max_underground_distance = c.max_underground_distance }
      end
      boxes[#boxes + 1] = { index = fb.index, production = fb.production_type,
                            filter = try(function() return fb.filter.name end), connections = cons }
    end
    if #boxes == 0 then return nil end
    return boxes
  end
  for name, _ in pairs(placeable) do
    local e = prototypes.entity[name]
    out.entities[name] = { type = e.type, width = e.tile_width, height = e.tile_height,
      crafting_categories = e.crafting_categories and keys(e.crafting_categories) or nil,
      crafting_speed = try(function() return e.get_crafting_speed() end) or try(function() return e.crafting_speed end),
      mining_speed = try(function() return e.mining_speed end),
      resource_categories = e.resource_categories and keys(e.resource_categories) or nil,
      mining_radius = try(function() return e.mining_drill_radius end),
      energy_usage = try(function() return e.energy_usage end),
      max_energy_usage = try(function() return e.get_max_energy_usage() end),
      max_energy_production = try(function() return e.get_max_energy_production() end),
      electric = try(function() return e.electric_energy_source_prototype ~= nil end),
      burner = try(function() return e.burner_prototype ~= nil end),
      supply_area = try(function() return e.get_supply_area_distance() end),
      wire_reach = try(function() return e.get_max_wire_distance() end),
      belt_speed = try(function() return e.belt_speed end),
      max_underground_distance = try(function() return e.max_underground_distance end),
      lab_inputs = try(function() return e.lab_inputs end),
      researching_speed = try(function() return e.get_researching_speed() end) or try(function() return e.researching_speed end),
      pumping_speed = try(function() return e.get_pumping_speed() end) or try(function() return e.pumping_speed end),
      fluidboxes = try(function() return #e.fluidbox_prototypes end),
      fluid_ports = fluid_ports(e),
      drop_vector = try(function() return { e.vector_to_place_result[1], e.vector_to_place_result[2] } end),
      inserter_pickup = try(function() return { e.inserter_pickup_position[1], e.inserter_pickup_position[2] } end),
      inserter_drop = try(function() return { e.inserter_drop_position[1], e.inserter_drop_position[2] } end),
      rocket_parts_required = try(function() return e.rocket_parts_required end),
      module_slots = try(function() return e.module_inventory_size end),
      rotatable = try(function() return e.supports_direction end),
      mining_time = try(function() return e.mineable_properties.mining_time end) }
  end
  for name, e in pairs(prototypes.entity) do
    if e.type == "resource" then
      local mp = e.mineable_properties
      local prods = {}
      for _, pr in pairs(mp.products or {}) do prods[#prods + 1] = { type = pr.type, name = pr.name, amount = pr.amount } end
      out.resources[name] = { category = try(function() return e.resource_category end), mining_time = mp.mining_time,
        products = prods, required_fluid = mp.required_fluid, fluid_amount = mp.fluid_amount,
        infinite = try(function() return e.infinite_resource end) and true or false }
    end
  end
  helpers.write_file("catalog.json", helpers.table_to_json(out), false)
  return { written = "catalog.json" }
end

-- -------------------------------------------------------------- interface
local function wrap(fn)
  return function(json)
    local args = json and helpers.json_to_table(json) or {}
    local okc, result = pcall(fn, args)
    if not okc then
      return helpers.table_to_json({ lua_error = tostring(result) })
    end
    return helpers.table_to_json(result)
  end
end

local send_to_bot, send_all_to_bot       -- spectators, defined at the bottom
local cam_tick                           -- the recording camera, defined at the bottom

remote.add_interface("arena", {
  ping = wrap(function() return { pong = true, tick = game.tick, paused = game.tick_paused } end),
  setup = wrap(setup),
  tick = wrap(function() return { tick = game.tick, ticks_to_run = game.ticks_to_run } end),
  act = wrap(function(a)
    if not (character() and character().valid) then return fail("no_character") end
    local fn = A[a.type]
    if not fn then return fail("unknown_action") end
    if not storage.realtime then
      local result = fn(a)
      if result.ok then result.target_tick = run(result.ticks or 0) end
      result.tick = game.tick
      return result
    end
    -- realtime
    local now = game.tick
    if not storage.clock_start then return fail("clock_not_started") end
    if storage.time_up then
      local r = fail("time_up", "the build time is over"); r.tick = now; return r
    end
    if not READ_ONLY[a.type] then
      local next_ok = (storage.last_act or -1e9) + storage.min_gap
      if now < next_ok then
        local r = fail("too_fast", "next action allowed at tick " .. next_ok); r.tick = now; return r
      end
      if HAND_JOBS[a.type] and now < (storage.hands_until or 0) then
        local r = fail("busy", "your hands are busy until tick " .. storage.hands_until); r.tick = now; return r
      end
      storage.last_act = now
    end
    a.max_ticks = 1e9                     -- game time is the clock itself, not a per-action budget
    local result = fn(a)
    if result.ok and HAND_JOBS[a.type] and (result.ticks or 0) > 0 then
      storage.hands_until = now + result.ticks
      result.data.hands_free_tick = storage.hands_until
    end
    if a.type == "batch" and result.ok then      -- every op costs batch_op_gap ticks of the rate limit
      local n = math.max(1, result.data.ops_run or 1)
      storage.last_act = now + math.max(0, n * (storage.batch_op_gap or storage.min_gap) - storage.min_gap)
      result.data.next_action_tick = storage.last_act + storage.min_gap
    end
    result.ticks = 0
    result.tick = now
    return result
  end),
  start_clock = wrap(function(a)
    storage.clock_start = game.tick
    storage.end_tick = game.tick + a.ticks
    storage.time_up = false
    if a.run then
      game.speed = storage.clock_speed or 1
      game.tick_paused = false
    end
    return { tick = game.tick, end_tick = storage.end_tick }
  end),
  stop_clock = wrap(function()
    game.tick_paused = true
    game.speed = storage.fast_speed or 1
    storage.walk = nil
    return { tick = game.tick, time_up = storage.time_up and true or false }
  end),
  run_ticks = wrap(function(a) return { target_tick = run(a.ticks) } end),
  spectate = wrap(function() return { on_arena = send_all_to_bot(), connected = #game.connected_players } end),
  stats = wrap(function(a)
    local st = stats()
    local out = {}
    for _, name in pairs(a.items or {}) do
      out[name] = { st.get_input_count(name), st.get_output_count(name) }
    end
    return { tick = game.tick, items = out }
  end),
  milestones = wrap(function()
    return { tick = game.tick, reached = storage.reached or {} }
  end),
  catalog = wrap(catalog),
  score = wrap(function(cfg)
    local value
    if cfg.type == "production" then
      value = stats().get_input_count(cfg.item) - (storage.baseline or 0)
    else
      value = (storage.delivery and storage.delivery.valid) and storage.delivery.get_item_count(cfg.item) or 0
    end
    local in_entities = 0
    for _, e in pairs(surface().find_entities_filtered{ force = "player" }) do
      if e.type ~= "character" then
        local okc, n = pcall(function() return e.get_item_count(cfg.item) end)
        if okc and n then in_entities = in_entities + n end
      end
    end
    return { score = value,
             produced = stats().get_input_count(cfg.item) - (storage.baseline or 0),
             in_character_inventory = character().get_item_count(cfg.item),
             in_entities = in_entities,
             coal_burned = stats().get_output_count("coal") }
  end),
})

-- ------------------------------------------------------------- spectators
script.on_init(function()
  game.tick_paused = true
end)

-- realtime: walk the character, and pause the world the moment the build time is up
-- every 10 s: production snapshot (rates), milestones; every second: trigger technologies;
-- every 10 s: chart and generate the map around the character so the world grows as the AI explores
-- a finished rocket goes up on its own: the AI has no silo window to press "Launch" in, and no verb for it
-- (the old silo's "auto launch" checkbox, always on)
local function launch_ready_rockets()
  local ready = defines.rocket_silo_status and defines.rocket_silo_status.rocket_ready
  if not ready then return end
  for _, silo in pairs(surface().find_entities_filtered{ type = "rocket-silo", force = "player" }) do
    if try(function() return silo.rocket_silo_status end) == ready then
      try(function() return silo.launch_rocket() end)
    end
  end
end

script.on_nth_tick(SNAPSHOT_EVERY, function()
  if not storage.setup_done then return end
  take_snapshot()
  launch_ready_rockets()
  check_milestones()
  local r = storage.chart_radius or 0
  local c = storage.character
  if r > 0 and c and c.valid then
    local p = c.position
    game.forces.player.chart(surface(), { { p.x - r, p.y - r }, { p.x + r, p.y + r } })
    surface().request_to_generate_chunks(p, math.ceil(r / 32))
  end
end)

script.on_nth_tick(60, function()
  if storage.setup_done and storage.tech == "real" and #(storage.trigger_techs or {}) > 0 then check_triggers() end
end)

script.on_event(defines.events.on_tick, function(event)
  local w = storage.walk
  if w then
    local c = storage.character
    if not (c and c.valid) then
      storage.walk = nil
    elseif not w.pts then                               -- a walk saved by an older version of this script
      storage.walk = nil
    elseif event.tick >= w.t1 then
      local last = w.pts[#w.pts]
      c.teleport({ last[1], last[2] })
      storage.walk = nil
    else
      local togo = (event.tick - w.t0) * RUN_SPEED            -- distance walked so far along the path
      for i = 2, #w.pts do
        local a, b = w.pts[i - 1], w.pts[i]
        local seg = math.sqrt((b[1] - a[1]) ^ 2 + (b[2] - a[2]) ^ 2)
        if togo <= seg or i == #w.pts then
          local f = seg > 0 and math.min(1, togo / seg) or 1
          c.teleport({ a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f })
          break
        end
        togo = togo - seg
      end
    end
  end
  if storage.end_tick and not storage.time_up and event.tick >= storage.end_tick then
    storage.time_up = true
    game.tick_paused = true
    game.speed = storage.fast_speed or 1
  end
  if storage.cams and next(storage.cams) then cam_tick(event.tick) end
end)

-- spectators watch the run's own surface ("arena"), never the default nauvis world
send_to_bot = function(player)
  if not (player and player.valid) then return end
  if player.controller_type ~= defines.controllers.spectator then
    local body = player.character
    player.set_controller{ type = defines.controllers.spectator }
    if body and body.valid then body.destroy() end
  end
  local s = surface()
  if not s then return end
  local c = storage.character
  local p = (c and c.valid) and c.position or { 0, 0 }
  player.teleport(p, s)
end

send_all_to_bot = function()
  local n = 0
  for _, player in pairs(game.connected_players) do
    send_to_bot(player)
    if player.surface.name == SURFACE then n = n + 1 end
  end
  return n
end

local auto_cam   -- set below, next to /cam

-- Factorio's own panels (surface list, minimap, side menu, bars, alerts) sit under the stream overlay and show
-- through it, so spectators watch a clean view. /gui on brings them back for that player.
local GUI_PANELS = { "show_surface_list", "show_minimap", "show_side_menu", "show_map_view_options",
  "show_shortcut_bar", "show_quickbar", "show_tool_bar", "show_controller_gui", "show_alert_gui",
  "show_research_info", "show_crafting_queue", "show_hotkey_suggestions" }

local function set_gui(pl, visible)
  local v = try(function() return pl.game_view_settings end)
  if not v then return end
  for _, k in ipairs(GUI_PANELS) do try(function() v[k] = visible end) end   -- a field this version lacks is skipped
  try(function() pl.game_view_settings = v end)
end

-- Alt view (the game's "show details": recipes on machines, belt and inserter arrows) is on for every spectator
-- who joins, so the stream shows what each machine makes from the first frame, after a fresh start or a restart
-- alike. Display only: it changes what the spectator sees, nothing the bot can read or do. Alt still toggles it.
local function set_alt_view(pl, on)
  local v = try(function() return pl.game_view_settings end)
  if not v then return end
  try(function() v.show_entity_info = on end)
  try(function() pl.game_view_settings = v end)
end

script.on_event(defines.events.on_player_joined_game, function(event)
  local player = game.get_player(event.player_index)
  send_to_bot(player)
  if player and auto_cam then try(function() auto_cam(player) end) end
  if player then try(function() set_gui(player, false) end) end
  if player then try(function() set_alt_view(player, true) end) end
  if player then player.print("Factorio AI Arena: you are a spectator, following the AI (/cam off to fly freely, /bot to jump back, /gui on for the game's panels).") end
end)

try(function()   -- guarded: a name the game already uses would stop the whole scenario from loading
  commands.add_command("gui", "Show or hide the game's own panels (surface list, minimap, bars): /gui on|off",
    function(cmd)
      local pl = game.get_player(cmd.player_index)
      if not pl then return end
      local on = (cmd.parameter or ""):match("^%s*on") ~= nil
      set_gui(pl, on)
      pl.print(on and "game panels shown (/gui off hides them)" or "game panels hidden (/gui on shows them)")
    end)
end)

commands.add_command("bot", "Jump to the AI's character.", function(cmd)
  send_to_bot(game.get_player(cmd.player_index))
end)

-- ------------------------------------------------------------ spectator camera
-- For recording a run: the camera follows the AI's character loosely. It aims at a running average of where
-- the character has been (its back-and-forth trips cancel out), stays put while that target is inside a dead
-- zone, eases toward it when outside, and follows the character itself on long walks so it never leaves the
-- frame. Optionally the game saves frames from the camera (a timelapse, rendered by the spectator's client
-- into its script-output folder: no screen recorder needed). Spectators only; nothing here touches the run.
local CAM_DEFAULTS = { zoom = 0.5, dead = 12, ease = 0.03, avg = 0.006, shots = nil, w = 1920, h = 1080, day = true }

cam_tick = function(tick)
  local c = storage.character
  if not (c and c.valid) then return end
  local p = c.position
  local a = storage.cam_avg
  if not a then a = { x = p.x, y = p.y }; storage.cam_avg = a end
  a.x = a.x + (p.x - a.x) * CAM_DEFAULTS.avg            -- where the character has been lately (~3 s memory)
  a.y = a.y + (p.y - a.y) * CAM_DEFAULTS.avg
  for index, cam in pairs(storage.cams) do
    local pl = game.get_player(index)
    if not (pl and pl.valid and pl.connected) then
      storage.cams[index] = nil
    elseif pl.controller_type == defines.controllers.spectator then
      local cx, cy = cam.x or p.x, cam.y or p.y
      local tx, ty = a.x, a.y
      local fx, fy = p.x - cx, p.y - cy
      if math.sqrt(fx * fx + fy * fy) > cam.dead * 2.5 then tx, ty = p.x, p.y end   -- a long walk: go with it
      local dx, dy = tx - cx, ty - cy
      local d = math.sqrt(dx * dx + dy * dy)
      if d > cam.dead then
        local step = (d - cam.dead) * cam.ease
        cx, cy = cx + dx / d * step, cy + dy / d * step
      end
      cam.x, cam.y = cx, cy
      if pl.surface.name ~= SURFACE then pl.teleport({ cx, cy }, surface()) else pl.teleport({ cx, cy }) end
      if cam.shots and tick % cam.shots == 0 then
        cam.frame = (cam.frame or 0) + 1
        try(function()
          game.take_screenshot{ player = pl, by_player = pl, surface = surface(), position = { cx, cy },
            resolution = { cam.w, cam.h }, zoom = cam.zoom, show_gui = false, show_entity_info = true,
            daytime = cam.day and 0 or nil, quality = 90, allow_in_replay = true,
            path = string.format("arena-cam/%s/%07d.jpg", cam.folder, cam.frame) }
        end)
      end
    end
  end
end

local function cam_help(pl, cam)
  pl.print(string.format("camera %s: zoom %.2f, dead zone %d tiles, ease %.3f, frames %s, day %s. " ..
    "/cam on|off, /cam zoom 0.4, /cam dead 12, /cam ease 0.03, /cam frames 120|off (every N game ticks), " ..
    "/cam size 1920 1080, /cam day on|off (frames only)",
    cam and "on" or "off", (cam or CAM_DEFAULTS).zoom, (cam or CAM_DEFAULTS).dead, (cam or CAM_DEFAULTS).ease,
    cam and cam.shots and (cam.shots .. " ticks -> script-output/arena-cam/" .. cam.folder) or "off",
    tostring((cam or CAM_DEFAULTS).day)))
end

-- A spectator who joins, or rejoins after the server restarted, gets the follow camera right away: the stream PC's
-- client reconnects on its own (stream/watchdog.py), so nobody has to type /cam on. /cam off still works.
auto_cam = function(pl)
  storage.cams = storage.cams or {}
  if storage.cams[pl.index] then return end
  local cam = {}
  for k, v in pairs(CAM_DEFAULTS) do cam[k] = v end
  cam.folder = string.format("%s-t%d", pl.name, game.tick)
  storage.cams[pl.index] = cam
  try(function() pl.zoom = cam.zoom end)
end

commands.add_command("cam", "Recording camera that follows the AI loosely: /cam on|off|zoom|dead|ease|frames|size|day",
  function(cmd)
    local pl = game.get_player(cmd.player_index)
    if not pl then return end
    storage.cams = storage.cams or {}
    local args = {}
    for word in string.gmatch(cmd.parameter or "", "%S+") do args[#args + 1] = word end
    local what, cam = args[1], storage.cams[pl.index]
    if what == "off" then
      storage.cams[pl.index] = nil
      return cam_help(pl, nil)
    end
    if not cam then
      send_to_bot(pl)
      cam = {}
      for k, v in pairs(CAM_DEFAULTS) do cam[k] = v end
      cam.folder = string.format("%s-t%d", pl.name, game.tick)
      storage.cams[pl.index] = cam
    end
    local n = tonumber(args[2])
    if what == "zoom" and n then cam.zoom = math.max(0.1, math.min(4, n))
    elseif what == "dead" and n then cam.dead = math.max(0, n)
    elseif what == "ease" and n then cam.ease = math.max(0.001, math.min(1, n))
    elseif what == "frames" then cam.shots = (args[2] ~= "off" and n and math.max(1, math.floor(n))) or nil
    elseif what == "size" and n and tonumber(args[3]) then cam.w, cam.h = math.floor(n), math.floor(tonumber(args[3]))
    elseif what == "day" then cam.day = args[2] ~= "off"
    end
    try(function() pl.zoom = cam.zoom end)
    cam_help(pl, cam)
  end)
