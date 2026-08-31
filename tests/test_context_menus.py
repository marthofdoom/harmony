"""Offline tests for right-click context menus and the "Similar music" dialog.

These build real GTK/libadwaita widgets (the only sanctioned way to test this
layer of UI logic -- see tests/test_devices.py's ``state`` fixture for the
same "bypass __init__, wire a fake state" idiom used here) but never call
``Gtk.Popover.popup()`` for real without either a realized top-level window or
a monkeypatch: an unparented popover's ``popup()`` reliably segfaults (no
GDK surface to anchor to), so ``_no_real_popup`` below captures the popover
instead of showing it wherever a test doesn't itself realize a window.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

# This whole file exercises the UI layer, which imports PyGObject. The
# offline multi-version CI job has no GTK, so skip cleanly there; the GTK
# ui-smoke job runs these.
pytest.importorskip("gi")

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from conftest import FakeProvider  # noqa: E402
from gi.repository import Adw, GLib, GObject, Gtk  # noqa: E402

from harmony import config as config_module  # noqa: E402
from harmony.models import Album, Artist, Playlist, Service, Track  # noqa: E402
from harmony.ui import similar_dialog as similar_dialog_module  # noqa: E402
from harmony.ui.playlists_page import PlaylistsPage  # noqa: E402
from harmony.ui.search_page import SearchPage  # noqa: E402
from harmony.ui.similar_dialog import _SimilarDialog, present_similar  # noqa: E402
from harmony.ui.state import AppState, PlaybackState  # noqa: E402
from harmony.ui.widgets import (  # noqa: E402
    TrackObject,
    attach_context_menu,
    build_track_column_view,
)

# -- shared fixtures --------------------------------------------------------------


@pytest.fixture
def fake_state(monkeypatch: pytest.MonkeyPatch, tmp_path) -> AppState:
    """A bare ``AppState`` with an isolated ``Settings`` file and no providers.

    Bypasses ``AppState.__init__`` (no db, no CredentialStore, no worker-
    thread provider build) like ``tests/test_devices.py``'s fixture, since
    everything under test here only reads ``providers``/``recommender`` and
    calls the synchronous, network-free bits of the public API.
    ``_playlist_cache`` is pre-seeded to ``{}`` (not ``None``) so
    ``all_playlists()`` returns immediately instead of kicking off a
    background load that nothing in this file drains.
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
    state.playback = PlaybackState()
    state.toasts: list[str] = []
    state.connect("toast", lambda _s, text: state.toasts.append(text))
    return state


@pytest.fixture
def no_real_popup(monkeypatch: pytest.MonkeyPatch) -> list[Gtk.Popover]:
    """Capture every ``Gtk.Popover.popup()`` call instead of performing it.

    Every widget built directly in this file (as opposed to realized inside
    a ``Gtk.Window``, see the ColumnView test below) has no top-level window,
    and a real ``popup()`` there segfaults on the missing GDK surface.
    """
    captured: list[Gtk.Popover] = []

    def fake_popup(self: Gtk.Popover) -> None:
        captured.append(self)

    monkeypatch.setattr(Gtk.Popover, "popup", fake_popup)
    return captured


def _find_gesture(widget: Gtk.Widget, klass: type, button: int | None = None):
    controllers = widget.observe_controllers()
    for i in range(controllers.get_n_items()):
        controller = controllers.get_item(i)
        if isinstance(controller, klass) and (button is None or controller.get_button() == button):
            return controller
    return None


def _row_titles(listbox: Gtk.ListBox) -> list[str]:
    titles = []
    row = listbox.get_row_at_index(0)
    i = 1
    while row is not None:
        titles.append(row.get_title())
        row = listbox.get_row_at_index(i)
        i += 1
    return titles


def _pump(predicate, timeout: float = 3.0) -> bool:
    """Iterate the default GLib main context until ``predicate()`` is true.

    Used only by the ColumnView realization test, which needs GTK to
    actually lay out and bind its list items -- something that only happens
    once a window is mapped and the main loop runs. Returns False (never
    raises) on timeout so that test can skip rather than hang or flake on a
    CI worker with no usable display.
    """
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while ctx.pending():
            ctx.iteration(False)
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# -- attach_context_menu ------------------------------------------------------------


