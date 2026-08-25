"""Shared row wrappers and small widget builders used across pages.

Kept separate so each page module stays focused on its own layout instead of
re-deriving the same ``Gtk.ColumnView`` plumbing or confirmation-dialog
boilerplate.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, GObject, Gtk  # noqa: E402

from harmony.models import Playlist, Track  # noqa: E402

log = logging.getLogger(__name__)


class TrackObject(GObject.Object):
    """Wraps a :class:`~harmony.models.Track` for use in a ``Gio.ListStore``."""

    def __init__(self, track: Track) -> None:
        super().__init__()
        self.track = track


class PlaylistObject(GObject.Object):
    """Wraps a :class:`~harmony.models.Playlist` for use in list widgets."""

    def __init__(self, playlist: Playlist) -> None:
        super().__init__()
        self.playlist = playlist


def tracks_to_store(tracks: Iterable[Track]) -> Gio.ListStore:
    store = Gio.ListStore(item_type=TrackObject)
    for track in tracks:
        store.append(TrackObject(track))
    return store


def replace_tracks(store: Gio.ListStore, tracks: Iterable[Track]) -> None:
    """Swap the contents of an existing track store without recreating widgets."""
    store.remove_all()
    for track in tracks:
        store.append(TrackObject(track))


# Column label -> (getter, expand). Shared by Search/Playlists/Sync track views.
_TRACK_COLUMNS: list[tuple[str, Callable[[Track], str], bool]] = [
    ("Title", lambda t: t.title, True),
    ("Artist", lambda t: t.artist_name, True),
    ("Album", lambda t: t.album or "", True),
    ("Duration", lambda t: t.duration_text, False),
    ("Service", lambda t: t.service.label, False),
]


def _label_factory(getter: Callable[[Track], str]) -> Gtk.SignalListItemFactory:
    factory = Gtk.SignalListItemFactory()

    def setup(_factory: Gtk.SignalListItemFactory, item: Gtk.ListItem) -> None:
        label = Gtk.Label(xalign=0.0, ellipsize=3)  # PANGO_ELLIPSIZE_END
        item.set_child(label)

    def bind(_factory: Gtk.SignalListItemFactory, item: Gtk.ListItem) -> None:
        label = item.get_child()
        track_obj = item.get_item()
        if isinstance(label, Gtk.Label) and isinstance(track_obj, TrackObject):
            label.set_label(getter(track_obj.track))

    factory.connect("setup", setup)
    factory.connect("bind", bind)
    return factory


def build_track_column_view() -> tuple[Gtk.ColumnView, Gio.ListStore, Gtk.MultiSelection]:
    """Build a multi-select ``Gtk.ColumnView`` for track lists.

    Returns the view plus the backing store and selection model so callers can
    populate rows and read the current selection.
    """
    store = Gio.ListStore(item_type=TrackObject)
    selection = Gtk.MultiSelection(model=store)
    column_view = Gtk.ColumnView(model=selection)
    column_view.add_css_class("data-table")
    column_view.set_show_row_separators(True)
    for title, getter, expand in _TRACK_COLUMNS:
        column = Gtk.ColumnViewColumn(title=title, factory=_label_factory(getter))
        column.set_expand(expand)
        column_view.append_column(column)
    return column_view, store, selection


def selected_tracks(selection: Gtk.MultiSelection) -> list[Track]:
    """Return the ``Track`` objects currently selected in a column view."""
    tracks: list[Track] = []
    bitset = selection.get_selection()
    ok, iterator, value = Gtk.BitsetIter.init_first(bitset)
    while ok:
        item = selection.get_item(value)
        if isinstance(item, TrackObject):
            tracks.append(item.track)
        ok, value = iterator.next()
    return tracks


def confirm_dialog(
    parent: Gtk.Widget,
    heading: str,
    body: str,
    *,
    on_confirm: Callable[[], None],
    ok_label: str = "Delete",
    destructive: bool = True,
) -> None:
    """Show an ``Adw.AlertDialog`` and invoke ``on_confirm`` if accepted."""
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("confirm", ok_label)
    appearance = Adw.ResponseAppearance.DESTRUCTIVE if destructive else Adw.ResponseAppearance.SUGGESTED
    dialog.set_response_appearance("confirm", appearance)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")

    def _on_response(_dlg: Adw.AlertDialog, response: str) -> None:
        if response == "confirm":
            on_confirm()

    dialog.connect("response", _on_response)
    dialog.present(parent)


def status_page(
    *,
    icon_name: str = "dialog-information-symbolic",
    title: str,
    description: str = "",
    child: Gtk.Widget | None = None,
) -> Adw.StatusPage:
    page = Adw.StatusPage(icon_name=icon_name, title=title, description=description)
    if child is not None:
        page.set_child(child)
    return page


def loading_status_page(title: str = "Loading…") -> Adw.StatusPage:
    spinner = Gtk.Spinner(spinning=True, width_request=32, height_request=32)
    return status_page(icon_name="", title=title, child=spinner)


def error_status_page(exc: BaseException, *, title: str = "Something went wrong") -> Adw.StatusPage:
    return status_page(
        icon_name="dialog-error-symbolic",
        title=title,
        description=str(exc) or exc.__class__.__name__,
    )


def set_stack_status(stack: Gtk.Stack, name: str, widget: Gtk.Widget) -> None:
    """Show ``widget`` as the page named ``name`` in ``stack``, replacing it and
    making it visible.

    ``Gtk.Stack.add_named`` warns and does nothing if ``name`` is already
    registered — it keeps showing whatever was added first under that name.
    Status pages that get rebuilt on every load/error (e.g. "Couldn't load
    tracks") must call this instead of ``add_named`` directly, or the new
    content silently never appears.
    """
    existing = stack.get_child_by_name(name)
    if existing is not None:
        stack.remove(existing)
    stack.add_named(widget, name)
    stack.set_visible_child_name(name)


def missing_layer_status_page(layer_name: str) -> Adw.StatusPage:
    """Placeholder shown when a backend module hasn't landed yet.

    Keeps the UI launchable while providers/matching/sync/etc. are written in
    parallel by other agents.
    """
    return status_page(
        icon_name="emblem-system-symbolic",
        title="Not available yet",
        description=f"The {layer_name} module isn't wired up yet. This page will "
        "come to life once it lands.",
    )


class ProgressDialog(Adw.Window):
    """Modal progress indicator for long-running sync/match operations.

    Backed by a ``CancelToken`` so the Cancel button can cooperatively stop
    the worker thread per the threading contract in ``tasks.py``. The normal
    path is: caller wires ``harmony.tasks.run_async(..., on_cancelled=dialog.close)``
    so the dialog closes itself the moment the worker actually unwinds.

    That normal path depends on the caller wiring ``on_cancelled`` correctly,
    which is easy to get wrong or forget — and this dialog is modal with
    ``deletable=False``, so a caller bug here means an unkillable window, not
    just a stale spinner. As a second line of defence, if "Cancelling…" sits
    for more than a few seconds with nobody having closed us, this dialog
    frees itself: it becomes deletable and the button turns into a plain
    "Close" the user can act on. This dialog must never be the only thing
    standing between the user and a usable window.
    """

    _CANCEL_GRACE_SECONDS = 5

    def __init__(self, parent: Gtk.Window, title: str, cancel_token: Any) -> None:
        super().__init__(
            transient_for=parent,
            modal=True,
            default_width=420,
            default_height=160,
            resizable=False,
            title=title,
            deletable=False,
        )
        self._cancel_token = cancel_token
        self._grace_source_id: int | None = None
        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        toolbar_view.add_top_bar(header)

        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=24,
            margin_bottom=24,
            margin_start=24,
            margin_end=24,
        )
        self._label = Gtk.Label(label="Starting…", xalign=0.0, wrap=True)
        self._bar = Gtk.ProgressBar()
        self._cancel_button = Gtk.Button(label="Cancel", halign=Gtk.Align.END)
        self._cancel_button.connect("clicked", self._on_cancel_clicked)
        box.append(self._label)
        box.append(self._bar)
        box.append(self._cancel_button)
        toolbar_view.set_content(box)
        self.set_content(toolbar_view)
        self.connect("close-request", lambda *_a: self._clear_grace_timeout() and False)

    def _on_cancel_clicked(self, _button: Gtk.Button) -> None:
        if self._cancel_token.cancelled:
            # Cancel was already requested and we're still open past the grace
            # period — this click is the "Close" fallback, not a real cancel.
            self.close()
            return
        self._cancel_token.cancel()
        self._cancel_button.set_sensitive(False)
        self._label.set_label("Cancelling…")
        self._grace_source_id = GLib.timeout_add_seconds(
            self._CANCEL_GRACE_SECONDS, self._on_cancel_grace_elapsed
        )

    def _on_cancel_grace_elapsed(self) -> bool:
        self._grace_source_id = None
        self.set_deletable(True)
        self._cancel_button.set_sensitive(True)
        self._cancel_button.set_label("Close")
        self._label.set_label("Still cancelling… you can close this window now.")
        return GLib.SOURCE_REMOVE

    def _clear_grace_timeout(self) -> bool:
        if self._grace_source_id is not None:
            GLib.source_remove(self._grace_source_id)
            self._grace_source_id = None
        return True

    def close(self) -> bool:
        self._clear_grace_timeout()
        return super().close()

    def update(self, fraction: float, message: str) -> None:
        self._bar.set_fraction(max(0.0, min(1.0, fraction)))
        if message:
            self._label.set_label(message)
