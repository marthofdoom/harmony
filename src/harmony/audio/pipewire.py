"""Enumerate audio sinks/sources via ``pactl`` (PipeWire ships the pactl compat).

Kept to the stable ``pactl -f json`` interface rather than a PipeWire binding so
there's no extra dependency and no sandbox binding to bundle. Never raises: a
missing pactl, a non-zero exit, or unparsable output all degrade to an empty
list, so a caller can always ask "what outputs are there?" safely.
"""

from __future__ import annotations

import atexit
import json
import logging
import pathlib
import shutil
import subprocess
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

_TIMEOUT_S = 5


@dataclass(frozen=True)
class AudioNode:
    """One audio device: its stable ``name`` (routing id) and human ``description``."""

    name: str
    description: str


def _list(kind: str) -> list[AudioNode]:
    try:
        result = subprocess.run(
            ["pactl", "-f", "json", "list", kind],
            capture_output=True, text=True, timeout=_TIMEOUT_S, check=True,
        )
        data = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        log.debug("pactl list %s failed: %s", kind, exc)
        return []
    nodes: list[AudioNode] = []
    for entry in data if isinstance(data, list) else []:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not name:
            continue
        nodes.append(AudioNode(name=name, description=entry.get("description") or name))
    return nodes


def list_sinks() -> list[AudioNode]:
    """Output devices (DACs, the WiiM's AirPlay sink when discovered, etc.)."""
    return _list("sinks")


