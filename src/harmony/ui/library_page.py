"""The Library page: an iTunes-style browser over every library on the mesh.

This instance's folders plus every peer's (instances sharing the personal key)
appear as one library. Four views behind a switcher:

* **Artists** -- a list of artists; picking one shows their albums (cover grid)
  and songs.
* **Albums** -- a cover-art grid, sortable, with a Hi-Res badge.
* **Songs** -- a sortable track table (Genre / Year / Quality columns) that loads
  in pages as you scroll; activating a row plays the whole list from there.
* **Genres** -- a grid of genres; picking one shows its albums.

Threading: every query runs in a ``run_async`` worker that returns plain data
(dataclasses/dicts); widgets (and every GDK texture) are only built in the done
callbacks on the main loop.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GObject, Gtk  # noqa: E402

from harmony.models import Track  # noqa: E402
from harmony.tasks import run_async  # noqa: E402
from harmony.ui.detail_widgets import track_menu_builder, tracks_widget  # noqa: E402
from harmony.ui.entity_nav import (  # noqa: E402
    Navigator,
    album_from_dict,
    load_artwork_into,
    track_from_dict,
)
from harmony.ui.np_info import install_css  # noqa: E402
from harmony.ui.state import AppState  # noqa: E402
from harmony.ui.widgets import (  # noqa: E402
    SegmentedToggle,
    TrackObject,
    build_track_column_view,
    error_status_page,
    loading_status_page,
    status_page,
)

log = logging.getLogger(__name__)

PAGE = 200
EMPTY_TEXT = ("No library found — set one up on your server; instances sharing your "
              "personal key appear here.")
SONGS_CONTEXT = {"kind": "songs", "title": "Songs"}

# Sort dropdown entries for the Albums grid: (label, server order key).
ALBUM_SORTS = [("Title", "title"), ("Artist", "artist"), ("Year", "year"),
               ("Recently added", "recent")]
# Songs column title -> server order key.
SONG_SORTS = {"Title": "title", "Artist": "artist", "Album": "album", "Duration": "duration",
              "Genre": "genre", "Year": "year", "Quality": "quality"}


def fetch_view(view: str, **kw: Any) -> dict[str, Any]:
    """One page of ``view`` from the in-process engine (blocking: call off the main loop)."""
    from harmony.web.server import get_engine

    return get_engine().library_view(view, **kw)


def _songs(page: dict[str, Any]) -> list[Track]:
    return [track_from_dict(d) for d in page.get("songs") or []]


def _albums(page: dict[str, Any]) -> list[Any]:
    return [album_from_dict(d) for d in page.get("albums") or []]


class _Item(GObject.Object):
    """Wraps any python object for a Gio.ListStore."""

    def __init__(self, data: Any) -> None:
        super().__init__()
        self.data = data


class _Pager:
    """Page-at-a-time loading with stale-result protection.

    ``fetch(offset, limit)`` runs on a worker and returns ``(items, total)``;
    ``apply(items, first)`` runs on the main loop. ``reset()`` starts over (a new
    sort/filter): any page still in flight is dropped by a generation check.
    """

    def __init__(self, fetch: Callable[[int, int], tuple[list[Any], int]],
                 apply: Callable[[list[Any], bool], None],
                 on_error: Callable[[BaseException], None]) -> None:
        self._fetch, self._apply, self._on_error = fetch, apply, on_error
        self._gen = 0
        self.offset = 0
        self.total: int | None = None
        self._busy = False

    def reset(self) -> None:
        self._gen += 1
        self.offset, self.total, self._busy = 0, None, False
        self.load_more()

    @property
    def exhausted(self) -> bool:
        return self.total is not None and self.offset >= self.total

    def load_more(self) -> None:
        if self._busy or self.exhausted:
            return
        self._busy = True
        gen, offset = self._gen, self.offset

        def done(result: tuple[list[Any], int]) -> None:
            if gen != self._gen:
                return
            items, total = result
            self._busy = False
            self.total = total if items else offset  # an empty page ends the list
            self.offset = offset + len(items)
            self._apply(items, offset == 0)

        def failed(exc: BaseException) -> None:
            if gen == self._gen:
                self._busy = False
                self._on_error(exc)

        run_async(lambda: self._fetch(offset, PAGE), done, failed)


def _near_bottom_loader(scroller: Gtk.ScrolledWindow, pager: _Pager) -> None:
    """Ask ``pager`` for more whenever ``scroller`` is within a screen of its end."""
    adj = scroller.get_vadjustment()

    def check(*_a: object) -> None:
        if adj.get_value() + adj.get_page_size() >= adj.get_upper() - 600:
            pager.load_more()

    adj.connect("value-changed", check)
    adj.connect("changed", check)


class ArtCard(Gtk.Box):
    """A cover + title + subtitle tile (album, genre). Reused as a grid cell."""

    SIZE = 150

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                         margin_top=8, margin_bottom=8, margin_start=8, margin_end=8)
        install_css()
        self._url: str | None = None
        self.set_size_request(self.SIZE, -1)
        self.image = Gtk.Image(pixel_size=self.SIZE, halign=Gtk.Align.CENTER)
        self.image.add_css_class("card")
        self.badge = Gtk.Label(label="Hi-Res", halign=Gtk.Align.END, valign=Gtk.Align.END,
                               margin_end=6, margin_bottom=6, visible=False)
        self.badge.add_css_class("osd")
        self.badge.add_css_class("hires-badge")
        overlay = Gtk.Overlay(child=self.image, halign=Gtk.Align.CENTER)
        overlay.add_overlay(self.badge)
        self.append(overlay)
        self.title = Gtk.Label(xalign=0.0, ellipsize=3, max_width_chars=1, hexpand=True)
        self.title.add_css_class("heading")
        self.subtitle = Gtk.Label(xalign=0.0, ellipsize=3, max_width_chars=1, hexpand=True)
        self.subtitle.add_css_class("dim-label")
        self.subtitle.add_css_class("caption")
        self.append(self.title)
        self.append(self.subtitle)

    def set_content(self, title: str, subtitle: str, art_url: str | None, hires: bool = False,
                    icon: str = "media-optical-symbolic") -> None:
        self._url = art_url
        self.title.set_label(title)
        self.title.set_tooltip_text(title)
        self.subtitle.set_label(subtitle)
        self.badge.set_visible(hires)
        load_artwork_into(self.image, art_url, fallback_icon=icon,
                          still_wanted=lambda u=art_url: self._url == u)


def album_card_text(album: Any) -> tuple[str, str, bool]:
    """``(title, "Artist · Year", hires)`` for an Album tile."""
    sub = " · ".join(b for b in (album.artist_name, str(album.year) if album.year else "") if b)
    return album.title, sub, (album.quality or {}).get("tier") == "hires"


class CardGrid(Gtk.ScrolledWindow):
    """A paged, virtualised grid of :class:`ArtCard` tiles."""

    def __init__(self, fetch: Callable[[int, int], tuple[list[Any], int]],
                 describe: Callable[[Any], tuple[str, str, str | None, bool]],
                 on_activate: Callable[[Any], None], on_error: Callable[[BaseException], None],
                 icon: str = "media-optical-symbolic") -> None:
        super().__init__(vexpand=True, hexpand=True)
        self.store = Gio.ListStore(item_type=_Item)
        self._describe, self._on_activate, self._icon = describe, on_activate, icon
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", lambda _f, item: item.set_child(ArtCard()))
        factory.connect("bind", self._bind)
        self.grid = Gtk.GridView(model=Gtk.NoSelection(model=self.store), factory=factory,
                                 min_columns=2, max_columns=14, single_click_activate=True)
        self.grid.connect("activate", self._activated)
        self.set_child(self.grid)
        self.pager = _Pager(fetch, self._apply, on_error)
        _near_bottom_loader(self, self.pager)

    def _bind(self, _f: Gtk.SignalListItemFactory, item: Gtk.ListItem) -> None:
        title, sub, art, hires = self._describe(item.get_item().data)
        item.get_child().set_content(title, sub, art, hires, self._icon)

    def _apply(self, items: list[Any], first: bool) -> None:
        if first:
            self.store.remove_all()
        self.store.splice(self.store.get_n_items(), 0, [_Item(i) for i in items])

    def _activated(self, _grid: Gtk.GridView, position: int) -> None:
        item = self.store.get_item(position)
        if item is not None:
            self._on_activate(item.data)

    def reload(self) -> None:
        self.pager.reset()


def _album_describe(album: Any) -> tuple[str, str, str | None, bool]:
    title, sub, hires = album_card_text(album)
    return title, sub, album.artwork_url, hires


# -- Albums ---------------------------------------------------------------------


class AlbumsView(Gtk.Box):
    """Cover grid + sort dropdown. ``genre`` filters it (the Genres drill-down)."""

    def __init__(self, navigator: Navigator, on_error: Callable[[BaseException], None],
                 genre: str | None = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.genre = genre
        self._order = "artist"
        self.count = Gtk.Label(xalign=0.0, hexpand=True)
        self.count.add_css_class("dim-label")
        self.sort_drop = Gtk.DropDown(model=Gtk.StringList.new([lbl for lbl, _k in ALBUM_SORTS]),
                                      selected=1, tooltip_text="Sort albums by")
        self.sort_drop.connect("notify::selected", self._on_sort)
        bar = Gtk.Box(spacing=8, margin_top=6, margin_bottom=2, margin_start=12, margin_end=12)
        bar.append(self.count)
        bar.append(Gtk.Label(label="Sort by"))
        bar.append(self.sort_drop)
        self.append(bar)
        self.grid = CardGrid(self._fetch, _album_describe,
                             lambda a: navigator.go_to_album(a.service, a.id), on_error)
        self.grid.store.connect("items-changed", lambda *_a: self._update_count())
        self.append(self.grid)

    def _fetch(self, offset: int, limit: int) -> tuple[list[Any], int]:
        page = fetch_view("albums", order=self._order, limit=limit, offset=offset,
                          genre=self.genre)
        return _albums(page), int(page.get("total") or 0)

    def _update_count(self) -> None:
        total = self.grid.pager.total
        n = self.grid.store.get_n_items()
        self.count.set_label(f"{n} of {total} albums" if total and n < total else f"{n} albums")

    def _on_sort(self, *_a: object) -> None:
        self._order = ALBUM_SORTS[self.sort_drop.get_selected()][1]
        self.reload()

    def reload(self) -> None:
        self.grid.reload()


# -- Songs ----------------------------------------------------------------------


def _quality_label(t: Track) -> str:
    return (t.quality or {}).get("label") or ""


SONG_COLUMNS: list[tuple[str, Callable[[Track], str], bool]] = [
    ("Title", lambda t: t.title, True),
    ("Artist", lambda t: t.artist_name, True),
    ("Album", lambda t: t.album or "", True),
    ("Duration", lambda t: t.duration_text, False),
    ("Genre", lambda t: t.genre or "", False),
    ("Year", lambda t: str(t.year) if t.year else "", False),
    ("Quality", _quality_label, False),
]


class SongsView(Gtk.Box):
    """Sortable, paged track table over the whole library."""

    def __init__(self, state: AppState, navigator: Navigator,
                 on_error: Callable[[BaseException], None]) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.state = state
        self._order, self._desc = "artist", False
        holder: dict[str, Gtk.Widget] = {}
        self.column_view, self.store, self.selection = build_track_column_view(
            on_row_menu=track_menu_builder(state, navigator, lambda: holder["cv"]),
            state=state,
            on_play_from=lambda ts, i: state.play_list(ts, i, context=dict(SONGS_CONTEXT)),
            columns=SONG_COLUMNS, sort_keys=SONG_SORTS, on_sort=self._on_sort)
        holder["cv"] = self.column_view
        self.count = Gtk.Label(xalign=0.0, margin_start=12, margin_top=6, margin_bottom=2)
        self.count.add_css_class("dim-label")
        self.append(self.count)
        self.scroller = Gtk.ScrolledWindow(child=self.column_view, vexpand=True, hexpand=True)
        self.append(self.scroller)
        self.pager = _Pager(self._fetch, self._apply, on_error)
        _near_bottom_loader(self.scroller, self.pager)
        # Show the default order's arrow without triggering a re-query.
        self._quiet = True
        title_col = next(c for c in self.column_view.get_columns() if c.get_title() == "Artist")
        self.column_view.sort_by_column(title_col, Gtk.SortType.ASCENDING)
        self._quiet = False

    def _fetch(self, offset: int, limit: int) -> tuple[list[Track], int]:
        page = fetch_view("songs", order=self._order, desc=self._desc, limit=limit, offset=offset)
        return _songs(page), int(page.get("total") or 0)

    def _apply(self, tracks: list[Track], first: bool) -> None:
        if first:
            self.store.remove_all()
        self.store.splice(self.store.get_n_items(), 0, [TrackObject(t) for t in tracks])
        total = self.pager.total or 0
        n = self.store.get_n_items()
        self.count.set_label(f"{n} of {total} songs" if n < total else f"{total} songs")

    def _on_sort(self, key: str, desc: bool) -> None:
        if self._quiet or (key, desc) == (self._order, self._desc):
            return
        self._order, self._desc = key, desc
        self.pager.reset()

    def reload(self) -> None:
        self.pager.reset()


# -- Artists --------------------------------------------------------------------


class ArtistsView(Gtk.Paned):
    """Artist list on the left; the selected artist's albums + songs on the right."""

    def __init__(self, state: AppState, navigator: Navigator,
                 on_error: Callable[[BaseException], None]) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, position=280,
                         shrink_start_child=False, shrink_end_child=False)
        self.state, self.nav, self._on_error = state, navigator, on_error
        self._detail_gen = 0
        self._artist_page: Gtk.Widget | None = None

        self.store = Gio.ListStore(item_type=_Item)
        factory = Gtk.SignalListItemFactory()

        def setup(_f: Gtk.SignalListItemFactory, item: Gtk.ListItem) -> None:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_top=6, margin_bottom=6,
                          margin_start=10, margin_end=10)
            name = Gtk.Label(xalign=0.0, ellipsize=3)
            sub = Gtk.Label(xalign=0.0, ellipsize=3)
            sub.add_css_class("dim-label")
            sub.add_css_class("caption")
            box.append(name)
            box.append(sub)
            item.set_child(box)

        def bind(_f: Gtk.SignalListItemFactory, item: Gtk.ListItem) -> None:
            artist = item.get_item().data
            box = item.get_child()
            name, sub = box.get_first_child(), box.get_last_child()
            name.set_label(artist.get("name") or "Unknown")
            n = int(artist.get("album_count") or 0)
            sub.set_label(f"{n} album{'s' if n != 1 else ''}")

        factory.connect("setup", setup)
        factory.connect("bind", bind)
        self.selection = Gtk.SingleSelection(model=self.store, autoselect=False,
                                             can_unselect=True)
        self.selection.set_selected(Gtk.INVALID_LIST_POSITION)
        self.selection.connect("selection-changed", self._on_selected)
        self.list_view = Gtk.ListView(model=self.selection, factory=factory)
        self.list_view.add_css_class("navigation-sidebar")
        self.set_start_child(Gtk.ScrolledWindow(child=self.list_view, vexpand=True,
                                                min_content_width=220))

        self._detail = Gtk.Stack()
        self._detail.add_named(
            status_page(icon_name="avatar-default-symbolic", title="Pick an artist",
                        description="Their albums and songs appear here."), "empty")
        self._detail.add_named(loading_status_page(), "loading")
        self.set_end_child(self._detail)
        self._detail.set_visible_child_name("empty")

    def reload(self) -> None:
        def work() -> list[dict[str, Any]]:
            return fetch_view("artists", limit=5000).get("artists") or []

        def done(artists: list[dict[str, Any]]) -> None:
            self.store.remove_all()
            self.store.splice(0, 0, [_Item(a) for a in artists])

        run_async(work, done, self._on_error)

    def _on_selected(self, *_a: object) -> None:
        pos = self.selection.get_selected()
        item = self.store.get_item(pos) if pos != Gtk.INVALID_LIST_POSITION else None
        if item is None:
            return
        artist = item.data
        self._detail_gen += 1
        gen = self._detail_gen
        self._detail.set_visible_child_name("loading")

        def work() -> tuple[list[Any], list[Track]]:
            aid = artist["id"]
            albums = _albums(fetch_view("albums", artist_id=aid, order="year", limit=500))
            songs = _songs(fetch_view("songs", artist_id=aid, order="album", limit=500))
            return albums, songs

        def done(result: tuple[list[Any], list[Track]]) -> None:
            if gen == self._detail_gen:
                self._show(artist, *result)

        def failed(exc: BaseException) -> None:
            if gen == self._detail_gen:
                self._detail.set_visible_child_name("empty")
                self._on_error(exc)

        run_async(work, done, failed)

    def _show(self, artist: dict[str, Any], albums: list[Any], songs: list[Track]) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=12, margin_bottom=18, margin_start=12, margin_end=12)
        title = Gtk.Label(label=artist.get("name") or "", xalign=0.0, wrap=True)
        title.add_css_class("title-1")
        box.append(title)

        if albums:
            heading = Gtk.Label(label="Albums", xalign=0.0)
            heading.add_css_class("heading")
            box.append(heading)
            flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                               min_children_per_line=2, max_children_per_line=8,
                               activate_on_single_click=True, valign=Gtk.Align.START)
            for album in albums:
                card = ArtCard()
                card.set_content(*_album_describe(album), icon="media-optical-symbolic")
                card.album = album  # type: ignore[attr-defined]
                flow.append(card)
            flow.connect("child-activated", lambda _f, child: self.nav.go_to_album(
                child.get_child().album.service, child.get_child().album.id))
            box.append(flow)

        if songs:
            context = {"kind": "artist", "title": artist.get("name") or "", "service": "local",
                       "id": artist["id"]}
            songs_w = tracks_widget([_track_dict(t) for t in songs], self.state, self.nav,
                                    title="Songs", context=context)
            box.append(songs_w)
        scroller = Gtk.ScrolledWindow(child=Adw.Clamp(child=box, maximum_size=1100),
                                      vexpand=True, hexpand=True)
        if self._artist_page is not None:
            self._detail.remove(self._artist_page)
        self._artist_page = scroller
        self._detail.add_named(scroller, "artist")
        self._detail.set_visible_child_name("artist")


