"""Enumerate audio sinks/sources via ``pactl`` (PipeWire ships the pactl compat).

Kept to the stable ``pactl -f json`` interface rather than a PipeWire binding so
there's no extra dependency and no sandbox binding to bundle. Never raises: a
missing pactl, a non-zero exit, or unparsable output all degrade to an empty
list, so a caller can always ask "what outputs are there?" safely.
"""

from __future__ import annotations

import json
import logging
import subprocess
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
