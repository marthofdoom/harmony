"""Qobuz provider.

There is no official public Qobuz API, so this is a small reverse-engineered
client against the same JSON endpoints the Qobuz web player uses
(``https://www.qobuz.com/api.json/0.2/``). The app id/secret pair and the
login flow mirror what tools like streamrip and qobuz-dl have documented.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import re
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

import requests

from .. import config
from ..config import CredentialStore, Settings, user_agent
from ..errors import AuthError, NotSupportedError, ProviderError, RateLimitedError
from ..models import Album, Artist, Playlist, SearchResults, Service, StreamSource, Track
from .base import MusicProvider, _chunked

log = logging.getLogger(__name__)

BASE_URL = "https://www.qobuz.com/api.json/0.2/"
LOGIN_PAGE_URL = "https://play.qobuz.com/login"
PLAYER_ORIGIN = "https://play.qobuz.com"

_BUNDLE_URL_RE = re.compile(r"/resources/[\d.]+-b\d+/bundle\.js")
_APP_ID_RE = re.compile(r'production:\{api:\{appId:"(?P<app_id>\d+)",appSecret:"(?P<inline_secret>\w+)"')
_SEED_RE = re.compile(r'[a-z]\.initialSeed\("(?P<seed>[\w=]+)",window\.utimezone\.(?P<timezone>[a-z]+)\)')
_INFO_RE = re.compile(r'name:"\w+/(?P<timezone>[A-Za-z]+)",info:"(?P<info>[\w=]+)",extras:"(?P<extras>[\w=]+)"')

# streamrip strips a trailing widevine-style hash from the decoded blob
_SECRET_SUFFIX_LEN = 44

_SEARCH_TYPES = {"tracks", "albums", "artists", "playlists"}
_ADD_CHUNK_SIZE = 50
_PAGE_SIZE = 500


def request_sig(object_name: str, method: str, params: Mapping[str, Any], ts: int, app_secret: str) -> str:
    """Compute the md5 request signature required by Qobuz's legacy signed endpoints.

    None of the endpoints this provider calls (catalog browsing, playlist CRUD,
    favorites) need a signature — only some streaming/format endpoints do — but
    it's implemented here per the documented scheme, kept pure so it's cheap to
    unit test on its own.
    """
    param_str = "".join(f"{k}{v}" for k, v in sorted(params.items()))
    payload = f"{object_name}{method}{param_str}{ts}{app_secret}"
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def _scrape_app_credentials(session: requests.Session) -> tuple[str, str]:
    """Scrape ``app_id``/``app_secret`` from the Qobuz web player bundle.

    Only called when the user hasn't pasted an ``app_id`` manually. Any
    failure (network, or Qobuz changing their bundle format) is surfaced as
    ``AuthError`` with a hint to configure it by hand instead of crashing.
    """
    try:
        login_page = session.get(LOGIN_PAGE_URL, timeout=15)
        login_page.raise_for_status()
        bundle_match = _BUNDLE_URL_RE.search(login_page.text)
        if not bundle_match:
            raise AuthError(
                "Could not find the Qobuz web player bundle to scrape credentials from. "
                "You can paste an app_id manually in Preferences."
            )
        bundle = session.get(PLAYER_ORIGIN + bundle_match.group(0), timeout=15)
        bundle.raise_for_status()
        text = bundle.text

        app_id_match = _APP_ID_RE.search(text)
        if not app_id_match:
            raise AuthError(
                "Could not extract a Qobuz app_id from the web player bundle. "
                "You can paste an app_id manually in Preferences."
            )
        app_id = app_id_match.group("app_id")

        seeds = {m.group("timezone").lower(): m.group("seed") for m in _SEED_RE.finditer(text)}
        secret = ""
        for m in _INFO_RE.finditer(text):
            seed = seeds.get(m.group("timezone").lower())
            if not seed:
                continue
            candidate = seed + m.group("info") + m.group("extras")
            try:
                decoded = base64.b64decode(candidate)[:-_SECRET_SUFFIX_LEN].decode("utf-8")
            except (binascii.Error, UnicodeDecodeError):
                continue
            secret = decoded  # last valid candidate wins, mirroring streamrip
        if not secret:
            log.warning("Could not derive a Qobuz app secret from the bundle; signed endpoints will fail")
        return app_id, secret
    except requests.RequestException as exc:
        raise AuthError(
            "Could not reach Qobuz to auto-detect app credentials. "
            "You can paste an app_id manually in Preferences."
        ) from exc


def _parse_year(date_str: Any) -> int | None:
    if not isinstance(date_str, str) or len(date_str) < 4:
        return None
    head = date_str[:4]
    return int(head) if head.isdigit() else None


def _first_str(*values: Any) -> str | None:
    for v in values:
        if isinstance(v, str) and v:
            return v
    return None


def _strip_html(text: str) -> str:
    """Flatten Qobuz's HTML biography (``<p>…</p><br/>``) to plain paragraphs."""
    text = re.sub(r"(?i)<\s*br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</\s*p\s*>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class QobuzProvider(MusicProvider):
    service = Service.QOBUZ

    def __init__(self, settings: Settings, credentials: CredentialStore) -> None:
        self._settings = settings
        self._credentials = credentials
        self._session = requests.Session()
        # Pass contact_email through explicitly rather than letting
        # user_agent() load Settings off disk itself: construction must stay
        # I/O-free (see the comment below), and `settings` is already the
        # in-memory object the caller loaded at startup.
        self._session.headers["User-Agent"] = user_agent(settings.contact_email)
        self._app_id = ""
        self._app_secret = ""
        self._auth_token: str | None = None
        self._display_name: str | None = None

        # Per docs/ARCHITECTURE.md's threading rule, construction must be
        # cheap and I/O-free (no network, no keyring) since providers get
        # built on the main thread at startup. Scraping the web player's
        # app_id/secret and reading the cached auth token from the keyring
        # both used to happen right here; they're now deferred to
        # ``_ensure_ready``, run at most once, lazily, from the first actual
        # request/authenticate call -- which per the same threading rule
        # always happens on a worker thread already.
        self._ready = False
        self._ready_lock = threading.Lock()

    # -- app credentials / auth ---------------------------------------------

    def _ensure_ready(self) -> None:
        """One-time, lazy setup: app credentials + any cached auth token.

        Never called from ``__init__`` or ``is_authenticated`` -- both must
        stay synchronous-safe. Everything that actually talks to Qobuz
        (``_request``) or that needs ``self._app_id`` before it can
        (``authenticate``/``_login``) calls this first; it's a no-op after
        the first successful run.
        """
        if self._ready:
            return
        with self._ready_lock:
            if self._ready:
                return
            self._ensure_app_credentials()
            if self._auth_token is None:
                self._auth_token = self._credentials.get(config.QOBUZ_TOKEN)
            self._ready = True

    def _ensure_app_credentials(self) -> None:
        if self._settings.qobuz_app_id:
            self._app_id = self._settings.qobuz_app_id
            self._app_secret = self._credentials.get(config.QOBUZ_APP_SECRET) or ""
            return
        self._app_id, self._app_secret = _scrape_app_credentials(self._session)
        self._settings.qobuz_app_id = self._app_id
        self._settings.save()
        if self._app_secret:
            self._credentials.set(config.QOBUZ_APP_SECRET, self._app_secret)

    @property
    def is_authenticated(self) -> bool:
        return bool(self._auth_token)

    @property
    def has_credentials(self) -> bool:
        """Cheap, I/O-free signal for "would authenticating be worth attempting?"

        Only inspects the in-memory ``Settings`` object already held since
        construction -- no keyring read, no network -- so callers like
        ``AppState._warm_up`` can skip an unconfigured account entirely
        instead of calling ``authenticate()`` and paying for (and logging)
        its failure. ``authenticate()`` itself still checks credentials too
        and still fails fast with zero I/O either way; this just lets a
        caller avoid the call altogether for the common "never set up
        Qobuz" case.

        In "token" mode the token itself lives in the keyring, and reading
        it here to check for presence would make this property I/O and
        defeat the point (same reason password mode deliberately never
        checks the keyring-stored password). Instead this trusts
        ``Settings.qobuz_token_saved`` -- a non-secret bool that ``prefs.py``
        sets the moment the user pastes something into the token row, right
        alongside the (keyring) write of the token itself. That mirrors what
        password mode already does structurally: it gates on the non-secret
        "did the user configure this" signal (``qobuz_email``), never on the
        secret's presence. Worst case if the two ever fall out of sync
        (e.g. a hand-edited settings.json) is a single doomed
        ``authenticate()`` call, exactly like an unconfigured password
        account already risks today.
        """
        if self._settings.qobuz_auth_kind == "token":
            return self._settings.qobuz_token_saved
        return bool(self._settings.qobuz_email)

    def authenticate(self) -> None:
        if self._settings.qobuz_auth_kind == "token":
            self._authenticate_with_token()
            return

        # Check for configured credentials *before* any scraping or network
        # work: _ensure_ready() may scrape play.qobuz.com plus a multi-MB
        # bundle.js (two 15s-timeout requests) to auto-detect app_id/secret
        # when none is pasted in Preferences, and an unconfigured account has
        # no use for those credentials anyway. This ordering is what makes
        # "no Qobuz account configured" fail instantly with zero I/O rather
        # than scraping the web player only to be told the same thing.
        email = self._settings.qobuz_email
        if not email:
            raise AuthError("Qobuz email/password are not configured in Preferences.")
        password = self._credentials.get(config.QOBUZ_PASSWORD)
        if not password:
            raise AuthError("Qobuz email/password are not configured in Preferences.")

        self._ensure_ready()
        # A cached token from a previous run is already loaded into
        # self._auth_token by _ensure_ready(). Trusting it forever would
        # mean never noticing a revoked/expired session until some unrelated
        # call 401s; but re-logging in unconditionally on every warm-up (the
        # bug this fixes) does a full network POST for an account that's
        # almost always still signed in. Split the difference: a cheap GET
        # to confirm the token still works beats both extremes.
        if self._auth_token and self._token_is_valid():
            return
        self._login(email, password)

    def _authenticate_with_token(self) -> None:
        """Token-mode ``authenticate()``: validate a pasted session token.

        For accounts with no password to hash in the first place -- e.g. the
        app owner's own Qobuz account, signed in via Google OAuth on Qobuz's
        side, which ``_login()``'s md5-of-password flow has nothing to work
        with. Everything past login already runs on the bearer token alone
        (``X-App-Id`` + ``X-User-Auth-Token`` on every request via
        ``_request``); this just skips straight to having one instead of
        minting it through ``user/login``. Never touches
        ``config.QOBUZ_PASSWORD`` and never calls ``_login()``.

        Mirrors the password path's "unconfigured fails with zero I/O"
        guarantee: reading the token from the credential store is the only
        thing that runs before bailing out on a missing token, so
        ``_ensure_ready()`` (which may scrape play.qobuz.com for the app_id)
        never fires for a token account that was never set up.
        """
        token = self._credentials.get(config.QOBUZ_TOKEN)
        if not token:
            raise AuthError(
                "No Qobuz session token is saved. In Preferences -> Accounts -> Qobuz, "
                "sign in at play.qobuz.com, copy the X-User-Auth-Token request header "
                "from devtools, and paste it into the token field."
            )
        self._auth_token = token
        # Still need the app_id for the X-App-Id header _token_is_valid()'s
        # request sends -- token mode skips the login step, not this.
        self._ensure_ready()
        if not self._token_is_valid():
            self._auth_token = None
            raise AuthError(
                "The saved Qobuz session token is no longer accepted. Sign in at "
                "play.qobuz.com again and paste a fresh token in Preferences."
            )

    def _token_is_valid(self) -> bool:
        """Cheaply confirm ``self._auth_token`` is still accepted by Qobuz.

        Uses ``user/get`` -- the lightest authed endpoint available, a small
        JSON response with no pagination -- with ``_retry_on_401=False`` so
        a dead token doesn't recurse back into ``authenticate()`` (the
        caller we're already inside). Any failure (expired token, network
        hiccup, rate limit) is treated as "can't confirm it's alive" rather
        than specifically "it's dead": the caller falls back to a real login
        attempt either way, which surfaces a clearer error (including a
        rate-limit one) if that's what's actually going on.
        """
        try:
            data = self._request("GET", "user/get", authed=True, _retry_on_401=False)
        except Exception:  # noqa: BLE001 - any failure means "not confirmed valid"
            return False
        user = data.get("user") if isinstance(data.get("user"), dict) else data
        display_name = user.get("display_name") if isinstance(user, dict) else None
        if display_name:
            self._display_name = display_name
        return True

    def _login(self, email: str, password: str) -> None:
        digest = hashlib.md5(password.encode("utf-8")).hexdigest()
        params = {"app_id": self._app_id, "username": email, "email": email, "password": digest}
        # POSTed as a form body, not GET query params: verified live that
        # Qobuz's login endpoint accepts POST identically to GET (same
        # validation, same response shape for a given app_id). Query params
        # end up embedded verbatim in urllib3's own exception text on any
        # transport failure (e.g. "Max retries exceeded with url:
        # /user/login?...&password=<md5>..."), which we then re-raise and log
        # -- a body isn't included there, so this removes the leak at the
        # source rather than relying solely on redaction as a backstop.
        data = self._request("POST", "user/login", data=params, authed=False)
        token = data.get("user_auth_token")
        if not token:
            raise AuthError("Qobuz login failed: no auth token in the response.")
        self._auth_token = token
        self._credentials.set(config.QOBUZ_TOKEN, token)
        self._display_name = (data.get("user") or {}).get("display_name")

    def account_name(self) -> str | None:
        return self._display_name

    # -- HTTP -----------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        authed: bool = True,
        _retry_on_401: bool = True,
    ) -> dict[str, Any]:
        """The single choke point every Qobuz HTTP call goes through."""
        self._ensure_ready()
        headers = {"X-App-Id": self._app_id}
        if authed:
            if not self._auth_token:
                raise AuthError("Not authenticated with Qobuz.")
            headers["X-User-Auth-Token"] = self._auth_token

        try:
            response = self._session.request(
                method, BASE_URL + path, params=params, data=data, headers=headers, timeout=20
            )
        except requests.RequestException as exc:
            # requests/urllib3 embed the full request URL - including any
            # query params - in their own exception text. That's normally
            # harmless, but the (now-former) GET login call put password's
            # md5 digest and the account email right there in the URL, so any
            # transport failure leaked them verbatim into this message - and
            # this message is both logged (with a traceback, by
            # tasks.run_async) and shown directly in the Preferences UI
            # (prefs.py's "Failed: {exc}"). Redact defensively regardless of
            # which HTTP method was used. Chaining "from exc" itself would
            # still leak: tasks.run_async's log.exception() renders the full
            # cause chain via exc_info, and the raw `exc` carries the
            # unredacted original text as __cause__ even though our own
            # message here is clean. Chain from a redacted stand-in instead.
            redacted = config.redact_secrets(str(exc))
            raise ProviderError(f"Qobuz request to {path} failed: {redacted}") from config.redact_exception(exc)

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            raise RateLimitedError(
                "Qobuz rate limit exceeded", retry_after=float(retry_after) if retry_after else None
            )
        if response.status_code == 401 and authed and _retry_on_401:
            log.info("Qobuz auth token rejected; re-authenticating once")
            self.authenticate()
            return self._request(method, path, params=params, data=data, authed=authed, _retry_on_401=False)
        if not response.ok:
            excerpt = config.redact_secrets(response.text[:200])
            raise ProviderError(f"Qobuz API error {response.status_code} on {path}: {excerpt}")
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError(f"Qobuz returned non-JSON response for {path}") from exc

    # -- normalisation ----------------------------------------------------

    def _track_from_raw(
        self,
        raw: dict[str, Any],
        *,
        album_ctx: dict[str, Any] | None = None,
        fallback_artist: str | None = None,
    ) -> Track:
        track_id = raw.get("id")
        if track_id is None:
            raise ProviderError("Qobuz track payload is missing an id")

        album = raw.get("album") or album_ctx or {}
        performer = raw.get("performer") or {}
        album_artist = album.get("artist") or {}
        artist_id = performer.get("id") or album_artist.get("id")
        album_id = album.get("id")
        # ``artist/page``'s ``top_tracks`` entries come back with
        # ``performer: None`` (verified live against Radiohead, artist_id
        # 43840) -- there's no per-track performer on that endpoint, so fall
        # back to the artist name the caller already knows (the payload's
        # own top-level ``name``) rather than leaving the track artist-less.
        artist_name = performer.get("name") or album_artist.get("name") or fallback_artist

        return Track(
            id=str(track_id),
            title=raw.get("title") or "",
            service=Service.QOBUZ,
            artists=[artist_name] if artist_name else [],
            artist_ids=[str(artist_id)] if artist_id else [],
            album=album.get("title"),
            album_id=str(album_id) if album_id else None,
            duration_s=raw.get("duration"),
            isrc=raw.get("isrc"),
            year=_parse_year(album.get("release_date_original") or album.get("release_date")),
            track_number=raw.get("track_number"),
            artwork_url=(album.get("image") or {}).get("large"),
            explicit=bool(raw.get("parental_warning")),
            play_count=None,
            raw=raw,
        )

    def _album_from_raw(self, raw: dict[str, Any]) -> Album:
        artist = raw.get("artist") or {}
        date = raw.get("release_date_original") or raw.get("release_date")
        return Album(
            id=str(raw.get("id")),
            title=raw.get("title") or "",
            service=Service.QOBUZ,
            artists=[artist["name"]] if artist.get("name") else [],
            artist_ids=[str(artist["id"])] if artist.get("id") else [],
            year=_parse_year(date),
            date=date or None,
            track_count=raw.get("tracks_count"),
            artwork_url=(raw.get("image") or {}).get("large"),
            raw=raw,
        )

    def _artist_from_raw(self, raw: dict[str, Any]) -> Artist:
        return Artist(id=str(raw.get("id")), name=raw.get("name") or "", service=Service.QOBUZ, raw=raw)

    def _playlist_from_raw(self, raw: dict[str, Any]) -> Playlist:
        owner = (raw.get("owner") or {}).get("name")
        image = raw.get("image") or raw.get("image_rectangle")
        if isinstance(image, list):
            artwork = image[0] if image else None
        else:
            artwork = image
        return Playlist(
            id=str(raw.get("id")),
            title=_first_str(raw.get("name"), raw.get("title")) or "",
            service=Service.QOBUZ,
            description=raw.get("description") or "",
            track_count=raw.get("tracks_count"),
            owner=owner,
            public=bool(raw.get("is_public")),
            artwork_url=artwork,
            raw=raw,
        )

    # -- search / browse ----------------------------------------------------

    def search(
        self, query: str, *, kinds: Sequence[str] = ("tracks",), limit: int = 25
    ) -> SearchResults:
        results = SearchResults()
        for kind in kinds:
            if kind not in _SEARCH_TYPES:
                log.warning("Unsupported Qobuz search kind %r; skipping", kind)
                continue
            data = self._request(
                "GET", "catalog/search", params={"query": query, "type": kind, "limit": limit, "offset": 0}
            )
            items = (data.get(kind) or {}).get("items", [])
            if kind == "tracks":
                results.tracks = [self._track_from_raw(t) for t in items]
            elif kind == "albums":
                results.albums = [self._album_from_raw(a) for a in items]
            elif kind == "artists":
                results.artists = [self._artist_from_raw(a) for a in items]
            elif kind == "playlists":
                results.playlists = [self._playlist_from_raw(p) for p in items]
        return results

    def get_track(self, track_id: str) -> Track:
        raw = self._request("GET", "track/get", params={"track_id": track_id})
        return self._track_from_raw(raw)

    def get_album_detail(self, album_id: str) -> Album:
        album = self._request("GET", "album/get", params={"album_id": album_id})
        return self._album_from_raw(album)

    def get_album_tracks(self, album_id: str) -> list[Track]:
        album = self._request("GET", "album/get", params={"album_id": album_id})
        items = (album.get("tracks") or {}).get("items", [])
        return [self._track_from_raw(t, album_ctx=album) for t in items]

    def get_artist_detail(self, artist_id: str) -> Artist:
        data = self._request("GET", "artist/get", params={"artist_id": artist_id, "extra": "biography"})
        image = data.get("image") or {}
        image_url = image.get("extralarge") or image.get("large") or image.get("medium")
        bio = data.get("biography") or {}
        summary = _strip_html(bio.get("content") or bio.get("summary") or "") if isinstance(bio, dict) else ""
        return Artist(
            id=str(data.get("id") or artist_id),
            name=data.get("name") or "",
            service=Service.QOBUZ,
            image_url=image_url,
            bio=summary,
            raw=data,
        )

    def get_artist_albums(self, artist_id: str, *, limit: int = 100) -> list[Album]:
        data = self._request(
            "GET", "artist/get", params={"artist_id": artist_id, "extra": "albums", "limit": limit}
        )
        items = (data.get("albums") or {}).get("items", [])
        return [self._album_from_raw(a) for a in items[:limit]]

    def get_artist_top_tracks(self, artist_id: str, *, limit: int = 20) -> list[Track]:
        # Qobuz calls this "most popular" in the web player, surfaced via
        # artist/page (not artist/get, which only carries extra=albums).
        # Verified live against Radiohead (artist_id 43840): the payload's
        # top-level "top_tracks" is a flat list of up to 25 track dicts with
        # "performer": None on every entry, so _track_from_raw's
        # fallback_artist picks up the payload's own top-level "name"
        # instead. There's no limit param on this endpoint, so we slice.
        data = self._request("GET", "artist/page", params={"artist_id": artist_id, "sort": "relevant"})
        items = data.get("top_tracks") or []
        artist_name = data.get("name")
        return [self._track_from_raw(t, fallback_artist=artist_name) for t in items[:limit]]

    # -- playlists ----------------------------------------------------------

    def list_playlists(self) -> list[Playlist]:
        data = self._request("GET", "playlist/getUserPlaylists", params={"limit": _PAGE_SIZE, "offset": 0})
        items = (data.get("playlists") or {}).get("items", [])
        return [self._playlist_from_raw(p) for p in items]

    def get_playlist(self, playlist_id: str) -> Playlist:
        raw = self._request("GET", "playlist/get", params={"playlist_id": playlist_id})
        return self._playlist_from_raw(raw)

    def _iter_playlist_track_pages(self, playlist_id: str) -> Iterator[list[dict[str, Any]]]:
        offset = 0
        while True:
            data = self._request(
                "GET",
                "playlist/get",
                params={"playlist_id": playlist_id, "extra": "tracks", "limit": _PAGE_SIZE, "offset": offset},
            )
            tracks_section = data.get("tracks") or {}
            items = tracks_section.get("items", [])
            if not items:
                return
            yield items
            offset += len(items)
            total = tracks_section.get("total", offset)
            if offset >= total or len(items) < _PAGE_SIZE:
                return

    def get_playlist_tracks(self, playlist_id: str) -> list[Track]:
        tracks: list[Track] = []
        for items in self._iter_playlist_track_pages(playlist_id):
            tracks.extend(self._track_from_raw(t) for t in items)
        return tracks

    def create_playlist(self, title: str, description: str = "", public: bool = False) -> Playlist:
        raw = self._request(
            "POST",
            "playlist/create",
            data={
                "name": title,
                "description": description,
                "is_public": "true" if public else "false",
                "is_collaborative": "false",
            },
        )
        return self._playlist_from_raw(raw)

    def add_tracks(self, playlist_id: str, track_ids: Sequence[str]) -> None:
        for chunk in _chunked(list(track_ids), _ADD_CHUNK_SIZE):
            self._request(
                "POST",
                "playlist/addTracks",
                data={"playlist_id": playlist_id, "track_ids": ",".join(str(t) for t in chunk)},
            )

    def remove_tracks(self, playlist_id: str, track_ids: Sequence[str]) -> None:
        # playlist/deleteTracks wants *playlist track* ids, not catalog track
        # ids, so we have to look them up from the playlist contents first.
        wanted = {str(t) for t in track_ids}
        playlist_track_ids: list[Any] = []
        for items in self._iter_playlist_track_pages(playlist_id):
            for item in items:
                if str(item.get("id")) in wanted and item.get("playlist_track_id") is not None:
                    playlist_track_ids.append(item["playlist_track_id"])
        if not playlist_track_ids:
            return
        for chunk in _chunked(playlist_track_ids, _ADD_CHUNK_SIZE):
            self._request(
                "POST",
                "playlist/deleteTracks",
                data={"playlist_id": playlist_id, "playlist_track_ids": ",".join(str(i) for i in chunk)},
            )

    def delete_playlist(self, playlist_id: str) -> None:
        self._request("POST", "playlist/delete", data={"playlist_id": playlist_id})

    def rename_playlist(self, playlist_id: str, title: str, description: str | None = None) -> None:
        data: dict[str, Any] = {"playlist_id": playlist_id, "name": title}
        if description is not None:
            data["description"] = description
        self._request("POST", "playlist/update", data=data)

    # -- discovery ------------------------------------------------------

    def similar_tracks(self, track: Track, *, limit: int = 20) -> list[Track]:
        raise NotSupportedError(
            "Qobuz has no native 'similar tracks' endpoint; use the Last.fm-backed recommender instead."
        )

    def liked_tracks(self, *, limit: int = 500) -> list[Track]:
        tracks: list[Track] = []
        offset = 0
        while len(tracks) < limit:
            page = min(_PAGE_SIZE, limit - len(tracks))
            data = self._request(
                "GET", "favorite/getUserFavorites", params={"type": "tracks", "limit": page, "offset": offset}
            )
            items = (data.get("tracks") or {}).get("items", [])
            if not items:
                break
            tracks.extend(self._track_from_raw(t) for t in items)
            offset += len(items)
            if len(items) < page:
                break
        return tracks[:limit]

    # -- streaming --------------------------------------------------------

    def resolve_stream(self, track_id: str, *, max_quality: bool = False) -> StreamSource:
        """Resolve a playable stream URL via Qobuz's signed ``track/getFileUrl``.

        Unlike every other endpoint this provider calls, ``getFileUrl`` requires
        a per-request md5 signature (see ``request_sig``) alongside the usual
        ``X-App-Id``/``X-User-Auth-Token`` headers, since it's what actually
        grants access to (rights-limited, format-selected) audio bytes rather
        than catalog metadata.

        ``format_id`` 6 is Qobuz's FLAC 16-bit/44.1kHz tier -- lossless but not
        the hi-res tiers, which keeps bandwidth/decoder requirements broadly
        compatible with LAN playback targets like the WiiM. ``max_quality``
        requests tier 27 (FLAC up to 24-bit/192kHz); Qobuz transparently hands
        back the best the track and subscription actually allow (reported in the
        response's ``bit_depth``/``sampling_rate``), so this is "highest
        available" rather than a fixed 24/192.
        """
        self._ensure_ready()
        if not self._app_secret:
            raise ProviderError("Qobuz app secret unavailable; cannot sign a stream request.")
        format_id = 27 if max_quality else 6  # 27: FLAC ≤24-bit/192kHz; 6: FLAC 16/44.1
        ts = int(time.time())
        sig_params = {"format_id": format_id, "intent": "stream", "track_id": str(track_id)}
        sig = request_sig("track", "getFileUrl", sig_params, ts, self._app_secret)
        data = self._request(
            "GET",
            "track/getFileUrl",
            params={
                "request_ts": ts,
                "request_sig": sig,
                "track_id": str(track_id),
                "format_id": format_id,
                "intent": "stream",
            },
        )
        url = data.get("url")
        if not url:
            # e.g. {"restrictions": [{"code": "..."}]} for a non-streamable
            # or unentitled track (wrong subscription tier, region lock, ...).
            raise ProviderError(
                f"Qobuz returned no stream URL for track {track_id} "
                "(needs a streaming subscription / not available)."
            )
        mime = data.get("mime_type") or "audio/flac"
        container = "flac" if "flac" in mime else ("mp3" if "mp" in mime else None)
        # Qobuz reports sampling_rate in kHz (44.1, 96, 192) and bit_depth in
        # bits; ``:g`` drops the trailing ".0" on integer rates.
        bit_depth, sampling_rate = data.get("bit_depth"), data.get("sampling_rate")
        label = (
            f"FLAC {bit_depth}/{sampling_rate:g}kHz"
            if bit_depth and sampling_rate
            else (mime or None)
        )
        return StreamSource(url=url, mime_type=mime, container=container, label=label)