def default_sink() -> str | None:
    """The system's current default output sink name (for its ``.monitor``)."""
    try:
        result = subprocess.run(
            ["pactl", "get-default-sink"],
            capture_output=True, text=True, timeout=_TIMEOUT_S, check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("pactl get-default-sink failed: %s", exc)
        return None
    name = result.stdout.strip()
    return name or None


def list_sources() -> list[AudioNode]:
    """Input devices (mics, monitors, and network sources once ROC/RTP is up)."""
    return _list("sources")


# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# RTP network receiver: pick up an RTP/SAP stream and route it into a sink
# --------------------------------------------------------------------------

from ..errors import ProviderError  # noqa: E402


@dataclass(frozen=True)
class RtpReceiver:
    """Handle to a live RTP receiver (a module-rtp-recv instance)."""

    module: int


def _pactl(*args: str) -> str:
    try:
        result = subprocess.run(
            ["pactl", *args], capture_output=True, text=True, timeout=_TIMEOUT_S, check=True
        )
    except FileNotFoundError as exc:
        raise ProviderError("pactl not found -- PipeWire/PulseAudio tools are required.") from exc
    except subprocess.CalledProcessError as exc:
        raise ProviderError(f"pactl {' '.join(args)} failed: {(exc.stderr or '').strip() or exc}") from exc
    except subprocess.SubprocessError as exc:
        raise ProviderError(f"pactl {' '.join(args)} failed: {exc}") from exc
    return result.stdout.strip()


def _load_module(name: str, *params: str) -> int:
    out = _pactl("load-module", name, *params)
    try:
        return int(out)
    except ValueError as exc:
        raise ProviderError(f"{name} did not return a module id (got {out!r})") from exc


def _unload(module_id: int) -> None:
    try:
        _pactl("unload-module", str(module_id))
    except ProviderError as exc:
        log.debug("unload-module %s failed: %s", module_id, exc)


def rtp_receiver_up(sink: str, *, latency_ms: int = 20) -> RtpReceiver:
    """Receive an RTP/SAP network audio stream and play it into ``sink`` (a DAC).

    Loads ``module-rtp-recv``, which picks up a stream announced over SAP (e.g.
    by a ``module-rtp-send`` on the sender) and routes it straight to ``sink``.
    Returns a handle to tear it down. Raises ``ProviderError`` if pactl or the
    module isn't available. Works in the Flatpak sandbox (the module ships in
    the runtime), unlike ROC.
    """
    module = _load_module("module-rtp-recv", f"sink={sink}", f"latency_msec={latency_ms}")
    return RtpReceiver(module=module)


def rtp_receiver_down(receiver: RtpReceiver) -> None:
    """Tear down an RTP receiver."""
    _unload(receiver.module)


# --------------------------------------------------------------------------
# ROC network receiver: run roc-recv (FEC + adaptive latency) into a sink
# --------------------------------------------------------------------------
#
# ROC is the preferred transport: forward error correction and an adaptive
# latency tuner keep it glitch-free at far lower latency than plain RTP over a
# lossy (Wi-Fi) LAN. It isn't a PipeWire module loaded over the socket -- that
# would run in the *host's* PipeWire, which has no ROC module -- so we run the
# bundled ``roc-recv`` binary in-process and let it output to a sink through the
# PulseAudio socket (``pulse://<sink>``). The sender runs ``roc-send``.

# roc-recv's three endpoints: audio (source), FEC repair, and RTCP control.
_ROC_SOURCE_PORT = 10001
_ROC_REPAIR_PORT = 10002
_ROC_CONTROL_PORT = 10003

# Live roc-recv processes, so a receiver never outlives the app: a subprocess
# isn't killed when its parent exits, which would leave a stream playing with no
# in-app way to stop it. We terminate any survivors on interpreter exit.
_live_roc_procs: set[subprocess.Popen] = set()
_atexit_registered = False


def _terminate_roc_procs() -> None:
    for proc in list(_live_roc_procs):
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception:  # noqa: BLE001 - best-effort shutdown cleanup
            pass
        _live_roc_procs.discard(proc)


def _track_roc_proc(proc: subprocess.Popen) -> None:
    global _atexit_registered
    _live_roc_procs.add(proc)
    if not _atexit_registered:
        atexit.register(_terminate_roc_procs)
        _atexit_registered = True


def _roc_log_path() -> pathlib.Path:
    """Where roc-recv's diagnostics go (a real file, never a pipe)."""
    import platformdirs

    directory = pathlib.Path(platformdirs.user_cache_dir("harmony"))
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "roc-recv.log"


@dataclass(frozen=True)
class RocReceiver:
    """Handle to a live ROC receiver (a running ``roc-recv`` process)."""

    process: subprocess.Popen
    log_file: object  # open file object roc-recv writes its log to; closed on teardown
    log_path: str
    source_port: int
    repair_port: int
    control_port: int


def roc_available() -> bool:
    """Whether the ``roc-recv`` binary is on PATH (bundled in the Flatpak)."""
    return shutil.which("roc-recv") is not None


def roc_receiver_up(
    sink: str,
    *,
    target_latency_ms: int = 100,
    source_port: int = _ROC_SOURCE_PORT,
    repair_port: int = _ROC_REPAIR_PORT,
    control_port: int = _ROC_CONTROL_PORT,
) -> RocReceiver:
    """Receive a ROC network audio stream and play it into ``sink``.

    Spawns ``roc-recv`` bound to the ROC source/repair/control endpoints,
    outputting to ``pulse://<sink>`` with the given target latency (ROC's tuner
    holds close to it). Returns a handle to tear it down. Raises
    ``ProviderError`` if roc-recv is missing or dies on startup (e.g. a bad sink
    name or a port already in use).

    Tuning for glitch-free playback over Wi-Fi:
    - a **wide** ``--latency-tolerance``. ROC restarts the whole session (an
      audible break) when the measured latency leaves ``target +/- tolerance``.
      The stream's natural latency sits well below the target while the tuner is
      converging, so a tight tolerance puts the lower bound right on the
      operating point and ROC restarts dozens of times a second. Making the
      tolerance >= the target pushes the lower bound to zero, so it only ever
      restarts on a genuinely pathological latency, not normal convergence.
      (Diagnosed from a -vv loopback: tight tolerance -> ~79 restarts/15s;
      wide -> 1 stable session.)
    - a high-quality resampler (``--resampler-profile=high``), since ROC is
      continuously resampling to track the sender clock -- a cheap resampler
      makes that audible;
    - a **widened watchdog** (``--no-play-timeout``/``--choppy-play-timeout``).
      By default roc tears the session down after only 133 ms of blank audio,
      so a brief Wi-Fi stall triggers a full teardown + FEC re-converge (a
      pop) instead of just resuming. Raising the blank tolerance to 2 s and the
      choppy tolerance to 4 s lets a transient stall ride through -- playback
      resumes seamlessly when packets return, keeping latency low without the
      ticks;
    - roc-recv's log is written to a **file**, never a pipe: an undrained stderr
      pipe fills after ~64 KB and then roc-recv blocks on write, which stalls
      audio -- another cause of periodic digital breaks.
    """
    exe = shutil.which("roc-recv")
    if exe is None:
        raise ProviderError(
            "roc-recv not found -- install roc-toolkit (bundled in the Flatpak) "
            "for FEC / low-latency receive."
        )
    # >= target so the lower restart bound (target - tolerance) is <= 0; floor
    # of 150 ms keeps the upper bound clear of the natural operating latency.
    tolerance_ms = max(target_latency_ms, 150)
    argv = [
        exe,
        "-v",  # info-level diagnostics (packet loss, latency tuning) -> the log file
        "-s", f"rtp+rs8m://0.0.0.0:{source_port}",
        "-r", f"rs8m://0.0.0.0:{repair_port}",
        "-c", f"rtcp://0.0.0.0:{control_port}",
        "-o", f"pulse://{sink}",
        f"--target-latency={target_latency_ms}ms",
        f"--latency-tolerance={tolerance_ms}ms",
        "--no-play-timeout=2s",     # ride through a Wi-Fi stall instead of tearing down
        "--choppy-play-timeout=4s",
        "--resampler-profile=high",
    ]
    log_path = _roc_log_path()
    log_file = open(log_path, "wb")
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log_file
        )
    except OSError as exc:
        log_file.close()
        raise ProviderError(f"couldn't start roc-recv: {exc}") from exc
    # roc-recv opens the output device immediately, so a fast exit means a real
    # error (bad sink, port taken). Give it a moment, then check it's alive.
    time.sleep(0.4)
    if proc.poll() is not None:
        log_file.close()
        err = ""
        try:
            err = log_path.read_text(errors="replace").strip().splitlines()[-1]
        except (OSError, IndexError):
            pass
        raise ProviderError(f"roc-recv exited immediately: {err or f'code {proc.returncode}'}")
    _track_roc_proc(proc)
    return RocReceiver(
        process=proc,
        log_file=log_file,
        log_path=str(log_path),
        source_port=source_port,
        repair_port=repair_port,
        control_port=control_port,
    )


