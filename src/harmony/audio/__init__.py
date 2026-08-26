"""Local audio: device enumeration and (later) PipeWire routing.

Engine layer — no GTK. The first piece of the local-player / network-source
work: list the output sinks (DACs) and input sources so the UI can offer them,
and so a network source can be routed to a chosen sink.
"""

from __future__ import annotations

from .pipewire import AudioNode, list_sinks, list_sources

__all__ = ["AudioNode", "list_sinks", "list_sources"]