def _track_dict(t: Track) -> dict[str, Any]:
    """A Track back to the engine-dict shape ``tracks_widget`` consumes."""
    from harmony.web.api import track_to_dict

    return track_to_dict(t)


# -- Genres ---------------------------------------------------------------------


class GenresView(Gtk.Stack):
    """Genre tiles; picking one swaps in that genre's albums (with a back button)."""

    def __init__(self, state: AppState, navigator: Navigator,
                 on_error: Callable[[BaseException], None]) -> None:
        super().__init__()
        self.state, self.nav, self._on_error = state, navigator, on_error
        self.grid = CardGrid(self._fetch, self._describe, self._open_genre, on_error,
                             icon="folder-music-symbolic")
        self.add_named(self.grid, "genres")
        self._drill: Gtk.Widget | None = None

    @staticmethod
    def _fetch(offset: int, limit: int) -> tuple[list[dict[str, Any]], int]:
        page = fetch_view("genres", limit=limit, offset=offset)
        return page.get("genres") or [], int(page.get("total") or 0)

    @staticmethod
    def _describe(g: dict[str, Any]) -> tuple[str, str, str | None, bool]:
        a, s = int(g.get("album_count") or 0), int(g.get("track_count") or 0)
        return g.get("name") or "Unknown", f"{a} album{'s' if a != 1 else ''} · {s} songs", \
            g.get("artwork_url"), False

    def _open_genre(self, genre: dict[str, Any]) -> None:
        name = genre.get("name") or ""
        if self._drill is not None:
            self.remove(self._drill)
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bar = Gtk.Box(spacing=8, margin_top=6, margin_start=12, margin_end=12)
        back = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="All genres",
                          css_classes=["flat"])
        back.connect("clicked", lambda *_a: self.set_visible_child_name("genres"))
        heading = Gtk.Label(label=name, xalign=0.0, hexpand=True)
        heading.add_css_class("title-2")
        play = Gtk.Button(css_classes=["suggested-action", "pill"])
        play.set_child(Adw.ButtonContent(icon_name="media-playback-start-symbolic",
                                         label="Play genre"))
        play.connect("clicked", lambda *_a: self._play_genre(name))
        bar.append(back)
        bar.append(heading)
        bar.append(play)
        page.append(bar)
        albums = AlbumsView(self.nav, self._on_error, genre=name)
        page.append(albums)
        albums.reload()
        self._drill = page
        self.add_named(page, "genre")
        self.set_visible_child_name("genre")

    def _play_genre(self, name: str) -> None:
        def work() -> list[Track]:
            return _songs(fetch_view("songs", genre=name, order="artist", limit=1000))

        def done(tracks: list[Track]) -> None:
            if tracks:
                self.state.play_list(tracks, 0, context={"kind": "genre", "title": name})
            else:
                self.state.toast(f"No songs in {name}.")

        run_async(work, done, self._on_error)

    def reload(self) -> None:
        self.set_visible_child_name("genres")
        self.grid.reload()