def test_attach_context_menu_builds_matching_rows(no_real_popup) -> None:
    actions = [("Foo", lambda: None), ("Bar", lambda: None)]
    widget = Gtk.Button()
    attach_context_menu(widget, lambda: actions)

    gesture = _find_gesture(widget, Gtk.GestureClick, button=3)
    assert gesture is not None
    gesture.emit("pressed", 1, 5.0, 6.0)

    assert len(no_real_popup) == 1
    listbox = no_real_popup[0].get_child()
    assert isinstance(listbox, Gtk.ListBox)
    assert _row_titles(listbox) == ["Foo", "Bar"]


def test_attach_context_menu_activating_a_row_invokes_its_callback(no_real_popup) -> None:
    called: list[str] = []
    actions = [("Foo", lambda: called.append("foo")), ("Bar", lambda: called.append("bar"))]
    widget = Gtk.Button()
    attach_context_menu(widget, lambda: actions)

    _find_gesture(widget, Gtk.GestureClick, button=3).emit("pressed", 1, 0.0, 0.0)
    listbox = no_real_popup[0].get_child()
    listbox.get_row_at_index(1).emit("activated")

    assert called == ["bar"]


def test_attach_context_menu_empty_actions_opens_nothing(no_real_popup) -> None:
    widget = Gtk.Button()
    attach_context_menu(widget, lambda: [])

    _find_gesture(widget, Gtk.GestureClick, button=3).emit("pressed", 1, 0.0, 0.0)

    assert no_real_popup == []


def test_attach_context_menu_long_press_also_opens(no_real_popup) -> None:
    widget = Gtk.Button()
    attach_context_menu(widget, lambda: [("Only", lambda: None)])

    gesture = _find_gesture(widget, Gtk.GestureLongPress)
    assert gesture is not None
    gesture.emit("pressed", 3.0, 4.0)

    assert len(no_real_popup) == 1


def test_attach_context_menu_rebuilds_actions_on_each_open(no_real_popup) -> None:
    """``build_actions`` runs at click time, not attach time -- state may
    have changed between two right-clicks on the same widget."""
    state = {"n": 0}

    def build_actions():
        state["n"] += 1
        return [(f"Item {state['n']}", lambda: None)]

    widget = Gtk.Button()
    attach_context_menu(widget, build_actions)
    gesture = _find_gesture(widget, Gtk.GestureClick, button=3)

    gesture.emit("pressed", 1, 0.0, 0.0)
    gesture.emit("pressed", 1, 0.0, 0.0)

    assert _row_titles(no_real_popup[0].get_child()) == ["Item 1"]
    assert _row_titles(no_real_popup[1].get_child()) == ["Item 2"]


# -- build_track_column_view per-row targeting ---------------------------------------


def test_track_column_view_row_menu_targets_the_clicked_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """Right-clicking a specific row's cell resolves *that* row's Track, not
    just whatever is selected -- realized inside a real Gtk.Window since
    per-row cell widgets only exist once GTK has actually bound the factory.
    """
    captured: list[Track] = []

    def on_row_menu(track: Track) -> list[tuple[str, object]]:
        captured.append(track)
        return [("Show Similar", lambda: None)]

    column_view, store, _selection = build_track_column_view(on_row_menu=on_row_menu)
    track_a = Track(id="a", title="Track A", service=Service.YTMUSIC, artists=["Artist A"])
    track_b = Track(id="b", title="Track B", service=Service.YTMUSIC, artists=["Artist B"])
    store.append(TrackObject(track_a))
    store.append(TrackObject(track_b))

    window = Gtk.Window()
    window.set_child(column_view)
    window.set_default_size(400, 300)

    pops: list[Gtk.Popover] = []
    monkeypatch.setattr(Gtk.Popover, "popup", lambda self: pops.append(self))

    window.present()

    def _find_cell(text: str) -> Gtk.Label | None:
        found: list[Gtk.Label] = []

        def walk(widget: Gtk.Widget) -> None:
            if isinstance(widget, Gtk.Label) and widget.get_label() == text:
                found.append(widget)
            child = widget.get_first_child()
            while child is not None:
                walk(child)
                child = child.get_next_sibling()

        walk(column_view)
        return found[0] if found else None

    if not _pump(lambda: _find_cell("Track B") is not None):
        window.destroy()
        pytest.skip("ColumnView never realized its rows -- no usable display in this environment")

    cell_b = _find_cell("Track B")
    gesture = _find_gesture(cell_b, Gtk.GestureClick, button=3)
    assert gesture is not None
    gesture.emit("pressed", 1, 2.0, 2.0)

    assert captured == [track_b]
    assert len(pops) == 1
    window.destroy()


