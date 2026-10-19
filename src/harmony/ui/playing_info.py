"""Text for "Playing from <source>" and the stream-quality line (gi-free, unit-testable).

Shared by the Now Playing bar and the Now Playing page so both say the same thing:

    Playing from Library  ·  Dark Side of the Moon      (album = a small link)
    Library · harmony-server · FLAC · 24-bit / 96 kHz   [Hi-Res]
"""

from __future__ import annotations

from typing import Any


def source_name(source: dict[str, Any] | None) -> str:
    """The stream's source as a person names it: ``Library``, ``YouTube Music``,
    ``Qobuz`` (empty when not known yet)."""
    if not source:
        return ""
    if source.get("service") == "local":
        return "Library"
    return str(source.get("label") or source.get("service") or "")


def playing_from_text(source: dict[str, Any] | None) -> str:
    """``"Playing from Library"`` -- the SOURCE of the audio, never the
    album/playlist it was picked from."""
    name = source_name(source)
    return f"Playing from {name}" if name else ""


def is_hires(quality: dict[str, Any] | None) -> bool:
    return bool(quality) and quality.get("tier") == "hires"


def tier_pill(quality: dict[str, Any] | None) -> str | None:
    """The pill label for a stream: ``Hi-Res``, ``Lossless`` or None (lossy/unknown)."""
    tier = (quality or {}).get("tier")
    return {"hires": "Hi-Res", "lossless": "Lossless"}.get(tier)


def quality_text(source: dict[str, Any] | None, quality: dict[str, Any] | None) -> str:
    """``"Library · harmony-server · FLAC · 24-bit / 96 kHz"`` (no Hi-Res tag —
    that's a pill, see :func:`is_hires`). Empty when neither is known."""
    parts: list[str] = []
    if source:
        parts.append(source_name(source))
        if source.get("service") == "local" and source.get("instance"):
            parts.append(str(source["instance"]))
    label = (quality or {}).get("label") or ""
    if label.endswith(" · Hi-Res"):
        label = label[: -len(" · Hi-Res")]
    if label:
        parts.append(label)
    return " · ".join(p for p in parts if p)
