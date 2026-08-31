"""Shared YouTube Music OAuth device-code flow (GTK-free).

Single source of truth for the flow used by the desktop dialog
(``ui/ytmusic_login``), the web Accounts page (``web/onboarding``), and the CLI
setup script -- previously duplicated three times and hand-synced.

Google retired the shared "TV and Limited Input" client, so the caller supplies
its own OAuth client id/secret (from a Google Cloud project with the YouTube
Data API enabled). Unlike browser cookies, the resulting **refresh token** does
not silently expire -- it auto-refreshes -- which is why this is the durable
sign-in path.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from harmony.errors import AuthError

log = logging.getLogger(__name__)

# Google returns these while the user hasn't approved yet -- keep polling.
_PENDING_ERRORS = {"authorization_pending", "slow_down"}


@dataclass(slots=True)
class DeviceCode:
    """What the user must see: a short code to enter at a Google URL.

    ``device_code`` is the bearer half used to poll for the token -- keep it
    server-side, never show it to a browser client.
    """

    user_code: str
    verification_url: str
    device_code: str
    expires_in: int
    interval: int

    @property
    def full_url(self) -> str:
        return f"{self.verification_url}?user_code={self.user_code}"


def _oauth_classes() -> tuple[Any, Any]:
    try:
        from ytmusicapi.auth.oauth import OAuthCredentials
        from ytmusicapi.auth.oauth.token import RefreshingToken
    except ImportError as exc:  # pragma: no cover - ytmusicapi is a hard dep
        raise AuthError("ytmusicapi is not available.") from exc
    return OAuthCredentials, RefreshingToken


def start_device_code(client_id: str, client_secret: str) -> tuple[Any, DeviceCode]:
    """Begin the flow. Returns ``(credentials, DeviceCode)``. Blocking (one HTTP call)."""
    if not client_id or not client_secret:
        raise AuthError(
            "YouTube Music needs an OAuth client ID and secret. Create a 'TV and "
            "Limited Input' OAuth client in a Google Cloud project with the "
            "YouTube Data API enabled, then enter them here."
        )
    oauth_credentials_cls, _ = _oauth_classes()
    credentials = oauth_credentials_cls(client_id=client_id, client_secret=client_secret)
    try:
        raw = credentials.get_code()
    except Exception as exc:  # noqa: BLE001 - surfaced as a clear auth error
        raise AuthError(f"Could not start Google sign-in: {exc}") from exc
    code = DeviceCode(
        user_code=raw["user_code"],
        verification_url=raw["verification_url"],
        device_code=raw["device_code"],
        expires_in=int(raw.get("expires_in", 300)),
        interval=max(int(raw.get("interval", 5)), 1),
    )
    return credentials, code


def poll_once(credentials: Any, device_code: str) -> dict[str, Any] | None:
    """One token exchange. Returns the raw token dict on success, ``None`` while
    the user hasn't approved yet; raises ``AuthError`` on a terminal failure."""
    try:
        raw = credentials.token_from_code(device_code)
    except AuthError:
        raise
    except Exception as exc:  # noqa: BLE001 - misconfigured client etc.
        raise AuthError(f"Google rejected the OAuth client: {exc}") from exc
    if "access_token" in raw:
        return raw
    error = raw.get("error")
    if error in _PENDING_ERRORS:
        return None
    raise AuthError(f"Google sign-in failed: {error or 'unknown error'}")


def store(credentials: Any, raw_token: dict[str, Any], oauth_path: str) -> None:
    """Persist the exchanged token to ``oauth_path`` (oauth.json)."""
    _, refreshing_token_cls = _oauth_classes()
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


def run_device_flow(
    client_id: str,
    client_secret: str,
    oauth_path: str,
    *,
    on_code,
    cancel=None,
) -> None:
    """Drive the flow to completion synchronously (desktop dialog / CLI).

    Blocking -- run on a worker thread. Calls ``on_code(DeviceCode)`` once, then
    polls until the user approves and stores the token. Raises ``AuthError`` on
    any terminal failure or timeout.
    """
    credentials, code = start_device_code(client_id, client_secret)
    on_code(code)
    deadline = time.monotonic() + code.expires_in
    while time.monotonic() < deadline:
        if cancel is not None:
            cancel.raise_if_cancelled()
        time.sleep(code.interval)
        raw = poll_once(credentials, code.device_code)
        if raw is not None:
            store(credentials, raw, oauth_path)
            return
    raise AuthError("Google sign-in timed out before it was approved.")
