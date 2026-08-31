"""Preferences: accounts, integrations, and sync defaults.

Non-secret values live in ``config.Settings`` and are saved immediately on
change; secrets go through ``config.CredentialStore`` (keyring-backed, with a
guarded file fallback the user is warned about if no keyring is available).
Every row is debounced so a burst of keystrokes doesn't hammer the keyring,
but pending edits are flushed the moment the dialog closes.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from harmony import config  # noqa: E402
from harmony.models import Service  # noqa: E402
from harmony.tasks import run_async  # noqa: E402
from harmony.ui.state import AppState  # noqa: E402
from harmony.ui.theming import THEMES, apply_theme, theme_index  # noqa: E402

log = logging.getLogger(__name__)

_YT_AUTH_KINDS = ["browser", "oauth"]
_QOBUZ_AUTH_KINDS = ["password", "token"]
_DIRECTIONS = ["mirror-a-to-b", "mirror-b-to-a", "two-way"]
_THEME_IDS = [t.id for t in THEMES]


class PreferencesDialog(Adw.PreferencesDialog):
    """GNOME HIG preferences window: Accounts / Integrations / Sync pages."""

    def __init__(self, state: AppState) -> None:
        super().__init__(title="Preferences")
        self.state = state
        self.settings = state.settings
        self.credentials = state.credentials
        self._debounce: dict[str, tuple[int, Callable[[], None]]] = {}
        self._syncing_thresholds = False

        self.add(self._build_appearance_page())
        self.add(self._build_accounts_page())
        self.add(self._build_integrations_page())
        self.add(self._build_sync_page())
        self.add(self._build_network_page())
        self.connect("closed", lambda *_a: self._flush_pending())

    # -- debounced persistence -------------------------------------------------

    def _schedule(self, key: str, fn: Callable[[], None], delay: int = 400) -> None:
        pending = self._debounce.pop(key, None)
        if pending is not None:
            GLib.source_remove(pending[0])

        def run() -> bool:
            self._debounce.pop(key, None)
            fn()
            return False

        self._debounce[key] = (GLib.timeout_add(delay, run), fn)

    def _flush_pending(self) -> None:
        pending = list(self._debounce.items())
        self._debounce.clear()
        for _key, (source_id, fn) in pending:
            GLib.source_remove(source_id)
            fn()

    def _set_setting(self, name: str, value: object) -> None:
        setattr(self.settings, name, value)
        self.settings.save()

    def _set_account_setting(self, name: str, value: object) -> None:
        self._set_setting(name, value)
        self.state.reload_providers()

    def _set_ai_setting(self, name: str, value: object) -> None:
        """Persist an AI setting and rebuild the planner so pages redraw."""
        self._set_setting(name, value)
        self.state.reload_planner()

    def _set_matching_setting(self, name: str, value: object) -> None:
        """Persist a match setting and push it into the running sync engine.

        Without the second step the new threshold would sit in settings.json
        doing nothing until the app was restarted.
        """
        self._set_setting(name, value)
        self.state.apply_matching_settings()

    # -- appearance ---------------------------------------------------------------

    def _build_network_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title="Network", name="network",
                                   icon_name="network-server-symbolic")
        group = Adw.PreferencesGroup(
            title="API server",
            description="Harmony always runs its API server so other devices — a phone, "
            "another Harmony instance — can discover this one on the LAN and use it as a "
            "backend. Closing the window keeps it running; Quit (Ctrl+Q) exits. Keep it on "
            "a trusted network, or set a personal key that clients must present.",
        )
        self.server_port_row = Adw.SpinRow.new_with_range(1024, 65535, 1)
        self.server_port_row.set_title("Port")
        self.server_port_row.set_value(self.settings.server_port or 8080)
        self.server_port_row.connect(
            "notify::value",
            lambda r, _p: self._set_account_setting("server_port", int(r.get_value())),
        )
        group.add(self.server_port_row)

        self.personal_key_row = Adw.EntryRow(title="Personal key",
                                             text=self.settings.personal_key)
        self.personal_key_row.connect(
            "notify::text",
            lambda r, _p: self._schedule(
                "personal_key", lambda: self._set_account_setting("personal_key", r.get_text())
            ),
        )
        group.add(self.personal_key_row)
        page.add(group)
        return page

    def _build_appearance_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(
            title="Appearance", name="appearance", icon_name="applications-graphics-symbolic"
        )

        group = Adw.PreferencesGroup(
            title="Theme", description="Pick a color theme for Harmony -- applied immediately."
        )
        self.theme_row = Adw.ComboRow(
            title="Theme", model=Gtk.StringList.new([t.name for t in THEMES])
        )
        self.theme_row.set_selected(theme_index(self.settings.theme))
        self.theme_row.connect("notify::selected", self._on_theme_changed)
        group.add(self.theme_row)
        page.add(group)
        return page

    def _on_theme_changed(self, row: Adw.ComboRow, _param: object) -> None:
        theme_id = _THEME_IDS[row.get_selected()]
        self._set_setting("theme", theme_id)
        apply_theme(theme_id)

    # -- accounts ---------------------------------------------------------------

    def _build_accounts_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title="Accounts", name="accounts", icon_name="avatar-default-symbolic")

        if not self.credentials.uses_keyring:
            warn_group = Adw.PreferencesGroup()
            warn_row = Adw.ActionRow(
                title="No system keyring found",
                subtitle=f"Secrets are stored in a local file under {config.config_dir()} instead of "
                "your keyring. Install gnome-keyring or kwallet for encrypted storage.",
            )
            warn_row.add_prefix(Gtk.Image.new_from_icon_name("dialog-warning-symbolic"))
            warn_group.add(warn_row)
            page.add(warn_group)

        yt_group = Adw.PreferencesGroup(title="YouTube Music")

        # Primary, zero-setup: detect a signed-in session from a browser on this
        # machine. No Google Cloud client, no DevTools paste.
        yt_detect_row = Adw.ActionRow(
            title="Connect YouTube",
            subtitle="Detects a signed-in session from a browser on this computer — "
            "no setup, no pasting.",
        )
        yt_detect_button = Gtk.Button(label="Connect", valign=Gtk.Align.CENTER,
                                      css_classes=["suggested-action"])
        yt_detect_button.connect("clicked", self._on_ytmusic_autodetect)
        yt_detect_row.add_suffix(yt_detect_button)
        yt_group.add(yt_detect_row)

        self.yt_kind_row = Adw.ComboRow(
            title="Authentication method", model=Gtk.StringList.new(["Browser headers", "OAuth"])
        )
        self.yt_kind_row.set_selected(_YT_AUTH_KINDS.index(self.settings.ytmusic_auth_kind)
                                       if self.settings.ytmusic_auth_kind in _YT_AUTH_KINDS else 0)
        self.yt_kind_row.connect("notify::selected", self._on_ytmusic_auth_kind_changed)
        yt_group.add(self.yt_kind_row)

        # OAuth device-flow sign-in: the clean "log in with your real browser"
        # path. Needs the user's own Google OAuth client (Google retired the
        # shared one), entered here.
        self.yt_client_id_row = Adw.EntryRow(
            title="OAuth Client ID", text=self.settings.ytmusic_oauth_client_id
        )
        self.yt_client_id_row.connect(
            "notify::text",
            lambda r, _p: self._schedule(
                "yt_client_id", lambda: self._set_account_setting("ytmusic_oauth_client_id", r.get_text())
            ),
        )
        yt_group.add(self.yt_client_id_row)

        self.yt_client_secret_row = Adw.PasswordEntryRow(
            title="OAuth Client Secret", text=self.credentials.get(config.YTMUSIC_OAUTH_SECRET) or ""
        )
        self.yt_client_secret_row.connect(
            "notify::text",
            lambda r, _p: self._schedule(
                "yt_client_secret", lambda: self.credentials.set(config.YTMUSIC_OAUTH_SECRET, r.get_text())
            ),
        )
        yt_group.add(self.yt_client_secret_row)

        # In-app setup guide so the OAuth client isn't a mystery — a one-time
        # Google Cloud setup, with the console linked directly.
        self.yt_help_row = yt_help_row = Adw.ExpanderRow(
            title="How to get a Client ID and Secret",
            subtitle="One-time Google Cloud setup — click to expand",
        )
        steps = Gtk.Label(
            use_markup=True, wrap=True, xalign=0.0, selectable=True,
            css_classes=["dim-label"],
            margin_top=8, margin_bottom=8, margin_start=12, margin_end=12,
        )
        steps.set_markup(
            "1. Open the "
            "<a href='https://console.cloud.google.com'>Google Cloud Console</a> "
            "and create or pick a project.\n"
            "2. Enable <b>YouTube Data API v3</b> (APIs &amp; Services → Library).\n"
            "3. Configure the <b>OAuth consent screen</b> (User type <b>External</b>) "
            "and add your own Google account as a <b>Test user</b>.\n"
            "4. Create <b>Credentials → OAuth client ID</b>, application type "
            "<b>TVs and Limited Input devices</b>.\n"
            "5. Paste the Client ID and Secret above, then Sign in.\n\n"
            "While the consent screen stays in “testing”, sign-in expires after "
            "~7 days; publishing it (no Google review needed for your own use) keeps it "
            "long-lived."
        )
        steps_row = Gtk.ListBoxRow(activatable=False, selectable=False, child=steps)
        yt_help_row.add_row(steps_row)
        console_button = Gtk.Button(
            label="Open Google Cloud Console", valign=Gtk.Align.CENTER,
        )
        console_button.connect(
            "clicked",
            lambda *_a: self._open_uri("https://console.cloud.google.com/apis/credentials"),
        )
        yt_help_row.add_suffix(console_button)
        yt_group.add(yt_help_row)

        self.yt_signin_row = yt_signin_row = Adw.ActionRow(
            title="Sign in with Google",
            subtitle="Opens Google in your browser and links this account with a short code — "
            "no devtools needed. Needs the OAuth client above.",
        )
        yt_signin_button = Gtk.Button(label="Sign in…", valign=Gtk.Align.CENTER)
        yt_signin_button.connect("clicked", self._on_ytmusic_google_signin)
        yt_signin_row.add_suffix(yt_signin_button)
        yt_group.add(yt_signin_row)

        self.yt_file_row = Adw.ActionRow(
            title="Auth file", subtitle=self.settings.ytmusic_auth_file or "Not set"
        )
        choose_button = Gtk.Button(label="Choose File…", valign=Gtk.Align.CENTER)
        choose_button.connect("clicked", self._on_choose_yt_file)
        self.yt_file_row.add_suffix(choose_button)
        yt_group.add(self.yt_file_row)

        paste_row = Adw.ActionRow(
            title="Paste Browser Headers",
            subtitle="Set up authentication from headers copied out of your browser's devtools",
        )
        paste_button = Gtk.Button(label="Paste…", valign=Gtk.Align.CENTER)
        paste_button.connect("clicked", self._on_paste_headers)
        paste_row.add_suffix(paste_button)
        yt_group.add(paste_row)

        self.yt_status_row = Adw.ActionRow(title="Connection", subtitle="Unknown")
        yt_test_button = Gtk.Button(label="Test", valign=Gtk.Align.CENTER)
        yt_test_button.connect("clicked", lambda *_a: self._test_provider(Service.YTMUSIC, self.yt_status_row))
        self.yt_status_row.add_suffix(yt_test_button)
        yt_group.add(self.yt_status_row)
        self._update_ytmusic_auth_visibility()
        page.add(yt_group)

        qb_group = Adw.PreferencesGroup(title="Qobuz")

        self.qb_auth_kind_row = Adw.ComboRow(
            title="Sign-in method",
            model=Gtk.StringList.new(["Email and password", "Paste session token"]),
        )
        self.qb_auth_kind_row.set_selected(
            _QOBUZ_AUTH_KINDS.index(self.settings.qobuz_auth_kind)
            if self.settings.qobuz_auth_kind in _QOBUZ_AUTH_KINDS else 0
        )
        self.qb_auth_kind_row.connect("notify::selected", self._on_qobuz_auth_kind_changed)
        qb_group.add(self.qb_auth_kind_row)

        self.qb_email_row = Adw.EntryRow(title="Email", text=self.settings.qobuz_email)
        self.qb_email_row.connect(
            "notify::text", lambda r, _p: self._schedule("qobuz_email", lambda: self._set_account_setting("qobuz_email", r.get_text()))
        )
        qb_group.add(self.qb_email_row)

        self.qb_password_row = Adw.PasswordEntryRow(
            title="Password", text=self.credentials.get(config.QOBUZ_PASSWORD) or ""
        )
        self.qb_password_row.connect(
            "notify::text", lambda r, _p: self._schedule("qobuz_password", lambda: self.credentials.set(config.QOBUZ_PASSWORD, r.get_text()))
        )
        qb_group.add(self.qb_password_row)

        # The pleasant path: an in-app browser that captures the token for you.
        # Only meaningful when WebKitGTK is present (always so in the Flatpak).
        from harmony.ui import qobuz_login

        self.qb_browser_login_row = Adw.ActionRow(
            title="Sign in with embedded browser",
            subtitle="Opens Qobuz's own login in-app and captures the session token for you. "
            "No devtools needed — but Google/social sign-in won't work here (see below).",
        )
        browser_login_button = Gtk.Button(label="Sign in…", valign=Gtk.Align.CENTER,
                                          css_classes=["suggested-action"])
        browser_login_button.connect("clicked", self._on_qobuz_browser_login)
        self.qb_browser_login_row.add_suffix(browser_login_button)
        self._webkit_login_available = qobuz_login.AVAILABLE
        qb_group.add(self.qb_browser_login_row)

        # For Google/social-login accounts, which Google refuses to sign in
        # inside any embedded webview: walks the user through running a
        # bookmarklet in their real browser instead. Always available — no
        # WebKit needed, since the sign-in itself never happens in-app.
        self.qb_browser_assist_row = Adw.ActionRow(
            title="Sign in via your browser",
            subtitle="For Google/social Qobuz accounts — opens Qobuz in your real browser "
            "and helps you grab the token.",
        )
        browser_assist_button = Gtk.Button(label="Sign in…", valign=Gtk.Align.CENTER)
        browser_assist_button.connect("clicked", self._on_qobuz_browser_assist)
        self.qb_browser_assist_row.add_suffix(browser_assist_button)
        qb_group.add(self.qb_browser_assist_row)

        self.qb_token_row = Adw.PasswordEntryRow(
            title="Session token", text=self.credentials.get(config.QOBUZ_TOKEN) or ""
        )
        self.qb_token_row.connect(
            "notify::text", lambda r, _p: self._schedule("qobuz_token", lambda: self._on_qobuz_token_changed(r.get_text()))
        )
        qb_group.add(self.qb_token_row)

        self.qb_token_help_row = Adw.ActionRow(
            title="Or paste a token manually (devtools)",
            subtitle="Sign in at play.qobuz.com, open devtools → Network, click any request to "
            "www.qobuz.com/api.json/0.2/, and copy the X-User-Auth-Token request header "
            "(the X-App-Id header on that same request goes in App ID below, if auto-detection "
            "ever fails).",
        )
        qb_group.add(self.qb_token_help_row)

        self._update_qobuz_auth_visibility()

        self.qb_app_id_row = Adw.EntryRow(title="App ID (optional)", text=self.settings.qobuz_app_id)
        self.qb_app_id_row.connect(
            "notify::text", lambda r, _p: self._schedule("qobuz_app_id", lambda: self._set_account_setting("qobuz_app_id", r.get_text()))
        )
        qb_group.add(self.qb_app_id_row)

        self.qb_status_row = Adw.ActionRow(title="Connection", subtitle="Unknown")
        qb_test_button = Gtk.Button(label="Test", valign=Gtk.Align.CENTER)
        qb_test_button.connect("clicked", lambda *_a: self._test_provider(Service.QOBUZ, self.qb_status_row))
        self.qb_status_row.add_suffix(qb_test_button)
        qb_group.add(self.qb_status_row)
        page.add(qb_group)
        return page

    def _on_qobuz_auth_kind_changed(self, row: Adw.ComboRow, _param: object) -> None:
        kind = _QOBUZ_AUTH_KINDS[row.get_selected()]
        self._set_account_setting("qobuz_auth_kind", kind)
        self._update_qobuz_auth_visibility()

    def _update_qobuz_auth_visibility(self) -> None:
        """Show only the row(s) the selected sign-in method actually uses.

        Keeps the dialog from implying both a password *and* a token are
        needed at once -- the two methods are mutually exclusive ways to
        reach the same bearer token, not additive settings.
        """
        is_token = self.settings.qobuz_auth_kind == "token"
        self.qb_email_row.set_visible(not is_token)
        self.qb_password_row.set_visible(not is_token)
        # The embedded browser-login button only makes sense in token mode,
        # and only when WebKit is actually there to open a webview.
        self.qb_browser_login_row.set_visible(is_token and self._webkit_login_available)
        # The browser-assist flow needs no WebKit at all — the sign-in
        # happens in the user's real, external browser — so it's offered in
        # token mode regardless of embedded-webview availability.
        self.qb_browser_assist_row.set_visible(is_token)
        self.qb_token_row.set_visible(is_token)
        self.qb_token_help_row.set_visible(is_token)

    def _on_qobuz_browser_login(self, _button: Gtk.Button) -> None:
        from harmony.ui import qobuz_login

        def handle(token: str | None) -> None:
            if not token:
                self.state.toast("Qobuz sign-in was cancelled or captured no token.")
                return
            self.credentials.set(config.QOBUZ_TOKEN, token)
            self.qb_token_row.set_text(token)  # reflect it in the manual field
            self._set_account_setting("qobuz_token_saved", True)
            self.state.toast("Signed in to Qobuz.")

        qobuz_login.present_login(self.get_root(), handle)

    def _on_qobuz_browser_assist(self, _button: Gtk.Button) -> None:
        from harmony.ui import qobuz_login

        def handle(token: str | None) -> None:
            if not token:
                self.state.toast("Cancelled.")
                return
            self.credentials.set(config.QOBUZ_TOKEN, token)
            self.qb_token_row.set_text(token)  # reflect it in the manual field
            self._set_account_setting("qobuz_token_saved", True)
            self.state.toast("Signed in to Qobuz.")

        qobuz_login.present_browser_assist(self.get_root(), handle)

    def _on_qobuz_token_changed(self, value: str) -> None:
        self.credentials.set(config.QOBUZ_TOKEN, value)
        # Non-secret companion flag so has_credentials (which must stay
        # I/O-free -- see QobuzProvider.has_credentials) can tell "a token
        # has been pasted" apart from "token mode selected but nothing
        # pasted yet" without reading the keyring.
        self._set_account_setting("qobuz_token_saved", bool(value))

    def _open_uri(self, uri: str) -> None:
        try:
            Gtk.UriLauncher.new(uri).launch(self.get_root(), None, None)
        except Exception:  # noqa: BLE001 - non-fatal; the link is also shown as text
            log.debug("Could not open %s", uri, exc_info=True)

    def _on_ytmusic_auth_kind_changed(self, row: Adw.ComboRow, _param: object) -> None:
        self._set_account_setting("ytmusic_auth_kind", _YT_AUTH_KINDS[row.get_selected()])
        self._update_ytmusic_auth_visibility()

    def _update_ytmusic_auth_visibility(self) -> None:
        """Show the OAuth client rows only when OAuth is the chosen method.

        The primary path is one-click auto-detect (top) or a browser-header
        paste; the Google Cloud OAuth client is a niche, high-friction option, so
        hide its fields unless the user deliberately switches to it.
        """
        is_oauth = self.settings.ytmusic_auth_kind == "oauth"
        for row in (self.yt_client_id_row, self.yt_client_secret_row,
                    self.yt_help_row, self.yt_signin_row):
            row.set_visible(is_oauth)

    def _on_ytmusic_autodetect(self, _button: Gtk.Button) -> None:
        self.state.toast("Detecting a signed-in browser…")

        def work() -> str:
            import ytmusicapi

            from harmony.providers.ytmusic_cookies import autodetect_headers

            headers = autodetect_headers()
            if not headers:
                raise RuntimeError(
                    "No signed-in YouTube session found in a browser on this machine. "
                    "Sign in to music.youtube.com in a browser here, then retry."
                )
            path = self.settings.ytmusic_auth_file or ""
            if not path or self.settings.ytmusic_auth_kind == "oauth" or path.endswith("oauth.json"):
                path = str(config.config_dir() / "browser.json")
            ytmusicapi.setup(filepath=path, headers_raw=headers)
            return path

        def done(path: str) -> None:
            self.settings.ytmusic_auth_file = path
            self.settings.ytmusic_auth_kind = "browser"
            self.settings.save()
            self.yt_file_row.set_subtitle(path)
            if "browser" in _YT_AUTH_KINDS:
                self.yt_kind_row.set_selected(_YT_AUTH_KINDS.index("browser"))
            self.state.reload_providers()
            self.state.toast("Connected to YouTube Music.")

        run_async(work, done, lambda exc: self.state.toast(f"Couldn't connect: {exc}"))

    def _on_ytmusic_google_signin(self, _button: Gtk.Button) -> None:
        from harmony.ui import ytmusic_login

        client_id = self.settings.ytmusic_oauth_client_id
        client_secret = self.credentials.get(config.YTMUSIC_OAUTH_SECRET) or ""
        if not client_id or not client_secret:
            self.state.toast("Enter your OAuth Client ID and Secret first.")
            return
        oauth_path = self.settings.ytmusic_auth_file or str(config.config_dir() / "ytmusic-oauth.json")

        def done(ok: bool) -> None:
            if not ok:
                self.state.toast("YouTube Music sign-in was cancelled or failed.")
                return
            self.settings.ytmusic_auth_file = oauth_path
            self.settings.ytmusic_auth_kind = "oauth"
            self.settings.save()
            self.yt_file_row.set_subtitle(oauth_path)
            if "oauth" in _YT_AUTH_KINDS:
                self.yt_kind_row.set_selected(_YT_AUTH_KINDS.index("oauth"))
            self.state.reload_providers()
            self.state.toast("Signed in to YouTube Music.")

        ytmusic_login.present_login(
            self.get_root(), client_id=client_id, client_secret=client_secret,
            oauth_path=oauth_path, on_done=done,
        )

    def _on_choose_yt_file(self, _button: Gtk.Button) -> None:
        dialog = Gtk.FileDialog(title="Select browser.json / oauth.json")
        dialog.open(self.get_root(), None, self._on_yt_file_chosen)

    def _on_yt_file_chosen(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error as exc:
            if not (exc.matches(Gtk.DialogError.quark(), Gtk.DialogError.CANCELLED)
                    or exc.matches(Gtk.DialogError.quark(), Gtk.DialogError.DISMISSED)):
                self.state.toast(f"Couldn't select file: {exc.message}")
            return
        path = gfile.get_path()
        self.yt_file_row.set_subtitle(path)
        self._set_account_setting("ytmusic_auth_file", path)

    def _on_paste_headers(self, _button: Gtk.Button) -> None:
        if not self.settings.ytmusic_auth_file:
            self.state.toast("Choose an auth file location first.")
            return
        dialog = Adw.AlertDialog(
            heading="Paste Browser Headers",
            body="Paste the raw request headers copied from your browser's network inspector "
            "(a request to music.youtube.com, \"Copy as cURL\" headers or raw header text).",
        )
        frame = Gtk.Frame()
        text_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD, height_request=160,
                                  top_margin=6, bottom_margin=6, left_margin=6, right_margin=6)
        frame.set_child(text_view)
        dialog.set_extra_child(frame)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_close_response("cancel")

        def on_response(_dlg: Adw.AlertDialog, response: str) -> None:
            if response != "save":
                return
            buf = text_view.get_buffer()
            headers_raw = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip()
            if not headers_raw:
                return
            path = self.settings.ytmusic_auth_file

            def work() -> str:
                import ytmusicapi

                return ytmusicapi.setup(filepath=path, headers_raw=headers_raw)

            def done(_headers: str) -> None:
                self.state.toast("YouTube Music headers saved.")
                self.state.reload_providers()

            run_async(work, done, lambda exc: self.state.toast(f"Couldn't set up headers: {exc}"))

        dialog.connect("response", on_response)
        dialog.present(self)

    def _test_provider(self, service: Service, status_row: Adw.ActionRow) -> None:
        provider = self.state.providers.get(service)
        if provider is None:
            status_row.set_subtitle("Not configured")
            return
        status_row.set_subtitle("Testing…")

        def work() -> str | None:
            provider.authenticate()
            return provider.account_name()

        def done(name: str | None) -> None:
            status_row.set_subtitle(f"Connected as {name}" if name else "Connected")

        def error(exc: BaseException) -> None:
            status_row.set_subtitle(f"Failed: {exc}")

        run_async(work, done, error)

    # -- integrations ---------------------------------------------------------

    def _build_integrations_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title="Integrations", name="integrations", icon_name="applications-internet-symbolic")

        lastfm_group = Adw.PreferencesGroup(title="Last.fm")
        enabled_row = Adw.SwitchRow(title="Enabled", active=self.settings.lastfm_enabled)
        enabled_row.connect("notify::active", lambda r, _p: self._set_setting("lastfm_enabled", r.get_active()))
        lastfm_group.add(enabled_row)
        key_row = Adw.PasswordEntryRow(title="API Key", text=self.credentials.get(config.LASTFM_API_KEY) or "")
        key_row.connect(
            "notify::text", lambda r, _p: self._schedule("lastfm_key", lambda: self.credentials.set(config.LASTFM_API_KEY, r.get_text()))
        )
        lastfm_group.add(key_row)
        page.add(lastfm_group)

        mb_group = Adw.PreferencesGroup(title="MusicBrainz / ListenBrainz")
        mb_row = Adw.SwitchRow(title="MusicBrainz enabled", active=self.settings.musicbrainz_enabled)
        mb_row.connect("notify::active", lambda r, _p: self._set_setting("musicbrainz_enabled", r.get_active()))
        mb_group.add(mb_row)
        lb_row = Adw.SwitchRow(title="ListenBrainz enabled", active=self.settings.listenbrainz_enabled)
        lb_row.connect("notify::active", lambda r, _p: self._set_setting("listenbrainz_enabled", r.get_active()))
        mb_group.add(lb_row)
        contact_row = Adw.EntryRow(title="Contact email (MusicBrainz User-Agent)", text=self.settings.contact_email)
        contact_row.connect(
            "notify::text", lambda r, _p: self._schedule("contact_email", lambda: self._set_contact_email(r.get_text()))
        )
        mb_group.add(contact_row)
        page.add(mb_group)

        ai_group = Adw.PreferencesGroup(title="AI Playlist Builder")
        ai_enabled_row = Adw.SwitchRow(title="Enabled", active=self.settings.ai_enabled)
        ai_enabled_row.connect(
            "notify::active",
            # Reload the planner too: flipping this changes whether the
            # Discover page shows its builder or its "not configured"
            # banner, and reload_planner is what tells that page to redraw.
            lambda r, _p: self._set_ai_setting("ai_enabled", r.get_active()),
        )
        ai_group.add(ai_enabled_row)
        ai_key_row = Adw.PasswordEntryRow(title="Anthropic API Key", text=self.credentials.get(config.ANTHROPIC_API_KEY) or "")
        ai_key_row.connect("notify::text", lambda r, _p: self._schedule("ai_key", lambda: self._on_ai_key_changed(r.get_text())))
        ai_group.add(ai_key_row)
        ai_model_row = Adw.EntryRow(title="Model", text=self.settings.ai_model)
        ai_model_row.connect("notify::text", lambda r, _p: self._schedule("ai_model", lambda: self._on_ai_model_changed(r.get_text())))
        ai_group.add(ai_model_row)
        page.add(ai_group)
        return page

    def _set_contact_email(self, value: str) -> None:
        self._set_setting("contact_email", value)
        # enrich._get_json caches the resolved User-Agent (config.user_agent())
        # for the life of the process to avoid a Settings.load() disk read on
        # every enrichment HTTP call; invalidate it here so a new contact
        # email actually reaches MusicBrainz/ListenBrainz's User-Agent header
        # instead of silently sticking at whatever was first resolved.
        from harmony.enrich import reset_user_agent_cache

        reset_user_agent_cache()

    def _on_ai_key_changed(self, value: str) -> None:
        self.credentials.set(config.ANTHROPIC_API_KEY, value)
        self.state.reload_planner()

    def _on_ai_model_changed(self, value: str) -> None:
        self._set_setting("ai_model", value)
        self.state.reload_planner()

    # -- sync ---------------------------------------------------------------------

    def _build_sync_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title="Sync", name="sync", icon_name="emblem-synchronizing-symbolic")

        group = Adw.PreferencesGroup(title="Defaults")
        direction_row = Adw.ComboRow(
            title="Default direction",
            model=Gtk.StringList.new(["Mirror source → target", "Mirror target → source", "Two-way"]),
        )
        direction_row.set_selected(
            _DIRECTIONS.index(self.settings.default_direction) if self.settings.default_direction in _DIRECTIONS else 2
        )
        direction_row.connect(
            "notify::selected", lambda r, _p: self._set_setting("default_direction", _DIRECTIONS[r.get_selected()])
        )
        group.add(direction_row)

        snapshot_row = Adw.SwitchRow(title="Snapshot playlists before syncing", active=self.settings.snapshot_before_sync)
        snapshot_row.connect("notify::active", lambda r, _p: self._set_setting("snapshot_before_sync", r.get_active()))
        group.add(snapshot_row)

        auto_accept_row = Adw.SwitchRow(
            title="Auto-accept high-confidence matches", active=self.settings.auto_accept_high
        )
        auto_accept_row.connect(
            "notify::active",
            lambda r, _p: self._schedule(
                "auto_accept_high", lambda: self._set_matching_setting("auto_accept_high", r.get_active())
            ),
        )
        group.add(auto_accept_row)
        page.add(group)

        threshold_group = Adw.PreferencesGroup(
            title="Match Thresholds", description="How similar two tracks must be before Harmony treats them as the same recording."
        )
        self.high_row = Adw.SpinRow.new_with_range(0.0, 1.0, 0.01)
        self.high_row.set_title("High-confidence threshold")
        self.high_row.set_value(self.settings.match_high_threshold)
        self.high_row.connect("notify::value", lambda r, _p: self._on_threshold_row_changed("match_high_threshold", r))
        threshold_group.add(self.high_row)

        self.low_row = Adw.SpinRow.new_with_range(0.0, 1.0, 0.01)
        self.low_row.set_title("Low-confidence threshold")
        self.low_row.set_value(self.settings.match_low_threshold)
        self.low_row.connect("notify::value", lambda r, _p: self._on_threshold_row_changed("match_low_threshold", r))
        threshold_group.add(self.low_row)
        page.add(threshold_group)
        return page

    def _on_threshold_row_changed(self, name: str, row: Adw.SpinRow) -> None:
        # Holding a SpinRow's +/- button fires "notify::value" once per tick;
        # route it through the same debounce every other row uses instead of
        # writing settings.json (and rebuilding the sync engine) synchronously
        # on the main loop for every tick. Skipped while we're the ones
        # programmatically setting a row's value (see _apply_threshold_change)
        # so clamping the *other* row doesn't schedule a redundant write.
        if self._syncing_thresholds:
            return
        self._schedule(name, lambda: self._apply_threshold_change(name, row.get_value()))

    def _apply_threshold_change(self, name: str, value: float) -> None:
        """Persist a match threshold, keeping ``low <= high`` coherent.

        An inverted pair (low > high) makes matching return "high" for scores
        below the low threshold, which is nonsensical. If this edit would
        invert the pair, clamp the *other* threshold to meet it rather than
        silently persisting garbage, and push the clamped value back into
        that row so the UI reflects what actually took effect.
        """
        value = max(0.0, min(1.0, value))
        high = value if name == "match_high_threshold" else self.settings.match_high_threshold
        low = value if name == "match_low_threshold" else self.settings.match_low_threshold
        if low > high:
            if name == "match_high_threshold":
                low = high
            else:
                high = low

        changed = False
        if high != self.settings.match_high_threshold:
            self.settings.match_high_threshold = high
            changed = True
        if low != self.settings.match_low_threshold:
            self.settings.match_low_threshold = low
            changed = True
        if not changed:
            return
        self.settings.save()

        self._syncing_thresholds = True
        try:
            self.high_row.set_value(high)
            self.low_row.set_value(low)
        finally:
            self._syncing_thresholds = False

        self.state.apply_matching_settings()
