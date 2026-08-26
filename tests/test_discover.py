"""Offline tests for the Discover page's Recommendations section.

Mirrors ``tests/test_context_menus.py``'s idioms: bypass ``AppState.__init__``
and wire a bare, network-free fake state (see its docstring), capture
``Gtk.Popover.popup()`` instead of performing it (an unparented popover's
real ``popup()`` segfaults with no GDK surface), and stand in for
``harmony.tasks.run_async``/``on_main`` with synchronous versions so nothing
here depends on a running GLib main loop.

Nothing touches the network, a real provider, or the user's real config/data
dirs.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

# This file exercises the UI layer, which imports PyGObject. The offline
# multi-version CI job has no GTK, so skip cleanly there; the GTK ui-smoke
# job runs these.
pytest.importorskip("gi")

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from conftest import FakeProvider  # noqa: E402
from gi.repository import GObject, Gtk  # noqa: E402

from harmony import config as config_module  # noqa: E402
from harmony.models import Playlist, Service, Track  # noqa: E402
from harmony.ui import discover_page as discover_page_module  # noqa: E402
from harmony.ui.discover_page import (  # noqa: E402
    DiscoverPage,
    _format_suggestion_subtitle,
    _humanize_sources,
)
from harmony.ui.state import AppState  # noqa: E402


@pytest.fixture
def fake_state(monkeypatch: pytest.MonkeyPatch, tmp_path) -> AppState:
    """A bare ``AppState`` with an isolated ``Settings`` file and no providers.

    Bypasses ``AppState.__init__`` (no db, no CredentialStore, no worker-
    thread provider build), same as ``tests/test_context_menus.py``, since
    everything under test here only reads ``providers``/``recommender`` and
    calls the synchronous, network-free bits of the public API.
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


@pytest.fixture
def no_real_popup(monkeypatch: pytest.MonkeyPatch) -> list[Gtk.Popover]:
    """Capture every ``Gtk.Popover.popup()`` call instead of performing it."""
    captured: list[Gtk.Popover] = []

    def fake_popup(self: Gtk.Popover) -> None:
        captured.append(self)

    monkeypatch.setattr(Gtk.Popover, "popup", fake_popup)
    return captured


def _sync_run_async(fn, on_done=None, on_error=None, *args, **kwargs):
    """Stand-in for ``harmony.tasks.run_async`` that runs ``fn`` inline.

    The real one hands the result back via ``GLib.idle_add``, which needs a
    running main loop to ever fire -- these tests don't run one, so
    ``discover_page.run_async`` is monkeypatched to this synchronous version.
    """
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - mirrors run_async's own catch-all
        if on_error is not None:
            on_error(exc)
        return
    if on_done is not None:
        on_done(result)


def _sync_on_main(fn, *args):
    """Stand-in for ``harmony.tasks.on_main`` that calls ``fn`` inline."""
    fn(*args)


