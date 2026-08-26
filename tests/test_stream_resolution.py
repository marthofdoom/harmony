"""Offline unit tests for ``MusicProvider.resolve_stream`` and its backends.

Nothing here touches the network, keyring, or disk: QobuzProvider is built
with a pre-set ``qobuz_app_id`` (skips bundle scraping) and its ``_request``
choke point is monkeypatched directly; YTMusicProvider is built with no auth
file (I/O-free per ``YTMusic()``'s own contract) and yt-dlp is stubbed via a
fake module injected into ``sys.modules`` rather than the real optional
dependency.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

import pytest

from harmony.config import Settings
from harmony.errors import NotSupportedError, ProviderError
from harmony.models import StreamSource
from harmony.providers.base import MusicProvider
from harmony.providers.qobuz import QobuzProvider, request_sig
from harmony.providers.ytmusic import YTMusicProvider, _pick_audio_format


class FakeCredentialStore:
    """Minimal stand-in for ``harmony.config.CredentialStore`` — no keyring, no disk."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self.get_calls: list[str] = []

    def get(self, key: str) -> str | None:
        self.get_calls.append(key)
        return self._data.get(key)

    def set(self, key: str, value: str) -> None:
        self._data[key] = value

    def delete(self, key: str) -> None:
        self._data.pop(key, None)


@pytest.fixture
def credentials() -> FakeCredentialStore:
    return FakeCredentialStore()


@pytest.fixture
def qobuz_provider(credentials: FakeCredentialStore) -> QobuzProvider:
    settings = Settings(qobuz_app_id="test_app_id")  # non-blank -> skips bundle scraping
    provider = QobuzProvider(settings, credentials)
    # Fast-forward past the lazy _ensure_ready() setup this test doesn't care about.
    provider._app_id = "test_app_id"
    provider._app_secret = "test_app_secret"
    provider._auth_token = "test_auth_token"
    provider._ready = True
    return provider


@pytest.fixture
def yt_provider(credentials: FakeCredentialStore) -> YTMusicProvider:
    settings = Settings()  # ytmusic_auth_file="" -> unauthenticated, no I/O
    return YTMusicProvider(settings, credentials)


# --------------------------------------------------------------------------
# base.py — default resolve_stream
# --------------------------------------------------------------------------


class TestBaseDefaultResolveStream:
    def test_default_raises_not_supported(self) -> None:
        class _StubProvider(MusicProvider):
            """Bare-minimum concrete subclass that doesn't override resolve_stream."""

            @property
            def is_authenticated(self) -> bool:
                return False

            def authenticate(self) -> None: ...
            def account_name(self) -> str | None:
                return None

            def search(self, query, *, kinds=("tracks",), limit=25):
                raise NotImplementedError

            def get_track(self, track_id): raise NotImplementedError
            def get_album_tracks(self, album_id): raise NotImplementedError
            def get_artist_albums(self, artist_id, *, limit=100): raise NotImplementedError
            def get_artist_top_tracks(self, artist_id, *, limit=20): raise NotImplementedError
            def list_playlists(self): raise NotImplementedError
            def get_playlist(self, playlist_id): raise NotImplementedError
            def get_playlist_tracks(self, playlist_id): raise NotImplementedError
            def create_playlist(self, title, description="", public=False): raise NotImplementedError
            def add_tracks(self, playlist_id, track_ids): raise NotImplementedError
            def remove_tracks(self, playlist_id, track_ids): raise NotImplementedError
            def delete_playlist(self, playlist_id): raise NotImplementedError
            def rename_playlist(self, playlist_id, title, description=None): raise NotImplementedError
            def similar_tracks(self, track, *, limit=20): raise NotImplementedError
            def liked_tracks(self, *, limit=500): raise NotImplementedError

        provider = _StubProvider()
        with pytest.raises(NotSupportedError):
            provider.resolve_stream("some-id")


# --------------------------------------------------------------------------
# qobuz.py — resolve_stream
# --------------------------------------------------------------------------


