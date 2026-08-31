"""YouTube Music provider, wrapping ``ytmusicapi.YTMusic``.

Unauthenticated construction is deliberately allowed: ``YTMusic()`` with no
auth still supports public ``search()``, so the app can show search results
before the user has signed in.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TypeVar

import requests
from ytmusicapi import OAuthCredentials, YTMusic
from ytmusicapi.exceptions import YTMusicServerError, YTMusicUserError

from .. import config
from ..config import CredentialStore, Settings
from ..errors import AuthError, NotSupportedError, ProviderError, RateLimitedError
from ..models import Album, Artist, Playlist, SearchResults, Service, StreamSource, Track
from .base import MusicProvider, _chunked, retry_on_rate_limit

log = logging.getLogger(__name__)

T = TypeVar("T")

# search() kind -> ytmusicapi search filter
_SEARCH_FILTERS = {
    "tracks": "songs",
    "albums": "albums",
    "artists": "artists",
    "playlists": "playlists",
}

_ADD_CHUNK_SIZE = 100
_ADD_CHUNK_SLEEP_S = 0.5


def _parse_duration(text: str | None) -> int | None:
    """Parse a ``"H:MM:SS"``/``"M:SS"`` duration string into whole seconds."""
    if not text:
        return None
    parts = text.split(":")
    try:
        numbers = [int(p) for p in parts]
    except ValueError:
        return None
    seconds = 0
    for n in numbers:
        seconds = seconds * 60 + n
    return seconds


def _parse_play_count(raw: Any) -> int | None:
    """Parse a play/view count.

    Only handles plain comma-grouped digits ("12,345"); abbreviated forms
    ("1.2M") are intentionally left as ``None`` rather than guessed at, per
    the normalisation contract.
    """
    if isinstance(raw, int):
        return raw
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip().replace(",", "")
    return int(cleaned) if cleaned.isdigit() else None


def _safe_int(raw: Any) -> int | None:
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip())
    return None


def _artist_names(raw: Any) -> list[str]:
    if not raw:
        return []
    names = []
    for item in raw:
        if isinstance(item, dict) and item.get("name"):
            names.append(item["name"])
        elif isinstance(item, str):
            names.append(item)
    return names


def _album_title(raw: Any) -> str | None:
    if isinstance(raw, dict):
        return raw.get("name") or raw.get("title")
    if isinstance(raw, str):
        return raw
    return None


def _largest_thumbnail(raw: Any) -> str | None:
    if isinstance(raw, dict):
        raw = [raw]
    if not raw:
        return None
    try:
        best = max(raw, key=lambda t: t.get("width") or 0)
    except (TypeError, AttributeError):
        return None
    return best.get("url")


def _pick_audio_format(info: dict[str, Any], *, prefer_highest: bool = False) -> dict[str, Any]:
    """Pick the best audio-only format out of a yt-dlp ``extract_info`` result.

    Default preference order: itag 140 (YouTube's standard 128kbps AAC/m4a,
    present on almost every video and a safe default for LAN playback targets)
    beats the highest-bitrate audio-only m4a, which beats any other audio-only
    format. With ``prefer_highest`` (the in-app local player, which can decode
    anything GStreamer handles) the itag-140 shortcut is skipped and the
    single highest-bitrate audio-only stream wins outright -- often Opus/WebM,
    which carries more bits per second than the 128kbps AAC default. Pure and
    network-free so it's cheap to unit test against canned ``formats`` lists.
    Raises ``ProviderError`` if nothing audio-only is available.
    """
    candidates: list[dict[str, Any]] = info.get("requested_formats") or info.get("formats") or []
    audio_only = [f for f in candidates if f.get("acodec") not in (None, "none") and f.get("vcodec") == "none"]
    if not audio_only:
        raise ProviderError("No audio-only format found in yt-dlp's extracted info.")

    if prefer_highest:
        return max(audio_only, key=lambda f: f.get("abr") or 0)

    for fmt in audio_only:
        if fmt.get("format_id") == "140":
            return fmt

    m4a = [f for f in audio_only if f.get("ext") == "m4a"]
    pool = m4a or audio_only
    return max(pool, key=lambda f: f.get("abr") or 0)


# yt-dlp ``ext`` -> (mime, container) for the audio-only formats YouTube serves.
_YT_EXT_MIME = {
    "m4a": ("audio/mp4", "m4a"),
    "webm": ("audio/webm", "webm"),
    "opus": ("audio/ogg", "opus"),
    "mp3": ("audio/mpeg", "mp3"),
}

# yt-dlp ``acodec`` prefix -> friendly codec name for the stream quality label.
_YT_CODEC_NAMES = {"mp4a": "AAC", "opus": "Opus", "vorbis": "Vorbis", "mp3": "MP3", "flac": "FLAC"}


def _stream_label(fmt: dict[str, Any]) -> str:
    """A human quality label for a chosen audio format: codec + bitrate when
    known (``"AAC 128kbps"``), else codec + itag (``"AAC (itag 140)"``)."""
    prefix = (fmt.get("acodec") or "").split(".")[0]
    codec = _YT_CODEC_NAMES.get(prefix, prefix or fmt.get("ext") or "audio")
    abr = fmt.get("abr")
    return f"{codec} {round(abr)}kbps" if abr else f"{codec} (itag {fmt.get('format_id')})"


class YTMusicProvider(MusicProvider):
    service = Service.YTMUSIC

    def __init__(self, settings: Settings, credentials: CredentialStore) -> None:
        self._settings = settings
        self._credentials = credentials
        self._authenticated = False
        self._yt: YTMusic = self._build_client()

    # -- construction / auth -----------------------------------------------

    def _build_client(self) -> YTMusic:
        self._authenticated = False
        auth_file = self._settings.ytmusic_auth_file
        if not auth_file or not Path(auth_file).exists():
            return YTMusic()

        try:
            if self._settings.ytmusic_auth_kind == "oauth":
                client_id = self._settings.ytmusic_oauth_client_id
                client_secret = self._credentials.get(config.YTMUSIC_OAUTH_SECRET)
                if not client_id or not client_secret:
                    log.warning(
                        "YTMusic oauth.json is configured but client id/secret are missing; "
                        "falling back to unauthenticated search"
                    )
                    return YTMusic()
                oauth_credentials = OAuthCredentials(client_id=client_id, client_secret=client_secret)
                yt = YTMusic(auth=auth_file, oauth_credentials=oauth_credentials)
            else:
                yt = YTMusic(auth=auth_file)
        except Exception as exc:  # noqa: BLE001 - any bad/stale auth file must not crash the app
            log.warning("Failed to load YTMusic auth from %s: %s", auth_file, exc)
            return YTMusic()

        self._authenticated = True
        return yt

    @property
    def is_authenticated(self) -> bool:
        return self._authenticated

    def authenticate(self) -> None:
        if not self._settings.ytmusic_auth_file:
            raise AuthError(
                "No YouTube Music auth file configured. Use Preferences to run "
                "'ytmusicapi setup' (browser cookies) or link an OAuth app first."
            )
        self._yt = self._build_client()
        if not self._authenticated:
            raise AuthError("Failed to authenticate with YouTube Music using the configured auth file.")

    def account_name(self) -> str | None:
        if not self._authenticated:
            return None
        try:
            info = self._call(self._yt.get_account_info)
        except Exception as exc:  # noqa: BLE001 - a stale session makes ytmusicapi's
            # navigator raise a bare KeyError (no account header in a signed-out
            # response), not just ProviderError; never let that crash a status read.
            log.debug("Could not fetch YTMusic account info: %s", exc)
            return None
        return info.get("accountName")

    # -- call wrapper ---------------------------------------------------

    def _call(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Run a raw ``ytmusicapi`` call, translating its exceptions to ours."""
        try:
            return fn(*args, **kwargs)
        except YTMusicUserError as exc:
            raise AuthError(str(exc)) from exc
        except YTMusicServerError as exc:
            message = str(exc)
            if "429" in message:
                raise RateLimitedError(message) from exc
            raise ProviderError(message) from exc
        except requests.RequestException as exc:
            raise ProviderError(f"YouTube Music request failed: {exc}") from exc

    # -- normalisation ----------------------------------------------------

    def _track_from_raw(self, raw: dict[str, Any], *, fallback_artist: str | None = None) -> Track:
        video_id = raw.get("videoId") or raw.get("id")
        if not video_id:
            raise ProviderError("YTMusic track payload is missing a videoId")

        duration_s = raw.get("duration_seconds")
        if duration_s is None:
            duration_s = _parse_duration(raw.get("duration") or raw.get("length"))

        artists = _artist_names(raw.get("artists"))
        if not artists:
            single = raw.get("artist")
            if isinstance(single, str) and single:
                artists = [single]
            elif fallback_artist:
                artists = [fallback_artist]

        return Track(
            id=str(video_id),
            title=raw.get("title") or "",
            service=Service.YTMUSIC,
            artists=artists,
            album=_album_title(raw.get("album")),
            duration_s=duration_s,
            isrc=None,
            year=_safe_int(raw.get("year")),
            track_number=raw.get("trackNumber"),
            artwork_url=_largest_thumbnail(raw.get("thumbnails") or raw.get("thumbnail")),
            explicit=bool(raw.get("isExplicit")),
            play_count=_parse_play_count(raw.get("playCount")),
            raw=raw,
        )

    def _album_from_raw(self, raw: dict[str, Any], *, fallback_artist: str | None = None) -> Album:
        browse_id = raw.get("browseId") or raw.get("id")
        artists = _artist_names(raw.get("artists"))
        if not artists and fallback_artist:
            artists = [fallback_artist]
        return Album(
            id=str(browse_id),
            title=raw.get("title") or "",
            service=Service.YTMUSIC,
            artists=artists,
            year=_safe_int(raw.get("year")),
            track_count=_safe_int(raw.get("trackCount")),
            artwork_url=_largest_thumbnail(raw.get("thumbnails")),
            raw=raw,
        )

    def _artist_from_raw(self, raw: dict[str, Any]) -> Artist:
        browse_id = raw.get("browseId") or raw.get("channelId") or raw.get("id")
        name = raw.get("artist") or raw.get("title") or raw.get("name") or ""
        return Artist(id=str(browse_id), name=name, service=Service.YTMUSIC, raw=raw)

    def _playlist_from_raw(self, raw: dict[str, Any]) -> Playlist:
        playlist_id = raw.get("playlistId") or raw.get("id")
        author = raw.get("author")
        if isinstance(author, list):
            owner = ", ".join(_artist_names(author)) or None
        elif isinstance(author, dict):
            owner = author.get("name")
        elif isinstance(author, str):
            owner = author
        else:
            owner = None
        track_count = _safe_int(raw.get("trackCount"))
        if track_count is None:
            track_count = _safe_int(raw.get("itemCount"))
        if track_count is None:
            track_count = _safe_int(raw.get("count"))
        return Playlist(
            id=str(playlist_id),
            title=raw.get("title") or "",
            service=Service.YTMUSIC,
            description=raw.get("description") or "",
            track_count=track_count,
            owner=owner,
            public=raw.get("privacy") == "PUBLIC",
            artwork_url=_largest_thumbnail(raw.get("thumbnails")),
            raw=raw,
        )

    # -- search / browse ----------------------------------------------------

    @retry_on_rate_limit()
    def search(
        self, query: str, *, kinds: Sequence[str] = ("tracks",), limit: int = 25
    ) -> SearchResults:
        results = SearchResults()
        for kind in kinds:
            filter_ = _SEARCH_FILTERS.get(kind)
            if filter_ is None:
                log.warning("Unsupported YTMusic search kind %r; skipping", kind)
                continue
            raw_results = self._call(self._yt.search, query, filter=filter_, limit=limit)
            if kind == "tracks":
                results.tracks = [self._track_from_raw(r) for r in raw_results if r.get("videoId")]
            elif kind == "albums":
                results.albums = [self._album_from_raw(r) for r in raw_results]
            elif kind == "artists":
                results.artists = [self._artist_from_raw(r) for r in raw_results]
            elif kind == "playlists":
                results.playlists = [self._playlist_from_raw(r) for r in raw_results]
        return results

    def get_track(self, track_id: str) -> Track:
        watch = self._call(self._yt.get_watch_playlist, videoId=track_id, limit=1)
        for raw in watch.get("tracks", []):
            if raw.get("videoId") == track_id:
                return self._track_from_raw(raw)
        raise ProviderError(f"YouTube Music track {track_id!r} could not be resolved")

    def get_album_tracks(self, album_id: str) -> list[Track]:
        album = self._call(self._yt.get_album, album_id)
        tracks = []
        for raw in album.get("tracks", []):
            track = self._track_from_raw(raw, fallback_artist=_album_artist_name(album))
            if track.artwork_url is None:
                track.artwork_url = _largest_thumbnail(album.get("thumbnails"))
            if not track.album:
                track.album = album.get("title")
            tracks.append(track)
        return tracks

    def get_artist_albums(self, artist_id: str, *, limit: int = 100) -> list[Album]:
        artist = self._call(self._yt.get_artist, artist_id)
        artist_name = artist.get("name")
        albums: list[Album] = []
        for category in ("albums", "singles"):
            section = artist.get(category)
            if not section:
                continue
            if "browseId" in section and "params" in section:
                raw_list = self._call(
                    self._yt.get_artist_albums, section["browseId"], section["params"], limit=limit
                )
            else:
                raw_list = section.get("results", [])
            albums.extend(self._album_from_raw(r, fallback_artist=artist_name) for r in raw_list)
        return albums[:limit] if limit else albums

    def get_artist_top_tracks(self, artist_id: str, *, limit: int = 20) -> list[Track]:
        artist = self._call(self._yt.get_artist, artist_id)
        artist_name = artist.get("name")
        songs = artist.get("songs") or {}
        if "browseId" in songs:
            raw_tracks = self._call(self._yt.get_playlist, songs["browseId"], limit=limit).get("tracks", [])
        else:
            raw_tracks = songs.get("results", [])
        tracks = [self._track_from_raw(r, fallback_artist=artist_name) for r in raw_tracks]
        return tracks[:limit]

    # -- playlists ----------------------------------------------------------

    def list_playlists(self) -> list[Playlist]:
        if not self._authenticated:
            raise AuthError("Sign in to YouTube Music to view your playlists.")
        raw_playlists = self._call(self._yt.get_library_playlists, limit=None)
        return [self._playlist_from_raw(r) for r in raw_playlists]

    def get_playlist(self, playlist_id: str) -> Playlist:
        raw = self._call(self._yt.get_playlist, playlist_id, limit=0)
        return self._playlist_from_raw(raw)

    def get_playlist_tracks(self, playlist_id: str) -> list[Track]:
        raw = self._call(self._yt.get_playlist, playlist_id, limit=None)
        return [self._track_from_raw(t) for t in raw.get("tracks", [])]

    def create_playlist(self, title: str, description: str = "", public: bool = False) -> Playlist:
        privacy_status = "PUBLIC" if public else "PRIVATE"
        result = self._call(self._yt.create_playlist, title, description, privacy_status=privacy_status)
        if not isinstance(result, str):
            raise ProviderError(f"YouTube Music rejected playlist creation: {result}")
        return Playlist(
            id=result,
            title=title,
            service=Service.YTMUSIC,
            description=description,
            track_count=0,
            owner=self.account_name(),
            public=public,
        )

    @retry_on_rate_limit()
    def add_tracks(self, playlist_id: str, track_ids: Sequence[str]) -> None:
        chunks = list(_chunked(list(track_ids), _ADD_CHUNK_SIZE))
        for i, chunk in enumerate(chunks):
            self._call(self._yt.add_playlist_items, playlist_id, chunk)
            if i < len(chunks) - 1:
                time.sleep(_ADD_CHUNK_SLEEP_S)

    def remove_tracks(self, playlist_id: str, track_ids: Sequence[str]) -> None:
        wanted = set(track_ids)
        raw_playlist = self._call(self._yt.get_playlist, playlist_id, limit=None)
        matches = [t for t in raw_playlist.get("tracks", []) if t.get("videoId") in wanted]
        if not matches:
            return
        if any(not t.get("setVideoId") for t in matches):
            raise NotSupportedError(
                "Cannot remove one or more tracks: YouTube Music did not provide a setVideoId "
                "for them (you likely don't own this playlist)."
            )
        self._call(self._yt.remove_playlist_items, playlist_id, matches)

    def delete_playlist(self, playlist_id: str) -> None:
        self._call(self._yt.delete_playlist, playlist_id)

    def rename_playlist(self, playlist_id: str, title: str, description: str | None = None) -> None:
        self._call(self._yt.edit_playlist, playlist_id, title=title, description=description)

    # -- discovery ------------------------------------------------------

    def similar_tracks(self, track: Track, *, limit: int = 20) -> list[Track]:
        watch = self._call(self._yt.get_watch_playlist, videoId=track.id, radio=True, limit=limit + 1)
        raw_tracks = [t for t in watch.get("tracks", []) if t.get("videoId") != track.id]
        return [self._track_from_raw(t) for t in raw_tracks[:limit]]

    def liked_tracks(self, *, limit: int = 500) -> list[Track]:
        if not self._authenticated:
            raise AuthError("Sign in to YouTube Music to view your liked songs.")
        raw = self._call(self._yt.get_liked_songs, limit=limit)
        return [self._track_from_raw(t) for t in raw.get("tracks", [])]

    # -- streaming --------------------------------------------------------

    def resolve_stream(self, track_id: str, *, max_quality: bool = False) -> StreamSource:
        """Resolve a playable stream URL for a videoId via yt-dlp.

        YouTube Music has no clean, stable stream-URL API of its own, so this
        shells out to yt-dlp's extractor -- the same approach every third-party
        YouTube player uses. yt-dlp is an optional runtime dependency (not
        imported at module scope) so importing this module doesn't force it on
        callers who never resolve a stream.

        ``max_quality`` widens yt-dlp's format filter to the best audio-only
        stream (any codec) instead of pinning the LAN-friendly AAC default --
        for the in-app player, which decodes whatever GStreamer supports.
        """
        try:
            import yt_dlp  # lazy: optional runtime dependency
        except ImportError as exc:
            raise NotSupportedError(
                "Playing YouTube Music to a device needs yt-dlp (pip install yt-dlp)."
            ) from exc

        url = f"https://music.youtube.com/watch?v={track_id}"
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "format": "bestaudio/best" if max_quality else "140/bestaudio[ext=m4a]/bestaudio",
            "noplaylist": True,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:  # noqa: BLE001 - yt_dlp raises its own DownloadError et al.
            raise ProviderError(f"Could not resolve a YouTube stream for {track_id}: {exc}") from exc

        fmt = _pick_audio_format(info, prefer_highest=max_quality)
        stream_url = fmt.get("url")
        if not stream_url:
            raise ProviderError(f"YouTube returned no playable audio URL for {track_id}.")
        mime, container = _YT_EXT_MIME.get(fmt.get("ext") or "", ("audio/mp4", "m4a"))
        return StreamSource(
            url=stream_url,
            mime_type=mime,
            container=container,
            headers=dict(fmt.get("http_headers") or {}),
            label=_stream_label(fmt),
        )


def _album_artist_name(album: dict[str, Any]) -> str | None:
    names = _artist_names(album.get("artists"))
    return names[0] if names else None
