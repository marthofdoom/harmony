"""A seek past the progressive download must never block the main loop.

With playbin's ``download`` flag a seek beyond what is on disk makes the download
buffer re-request the file from there. If the server then answers that request with
403 (an expired Qobuz URL / peer token) or ignores ``Range`` and sends the whole file
(200), GStreamer's FLUSH seek never returns -- and the state change to NULL that the
resulting error triggers hangs behind it -- freezing the GTK main loop. The player
now reloads such a seek on the queue plan, which fails fast into the ordinary error
-> toast -> next-track path.

Each scenario runs in a SUBPROCESS with a hard timeout (a regression fails the test
instead of hanging the suite): a real playbin (fakesink, no audio stack) fed by a
throttled HTTP server that misbehaves on every request after the first.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("gi")
gi = pytest.importorskip("gi")
try:
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst  # noqa: F401
except (ValueError, ImportError):  # pragma: no cover
    pytest.skip("GStreamer typelib unavailable", allow_module_level=True)

SRC = str(Path(__file__).resolve().parent.parent / "src")

_SCRIPT = textwrap.dedent(
    """
    import http.server, socketserver, sys, threading, time
    from pathlib import Path
    from gi.repository import GLib, Gst
    from harmony.ui import local_player as lpm

    mode, tmp, unsafe = sys.argv[1], Path(sys.argv[2]), len(sys.argv) > 3
    Gst.init(None)
    wav = tmp / "t.flac"
    enc = Gst.parse_launch(
        "audiotestsrc wave=white-noise num-buffers=600 samplesperbuffer=4410 ! "
        "audio/x-raw,rate=44100,channels=2,format=S16LE ! flacenc ! filesink location=%s" % wav)
    enc.set_state(Gst.State.PLAYING)
    msg = enc.get_bus().timed_pop_filtered(60 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    enc.set_state(Gst.State.NULL)
    assert msg is not None and msg.type == Gst.MessageType.EOS, "could not make the test FLAC (no flacenc?)"
    size = wav.stat().st_size                                   # 60 s of noise, ~10 MB
    reqs = []

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *a): pass
        def do_GET(self):
            reqs.append(self.headers.get("Range"))
            if len(reqs) > 1 and mode == "403":
                self.send_response(403); self.send_header("Content-Length", "0"); self.end_headers(); return
            rng = self.headers.get("Range")
            start, code = 0, 200
            if rng and mode != "norange":
                start, code = int(rng.split("=")[1].split("-")[0]), 206
            self.send_response(code)
            self.send_header("Content-Type", "audio/flac")
            if mode != "norange":
                self.send_header("Accept-Ranges", "bytes")
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{size - 1}/{size}")
            self.send_header("Content-Length", str(size - start))
            self.end_headers()
            data = wav.read_bytes()[start:]
            try:
                for i in range(0, len(data), 16384):
                    self.wfile.write(data[i:i + 16384]); time.sleep(16384 / 400_000)   # 400 KB/s
            except OSError:
                pass

    class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    srv = S(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    errors = []
    lp = lpm.LocalPlayer(on_error=errors.append)
    sink = Gst.ElementFactory.make("fakesink", None)
    sink.set_property("sync", True)
    lp._playbin.set_property("audio-sink", sink)
    lp._sink = None
    if unsafe:   # negative control: the old behaviour (seek the download buffer directly)
        lp._downloaded_past = lambda _s: True
    lp.load_and_play(f"http://127.0.0.1:{srv.server_address[1]}/t.flac")

    ctx = GLib.MainContext.default()
    def spin(sec, until=None):
        end = time.monotonic() + sec
        while time.monotonic() < end:
            while ctx.iteration(False): pass
            if until and until(): return True
            time.sleep(0.01)
        return False

    assert spin(10, lambda: lp.status().state == "playing"), "never started"
    spin(3)                                                    # let some of the file download
    t = time.monotonic()
    ok = lp.seek(55)                                           # far past the download
    took = time.monotonic() - t
    print("seek returned", ok, "in %.2fs" % took, flush=True)
    assert took < 2.0, "seek blocked the main loop for %.1fs" % took
    spin(8 if mode != "range" else 3, lambda: errors)
    t = time.monotonic()
    lp.stop()
    stop_took = time.monotonic() - t
    print("stop took %.2fs, errors=%r, requests=%r" % (stop_took, errors, reqs), flush=True)
    assert stop_took < 3.0, "stop blocked for %.1fs" % stop_took
    if mode in ("403", "norange"):
        assert errors, "the failed re-request never reached the error path"
    sys.exit(0)
    """
)


@pytest.mark.parametrize("mode", ["403", "norange", "range"])
def test_seek_past_the_download_never_blocks(mode: str, tmp_path: Path) -> None:
    env = {"PYTHONPATH": SRC, "PATH": "/usr/bin", "HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "c"),
           "XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_DATA_HOME": str(tmp_path / "d")}
    try:
        proc = subprocess.run([sys.executable, "-c", _SCRIPT, mode, str(tmp_path)], capture_output=True,
                              text=True, timeout=45, env=env)
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"the player hung on a seek past the download ({mode}): {exc.stdout}")
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-2000:]
