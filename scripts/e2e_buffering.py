#!/usr/bin/env python
"""Does a mid-track network stall reach the speakers?

Serves a generated 24/96 FLAC from a local Range-capable HTTP server that is
throttled and then *stalls once* mid-file, and plays it through the REAL
``LocalPlayer`` (GStreamer playbin -> pipewiresink), sampling the play head every
100 ms. A "hold" is any stretch where the audio stopped advancing (the player
paused itself to re-buffer). With the progressive-download plan the whole file is
fetched ahead of the play head, so a stall far ahead of it is never heard; with
``--baseline`` (playbin's stock ~2 s / 2 MB buffering) the same stall is.

Run against a PRIVATE PipeWire so nothing is heard (see docs/design/local-playback.md
for the throw-away daemon recipe) and an isolated profile:

    PIPEWIRE_RUNTIME_DIR=... PIPEWIRE_REMOTE=pipewire-test \\
        ./.venv/bin/python scripts/e2e_buffering.py [--baseline] [--stall-s 25]

With ``--capture`` it also records the sink's monitor and counts stretches of
digital silence (the test signal is noise), the ground truth for an audible dropout.
Exit status 1 if the play head was held for more than ``--tolerance`` seconds
(after start-up) or a silent gap was captured. Needs ffmpeg to make the test file.
"""

from __future__ import annotations

import argparse
import http.server
import os
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

if not os.environ.get("PIPEWIRE_REMOTE"):
    sys.exit("refusing to run: set PIPEWIRE_REMOTE (+PIPEWIRE_RUNTIME_DIR) to a private PipeWire daemon "
             "(this plays real audio; never point it at your desktop's PipeWire)")

HOME = Path(os.environ.get("HARMONY_E2E_HOME", Path.home() / ".cache/harmony-e2e/buffering"))
for sub, var in (("config", "XDG_CONFIG_HOME"), ("data", "XDG_DATA_HOME"),
                 ("cache", "XDG_CACHE_HOME"), ("state", "XDG_STATE_HOME")):
    (HOME / sub).mkdir(parents=True, exist_ok=True)
    os.environ[var] = str(HOME / sub)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from gi.repository import GLib  # noqa: E402

from harmony.ui import local_player as lp_mod  # noqa: E402
from harmony.ui.local_player import LocalPlayer, stream_buffer_dir  # noqa: E402


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


REQUESTS: list[str] = []
_T0 = time.monotonic()


