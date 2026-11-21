"""Build the arena's stream scenes in a running OBS, over OBS's WebSocket server.

    OBS: Tools > WebSocket Server Settings > Enable WebSocket server (port 4455). Copy the password from
    "Show Connect Info", then:

        $env:OBS_WS_PASSWORD = "..."
        uv run --no-project --with obsws-python python stream/obs_setup.py

    Safe to run again: scenes and sources that already exist are left alone, missing ones are added.
    `--switch "Fixing Bugs"` only changes the live scene (handy for a Stream Deck or a hotkey script).

Scenes (each screen is a page served by `arena monitor`, so the monitor must be running for them to show):
    Starting Soon   /screen?mode=starting&t=10
    Live            Factorio game capture + Factorio audio + /overlay on top
    Self-Upgrading  /screen?mode=upgrading
    Fixing Bugs     /screen?mode=bugs
    Between Runs    /screen?mode=between
    BRB             /screen?mode=brb&t=5
    Ending          /screen?mode=ending
"""
from __future__ import annotations

import argparse
import os
import sys

import json
from pathlib import Path

try:
    import obsws_python as obs
except ImportError:
    sys.exit("needs obsws-python: uv run --no-project --with obsws-python python stream/obs_setup.py")

SECRETS = Path(__file__).resolve().parent.parent / "secrets" / "stream.json"   # git-ignored


def secrets() -> dict:
    try:
        return json.loads(SECRETS.read_text())
    except (OSError, ValueError):
        return {}


def connect(host: str | None = None, port: int | None = None, password: str | None = None):
    """An OBS WebSocket client; anything not given comes from secrets/stream.json, then OBS_WS_PASSWORD."""
    s = secrets()
    return obs.ReqClient(host=host or s.get("obs_host", "localhost"), port=port or s.get("obs_port", 4455),
                         password=password or s.get("obs_password") or os.environ.get("OBS_WS_PASSWORD", ""),
                         timeout=5)


RELAY_URL = "rtmp://127.0.0.1:1935/live"   # stream/relay.py listens here and copies the stream to every platform


def configure(c) -> None:
    """Point OBS's stream output at the local relay and set 1080p60, NVENC, 6000 kbps (Simple output mode)."""
    c.set_stream_service_settings("rtmp_custom", {"server": RELAY_URL, "key": "obs", "use_auth": False})
    c.set_video_settings(numerator=60, denominator=1, base_width=1920, base_height=1080,
                         out_width=1920, out_height=1080)
    for cat, name, val in [("Output", "Mode", "Simple"),
                           ("SimpleOutput", "StreamEncoder", "nvenc"), ("SimpleOutput", "VBitrate", "6000"),
                           ("SimpleOutput", "ABitrate", "160"), ("SimpleOutput", "NVPreset2", "p5"),
                           ("SimpleOutput", "RecQuality", "Small"), ("SimpleOutput", "RecEncoder", "nvenc_hevc"),
                           ("SimpleOutput", "RecFormat2", "mkv"),
                           ("Audio", "SampleRate", "48000")]:
        c.set_profile_parameter(cat, name, val)
    print(f"stream output -> {RELAY_URL} (run stream/relay.py), 1920x1080@60, NVENC 6000 kbps, recording HEVC mkv")

SCREENS = [  # scene name, browser source name, path on the monitor
    ("Starting Soon", "Screen: Starting Soon", "/screen?mode=starting&t=10"),
    ("Self-Upgrading", "Screen: Self-Upgrading", "/screen?mode=upgrading"),
    ("Fixing Bugs", "Screen: Fixing Bugs", "/screen?mode=bugs"),
    ("Between Runs", "Screen: Between Runs", "/screen?mode=between"),
    ("BRB", "Screen: BRB", "/screen?mode=brb&t=5"),
    ("Ending", "Screen: Ending", "/screen?mode=ending"),
]
LIVE = "Live"


def browser(url: str) -> dict:
    return {"url": url, "width": 1920, "height": 1080, "fps_custom": True, "fps": 30,
            "shutdown": False, "restart_when_active": False, "reroute_audio": False}