class TestQobuzResolveStream:
    def test_builds_stream_source_from_canned_payload(
        self, qobuz_provider: QobuzProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = {
            "url": "https://qobuz.example/stream.flac",
            "mime_type": "audio/flac",
            "bit_depth": 16,
            "sampling_rate": 44100,
        }
        captured: dict[str, Any] = {}

        def fake_request(method, path, *, params=None, data=None, **kw):
            captured["method"] = method
            captured["path"] = path
            captured["params"] = params
            return payload

        monkeypatch.setattr(qobuz_provider, "_request", fake_request)

        source = qobuz_provider.resolve_stream("123")

        assert isinstance(source, StreamSource)
        assert source.url == "https://qobuz.example/stream.flac"
        assert source.mime_type == "audio/flac"
        assert source.container == "flac"
        assert source.label == "FLAC 16/44100"
        assert source.headers == {}

        assert captured["method"] == "GET"
        assert captured["path"] == "track/getFileUrl"
        assert captured["params"]["track_id"] == "123"
        assert captured["params"]["format_id"] == 6
        assert captured["params"]["intent"] == "stream"

    def test_request_signature_matches_request_sig(
        self, qobuz_provider: QobuzProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        def fake_request(method, path, *, params=None, data=None, **kw):
            captured["params"] = params
            return {"url": "https://qobuz.example/stream.flac", "mime_type": "audio/flac"}

        monkeypatch.setattr(qobuz_provider, "_request", fake_request)
        monkeypatch.setattr("harmony.providers.qobuz.time.time", lambda: 1700000000)

        qobuz_provider.resolve_stream("123")

        expected_sig = request_sig(
            "track",
            "getFileUrl",
            {"format_id": 6, "intent": "stream", "track_id": "123"},
            1700000000,
            "test_app_secret",
        )
        assert captured["params"]["request_ts"] == 1700000000
        assert captured["params"]["request_sig"] == expected_sig

    def test_no_url_raises_provider_error(
        self, qobuz_provider: QobuzProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        restricted = {"restrictions": [{"code": "TrackStreamNotAuthorized"}]}
        monkeypatch.setattr(qobuz_provider, "_request", lambda method, path, **kw: restricted)

        with pytest.raises(ProviderError):
            qobuz_provider.resolve_stream("123")

    def test_missing_app_secret_raises_provider_error(
        self, qobuz_provider: QobuzProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        qobuz_provider._app_secret = ""

        def boom(*a, **kw):
            raise AssertionError("must not make a request without an app secret")

        monkeypatch.setattr(qobuz_provider, "_request", boom)

        with pytest.raises(ProviderError):
            qobuz_provider.resolve_stream("123")

    def test_mp3_mime_maps_to_mp3_container(
        self, qobuz_provider: QobuzProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = {"url": "https://qobuz.example/stream.mp3", "mime_type": "audio/mpeg"}
        monkeypatch.setattr(qobuz_provider, "_request", lambda method, path, **kw: payload)

        source = qobuz_provider.resolve_stream("123")

        assert source.container == "mp3"
        assert source.label == "audio/mpeg"  # no bit_depth/sampling_rate -> falls back to mime


# --------------------------------------------------------------------------
# ytmusic.py — _pick_audio_format
# --------------------------------------------------------------------------


class TestPickAudioFormat:
    def test_prefers_itag_140(self) -> None:
        info = {
            "formats": [
                {"format_id": "251", "acodec": "opus", "vcodec": "none", "ext": "webm", "abr": 160},
                {"format_id": "140", "acodec": "mp4a.40.2", "vcodec": "none", "ext": "m4a", "abr": 128},
            ]
        }
        fmt = _pick_audio_format(info)
        assert fmt["format_id"] == "140"

    def test_falls_back_to_highest_abr_m4a(self) -> None:
        info = {
            "formats": [
                {"format_id": "139", "acodec": "mp4a.40.2", "vcodec": "none", "ext": "m4a", "abr": 48},
                {"format_id": "141", "acodec": "mp4a.40.2", "vcodec": "none", "ext": "m4a", "abr": 256},
                {"format_id": "251", "acodec": "opus", "vcodec": "none", "ext": "webm", "abr": 160},
            ]
        }
        fmt = _pick_audio_format(info)
        assert fmt["format_id"] == "141"

    def test_falls_back_to_any_audio_only_when_no_m4a(self) -> None:
        info = {
            "formats": [
                {"format_id": "251", "acodec": "opus", "vcodec": "none", "ext": "webm", "abr": 160},
                {"format_id": "18", "acodec": "mp4a.40.2", "vcodec": "avc1.42001E", "ext": "mp4", "abr": 96},
            ]
        }
        fmt = _pick_audio_format(info)
        assert fmt["format_id"] == "251"

    def test_uses_requested_formats_when_present(self) -> None:
        info = {
            "requested_formats": [{"format_id": "140", "acodec": "mp4a.40.2", "vcodec": "none", "ext": "m4a"}],
            "formats": [{"format_id": "251", "acodec": "opus", "vcodec": "none", "ext": "webm"}],
        }
        fmt = _pick_audio_format(info)
        assert fmt["format_id"] == "140"

    def test_no_audio_only_format_raises(self) -> None:
        info = {"formats": [{"format_id": "18", "acodec": "mp4a.40.2", "vcodec": "avc1.42001E", "ext": "mp4"}]}
        with pytest.raises(ProviderError):
            _pick_audio_format(info)

    def test_empty_formats_raises(self) -> None:
        with pytest.raises(ProviderError):
            _pick_audio_format({"formats": []})


# --------------------------------------------------------------------------
# ytmusic.py — resolve_stream
# --------------------------------------------------------------------------


class _FakeYoutubeDL:
    """Stand-in for ``yt_dlp.YoutubeDL`` used as a context manager."""

    def __init__(self, info: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self._info = info
        self._error = error
        self.extract_info_calls: list[tuple[str, bool]] = []

    def __call__(self, opts: dict[str, Any]) -> _FakeYoutubeDL:
        self.opts = opts
        return self

    def __enter__(self) -> _FakeYoutubeDL:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def extract_info(self, url: str, download: bool = False) -> dict[str, Any]:
        self.extract_info_calls.append((url, download))
        if self._error is not None:
            raise self._error
        assert self._info is not None
        return self._info


def _install_fake_yt_dlp(monkeypatch: pytest.MonkeyPatch, ydl: _FakeYoutubeDL) -> None:
    fake_module = ModuleType("yt_dlp")
    fake_module.YoutubeDL = ydl  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yt_dlp", fake_module)


class TestYTMusicResolveStream:
    def test_resolves_itag_140_stream(self, yt_provider: YTMusicProvider, monkeypatch: pytest.MonkeyPatch) -> None:
        info = {
            "formats": [
                {"format_id": "251", "acodec": "opus", "vcodec": "none", "ext": "webm", "abr": 160, "url": "webm-url"},
                {
                    "format_id": "140",
                    "acodec": "mp4a.40.2",
                    "vcodec": "none",
                    "ext": "m4a",
                    "abr": 128,
                    "url": "https://googlevideo.example/140",
                    "http_headers": {"User-Agent": "yt-dlp"},
                },
            ]
        }
        ydl = _FakeYoutubeDL(info=info)
        _install_fake_yt_dlp(monkeypatch, ydl)

        source = yt_provider.resolve_stream("abc123")

        assert isinstance(source, StreamSource)
        assert source.url == "https://googlevideo.example/140"
        assert source.mime_type == "audio/mp4"
        assert source.container == "m4a"
        assert source.headers == {"User-Agent": "yt-dlp"}
        assert source.label == "AAC (itag 140)"
        assert ydl.extract_info_calls == [("https://music.youtube.com/watch?v=abc123", False)]

    def test_missing_yt_dlp_raises_not_supported(
        self, yt_provider: YTMusicProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "yt_dlp", None)  # import yt_dlp -> ImportError

        with pytest.raises(NotSupportedError):
            yt_provider.resolve_stream("abc123")

    def test_extraction_failure_raises_provider_error(
        self, yt_provider: YTMusicProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ydl = _FakeYoutubeDL(error=RuntimeError("video unavailable"))
        _install_fake_yt_dlp(monkeypatch, ydl)

        with pytest.raises(ProviderError):
            yt_provider.resolve_stream("abc123")

    def test_no_playable_url_raises_provider_error(
        self, yt_provider: YTMusicProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        info = {"formats": [{"format_id": "140", "acodec": "mp4a.40.2", "vcodec": "none", "ext": "m4a", "url": None}]}
        ydl = _FakeYoutubeDL(info=info)
        _install_fake_yt_dlp(monkeypatch, ydl)

        with pytest.raises(ProviderError):
            yt_provider.resolve_stream("abc123")
