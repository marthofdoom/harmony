"""Manage and control WiiM/LinkPlay playback devices.

Every WiiM call (status/play/pause/volume/discover) is blocking HTTP, so
every one of them is dispatched through ``harmony.tasks.run_async`` and
widgets are only ever touched from the ``on_done``/``on_error`` callbacks it
marshals back to the main loop, per the threading rule in
docs/ARCHITECTURE.md. This module never imports ``harmony.playback`` at
module scope — ``AppState.known_devices``/``device_for`` already do that
lazily, and the one place here that needs a playback-layer function
directly (``discover_wiim``) imports it inside the worker function that
calls it, for the same reason.
"""

from __future__ import annotations

import logging
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from harmony.errors import NotSupportedError, ProviderError  # noqa: E402
from harmony.tasks import run_async  # noqa: E402
from harmony.ui.state import AppState  # noqa: E402
from harmony.ui.widgets import confirm_dialog, status_page  # noqa: E402

log = logging.getLogger(__name__)

_VOLUME_DEBOUNCE_MS = 300
_POLL_INTERVAL_S = 2


def _format_time(seconds: int | None) -> str:
    total = max(0, int(seconds or 0))
    minutes, secs = divmod(total, 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


class DevicesPage(Gtk.Box):
    """Add/discover devices on the left, transport controls for one on the right."""

    def __init__(self, state: AppState) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.state = state
        self._selected_host: str | None = None
        self._last_status: Any | None = None
        self._device_cache: dict[str, Any] = {}
        self._updating_from_status = False
        self._volume_debounce_id: int | None = None
        # Quiet position-polling while a device is selected and playing.
        self._poll_id: int | None = None
        self._poll_in_flight = False

        self.append(self._build_top_bar())

        self.content_stack = Gtk.Stack(vexpand=True)
        self.content_stack.add_named(
            status_page(
                icon_name="audio-speakers-symbolic",
                title="No devices",
                description="Add one by IP or hostname, or discover devices on your network.",
            ),
            "empty",
        )
        self.content_stack.add_named(self._build_main(), "main")
        self.append(self.content_stack)

        self.state.connect("devices-changed", self._on_devices_changed)
        self._on_devices_changed()

    # -- layout ---------------------------------------------------------------

    def _build_top_bar(self) -> Gtk.Widget:
        box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
            margin_top=8, margin_bottom=8, margin_start=8, margin_end=8,
        )
        self.add_entry = Gtk.Entry(hexpand=True, placeholder_text="Add device by IP or hostname")
        self.add_entry.connect("activate", self._on_add_clicked)
        box.append(self.add_entry)

        add_button = Gtk.Button(label="Add")
        add_button.connect("clicked", self._on_add_clicked)
        box.append(add_button)

        self.discover_button = Gtk.Button(label="Discover")
        self.discover_button.connect("clicked", self._on_discover_clicked)
        box.append(self.discover_button)

        self.discover_spinner = Gtk.Spinner()
        box.append(self.discover_spinner)
        return box

    def _build_main(self) -> Gtk.Widget:
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, wide_handle=True,
                           shrink_start_child=False, shrink_end_child=False)

        self.device_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.device_list.add_css_class("navigation-sidebar")
        self.device_list.connect("row-selected", self._on_row_selected)
        list_scroller = Gtk.ScrolledWindow(child=self.device_list, vexpand=True, min_content_width=260)
        paned.set_start_child(list_scroller)

        paned.set_end_child(self._build_detail_panel())
        return paned

    def _build_detail_panel(self) -> Gtk.Widget:
        outer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12, hexpand=True,
            margin_top=16, margin_bottom=16, margin_start=16, margin_end=16,
        )

        self.device_title_label = Gtk.Label(xalign=0.0, wrap=True)
        self.device_title_label.add_css_class("title-2")
        outer.append(self.device_title_label)

        now_playing = Adw.PreferencesGroup(title="Now Playing")
        self.state_row = Adw.ActionRow(title="State", subtitle="Unknown")
        self.title_row = Adw.ActionRow(title="Title", subtitle="Nothing playing")
        self.artist_row = Adw.ActionRow(title="Artist", subtitle="—")
        now_playing.add(self.state_row)
        now_playing.add(self.title_row)
        now_playing.add(self.artist_row)
        outer.append(now_playing)

        # Playback position. Only meaningful once the device reports a duration
        # (the UPnP/passthrough path provides one); hidden until then.
        self.progress_bar = Gtk.ProgressBar(show_text=True, margin_top=4)
        self.progress_bar.set_visible(False)
        outer.append(self.progress_bar)

        controls = Adw.PreferencesGroup(title="Volume")
        volume_row = Adw.ActionRow(title="Volume")
        self.volume_scale = Gtk.Scale(
            orientation=Gtk.Orientation.HORIZONTAL, hexpand=True, valign=Gtk.Align.CENTER,
            draw_value=True, width_request=200,
        )
        self.volume_scale.set_range(0, 100)
        self.volume_scale.set_digits(0)
        self.volume_scale.connect("value-changed", self._on_volume_changed)
        volume_row.add_suffix(self.volume_scale)
        controls.add(volume_row)

        self.mute_row = Adw.ActionRow(title="Muted")
        self.mute_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.mute_switch.connect("notify::active", self._on_mute_toggled)
        self.mute_row.add_suffix(self.mute_switch)
        self.mute_row.set_activatable_widget(self.mute_switch)
        controls.add(self.mute_row)
        outer.append(controls)

        transport = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER, margin_top=6)
        self.prev_button = Gtk.Button(icon_name="media-skip-backward-symbolic", tooltip_text="Previous")
        self.play_pause_button = Gtk.Button(icon_name="media-playback-start-symbolic", tooltip_text="Play/Pause")
        self.stop_button = Gtk.Button(icon_name="media-playback-stop-symbolic", tooltip_text="Stop")
        self.next_button = Gtk.Button(icon_name="media-skip-forward-symbolic", tooltip_text="Next")
        self.prev_button.connect("clicked", lambda *_a: self._run_device_action(lambda d: d.previous()))
        self.play_pause_button.connect("clicked", self._on_play_pause_clicked)
        self.stop_button.connect("clicked", lambda *_a: self._run_device_action(lambda d: d.stop()))
        self.next_button.connect("clicked", lambda *_a: self._run_device_action(lambda d: d.next()))
        for button in (self.prev_button, self.play_pause_button, self.stop_button, self.next_button):
            transport.append(button)
        outer.append(transport)

        self.refresh_button = Gtk.Button(label="Refresh", halign=Gtk.Align.START)
        self.refresh_button.connect("clicked", lambda *_a: self._refresh_status())
        outer.append(self.refresh_button)

        self._set_controls_sensitive(False)
        return outer

    # -- device list ------------------------------------------------------------

    def _build_device_row(self, info: Any) -> Gtk.ListBoxRow:
        row = Adw.ActionRow(title=info.name, subtitle=info.host)
        row.device_host = info.host  # type: ignore[attr-defined]
        remove_button = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                                    tooltip_text="Remove device")
        remove_button.add_css_class("flat")
        remove_button.connect("clicked", lambda *_a, h=info.host, n=info.name: self._on_remove_clicked(h, n))
        row.add_suffix(remove_button)
        return row

    def _on_devices_changed(self, *_args: object) -> None:
        devices = self.state.known_devices()
        self._device_cache = {h: d for h, d in self._device_cache.items() if h in {d2.host for d2 in devices}}

        while (row := self.device_list.get_row_at_index(0)) is not None:
            self.device_list.remove(row)

        if not devices:
            self._selected_host = None
            self.content_stack.set_visible_child_name("empty")
            return

        self.content_stack.set_visible_child_name("main")
        target_host = self._selected_host
        select_row = None
        for info in devices:
            row = self._build_device_row(info)
            self.device_list.append(row)
            if info.host == target_host:
                select_row = row
        if select_row is None:
            select_row = self.device_list.get_row_at_index(0)
        self.device_list.select_row(select_row)

    def _on_row_selected(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        host = getattr(row, "device_host", None) if row is not None else None
        if host == self._selected_host:
            return
        self._selected_host = host
        self._reset_detail_panel()
        if host is None:
            self.device_title_label.set_label("")
            return
        info = next((d for d in self.state.known_devices() if d.host == host), None)
        self.device_title_label.set_label(info.name if info is not None else host)
        self._refresh_status()

    def _on_remove_clicked(self, host: str, name: str) -> None:
        def confirmed() -> None:
            self._device_cache.pop(host, None)
            self.state.remove_device(host)

        confirm_dialog(
            self, f"Remove {name}?",
            "Harmony will forget this device. You can add it again by IP or discovery later.",
            on_confirm=confirmed, ok_label="Remove",
        )

    # -- add / discover -----------------------------------------------------

    def _on_add_clicked(self, _widget: Gtk.Widget) -> None:
        host = self.add_entry.get_text().strip()
        if not host:
            return
        self.state.add_device(host)
        self.add_entry.set_text("")

    def _on_discover_clicked(self, _button: Gtk.Button) -> None:
        self.discover_button.set_sensitive(False)
        self.discover_spinner.set_spinning(True)

        def work() -> list[Any]:
            from harmony.playback import discover_wiim

            return discover_wiim()

        run_async(work, self._on_discover_done, self._on_discover_error)

    def _on_discover_done(self, infos: list[Any]) -> None:
        self.discover_button.set_sensitive(True)
        self.discover_spinner.set_spinning(False)
        if not infos:
            self.state.toast("No devices found on the network.")
            return
        known_hosts = {d.host for d in self.state.known_devices()}
        new_infos = [i for i in infos if i.host not in known_hosts]
        if not new_infos:
            self.state.toast(f"Found {len(infos)} device(s) — all already added.")
            return
        self._show_discovery_results(new_infos)

    def _on_discover_error(self, exc: BaseException) -> None:
        self.discover_button.set_sensitive(True)
        self.discover_spinner.set_spinning(False)
        self.state.toast(f"Discovery failed: {exc}")

    def _show_discovery_results(self, infos: list[Any]) -> None:
        popover = Gtk.Popover()
        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")
        for info in infos:
            row = Adw.ActionRow(title=info.name, subtitle=info.host)
            row.set_activatable(True)
            row.add_suffix(Gtk.Image.new_from_icon_name("list-add-symbolic"))

            def _pick(r: Adw.ActionRow, i: Any = info) -> None:
                self.state.add_device(i.host, i.name)
                r.set_sensitive(False)
                r.set_subtitle(f"{i.host} · added")

            row.connect("activated", _pick)
            listbox.append(row)
        scroller = Gtk.ScrolledWindow(child=listbox, max_content_height=320,
                                       propagate_natural_height=True, width_request=280)
        popover.set_child(scroller)
        popover.set_parent(self.discover_button)
        popover.connect("closed", lambda p: p.unparent())
        popover.popup()

    # -- status / controls ----------------------------------------------------

    def _get_device(self, host: str) -> Any:
        device = self._device_cache.get(host)
        if device is None:
            device = self.state.device_for(host)
            self._device_cache[host] = device
        return device

    def _set_controls_sensitive(self, enabled: bool) -> None:
        for widget in (self.volume_scale, self.mute_switch, self.prev_button,
                       self.play_pause_button, self.stop_button, self.next_button):
            widget.set_sensitive(enabled)

    def _reset_detail_panel(self) -> None:
        self._last_status = None
        self.state_row.set_subtitle("Unknown")
        self.title_row.set_subtitle("Nothing playing")
        self.artist_row.set_subtitle("—")
        self._updating_from_status = True
        try:
            self.volume_scale.set_value(0)
            self.mute_switch.set_active(False)
        finally:
            self._updating_from_status = False
        self.play_pause_button.set_icon_name("media-playback-start-symbolic")
        self._set_controls_sensitive(False)
        self.progress_bar.set_visible(False)
        self.progress_bar.set_fraction(0.0)
        self._stop_polling()

    def _apply_status(self, status: Any) -> None:
        self._last_status = status
        self.state_row.set_subtitle((status.state or "unknown").capitalize())
        # A bare relay URL carries no metadata, so the device reports either an
        # empty title or the URL itself while playing what we relayed. In both
        # cases fall back to what we sent it so the UI shows the real track.
        title, artist = status.title or "", status.artist or ""
        if not title or title.startswith(("http://", "https://")):
            remembered = self.state.last_played_on(self._selected_host)
            if remembered is not None:
                title, artist = remembered
        self.title_row.set_subtitle(title or "Nothing playing")
        self.artist_row.set_subtitle(artist or "—")
        self._updating_from_status = True
        try:
            if status.volume is not None:
                self.volume_scale.set_value(status.volume)
            self.mute_switch.set_active(bool(status.muted))
        finally:
            self._updating_from_status = False
        self.play_pause_button.set_icon_name(
            "media-playback-pause-symbolic" if status.state == "playing" else "media-playback-start-symbolic"
        )
        self._set_controls_sensitive(True)
        self._update_progress(status)
        # Keep the position live while playing; stop hammering the device otherwise.
        if status.state == "playing":
            self._start_polling()
        else:
            self._stop_polling()

    def _update_progress(self, status: Any) -> None:
        position, duration = status.position_s, status.duration_s
        if duration and duration > 0 and position is not None:
            self.progress_bar.set_fraction(max(0.0, min(1.0, position / duration)))
            self.progress_bar.set_text(f"{_format_time(position)} / {_format_time(duration)}")
            self.progress_bar.set_visible(True)
        else:
            self.progress_bar.set_visible(False)

    def _start_polling(self) -> None:
        if self._poll_id is None:
            self._poll_id = GLib.timeout_add_seconds(_POLL_INTERVAL_S, self._on_poll_tick)

    def _stop_polling(self) -> None:
        if self._poll_id is not None:
            GLib.source_remove(self._poll_id)
            self._poll_id = None

    def _on_poll_tick(self) -> bool:
        host = self._selected_host
        if host is None:
            self._poll_id = None
            return GLib.SOURCE_REMOVE
        if self._poll_in_flight:
            return GLib.SOURCE_CONTINUE  # don't stack calls on a slow device
        device = self._get_device(host)
        self._poll_in_flight = True

        def work() -> Any:
            return device.status()

        def done(status: Any) -> None:
            self._poll_in_flight = False
            if self._selected_host == host:
                self._apply_status(status)

        def error(_exc: BaseException) -> None:
            # Quiet: a transient poll failure must not disturb the UI or toast.
            self._poll_in_flight = False

        run_async(work, done, error)
        return GLib.SOURCE_CONTINUE

    def _refresh_status(self) -> None:
        host = self._selected_host
        if host is None:
            return
        device = self._get_device(host)
        self.refresh_button.set_sensitive(False)
        # Immediate feedback: a device call can take a few seconds against a
        # slow or unreachable device, and disabled controls with no cue read
        # as a freeze rather than as work in progress.
        self.state_row.set_subtitle("Contacting device…")

        def work() -> tuple[Any, Any]:
            return device.info, device.status()

        def done(result: tuple[Any, Any]) -> None:
            self.refresh_button.set_sensitive(True)
            if self._selected_host != host:
                return  # user picked a different device while this was in flight
            info, status = result
            self._apply_status(status)
            if info.name:
                self.state.set_device_name(host, info.name)

        def error(exc: BaseException) -> None:
            self.refresh_button.set_sensitive(True)
            if self._selected_host != host:
                return
            self.state_row.set_subtitle("Unavailable")
            self._report_error(exc, "Couldn't reach device")

        run_async(work, done, error)

    def _run_device_action(self, action: Any) -> None:
        """Run ``action(device)`` off the main loop, then refresh status from the same call.

        Bundling the follow-up ``status()`` into the same worker call (rather
        than a second ``run_async`` round trip) is what satisfies "refresh
        after each command completes" without hammering the device with an
        extra request per click.
        """
        host = self._selected_host
        if host is None:
            return
        device = self._get_device(host)
        self._set_controls_sensitive(False)
        self.state_row.set_subtitle("Contacting device…")

        def work() -> Any:
            action(device)
            return device.status()

        def done(status: Any) -> None:
            if self._selected_host != host:
                return
            self._apply_status(status)

        def error(exc: BaseException) -> None:
            if self._selected_host == host:
                self._set_controls_sensitive(True)
                self.state_row.set_subtitle("Unavailable")
            self._report_error(exc, "Command failed")

        run_async(work, done, error)

    def _report_error(self, exc: BaseException, prefix: str) -> None:
        if isinstance(exc, NotSupportedError):
            self.state.toast(f"{prefix}: this device doesn't support that ({exc})")
        elif isinstance(exc, ProviderError):
            self.state.toast(f"{prefix}: {exc}")
        else:
            log.exception("Unexpected device error")
            self.state.toast(f"{prefix}: {exc}")

    def _on_play_pause_clicked(self, _button: Gtk.Button) -> None:
        status = self._last_status
        if status is not None and status.state == "playing":
            self._run_device_action(lambda d: d.pause())
        else:
            self._run_device_action(lambda d: d.resume())

    def _on_volume_changed(self, _scale: Gtk.Scale) -> None:
        if self._updating_from_status:
            return
        if self._volume_debounce_id is not None:
            GLib.source_remove(self._volume_debounce_id)
        self._volume_debounce_id = GLib.timeout_add(_VOLUME_DEBOUNCE_MS, self._on_volume_debounce_elapsed)

    def _on_volume_debounce_elapsed(self) -> bool:
        self._volume_debounce_id = None
        level = int(self.volume_scale.get_value())
        self._run_device_action(lambda d, lvl=level: d.set_volume(lvl))
        return GLib.SOURCE_REMOVE

    def _on_mute_toggled(self, switch: Gtk.Switch, _pspec: object) -> None:
        if self._updating_from_status:
            return
        muted = switch.get_active()
        self._run_device_action(lambda d, m=muted: d.set_muted(m))
