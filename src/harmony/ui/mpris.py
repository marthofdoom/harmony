"""MPRIS (org.mpris.MediaPlayer2) for the desktop app.

Media keys, the GNOME/KDE shell media controls, the lock screen and
``playerctl`` all speak MPRIS. This exports the app-wide playback model
(``AppState.playback`` + the active queue) and routes their commands to the
same ``AppState.playback_*`` methods the Now Playing bar uses — so it works
for local playback and for a cast device alike. Main loop only.
"""

from __future__ import annotations

import logging
from typing import Any

from gi.repository import Gio, GLib

log = logging.getLogger(__name__)

_PATH = "/org/mpris/MediaPlayer2"
_XML = """
<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek"><arg direction="in" name="Offset" type="x"/></method>
    <method name="SetPosition">
      <arg direction="in" name="TrackId" type="o"/><arg direction="in" name="Position" type="x"/>
    </method>
    <method name="OpenUri"><arg direction="in" name="Uri" type="s"/></method>
    <signal name="Seeked"><arg name="Position" type="x"/></signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="LoopStatus" type="s" access="readwrite"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Shuffle" type="b" access="readwrite"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>
"""

_LOOP = {"off": "None", "all": "Playlist", "one": "Track"}
_LOOP_BACK = {v: k for k, v in _LOOP.items()}