def roc_receiver_down(receiver: RocReceiver) -> None:
    """Stop a ROC receiver (terminate the roc-recv process)."""
    proc = receiver.process
    _live_roc_procs.discard(proc)
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    try:
        receiver.log_file.close()
    except OSError:
        pass


# --------------------------------------------------------------------------
# Senders: broadcast this machine's audio to a peer's receiver
# --------------------------------------------------------------------------
#
# The mirror image of the receivers above, so an instance can *send* its full
# output (the default sink's monitor) to another instance running a receiver.
# ROC (``roc-send``) is preferred; RTP (``module-rtp-send`` with a unicast
# ``destination``) is the fallback. Senders join the same tracked-process /
# atexit machinery as ROC receivers so nothing outlives the app.


def _default_monitor_source(source: str | None) -> str:
    """Resolve the PulseAudio source to capture: an explicit one, or the default
    sink's monitor (i.e. everything currently playing on this machine)."""
    if source:
        return source
    sink = default_sink()
    if not sink:
        raise ProviderError("No default output sink to capture (is PipeWire/Pulse running?).")
    return f"{sink}.monitor"


@dataclass(frozen=True)
class RocSender:
    """Handle to a live ROC sender (a running ``roc-send`` process)."""

    process: subprocess.Popen
    log_file: object
    log_path: str
    host: str


def roc_sender_up(
    host: str,
    *,
    source: str | None = None,
    source_port: int = _ROC_SOURCE_PORT,
    repair_port: int = _ROC_REPAIR_PORT,
    control_port: int = _ROC_CONTROL_PORT,
) -> RocSender:
    """Broadcast this machine's audio to ``host`` via ``roc-send``.

    Captures ``source`` (default: the current default sink's ``.monitor``, i.e.
    the full system output) and sends it to the peer's ROC receiver endpoints.
    Returns a handle to tear it down. Raises ``ProviderError`` if roc-send is
    missing or dies on startup.
    """
    exe = shutil.which("roc-send")
    if exe is None:
        raise ProviderError(
            "roc-send not found -- install roc-toolkit (bundled in the Flatpak) "
            "to send audio to another instance."
        )
    monitor = _default_monitor_source(source)
    argv = [
        exe,
        "-v",
        "-i", f"pulse://{monitor}",
        "-s", f"rtp+rs8m://{host}:{source_port}",
        "-r", f"rs8m://{host}:{repair_port}",
        "-c", f"rtcp://{host}:{control_port}",
    ]
    log_path = _roc_log_path().with_name("roc-send.log")
    log_file = open(log_path, "wb")
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log_file
        )
    except OSError as exc:
        log_file.close()
        raise ProviderError(f"couldn't start roc-send: {exc}") from exc
    time.sleep(0.4)
    if proc.poll() is not None:
        log_file.close()
        err = ""
        try:
            err = log_path.read_text(errors="replace").strip().splitlines()[-1]
        except (OSError, IndexError):
            pass
        raise ProviderError(f"roc-send exited immediately: {err or f'code {proc.returncode}'}")
    _track_roc_proc(proc)
    return RocSender(process=proc, log_file=log_file, log_path=str(log_path), host=host)


def roc_sender_down(sender: RocSender) -> None:
    """Stop a ROC sender (terminate the roc-send process)."""
    proc = sender.process
    _live_roc_procs.discard(proc)
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    try:
        sender.log_file.close()
    except OSError:
        pass


@dataclass(frozen=True)
class RtpSender:
    """Handle to a live RTP sender (a module-rtp-send instance)."""

    module: int


def rtp_sender_up(host: str, *, source: str | None = None) -> RtpSender:
    """Broadcast this machine's audio to ``host`` via ``module-rtp-send`` (unicast).

    The RTP fallback when roc-send isn't present. Loads ``module-rtp-send`` with
    a unicast ``destination`` so it targets one peer rather than SAP multicast.
    """
    monitor = _default_monitor_source(source)
    module = _load_module("module-rtp-send", f"source={monitor}", f"destination={host}")
    return RtpSender(module=module)


def rtp_sender_down(sender: RtpSender) -> None:
    """Tear down an RTP sender."""
    _unload(sender.module)
