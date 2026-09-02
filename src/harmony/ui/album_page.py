"""Album detail page: header + navigable artist + the track listing."""

from __future__ import annotations

import logging
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from harmony.tasks import run_async  # noqa: E402
from harmony.ui.detail_widgets import artwork_header, tracks_widget  # noqa: E402
from harmony.ui.entity_nav import Navigator  # noqa: E402
from harmony.ui.widgets import error_status_page, loading_status_page  # noqa: E402

log = logging.getLogger(__name__)


class AlbumPage(Adw.NavigationPage):
    def __init__(self, state: Any, navigator: Navigator, service: str, album_id: str) -> None:
        super().__init__(title="Album")
        self.state = state
        self.nav = navigator
        self.service = service
        self.album_id = album_id

        self._title = Adw.WindowTitle(title="Album")
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(self._title)
        toolbar.add_top_bar(header)

        self._stack = Gtk.Stack()
        self._stack.add_named(loading_status_page("Loading album…"), "loading")
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

            return get_engine().album_page(self.service, self.album_id)

        run_async(work, self._populate, self._on_error)

    def _populate(self, data: dict[str, Any]) -> None:
        album = data.get("album") or {}
        title = album.get("title") or "Album"
        self.set_title(title)
        self._title.set_title(title)

        bits = [album.get("artist") or ""]
        when = album.get("date") or (str(album["year"]) if album.get("year") else "")
        if when:
            bits.append(when)
        count = album.get("track_count")
        if count:
            bits.append(f"{count} tracks")
        subtitle = " · ".join(b for b in bits if b)
        self._content.append(artwork_header(album.get("artwork_url"), title, subtitle,
                                            icon="media-optical-symbolic"))

        artist_ref = data.get("artist_ref")
        if artist_ref and artist_ref.get("id"):
            group = Adw.PreferencesGroup()
            row = Adw.ActionRow(title=artist_ref.get("name", ""), subtitle="Artist")
            row.set_activatable(True)
            row.add_prefix(Gtk.Image.new_from_icon_name("avatar-default-symbolic"))
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            row.connect("activated", lambda *_a: self.nav.go_to_ref(artist_ref))
            group.add(row)
            self._content.append(group)

        tracks = data.get("tracks") or []
        self._content.append(tracks_widget(tracks, self.state, self.nav, title="Tracks"))
        self._stack.set_visible_child_name("content")

    def _on_error(self, exc: BaseException) -> None:
        log.exception("Couldn't load album page")
        self._stack.add_named(error_status_page(exc, title="Couldn't load this album"), "error")
        self._stack.set_visible_child_name("error")