# -- the page -------------------------------------------------------------------


class LibraryPage(Gtk.Box):
    """Library: Artists | Albums | Songs | Genres across every library on the mesh."""

    STALE_S = 60

    def __init__(self, state: AppState, navigator: Navigator) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.state, self.nav = state, navigator
        self._loaded_at = 0.0
        self._ready = False       # a library exists (probe succeeded)
        self._view_loaded: set[str] = set()
        self._error_page: Gtk.Widget | None = None  # lazily created and reused

        self.switcher = SegmentedToggle(
            [("artists", "Artists"), ("albums", "Albums"), ("songs", "Songs"),
             ("genres", "Genres")], active="songs")
        self.switcher.connect("changed", self._on_view_changed)
        self.switcher_bar = Gtk.Box(halign=Gtk.Align.CENTER,
                                    margin_top=8, margin_bottom=4, visible=False)
        self.switcher_bar.append(self.switcher)
        self.append(self.switcher_bar)

        self.stack = Gtk.Stack(vexpand=True)
        self.append(self.stack)
        self.stack.add_named(loading_status_page("Loading the library…"), "loading")
        self.stack.add_named(
            status_page(icon_name="folder-music-symbolic", title="No library found",
                        description=EMPTY_TEXT), "empty")
        self.views: dict[str, Any] = {
            "artists": ArtistsView(state, navigator, self._on_error),
            "albums": AlbumsView(navigator, self._on_error),
            "songs": SongsView(state, navigator, self._on_error),
            "genres": GenresView(state, navigator, self._on_error),
        }
        for name, view in self.views.items():
            self.stack.add_named(view, name)
        self.stack.set_visible_child_name("loading")

        self.connect("map", self._on_map)
        state.connect("providers-changed", lambda *_a: self.refresh(force=True))

    # -- loading ------------------------------------------------------------

    def _on_map(self, *_a: object) -> None:
        self.refresh()

    def refresh(self, force: bool = False) -> None:
        """Probe for a library, then (re)load the visible view -- at most once a
        minute unless ``force``."""
        if not force and self._ready and time.monotonic() - self._loaded_at < self.STALE_S:
            return
        if not force and not self._ready and self._loaded_at and \
                time.monotonic() - self._loaded_at < 5:
            return
        self._loaded_at = time.monotonic()

        def work() -> int | None:
            page = fetch_view("songs", limit=1)
            return int(page.get("total") or 0) if page.get("enabled") else None

        def done(total: int | None) -> None:
            self._ready = bool(total)
            self.switcher_bar.set_visible(self._ready)
            if not self._ready:
                self.stack.set_visible_child_name("empty")
                return
            self._view_loaded.clear()
            self._show_view(self.switcher.get_active_name() or "songs")

        def failed(exc: BaseException) -> None:
            log.warning("library probe failed: %s", exc)
            if self._error_page is None:
                self._error_page = error_status_page(exc, title="Couldn't load the library")
                self.stack.add_named(self._error_page, "error")
            else:
                # Update the error page's description instead of creating a new one
                error_widget = self._error_page
                if hasattr(error_widget, "get_subtitle"):
                    error_widget.set_subtitle(str(exc))
            self.stack.set_visible_child_name("error")

        run_async(work, done, failed)

    def _on_view_changed(self, *_a: object) -> None:
        if self._ready:
            self._show_view(self.switcher.get_active_name() or "songs")

    def _show_view(self, name: str) -> None:
        if name not in self.views:
            return
        view = self.views[name]
        if name not in self._view_loaded:
            self._view_loaded.add(name)
            view.reload()
        self.stack.set_visible_child_name(name)

    def _on_error(self, exc: BaseException) -> None:
        log.warning("library query failed: %s", exc)
        self.state.toast(f"Library: {exc}")
