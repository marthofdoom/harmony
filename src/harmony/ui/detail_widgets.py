"""Reusable building blocks for the artist/album/track detail pages.

Keeps the three page modules thin: a header with artwork, a bio card, album-row
groups (navigable, with context menus), name-row groups (members/bands, resolved
by name), and a track list that reuses Search's column view + the shared
Play/Add collection actions plus "Go to artist/album" navigation.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from harmony.ui.collection_actions import (  # noqa: E402
    add_collection_to_playlist,
    play_collection_on_device,
    play_track_here,
)
from harmony.ui.entity_nav import Navigator, load_artwork_into, track_from_dict  # noqa: E402
from harmony.ui.widgets import (  # noqa: E402
    attach_context_menu,
    build_track_column_view,
    replace_tracks,
)


def artwork_header(image_url: str | None, title: str, subtitle: str,
                   *, badge: str | None = None, icon: str = "emblem-music-symbolic") -> Gtk.Widget:
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18,
                  margin_top=8, margin_bottom=4)
    image = Gtk.Image(pixel_size=128)
    image.add_css_class("card")
    load_artwork_into(image, image_url, fallback_icon=icon)
    box.append(image)

    text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER, hexpand=True)
    name = Gtk.Label(label=title, xalign=0.0, wrap=True)
    name.add_css_class("title-1")
    text.append(name)
    if subtitle:
        sub = Gtk.Label(label=subtitle, xalign=0.0, wrap=True)
        sub.add_css_class("dim-label")
        text.append(sub)
    if badge:
        chip = Gtk.Label(label=badge, xalign=0.0)
        chip.add_css_class("caption")
        chip.add_css_class("dim-label")
        chip_row = Gtk.Box()
        chip_row.append(chip)
        text.append(chip_row)
    box.append(text)
    return box


def bio_card(bio: dict[str, Any] | None) -> Gtk.Widget | None:
    if not bio or not bio.get("text"):
        return None
    group = Adw.PreferencesGroup()
    label = Gtk.Label(label=bio["text"], xalign=0.0, wrap=True, margin_top=6,
                      margin_bottom=6, margin_start=6, margin_end=6)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    box.append(label)
    url = bio.get("url")
    if url:
        source = "Wikipedia" if bio.get("source") == "wikipedia" else "Source"
        link = Gtk.LinkButton(uri=url, label=f"Read more on {source}", halign=Gtk.Align.START)
        box.append(link)
    group.add(box)
    return group


def _album_subtitle(album: dict[str, Any]) -> str:
    return album.get("artist") or ""


def album_group(title: str, albums: list[dict[str, Any]], navigator: Navigator,
                menu_builder: Callable[[dict[str, Any]], list[tuple[str, Callable[[], None]]]] | None = None,
                *, empty_text: str = "Nothing here yet.") -> Adw.PreferencesGroup:
    """A PreferencesGroup of album rows, chronological with a year column.

    Rows with a truthy ``id`` navigate to the album page and get a context menu;
    rows without one (a person's performed-on album with no provider match) are
    shown as informational, non-activatable rows.
    """
    group = Adw.PreferencesGroup(title=title)
    if not albums:
        group.add(Adw.ActionRow(title=empty_text, sensitive=False))
        return group
    for album in albums:
        row = Adw.ActionRow(title=album.get("title", ""), subtitle=_album_subtitle(album))
        year = album.get("year")
        if year:
            year_label = Gtk.Label(label=str(year), valign=Gtk.Align.CENTER)
            year_label.add_css_class("dim-label")
            year_label.add_css_class("numeric")
            row.add_prefix(year_label)
        aid = album.get("id")
        if aid:
            row.set_activatable(True)
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            row.connect("activated", lambda _r, a=album: navigator.go_to_album(a["service"], a["id"]))
            if menu_builder is not None:
                attach_context_menu(row, lambda a=album: menu_builder(a))
        else:
            row.set_subtitle((_album_subtitle(album) + " · not on this service").strip(" ·"))
        group.add(row)
    return group


def name_group(title: str, entries: list[dict[str, Any]],
               on_activate: Callable[[str], None]) -> Adw.PreferencesGroup | None:
    """A group of name rows (band members / a person's bands), resolved by name."""
    if not entries:
        return None
    group = Adw.PreferencesGroup(title=title)
    for e in entries:
        name = e.get("name", "")
        row = Adw.ActionRow(title=name, subtitle=e.get("subtitle", ""))
        row.set_activatable(True)
        row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        row.connect("activated", lambda _r, n=name: on_activate(n))
        group.add(row)
    return group


def track_menu_builder(state: Any, navigator: Navigator, anchor_getter: Callable[[], Gtk.Widget]):
    """Return an ``on_row_menu(track)`` for a detail-page track column view.

    Reuses Search's tested Play/Add collection actions for a single track and
    adds navigation to the track's artist/album/track pages.
    """
    def build(track: Any) -> list[tuple[str, Callable[[], None]]]:
        anchor = anchor_getter()
        actions: list[tuple[str, Callable[[], None]]] = [
            ("Play on Device",
             lambda: play_collection_on_device(anchor, state, label=track.title, fetch_tracks=lambda: [track])),
            ("Add to Playlist…",
             lambda: add_collection_to_playlist(anchor, state, label=track.title, fetch_tracks=lambda: [track])),
        ]
        if track.artist_ids:
            actions.append(("Go to Artist", lambda: navigator.go_to_artist(track.service, track.artist_ids[0])))
        if track.album_id:
            actions.append(("Go to Album", lambda: navigator.go_to_album(track.service, track.album_id)))
        actions.append(("Track details", lambda: navigator.go_to_track(track.service, track.id)))
        return actions

    return build


def tracks_widget(track_dicts: list[dict[str, Any]], state: Any, navigator: Navigator,
                  *, title: str | None = None) -> Gtk.Widget:
    """A titled, naturally-sized track list reusing Search's column view."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    if title:
        heading = Gtk.Label(label=title, xalign=0.0)
        heading.add_css_class("heading")
        box.append(heading)
    holder: dict[str, Gtk.Widget] = {}
    column_view, store, _sel = build_track_column_view(
        on_row_menu=track_menu_builder(state, navigator, lambda: holder["cv"]), state=state,
        on_row_activate=lambda t: play_track_here(state, t))
    holder["cv"] = column_view
    replace_tracks(store, [track_from_dict(d) for d in track_dicts])
    scroller = Gtk.ScrolledWindow(child=column_view)
    scroller.set_propagate_natural_height(True)
    scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
    scroller.set_min_content_height(120)
    box.append(scroller)
    return box