def test_build_track_column_view_without_on_row_menu_still_works() -> None:
    """Existing callers passing nothing must keep working unchanged."""
    column_view, store, selection = build_track_column_view()
    assert isinstance(column_view, Gtk.ColumnView)
    store.append(TrackObject(Track(id="1", title="T", service=Service.YTMUSIC, artists=["A"])))
    assert selection.get_n_items() == 1


# -- entity action-builders: search_page -------------------------------------------


def _search_page(fake_state: AppState) -> SearchPage:
    return SearchPage(fake_state)


def test_track_row_actions_full_providers(fake_state: AppState) -> None:
    fake_state.providers = {
        Service.YTMUSIC: FakeProvider(Service.YTMUSIC),
        Service.QOBUZ: FakeProvider(Service.QOBUZ),
    }
    page = _search_page(fake_state)
    track = Track(id="1", title="T", service=Service.YTMUSIC, artists=["A"])

    labels = [label for label, _cb in page._track_row_actions(track)]

    assert labels == ["Play on Device", "Add to Playlist…", "Show Similar", "Find on Other Service"]


def test_track_row_actions_no_other_service_configured(fake_state: AppState) -> None:
    fake_state.providers = {Service.YTMUSIC: FakeProvider(Service.YTMUSIC)}
    page = _search_page(fake_state)
    track = Track(id="1", title="T", service=Service.YTMUSIC, artists=["A"])

    labels = [label for label, _cb in page._track_row_actions(track)]

    assert labels == ["Play on Device", "Add to Playlist…", "Show Similar"]


def test_track_row_actions_no_providers_at_all(fake_state: AppState) -> None:
    fake_state.providers = {}
    page = _search_page(fake_state)
    track = Track(id="1", title="T", service=Service.YTMUSIC, artists=["A"])

    labels = [label for label, _cb in page._track_row_actions(track)]

    assert labels == ["Play on Device", "Add to Playlist…"]


def test_artist_row_actions_with_provider(fake_state: AppState) -> None:
    fake_state.providers = {Service.YTMUSIC: FakeProvider(Service.YTMUSIC)}
    page = _search_page(fake_state)
    artist = Artist(id="ar1", name="Bowie", service=Service.YTMUSIC)

    labels = [label for label, _cb in page._other_row_actions(artist)]

    assert labels == ["Play on Device", "Add to Playlist…", "Show Similar", "Open"]


def test_artist_row_actions_falls_back_to_other_provider(fake_state: AppState) -> None:
    """Artist rows only need *somewhere* to resolve matches -- no native
    provider for the artist's own service is required."""
    fake_state.providers = {Service.QOBUZ: FakeProvider(Service.QOBUZ)}
    page = _search_page(fake_state)
    artist = Artist(id="ar1", name="Bowie", service=Service.YTMUSIC)

    labels = [label for label, _cb in page._other_row_actions(artist)]

    assert labels == ["Show Similar", "Open"]


def test_artist_row_actions_no_providers(fake_state: AppState) -> None:
    fake_state.providers = {}
    page = _search_page(fake_state)
    artist = Artist(id="ar1", name="Bowie", service=Service.YTMUSIC)

    labels = [label for label, _cb in page._other_row_actions(artist)]

    assert labels == ["Open"]


def test_album_row_actions_with_native_provider(fake_state: AppState) -> None:
    fake_state.providers = {Service.YTMUSIC: FakeProvider(Service.YTMUSIC)}
    page = _search_page(fake_state)
    album = Album(id="al1", title="Ziggy", service=Service.YTMUSIC, artists=["Bowie"])

    labels = [label for label, _cb in page._other_row_actions(album)]

    assert labels == ["Play on Device", "Add to Playlist…", "Show Similar", "Open"]