@pytest.fixture(autouse=True)
def sync_tasks(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(discover_page_module, "run_async", _sync_run_async)
    monkeypatch.setattr(discover_page_module, "on_main", _sync_on_main)


def _row_titles(listbox: Gtk.ListBox) -> list[str]:
    titles = []
    row = listbox.get_row_at_index(0)
    i = 1
    while row is not None:
        titles.append(row.get_title())
        row = listbox.get_row_at_index(i)
        i += 1
    return titles


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


def _icon_names(widget: Gtk.Widget) -> list[str]:
    names = []
    for w in _children(widget):
        if isinstance(w, Gtk.Image):
            name = w.get_icon_name()
            if name:
                names.append(name)
    return names


def _suggestion(title, artist, *, resolved=None, sources=("lastfm",), score=1.0):
    return SimpleNamespace(title=title, artist=artist, sources=list(sources), score=score,
                            resolved=resolved, reason="")


def _page(fake_state: AppState, *, with_recommender: bool = True) -> DiscoverPage:
    if with_recommender:
        fake_state.recommender = SimpleNamespace(similar_to_tracks=lambda *a, **k: [])
    return DiscoverPage(fake_state)


# -- _format_suggestion_subtitle / _humanize_sources (pure functions) -----------------


def test_humanize_sources_maps_known_keys() -> None:
    assert _humanize_sources(["lastfm", "listenbrainz"], "YouTube Music") == ["Last.fm", "ListenBrainz"]


def test_humanize_sources_maps_provider_to_target_label() -> None:
    assert _humanize_sources(["provider"], "Qobuz") == ["Qobuz"]


def test_humanize_sources_dedupes_preserving_order() -> None:
    assert _humanize_sources(["lastfm", "provider", "lastfm"], "Qobuz") == ["Last.fm", "Qobuz"]


def test_humanize_sources_passes_through_unknown_keys() -> None:
    assert _humanize_sources(["mystery"], "Qobuz") == ["mystery"]


def test_format_suggestion_subtitle_no_raw_score_or_keys() -> None:
    subtitle = _format_suggestion_subtitle("Radiohead", ["lastfm", "listenbrainz"], "YouTube Music")
    assert subtitle == "Radiohead · via Last.fm, ListenBrainz"
    assert "score" not in subtitle
    assert "lastfm" not in subtitle


def test_format_suggestion_subtitle_no_sources_falls_back_to_artist() -> None:
    assert _format_suggestion_subtitle("Radiohead", [], "YouTube Music") == "Radiohead"


# -- row actions: resolved vs unresolved ------------------------------------------------


def test_resolved_suggestion_row_gets_play_and_add_buttons(fake_state: AppState, no_real_popup) -> None:
    fake_state.providers = {Service.YTMUSIC: FakeProvider(Service.YTMUSIC)}
    page = _page(fake_state)
    resolved_track = Track(id="1", title="Resolved", service=Service.YTMUSIC, artists=["A"])

    row = page._build_suggestion_row(_suggestion("Resolved", "A", resolved=resolved_track), "YouTube Music")

    assert "emblem-ok-symbolic" in _icon_names(row)
    buttons = [w for w in _children(row) if isinstance(w, Gtk.Button)]
    assert len(buttons) == 2
    assert {b.get_tooltip_text() for b in buttons} == {"Play on Device", "Add to Playlist…"}


def test_unresolved_suggestion_row_has_no_action_buttons(fake_state: AppState) -> None:
    page = _page(fake_state)

    row = page._build_suggestion_row(_suggestion("Unresolved", "B", resolved=None), "YouTube Music")

    assert "dialog-question-symbolic" in _icon_names(row)
    assert [w for w in _children(row) if isinstance(w, Gtk.Button)] == []


def test_resolved_suggestion_row_subtitle_is_humanized(fake_state: AppState) -> None:
    page = _page(fake_state)
    resolved_track = Track(id="1", title="Resolved", service=Service.YTMUSIC, artists=["A"])

    row = page._build_suggestion_row(
        _suggestion("Resolved", "A", resolved=resolved_track, sources=["lastfm", "provider"], score=2.34),
        "YouTube Music",
    )

    assert row.get_subtitle() == "A · via Last.fm, YouTube Music"


# -- empty/error states -------------------------------------------------------------------


def test_get_suggestions_done_with_empty_list_shows_status_page(fake_state: AppState) -> None:
    page = _page(fake_state)

    page._on_suggestions_done([], Service.YTMUSIC)

    assert page.suggestions_stack.get_visible_child_name() == "empty"
    assert page.create_from_suggestions_button.get_sensitive() is False


def test_get_suggestions_done_with_results_shows_results(fake_state: AppState) -> None:
    page = _page(fake_state)
    resolved_track = Track(id="1", title="Resolved", service=Service.YTMUSIC, artists=["A"])

    page._on_suggestions_done([_suggestion("Resolved", "A", resolved=resolved_track)], Service.YTMUSIC)

    assert page.suggestions_stack.get_visible_child_name() == "results"
    assert _row_titles(page.suggestions_list) == ["Resolved"]
    assert page.create_from_suggestions_button.get_sensitive() is True
    assert page.regenerate_button.get_sensitive() is True


def test_get_suggestions_error_shows_error_status_and_toasts(fake_state: AppState) -> None:
    page = _page(fake_state)

    page._on_suggestions_error(RuntimeError("network down"))

    assert page.suggestions_stack.get_visible_child_name() == "error"
    assert fake_state.toasts and "network down" in fake_state.toasts[0]


def test_no_recommender_shows_missing_layer_placeholder(fake_state: AppState) -> None:
    page = _page(fake_state, with_recommender=False)

    assert not hasattr(page, "suggestions_stack")
    assert not hasattr(page, "get_suggestions_button")


def test_no_providers_shows_empty_controls_state(fake_state: AppState) -> None:
    page = _page(fake_state)

    assert page.controls_stack.get_visible_child_name() == "empty"
    assert page.get_suggestions_button.get_sensitive() is False


def test_providers_and_seed_available_shows_controls(fake_state: AppState) -> None:
    fake_state.providers = {Service.YTMUSIC: FakeProvider(Service.YTMUSIC)}
    fake_state._playlist_cache = {
        Service.YTMUSIC: [Playlist(id="pl1", title="Faves", service=Service.YTMUSIC)],
    }
    page = _page(fake_state)

    assert page.controls_stack.get_visible_child_name() == "controls"
    assert page.get_suggestions_button.get_sensitive() is True


# -- selection-change invalidation ---------------------------------------------------------


def test_changing_seed_selection_clears_stale_suggestions(fake_state: AppState) -> None:
    fake_state.providers = {Service.YTMUSIC: FakeProvider(Service.YTMUSIC)}
    fake_state._playlist_cache = {
        Service.YTMUSIC: [
            Playlist(id="pl1", title="Faves", service=Service.YTMUSIC),
            Playlist(id="pl2", title="Chill", service=Service.YTMUSIC),
        ],
    }
    page = _page(fake_state)
    resolved_track = Track(id="1", title="Resolved", service=Service.YTMUSIC, artists=["A"])
    page._on_suggestions_done([_suggestion("Resolved", "A", resolved=resolved_track)], Service.YTMUSIC)
    assert page.suggestions_stack.get_visible_child_name() == "results"

    page.seed_dropdown.set_selected(1)

    assert page.suggestions_stack.get_visible_child_name() == "idle"
    assert page.create_from_suggestions_button.get_sensitive() is False
    assert page.regenerate_button.get_sensitive() is False
    assert page._suggestions == []


# -- progress + loading state ---------------------------------------------------------------


def test_get_suggestions_wires_progress_and_reenables_button_when_done(fake_state: AppState) -> None:
    fake_state.providers = {Service.YTMUSIC: FakeProvider(Service.YTMUSIC)}
    seed_playlist = Playlist(id="pl1", title="Faves", service=Service.YTMUSIC)
    fake_state._playlist_cache = {Service.YTMUSIC: [seed_playlist]}

    seen_progress: list[tuple[float, str]] = []
    was_disabled_mid_flight: list[bool] = []

    def fake_similar_to_tracks(seeds, provider, *, limit, progress=None):
        assert progress is not None
        was_disabled_mid_flight.append(page.get_suggestions_button.get_sensitive() is False)
        progress(0.5, "Halfway there")
        seen_progress.append((0.5, "Halfway there"))
        return []

    fake_state.recommender = SimpleNamespace(similar_to_tracks=fake_similar_to_tracks)
    page = DiscoverPage(fake_state)

    page._on_get_suggestions_clicked(page.get_suggestions_button)

    assert was_disabled_mid_flight == [True]
    assert seen_progress == [(0.5, "Halfway there")]
    assert page.suggestions_progress_bar.get_fraction() == 0.5
    assert page.suggestions_progress_label.get_label() == "Halfway there"
    # The synchronous run_async stand-in completes inline, so by the time
    # this returns, the done callback has already re-enabled the button --
    # `was_disabled_mid_flight` above is what actually proves it was
    # disabled *during* the run, not just before/after.
    assert page.get_suggestions_button.get_sensitive() is True


def test_get_suggestions_reenables_button_on_error(fake_state: AppState) -> None:
    fake_state.providers = {Service.YTMUSIC: FakeProvider(Service.YTMUSIC)}
    seed_playlist = Playlist(id="pl1", title="Faves", service=Service.YTMUSIC)
    fake_state._playlist_cache = {Service.YTMUSIC: [seed_playlist]}

    def boom(seeds, provider, *, limit, progress=None):
        raise RuntimeError("recommender exploded")

    fake_state.recommender = SimpleNamespace(similar_to_tracks=boom)
    page = DiscoverPage(fake_state)

    page._on_get_suggestions_clicked(page.get_suggestions_button)

    assert page.get_suggestions_button.get_sensitive() is True
    assert page.suggestions_stack.get_visible_child_name() == "error"
    assert fake_state.toasts and "recommender exploded" in fake_state.toasts[-1]
