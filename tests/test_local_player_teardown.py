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
     "dropped_stream"],
)
def test_teardown_of_a_never_played_pipeline_does_not_hang(
    scenario: str, script_path: str, wav_path: str
) -> None:
    if not _gst_available():
        pytest.skip("GStreamer / pipewiresink unavailable")
    env = dict(os.environ, PYTHONPATH=SRC + os.pathsep + os.environ.get("PYTHONPATH", ""))
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
