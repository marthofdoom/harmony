"""YouTube Music sign-in via the OAuth device-code flow.

Unlike Qobuz (no OAuth, so we grab a token from the page), YouTube Music has a
proper device-code flow: the app shows a short code, the user opens a Google URL
in their **real** browser, approves, and the app polls for the token. Google
sign-in works because it happens in the user's own browser — the thing an
embedded webview is forbidden to do.

Google requires the caller to supply its own OAuth client (a "TV and Limited
Input" client id/secret from a Google Cloud project with the YouTube Data API
enabled) — the shared client was retired. That id/secret is configured in
Preferences; this module drives the flow with it.

The flow logic (`run_device_flow`) is GTK-free and unit-testable; the dialog is
a thin wrapper over it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from harmony import tasks  # noqa: E402
from harmony.errors import AuthError  # noqa: E402

log = logging.getLogger(__name__)

# Google returns these while the user hasn't approved yet — keep polling.
_PENDING_ERRORS = {"authorization_pending", "slow_down"}


@dataclass(slots=True)
class DeviceCode:
    """What the user needs to see: a code to type at a URL."""

    user_code: str
    verification_url: str
    expires_in: int
    interval: int

    @property
    def full_url(self) -> str:
        # Prefilled URL so the user can skip typing the code where Google allows.
        return f"{self.verification_url}?user_code={self.user_code}"


def run_device_flow(
    client_id: str,
    client_secret: str,
    oauth_path: str,
    *,
    on_code: Callable[[DeviceCode], None],
    cancel: tasks.CancelToken | None = None,
) -> str | None:
    """Drive the device-code flow to completion and write the oauth token file.

    Blocking — must run on a worker thread. Calls ``on_code`` once, as soon as
    the code is issued, so the caller can show it and open the browser; then
    polls until the user approves. Returns the account's display name (or None
    if unavailable) on success. Raises ``AuthError`` on any terminal failure.
    """
    try:
        from ytmusicapi.auth.oauth import OAuthCredentials
        from ytmusicapi.auth.oauth.token import RefreshingToken
    except ImportError as exc:  # pragma: no cover - ytmusicapi is a hard dep
        raise AuthError("ytmusicapi is not available.") from exc

    if not client_id or not client_secret:
        raise AuthError(
            "YouTube Music needs an OAuth client ID and secret. Create a "
            "'TV and Limited Input' OAuth client in a Google Cloud project with "
            "the YouTube Data API enabled, then enter them in Preferences."
        )

    credentials = OAuthCredentials(client_id=client_id, client_secret=client_secret)

    try:
        raw_code = credentials.get_code()
    except Exception as exc:  # noqa: BLE001 - surfaced as a clear auth error
        raise AuthError(f"Could not start Google sign-in: {exc}") from exc

    code = DeviceCode(
        user_code=raw_code["user_code"],
        verification_url=raw_code["verification_url"],
        expires_in=int(raw_code.get("expires_in", 300)),
        interval=max(int(raw_code.get("interval", 5)), 1),
    )
    on_code(code)

    device_code = raw_code["device_code"]
    deadline = time.monotonic() + code.expires_in
    while time.monotonic() < deadline:
        if cancel is not None:
            cancel.raise_if_cancelled()
        time.sleep(code.interval)
        try:
            raw_token = credentials.token_from_code(device_code)
        except AuthError:
            raise
        except Exception as exc:  # noqa: BLE001 - misconfigured client etc.
            # _send_request raises BadOAuthClient / UnauthorizedOAuthClient on
            # 401 — a real, terminal configuration problem worth naming.
            raise AuthError(f"Google rejected the OAuth client: {exc}") from exc

        if "access_token" in raw_token:
            return _store_token(RefreshingToken, credentials, raw_token, oauth_path)

        error = raw_token.get("error")
        if error in _PENDING_ERRORS:
            continue
        raise AuthError(f"Google sign-in failed: {error or 'unknown error'}")

    raise AuthError("The sign-in code expired before it was approved. Try again.")


def _store_token(refreshing_token_cls, credentials, raw_token, oauth_path: str) -> str | None:
    """Build the RefreshingToken from the exchange result and persist it."""
    refresh_expires_in = raw_token.get("refresh_token_expires_in", raw_token["expires_in"])
    token = refreshing_token_cls(
        credentials=credentials,
        access_token=raw_token["access_token"],
        refresh_token=raw_token["refresh_token"],
        scope=raw_token["scope"],
        token_type=raw_token["token_type"],
        expires_in=refresh_expires_in,
    )
    token.update(raw_token)
    token.store_token(oauth_path)
    return None


class YTMusicLoginDialog(Adw.Window):
    """Shows the device code, opens the browser, and waits for approval.

    ``on_done(True)`` fires on success (token written to ``oauth_path``),
    ``on_done(False)`` on cancel or failure. Called exactly once.
    """

    def __init__(
        self,
        parent: Gtk.Window,
        *,
        client_id: str,
        client_secret: str,
        oauth_path: str,
        on_done: Callable[[bool], None],
    ) -> None:
        super().__init__(
            title="Sign in to YouTube Music",
            modal=True,
            transient_for=parent,
            default_width=460,
            default_height=340,
        )
        self._on_done = on_done
        self._settled = False
        self._cancel = tasks.CancelToken()
        self._full_url: str | None = None

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())

        self._box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12,
            margin_top=24, margin_bottom=24, margin_start=24, margin_end=24,
            valign=Gtk.Align.CENTER,
        )
        self._spinner = Gtk.Spinner(spinning=True, width_request=32, height_request=32)
        self._box.append(self._spinner)
        self._status = Gtk.Label(label="Starting Google sign-in…", wrap=True, justify=Gtk.Justification.CENTER)
        self._box.append(self._status)
        toolbar.set_content(self._box)
        self.set_content(toolbar)

        self.connect("close-request", self._on_close_request)

        tasks.run_async(
            lambda: run_device_flow(
                client_id, client_secret, oauth_path,
                on_code=lambda c: tasks.on_main(self._present_code, c),
                cancel=self._cancel,
            ),
            on_done=lambda _r: self._settle(True),
            on_error=self._on_error,
        )

    def _present_code(self, code: DeviceCode) -> None:
        if self._settled:
            return
        self._full_url = code.full_url
        self._spinner.stop()
        self._spinner.set_visible(False)
        while child := self._box.get_first_child():
            self._box.remove(child)

        self._box.append(Gtk.Label(label="In your browser, sign in and enter this code:",
                                   wrap=True, justify=Gtk.Justification.CENTER))
        code_label = Gtk.Label(label=code.user_code, selectable=True,
                               css_classes=["title-1"])
        self._box.append(code_label)
        open_button = Gtk.Button(label="Open Google in Browser",
                                 css_classes=["suggested-action"], halign=Gtk.Align.CENTER)
        open_button.connect("clicked", self._open_browser)
        self._box.append(open_button)
        self._box.append(Gtk.Label(label=code.verification_url, selectable=True,
                                   css_classes=["dim-label", "caption"]))
        waiting = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        waiting.append(Gtk.Spinner(spinning=True))
        waiting.append(Gtk.Label(label="Waiting for approval…"))
        self._box.append(waiting)
        # Nudge the browser open immediately; the button is there if it doesn't.
        self._open_browser(None)

    def _open_browser(self, _button: Gtk.Button | None) -> None:
        if not self._full_url:
            return
        try:
            launcher = Gtk.UriLauncher.new(self._full_url)
            launcher.launch(self, None, None)
        except Exception:  # noqa: BLE001 - non-fatal; the URL is shown to copy
            log.debug("Could not open the browser automatically", exc_info=True)

    def _on_error(self, exc: BaseException) -> None:
        if self._settled:
            return
        self._spinner.stop()
        self._spinner.set_visible(False)
        while child := self._box.get_first_child():
            self._box.remove(child)
        self._box.append(Gtk.Image.new_from_icon_name("dialog-error-symbolic"))
        self._box.append(Gtk.Label(label=str(exc), wrap=True, justify=Gtk.Justification.CENTER))
        close = Gtk.Button(label="Close", halign=Gtk.Align.CENTER)
        close.connect("clicked", lambda *_a: self._settle(False))
        self._box.append(close)
        # Don't mark settled yet — let the user read it and close. But the flow
        # is over, so cancel any lingering worker.
        self._cancel.cancel()

    def _settle(self, ok: bool) -> None:
        if self._settled:
            return
        self._settled = True
        self._cancel.cancel()
        GLib.idle_add(lambda: (self._on_done(ok), False)[1])
        self.close()

    def _on_close_request(self, _window: object) -> bool:
        if not self._settled:
            self._settled = True
            self._cancel.cancel()
            GLib.idle_add(lambda: (self._on_done(False), False)[1])
        return False


def present_login(
    parent: Gtk.Window,
    *,
    client_id: str,
    client_secret: str,
    oauth_path: str,
    on_done: Callable[[bool], None],
) -> None:
    YTMusicLoginDialog(
        parent, client_id=client_id, client_secret=client_secret,
        oauth_path=oauth_path, on_done=on_done,
    ).present()
