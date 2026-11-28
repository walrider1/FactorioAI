"""Organizer tool for `arena vet-seed`: how big the ore patches near spawn are and how far crude oil is.

Read-only map queries, run once on a throwaway vet boot before a seed goes into the stream's pool, never
during a game. The only thing it does to the map is generate chunks out to the crude radius (the map the
seed would make anyway); no game rule or entity changes.

Cost: setup generates chunks 4 out from spawn (9x9 chunks); the survey asks for 8 out (17x17, ~210 more
chunks, a few seconds of map generation inside one RCON command), then two find_entities_filtered calls:
all resources within ORE_RADIUS (clustered into patches in Lua so only patch totals cross RCON) and crude
oil within CRUDE_RADIUS.
"""

from __future__ import annotations

import math

ORE_RADIUS = 150      # patches and ore totals are counted within this many tiles of spawn
CRUDE_RADIUS = 256    # crude oil is looked for out to here
NEAR_RADIUS = 64      # "near spawn", as in the vet's existing resources_within_64
PATCH_GAP = 2         # ore tiles at most this far apart (each axis) belong to the same patch

_LUA = """
local ok, res = pcall(function()
  local s = game.surfaces["arena"]
  local ch = s.find_entities_filtered{ name = "character", limit = 1 }[1]
  local o = ch and ch.position or { x = 0, y = 0 }
  s.request_to_generate_chunks(o, %(chunks)d)
  s.force_generate_chunk_requests()
  local function dist(x, y) local dx, dy = x - o.x, y - o.y; return math.sqrt(dx * dx + dy * dy) end
  local grid = {}
  for _, r in pairs(s.find_entities_filtered{ position = o, radius = %(ore)d, type = "resource" }) do
    if r.name ~= "crude-oil" then
      local g = grid[r.name]
      if not g then g = {}; grid[r.name] = g end
      local x, y = math.floor(r.position.x), math.floor(r.position.y)
      g[x .. "," .. y] = { x, y, r.amount }
    end
  end
  local patches = {}
  for name, g in pairs(grid) do
    for _, t in pairs(g) do
      if not t.seen then
        t.seen = true
        local stack, p = { t }, { name = name, tiles = 0, amount = 0, distance = 1e9 }
        while #stack > 0 do
          local u = table.remove(stack)
          p.tiles = p.tiles + 1
          p.amount = p.amount + u[3]
          local d = dist(u[1] + 0.5, u[2] + 0.5)
          if d < p.distance then p.distance = d; p.x = u[1] + 0.5; p.y = u[2] + 0.5 end
          for dx = -%(gap)d, %(gap)d do
            for dy = -%(gap)d, %(gap)d do
              local v = g[(u[1] + dx) .. "," .. (u[2] + dy)]
              if v and not v.seen then v.seen = true; stack[#stack + 1] = v end
            end
          end
        end
        patches[#patches + 1] = p
      end
    end
  end
  local crude = { wells = 0, amount = 0 }
  for _, r in pairs(s.find_entities_filtered{ position = o, radius = %(crude)d, name = "crude-oil" }) do
    crude.wells = crude.wells + 1
    crude.amount = crude.amount + r.amount
    local d = dist(r.position.x, r.position.y)
    if not crude.distance or d < crude.distance then crude.distance = d; crude.x = r.position.x; crude.y = r.position.y end
  end
  return { origin = { x = o.x, y = o.y }, ore_radius = %(ore)d, crude_radius = %(crude)d,
           patches = patches, crude = crude }
end)
rcon.print(helpers.table_to_json(ok and res or { lua_error = tostring(res) }))
"""


def lua_command(ore_radius: int = ORE_RADIUS, crude_radius: int = CRUDE_RADIUS, gap: int = PATCH_GAP) -> str:
    """The one-line /sc command that surveys the map (numbers only are formatted in)."""
    chunks = math.ceil(max(ore_radius, crude_radius) / 32)
    body = _LUA % {"chunks": int(chunks), "ore": int(ore_radius), "crude": int(crude_radius), "gap": int(gap)}
    return "/sc " + " ".join(line.strip() for line in body.strip().splitlines())


def _patch(p: dict) -> dict:
    return {"distance": round(float(p.get("distance", 1e9)), 1), "tiles": int(p.get("tiles", 0)),
            "amount": int(p.get("amount", 0)), "x": p.get("x"), "y": p.get("y")}


def summarize(raw: dict, near: int = NEAR_RADIUS) -> dict:
    """Turn the Lua survey (a list of patches + crude) into the vet report's "survey" block. Pure."""
    patches = [p for p in (raw.get("patches") or []) if isinstance(p, dict) and p.get("name")]
    nearest: dict[str, dict] = {}
    largest_near: dict[str, dict] = {}
    totals: dict[str, dict] = {}
    for p in patches:
        name = p["name"]
        t = totals.setdefault(name, {"tiles": 0, "amount": 0, "patches": 0})
        t["tiles"] += int(p.get("tiles", 0))
        t["amount"] += int(p.get("amount", 0))
        t["patches"] += 1
        if name not in nearest or p.get("distance", 1e9) < nearest[name]["distance"]:
            nearest[name] = _patch(p)
        if p.get("distance", 1e9) <= near and (name not in largest_near
                                               or p.get("tiles", 0) > largest_near[name]["tiles"]):
            largest_near[name] = _patch(p)
    crude = raw.get("crude") or {}
    cd = crude.get("distance")
    return {"origin": raw.get("origin"), "ore_radius": raw.get("ore_radius"),
            "crude_radius": raw.get("crude_radius"), "near_radius": near,
            "nearest_patch": nearest, f"largest_patch_within_{near}": largest_near, "totals": totals,
            "crude": {"distance": round(float(cd), 1) if cd is not None else None,
                      "wells": int(crude.get("wells", 0)), "amount": int(crude.get("amount", 0)),
                      "x": crude.get("x"), "y": crude.get("y")}}
