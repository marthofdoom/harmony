"""Account onboarding for the web server: seed the credentials the server holds
on behalf of every client (this browser, the mobile app).

Kept out of the Engine so all account/auth logic lives in one focused module.
GTK-free. The YouTube OAuth device-flow (the durable primary path) is driven
here via the shared ``providers.ytmusic_oauth`` module; Qobuz stays token-paste
(no OAuth exists).
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

_OAUTH_FLOW_TTL_S = 900  # device codes expire in ~minutes; prune stale flows


class Onboarding:
    def __init__(self, on_change: Callable[[], None], status: Callable[[], dict[str, Any]]) -> None:
        self._on_change = on_change  # reset the provider cache after a credential change
        self._status = status  # -> accounts() dict, returned so the UI updates
        self._oauth_flows: dict[str, dict[str, Any]] = {}

    # -- Qobuz --------------------------------------------------------------

    def set_qobuz_token(self, token: str) -> dict[str, Any]:
        from harmony import config
        from harmony.config import CredentialStore, Settings

        s = Settings.load()
        s.qobuz_auth_kind = "token"
        s.qobuz_token_saved = True
        s.save()
        CredentialStore().set(config.QOBUZ_TOKEN, token.strip())
        self._on_change()
        return self._status()

    # -- YouTube Music: browser headers (fallback) --------------------------

    def set_ytmusic_browser(self, headers_raw: str) -> dict[str, Any]:
        import ytmusicapi

        from harmony import config
        from harmony.config import Settings

        s = Settings.load()
        path = s.ytmusic_auth_file or ""
        if not path or s.ytmusic_auth_kind == "oauth" or path.endswith("oauth.json"):
            path = str(config.config_dir() / "browser.json")
        ytmusicapi.setup(filepath=path, headers_raw=headers_raw)
        s.ytmusic_auth_file = path
        s.ytmusic_auth_kind = "browser"
        s.save()
        self._on_change()
        return self._status()

    # -- YouTube Music: OAuth device-flow (primary, durable) ----------------

    def set_ytmusic_oauth_client(self, client_id: str, client_secret: str) -> dict[str, Any]:
        from harmony import config
        from harmony.config import CredentialStore, Settings

        s = Settings.load()
        s.ytmusic_oauth_client_id = (client_id or "").strip()
        s.save()
        CredentialStore().set(config.YTMUSIC_OAUTH_SECRET, (client_secret or "").strip())
        return {"ok": True}

    def ytmusic_oauth_start(self) -> dict[str, Any]:
        from harmony import config
        from harmony.config import CredentialStore, Settings
        from harmony.providers import ytmusic_oauth

        s = Settings.load()
        client_id = s.ytmusic_oauth_client_id
        client_secret = CredentialStore().get(config.YTMUSIC_OAUTH_SECRET) or ""
        credentials, code = ytmusic_oauth.start_device_code(client_id, client_secret)
        poll_token = secrets.token_urlsafe(12)
        self._oauth_flows[poll_token] = {
            "credentials": credentials, "device_code": code.device_code, "at": time.monotonic(),
        }
        self._prune()
        return {
            "poll_token": poll_token, "user_code": code.user_code,
            "verification_url": code.verification_url, "full_url": code.full_url,
            "interval": code.interval, "expires_in": code.expires_in,
        }

    def ytmusic_oauth_poll(self, poll_token: str) -> dict[str, Any]:
        from harmony import config
        from harmony.config import Settings
        from harmony.providers import ytmusic_oauth

        flow = self._oauth_flows.get(poll_token)
        if flow is None:
            raise KeyError("poll token")
        raw = ytmusic_oauth.poll_once(flow["credentials"], flow["device_code"])
        if raw is None:
            return {"status": "pending"}
        path = str(config.config_dir() / "oauth.json")
        ytmusic_oauth.store(flow["credentials"], raw, path)
        s = Settings.load()
        s.ytmusic_auth_file = path
        s.ytmusic_auth_kind = "oauth"
        s.save()
        self._oauth_flows.pop(poll_token, None)
        self._on_change()
        return {"status": "done", **self._status()}

    # -- sign out -----------------------------------------------------------

    def signout(self, service_value: str) -> dict[str, Any]:
        from harmony import config
        from harmony.config import CredentialStore, Settings

        s = Settings.load()
        if service_value == "qobuz":
            s.qobuz_token_saved = False
            CredentialStore().delete(config.QOBUZ_TOKEN)
        elif service_value == "ytmusic":
            s.ytmusic_auth_file = ""
        else:
            raise KeyError(service_value)
        s.save()
        self._on_change()
        return self._status()

    def _prune(self) -> None:
        cutoff = time.monotonic() - _OAUTH_FLOW_TTL_S
        for token in [k for k, v in self._oauth_flows.items() if v["at"] < cutoff]:
            self._oauth_flows.pop(token, None)
