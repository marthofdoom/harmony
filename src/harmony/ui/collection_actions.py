"""Shared right-click actions for "collections" (albums, artists, playlists).

Search and Playlists both need "Play on Device" and "Add to Playlist" on
album/artist/playlist rows, not just on individual tracks -- both play a
whole collection as a device queue or add every track in it to a playlist in
one shot. Factored here so neither page module re-derives the same
device/playlist picker popovers (see ``search_page._open_device_popover`` /
``_open_playlist_popover`` for the single-track originals this generalizes).

Also exposes ``track_menu_actions``, the full per-track context menu (Play on
Device, Add to Playlist, Show Similar, Find on Other Service) so a page that
lists tracks (e.g. Playlists' track ``Gtk.ColumnView``) can wire the same menu
Search already has for its own track list, without hand-rolling it again.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from harmony.models import Playlist, Service, Track  # noqa: E402
from harmony.tasks import run_async  # noqa: E402
from harmony.ui.similar_dialog import present_similar  # noqa: E402
from harmony.ui.state import AppState  # noqa: E402
from harmony.ui.widgets import open_list_popover  # noqa: E402

log = logging.getLogger(__name__)


# -- play a collection (album/artist/playlist/track) on a device ------------------


def play_collection_on_device(
    parent: Gtk.Widget,
    state: AppState,
    *,
    label: str,
    fetch_tracks: Callable[[], list[Track]],
    collection_key: tuple[Service, str] | None = None,
) -> None:
    """Open a device picker; on pick, fetch the collection's tracks and queue them.

    ``fetch_tracks`` does provider I/O and ``state.play_tracks_on_device``
    does device I/O, so both run together on the worker thread once a device
    is chosen -- this function itself only ever touches widgets.

    ``collection_key`` is the album/playlist's ``(service, id)``; passing it
    lets on-screen indicators light up the source collection while it plays.
    """
    devices = state.playback_targets()
    popover = Gtk.Popover()
    listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
    listbox.add_css_class("boxed-list")
    for info in devices:
        is_local = info.kind == "local"
        row = Adw.ActionRow(title=info.name, subtitle="In this app" if is_local else info.host)
        row.set_activatable(True)
        row.add_prefix(Gtk.Image.new_from_icon_name(
            "computer-symbolic" if is_local else "audio-speakers-symbolic"
        ))

        def _pick(_row: Adw.ActionRow, host: str = info.host, name: str = info.name, pop: Gtk.Popover = popover) -> None:
            pop.popdown()
            _play_collection(state, label, fetch_tracks, host, name, collection_key)

        row.connect("activated", _pick)
        listbox.append(row)
    open_list_popover(popover, parent, listbox)


def _play_collection(
    state: AppState,
    label: str,
    fetch_tracks: Callable[[], list[Track]],
    host: str,
    name: str,
    collection_key: tuple[Service, str] | None = None,
) -> None:
    def work() -> int:
        tracks = list(fetch_tracks())
        if not tracks:
            return 0
        state.play_tracks_on_device(tracks, host, collection_key)
        return len(tracks)

    def done(count: int) -> None:
        if count == 0:
            state.toast(f"{label} has no tracks to play.")
            return
        state.toast(f"Playing {label} on {name}")

    run_async(work, done, lambda exc: state.toast(f"Couldn't play {label} on {name}: {exc}"))


# -- add a collection's tracks to a playlist ---------------------------------------


def add_collection_to_playlist(
    parent: Gtk.Widget,
    state: AppState,
    *,
    label: str,
    fetch_tracks: Callable[[], list[Track]],
) -> None:
    """Open a playlist picker; on pick, fetch the collection's tracks and add them.

    Adds to the chosen playlist's own provider (which may differ from the
    collection's service -- e.g. adding an album's tracks to a Qobuz
    playlist), the same cross-service shape ``search_page`` already uses for
    single tracks.
    """
    playlists_by_service = state.all_playlists()
    popover = Gtk.Popover()
    listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
    listbox.add_css_class("boxed-list")
    found = False
    for service in Service:
        provider = state.providers.get(service)
        if provider is None:
            continue
        for playlist in playlists_by_service.get(service, []):
            row = Adw.ActionRow(title=playlist.title, subtitle=f"{service.label} · {playlist.track_count or 0} tracks")
            row.set_activatable(True)

            def _pick(_row: Adw.ActionRow, p: Playlist = playlist, pop: Gtk.Popover = popover) -> None:
                pop.popdown()
                _add_collection(state, label, fetch_tracks, p)

            row.connect("activated", _pick)
            listbox.append(row)
            found = True
    if not found:
        listbox.append(Adw.ActionRow(title="No playlists yet", sensitive=False))
    open_list_popover(popover, parent, listbox)


def _add_collection(
    state: AppState, label: str, fetch_tracks: Callable[[], list[Track]], playlist: Playlist
) -> None:
    provider = state.providers.get(playlist.service)
    if provider is None:
        state.toast(f"No provider configured for {playlist.service.label}")
        return

    def work() -> int:
        tracks = list(fetch_tracks())
        if not tracks:
            return 0
        provider.add_tracks(playlist.id, [t.id for t in tracks])
        return len(tracks)

    def done(count: int) -> None:
        if count == 0:
            state.toast(f"{label} has no tracks to add.")
            return
        state.toast(
            GLib.dngettext(None,
                "Added %d track from %s to %s", "Added %d tracks from %s to %s", count
            )
            % (count, label, playlist.title)
        )
        state.all_playlists(refresh=True)
        state.emit("playlist-tracks-changed", playlist)

    run_async(work, done, lambda exc: state.toast(f"Couldn't add tracks: {exc}"))


# -- full per-track context menu, shared across pages ------------------------------


def _open_similar_or_toast(parent: Gtk.Widget, state: AppState, title: str, fetch: Callable[[], list]) -> None:
    if state.recommender is None:
        state.toast("Recommendations aren't available.")
        return
    present_similar(parent, state, title=title, fetch=fetch)


def _find_other_for_track(state: AppState, track: Track) -> None:
    other = Service.QOBUZ if track.service == Service.YTMUSIC else Service.YTMUSIC
    target_provider = state.providers.get(other)
    if target_provider is None:
        state.toast(f"{other.label} isn't configured")
        return

    def work():  # noqa: ANN202 - MatchResult, imported lazily below
        from harmony.matching import match_track

        return match_track(track, target_provider)

    def done(result: object) -> None:
        best = getattr(result, "best", None)
        confidence = getattr(result, "confidence", "none")
        if best is None:
            state.toast("No match found on the other service.")
            return
        matched = best.track
        state.toast(f"{confidence.title()} match: {matched.artist_name} — {matched.title} ({best.score:.2f})")

    run_async(work, done, lambda exc: state.toast(f"Matching failed: {exc}"))


def track_menu_actions(parent: Gtk.Widget, state: AppState, track: Track) -> list[tuple[str, Callable[[], None]]]:
    """The full track context menu: Play on Device, Add to Playlist, Show
    Similar, Find on Other Service.

    Mirrors ``search_page.SearchPage._track_row_actions`` so any page that
    lists tracks (Playlists' track ``ColumnView``, in particular) gets the
    identical menu Search's own track list already has.
    """
    actions: list[tuple[str, Callable[[], None]]] = [
        ("Play on Device", lambda: play_collection_on_device(parent, state, label=track.title, fetch_tracks=lambda: [track])),
        ("Add to Playlist…", lambda: add_collection_to_playlist(parent, state, label=track.title, fetch_tracks=lambda: [track])),
    ]
    provider = state.providers.get(track.service) or next(iter(state.providers.values()), None)
    if provider is not None:
        actions.append((
            "Show Similar",
            lambda: _open_similar_or_toast(
                parent, state, f"Similar to {track.title}",
                lambda: state.recommender.similar_to_tracks([track], provider, limit=40),
            ),
        ))
    other = Service.QOBUZ if track.service == Service.YTMUSIC else Service.YTMUSIC
    if other in state.providers:
        actions.append(("Find on Other Service", lambda: _find_other_for_track(state, track)))
    return actions
