# Lights Out: stream runbook

*One AI, no humans: it plays Factorio alone, live, until it launches a rocket.* A "lights-out factory" is a factory
that runs fully automated, with the lights off because nobody is inside.

## Pieces

| Piece | What it does | Port |
|---|---|---|
| `arena monitor` | Overlay (`/overlay`) and full-screen cards (`/screen?mode=...`) for OBS | 8730 |
| `stream/relay.py` | OBS streams once to it; ffmpeg copies the stream to Twitch and YouTube | 1935 |
| `stream/watchdog.py` | Hourly Claude review of the run, phone alerts, fix flow, phone API | 8740 |
| `stream/obs_setup.py` | Builds the OBS scenes over WebSocket; `--switch NAME` changes the scene | - |

Secrets live in `secrets/` (git-ignored): `stream.json` holds the OBS password and stream keys, and `phone_token`
holds the phone app's token.

## Show day

1. Start OBS (its WebSocket server is on) and Factorio (spectating, `/cam on`).
2. Start the bot's run (`arena local-run ...` or Resume on the monitor).
3. `powershell -ExecutionPolicy Bypass -File stream\start.ps1` opens the monitor, relay and watchdog windows.
4. In OBS: scene **Starting Soon**, then **Start Streaming**, then **Live** when ready.

Check the relay before going live: `uv run --no-sync python stream/relay.py --check` shows where it sends (keys masked).

## Scenes

| Scene | When |
|---|---|
| Starting Soon | Before going live (10-minute countdown; edit `t=` in the browser source URL) |
| Live | The game with the overlay |
| Self-Upgrading | Switched to automatically while Claude fixes the bot |
| Fixing Bugs | Manual: something broke on our side |
| Between Runs | Manual, or automatic for 90 s while a resumed game loads |
| BRB / Ending | Manual |

## Watchdog and phone

Every hour Claude reads a review packet of the run (`runs/watch/reviews/`) and answers ok, unsure or stuck.

- **ok:** nothing happens.
- **unsure:** Claude looks again after 20 minutes.
- **stuck, and certain:** you get an alert in the phone app with a problem analysis.

Answer the alert in the app:

- **Let it run:** the alert closes and the bot carries on.
- **Fix it (+ text):** the stream switches to Self-Upgrading, the bot saves and stops, and Claude fixes the code
  (short tests only, never a real game). Its report comes back as a new alert. Answer "Let it run" to resume from
  the save (the stream goes to Between Runs, then Live), or "Fix it" to start another round.

The phone reaches the PC at `http://<this PC's LAN IP>:8740` at home, or over Tailscale (`tailscale up`, then
`http://<this PC's Tailscale IP>:8740`). The first time the watchdog starts, Windows asks whether to allow Python through the
firewall: allow it on private networks.

Useful commands:

```
uv run --no-sync python stream/watchdog.py --token       # the phone app's token
uv run --no-sync python stream/watchdog.py --packet      # what the next review sees
uv run --no-sync --with obsws-python python stream/watchdog.py --check-now   # one review now, no alert
uv run --no-sync --with obsws-python python stream/obs_setup.py --switch "Fixing Bugs"
```

## YouTube (horizontal + vertical)

In YouTube Studio create **two** streams, each with its own stream key. One is the normal 16:9 stream. The other is
for phones: YouTube puts a 9:16 stream in the Shorts feed, where new channels get found. Put both keys in
`secrets/stream.json`:

```json
{ "youtube_key": "xxxx-xxxx-xxxx-xxxx-xxxx", "youtube_vertical_key": "yyyy-yyyy-yyyy-yyyy-yyyy" }
```

The relay sends the main stream as it comes from OBS (8000 kbps) and builds the vertical stream with NVENC
(6000 kbps): the whole frame with the overlay on top, and the middle of the game zoomed in underneath. That's about
14 Mbps of your 29 Mbps upload. Add `"twitch_key"` later and Twitch gets the same stream as YouTube.

## Thumbnail

`stream/thumbnail.html` puts the bot's real text floor plan ("the AI reads this") next to the game ("and builds
this"). Render it to `media/stream-thumbnail.png`:

```
"C:\Program Files\Google\Chrome\Application\chrome.exe" --headless=new --hide-scrollbars --window-size=1280,720 --virtual-time-budget=6000 --screenshot=..\media\stream-thumbnail.png "file:///D:/Projekti/Tesserac/AI_Factorio/freeplay/stream/thumbnail.html"
```

Swap `stream/thumb-bg.jpg` for a screenshot of the current base (`?bg=other.jpg` also works) when the base looks better.

## Stream title and description

- **Title:** `AI Attempts to Beat Factorio | No Human Help, Day 1`
- **Description:** in `stream/description.txt`.
