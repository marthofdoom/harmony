"""Offline tests for ``harmony.ui.collection_actions``.

Covers the two shared entry points (``play_collection_on_device`` and
``add_collection_to_playlist``) directly against a lightweight fake state,
plus ``track_menu_actions`` and the wiring of all four action sets (album,
artist, playlist, track) in ``search_page``/``playlists_page``.

Follows the same offline idioms as ``tests/test_context_menus.py``: real
GTK/libadwaita widgets, never a real ``Gtk.Popover.popup()`` (no realized
top-level window here -> segfault), and a synchronous stand-in for
``run_async`` since nothing here pumps a live GLib main loop.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

# This module exercises the UI layer, which imports PyGObject. The offline
# multi-version CI job has no GTK, so skip cleanly there.
pytest.importorskip("gi")

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from conftest import FakeProvider  # noqa: E402
from gi.repository import GObject, Gtk  # noqa: E402

from harmony import config as config_module  # noqa: E402
from harmony.models import Album, Artist, Playlist, Service, Track  # noqa: E402
from harmony.ui import collection_actions as collection_actions_module  # noqa: E402
from harmony.ui.collection_actions import (  # noqa: E402
    add_collection_to_playlist,
    play_collection_on_device,
    track_menu_actions,
)
from harmony.ui.playlists_page import PlaylistsPage  # noqa: E402
from harmony.ui.search_page import SearchPage  # noqa: E402
from harmony.ui.state import AppState  # noqa: E402

# -- shared fixtures --------------------------------------------------------------


@pytest.fixture
def no_real_popup(monkeypatch: pytest.MonkeyPatch) -> list[Gtk.Popover]:
    """Capture every ``Gtk.Popover.popup()`` call instead of performing it.

    None of the widgets built directly in this file are realized inside a
    ``Gtk.Window``, and a real ``popup()`` there segfaults on the missing GDK
    surface (see ``tests/test_context_menus.py`` for the same idiom).
    """
    captured: list[Gtk.Popover] = []
    monkeypatch.setattr(Gtk.Popover, "popup", lambda self: captured.append(self))
    return captured


@pytest.fixture(autouse=True)
def sync_run_async(monkeypatch: pytest.MonkeyPatch):
    """Run ``collection_actions.run_async`` inline instead of on a worker thread.

    The real ``run_async`` delivers its callback via ``GLib.idle_add``, which
    needs a running main loop to ever fire -- nothing here runs one.
    """

    def _sync_run_async(fn, on_done=None, on_error=None, *args, **kwargs):
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - mirrors run_async's own catch-all
            if on_error is not None:
                on_error(exc)
            return
        if on_done is not None:
            on_done(result)

    monkeypatch.setattr(collection_actions_module, "run_async", _sync_run_async)


class FakeState:
    """Minimal duck-typed stand-in for ``AppState`` exposing only what
    ``collection_actions`` touches: devices, playlists, providers, toasts,
    and ``play_tracks_on_device``."""

    def __init__(self) -> None:
        self.providers: dict[Service, object] = {}
        self.recommender = None
        self._devices: list[object] = []
        self._playlists: dict[Service, list[Playlist]] = {}
        self.toasts: list[str] = []
        self.play_calls: list[tuple[list[Track], str]] = []
        self.refreshed = False

    def toast(self, text: str) -> None:
        self.toasts.append(text)

    def known_devices(self) -> list[object]:
        return self._devices

    def all_playlists(self, refresh: bool = False) -> dict[Service, list[Playlist]]:
        if refresh:
            self.refreshed = True
        return self._playlists

    def play_tracks_on_device(self, tracks: list[Track], host: str) -> None:
        self.play_calls.append((list(tracks), host))


def _device(name: str, host: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, host=host)


def _listbox_of(popover: Gtk.Popover) -> Gtk.ListBox:
    scroller = popover.get_child()
    child = scroller.get_child()
    # A Gtk.ScrolledWindow wraps a non-Gtk.Scrollable child (our ListBox) in
    # an implicit Gtk.Viewport -- unwrap it to get at the real list.
    if isinstance(child, Gtk.Viewport):
        child = child.get_child()
    assert isinstance(child, Gtk.ListBox)
    return child


def _row_titles(listbox: Gtk.ListBox) -> list[str]:
    titles = []
    row = listbox.get_row_at_index(0)
    i = 1
    while row is not None:
        titles.append(row.get_title())
        row = listbox.get_row_at_index(i)
        i += 1
    return titles


# -- play_collection_on_device -----------------------------------------------------


def test_play_collection_on_device_lists_known_devices(no_real_popup) -> None:
    state = FakeState()
    state._devices = [_device("Living Room", "10.0.0.5"), _device("Kitchen", "10.0.0.6")]
    widget = Gtk.Button()

    play_collection_on_device(widget, state, label="Ziggy Stardust", fetch_tracks=lambda: [])

    assert len(no_real_popup) == 1
    assert _row_titles(_listbox_of(no_real_popup[0])) == ["Living Room", "Kitchen"]


def test_play_collection_on_device_no_devices_shows_placeholder(no_real_popup) -> None:
    state = FakeState()
    widget = Gtk.Button()

    play_collection_on_device(widget, state, label="Ziggy Stardust", fetch_tracks=lambda: [])

    listbox = _listbox_of(no_real_popup[0])
    assert _row_titles(listbox) == ["No devices yet"]
    assert listbox.get_row_at_index(0).get_sensitive() is False


def test_play_collection_on_device_picking_a_device_plays_fetched_tracks(no_real_popup) -> None:
    state = FakeState()
    state._devices = [_device("Living Room", "10.0.0.5")]
    tracks = [
        Track(id="1", title="Five Years", service=Service.YTMUSIC, artists=["Bowie"]),
        Track(id="2", title="Soul Love", service=Service.YTMUSIC, artists=["Bowie"]),
    ]
    widget = Gtk.Button()

    play_collection_on_device(widget, state, label="Ziggy Stardust", fetch_tracks=lambda: tracks)
    listbox = _listbox_of(no_real_popup[0])
    listbox.get_row_at_index(0).emit("activated")

    assert state.play_calls == [(tracks, "10.0.0.5")]
    assert state.toasts == ["Playing Ziggy Stardust on Living Room…", "Playing Ziggy Stardust on Living Room"]


def test_play_collection_on_device_empty_tracks_toasts_and_does_not_play(no_real_popup) -> None:
    state = FakeState()
    state._devices = [_device("Living Room", "10.0.0.5")]
    widget = Gtk.Button()

    play_collection_on_device(widget, state, label="Empty Album", fetch_tracks=lambda: [])
    listbox = _listbox_of(no_real_popup[0])
    listbox.get_row_at_index(0).emit("activated")

    assert state.play_calls == []
    assert state.toasts == ["Playing Empty Album on Living Room…", "Empty Album has no tracks to play."]


def test_play_collection_on_device_fetch_error_toasts(no_real_popup) -> None:
    state = FakeState()
    state._devices = [_device("Living Room", "10.0.0.5")]
    widget = Gtk.Button()

    def boom():
        raise RuntimeError("network down")

    play_collection_on_device(widget, state, label="Ziggy Stardust", fetch_tracks=boom)
    listbox = _listbox_of(no_real_popup[0])
    listbox.get_row_at_index(0).emit("activated")

    assert state.play_calls == []
    assert state.toasts[-1] == "Couldn't play Ziggy Stardust on Living Room: network down"


# -- add_collection_to_playlist -----------------------------------------------------


def test_add_collection_to_playlist_lists_playlists_across_services(no_real_popup) -> None:
    state = FakeState()
    state.providers = {Service.YTMUSIC: FakeProvider(Service.YTMUSIC), Service.QOBUZ: FakeProvider(Service.QOBUZ)}
    state._playlists = {
        Service.YTMUSIC: [Playlist(id="pl1", title="Faves", service=Service.YTMUSIC, track_count=3)],
        Service.QOBUZ: [Playlist(id="pl2", title="Chill", service=Service.QOBUZ, track_count=1)],
    }
    widget = Gtk.Button()

    add_collection_to_playlist(widget, state, label="Ziggy Stardust", fetch_tracks=lambda: [])

    assert _row_titles(_listbox_of(no_real_popup[0])) == ["Faves", "Chill"]


def test_add_collection_to_playlist_no_playlists_shows_placeholder(no_real_popup) -> None:
    state = FakeState()
    widget = Gtk.Button()

    add_collection_to_playlist(widget, state, label="Ziggy Stardust", fetch_tracks=lambda: [])

    listbox = _listbox_of(no_real_popup[0])
    assert _row_titles(listbox) == ["No playlists yet"]
    assert listbox.get_row_at_index(0).get_sensitive() is False


def test_add_collection_to_playlist_picking_a_playlist_adds_fetched_track_ids(no_real_popup) -> None:
    state = FakeState()
    tracks = [
        Track(id="a", title="Five Years", service=Service.YTMUSIC, artists=["Bowie"]),
        Track(id="b", title="Soul Love", service=Service.YTMUSIC, artists=["Bowie"]),
    ]
    provider = FakeProvider(Service.YTMUSIC, catalog=tracks)
    state.providers = {Service.YTMUSIC: provider}
    playlist = Playlist(id="pl1", title="Faves", service=Service.YTMUSIC, track_count=0)
    state._playlists = {Service.YTMUSIC: [playlist]}
    widget = Gtk.Button()

    add_collection_to_playlist(widget, state, label="Ziggy Stardust", fetch_tracks=lambda: tracks)
    listbox = _listbox_of(no_real_popup[0])
    listbox.get_row_at_index(0).emit("activated")

    assert [t.id for t in provider.playlists["pl1"]] == ["a", "b"]
    assert state.refreshed is True
    assert state.toasts == ["Added 2 track(s) from Ziggy Stardust to Faves"]


def test_add_collection_to_playlist_empty_tracks_toasts_and_does_not_add(no_real_popup) -> None:
    state = FakeState()
    provider = FakeProvider(Service.YTMUSIC)
    state.providers = {Service.YTMUSIC: provider}
    playlist = Playlist(id="pl1", title="Faves", service=Service.YTMUSIC)
    state._playlists = {Service.YTMUSIC: [playlist]}
    widget = Gtk.Button()

    add_collection_to_playlist(widget, state, label="Empty Album", fetch_tracks=lambda: [])
    listbox = _listbox_of(no_real_popup[0])
    listbox.get_row_at_index(0).emit("activated")

    assert provider.playlists.get("pl1", []) == []
    assert state.refreshed is False
    assert state.toasts == ["Empty Album has no tracks to add."]


# -- track_menu_actions ---------------------------------------------------------


def test_track_menu_actions_full_providers() -> None:
    state = FakeState()
    state.providers = {Service.YTMUSIC: FakeProvider(Service.YTMUSIC), Service.QOBUZ: FakeProvider(Service.QOBUZ)}
    track = Track(id="1", title="T", service=Service.YTMUSIC, artists=["A"])

    labels = [label for label, _cb in track_menu_actions(Gtk.Button(), state, track)]

    assert labels == ["Play on Device", "Add to Playlist…", "Show Similar", "Find on Other Service"]


def test_track_menu_actions_no_other_service_configured() -> None:
    state = FakeState()
    state.providers = {Service.YTMUSIC: FakeProvider(Service.YTMUSIC)}
    track = Track(id="1", title="T", service=Service.YTMUSIC, artists=["A"])

    labels = [label for label, _cb in track_menu_actions(Gtk.Button(), state, track)]

    assert labels == ["Play on Device", "Add to Playlist…", "Show Similar"]


def test_track_menu_actions_no_providers_at_all() -> None:
    state = FakeState()
    track = Track(id="1", title="T", service=Service.YTMUSIC, artists=["A"])

    labels = [label for label, _cb in track_menu_actions(Gtk.Button(), state, track)]

    assert labels == ["Play on Device", "Add to Playlist…"]


def test_track_menu_actions_show_similar_toasts_when_recommender_unavailable(no_real_popup) -> None:
    state = FakeState()
    state.providers = {Service.YTMUSIC: FakeProvider(Service.YTMUSIC)}
    state.recommender = None
    track = Track(id="1", title="T", service=Service.YTMUSIC, artists=["A"])

    actions = dict(track_menu_actions(Gtk.Button(), state, track))
    actions["Show Similar"]()

    assert state.toasts == ["Recommendations aren't available."]
    assert no_real_popup == []  # present_similar (an Adw.Dialog) was never reached


def test_track_menu_actions_play_on_device_wraps_single_track(no_real_popup) -> None:
    state = FakeState()
    state._devices = [_device("Living Room", "10.0.0.5")]
    track = Track(id="1", title="T", service=Service.YTMUSIC, artists=["A"])

    actions = dict(track_menu_actions(Gtk.Button(), state, track))
    actions["Play on Device"]()
    listbox = _listbox_of(no_real_popup[0])
    listbox.get_row_at_index(0).emit("activated")

    assert state.play_calls == [([track], "10.0.0.5")]


# -- wiring: search_page / playlists_page entity rows include the new actions -----


@pytest.fixture
def fake_state(monkeypatch: pytest.MonkeyPatch, tmp_path) -> AppState:
    """A bare ``AppState`` with an isolated ``Settings`` file and no providers.

    Same "bypass __init__, wire a fake state" idiom as
    ``tests/test_context_menus.py`` -- everything exercised here only reads
    ``providers``/``recommender`` and calls the synchronous, network-free
    bits of the public API.
    """
    monkeypatch.setattr(config_module, "settings_path", lambda: tmp_path / "settings.json")
    state = AppState.__new__(AppState)
    GObject.Object.__init__(state)
    state.settings = config_module.Settings.load()
    state.credentials = None
    state.db = None
    state.providers = {}
    state.provider_errors = {}
    state.sync_engine = None
    state.recommender = None
    state.planner = None
    state._playlist_cache = {}
    state._loading_playlists = False
    state._playlists_refresh_pending = False
    state._loading_providers = False
    state._providers_reload_pending = False
    state._device_session = None
    state._relay = None
    state._now_playing = {}
    state.toasts: list[str] = []
    state.connect("toast", lambda _s, text: state.toasts.append(text))
    return state


def test_search_page_album_row_actions_include_play_and_add(fake_state: AppState) -> None:
    fake_state.providers = {Service.YTMUSIC: FakeProvider(Service.YTMUSIC)}
    page = SearchPage(fake_state)
    album = Album(id="al1", title="Ziggy", service=Service.YTMUSIC, artists=["Bowie"])

    labels = [label for label, _cb in page._other_row_actions(album)]

    assert labels[:2] == ["Play on Device", "Add to Playlist…"]


def test_search_page_artist_row_actions_include_play_and_add(fake_state: AppState) -> None:
    fake_state.providers = {Service.YTMUSIC: FakeProvider(Service.YTMUSIC)}
    page = SearchPage(fake_state)
    artist = Artist(id="ar1", name="Bowie", service=Service.YTMUSIC)

    labels = [label for label, _cb in page._other_row_actions(artist)]

    assert labels[:2] == ["Play on Device", "Add to Playlist…"]


def test_search_page_playlist_row_actions_include_play_and_add(fake_state: AppState) -> None:
    fake_state.providers = {Service.YTMUSIC: FakeProvider(Service.YTMUSIC)}
    page = SearchPage(fake_state)
    playlist = Playlist(id="pl1", title="Faves", service=Service.YTMUSIC)

    labels = [label for label, _cb in page._other_row_actions(playlist)]

    assert labels[:2] == ["Play on Device", "Add to Playlist…"]


def test_search_page_collection_row_actions_omit_play_and_add_without_native_provider(fake_state: AppState) -> None:
    """No provider for the item's own service -> nothing to fetch tracks with,
    so Play on Device / Add to Playlist are omitted entirely (same shape as
    the pre-existing Show Similar guard)."""
    fake_state.providers = {Service.QOBUZ: FakeProvider(Service.QOBUZ)}
    page = SearchPage(fake_state)
    album = Album(id="al1", title="Ziggy", service=Service.YTMUSIC, artists=["Bowie"])

    labels = [label for label, _cb in page._other_row_actions(album)]

    assert "Play on Device" not in labels
    assert "Add to Playlist…" not in labels


def test_playlists_page_playlist_row_actions_include_play_and_add(fake_state: AppState) -> None:
    fake_state.providers = {Service.YTMUSIC: FakeProvider(Service.YTMUSIC)}
    page = PlaylistsPage(fake_state)
    playlist = Playlist(id="pl1", title="Faves", service=Service.YTMUSIC)
    wrapper = Gtk.ListBoxRow()

    labels = [label for label, _cb in page._playlist_row_actions(playlist, wrapper)]

    assert labels == ["Play on Device", "Add to Playlist…", "Show Similar", "Open"]


def test_playlists_page_track_column_view_has_full_track_menu(fake_state: AppState) -> None:
    """The Playlists page's track ``ColumnView`` must pass ``on_row_menu`` so
    tracks there get the same menu Search's own track list has."""
    fake_state.providers = {Service.YTMUSIC: FakeProvider(Service.YTMUSIC), Service.QOBUZ: FakeProvider(Service.QOBUZ)}
    page = PlaylistsPage(fake_state)
    track = Track(id="1", title="T", service=Service.YTMUSIC, artists=["A"])

    labels = [label for label, _cb in page._track_row_actions(track)]

    assert labels == ["Play on Device", "Add to Playlist…", "Show Similar", "Find on Other Service"]