# window match: "title:class:exe"; priority 2 = match the executable, so a changed window title still matches
FACTORIO_WINDOW = "Factorio:SDL_app:factorio.exe"
GAME = ("Factorio (game)", "game_capture",
        {"capture_mode": "window", "window": FACTORIO_WINDOW, "priority": 2, "capture_cursor": False})
GAME_AUDIO = ("Factorio (audio)", "wasapi_process_output_capture", {"window": FACTORIO_WINDOW, "priority": 2})


class Builder:
    def __init__(self, client, base: str):
        self.c, self.base = client, base.rstrip("/")
        self.scenes = {s["sceneName"] for s in client.get_scene_list().scenes}
        self.inputs = {i["inputName"] for i in client.get_input_list().inputs}
        v = client.get_video_settings()
        self.canvas = (v.base_width, v.base_height)

    def scene(self, name: str) -> bool:
        """Create the scene if missing. True if it was created (then it gets its sources)."""
        if name in self.scenes:
            print(f"  = scene {name!r} already there, left as is")
            return False
        self.c.create_scene(name)
        self.scenes.add(name)
        print(f"  + scene {name!r}")
        return True

    def source(self, scene: str, name: str, kind: str, settings: dict, fit: bool = True) -> None:
        """Add a source to the scene: a new input, or the existing input of that name (OBS names are global)."""
        if name in self.inputs:
            item = self.c.create_scene_item(scene, name, True).scene_item_id
        else:
            item = self.c.create_input(scene, name, kind, settings, True).scene_item_id
            self.inputs.add(name)
        if fit and self.canvas != (1920, 1080):   # stretch 1080p sources to a canvas of another size
            w, h = self.canvas
            self.c.set_scene_item_transform(scene, item, {"boundsType": "OBS_BOUNDS_SCALE_INNER",
                                                          "boundsWidth": float(w), "boundsHeight": float(h),
                                                          "positionX": 0.0, "positionY": 0.0})
        print(f"      {name}")

    def build(self) -> None:
        for scene, src, path in SCREENS:
            if self.scene(scene):
                self.source(scene, src, "browser_source", browser(self.base + path))
        if LIVE not in self.scenes and "factorio" in self.scenes:   # the hand-made game scene becomes Live
            self.c.set_scene_name("factorio", LIVE)
            self.scenes = (self.scenes - {"factorio"}) | {LIVE}
            print(f"  ~ scene 'factorio' renamed to {LIVE!r} (kept as is)")
        if self.scene(LIVE):
            self.source(LIVE, *GAME)                                   # bottom
            self.source(LIVE, *GAME_AUDIO, fit=False)
            self.source(LIVE, "Overlay", "browser_source", browser(self.base + "/overlay"))   # on top


def main() -> None:
    p = argparse.ArgumentParser(description="Create the arena's stream scenes in OBS (WebSocket, OBS 28+).")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--password", help="default: secrets/stream.json obs_password, then OBS_WS_PASSWORD")
    p.add_argument("--monitor", default="http://127.0.0.1:8730", help="where `arena monitor` serves the pages")
    p.add_argument("--switch", metavar="SCENE", help="only switch the live (program) scene to SCENE")
    p.add_argument("--configure", action="store_true", help="also set the stream output (relay) and video/encoder")
    a = p.parse_args()

    try:
        c = connect(a.host, a.port, a.password)
    except Exception as exc:  # noqa: BLE001 - refused connection or bad password
        sys.exit(f"could not reach OBS ({exc}).\n"
                 "In OBS: Tools > WebSocket Server Settings > Enable WebSocket server, "
                 "and put the password from Show Connect Info in secrets/stream.json as obs_password.")
    if a.switch:
        c.set_current_program_scene(a.switch)
        print(f"live scene: {a.switch}")
        return
    if a.configure:
        configure(c)
    v = c.get_version()
    print(f"OBS {v.obs_version}, building scenes (pages from {a.monitor}):")
    Builder(c, a.monitor).build()
    print("done.")


if __name__ == "__main__":
    main()
