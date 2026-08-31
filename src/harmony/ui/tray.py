"""System-tray icon via the StatusNotifierItem (SNI) protocol, over DBus.

GTK4 has no tray support and the AppIndicator library's menu is GTK3 (which can't
coexist with GTK4 in one process), so this speaks the freedesktop/KDE
StatusNotifierItem + com.canonical.dbusmenu protocols directly with ``Gio``.

The host (a panel, or GNOME's AppIndicator extension) shows the icon; left-click
activates (shows the window), right-click shows a small menu with **Show
Harmony** and **Quit**. If no StatusNotifierWatcher is present, ``start()``
returns False and the caller keeps its fallback behaviour.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from gi.repository import Gio, GLib

log = logging.getLogger(__name__)

_ITEM_PATH = "/StatusNotifierItem"
_MENU_PATH = "/StatusNotifierItem/Menu"

_ITEM_XML = """
<node><interface name="org.kde.StatusNotifierItem">
  <property name="Category" type="s" access="read"/>
  <property name="Id" type="s" access="read"/>
  <property name="Title" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="IconName" type="s" access="read"/>
  <property name="ItemIsMenu" type="b" access="read"/>
  <property name="Menu" type="o" access="read"/>
  <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
  <signal name="NewIcon"/>
  <signal name="NewStatus"><arg type="s"/></signal>
</interface></node>
"""

_MENU_XML = """
<node><interface name="com.canonical.dbusmenu">
  <property name="Version" type="u" access="read"/>
  <property name="Status" type="s" access="read"/>
  <method name="GetLayout">
    <arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/>
    <arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/>
  </method>
  <method name="GetGroupProperties">
    <arg type="ai" direction="in"/><arg type="as" direction="in"/><arg type="a(ia{sv})" direction="out"/>
  </method>
  <method name="Event">
    <arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/><arg type="u" direction="in"/>
  </method>
  <method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
  <signal name="ItemsPropertiesUpdated"><arg type="a(ia{sv})"/><arg type="a(ias)"/></signal>
  <signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
</interface></node>
"""

# (id, label) for the flat right-click menu.
_MENU_ITEMS = [(1, "Show Harmony"), (2, "Quit")]


def _menu_item_variant(item_id: int, label: str) -> GLib.Variant:
    props = {"label": GLib.Variant("s", label),
             "enabled": GLib.Variant("b", True),
             "visible": GLib.Variant("b", True)}
    return GLib.Variant("(ia{sv}av)", (item_id, props, []))


class Tray:
    def __init__(self, icon_name: str, title: str,
                 on_activate: Callable[[], None], on_quit: Callable[[], None]) -> None:
        self._icon_name = icon_name
        self._title = title
        self._on_activate = on_activate
        self._on_quit = on_quit
        self._conn: Gio.DBusConnection | None = None
        self._reg_ids: list[int] = []

    def start(self) -> bool:
        try:
            self._conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            item_info = Gio.DBusNodeInfo.new_for_xml(_ITEM_XML).interfaces[0]
            menu_info = Gio.DBusNodeInfo.new_for_xml(_MENU_XML).interfaces[0]
            self._reg_ids.append(self._conn.register_object(
                _ITEM_PATH, item_info, self._item_method, self._item_get, None))
            self._reg_ids.append(self._conn.register_object(
                _MENU_PATH, menu_info, self._menu_method, self._menu_get, None))
            # Register with the watcher; if there's no tray host this fails and
            # we report unavailable so the caller keeps its fallback.
            self._conn.call_sync(
                "org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher",
                "org.kde.StatusNotifierWatcher", "RegisterStatusNotifierItem",
                GLib.Variant("(s)", (self._conn.get_unique_name(),)),
                None, Gio.DBusCallFlags.NONE, 2000, None)
            log.info("tray: registered StatusNotifierItem")
            return True
        except GLib.Error as exc:
            log.info("tray: no StatusNotifierWatcher (%s); tray disabled", exc.message)
            self.stop()
            return False

    def stop(self) -> None:
        if self._conn is not None:
            for rid in self._reg_ids:
                try:
                    self._conn.unregister_object(rid)
                except Exception:  # noqa: BLE001
                    pass
        self._reg_ids = []

    # -- StatusNotifierItem -------------------------------------------------

    def _item_get(self, _conn, _sender, _path, _iface, prop, _err_unused):
        return {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", "harmony"),
            "Title": GLib.Variant("s", self._title),
            "Status": GLib.Variant("s", "Active"),
            "IconName": GLib.Variant("s", self._icon_name),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", _MENU_PATH),
        }.get(prop)

    def _item_method(self, _conn, _sender, _path, _iface, method, _params, invocation):
        if method in ("Activate", "SecondaryActivate"):
            GLib.idle_add(self._on_activate)
        invocation.return_value(None)

    # -- com.canonical.dbusmenu ---------------------------------------------

    def _menu_get(self, _conn, _sender, _path, _iface, prop, _err_unused):
        if prop == "Version":
            return GLib.Variant("u", 3)
        if prop == "Status":
            return GLib.Variant("s", "normal")
        return None

    def _menu_method(self, _conn, _sender, _path, _iface, method, params, invocation):
        if method == "GetLayout":
            children = [GLib.Variant("v", _menu_item_variant(i, label)) for i, label in _MENU_ITEMS]
            root = GLib.Variant("(ia{sv}av)", (0, {"children-display": GLib.Variant("s", "submenu")}, children))
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (1, root)))
        elif method == "GetGroupProperties":
            rows = [GLib.Variant("(ia{sv})", (i, {"label": GLib.Variant("s", label)}))
                    for i, label in _MENU_ITEMS]
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (rows,)))
        elif method == "Event":
            item_id, event_id = params.get_child_value(0).get_int32(), params.get_child_value(1).get_string()
            if event_id == "clicked":
                if item_id == 1:
                    GLib.idle_add(self._on_activate)
                elif item_id == 2:
                    GLib.idle_add(self._on_quit)
            invocation.return_value(None)
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        else:
            invocation.return_value(None)
