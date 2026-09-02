"""Artist detail page: bio, discography, top tracks, and (for a band) the
member-chronology chart. For a person the discography is the "performed-on" list.

Data comes from the in-process engine (``get_engine().artist_page``) off the main
loop; every widget is built back on the main thread in the callback.
"""

from __future__ import annotations

import logging
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from harmony.tasks import run_async  # noqa: E402
from harmony.ui.chronology_chart import ChronologyChart  # noqa: E402
from harmony.ui.detail_widgets import (  # noqa: E402
    album_group,
    artwork_header,
    bio_card,
    name_group,
    tracks_widget,
)
from harmony.ui.entity_nav import Navigator  # noqa: E402
from harmony.ui.widgets import error_status_page, loading_status_page  # noqa: E402

log = logging.getLogger(__name__)


class ArtistPage(Adw.NavigationPage):
    def __init__(self, state: Any, navigator: Navigator, service: str, artist_id: str) -> None:
        super().__init__(title="Artist")
        self.state = state
        self.nav = navigator
        self.service = service
        self.artist_id = artist_id

        self._title = Adw.WindowTitle(title="Artist")
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(self._title)
        toolbar.add_top_bar(header)

        self._stack = Gtk.Stack()
        self._stack.add_named(loading_status_page("Loading artist…"), "loading")
        self._content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                                margin_top=16, margin_bottom=24, margin_start=12, margin_end=12)
        clamp = Adw.Clamp(child=self._content, maximum_size=920)
        self._stack.add_named(Gtk.ScrolledWindow(child=clamp, vexpand=True), "content")
        toolbar.set_content(self._stack)
        self.set_child(toolbar)
        self._stack.set_visible_child_name("loading")

        self._load()

    def _load(self) -> None:
        def work() -> dict[str, Any]:
            from harmony.web.server import get_engine

            return get_engine().artist_page(self.service, self.artist_id)

        run_async(work, self._populate, self._on_error)

    def _album_menu(self, album: dict[str, Any]) -> list[tuple[str, Any]]:
        actions: list[tuple[str, Any]] = [
            ("Open Album", lambda: self.nav.go_to_album(album["service"], album["id"])),
        ]
        artist_ids = album.get("artist_ids") or []
        if artist_ids:
            actions.append(("Go to Artist", lambda: self.nav.go_to_artist(album["service"], artist_ids[0])))
        return actions

    def _populate(self, data: dict[str, Any]) -> None:
        artist = data.get("artist") or {}
        name = artist.get("name") or "Artist"
        self.set_title(name)
        self._title.set_title(name)

        kind = data.get("kind", "unknown")
        badge = {"group": "Band", "person": "Artist"}.get(kind)
        subtitle = {"group": "Band", "person": "Performer"}.get(kind, "")
        self._content.append(artwork_header(artist.get("image_url"), name, subtitle,
                                            badge=badge, icon="avatar-default-symbolic"))

        bio = bio_card(artist.get("bio"))
        if bio is not None:
            self._content.append(bio)

        # Member-chronology chart (bands only).
        chronology = data.get("chronology")
        if chronology:
            chart = ChronologyChart()
            chart.set_chronology(chronology)
            if chart.has_content():
                group = Adw.PreferencesGroup(title="Member timeline")
                frame = Gtk.ScrolledWindow(child=chart)
                frame.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
                frame.set_propagate_natural_height(True)
                frame.add_css_class("card")
                group.add(frame)
                self._content.append(group)

        # Discography (for a person these are the performed-on albums).
        disco_title = "Appears on" if kind == "person" else "Albums"
        albums = data.get("albums") or []
        self._content.append(album_group(disco_title, albums, self.nav, self._album_menu,
                                         empty_text="No albums found."))
        singles = data.get("singles") or []
        if singles:
            self._content.append(album_group("Singles & EPs", singles, self.nav, self._album_menu))

        # Members (band) or bands (person).
        members = data.get("members") or []
        if members:
            entries = [{"name": m.get("name", ""),
                        "subtitle": ", ".join(m.get("instruments", []) or [])} for m in members]
            grp = name_group("Members", entries, self.nav.go_to_artist_by_name)
            if grp is not None:
                self._content.append(grp)
        member_of = data.get("member_of") or []
        if member_of:
            entries = [{"name": b.get("name", ""), "subtitle": _span_text(b.get("spans"))}
                       for b in member_of]
            grp = name_group("Member of", entries, self.nav.go_to_artist_by_name)
            if grp is not None:
                self._content.append(grp)

        top = data.get("top_tracks") or []
        if top:
            self._content.append(tracks_widget(top, self.state, self.nav, title="Top tracks"))

        self._stack.set_visible_child_name("content")

    def _on_error(self, exc: BaseException) -> None:
        log.exception("Couldn't load artist page")
        self._stack.add_named(error_status_page(exc, title="Couldn't load this artist"), "error")
        self._stack.set_visible_child_name("error")


def _span_text(spans: list[list[int | None]] | None) -> str:
    """Render membership spans like ``1999–2017 · 2020–present``."""
    if not spans:
        return ""
    parts = []
    for pair in spans:
        start = pair[0] if pair[0] is not None else "?"
        end = "present" if pair[1] is None else pair[1]
        parts.append(f"{start}–{end}")
    return " · ".join(parts)