def serve(directory: Path) -> tuple[_Server, int]:
    stalled: set[int] = set()

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            path = directory / Path(u.path).name
            size = path.stat().st_size
            rate, stall_at, stall_s = int(q.get("rate", 0)), int(q.get("stall_at", -1)), float(q.get("stall_s", 0))
            start, end, code = 0, size - 1, 200
            rng = self.headers.get("Range")
            if rng:
                a, _, b = rng.replace("bytes=", "").partition("-")
                start, end, code = int(a), (int(b) if b else size - 1), 206
            REQUESTS.append(f"t={time.monotonic() - _T0:6.2f} GET range={rng}")
            self.send_response(code)
            self.send_header("Content-Type", "audio/flac")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            pos = start
            try:
                with path.open("rb") as f:
                    f.seek(start)
                    while pos <= end:
                        n = min(16384, end - pos + 1)
                        if stall_at >= 0 and pos <= stall_at < pos + n and stall_at not in stalled:
                            stalled.add(stall_at)
                            time.sleep(stall_s)
                        self.wfile.write(f.read(n))
                        pos += n
                        if rate:
                            time.sleep(n / rate)
            except (BrokenPipeError, ConnectionResetError):
                pass

    srv = _Server(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def make_flac(path: Path, seconds: int) -> None:
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         f"anoisesrc=color=pink:sample_rate=96000:duration={seconds}:amplitude=0.2", "-ac", "2",
         "-sample_fmt", "s32", "-bits_per_raw_sample", "24", str(path)], check=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", action="store_true", help="playbin's stock buffering (no look-ahead)")
    ap.add_argument("--seconds", type=int, default=90, help="length of the generated track")
    ap.add_argument("--stall-at", type=int, default=12_000_000, help="byte offset of the stall")
    ap.add_argument("--stall-s", type=float, default=25.0)
    ap.add_argument("--rate", type=int, default=3_000_000, help="server throttle, bytes/s")
    ap.add_argument("--play-s", type=float, default=60.0, help="how long to listen")
    ap.add_argument("--window-mb", type=float, help="override the download start window (bytes cap), MiB")
    ap.add_argument("--window-s", type=int, help="override the download start window, seconds")
    ap.add_argument("--flac", help="serve this FLAC instead of generating one (no ffmpeg needed)")
    ap.add_argument("--capture", action="store_true",
                    help="record the sink's monitor and report digital-silence gaps (ground truth for audible dropouts)")
    ap.add_argument("--local", action="store_true", help="play the file from disk (control: no network)")
    ap.add_argument("--trace", action="store_true", help="print every sample around each hold")
    ap.add_argument("--tolerance", type=float, default=1.0, help="allowed total hold, s")
    args = ap.parse_args()

    if args.baseline:  # what shipped before: no download flag, default 2 s / 2 MB
        lp_mod.buffering_plan = lambda *_a, **_k: lp_mod._NO_BUFFERING

    if args.window_mb or args.window_s:
        import dataclasses

        over = {}
        if args.window_mb:
            over["window_bytes"] = int(args.window_mb * 1024 * 1024)
        if args.window_s:
            over["start_window_s"] = args.window_s
        lp_mod._DOWNLOAD_BUFFERING = dataclasses.replace(lp_mod._DOWNLOAD_BUFFERING, **over)

    media = Path(tempfile.mkdtemp(prefix="buf-", dir=HOME))
    if args.flac:
        (media / "t.flac").symlink_to(Path(args.flac).resolve())
    else:
        make_flac(media / "t.flac", args.seconds)
    srv, port = serve(media)
    url = f"http://127.0.0.1:{port}/t.flac?rate={args.rate}&stall_at={args.stall_at}&stall_s={args.stall_s}"

    if args.local:
        url = (media / "t.flac").as_uri()
    lp = LocalPlayer(on_error=lambda m: print("ERROR", m))
    loop = GLib.MainLoop()
    t0 = time.monotonic()
    samples: list[tuple[float, float, bool]] = []
    first_audio: list[float] = []
    temp_seen: list[str] = []

    def tick() -> bool:
        st = lp.status()
        el = time.monotonic() - t0
        pos = float(lp.position_s() or 0) if st.position_s is not None else 0.0
        precise = lp._playbin.query_position(lp_mod.Gst.Format.TIME)
        if precise[0]:
            pos = precise[1] / lp_mod.Gst.SECOND
        samples.append((el, pos, lp._buffering or st.state != "playing"))
        if pos > 0.2 and not first_audio:
            first_audio.append(el)
        found = [p.name for p in stream_buffer_dir().glob("dl-*")]
        if found and not temp_seen:
            temp_seen.append(found[0])
        if el > args.play_s:
            loop.quit()
            return False
        return True

    events: list[str] = []
    bus = lp._playbin.get_bus()
    bus.connect("message::buffering", lambda _b, m: events.append(
        f"t={time.monotonic() - t0:6.2f} buffering {m.parse_buffering()}%"))
    bus.connect("message::state-changed", lambda _b, m: m.src is lp._playbin and events.append(
        f"t={time.monotonic() - t0:6.2f} state {m.parse_state_changed()[1].value_nick}"))
    GLib.timeout_add(100, tick)
    rec = None
    cap_path = media / "capture.raw"
    if args.capture:
        sink = os.environ.get("E2E_SINK", "bp-test-sink")
        rec = subprocess.Popen(["pw-record", "--target", sink, "-P", "stream.capture.sink=true", "--rate", "96000",
                                "--channels", "2", "--format", "s32", "--raw", "-"],
                               stdout=cap_path.open("wb"), stderr=subprocess.DEVNULL)
    lp.load_and_play(url)
    loop.run()
    if rec is not None:
        rec.send_signal(2)
        rec.wait(timeout=5)
    if args.trace and lp._sink is not None:
        print("pipewiresink stats:", lp._sink.get_property("stats").to_string())
        top = subprocess.run(["pw-top", "-b", "-n", "2"], capture_output=True, text=True).stdout
        print("\n".join(line for line in top.splitlines()[-12:] if "harmony-local" in line or line.startswith("S ")))
    lp.stop()
    srv.shutdown()

    # A hold: the play head moved < 0.05 s over a 0.5 s window, after start-up.
    start = first_audio[0] if first_audio else args.play_s
    held = 0.0
    longest = cur = 0.0
    holds: list[float] = []
    for (t_a, p_a, _b1), (t_b, p_b, _b2) in zip(samples, samples[5:], strict=False):
        if t_a < start:
            continue
        if p_b - p_a < 0.05:
            held += (t_b - t_a) / 5
            cur += (t_b - t_a) / 5
            longest = max(longest, cur)
            if not holds or t_a - holds[-1] > 1.0:
                holds.append(t_a)
        else:
            cur = 0.0
    final_pos = samples[-1][1] if samples else 0
    print(f"mode={'baseline (playbin default)' if args.baseline else 'download-ahead'}")
    print(f"time to first audio: {start:.2f}s")
    print(f"play head at end: {final_pos:.1f}s of {args.play_s:.0f}s listened (stall of {args.stall_s:.0f}s at byte {args.stall_at})")
    print(f"audio held (re-buffering): {held:.1f}s total, longest {longest:.1f}s")
    if args.trace:
        print("bus events:", *events, sep="\n  ")
        print("http requests (t is since script start):", *REQUESTS, sep="\n  ")
    if holds and args.trace:
        for h in holds:
            for t, p_, b in samples:
                if h - 0.6 <= t <= h + 1.0:
                    print(f"  t={t:6.2f} pos={p_:7.3f} held={b}")
    if holds:
        print("holds began at t=" + ", ".join(f"{h:.1f}s" for h in holds))
    print(f"temp file in cache dir: {temp_seen[0] if temp_seen else 'none'}; left behind: "
          f"{len(list(stream_buffer_dir().glob('dl-*')))}")
    gaps: list[float] = []
    if args.capture:
        import re

        data = cap_path.read_bytes()
        # digital silence of >= 5 ms (the test signal is noise: it has none) after the first sound
        first = re.search(rb"[^\x00]", data)
        start_at = first.start() if first else len(data)
        min_bytes = int(0.005 * 96000) * 2 * 4
        for m in re.finditer(rb"(?:\x00{4}){%d,}" % (min_bytes // 4), data[start_at:]):
            gaps.append((m.end() - m.start()) / (96000 * 2 * 4))
        print(f"audible-gap check (sink monitor capture, {len(data) / (96000 * 8):.0f}s): "
              f"{len(gaps)} silent gap(s) >= 5 ms" + (f", longest {max(gaps):.2f}s" if gaps else ""))
    return 1 if (held > args.tolerance or gaps) else 0


if __name__ == "__main__":
    raise SystemExit(main())
