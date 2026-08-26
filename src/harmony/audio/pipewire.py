"""Enumerate audio sinks/sources via ``pactl`` (PipeWire ships the pactl compat).

Kept to the stable ``pactl -f json`` interface rather than a PipeWire binding so
there's no extra dependency and no sandbox binding to bundle. Never raises: a
missing pactl, a non-zero exit, or unparsable output all degrade to an empty
list, so a caller can always ask "what outputs are there?" safely.
"""

from __future__ import annotations

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
