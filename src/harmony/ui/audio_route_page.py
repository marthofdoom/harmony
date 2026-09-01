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

from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

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
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.state = state
        self._sinks: list = []
        self._peers: list = []
        self._receiver = None
        self._routing = False  # an inter-instance route is currently active

        from harmony.audio import roc_available
        self._roc = roc_available()
        transport = "ROC (with error correction, for low latency)" if self._roc else "RTP"

        scroller = Gtk.ScrolledWindow(vexpand=True)
        clamp = Adw.Clamp(maximum_size=720, margin_top=18, margin_bottom=18,
                          margin_start=12, margin_end=12)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        clamp.set_child(content)
        scroller.set_child(clamp)
        self.append(scroller)

        heading = Gtk.Label(xalign=0.0, label="Route Audio Between Machines")
        heading.add_css_class("title-1")
        content.append(heading)
        content.append(Gtk.Label(
            xalign=0.0, wrap=True, css_classes=["dim-label"],
            label=f"Play another Harmony computer's audio here, or send this computer's "
                  f"sound to one. Streaming uses {transport} over your local network.",
        ))

        # -- route with another Harmony instance --------------------------------
        mesh = Adw.PreferencesGroup(
            title="Another Harmony computer",
            description="Found on your network. Both computers need the same personal key.",
        )
        self.refresh_peers_button = Gtk.Button(
            icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER,
            tooltip_text="Refresh", css_classes=["flat"],
        )
        self.refresh_peers_button.connect("clicked", lambda *_a: self._load_peers())
        mesh.set_header_suffix(self.refresh_peers_button)

        self.peer_row = Adw.ComboRow(title="Computer")
        mesh.add(self.peer_row)
        preset_row = Adw.ActionRow(
            title="Latency profile",
            subtitle="Music is rock-solid; Gaming trades a little stability for tightness.",
        )
        self._preset = SegmentedToggle([("music", "Music"), ("gaming", "Gaming")], active="music")
        self._preset.set_valign(Gtk.Align.CENTER)
        self._preset.connect("changed", lambda *_a: self._on_preset_changed())
        preset_row.add_suffix(self._preset)
        mesh.add(preset_row)

        self.route_status_row = Adw.ActionRow(title="Status", subtitle="Not routing")
        self.route_spinner = Gtk.Spinner(valign=Gtk.Align.CENTER)
        self.route_status_row.add_suffix(self.route_spinner)
        mesh.add(self.route_status_row)
        content.append(mesh)

        mesh_buttons = Gtk.Box(spacing=6, halign=Gtk.Align.END)
        self.recv_peer_button = Gtk.Button(label="Receive Their Audio", sensitive=False)
        self.recv_peer_button.connect("clicked", lambda *_a: self._on_route("receive"))
        self.send_peer_button = Gtk.Button(label="Send My Audio", sensitive=False,
                                           css_classes=["suggested-action"])
        self.send_peer_button.connect("clicked", lambda *_a: self._on_route("send"))
        self.stop_route_button = Gtk.Button(label="Stop", sensitive=False)
        self.stop_route_button.connect("clicked", lambda *_a: self._on_stop_routing())
        for button in (self.stop_route_button, self.recv_peer_button, self.send_peer_button):
            mesh_buttons.append(button)
        content.append(mesh_buttons)

        # -- receiver from a non-Harmony sender ---------------------------------
        group = Adw.PreferencesGroup(title="Receive from another app")
        self.refresh_button = Gtk.Button(
            icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER,
            tooltip_text="Refresh devices", css_classes=["flat"],
        )
        self.refresh_button.connect("clicked", lambda *_a: self._load_sinks())
        group.set_header_suffix(self.refresh_button)

        self.sink_row = Adw.ComboRow(title="Output device")
        group.add(self.sink_row)
        self.latency_row = Adw.SpinRow.new_with_range(20, 500, 5)
        self.latency_row.set_title("Target latency (ms)")
        self.latency_row.set_subtitle("Higher is more stable over Wi-Fi; lower is tighter.")
        self.latency_row.set_value(150 if self._roc else 40)  # Music preset default
        group.add(self.latency_row)

        self.recv_status_row = Adw.ActionRow(title="Status", subtitle="Not receiving")
        self.recv_spinner = Gtk.Spinner(valign=Gtk.Align.CENTER)
        self.recv_status_row.add_suffix(self.recv_spinner)
        group.add(self.recv_status_row)
        content.append(group)

        recv_buttons = Gtk.Box(spacing=6, halign=Gtk.Align.END)
        self.stop_button = Gtk.Button(label="Stop", sensitive=False)
        self.stop_button.connect("clicked", self._on_stop)
        self.start_button = Gtk.Button(label="Start", css_classes=["suggested-action"], sensitive=False)
        self.start_button.connect("clicked", self._on_start)
        recv_buttons.append(self.stop_button)
        recv_buttons.append(self.start_button)
        content.append(recv_buttons)

        # -- the command to run on the sending machine --------------------------
        note = ("Run this on the sender to broadcast its audio, then press Start above. "
                "The Steam Deck needs roc-toolkit installed for ROC."
                if self._roc else
                "Run this on the sender to broadcast its audio, then press Start above.")
        sender = Adw.PreferencesGroup(title="On the sending machine (e.g. Steam Deck)",
                                      description=note)
        self._sender_cmd = self._sender_command()
        self.sender_row = Adw.ActionRow(
            title=self._markup_command(self._sender_cmd),
            title_selectable=True,
        )
        copy_button = Gtk.Button(
            icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER,
            tooltip_text="Copy command", css_classes=["flat"],
        )
        copy_button.connect("clicked", lambda *_a: self._copy_sender_command())
        self.sender_row.add_suffix(copy_button)
        sender.add(self.sender_row)
        content.append(sender)

        self._load_sinks()
        self._load_peers()

    # -- route with another Harmony instance --------------------------------

    def _on_preset_changed(self) -> None:
        """Music vs Gaming preset writes a sensible latency into the adjuster."""
        self.latency_row.set_value(150 if self._preset.get_active_name() == "music" else 40)

    def _update_mesh_buttons(self) -> None:
        """Send/Receive need a peer and no active route; Stop needs an active route."""
        can_start = bool(self._peers) and not self._routing
        self.recv_peer_button.set_sensitive(can_start)
        self.send_peer_button.set_sensitive(can_start)
        self.stop_route_button.set_sensitive(self._routing)

    def _load_peers(self) -> None:
        self.refresh_peers_button.set_sensitive(False)

        def work() -> list:
            from harmony.web.server import get_engine

            return get_engine().instances().get("instances", [])

        def done(peers: list) -> None:
            self.refresh_peers_button.set_sensitive(True)
            self._peers = peers
            if peers:
                self.peer_row.set_model(
                    Gtk.StringList.new([f"{p.get('name')} ({p.get('host')})" for p in peers])
                )
                self.peer_row.set_sensitive(True)
                self.peer_row.set_subtitle("")
            else:
                self.peer_row.set_model(Gtk.StringList.new([]))
                self.peer_row.set_sensitive(False)
                self.peer_row.set_subtitle("No other Harmony computers found on your network.")
            self._update_mesh_buttons()

        def error(exc: BaseException) -> None:
            self.refresh_peers_button.set_sensitive(True)
            log.exception("Couldn't list instances")
            self.state.toast("Couldn't look for other Harmony computers.")

        run_async(work, done, error)

    def _on_route(self, direction: str) -> None:
        index = self.peer_row.get_selected()
        if not (0 <= index < len(self._peers)):
            self.state.toast("Pick a computer first.")
            return
        peer = self._peers[index]
        host, port = peer.get("host"), peer.get("port")
        if not host or not port:
            self.state.toast("That computer hasn't resolved an address yet.")
            return
        latency = int(self.latency_row.get_value())
        sink = None
        if direction == "receive":  # play the incoming stream into the chosen local sink
            i = self.sink_row.get_selected()
            if 0 <= i < len(self._sinks):
                sink = self._sinks[i].name
        verb = "Receiving from" if direction == "receive" else "Sending to"
        self.route_status_row.set_subtitle(f"Setting up… ({verb.lower()} {peer.get('name')})")
        self.route_spinner.set_spinning(True)
        self.recv_peer_button.set_sensitive(False)
        self.send_peer_button.set_sensitive(False)

        def work() -> object:
            from harmony.web.server import get_engine

            return get_engine().audio_route(direction, host, int(port), sink=sink, latency_ms=latency)

        def done(_result: object) -> None:
            self.route_spinner.set_spinning(False)
            self._routing = True
            self.route_status_row.set_subtitle(f"{verb} {peer.get('name')}. Stop when you're done.")
            self._update_mesh_buttons()

        def error(exc: BaseException) -> None:
            self.route_spinner.set_spinning(False)
            self.route_status_row.set_subtitle("Not routing")
            self._update_mesh_buttons()
            log.exception("Couldn't route audio")
            self.state.toast("Couldn't route audio to that computer.")

        run_async(work, done, error)

    def _on_stop_routing(self) -> None:
        self.route_spinner.set_spinning(True)

        def work() -> object:
            from harmony.web.server import get_engine

            return get_engine().audio_stop()

        def done(_result: object) -> None:
            self.route_spinner.set_spinning(False)
            self._routing = False
            self.route_status_row.set_subtitle("Not routing")
            self._update_mesh_buttons()

        def error(exc: BaseException) -> None:
            self.route_spinner.set_spinning(False)
            log.exception("Couldn't stop routing")
            self.state.toast("Couldn't stop routing.")

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

    def _markup_command(self, command: str) -> str:
        """Monospace Pango markup for a shell command shown in a row title."""
        return f"<tt>{GLib.markup_escape_text(command)}</tt>"

    def _set_sender_command(self, command: str) -> None:
        self._sender_cmd = command
        self.sender_row.set_title(self._markup_command(command))

    def _copy_sender_command(self) -> None:
        display = Gdk.Display.get_default()
        if display is not None:
            display.get_clipboard().set(self._sender_cmd)
            self.state.toast("Command copied.")

    def _load_sinks(self) -> None:
        self.refresh_button.set_sensitive(False)

        def work() -> list:
            from harmony.audio import list_sinks

            return list_sinks()

        def done(sinks: list) -> None:
            self.refresh_button.set_sensitive(True)
            self._sinks = sinks
            if sinks:
                self.sink_row.set_model(Gtk.StringList.new([s.description for s in sinks]))
                self.sink_row.set_sensitive(True)
                self.sink_row.set_subtitle("")
            else:
                self.sink_row.set_model(Gtk.StringList.new([]))
                self.sink_row.set_sensitive(False)
                self.sink_row.set_subtitle("No output devices found.")
            self.start_button.set_sensitive(bool(sinks) and self._receiver is None)

        def error(exc: BaseException) -> None:
            self.refresh_button.set_sensitive(True)
            log.exception("Couldn't list output devices")
            self.state.toast("Couldn't list output devices.")

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
        self.recv_status_row.set_subtitle(f"Starting {kind} receiver → {sink.description}…")
        self.recv_spinner.set_spinning(True)

        def work():  # noqa: ANN202 - receiver handle
            if use_roc:
                from harmony.audio import roc_receiver_up

                return roc_receiver_up(sink.name, target_latency_ms=latency)
            from harmony.audio import rtp_receiver_up

            return rtp_receiver_up(sink.name, latency_ms=latency)

        def done(receiver: object) -> None:
            self.recv_spinner.set_spinning(False)
            self._receiver = receiver
            self.stop_button.set_sensitive(True)
            self._set_sender_command(self._sender_command(_local_ip()))
            msg = f"Receiving {kind} → {sink.description}. Start sending on the other machine."
            if use_roc and getattr(receiver, "log_path", None):
                msg += f" If audio breaks up, raise the target latency (log: {receiver.log_path})."
            self.recv_status_row.set_subtitle(msg)

        def error(exc: BaseException) -> None:
            self.recv_spinner.set_spinning(False)
            self.start_button.set_sensitive(True)
            self.recv_status_row.set_subtitle("Not receiving")
            log.exception("Couldn't start the receiver")
            self.state.toast("Couldn't start the receiver.")

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
        self.recv_spinner.set_spinning(True)

        def work() -> None:
            if use_roc:
                from harmony.audio import roc_receiver_down

                roc_receiver_down(receiver)
                return
            from harmony.audio import rtp_receiver_down

            rtp_receiver_down(receiver)

        def done(_result: None) -> None:
            self.recv_spinner.set_spinning(False)
            self._receiver = None
            self.start_button.set_sensitive(bool(self._sinks))
            self.recv_status_row.set_subtitle("Not receiving")

        def error(exc: BaseException) -> None:
            self.recv_spinner.set_spinning(False)
            self.stop_button.set_sensitive(True)
            log.exception("Couldn't stop the receiver")
            self.state.toast("Couldn't stop the receiver.")

        run_async(work, done, error)
