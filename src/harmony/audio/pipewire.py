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
# ROC network receiver: pick up a ROC stream and route it into a sink (DAC)
# --------------------------------------------------------------------------

from ..errors import ProviderError  # noqa: E402

_ROC_SOURCE_NAME = "harmony-roc"


@dataclass(frozen=True)
class RocReceiver:
    """Handle to a live ROC receiver: the roc-source module + its loopback."""

    source_module: int
    loopback_module: int


def _pactl(*args: str) -> str:
    try:
        result = subprocess.run(
            ["pactl", *args], capture_output=True, text=True, timeout=_TIMEOUT_S, check=True
        )
    except FileNotFoundError as exc:
        raise ProviderError("pactl not found — PipeWire/PulseAudio tools are required.") from exc
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


def roc_receiver_up(
    sink: str, *, source_port: int = 10001, repair_port: int = 10002, latency_ms: int = 20
) -> RocReceiver:
    """Receive a ROC network stream and route it into ``sink`` (a DAC).

    Loads ``module-roc-source`` (listening on the given ports) plus a
    ``module-loopback`` feeding it into ``sink`` at minimal latency, and returns
    a handle to tear both down. Raises ``ProviderError`` if pactl or the ROC
    module isn't available. Param names can vary across PipeWire versions; this
    uses the common form.
    """
    source_module = _load_module(
        "module-roc-source",
        "local_ip=0.0.0.0",
        f"local_source_port={source_port}",
        f"local_repair_port={repair_port}",
        f"sess_latency_msec={latency_ms}",
        f"source_name={_ROC_SOURCE_NAME}",
    )
    try:
        loopback_module = _load_module(
            "module-loopback", f"source={_ROC_SOURCE_NAME}", f"sink={sink}", "latency_msec=1"
        )
    except ProviderError:
        _unload(source_module)  # don't leak the source module on partial failure
        raise
    return RocReceiver(source_module=source_module, loopback_module=loopback_module)


def roc_receiver_down(receiver: RocReceiver) -> None:
    """Tear down a ROC receiver (loopback first, then the source)."""
    _unload(receiver.loopback_module)
    _unload(receiver.source_module)
