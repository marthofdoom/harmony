"""Shared plumbing for the entity detail pages (artist/album/track).

Three small concerns live here so the page modules stay focused on layout:

* **Dict → model converters.** The engine hands the desktop plain dicts (the
  same JSON the web client gets — see ``harmony.web.api`` and the API
  contract), but the rest of the UI (``build_track_column_view``,
  ``collection_actions``) speaks in ``harmony.models`` dataclasses. These
  rebuild a ``Track``/``Album`` from an engine dict so the detail pages reuse
  the exact track list and context-menu machinery Search already has.
* **A shared artwork loader.** The fetch + decode runs off the main loop and
  the ``Gdk.Texture`` is built back on it (a texture created on a worker thread
  segfaults GDK — the same rule the Now Playing bar follows). Cached per URL.
* **The ``Navigator``.** A thin handle on the window's ``Adw.NavigationView``
  that any page can call to push an artist/album/track page, or to navigate to
  an artist by *name* (members/bands have no provider id, so they resolve via a
  smart search).
"""

from __future__ import annotations

import logging
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gtk  # noqa: E402

from harmony.models import Album, Service, Track  # noqa: E402
from harmony.tasks import run_async  # noqa: E402
from harmony.ui.state import AppState  # noqa: E402

log = logging.getLogger(__name__)


def service_value(service: Service | str) -> str:
    """Normalize a ``Service`` enum or its raw value to the engine's string id."""
    return service.value if isinstance(service, Service) else str(service)


# -- dict → model converters --------------------------------------------------


def track_from_dict(d: dict[str, Any]) -> Track:
    """Rebuild a :class:`~harmony.models.Track` from an engine track dict.

    ``artist`` arrives pre-joined for display, so it becomes a single-element
    ``artists`` list (``artist_name`` then renders it verbatim); the parallel
    ``artist_ids`` are preserved for "Go to artist" navigation.
    """
    artist = d.get("artist") or ""
    return Track(
        id=d.get("id", ""),
        title=d.get("title", ""),
        service=Service(d["service"]),
        artists=[artist] if artist else [],
        artist_ids=list(d.get("artist_ids") or []),
        album=d.get("album"),
        album_id=d.get("album_id"),
        duration_s=d.get("duration_s"),
        isrc=d.get("isrc"),
        year=d.get("year"),
        track_number=d.get("track_number"),
        artwork_url=d.get("artwork_url"),
    )


def album_from_dict(d: dict[str, Any]) -> Album:
    """Rebuild an :class:`~harmony.models.Album` from an engine album dict.

    ``id`` may be ``None`` (a person's "performed-on" album with no confident
    provider match); callers guard navigation/playback on it being truthy.
    """
    artist = d.get("artist") or ""
    return Album(
        id=d.get("id") or "",
        title=d.get("title", ""),
        service=Service(d["service"]),
        artists=[artist] if artist else [],
        artist_ids=list(d.get("artist_ids") or []),
        year=d.get("year"),
        date=d.get("date"),
        track_count=d.get("track_count"),
        artwork_url=d.get("artwork_url"),
    )


# -- shared artwork loader ----------------------------------------------------

#: url -> Gdk.Texture, shared across every detail page for the app's lifetime.
_ART_CACHE: dict[str, Any] = {}


def load_artwork_into(
    image: Gtk.Image, url: str | None, *, fallback_icon: str = "emblem-music-symbolic"
) -> None:
    """Load ``url`` into ``image`` off the main loop, falling back to an icon.

    Mirrors ``NowPlayingBar._load_art``: only the network fetch and the
    GdkPixbuf decode run on the worker thread (both thread-safe); the
    ``Gdk.Texture`` — a GDK object — is created in the main-thread callback,
    because building it off-thread segfaults GTK.
    """
    if not url:
        image.set_from_icon_name(fallback_icon)
        return
    cached = _ART_CACHE.get(url)
    if cached is not None:
        image.set_from_paintable(cached)
        return
    image.set_from_icon_name(fallback_icon)

    def work() -> Any:
        import requests
        from gi.repository import GdkPixbuf

        data = requests.get(url, timeout=8).content
        loader = GdkPixbuf.PixbufLoader()
        loader.write(data)
        loader.close()
        return loader.get_pixbuf()

    def done(pixbuf: Any) -> None:
        from gi.repository import Gdk

        texture = Gdk.Texture.new_for_pixbuf(pixbuf)
        _ART_CACHE[url] = texture
        image.set_from_paintable(texture)

    run_async(work, done, lambda exc: log.debug("art load failed for %s: %s", url, exc))


# -- navigation ---------------------------------------------------------------


class Navigator:
    """A page-facing handle on the window's ``Adw.NavigationView``.

    Constructed once by the window and handed to every detail page so a row,
    a name link, or a context-menu action can push the next page (or pop back
    to the top-level view stack). Page classes are imported lazily inside the
    methods so this module — and the pages that hold a ``Navigator`` — never
    form an import cycle with one another or with the window.
    """

    def __init__(self, nav_view: Any, state: AppState) -> None:
        self._nav_view = nav_view
        self.state = state

    def go_to_artist(self, service: Service | str, artist_id: str) -> None:
        from harmony.ui.artist_page import ArtistPage

        self._nav_view.push(ArtistPage(self.state, self, service_value(service), artist_id))

    def go_to_album(self, service: Service | str, album_id: str) -> None:
        from harmony.ui.album_page import AlbumPage

        self._nav_view.push(AlbumPage(self.state, self, service_value(service), album_id))

    def go_to_track(self, service: Service | str, track_id: str) -> None:
        from harmony.ui.track_page import TrackPage

        self._nav_view.push(TrackPage(self.state, self, service_value(service), track_id))

    def go_to_ref(self, ref: dict[str, Any]) -> None:
        """Navigate to an ``ArtistRef``/``AlbumRef`` dict (``{service,id,...}``)."""
        if not ref or not ref.get("id"):
            return
        if "title" in ref:
            self.go_to_album(ref["service"], ref["id"])
        else:
            self.go_to_artist(ref["service"], ref["id"])

    def go_to_artist_by_name(self, name: str) -> None:
        """Navigate to an artist we only know by name (a band member / a band).

        Members and bands carry no provider id, so this resolves the name via a
        smart search off the main loop and pushes the artist page for the
        confident match; a miss just toasts rather than opening a dead page.
        """
        name = (name or "").strip()
        if not name:
            return

        def work() -> dict[str, Any]:
            from harmony.web.server import get_engine

            return get_engine().search_smart(name)

        def done(result: dict[str, Any]) -> None:
            section = result.get("artist")
            if section and section.get("ref", {}).get("id"):
                self.go_to_ref(section["ref"])
                return
            artists = (result.get("incidental") or {}).get("artists") or []
            if artists and artists[0].get("id"):
                self.go_to_ref(artists[0])
                return
            self.state.toast(f"Couldn't find “{name}”.")

        run_async(work, done, lambda exc: self.state.toast(f"Couldn't look up “{name}”: {exc}"))
