"""Zero-setup YouTube Music sign-in: auto-detect the session from a browser
already logged in on THIS machine and build the ytmusicapi browser-auth headers.

No Google Cloud OAuth client, no DevTools paste -- one click when a signed-in
browser is on the same box (the desktop-as-server case). Uses yt-dlp's robust
cookie support (Firefox/Chrome/… incl. decryption), reading only the user's own
cookies on their own machine. Cookies expire in months; the stale flag +
one-click reconnect handle re-auth. GTK-free.
"""

from __future__ import annotations

import hashlib
import logging
import time

from harmony.errors import AuthError

log = logging.getLogger(__name__)

_YTM_ORIGIN = "https://music.youtube.com"
_SAPISID_COOKIE_NAMES = ("SAPISID", "__Secure-3PAPISID", "__Secure-1PAPISID")
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
_BROWSERS = ("firefox", "chrome", "chromium", "brave", "edge", "vivaldi", "opera")


def _sapisid_hash(sapisid: str, origin: str = _YTM_ORIGIN) -> str:
    """An ``Authorization: SAPISIDHASH`` value; ytmusicapi only reads it to detect
    browser auth and recomputes a fresh one per request, so the timestamp need
    only be well-formed."""
    ts = str(int(time.time()))
    digest = hashlib.sha1(f"{ts} {sapisid} {origin}".encode()).hexdigest()  # noqa: S324
    return f"SAPISIDHASH {ts}_{digest}"


def build_headers_raw(cookies: dict[str, str]) -> str | None:
    """A ytmusicapi ``headers_raw`` string from a YouTube cookie dict, or None if
    it lacks the SAPISID cookie that browser auth requires."""
    sapisid = next((cookies[k] for k in _SAPISID_COOKIE_NAMES if cookies.get(k)), None)
    if not sapisid:
        return None
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return "\n".join([
        f"Authorization: {_sapisid_hash(sapisid)}",
        f"Cookie: {cookie_header}",
        f"User-Agent: {_USER_AGENT}",
        "X-Goog-AuthUser: 0",
        f"Origin: {_YTM_ORIGIN}",
    ])


def autodetect_headers(browser: str | None = None) -> str | None:
    """Return ytmusicapi ``headers_raw`` from a signed-in browser on this box, or
    None if no YouTube session is found. Reads only the user's own cookies."""
    try:
        from yt_dlp.cookies import extract_cookies_from_browser
    except ImportError as exc:
        raise AuthError("yt-dlp is required for browser session auto-detect.") from exc
    for name in ([browser] if browser else _BROWSERS):
        try:
            jar = extract_cookies_from_browser(name)
        except Exception as exc:  # noqa: BLE001 - browser absent/locked; try the next
            log.debug("cookie extract from %s failed: %s", name, exc)
            continue
        cookies = {c.name: c.value for c in jar if c.domain and "youtube.com" in c.domain}
        headers = build_headers_raw(cookies)
        if headers:
            log.info("YouTube session auto-detected from %s", name)
            return headers
    return None