def test_album_row_actions_without_native_provider_omits_show_similar(fake_state: AppState) -> None:
    """Unlike tracks/artists, an album's tracks can only be fetched from its
    own service -- there is no fallback provider that could serve them."""
    fake_state.providers = {Service.QOBUZ: FakeProvider(Service.QOBUZ)}
    page = _search_page(fake_state)
    album = Album(id="al1", title="Ziggy", service=Service.YTMUSIC, artists=["Bowie"])

    labels = [label for label, _cb in page._other_row_actions(album)]

    assert labels == ["Open"]


def test_playlist_row_actions_with_native_provider(fake_state: AppState) -> None:
    fake_state.providers = {Service.YTMUSIC: FakeProvider(Service.YTMUSIC)}
    page = _search_page(fake_state)
    playlist = Playlist(id="pl1", title="Faves", service=Service.YTMUSIC)

    labels = [label for label, _cb in page._other_row_actions(playlist)]

    assert labels == ["Play on Device", "Add to Playlist…", "Show Similar", "Open"]


def test_playlist_row_actions_without_native_provider_omits_show_similar(fake_state: AppState) -> None:
    fake_state.providers = {Service.QOBUZ: FakeProvider(Service.QOBUZ)}
    page = _search_page(fake_state)
    playlist = Playlist(id="pl1", title="Faves", service=Service.YTMUSIC)

    labels = [label for label, _cb in page._other_row_actions(playlist)]

    assert labels == ["Open"]


# -- entity action-builders: playlists_page -----------------------------------------


def test_playlists_page_row_actions_with_provider(fake_state: AppState) -> None:
    fake_state.providers = {Service.YTMUSIC: FakeProvider(Service.YTMUSIC)}
    page = PlaylistsPage(fake_state)
    playlist = Playlist(id="pl1", title="Faves", service=Service.YTMUSIC)
    wrapper = Gtk.ListBoxRow()

    labels = [label for label, _cb in page._playlist_row_actions(playlist, wrapper)]

    assert labels == ["Play on Device", "Add to Playlist…", "Show Similar", "Open"]


def test_playlists_page_row_actions_without_provider(fake_state: AppState) -> None:
    fake_state.providers = {}
    page = PlaylistsPage(fake_state)
    playlist = Playlist(id="pl1", title="Faves", service=Service.YTMUSIC)
    wrapper = Gtk.ListBoxRow()

    labels = [label for label, _cb in page._playlist_row_actions(playlist, wrapper)]

    assert labels == ["Open"]


def test_playlists_page_open_action_selects_the_row(fake_state: AppState) -> None:
    fake_state.providers = {}
    page = PlaylistsPage(fake_state)
    playlist = Playlist(id="pl1", title="Faves", service=Service.YTMUSIC)
    row = Adw.ActionRow(title=playlist.title)
    row.playlist = playlist
    wrapper = Gtk.ListBoxRow(child=row)
    wrapper.playlist = playlist
    page.playlist_list.append(wrapper)

    actions = dict(page._playlist_row_actions(playlist, wrapper))
    actions["Open"]()

    assert page.playlist_list.get_selected_row() is wrapper


# -- recommender/provider guards -----------------------------------------------------


def test_open_similar_toasts_when_recommender_unavailable(fake_state: AppState) -> None:
    fake_state.providers = {Service.YTMUSIC: FakeProvider(Service.YTMUSIC)}
    fake_state.recommender = None
    page = _search_page(fake_state)

    page._open_similar("Similar to X", lambda: [])

    assert fake_state.toasts == ["Recommendations aren't available."]


# -- similar_dialog: _SimilarDialog rendering ---------------------------------------


def _suggestion(title: str, artist: str, *, resolved: Track | None = None, sources=("lastfm",), score: float = 1.0):
    return SimpleNamespace(title=title, artist=artist, sources=list(sources), score=score,
                            resolved=resolved, reason="")


def _sync_run_async(fn, on_done=None, on_error=None, *args, **kwargs):
    """Stand-in for ``harmony.tasks.run_async`` that runs ``fn`` inline.

    The real one hands the result back via ``GLib.idle_add``, which needs a
    running main loop to ever fire -- these tests don't run one, so
    ``similar_dialog.run_async`` is monkeypatched to this synchronous
    version instead. No thread, no idle source, no flakiness.
    """
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - mirrors run_async's own catch-all
        if on_error is not None:
            on_error(exc)
        return
    if on_done is not None:
        on_done(result)


