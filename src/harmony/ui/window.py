"""Top-level application window: sidebar navigation + a page stack."""

from __future__ import annotations

import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GObject, Gtk  # noqa: E402

from harmony.models import Service  # noqa: E402
from harmony.ui.state import AppState  # noqa: E402

log = logging.getLogger(__name__)

# (internal name, display title, symbolic icon)
_PAGES: list[tuple[str, str, str]] = [
    ("search", "Search", "system-search-symbolic"),
    ("playlists", "Playlists", "view-list-symbolic"),
    ("sync", "Sync", "emblem-synchronizing-symbolic"),
    ("discover", "Discover", "starred-symbolic"),
    ("devices", "Devices", "audio-speakers-symbolic"),
    ("audio_route", "Route Audio", "network-transmit-receive-symbolic"),
]
_TITLES = {name: title for name, title, _ in _PAGES}


class HarmonyWindow(Adw.ApplicationWindow):
    """Sidebar (nav list + account status) / content (view stack) split view."""

    def __init__(self, application: Adw.Application, state: AppState) -> None:
        super().__init__(application=application, title="Harmony")
        self.state = state

        self.set_default_size(state.settings.window_width, state.settings.window_height)
        if state.settings.window_maximized:
            self.maximize()

        self._nav_rows: dict[str, Gtk.ListBoxRow] = {}
        self._pages: dict[str, Gtk.Widget] = {}

        self.split_view = Adw.NavigationSplitView(min_sidebar_width=220, max_sidebar_width=320)
        self.split_view.set_sidebar(self._build_sidebar())
        self.split_view.set_content(self._build_content())
        self.split_view.set_vexpand(True)

        # The split view fills the window; the Now Playing bar spans the full
        # width beneath it (across every page).
        from harmony.ui.now_playing_bar import NowPlayingBar

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.append(self.split_view)
        self.now_playing_bar = NowPlayingBar(self.state)
        separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        self.now_playing_bar.bind_property(
            "visible", separator, "visible", GObject.BindingFlags.SYNC_CREATE
        )
        outer.append(separator)
        outer.append(self.now_playing_bar)

        self.toast_overlay = Adw.ToastOverlay(child=outer)
        self.set_content(self.toast_overlay)

        focus_action = Gio.SimpleAction.new("focus-search", None)
        focus_action.connect("activate", self._on_focus_search)
        self.add_action(focus_action)

        self.state.connect("toast", self._on_toast)
        self.state.connect("providers-changed", self._update_account_status)
        self.connect("close-request", self._on_close_request)

        self._update_account_status()
        self._activate_page(state.settings.last_page if state.settings.last_page in _TITLES else "search")

    # -- construction -------------------------------------------------------

    def _build_sidebar(self) -> Adw.NavigationPage:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Harmony"))
        toolbar.add_top_bar(header)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        nav_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        nav_list.add_css_class("navigation-sidebar")
        for name, title, icon in _PAGES:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL,
                spacing=12,
                margin_top=8,
                margin_bottom=8,
                margin_start=12,
                margin_end=12,
            )
            box.append(Gtk.Image.new_from_icon_name(icon))
            box.append(Gtk.Label(label=title, xalign=0.0))
            row.set_child(box)
            row.page_name = name  # type: ignore[attr-defined]
            nav_list.append(row)
            self._nav_rows[name] = row
        nav_list.connect("row-activated", self._on_row_activated)
        self._nav_list = nav_list
        outer.append(nav_list)

        outer.append(Gtk.Box(vexpand=True))  # spacer pushes the account row down
        outer.append(Gtk.Separator())

        self._account_row = Adw.ActionRow(
            title="Accounts", subtitle="Checking status…", activatable=True
        )
        self._account_row.add_prefix(Gtk.Image.new_from_icon_name("avatar-default-symbolic"))
        self._account_row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        # An ActionRow only emits "activated" when a GtkListBox activates it —
        # appended straight to a Box it is inert, which is what made this row a
        # dead button. Give it the ListBox it needs and handle row-activated.
        account_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        account_list.add_css_class("navigation-sidebar")
        account_list.append(self._account_row)
        account_list.connect("row-activated", lambda *_a: self._open_accounts_preferences())
        outer.append(account_list)

        toolbar.set_content(outer)
        return Adw.NavigationPage(title="Harmony", child=toolbar)

    def _open_accounts_preferences(self) -> None:
        app = self.get_application()
        if app is not None and hasattr(app, "open_preferences"):
            app.open_preferences("accounts")
        else:  # pragma: no cover - only if the window outlives its application
            self.activate_action("app.preferences", None)

    def _build_content(self) -> Adw.NavigationPage:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self._window_title = Adw.WindowTitle(title="Search")
        header.set_title_widget(self._window_title)

        # Without a primary menu the only ways to reach Preferences were
        # Ctrl+comma and an incidental banner on the Discover page.
        menu = Gio.Menu()
        menu.append("Preferences", "app.preferences")
        menu.append("About Harmony", "app.about")
        menu.append("Quit", "app.quit")
        menu_button = Gtk.MenuButton(
            icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Main menu"
        )
        header.pack_end(menu_button)
        toolbar.add_top_bar(header)

        self.stack = Adw.ViewStack()
        for name, title, _icon in _PAGES:
            widget = self._build_page(name)
            self._pages[name] = widget
            self.stack.add_titled(widget, name, title)
        toolbar.set_content(self.stack)

        self._content_page = Adw.NavigationPage(title="Search", child=toolbar)
        return self._content_page

    def _build_page(self, name: str) -> Gtk.Widget:
        """Construct a page widget, degrading to a status page if it blows up.

        Pages already degrade internally when a backend layer is missing;
        this is a second line of defence against an outright bug in a page
        module so one broken page never takes the whole window down.
        """
        try:
            if name == "search":
                from harmony.ui.search_page import SearchPage

                return SearchPage(self.state)
            if name == "playlists":
                from harmony.ui.playlists_page import PlaylistsPage

                return PlaylistsPage(self.state)
            if name == "sync":
                from harmony.ui.sync_page import SyncPage

                return SyncPage(self.state)
            if name == "discover":
                from harmony.ui.discover_page import DiscoverPage

                return DiscoverPage(self.state)
            if name == "devices":
                from harmony.ui.devices_page import DevicesPage

                return DevicesPage(self.state)
            if name == "audio_route":
                from harmony.ui.audio_route_page import AudioRoutePage

                return AudioRoutePage(self.state)
        except Exception:
            log.exception("Failed to build page %r", name)
        return Adw.StatusPage(
            icon_name="dialog-error-symbolic",
            title="This page failed to load",
            description="Check the application log for details.",
        )

    # -- navigation -----------------------------------------------------------

    def _on_row_activated(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        name = getattr(row, "page_name", None)
        if name:
            self._activate_page(name)

    def _activate_page(self, name: str) -> None:
        if name not in _TITLES:
            return
        self.stack.set_visible_child_name(name)
        self._window_title.set_title(_TITLES[name])
        self._content_page.set_title(_TITLES[name])
        row = self._nav_rows.get(name)
        if row is not None:
            self._nav_list.select_row(row)
        self.split_view.set_show_content(True)
        self.state.settings.last_page = name
        self.state.settings.save()

    def focus_search(self) -> None:
        self._activate_page("search")
        page = self._pages.get("search")
        focus = getattr(page, "focus_search_entry", None)
        if callable(focus):
            focus()

    def _on_focus_search(self, _action: Gio.SimpleAction, _param: None) -> None:
        self.focus_search()

    # -- state plumbing -----------------------------------------------------

    def _on_toast(self, _state: AppState, text: str) -> None:
        self.toast_overlay.add_toast(Adw.Toast(title=text, timeout=4))

    def _update_account_status(self, *_args: object) -> None:
        parts = []
        for service in Service:
            provider = self.state.providers.get(service)
            if provider is None:
                parts.append(f"{service.label}: not set up")
                continue
            try:
                connected = provider.is_authenticated
            except Exception:  # noqa: BLE001 - status probe must never crash the window
                connected = False
            parts.append(f"{service.label}: {'connected' if connected else 'not connected'}")
        self._account_row.set_subtitle(" · ".join(parts))

    def _on_close_request(self, *_args: object) -> bool:
        self.state.settings.window_width = self.get_width()
        self.state.settings.window_height = self.get_height()
        self.state.settings.window_maximized = self.is_maximized()
        self.state.settings.save()
        return False
