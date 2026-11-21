"""Multistream relay: OBS streams once to this PC; ffmpeg copies it to YouTube (and Twitch) and makes a vertical cut.

    OBS -> rtmp://127.0.0.1:1935/live (key "obs") -> ffmpeg -> YouTube (copy) + YouTube vertical (NVENC) + Twitch (copy)

Keys live in secrets/stream.json (git-ignored): "youtube_key", "youtube_vertical_key" (a second YouTube live
stream, for the Shorts feed), "twitch_key". Any can be left out.
Optional: "twitch_server", "youtube_server", or "extra_outputs": ["rtmp://...", ...] for more platforms.

    uv run python stream/relay.py            # keep it running; start it before you press Start Streaming
    uv run python stream/relay.py --check    # print where it would send (keys masked) and exit

Each platform is independent (onfail=ignore + fifo): if YouTube drops, Twitch keeps going, and ffmpeg is restarted
when OBS reconnects. Uploads add up: YouTube 8000 kbps + vertical 6000 kbps is about 14 Mbps.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

SECRETS = Path(__file__).resolve().parent.parent / "secrets" / "stream.json"
LISTEN = "rtmp://127.0.0.1:1935/live/obs"
SERVERS = {"twitch": "rtmps://live.twitch.tv:443/app", "youtube": "rtmp://a.rtmp.youtube.com/live2"}


def outputs(cfg: dict) -> list[str]:
    out = []
    for name, default in SERVERS.items():
        key = (cfg.get(f"{name}_key") or "").strip()
        if "://" in key or "/" in key:          # the server address pasted instead of the stream key
            sys.exit(f"{name}_key in secrets/stream.json looks like a server address ({key[:40]}). It must be the "
                     "stream key itself: in YouTube Studio, the copy button next to 'Key: Default stream key'.")
        if key:
            out.append(f"{cfg.get(f'{name}_server', default).rstrip('/')}/{key}")
    return out + list(cfg.get("extra_outputs") or [])


def mask(url: str) -> str:
    head, _, key = url.rpartition("/")
    return f"{head}/{key[:4]}..." if len(key) > 6 and "://" in head else url


def vertical_output(cfg: dict) -> str | None:
    key = (cfg.get("youtube_vertical_key") or "").strip()
    return f"{cfg.get('youtube_server', SERVERS['youtube']).rstrip('/')}/{key}" if key else None


# 1080x1920 for phones, made from the 1920x1080 stream: the whole frame (overlay included) on top, and below it the
# middle of the game zoomed in, where /cam keeps the bot. Encoded on the GPU, so it costs almost no CPU.
VERTICAL_FILTER = ("[0:v]split=2[a][b];[a]scale=1080:608[top];"
                   "[b]crop=708:860:606:100,scale=1080:1312[bot];[top][bot]vstack=inputs=2,format=yuv420p[v]")


def tee(outs: list[str]) -> str:
    return "|".join(f"[f=flv:onfail=ignore:flvflags=no_duration_filesize]{u}" for u in outs)


def command(ffmpeg: str, outs: list[str], vertical: str | None = None, encoder: str = "h264_nvenc") -> list[str]:
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-listen", "1", "-i", LISTEN]
    if outs:
        cmd += ["-map", "0:v", "-map", "0:a?", "-c", "copy", "-f", "tee", "-use_fifo", "1", tee(outs)]
    if vertical:
        cmd += ["-filter_complex", VERTICAL_FILTER, "-map", "[v]", "-map", "0:a?",
                "-c:v", encoder, "-b:v", "6000k", "-maxrate", "6000k", "-bufsize", "12000k", "-g", "120",
                "-c:a", "copy", "-f", "tee", "-use_fifo", "1", tee([vertical])]
    return cmd


def main() -> None:
    p = argparse.ArgumentParser(description="Copy one OBS stream to several platforms.")
    p.add_argument("--check", action="store_true", help="print the outputs (keys masked) and exit")
    p.add_argument("--out", action="append", help="send here instead of the configured platforms (for tests)")
    p.add_argument("--once", action="store_true", help="stop after one OBS session instead of waiting for the next")
    p.add_argument("--vertical-out", help="send the vertical stream here instead (for tests)")
    p.add_argument("--encoder", default="h264_nvenc", help="encoder for the vertical stream (h264_nvenc)")
    a = p.parse_args()

    try:
        cfg = json.loads(SECRETS.read_text())
    except (OSError, ValueError):
        cfg = {}
    outs = a.out if a.out is not None or a.vertical_out else outputs(cfg)
    outs = outs or []
    vertical = a.vertical_out or (None if a.out else vertical_output(cfg))
    if not outs and not vertical:
        sys.exit(f"no outputs: put youtube_key (and youtube_vertical_key, twitch_key) in {SECRETS}")
    print("relay: OBS -> " + LISTEN)
    for u in outs:
        print("  ->", mask(u))
    if vertical:
        print("  -> (vertical 1080x1920)", mask(vertical))
    if a.check:
        return
    ffmpeg = shutil.which("ffmpeg") or sys.exit("ffmpeg is not on PATH")
    while True:
        print(time.strftime("%H:%M:%S"), "waiting for OBS (Start Streaming)...", flush=True)
        t0 = time.time()
        code = subprocess.call(command(ffmpeg, outs, vertical, a.encoder))
        print(time.strftime("%H:%M:%S"), f"OBS session ended after {int(time.time() - t0)} s (ffmpeg exit {code})",
              flush=True)
        if a.once:
            return
        time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