@pytest.fixture(autouse=True)
def sync_run_async(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(similar_dialog_module, "run_async", _sync_run_async)


def _icon_names(widget: Gtk.Widget) -> list[str]:
    names: list[str] = []

    def walk(w: Gtk.Widget) -> None:
        if isinstance(w, Gtk.Image):
            name = w.get_icon_name()
            if name:
                names.append(name)
        child = w.get_first_child()
        while child is not None:
            walk(child)
            child = child.get_next_sibling()

    walk(widget)
    return names


def test_similar_dialog_renders_resolved_and_unresolved_rows(fake_state: AppState) -> None:
    fake_state.providers = {Service.YTMUSIC: FakeProvider(Service.YTMUSIC)}
    resolved_track = Track(id="1", title="Resolved", service=Service.YTMUSIC, artists=["A"])
    suggestions = [
        _suggestion("Resolved", "A", resolved=resolved_track),
        _suggestion("Unresolved", "B", resolved=None),
    ]
    dialog = _SimilarDialog(fake_state, "Similar to X")

    dialog.load(lambda: suggestions)

    assert dialog.stack.get_visible_child_name() == "results"
    rows = []
    row = dialog.results_list.get_row_at_index(0)
    i = 1
    while row is not None:
        rows.append(row)
        row = dialog.results_list.get_row_at_index(i)
        i += 1
    assert [r.get_title() for r in rows] == ["Resolved", "Unresolved"]
    assert "emblem-ok-symbolic" in _icon_names(rows[0])
    assert "dialog-question-symbolic" in _icon_names(rows[1])
    # Only the resolved suggestion gets Play/Add suffix buttons.
    assert any(isinstance(c, Gtk.Button) for c in _children(rows[0]))
    assert dialog.create_button.get_sensitive() is True


def test_similar_dialog_empty_results_shows_status_page(fake_state: AppState) -> None:
    dialog = _SimilarDialog(fake_state, "Similar to X")

    dialog.load(lambda: [])

    assert dialog.stack.get_visible_child_name() == "empty"
    assert dialog.create_button.get_sensitive() is False


def test_similar_dialog_all_unresolved_disables_create_button(fake_state: AppState) -> None:
    dialog = _SimilarDialog(fake_state, "Similar to X")

    dialog.load(lambda: [_suggestion("Unresolved", "B", resolved=None)])

    assert dialog.create_button.get_sensitive() is False


def test_similar_dialog_fetch_error_shows_error_status_and_toasts(fake_state: AppState) -> None:
    dialog = _SimilarDialog(fake_state, "Similar to X")

    def boom():
        raise RuntimeError("network down")

    dialog.load(boom)

    assert dialog.stack.get_visible_child_name() == "error"
    assert fake_state.toasts and "network down" in fake_state.toasts[0]


def _children(widget: Gtk.Widget) -> list[Gtk.Widget]:
    found: list[Gtk.Widget] = []

    def walk(w: Gtk.Widget) -> None:
        found.append(w)
        child = w.get_first_child()
        while child is not None:
            walk(child)
            child = child.get_next_sibling()

    walk(widget)
    return found


def test_present_similar_presents_and_loads(fake_state: AppState, monkeypatch: pytest.MonkeyPatch) -> None:
    """``present_similar`` should present the dialog and kick off the fetch --
    checked by spying on ``_SimilarDialog`` rather than calling the real
    ``Adw.Dialog.present()`` (needs a realized parent window)."""
    presented: list[Gtk.Widget] = []
    loaded: list = []

    monkeypatch.setattr(_SimilarDialog, "present", lambda self, parent: presented.append(parent))
    monkeypatch.setattr(_SimilarDialog, "load", lambda self, fetch: loaded.append(fetch))

    parent = Gtk.Button()
    fetch = lambda: []  # noqa: E731

    present_similar(parent, fake_state, title="Similar to X", fetch=fetch)

    assert presented == [parent]
    assert loaded == [fetch]
