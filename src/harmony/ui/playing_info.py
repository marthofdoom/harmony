"""Text for "Playing from <source>" and the stream-quality line (gi-free, unit-testable).

Shared by the Now Playing bar and the Now Playing page so both say the same thing:

    Playing from Library  ·  Dark Side of the Moon      (album = a small link)
    Library · harmony-server · FLAC · 24-bit / 96 kHz   [Hi-Res]
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# Contexts that open a top-level page rather than an entity page.
_PAGE_FOR_KIND = {"songs": "library", "genre": "library", "library": "library",
                  "playlist": "playlists", "search": "search"}


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
    album/playlist it was picked from (that's :func:`context_text`)."""
    name = source_name(source)
    return f"Playing from {name}" if name else ""


def context_text(context: dict[str, Any] | None) -> str:
    """The album/playlist/artist/list the queue came from, as plain link text."""
    return ((context or {}).get("title") or "").strip()


def context_action(context: dict[str, Any] | None, navigator: Any,
                   open_page: Callable[[str], None] | None) -> Callable[[], None] | None:
    """What clicking "Playing from …" does, or None when there is nowhere to go."""
    if not context or not context.get("title"):
        return None
    kind, service, cid = context.get("kind"), context.get("service"), context.get("id")
    if navigator is not None and service and cid:
        if kind == "album":
            return lambda: navigator.go_to_album(service, cid)
        if kind == "artist":
            return lambda: navigator.go_to_artist(service, cid)
        if kind == "track":
            return lambda: navigator.go_to_track(service, cid)
    page = _PAGE_FOR_KIND.get(str(kind))
    if page and open_page is not None:
        return lambda: open_page(page)
    return None


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
