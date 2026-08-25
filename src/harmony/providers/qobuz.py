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
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

import requests

from .. import config
from ..config import CredentialStore, Settings, user_agent
from ..errors import AuthError, NotSupportedError, ProviderError, RateLimitedError
from ..models import Album, Artist, Playlist, SearchResults, Service, Track
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


class QobuzProvider(MusicProvider):
    service = Service.QOBUZ

    def __init__(self, settings: Settings, credentials: CredentialStore) -> None:
        self._settings = settings
        self._credentials = credentials
        self._session = requests.Session()
        self._session.headers["User-Agent"] = user_agent()
        self._app_id = ""
        self._app_secret = ""
        self._auth_token: str | None = None
        self._display_name: str | None = None

        self._ensure_app_credentials()
        self._auth_token = self._credentials.get(config.QOBUZ_TOKEN)

    # -- app credentials / auth ---------------------------------------------

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

    def authenticate(self) -> None:
        email = self._settings.qobuz_email
        password = self._credentials.get(config.QOBUZ_PASSWORD)
        if not email or not password:
            raise AuthError("Qobuz email/password are not configured in Preferences.")
        self._login(email, password)

    def _login(self, email: str, password: str) -> None:
        digest = hashlib.md5(password.encode("utf-8")).hexdigest()
        params = {"app_id": self._app_id, "username": email, "email": email, "password": digest}
        data = self._request("GET", "user/login", params=params, authed=False)
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
            raise ProviderError(f"Qobuz request to {path} failed: {exc}") from exc

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
            excerpt = response.text[:200]
            raise ProviderError(f"Qobuz API error {response.status_code} on {path}: {excerpt}")
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError(f"Qobuz returned non-JSON response for {path}") from exc

    # -- normalisation ----------------------------------------------------

    def _track_from_raw(self, raw: dict[str, Any], *, album_ctx: dict[str, Any] | None = None) -> Track:
        track_id = raw.get("id")
        if track_id is None:
            raise ProviderError("Qobuz track payload is missing an id")

        album = raw.get("album") or album_ctx or {}
        performer = raw.get("performer") or {}
        album_artist = album.get("artist") or {}
        artist_name = performer.get("name") or album_artist.get("name")

        return Track(
            id=str(track_id),
            title=raw.get("title") or "",
            service=Service.QOBUZ,
            artists=[artist_name] if artist_name else [],
            album=album.get("title"),
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
        return Album(
            id=str(raw.get("id")),
            title=raw.get("title") or "",
            service=Service.QOBUZ,
            artists=[artist["name"]] if artist.get("name") else [],
            year=_parse_year(raw.get("release_date_original") or raw.get("release_date")),
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

    def get_album_tracks(self, album_id: str) -> list[Track]:
        album = self._request("GET", "album/get", params={"album_id": album_id})
        items = (album.get("tracks") or {}).get("items", [])
        return [self._track_from_raw(t, album_ctx=album) for t in items]

    def get_artist_albums(self, artist_id: str, *, limit: int = 100) -> list[Album]:
        data = self._request(
            "GET", "artist/get", params={"artist_id": artist_id, "extra": "albums", "limit": limit}
        )
        items = (data.get("albums") or {}).get("items", [])
        return [self._album_from_raw(a) for a in items[:limit]]

    def get_artist_top_tracks(self, artist_id: str, *, limit: int = 20) -> list[Track]:
        # Qobuz's reverse-engineered API has no "top tracks" endpoint (only
        # artist/get with extra=albums, which we already use for
        # get_artist_albums). Approximating "top" from album order would be
        # actively misleading downstream in matching/sync, so this is
        # unsupported rather than silently wrong.
        raise NotSupportedError("Qobuz has no top-tracks endpoint for artists.")

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
