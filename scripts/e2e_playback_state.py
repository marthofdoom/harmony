#!/usr/bin/env python
"""Drive the REAL AppState + NowPlayingBar + LocalPlayer (GStreamer) and sample
the transport state while generated FLACs play -- from disk and through a slow,
Range-capable HTTP server (to force preroll delay + buffering).

It records, every ~100 ms, ``pb.state``, the bar's play icon and Stop
sensitivity, and the raw GStreamer (current, pending) state, against the
*intent* of the last user action. A sample is "bad" when the model disagrees
with the intent (e.g. intent=playing but the bar shows paused/stopped, or Stop
is insensitive while audio is playing).

Run (isolated profile under ~/.cache/harmony-e2e, nothing real touched):

    ./.venv/bin/python scripts/e2e_playback_state.py [--poll-ms 1000] [--only file|http]

Exit status 1 if any bad sample was seen.
"""

from __future__ import annotations

import argparse
import http.server
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

HOME = Path(os.environ.get("HARMONY_E2E_HOME", Path.home() / ".cache/harmony-e2e/playback-state"))
for sub, var in (("config", "XDG_CONFIG_HOME"), ("data", "XDG_DATA_HOME"),
                 ("cache", "XDG_CACHE_HOME"), ("state", "XDG_STATE_HOME")):
    (HOME / sub).mkdir(parents=True, exist_ok=True)
    os.environ[var] = str(HOME / sub)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gst", "1.0")
from gi.repository import GLib, GObject, Gst  # noqa: E402

from harmony import config as config_module  # noqa: E402
from harmony.models import Service, StreamSource, Track  # noqa: E402
from harmony.ui.now_playing_bar import NowPlayingBar  # noqa: E402
from harmony.ui.state import LOCAL_HOST, AppState  # noqa: E402

MEDIA = HOME / "media"
SECS = 40


def make_media() -> list[Path]:
    MEDIA.mkdir(exist_ok=True)
    out = []
    for i in range(3):
        p = MEDIA / f"t{i}.flac"
        if not p.exists():
            subprocess.run(
                ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                 f"anoisesrc=d={SECS}:c=pink:r=44100:a=0.2:seed={i + 1}", "-ac", "2",
                 "-sample_fmt", "s16", str(p)], check=True)
        out.append(p)
    return out


class SlowHandler(http.server.BaseHTTPRequestHandler):
    """Range-capable file server throttled to ``rate`` B/s after ``delay`` s."""

    rate = 110_000
    delay = 1.8

    def log_message(self, *_a) -> None:  # quiet
        pass

    def do_GET(self) -> None:  # noqa: N802
        data = (MEDIA / self.path.lstrip("/")).read_bytes()
        start, end = 0, len(data) - 1
        rng = self.headers.get("Range")
        if rng and (m := re.match(r"bytes=(\d+)-(\d*)", rng)):
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else end
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", "audio/flac")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        time.sleep(self.delay)
        pos, chunk = start, 8192
        try:
            while pos <= end:
                self.wfile.write(data[pos:min(pos + chunk, end + 1)])
                pos += chunk
                time.sleep(chunk / self.rate)
        except (BrokenPipeError, ConnectionResetError):
            pass


class FakeProvider:
    def __init__(self, urls: dict[str, str]) -> None:
        self.urls = urls

    def resolve_stream(self, track_id: str, max_quality: bool = False) -> StreamSource:
        return StreamSource(url=self.urls[track_id], mime_type="audio/flac", container="flac")


def build_state(urls: dict[str, str], poll_ms: int) -> AppState:
    config_module.settings_path = lambda: HOME / "settings.json"
    state = AppState.__new__(AppState)
    GObject.Object.__init__(state)
    state.settings = config_module.Settings.load()
    state._device_session = None
    state._init_playback()
    state.providers = {Service.YTMUSIC: FakeProvider(urls)}

    def no_engine():
        raise RuntimeError("no engine in e2e")

    state._engine = no_engine
    state._ensure_poller = lambda: None  # we install our own poller below
    real_get = state._get_local_player

    def silent_player():
        fresh = state._local_player is None
        player = real_get()
        if fresh:
            player.set_volume(0)  # never audible, set before any stream loads
        return player

    state._get_local_player = silent_player
    state._e2e_poll_id = GLib.timeout_add(poll_ms, lambda: (state._poll(), True)[1])
    return state


EXPECT = {"playing": {"playing", "loading"}, "paused": {"paused"}, "stopped": {"stopped"}}