class Mpris:
    def __init__(self, app: Any, state: Any, app_id: str, name: str) -> None:
        self.app = app
        self.state = state
        self.app_id = app_id
        self.name = name
        self._conn: Gio.DBusConnection | None = None
        self._reg: list[int] = []
        self._last: dict[str, Any] = {}
        self._owner = Gio.bus_own_name(
            Gio.BusType.SESSION, f"org.mpris.MediaPlayer2.{app_id}",
            Gio.BusNameOwnerFlags.NONE, self._on_bus, None, None)

    def _on_bus(self, conn: Gio.DBusConnection, _name: str) -> None:
        self._conn = conn
        info = Gio.DBusNodeInfo.new_for_xml(_XML)
        for iface in info.interfaces:
            self._reg.append(conn.register_object(_PATH, iface, self._on_call, self._on_get, self._on_set))
        self.state.connect("playback-changed", lambda *_a: self._emit_changes())
        self._emit_changes()

    def stop(self) -> None:
        if self._conn is not None:
            for rid in self._reg:
                self._conn.unregister_object(rid)
        Gio.bus_unown_name(self._owner)

    # -- values ---------------------------------------------------------------

    def _status(self) -> str:
        st = self.state.playback.state
        return "Playing" if st in ("playing", "loading") else "Paused" if st == "paused" else "Stopped"

    def _metadata(self) -> GLib.Variant:
        t = self.state.playback.track
        if t is None:
            return GLib.Variant("a{sv}", {"mpris:trackid": GLib.Variant("o", "/org/mpris/MediaPlayer2/TrackList/NoTrack")})
        tid = "".join(c if c.isalnum() else "_" for c in f"{t.service.value}_{t.id}")
        md: dict[str, GLib.Variant] = {
            "mpris:trackid": GLib.Variant("o", f"/io/github/marthofdoom/Harmony/track/{tid}"),
            "xesam:title": GLib.Variant("s", t.title or ""),
            "xesam:artist": GLib.Variant("as", list(t.artists or [])),
        }
        if t.album:
            md["xesam:album"] = GLib.Variant("s", t.album)
        dur = self.state.playback.duration_s or t.duration_s
        if dur:
            md["mpris:length"] = GLib.Variant("x", int(dur) * 1_000_000)
        if t.artwork_url:
            md["mpris:artUrl"] = GLib.Variant("s", t.artwork_url)
        return GLib.Variant("a{sv}", md)

    def _player_props(self) -> dict[str, GLib.Variant]:
        pb = self.state.playback
        has = pb.track is not None
        return {
            "PlaybackStatus": GLib.Variant("s", self._status()),
            "LoopStatus": GLib.Variant("s", _LOOP.get(pb.repeat, "None")),
            "Rate": GLib.Variant("d", 1.0),
            "Shuffle": GLib.Variant("b", bool(pb.shuffle)),
            "Metadata": self._metadata(),
            "Volume": GLib.Variant("d", (pb.volume if pb.volume is not None else 100) / 100.0),
            "Position": GLib.Variant("x", int(pb.position_s or 0) * 1_000_000),
            "MinimumRate": GLib.Variant("d", 1.0),
            "MaximumRate": GLib.Variant("d", 1.0),
            "CanGoNext": GLib.Variant("b", bool(pb.has_next)),
            "CanGoPrevious": GLib.Variant("b", has),
            "CanPlay": GLib.Variant("b", has),
            "CanPause": GLib.Variant("b", has),
            "CanSeek": GLib.Variant("b", bool(has and pb.duration_s)),
            "CanControl": GLib.Variant("b", True),
        }

    def _root_props(self) -> dict[str, GLib.Variant]:
        return {
            "CanQuit": GLib.Variant("b", True),
            "CanRaise": GLib.Variant("b", True),
            "HasTrackList": GLib.Variant("b", False),
            "Identity": GLib.Variant("s", self.name),
            "DesktopEntry": GLib.Variant("s", self.app_id),
            "SupportedUriSchemes": GLib.Variant("as", []),
            "SupportedMimeTypes": GLib.Variant("as", []),
        }

    # -- D-Bus handlers ---------------------------------------------------------

    def _on_get(self, _c, _s, _p, iface: str, prop: str) -> GLib.Variant | None:
        props = self._root_props() if iface == "org.mpris.MediaPlayer2" else self._player_props()
        return props.get(prop)

    def _on_set(self, _c, _s, _p, _iface: str, prop: str, value: GLib.Variant) -> bool:
        v = value.unpack()
        if prop == "Shuffle":
            self.state.playback_set_shuffle(bool(v))
        elif prop == "LoopStatus":
            self.state.playback_set_repeat(_LOOP_BACK.get(v, "off"))
        elif prop == "Volume":
            self.state.playback_set_volume(int(max(0.0, min(1.0, float(v))) * 100))
        return True

    def _on_call(self, _c, _s, _p, _iface: str, method: str, params: GLib.Variant,
                 invocation: Gio.DBusMethodInvocation) -> None:
        st = self.state
        pb = st.playback
        if method == "Raise":
            self.app.activate()
        elif method == "Quit":
            self.app.quit()
        elif method == "Next":
            st.playback_next()
        elif method == "Previous":
            st.playback_previous()
        elif method == "PlayPause":
            st.playback_toggle_pause()
        elif method == "Play":
            if pb.state != "playing":
                st.playback_toggle_pause()
        elif method == "Pause":
            if pb.state in ("playing", "loading"):
                st.playback_toggle_pause()
        elif method == "Stop":
            st.playback_stop()
        elif method == "Seek":
            (offset,) = params.unpack()
            st.playback_seek(max(0, int(pb.position_s or 0) + offset // 1_000_000))
        elif method == "SetPosition":
            _tid, pos = params.unpack()
            st.playback_seek(max(0, pos // 1_000_000))
        invocation.return_value(None)

    def _emit_changes(self) -> None:
        if self._conn is None:
            return
        props = self._player_props()
        props.pop("Position", None)  # position is polled by clients, never signalled
        changed = {k: v for k, v in props.items() if self._last.get(k) != v.print_(False)}
        if not changed:
            return
        self._last.update({k: v.print_(False) for k, v in changed.items()})
        try:
            self._conn.emit_signal(
                None, _PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                GLib.Variant("(sa{sv}as)", ("org.mpris.MediaPlayer2.Player", changed, [])))
        except GLib.Error as exc:
            log.debug("MPRIS signal failed: %s", exc)
