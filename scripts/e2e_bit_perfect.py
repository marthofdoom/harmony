#!/usr/bin/env python
"""Drive the REAL AppState + LocalPlayer + NowPlayingBar in bit-perfect mode
against a REAL PipeWire daemon, and check the indicator against what PipeWire
is actually doing.

For each generated FLAC (44.1/16, 48/24, 96/24, 192/24) it plays the track on
"This computer" with bit-perfect on and, independently of Harmony's own probe,
reads the graph with ``pw-dump``: the output node's rate must equal the track's
(when the daemon allows it) and the bar's indicator must say so (green) or name
the resample (amber). It then checks the volume lock (bar slider, ``playback_set_
volume``/MPRIS path), and -- with ``--capture`` -- records the sink's monitor and
compares it SAMPLE FOR SAMPLE with the decoded file (bit-exactness, plus a
negative control at 50% volume).

The null sink is a virtual output, which Harmony (rightly) never verifies green; the
script therefore sets ``HARMONY_BITPERFECT_TEST_SINKS`` to its name -- a test-only
switch that is never set by the app -- so it is treated as a real device here.

Needs a PRIVATE PipeWire (so nothing is heard and the real graph is untouched);
the script refuses to run without ``PIPEWIRE_REMOTE`` set. Recipe:

    D=~/.cache/harmony-e2e/pw; mkdir -p $D/run
    sed 's|^\\s*#default.clock.allowed-rates.*|    default.clock.allowed-rates = [ 44100 48000 88200 96000 176400 192000 ]|' \\
        /usr/share/pipewire/pipewire.conf > $D/pipewire.conf     # + a null-audio-sink, see docs
    PIPEWIRE_RUNTIME_DIR=$D/run PIPEWIRE_CORE=pw-test pipewire -c $D/pipewire.conf &
    PIPEWIRE_RUNTIME_DIR=$D/run PIPEWIRE_REMOTE=pw-test XDG_CONFIG_HOME=$D/wp XDG_STATE_HOME=$D/state \\
        wireplumber &        # links the stream to the sink; NEVER run it without the XDG_* above,
                             # or it writes ~/.local/state/wireplumber (and wp/ holds a config that
                             # disables the real-device monitors: see docs/design/local-playback.md)
    PIPEWIRE_RUNTIME_DIR=$D/run PIPEWIRE_REMOTE=pw-test ./.venv/bin/python scripts/e2e_bit_perfect.py --capture

Exit status 1 if any check fails. Isolated profile under ~/.cache/harmony-e2e.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

if not os.environ.get("PIPEWIRE_REMOTE"):
    sys.exit("refusing to run: set PIPEWIRE_REMOTE (+PIPEWIRE_RUNTIME_DIR) to a private PipeWire daemon")

HOME = Path(os.environ.get("HARMONY_E2E_HOME", Path.home() / ".cache/harmony-e2e/bit-perfect"))
for sub, var in (("config", "XDG_CONFIG_HOME"), ("data", "XDG_DATA_HOME"),
                 ("cache", "XDG_CACHE_HOME"), ("state", "XDG_STATE_HOME")):
    (HOME / sub).mkdir(parents=True, exist_ok=True)
    os.environ[var] = str(HOME / sub)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, GObject  # noqa: E402

# Test-only: let Harmony treat this script's null sink as a real device (see the docstring).
os.environ.setdefault("HARMONY_BITPERFECT_TEST_SINKS", os.environ.get("E2E_SINK", "bp-test-sink"))

from harmony import config as config_module  # noqa: E402
from harmony.models import Service, StreamSource, Track  # noqa: E402
from harmony.ui.now_playing_bar import NowPlayingBar  # noqa: E402
from harmony.ui.state import LOCAL_HOST, AppState  # noqa: E402

MEDIA = HOME / "media"
SINK = os.environ.get("E2E_SINK", "bp-test-sink")
# (name, rate, bits, sample_fmt)
FILES = [("cd", 44100, 16, "s16"), ("hi48", 48000, 24, "s32"), ("hi96", 96000, 24, "s32"),
         ("hi192", 192000, 24, "s32")]

fails: list[str] = []


def check(ok: bool, what: str) -> None:
    print(("  ok   " if ok else "  FAIL ") + what, flush=True)
    if not ok:
        fails.append(what)


def make_media() -> dict[str, Path]:
    MEDIA.mkdir(exist_ok=True)
    out = {}
    for name, rate, bits, fmt in FILES:
        p = MEDIA / f"{name}.flac"
        if not p.exists():
            subprocess.run(
                ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                 f"anoisesrc=d=30:c=pink:r={rate}:a=0.3:seed=7", "-ac", "2", "-sample_fmt", fmt,
                 "-bits_per_raw_sample", str(bits), str(p)], check=True)
        out[name] = p
    return out


def graph_rates() -> dict[str, object]:
    """What PipeWire says, read here with pw-dump independently of Harmony's probe."""
    dump = json.loads(subprocess.run(["pw-dump"], capture_output=True, text=True, check=True).stdout)
    info: dict[str, object] = {}
    for o in dump:
        if o.get("type") == "PipeWire:Interface:Node":
            props = o["info"]["props"]
            fmt = (o["info"]["params"].get("Format") or [{}])[0]
            if props.get("node.name") == "harmony-local":
                info["stream_rate"], info["stream_fmt"] = fmt.get("rate"), fmt.get("format")
                info["stream_props"] = {k: v for k, v in props.items()
                                        if k in ("node.dont-remix", "node.lock-rate", "node.latency", "node.rate")}
            elif props.get("node.name") == SINK:
                info["sink_rate"], info["sink_fmt"] = fmt.get("rate"), fmt.get("format")
        elif o.get("type") == "PipeWire:Interface:Metadata" and o["props"].get("metadata.name") == "settings":
            for m in o["metadata"]:
                if m["key"] == "clock.allowed-rates":
                    info["allowed"] = m["value"]
    return info


