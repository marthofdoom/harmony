"""Embedded Qobuz sign-in via a WebKitGTK webview.

Qobuz accounts created through Google/social sign-in have no password the
reverse-engineered API accepts, so the only way in is a session token. This
hosts Qobuz's own login page in an in-app browser, lets the user sign in
however they normally do (Google included, since it is a real browser engine),
and lifts the resulting ``user_auth_token`` out of the page's own storage —
never another application's.

WebKitGTK is provided by the GNOME runtime, so this works out of the box in the
Flatpak. On a source checkout without ``webkitgtk6.0`` installed, ``AVAILABLE``
is False and the caller falls back to manual token paste; importing this module
never hard-fails.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

log = logging.getLogger(__name__)

LOGIN_URL = "https://play.qobuz.com/login"

# Try the modern WebKitGTK 6.0 binding. Absent on a bare source install; present
# in org.gnome.Platform (the Flatpak) and on systems with webkitgtk6.0.
try:
    gi.require_version("WebKit", "6.0")
    from gi.repository import WebKit  # noqa: E402

    AVAILABLE = True
except (ValueError, ImportError) as exc:  # pragma: no cover - depends on host
    WebKit = None  # type: ignore[assignment]
    AVAILABLE = False
    log.debug("WebKitGTK 6.0 not available; embedded Qobuz login disabled: %s", exc)


# JS that walks every localStorage entry, JSON-parses each value, and deep-scans
# for a ``user_auth_token``. The web player's storage keys are runtime-obfuscated
# (a computed prefix plus a transform), so keying off a fixed name is unreliable;
# the token value, however, is always stored inside a JSON blob. Returns the
# token string, or null when the user hasn't finished signing in yet.
_EXTRACT_TOKEN_JS = """
(function () {
  function deepFind(node, depth) {
    if (node == null || depth > 6) return null;
    if (typeof node !== 'object') return null;
    if (typeof node.user_auth_token === 'string' && node.user_auth_token.length > 20) {
      return node.user_auth_token;
    }
    for (var k in node) {
      var found = deepFind(node[k], depth + 1);
      if (found) return found;
    }
    return null;
  }
  try {
    for (var i = 0; i < localStorage.length; i++) {
      var raw = localStorage.getItem(localStorage.key(i));
      if (!raw || raw[0] !== '{' && raw[0] !== '[') continue;
      var parsed;
      try { parsed = JSON.parse(raw); } catch (e) { continue; }
      var token = deepFind(parsed, 0);
      if (token) return token;
    }
  } catch (e) {}
  return null;
})();
"""

_POLL_INTERVAL_MS = 1500


class QobuzLoginDialog(Adw.Window):
    """Modal in-app browser that captures a Qobuz session token on sign-in.

    ``on_token`` is called once with the captured token string, on the main
    loop, when sign-in succeeds. If the user closes the window first it is
    called with ``None``. It is called exactly once either way.
    """

    def __init__(self, parent: Gtk.Window, on_token: Callable[[str | None], None]) -> None:
        super().__init__(
            title="Sign in to Qobuz",
            modal=True,
            transient_for=parent,
            default_width=520,
            default_height=720,
        )
        self._on_token = on_token
        self._settled = False
        self._poll_id: int | None = None

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self._status = Gtk.Label(label="Sign in with your Qobuz account", xalign=0.0)
        header.set_title_widget(self._status)
        toolbar.add_top_bar(header)

        # A persistent session (rather than ephemeral) so Google's sign-in
        # behaves like a normal browser tab; it lives in the app cache and holds
        # nothing we don't already extract.
        try:
            from harmony import config

            data_dir = str(config.cache_dir() / "qobuz-login")
            session = WebKit.NetworkSession.new(data_dir, data_dir)
            self._webview = WebKit.WebView(network_session=session)
        except Exception:  # noqa: BLE001 - fall back to the default session
            log.debug("Falling back to default WebKit session", exc_info=True)
            self._webview = WebKit.WebView()

        self._webview.set_vexpand(True)
        self._webview.connect("load-changed", self._on_load_changed)
        toolbar.set_content(self._webview)
        self.set_content(toolbar)

        self.connect("close-request", self._on_close_request)
        self._webview.load_uri(LOGIN_URL)
        # Poll regardless of navigation signals: the web player is a SPA, so a
        # successful login may not emit a load event we can hook.
        self._poll_id = GLib.timeout_add(_POLL_INTERVAL_MS, self._poll_for_token)

    # -- token capture ---------------------------------------------------

    def _on_load_changed(self, _webview: object, event: object) -> None:
        if WebKit is not None and event == WebKit.LoadEvent.FINISHED:
            self._try_extract()

    def _poll_for_token(self) -> bool:
        if self._settled:
            return GLib.SOURCE_REMOVE
        self._try_extract()
        return GLib.SOURCE_CONTINUE

    def _try_extract(self) -> None:
        if self._settled:
            return
        try:
            self._webview.evaluate_javascript(
                _EXTRACT_TOKEN_JS, -1, None, None, None, self._on_js_result, None
            )
        except Exception:  # noqa: BLE001 - webview may not be ready yet
            log.debug("evaluate_javascript failed (page not ready?)", exc_info=True)

    def _on_js_result(self, webview: object, result: object, _data: object) -> None:
        if self._settled:
            return
        try:
            value = webview.evaluate_javascript_finish(result)
        except GLib.Error:
            return  # page navigated / not ready; the poll will retry
        token = None
        try:
            if value is not None and value.is_string():
                token = value.to_string()
        except Exception:  # noqa: BLE001 - defensive against binding shape
            log.debug("Unexpected JS result shape", exc_info=True)
        if token:
            self._settle(token)

    def _settle(self, token: str | None) -> None:
        if self._settled:
            return
        self._settled = True
        if self._poll_id is not None:
            GLib.source_remove(self._poll_id)
            self._poll_id = None
        # Deliver on the next loop turn so we can close cleanly first.
        GLib.idle_add(lambda: (self._on_token(token), False)[1])
        self.close()

    def _on_close_request(self, _window: object) -> bool:
        # A user-initiated close before capture is a cancellation.
        if not self._settled:
            self._settled = True
            if self._poll_id is not None:
                GLib.source_remove(self._poll_id)
                self._poll_id = None
            GLib.idle_add(lambda: (self._on_token(None), False)[1])
        return False  # allow the close to proceed


def present_login(parent: Gtk.Window, on_token: Callable[[str | None], None]) -> None:
    """Open the embedded login, or report unavailability via ``on_token(None)``."""
    if not AVAILABLE:
        log.warning("Embedded Qobuz login requested but WebKitGTK is unavailable")
        on_token(None)
        return
    QobuzLoginDialog(parent, on_token).present()
