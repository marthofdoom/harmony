"""``Adw.Application`` subclass: actions, accelerators, and the single window."""

from __future__ import annotations

import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, Gtk  # noqa: E402

from harmony import APP_ID, APP_NAME, __version__  # noqa: E402
from harmony.ui.state import AppState  # noqa: E402
from harmony.ui.window import HarmonyWindow  # noqa: E402

log = logging.getLogger(__name__)


class HarmonyApplication(Adw.Application):
    """Owns the process-wide ``AppState`` and the (single) main window."""

    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.state: AppState | None = None
        self._window: HarmonyWindow | None = None

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        self.state = AppState()
        self._install_actions()

    def do_activate(self) -> None:
        if self._window is None:
            self._window = HarmonyWindow(self, self.state)
        self._window.present()

    def do_shutdown(self) -> None:
        from harmony import tasks

        tasks.shutdown(wait=False)
        Adw.Application.do_shutdown(self)

    # -- actions --------------------------------------------------------------

    def _install_actions(self) -> None:
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_a: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Control>q"])

        prefs_action = Gio.SimpleAction.new("preferences", None)
        prefs_action.connect("activate", self._on_preferences)
        self.add_action(prefs_action)
        self.set_accels_for_action("app.preferences", ["<Control>comma"])

        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", self._on_about)
        self.add_action(about_action)

        self.set_accels_for_action("win.focus-search", ["<Control>f"])

    def _on_preferences(self, _action: Gio.SimpleAction, _param: None) -> None:
        from harmony.ui.prefs import PreferencesDialog

        if self._window is None:
            return
        try:
            dialog = PreferencesDialog(self.state)
        except Exception:
            log.exception("Failed to open Preferences")
            self.state.toast("Couldn't open Preferences.")
            return
        dialog.present(self._window)

    def _on_about(self, _action: Gio.SimpleAction, _param: None) -> None:
        if self._window is None:
            return
        about = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon=APP_ID,
            version=__version__,
            developer_name="Harmony contributors",
            license_type=Gtk.License.GPL_3_0,
            comments="Create and sync playlists across YouTube Music and Qobuz, with search "
            "and recommendation tooling.",
            website="https://github.com/marthofdoom/harmony",
            issue_url="https://github.com/marthofdoom/harmony/issues",
        )
        about.present(self._window)


def build_application() -> HarmonyApplication:
    """Factory used by ``__main__`` (and tests) to construct the application."""
    return HarmonyApplication()
