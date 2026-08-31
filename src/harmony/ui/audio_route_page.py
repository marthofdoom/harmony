"""Route a network audio source into a local output device.

Receives a low-latency network audio stream from another machine on the LAN
(e.g. a Steam Deck) and plays it out a chosen local sink/DAC, via
``harmony.audio``. Prefers **ROC** (forward error correction + adaptive latency,
via the bundled ``roc-recv``) and falls back to plain **RTP** (a PipeWire module)
when roc-recv isn't present. The page shows the matching command to run on the
sending machine. All device / process work runs off the main loop.
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
from harmony.ui.widgets import SegmentedToggle  # noqa: E402

log = logging.getLogger(__name__)

# Must match harmony.audio.pipewire's ROC endpoint defaults.
_ROC_PORTS = (10001, 10002, 10003)


def _local_ip() -> str:
    """This machine's LAN IP (best effort) so the sender knows where to send."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        finally:
            sock.close()
    except OSError:
        return "<this-computer-ip>"


class AudioRoutePage(Gtk.Box):
    """Pick an output device and start/stop a ROC (or RTP) network-audio receiver."""

    def __init__(self, state: AppState) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                         margin_top=16, margin_bottom=16, margin_start=16, margin_end=16)
        self.state = state
        self._sinks: list = []
        self._receiver = None

        from harmony.audio import roc_available
        self._roc = roc_available()
        transport = "ROC (FEC, low-latency)" if self._roc else "RTP"

        heading = Gtk.Label(xalign=0.0, label="Route Audio")
        heading.add_css_class("title-2")
        self.append(heading)
        self.append(Gtk.Label(
            xalign=0.0, wrap=True,
            label=f"Route audio between machines ({transport}). Play another Harmony "
                  "instance's audio here, or send this machine's output to one.",
        ))

        # -- route with another Harmony instance --------------------------------
        self._peers: list = []
        mesh = Adw.PreferencesGroup(
            title="Route with another Harmony instance",
            description="Discovered on your network. Both instances need the same personal key.",
        )
        self.peer_row = Adw.ComboRow(title="Instance")
        mesh.add(self.peer_row)
        preset_row = Adw.ActionRow(
            title="Latency profile",
            subtitle="Music is rock-solid; Gaming trades a little stability for tightness",
        )
        self._preset = SegmentedToggle([("music", "Music"), ("gaming", "Gaming")], active="music")
        self._preset.set_valign(Gtk.Align.CENTER)
        self._preset.connect("changed", lambda *_a: self._on_preset_changed())
        preset_row.add_suffix(self._preset)
        mesh.add(preset_row)
        mesh_buttons = Gtk.Box(spacing=6, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        self.recv_peer_button = Gtk.Button(label="Receive their audio", sensitive=False)
        self.recv_peer_button.connect("clicked", lambda *_a: self._on_route("receive"))
        self.send_peer_button = Gtk.Button(label="Send my audio to them",
                                           css_classes=["suggested-action"], sensitive=False)
        self.send_peer_button.connect("clicked", lambda *_a: self._on_route("send"))
        self.stop_route_button = Gtk.Button(label="Stop routing")
        self.stop_route_button.connect("clicked", lambda *_a: self._on_stop_routing())
        self.refresh_peers_button = Gtk.Button(label="Refresh")
        self.refresh_peers_button.connect("clicked", lambda *_a: self._load_peers())
        for button in (self.recv_peer_button, self.send_peer_button,
                       self.stop_route_button, self.refresh_peers_button):
            mesh_buttons.append(button)
        buttons_row = Adw.ActionRow()
        buttons_row.set_child(mesh_buttons)
        mesh.add(buttons_row)
        self.route_status = Gtk.Label(xalign=0.0, wrap=True, margin_start=6, margin_end=6, margin_bottom=6)
        status_row = Adw.ActionRow()
        status_row.set_child(self.route_status)
        mesh.add(status_row)
        self.append(mesh)

        group = Adw.PreferencesGroup(title="Receiver (from a non-Harmony sender)")
        self.sink_row = Adw.ComboRow(title="Output device")
        group.add(self.sink_row)
        self.latency_row = Adw.SpinRow.new_with_range(20, 500, 5)
        self.latency_row.set_title("Target latency (ms)")
        self.latency_row.set_subtitle("Higher is more stable over Wi-Fi; lower is tighter")
        self.latency_row.set_value(150 if self._roc else 40)  # Music preset default
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

        note = ("Run this on the sender to broadcast its audio, then Start here. "
                "The Steam Deck needs roc-toolkit installed for ROC."
                if self._roc else
                "Run this on the sender to broadcast its audio, then Start here.")
        sender = Adw.PreferencesGroup(title="On the sending machine (e.g. Steam Deck)",
                                      description=note)
        self.sender_label = Gtk.Label(xalign=0.0, wrap=True, selectable=True,
                                      label=self._sender_command())
        self.sender_label.add_css_class("monospace")
        row = Adw.ActionRow()
        row.set_child(self.sender_label)
        sender.add(row)
        self.append(sender)

        self._load_sinks()
        self._load_peers()

    # -- route with another Harmony instance --------------------------------

    def _on_preset_changed(self) -> None:
        """Music vs Gaming preset writes a sensible latency into the adjuster."""
        self.latency_row.set_value(150 if self._preset.get_active_name() == "music" else 40)

    def _load_peers(self) -> None:
        self.refresh_peers_button.set_sensitive(False)

        def work() -> list:
            from harmony.web.server import get_engine

            return get_engine().instances().get("instances", [])

        def done(peers: list) -> None:
            self.refresh_peers_button.set_sensitive(True)
            self._peers = peers
            names = [f"{p.get('name')} ({p.get('host')})" for p in peers] or ["No instances found"]
            self.peer_row.set_model(Gtk.StringList.new(names))
            self.recv_peer_button.set_sensitive(bool(peers))
            self.send_peer_button.set_sensitive(bool(peers))

        def error(exc: BaseException) -> None:
            self.refresh_peers_button.set_sensitive(True)
            self.state.toast(f"Couldn't list instances: {exc}")

        run_async(work, done, error)

    def _on_route(self, direction: str) -> None:
        index = self.peer_row.get_selected()
        if not (0 <= index < len(self._peers)):
            self.state.toast("Pick an instance first.")
            return
        peer = self._peers[index]
        host, port = peer.get("host"), peer.get("port")
        if not host or not port:
            self.state.toast("That instance hasn't resolved an address yet.")
            return
        latency = int(self.latency_row.get_value())
        sink = None
        if direction == "receive":  # play the incoming stream into the chosen local sink
            i = self.sink_row.get_selected()
            if 0 <= i < len(self._sinks):
                sink = self._sinks[i].name
        verb = "Receiving from" if direction == "receive" else "Sending to"
        self.route_status.set_label(f"Setting up… ({verb.lower()} {peer.get('name')})")

        def work() -> object:
            from harmony.web.server import get_engine

            return get_engine().audio_route(direction, host, int(port), sink=sink, latency_ms=latency)

        def done(_result: object) -> None:
            self.route_status.set_label(f"{verb} {peer.get('name')}. Stop when you're done.")

        def error(exc: BaseException) -> None:
            self.route_status.set_label(f"Couldn't route: {exc}")

        run_async(work, done, error)

    def _on_stop_routing(self) -> None:
        def work() -> object:
            from harmony.web.server import get_engine

            return get_engine().audio_stop()

        def done(_result: object) -> None:
            self.route_status.set_label("Routing stopped.")

        def error(exc: BaseException) -> None:
            self.state.toast(f"Couldn't stop routing: {exc}")

        run_async(work, done, error)

    def _sender_command(self, ip: str | None = None) -> str:
        """The command to run on the sender, for the active transport."""
        if not self._roc:
            return "pactl load-module module-rtp-send source=@DEFAULT_SINK@.monitor"
        host = ip or "<this-computer-ip>"
        src, rpr, ctl = _ROC_PORTS
        return (
            f"roc-send -i pulse://$(pactl get-default-sink).monitor "
            f"-s rtp+rs8m://{host}:{src} -r rs8m://{host}:{rpr} -c rtcp://{host}:{ctl}"
        )

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

    def _on_start(self, _button: Gtk.Button) -> None:
        index = self.sink_row.get_selected()
        if not (0 <= index < len(self._sinks)):
            self.state.toast("Pick an output device first.")
            return
        sink = self._sinks[index]
        latency = int(self.latency_row.get_value())
        use_roc = self._roc
        self.start_button.set_sensitive(False)
        kind = "ROC" if use_roc else "RTP"
        self.status_label.set_label(f"Starting {kind} receiver → {sink.description}…")

        def work():  # noqa: ANN202 - receiver handle
            if use_roc:
                from harmony.audio import roc_receiver_up

                return roc_receiver_up(sink.name, target_latency_ms=latency)
            from harmony.audio import rtp_receiver_up

            return rtp_receiver_up(sink.name, latency_ms=latency)

        def done(receiver: object) -> None:
            self._receiver = receiver
            self.stop_button.set_sensitive(True)
            self.sender_label.set_label(self._sender_command(_local_ip()))
            msg = f"Receiving {kind} → {sink.description}. Start sending on the other machine."
            if use_roc and getattr(receiver, "log_path", None):
                msg += f"\nIf audio breaks up, raise the target latency. Diagnostics: {receiver.log_path}"
            self.status_label.set_label(msg)

        def error(exc: BaseException) -> None:
            self.start_button.set_sensitive(True)
            self.status_label.set_label("")
            self.state.toast(f"Couldn't start the receiver: {exc}")

        run_async(work, done, error)

    def shutdown(self) -> None:
        """Tear down a running receiver synchronously (called on window close).

        A ROC receiver is a subprocess and an RTP receiver is a module loaded
        into the host's PipeWire; either would outlive the app otherwise, with
        no in-app way to stop it after reopening.
        """
        try:  # tear down any inter-instance route owned by the engine
            from harmony.web.server import get_engine

            get_engine().audio_stop()
        except Exception:  # noqa: BLE001 - best-effort shutdown cleanup
            log.debug("route shutdown failed", exc_info=True)

        receiver = self._receiver
        if receiver is None:
            return
        self._receiver = None
        try:
            if self._roc:
                from harmony.audio import roc_receiver_down

                roc_receiver_down(receiver)
            else:
                from harmony.audio import rtp_receiver_down

                rtp_receiver_down(receiver)
        except Exception:  # noqa: BLE001 - best-effort shutdown cleanup
            log.debug("receiver shutdown failed", exc_info=True)

    def _on_stop(self, _button: Gtk.Button) -> None:
        receiver = self._receiver
        if receiver is None:
            return
        use_roc = self._roc
        self.stop_button.set_sensitive(False)

        def work() -> None:
            if use_roc:
                from harmony.audio import roc_receiver_down

                roc_receiver_down(receiver)
                return
            from harmony.audio import rtp_receiver_down

            rtp_receiver_down(receiver)

        def done(_result: None) -> None:
            self._receiver = None
            self.start_button.set_sensitive(bool(self._sinks))
            self.status_label.set_label("Stopped.")

        def error(exc: BaseException) -> None:
            self.stop_button.set_sensitive(True)
            self.state.toast(f"Couldn't stop the receiver: {exc}")

        run_async(work, done, error)
