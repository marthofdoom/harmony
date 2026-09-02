"""Track detail page: navigable album/artist links + who performed on it.

Performer credits come from MusicBrainz and are sparse for mainstream pop/rock,
so an empty list is a normal outcome — the page then falls back to the credited
artists and says the detailed credits aren't in MusicBrainz, rather than looking
broken.
"""

from __future__ import annotations

import logging
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from harmony.tasks import run_async  # noqa: E402
from harmony.ui.detail_widgets import artwork_header  # noqa: E402
from harmony.ui.entity_nav import Navigator  # noqa: E402
from harmony.ui.widgets import error_status_page, loading_status_page  # noqa: E402

log = logging.getLogger(__name__)


class TrackPage(Adw.NavigationPage):
    def __init__(self, state: Any, navigator: Navigator, service: str, track_id: str) -> None:
        super().__init__(title="Track")
        self.state = state
        self.nav = navigator
        self.service = service
        self.track_id = track_id

        self._title = Adw.WindowTitle(title="Track")
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(self._title)
        toolbar.add_top_bar(header)

        self._stack = Gtk.Stack()
        self._stack.add_named(loading_status_page("Loading track…"), "loading")
        self._content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                                margin_top=16, margin_bottom=24, margin_start=12, margin_end=12)
        clamp = Adw.Clamp(child=self._content, maximum_size=820)
        self._stack.add_named(Gtk.ScrolledWindow(child=clamp, vexpand=True), "content")
        toolbar.set_content(self._stack)
        self.set_child(toolbar)
        self._stack.set_visible_child_name("loading")
        self._load()

    def _load(self) -> None:
        def work() -> dict[str, Any]:
            from harmony.web.server import get_engine

            return get_engine().track_page(self.service, self.track_id)

        run_async(work, self._populate, self._on_error)

    def _populate(self, data: dict[str, Any]) -> None:
        track = data.get("track") or {}
        title = track.get("title") or "Track"
        self.set_title(title)
        self._title.set_title(title)

        bits = [track.get("artist") or "", track.get("album") or ""]
        if track.get("year"):
            bits.append(str(track["year"]))
        subtitle = " · ".join(b for b in bits if b)
        self._content.append(artwork_header(track.get("artwork_url"), title, subtitle))

        # Navigation to the album + performing artists.
        nav_group = Adw.PreferencesGroup(title="Go to")
        added = False
        album_ref = data.get("album_ref")
        if album_ref and album_ref.get("id"):
            added = True
            nav_group.add(self._ref_row(album_ref.get("title", "Album"), "Album",
                                        "media-optical-symbolic", lambda: self.nav.go_to_ref(album_ref)))
        for ref in data.get("artist_refs") or []:
            if ref.get("id"):
                added = True
                nav_group.add(self._ref_row(ref.get("name", "Artist"), "Artist",
                                            "avatar-default-symbolic",
                                            lambda r=ref: self.nav.go_to_artist(r["service"], r["id"])))
        if added:
            self._content.append(nav_group)

        # Performers (performance credits, not writers).
        performers = data.get("performers") or []
        perf_group = Adw.PreferencesGroup(title="Performers")
        if performers:
            for p in performers:
                roles = ", ".join(p.get("roles", []) or [])
                perf_group.add(Adw.ActionRow(title=p.get("name", ""), subtitle=roles))
        else:
            credited = track.get("artist") or "the credited artists"
            row = Adw.ActionRow(
                title=credited,
                subtitle="Detailed performer credits aren't in MusicBrainz for this recording.",
            )
            row.set_subtitle_lines(0)
            perf_group.add(row)
        self._content.append(perf_group)

        self._stack.set_visible_child_name("content")

    def _ref_row(self, title: str, subtitle: str, icon: str, on_activate: Any) -> Adw.ActionRow:
        row = Adw.ActionRow(title=title, subtitle=subtitle)
        row.set_activatable(True)
        row.add_prefix(Gtk.Image.new_from_icon_name(icon))
        row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        row.connect("activated", lambda *_a: on_activate())
        return row

    def _on_error(self, exc: BaseException) -> None:
        log.exception("Couldn't load track page")
        self._stack.add_named(error_status_page(exc, title="Couldn't load this track"), "error")
        self._stack.set_visible_child_name("error")