def run_scenario(name: str, urls: dict[str, str], poll_ms: int) -> dict:
    state = build_state(urls, poll_ms)
    bar = NowPlayingBar(state)
    state.connect("playback-changed", lambda *_a: bar._render())
    samples: list[dict] = []
    intent = {"v": "stopped", "label": "start"}
    t0 = time.monotonic()
    loop = GLib.MainLoop()

    def gst_raw() -> str:
        lp = state._local_player
        if lp is None:
            return "-"
        _ok, cur, pend = lp._playbin.get_state(0)
        return f"{cur.value_nick}/{pend.value_nick}"

    def sample() -> bool:
        pb = state.playback
        samples.append({
            "t": time.monotonic() - t0, "intent": intent["v"], "label": intent["label"],
            "state": pb.state, "icon": bar._play.get_icon_name(),
            "stop": bar._stop.get_sensitive(), "gst": gst_raw(), "pos": pb.position_s,
        })
        return True

    def act(label: str, want: str, fn) -> None:
        intent["v"], intent["label"] = want, label
        fn()

    tracks = [Track(id=k, title=f"Track {k}", service=Service.YTMUSIC, artists=["E2E"],
                    duration_s=SECS) for k in urls]
    steps = [
        (0.2, "play_list", "playing", lambda: state.play_list(
            tracks, 0, LOCAL_HOST, context={"kind": "e2e", "title": "e2e"})),
        (7.0, "seek 20s", "playing", lambda: state.playback_seek(20)),
        (11.0, "next", "playing", state.playback_next),
        (16.0, "pause", "paused", state.playback_toggle_pause),
        (19.0, "resume", "playing", state.playback_toggle_pause),
        (24.0, "seek 5s", "playing", lambda: state.playback_seek(5)),
        (28.0, "stop", "stopped", state.playback_stop),
        (30.0, "play (from stopped)", "playing", state.playback_toggle_pause),
        (36.0, "next", "playing", state.playback_next),
    ]
    for at, label, want, fn in steps:
        GLib.timeout_add(int(at * 1000), lambda a=(label, want, fn): (act(*a), False)[1])
    GLib.timeout_add(100, sample)
    GLib.timeout_add(42000, lambda: (loop.quit(), False)[1])
    loop.run()
    GLib.source_remove(state._e2e_poll_id)
    if state._local_player is not None:
        state._local_player.stop()

    def is_bad(s: dict) -> bool:
        if s["state"] not in EXPECT[s["intent"]]:
            return True
        return s["intent"] in ("playing", "paused") and (
            not s["stop"] or (s["intent"] == "playing" and s["icon"] != "media-playback-pause-symbolic"))

    bad = [s for s in samples if is_bad(s)]
    print(f"\n=== {name} (poll {poll_ms} ms): {len(samples)} samples, {len(bad)} BAD ===")
    last = None
    for s in samples:
        key = (s["intent"], s["state"], s["icon"], s["stop"], s["gst"])
        if key != last:
            flag = "BAD " if is_bad(s) else "ok  "
            icon = s["icon"].replace("media-playback-", "").replace("-symbolic", "")
            print(f"{flag}t={s['t']:5.1f} [{s['label']:<20}] intent={s['intent']:<8} "
                  f"pb.state={s['state']:<8} icon={icon:<6} stop={'on ' if s['stop'] else 'OFF'} "
                  f"gst={s['gst']:<18} pos={s['pos']}")
            last = key
    return {"name": name, "samples": len(samples), "bad": len(bad)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--poll-ms", type=int, default=1000)
    ap.add_argument("--only", choices=("file", "http"), default=None)
    ap.add_argument("--throttle", type=int, default=SlowHandler.rate)
    args = ap.parse_args()
    Gst.init(None)
    files = make_media()
    results = []
    if args.only in (None, "file"):
        urls = {f"f{i}": p.as_uri() for i, p in enumerate(files)}
        results.append(run_scenario("local files", urls, args.poll_ms))
    if args.only in (None, "http"):
        SlowHandler.rate = args.throttle
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        urls = {f"h{i}": f"http://127.0.0.1:{port}/{p.name}" for i, p in enumerate(files)}
        results.append(run_scenario("slow HTTP (throttled, Range)", urls, args.poll_ms))
        srv.shutdown()
    print("\nSUMMARY")
    for r in results:
        print(f"  {r['name']}: {r['bad']} bad of {r['samples']} samples (~{r['bad'] * 0.1:.1f}s)")
    return 1 if any(r["bad"] for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
