"""Real-GStreamer regression tests for the pipewiresink teardown hang.

A ``pipewiresink`` that prerolled in PAUSED but never reached PLAYING blocks
``set_state(NULL)`` forever, freezing the GTK main thread on Stop / next track.
That covers every pipeline that never played since its load, not just one
loaded paused: pause pressed while it was still prerolling, a buffering hold
before the first play, and an errored stream (the server dropped the
connection). The fix is ``LocalPlayer._to_null`` plus its ``_started`` flag.

Each scenario runs in a SUBPROCESS with a hard timeout, so a regression fails
the test instead of hanging the suite. Skipped where GStreamer or a working
pipewiresink (needs a PipeWire daemon) is unavailable.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import wave
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parent.parent / "src")
SKIP_CODE = 77
TIMEOUT_S = 25

_SCRIPT = textwrap.dedent(
    """
    import socket, sys, threading, time
    from gi.repository import GLib
    from harmony.ui.local_player import LocalPlayer, _ensure_gst

    Gst = _ensure_gst()
    scenario, wav = sys.argv[1], sys.argv[2]
    uri = "file://" + wav
    errors = []
    lp = LocalPlayer(on_error=errors.append)
    if lp._sink is None:
        sys.exit(77)  # no pipewiresink: the hang cannot be reproduced here
    lp.set_volume(0)

    def spin(sec):
        ctx = GLib.MainContext.default()
        end = time.monotonic() + sec
        while time.monotonic() < end:
            ctx.iteration(False)
            time.sleep(0.005)

    def state():
        return lp._playbin.get_state(0)[1]

    def wait_for(want, sec=4):
        end = time.monotonic() + sec
        while time.monotonic() < end:
            spin(0.02)
            if state() == want:
                return True
        return False

    class Buf:
        def parse_buffering(self):
            return 10
        def parse_buffering_stats(self):
            class S:
                mode = Gst.BufferingMode.STREAM
            return S()

    if scenario == "paused_load":
        lp.load_and_play(uri, paused=True)
        if not wait_for(Gst.State.PAUSED):
            sys.exit(77)
    elif scenario == "pause_before_first_play":
        lp.load_and_play(uri)
        lp.pause()
        if not wait_for(Gst.State.PAUSED):
            sys.exit(77)
    elif scenario == "buffering_hold":
        lp.load_and_play(uri)
        lp._handle_buffering(None, Buf())
        if not wait_for(Gst.State.PAUSED):
            sys.exit(77)
    elif scenario in ("paused_seek", "buffering_seek", "load_start_at"):
        # Seeking a pipeline that never played hangs pipewiresink (FLUSH seek never
        # returns): the target must be held and applied once it really plays.
        def pos():
            return lp.status().position_s
        if scenario == "load_start_at":
            lp.load_and_play(uri, start_s=7)
        elif scenario == "paused_seek":
            lp.load_and_play(uri, paused=True)
            if not wait_for(Gst.State.PAUSED):
                sys.exit(77)
            t = time.monotonic()
            assert lp.seek(6) is True
            assert time.monotonic() - t < 1.0, "seek blocked"
            assert pos() == 6, pos()  # the pending target is what the bar shows
            lp.resume()
        else:
            lp.load_and_play(uri)
            lp._handle_buffering(None, Buf())
            if not wait_for(Gst.State.PAUSED):
                sys.exit(77)
            t = time.monotonic()
            assert lp.seek(6) is True
            assert time.monotonic() - t < 1.0, "seek blocked"
            assert pos() == 6, pos()
            class Full(Buf):
                def parse_buffering(self):
                    return 100
            lp._handle_buffering(None, Full())  # buffering done: resumes playing
        if not wait_for(Gst.State.PLAYING):
            sys.exit(77)
        spin(0.4)
        want = 7 if scenario == "load_start_at" else 6
        assert lp._pending_seek_s is None
        assert pos() is not None and want <= pos() <= want + 3, "seek not applied: %r" % pos()
    elif scenario == "pause_then_next":
        lp.load_and_play(uri)
        lp.pause()
        if not wait_for(Gst.State.PAUSED):
            sys.exit(77)
        lp.load_and_play(uri)  # the next track tears the first one down
        if not wait_for(Gst.State.PLAYING):
            sys.exit(77)
    elif scenario == "dropped_stream":
        data = open(wav, "rb").read()
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        def serve():
            while True:
                c, _ = srv.accept()
                c.recv(65536)
                c.sendall(b"HTTP/1.1 200 OK\\r\\nContent-Type: audio/x-wav\\r\\n"
                          b"Content-Length: %d\\r\\n\\r\\n" % len(data))
                c.sendall(data[:300000])
                time.sleep(1.0)
                c.close()
        threading.Thread(target=serve, daemon=True).start()
        lp.load_and_play("http://127.0.0.1:%d/x.wav" % srv.getsockname()[1], paused=True)
        end = time.monotonic() + 8
        while not errors and time.monotonic() < end:
            spin(0.05)
        if not errors:
            sys.exit(77)  # the server never dropped it on this setup
    else:
        raise SystemExit("unknown scenario " + scenario)

    spin(0.5)  # let the sink settle: the hang only shows once it is connected
    t = time.monotonic()
    lp.stop()
    took = time.monotonic() - t
    assert took < 1.5, "stop took %.2fs" % took
    assert lp._playbin.get_property("mute") is False
    assert state() == Gst.State.NULL
    # and the player is reusable afterwards
    lp.load_and_play(uri)
    if not wait_for(Gst.State.PLAYING):
        raise SystemExit("did not play after the teardown")
    lp.stop()
    print("OK")
    """
)


@pytest.fixture(scope="module")
def wav_path(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("wav") / "silence.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\0" * 44100 * 4 * 30)
    return str(path)


@pytest.fixture(scope="module")
def script_path(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("scn") / "scenario.py"
    path.write_text(_SCRIPT)
    return str(path)


def _env() -> dict[str, str]:
    """Subprocess env with PYTHONPATH, and the REAL PipeWire runtime dir: the
    suite's host-isolation fixture points XDG_RUNTIME_DIR at an empty temp dir,
    which would make every pipewiresink unreachable (and these tests skip)."""
    env = dict(os.environ, PYTHONPATH=SRC + os.pathsep + os.environ.get("PYTHONPATH", ""))
    real = f"/run/user/{os.getuid()}"
    if os.path.isdir(real):
        env["XDG_RUNTIME_DIR"] = real
    return env


def _gst_available() -> bool:
    probe = (
        "import gi; gi.require_version('Gst','1.0');"
        "from gi.repository import Gst; Gst.init(None);"
        "import sys; sys.exit(0 if Gst.ElementFactory.find('pipewiresink') and "
        "Gst.ElementFactory.find('playbin') else 1)"
    )
    try:
        return subprocess.run([sys.executable, "-c", probe], timeout=20,
                              capture_output=True).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


@pytest.mark.parametrize(
    "scenario",
    ["paused_load", "pause_before_first_play", "buffering_hold", "pause_then_next",
     "dropped_stream", "paused_seek", "buffering_seek", "load_start_at"],
)
def test_teardown_of_a_never_played_pipeline_does_not_hang(
    scenario: str, script_path: str, wav_path: str
) -> None:
    if not _gst_available():
        pytest.skip("GStreamer / pipewiresink unavailable")
    env = _env()
    try:
        proc = subprocess.run(
            [sys.executable, script_path, scenario, wav_path],
            env=env, capture_output=True, text=True, timeout=TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"{scenario}: stop() hung (pipewiresink teardown deadlock)")
    if proc.returncode == SKIP_CODE:
        pytest.skip(f"{scenario}: pipewiresink cannot preroll / scenario not reproducible here")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "OK" in proc.stdout


_LEAK_SCRIPT = textwrap.dedent(
    """
    import os, struct, subprocess, sys, time, wave
    from gi.repository import GLib
    from harmony.ui.local_player import LocalPlayer, _ensure_gst

    Gst = _ensure_gst()
    wav, out = sys.argv[1], sys.argv[2]
    mod = subprocess.check_output(["pactl", "load-module", "module-null-sink",
                                   "sink_name=harmony_leak_test"]).decode().strip()
    rec = None
    try:
        rec = subprocess.Popen(["pw-record", "--target", "harmony_leak_test",
                                "-P", "stream.capture.sink=true", "--rate", "44100",
                                "--channels", "2", "--format", "s16", out])
        time.sleep(1.0)
        lp = LocalPlayer()
        if lp._sink is None:
            sys.exit(77)
        lp._sink.set_property("target-object", "harmony_leak_test")
        lp.set_volume(100)
        playing = []

        class Spy:  # records the set_state(PLAYING) of the safe start (bus messages
            # are flushed when the pipeline goes to NULL, so they can't show it)
            def __init__(self, inner):
                self._inner = inner

            def set_state(self, state):
                if state == Gst.State.PLAYING:
                    playing.append(1)
                return self._inner.set_state(state)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        lp._playbin = Spy(lp._playbin)

        def spin(sec):
            ctx = GLib.MainContext.default()
            end = time.monotonic() + sec
            while time.monotonic() < end:
                ctx.iteration(False)
                time.sleep(0.002)

        lp.load_and_play("file://" + wav, paused=True)
        spin(1.0)
        t = time.monotonic()
        lp.stop()  # the muted start-and-teardown of a never-played pipeline
        took = time.monotonic() - t
        spin(1.0)
        # The sink's already-prerolled buffer is not covered by mute/volume, so
        # the only guard is the start being too short to render it.
        assert took < 0.25, "the muted start dwelt %.3fs" % took
        assert playing, "the safe start did not run, the test proves nothing"
        assert lp._playbin.get_property("volume") == 1.0
        assert lp._playbin.get_property("mute") is False
    finally:
        if rec:
            rec.terminate()
            rec.wait()
        subprocess.call(["pactl", "unload-module", mod])
    w = wave.open(out)
    data = w.readframes(w.getnframes())
    peak = max((abs(v) for v in struct.unpack("<%dh" % (len(data) // 2), data)), default=0)
    assert peak < 200, "audible audio reached the sink during the teardown: peak %d" % peak
    print("OK")
    """
)


def test_no_audio_reaches_the_sink_during_the_muted_teardown(tmp_path) -> None:
    """The muted start that unsticks a never-played pipewiresink must stay
    silent: a loud track loaded paused is stopped while a PipeWire null sink
    records everything the player sends."""
    import shutil

    # Opt-in: it adds (then removes) a null sink on the user's live PipeWire
    # graph, which a routine test run must not do.
    if os.environ.get("HARMONY_AUDIO_TESTS") != "1":
        pytest.skip("set HARMONY_AUDIO_TESTS=1 to run the live-PipeWire leak test")
    if not (shutil.which("pactl") and shutil.which("pw-record")) or not _gst_available():
        pytest.skip("pactl / pw-record / GStreamer pipewiresink unavailable")
    wav = tmp_path / "loud.wav"
    import math
    import struct

    with wave.open(str(wav), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"".join(
            struct.pack("<hh", v, v)
            for v in (int(12000 * math.sin(i * 2 * math.pi * 440 / 44100)) for i in range(44100 * 10))
        ))
    script = tmp_path / "leak.py"
    script.write_text(_LEAK_SCRIPT)
    env = _env()
    try:
        proc = subprocess.run(
            [sys.executable, str(script), str(wav), str(tmp_path / "cap.wav")],
            env=env, capture_output=True, text=True, timeout=TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        subprocess.call("pactl list short modules | grep harmony_leak_test | cut -f1 | "
                        "xargs -r -n1 pactl unload-module", shell=True, env=_env())
        pytest.fail("teardown hung")
    if proc.returncode == SKIP_CODE or "pactl" in proc.stderr and "Failure" in proc.stderr:
        pytest.skip("cannot create a PipeWire null sink here")
    assert proc.returncode == 0, proc.stdout + proc.stderr


class _FakeElement:
    """A GStreamer element stand-in: fixed get_state, records set_state/properties."""

    def __init__(self, Gst, current, pending=None) -> None:
        self.Gst = Gst
        self.current = current
        self.pending = pending or Gst.State.VOID_PENDING
        self.calls: list = []
        self.props = {"volume": 0.7, "mute": False}

    def get_state(self, _timeout=0):
        return (self.Gst.StateChangeReturn.SUCCESS, self.current, self.pending)

    def set_state(self, state):
        self.calls.append(state)

    def get_property(self, name):
        return self.props[name]

    def set_property(self, name, value):
        self.props[name] = value
        self.calls.append((name, value))


def _bare_player(playbin_state, sink_state):
    from harmony.ui.local_player import LocalPlayer, _ensure_gst

    try:
        Gst = _ensure_gst()
    except (ImportError, ValueError):
        pytest.skip("GStreamer unavailable")
    lp = LocalPlayer.__new__(LocalPlayer)
    lp._started = False
    lp._pending_seek_s = None
    lp._playbin = _FakeElement(Gst, playbin_state(Gst))
    lp._sink = _FakeElement(Gst, sink_state(Gst))
    return Gst, lp


def test_teardown_starts_the_sink_when_only_the_sink_is_prerolled_paused() -> None:
    """The bin can read READY while the pipewiresink inside it still sits
    prerolled in PAUSED; that also needs the safe (muted) start before NULL."""
    Gst, lp = _bare_player(lambda G: G.State.READY, lambda G: G.State.PAUSED)
    lp._to_null()
    states = [c for c in lp._playbin.calls if not isinstance(c, tuple)]
    assert states == [Gst.State.PLAYING, Gst.State.NULL]
    # muted + volume 0 during the start, and both restored afterwards
    assert ("volume", 0.0) in lp._playbin.calls and ("mute", True) in lp._playbin.calls
    assert lp._playbin.props == {"volume": 0.7, "mute": False}


def test_teardown_skips_the_safe_start_when_nothing_is_prerolled() -> None:
    Gst, lp = _bare_player(lambda G: G.State.READY, lambda G: G.State.READY)
    lp._to_null()
    assert [c for c in lp._playbin.calls] == [Gst.State.NULL]


def test_seek_on_an_unstarted_pipeline_is_deferred_not_issued() -> None:
    Gst, lp = _bare_player(lambda G: G.State.PAUSED, lambda G: G.State.PAUSED)
    lp._intent = "paused"
    lp._playbin.seek_simple = lambda *a: pytest.fail("seek_simple on a never-started pipeline")
    assert lp.seek(42) is True and lp._pending_seek_s == 42