class FakeProvider:
    def __init__(self, urls: dict[str, str]) -> None:
        self.urls = urls

    def resolve_stream(self, track_id: str, max_quality: bool = False) -> StreamSource:
        return StreamSource(url=self.urls[track_id], mime_type="audio/flac", container="flac")


def build_state(urls: dict[str, str]) -> AppState:
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
    state._ensure_poller = lambda: None
    GLib.timeout_add(500, lambda: (state._poll(), True)[1])
    return state


def spin(seconds: float, until=None) -> bool:
    ctx = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        while ctx.iteration(False):
            pass
        if until is not None and until():
            return True
        time.sleep(0.02)
    return until() if until else True


def record(seconds: float, rate: int, out: Path) -> subprocess.Popen:
    return subprocess.Popen(
        ["pw-record", "--target", SINK, "-P", "stream.capture.sink=true", "--rate", str(rate),
         "--channels", "2", "--format", "s32", "--raw", "-"],
        stdout=out.open("wb"), stderr=subprocess.DEVNULL)


def decode_ref(path: Path) -> bytes:
    """The file decoded by ffmpeg to interleaved s32le (24-bit left-justified)."""
    return subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(path), "-f", "s32le", "-"],
                          capture_output=True, check=True).stdout


def compare(captured: bytes, ref: bytes, rate: int) -> tuple[int, int]:
    """(matched, total) samples of 3 s of the reference found, verbatim, in the capture."""
    from array import array

    n = rate * 2 * 3            # samples (2 channels) in 3 s
    start = rate * 2 * 5 * 4    # byte offset: skip the first 5 s of the reference
    sig = ref[start:start + 64 * 4]
    idx = captured.find(sig)
    while idx != -1 and idx % 4:
        idx = captured.find(sig, idx + 1)
    if idx == -1:
        return 0, n
    seg = captured[idx: idx + n * 4]
    seg_ref = ref[start: start + len(seg)]
    a, b = array("i"), array("i")
    a.frombytes(seg[: len(seg) // 4 * 4])
    b.frombytes(seg_ref[: len(seg) // 4 * 4])
    return sum(x == y for x, y in zip(a, b, strict=False)), len(b)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture", action="store_true", help="also check sample-exactness on the sink monitor")
    ap.add_argument("--screenshot", help="directory to write PNGs of the bar into (needs a display)")
    args = ap.parse_args()

    media = make_media()
    urls = {n: p.as_uri() for n, p in media.items()}
    state = build_state(urls)
    bar = NowPlayingBar(state)
    state.connect("playback-changed", lambda *_a: bar._render())
    tracks = {n: Track(id=n, title=f"{n} test", service=Service.YTMUSIC, artists=["E2E"], duration_s=30)
              for n in urls}
    state.playback.quality = None

    print("PipeWire:", graph_rates().get("allowed"))
    state.settings.bit_perfect = False
    state.set_bit_perfect(True)
    check(state.settings.bit_perfect, "bit-perfect switch persisted on")

    for name, rate, bits, _fmt in FILES:
        print(f"\n== {name}: {rate / 1000:g} kHz / {bits}-bit")
        state.play_list([tracks[name]], 0, LOCAL_HOST, context={"kind": "e2e", "title": "e2e"})
        state.playback.quality = {"codec": "FLAC", "lossless": True, "tier": "lossless", "sample_rate": rate}
        spin(10, lambda: state.playback.state == "playing")
        # let the probe run (it needs the stream linked)
        spin(8, lambda: state.bit_perfect_status().level in ("verified", "warning"))
        spin(0.5)
        bar._render()
        g = graph_rates()
        s = state.bit_perfect_status()
        if s.level not in ("verified", "warning"):
            print(f"   [debug] level={s.level} probed={state._bp_probed} inflight={state._bp_inflight} "
                  f"next-in={state._bp_next - time.monotonic():.1f}s report={state._bp_report} "
                  f"key={state._bp_key} pb={state.playback.state} decoded={state._local_player.decoded_format()}")
        print(f"   pw-dump: stream {g.get('stream_rate')} Hz {g.get('stream_fmt')} -> sink {g.get('sink_rate')} Hz "
              f"{g.get('sink_fmt')}  props={g.get('stream_props')}")
        print(f"   indicator: {s.level} / {s.label!r}")
        print(f"   bar pill: {bar._info.bp_label.get_label()!r} css={[c for c in bar._info.bp.get_css_classes()]}")
        check(g.get("stream_rate") == rate, f"stream opened at the native rate {rate} Hz")
        allowed = str(g.get("allowed") or "")
        permitted = str(rate) in allowed
        if permitted:
            check(g.get("sink_rate") == rate, f"PipeWire's output node switched to {rate} Hz")
            check(s.level == "verified", "indicator is green (verified)")
            check(bar._info.bp.has_css_class("bp-verified"), "bar pill is green")
        else:
            check(g.get("sink_rate") != rate, f"output stays at {g.get('sink_rate')} Hz (daemon doesn't allow {rate})")
            check(s.level == "warning" and "Resampled" in s.label, f"indicator is amber: {s.label}")
            check(bar._info.bp.has_css_class("bp-warning"), "bar pill is amber")
            print("   tooltip:", bar._info.bp.get_tooltip_text().replace("\n", " | "))
        props = g.get("stream_props") or {}
        check(str(props.get("node.dont-remix", "")).lower() == "true", "node.dont-remix set on the stream")
        check(str(props.get("node.lock-rate", "")).lower() == "true", "node.lock-rate set on the stream")
        if args.screenshot:
            save_png(bar, Path(args.screenshot) / f"bar-{name}.png")

    print("\n== volume lock")
    state.playback_set_volume(20)  # the bar slider, MPRIS Volume and media keys all land here
    spin(0.5)
    check(state.playback.volume == 100, "playback_set_volume(20) leaves the model at 100")
    check(abs(state._local_player._playbin.get_property("volume") - 1.0) < 1e-9, "playbin volume stays 1.0")
    check(not bar._volume.get_sensitive(), "bar volume slider is insensitive")
    print("   slider tooltip:", bar._vol_box.get_tooltip_text())

    if args.capture:
        print("\n== sample-exactness (record the sink monitor, compare with the decoded file)")
        for name, rate, _bits, _fmt in FILES:
            if str(rate) not in str(graph_rates().get("allowed")):
                continue
            for label, vol, expect_exact in (("bit-perfect", None, True), ("50% volume (control)", 50, False)):
                if vol is not None:
                    state.set_bit_perfect(False)
                    state.playback_set_volume(vol)
                else:
                    state.set_bit_perfect(True)
                out = HOME / f"cap-{name}.raw"
                state.play_list([tracks[name]], 0, LOCAL_HOST, context={"kind": "e2e", "title": "e2e"})
                spin(10, lambda: state.playback.state == "playing")
                spin(1.0)
                rec = record(12, rate, out)
                spin(11)
                rec.send_signal(signal.SIGINT)
                rec.wait(timeout=5)
                state.playback_stop()
                spin(0.5)
                matched, total = compare(out.read_bytes(), decode_ref(media[name]), rate)
                print(f"   {name} {label}: {matched}/{total} samples identical")
                if expect_exact:
                    check(total > 0 and matched == total, f"{name}: every sample identical to the decoded file")
                else:
                    check(matched < total * 0.5, f"{name}: control at 50% volume is NOT identical")
                state.set_bit_perfect(True)

    state.playback_stop()
    print("\nFAILED:" if fails else "\nall checks passed", *fails, sep="\n  " if fails else "")
    return 1 if fails else 0


def save_png(widget, path: Path) -> None:
    from gi.repository import Gdk, Graphene, Gtk

    path.parent.mkdir(parents=True, exist_ok=True)
    paintable = Gtk.WidgetPaintable.new(widget)
    w, h = max(widget.get_width(), 900), max(widget.get_height(), 80)
    snap = Gtk.Snapshot()
    paintable.snapshot(snap, w, h)
    node = snap.to_node()
    if node is None:
        return
    window = widget.get_root()
    renderer = window.get_renderer() if window is not None else None
    if renderer is None:
        return
    texture = renderer.render_texture(node, Graphene.Rect().init(0, 0, w, h))
    texture.save_to_png(str(path))
    _ = Gdk


if __name__ == "__main__":
    raise SystemExit(main())
