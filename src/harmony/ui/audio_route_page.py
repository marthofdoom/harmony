"""Route a network (ROC) audio source into a local output device.

Receives a low-latency ROC stream (e.g. a Steam Deck's audio, exposed with one
`module-roc-sink` command shown here) and plays it out a chosen local sink/DAC,
via ``harmony.audio``. All pactl work runs off the main loop per the threading
rule; the page only touches widgets in run_async callbacks.
"""

from __future__ import annotations

import logging
import socket

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from harmony.tasks import run_async  # noqa: E402
from harmony.ui.state import AppState  # noqa: E402

log = logging.getLogger(__name__)

_SOURCE_PORT = 10001
_REPAIR_PORT = 10002


def _local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "<this-machine-ip>"


class AudioRoutePage(Gtk.Box):
    """Pick an output device and start/stop a ROC network-audio receiver."""

    def __init__(self, state: AppState) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                         margin_top=16, margin_bottom=16, margin_start=16, margin_end=16)
        self.state = state
        self._sinks: list = []
        self._receiver = None

        heading = Gtk.Label(xalign=0.0, label="Route Audio")
        heading.add_css_class("title-2")
        self.append(heading)
        self.append(Gtk.Label(
            xalign=0.0, wrap=True,
            label="Receive a low-latency network audio stream (ROC) from another machine "
                  "and play it out a local output device.",
        ))

        group = Adw.PreferencesGroup(title="Receiver")
        self.sink_row = Adw.ComboRow(title="Output device")
        group.add(self.sink_row)
        self.latency_row = Adw.SpinRow.new_with_range(5, 200, 5)
        self.latency_row.set_title("Target latency (ms)")
        self.latency_row.set_value(20)
        group.add(self.latency_row)
        self.append(group)

        buttons = Gtk.Box(spacing=6)
        self.refresh_button = Gtk.Button(label="Refresh devices")
        self.refresh_button.connect("clicked", lambda *_a: self._load_sinks())
        self.start_button = Gtk.Button(label="Start", css_classes=["suggested-action"], sensitive=False)
        self.start_button.connect("clicked", self._on_start)
        self.stop_button = Gtk.Button(label="Stop", sensitive=False)
        self.stop_button.connect("clicked", self._on_stop)
        for button in (self.refresh_button, self.start_button, self.stop_button):
            buttons.append(button)
        self.append(buttons)

        self.status_label = Gtk.Label(xalign=0.0, wrap=True)
        self.append(self.status_label)

        sender = Adw.PreferencesGroup(
            title="On the sending machine (e.g. Steam Deck)",
            description="Run this, then choose its ROC output as the audio device:",
        )
        cmd = Gtk.Label(
            xalign=0.0, wrap=True, selectable=True,
            label=f"pactl load-module module-roc-sink remote_ip={_local_ip()} "
                  f"remote_source_port={_SOURCE_PORT} remote_repair_port={_REPAIR_PORT} "
                  f"sess_latency_msec=20",
        )
        cmd.add_css_class("monospace")
        row = Adw.ActionRow()
        row.set_child(cmd)
        sender.add(row)
        self.append(sender)

        self._load_sinks()

    # -- devices --------------------------------------------------------------

    def _load_sinks(self) -> None:
        self.refresh_button.set_sensitive(False)

        def work() -> list:
            from harmony.audio import list_sinks

            return list_sinks()

        def done(sinks: list) -> None:
            self.refresh_button.set_sensitive(True)
            self._sinks = sinks
            names = [s.description for s in sinks] or ["No output devices found"]
            self.sink_row.set_model(Gtk.StringList.new(names))
            self.start_button.set_sensitive(bool(sinks) and self._receiver is None)

        def error(exc: BaseException) -> None:
            self.refresh_button.set_sensitive(True)
            self.state.toast(f"Couldn't list output devices: {exc}")

        run_async(work, done, error)

    # -- start / stop ---------------------------------------------------------

    def _on_start(self, _button: Gtk.Button) -> None:
        index = self.sink_row.get_selected()
        if not (0 <= index < len(self._sinks)):
            self.state.toast("Pick an output device first.")
            return
        sink = self._sinks[index]
        latency = int(self.latency_row.get_value())
        self.start_button.set_sensitive(False)
        self.status_label.set_label(f"Starting receiver on port {_SOURCE_PORT} → {sink.description}…")

        def work():  # noqa: ANN202 - RocReceiver
            from harmony.audio import roc_receiver_up

            return roc_receiver_up(sink.name, source_port=_SOURCE_PORT,
                                   repair_port=_REPAIR_PORT, latency_ms=latency)

        def done(receiver: object) -> None:
            self._receiver = receiver
            self.stop_button.set_sensitive(True)
            self.status_label.set_label(
                f"Receiving on port {_SOURCE_PORT} → {sink.description}. Play audio on the sender."
            )

        def error(exc: BaseException) -> None:
            self.start_button.set_sensitive(True)
            self.status_label.set_label("")
            self.state.toast(f"Couldn't start the receiver: {exc}")

        run_async(work, done, error)

    def _on_stop(self, _button: Gtk.Button) -> None:
        receiver = self._receiver
        if receiver is None:
            return
        self.stop_button.set_sensitive(False)

        def work() -> None:
            from harmony.audio import roc_receiver_down

            roc_receiver_down(receiver)

        def done(_result: None) -> None:
            self._receiver = None
            self.start_button.set_sensitive(bool(self._sinks))
            self.status_label.set_label("Stopped.")

        def error(exc: BaseException) -> None:
            self.stop_button.set_sensitive(True)
            self.state.toast(f"Couldn't stop the receiver: {exc}")

        run_async(work, done, error)
