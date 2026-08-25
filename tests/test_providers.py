"""Offline unit tests for the provider layer.

Nothing here touches the network: YTMusicProvider is constructed with no auth
file (which is safe — ``YTMusic()`` with no auth performs no I/O), and
QobuzProvider is constructed with a pre-set ``qobuz_app_id`` so it never
scrapes the web player. Fixture dicts stand in for real API responses.
"""

from __future__ import annotations

import hashlib

import pytest
import requests

from harmony.config import Settings, redact_secrets
from harmony.errors import NotSupportedError, ProviderError, RateLimitedError
from harmony.models import Service
from harmony.providers.base import _chunked, retry_on_rate_limit
from harmony.providers.qobuz import QobuzProvider, request_sig
from harmony.providers.ytmusic import YTMusicProvider


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
def yt_provider(credentials: FakeCredentialStore) -> YTMusicProvider:
    settings = Settings()  # ytmusic_auth_file="" -> unauthenticated, no I/O
    return YTMusicProvider(settings, credentials)


@pytest.fixture
def qobuz_provider(credentials: FakeCredentialStore) -> QobuzProvider:
    settings = Settings(qobuz_app_id="test_app_id")  # non-blank -> skips bundle scraping
    return QobuzProvider(settings, credentials)


# --------------------------------------------------------------------------
# base.py — _chunked / retry_on_rate_limit
# --------------------------------------------------------------------------


class TestChunked:
    def test_even_split(self) -> None:
        assert list(_chunked(list(range(6)), 2)) == [[0, 1], [2, 3], [4, 5]]

    def test_remainder(self) -> None:
        assert list(_chunked(list(range(5)), 2)) == [[0, 1], [2, 3], [4]]

    def test_chunk_larger_than_seq(self) -> None:
        assert list(_chunked([1, 2], 10)) == [[1, 2]]

    def test_empty(self) -> None:
        assert list(_chunked([], 5)) == []

    def test_size_100_and_50_boundaries(self) -> None:
        items = list(range(101))
        chunks = list(_chunked(items, 100))
        assert [len(c) for c in chunks] == [100, 1]

        items = list(range(150))
        chunks = list(_chunked(items, 50))
        assert [len(c) for c in chunks] == [50, 50, 50]


class TestRetryOnRateLimit:
    def test_succeeds_after_transient_rate_limits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleeps: list[float] = []
        monkeypatch.setattr("harmony.providers.base.time.sleep", lambda s: sleeps.append(s))

        calls = {"n": 0}

        @retry_on_rate_limit(attempts=3, base_delay=1.0)
        def flaky() -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise RateLimitedError("slow down")
            return "ok"

        assert flaky() == "ok"
        assert calls["n"] == 3
        assert len(sleeps) == 2
        assert sleeps == [1.0, 2.0]  # exponential backoff between the 2 retries

    def test_gives_up_after_exhausting_attempts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("harmony.providers.base.time.sleep", lambda s: None)

        @retry_on_rate_limit(attempts=3, base_delay=0.01)
        def always_limited() -> None:
            raise RateLimitedError("nope")

        with pytest.raises(RateLimitedError):
            always_limited()

    def test_honours_retry_after_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleeps: list[float] = []
        monkeypatch.setattr("harmony.providers.base.time.sleep", lambda s: sleeps.append(s))

        @retry_on_rate_limit(attempts=2, base_delay=1.0)
        def flaky() -> str:
            if not sleeps:
                raise RateLimitedError("slow down", retry_after=5.5)
            return "ok"

        assert flaky() == "ok"
        assert sleeps == [5.5]


# --------------------------------------------------------------------------
# ytmusic.py — normalisation
# --------------------------------------------------------------------------


class TestYTMusicNormalisation:
    def test_search_song_result(self, yt_provider: YTMusicProvider) -> None:
        raw = {
            "resultType": "song",
            "videoId": "abc123",
            "title": "Wonderwall",
            "artists": [{"name": "Oasis", "id": "UC123"}],
            "album": {"name": "Morning Glory", "id": "MPRE1"},
            "duration": "3:45",
            "duration_seconds": 225,
            "year": None,
            "isExplicit": False,
            "thumbnails": [
                {"url": "small.jpg", "width": 60, "height": 60},
                {"url": "large.jpg", "width": 540, "height": 540},
            ],
        }
        track = yt_provider._track_from_raw(raw)
        assert track.id == "abc123"
        assert track.title == "Wonderwall"
        assert track.artists == ["Oasis"]
        assert track.album == "Morning Glory"
        assert track.duration_s == 225
        assert track.service is Service.YTMUSIC
        assert track.artwork_url == "large.jpg"
        assert track.explicit is False

    def test_duration_parsed_from_string_when_seconds_missing(self, yt_provider: YTMusicProvider) -> None:
        raw = {"videoId": "v1", "title": "Song", "duration": "1:02:03"}
        track = yt_provider._track_from_raw(raw)
        assert track.duration_s == 3723

    def test_album_as_plain_string(self, yt_provider: YTMusicProvider) -> None:
        raw = {"videoId": "v1", "title": "Song", "album": "Revival"}
        track = yt_provider._track_from_raw(raw)
        assert track.album == "Revival"

    def test_play_count_comma_formatted(self, yt_provider: YTMusicProvider) -> None:
        raw = {"videoId": "v1", "title": "Song", "playCount": "12,345,678"}
        track = yt_provider._track_from_raw(raw)
        assert track.play_count == 12345678

    def test_play_count_abbreviated_returns_none(self, yt_provider: YTMusicProvider) -> None:
        raw = {"videoId": "v1", "title": "Song", "playCount": "1.2M"}
        track = yt_provider._track_from_raw(raw)
        assert track.play_count is None

    def test_play_count_missing(self, yt_provider: YTMusicProvider) -> None:
        raw = {"videoId": "v1", "title": "Song"}
        assert yt_provider._track_from_raw(raw).play_count is None

    def test_watch_track_uses_length_key(self, yt_provider: YTMusicProvider) -> None:
        raw = {"videoId": "v1", "title": "Song", "length": "4:20", "artists": [{"name": "X", "id": None}]}
        track = yt_provider._track_from_raw(raw)
        assert track.duration_s == 260
        assert track.artists == ["X"]

    def test_missing_video_id_raises(self, yt_provider: YTMusicProvider) -> None:
        from harmony.errors import ProviderError

        with pytest.raises(ProviderError):
            yt_provider._track_from_raw({"title": "No id"})

    def test_album_from_raw(self, yt_provider: YTMusicProvider) -> None:
        raw = {
            "browseId": "MPREb_x",
            "title": "Familiar To Millions",
            "artists": [{"name": "Oasis", "id": "UC1"}],
            "year": "2018",
            "trackCount": "12",
            "thumbnails": [{"url": "a.jpg", "width": 300}],
        }
        album = yt_provider._album_from_raw(raw)
        assert album.id == "MPREb_x"
        assert album.artists == ["Oasis"]
        assert album.year == 2018
        assert album.track_count == 12
        assert album.artwork_url == "a.jpg"

    def test_artist_from_raw_search_result(self, yt_provider: YTMusicProvider) -> None:
        raw = {"browseId": "UC1", "artist": "Oasis", "resultType": "artist"}
        artist = yt_provider._artist_from_raw(raw)
        assert artist.id == "UC1"
        assert artist.name == "Oasis"

    def test_playlist_from_raw_library_playlist(self, yt_provider: YTMusicProvider) -> None:
        raw = {"playlistId": "PL1", "title": "Road Trip", "thumbnails": [], "count": "5", "owned": True}
        playlist = yt_provider._playlist_from_raw(raw)
        assert playlist.id == "PL1"
        assert playlist.track_count == 5

    def test_playlist_from_raw_header(self, yt_provider: YTMusicProvider) -> None:
        raw = {
            "id": "PL2",
            "title": "New EDM",
            "privacy": "PUBLIC",
            "author": {"name": "sigmatics", "id": "UC9"},
            "trackCount": 237,
        }
        playlist = yt_provider._playlist_from_raw(raw)
        assert playlist.public is True
        assert playlist.owner == "sigmatics"
        assert playlist.track_count == 237


class TestYTMusicChunkingAndErrors:
    def test_add_tracks_chunks_at_100_with_sleep_between(
        self, yt_provider: YTMusicProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []
        sleeps: list[float] = []
        monkeypatch.setattr(yt_provider._yt, "add_playlist_items", lambda pid, ids: calls.append(list(ids)))
        monkeypatch.setattr("harmony.providers.ytmusic.time.sleep", lambda s: sleeps.append(s))

        track_ids = [f"v{i}" for i in range(250)]
        yt_provider.add_tracks("PL1", track_ids)

        assert [len(c) for c in calls] == [100, 100, 50]
        assert sleeps == [0.5, 0.5]  # no trailing sleep after the last chunk

    def test_remove_tracks_raises_without_set_video_id(
        self, yt_provider: YTMusicProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        raw_playlist = {"tracks": [{"videoId": "v1", "title": "Song"}]}  # no setVideoId
        monkeypatch.setattr(yt_provider._yt, "get_playlist", lambda pid, limit=None: raw_playlist)

        with pytest.raises(NotSupportedError):
            yt_provider.remove_tracks("PL1", ["v1"])

    def test_remove_tracks_passes_matching_raw_dicts(
        self, yt_provider: YTMusicProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        raw_playlist = {
            "tracks": [
                {"videoId": "v1", "setVideoId": "sv1", "title": "Keep out"},
                {"videoId": "v2", "setVideoId": "sv2", "title": "Remove me"},
            ]
        }
        removed: list[list[dict]] = []
        monkeypatch.setattr(yt_provider._yt, "get_playlist", lambda pid, limit=None: raw_playlist)
        monkeypatch.setattr(
            yt_provider._yt, "remove_playlist_items", lambda pid, videos: removed.append(videos)
        )

        yt_provider.remove_tracks("PL1", ["v2"])

        assert len(removed) == 1
        assert [t["videoId"] for t in removed[0]] == ["v2"]

    def test_search_aggregates_kinds(self, yt_provider: YTMusicProvider, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_search(query, filter=None, limit=20, **kw):
            if filter == "songs":
                return [{"videoId": "v1", "title": "Song", "artists": [{"name": "A", "id": None}]}]
            if filter == "albums":
                return [{"browseId": "MPRE1", "title": "Album"}]
            return []

        monkeypatch.setattr(yt_provider._yt, "search", fake_search)
        results = yt_provider.search("query", kinds=("tracks", "albums"))
        assert len(results.tracks) == 1
        assert len(results.albums) == 1
        assert results.tracks[0].id == "v1"


# --------------------------------------------------------------------------
# qobuz.py — normalisation
# --------------------------------------------------------------------------


class TestQobuzNormalisation:
    def test_track_from_raw(self, qobuz_provider: QobuzProvider) -> None:
        raw = {
            "id": 12345,
            "title": "Everlong",
            "duration": 250,
            "isrc": "USRC10000001",
            "track_number": 3,
            "parental_warning": True,
            "performer": {"id": 1, "name": "Foo Fighters"},
            "album": {
                "title": "The Colour and the Shape",
                "release_date_original": "1997-05-20",
                "image": {"large": "cover.jpg"},
                "artist": {"name": "Foo Fighters"},
            },
        }
        track = qobuz_provider._track_from_raw(raw)
        assert track.id == "12345"
        assert track.service is Service.QOBUZ
        assert track.duration_s == 250
        assert track.isrc == "USRC10000001"
        assert track.artists == ["Foo Fighters"]
        assert track.album == "The Colour and the Shape"
        assert track.year == 1997
        assert track.artwork_url == "cover.jpg"
        assert track.explicit is True

    def test_track_from_raw_uses_album_ctx_fallback(self, qobuz_provider: QobuzProvider) -> None:
        album_ctx = {
            "title": "Album Title",
            "artist": {"name": "Album Artist"},
            "image": {"large": "a.jpg"},
        }
        raw = {"id": 1, "title": "Track", "duration": 100}
        track = qobuz_provider._track_from_raw(raw, album_ctx=album_ctx)
        assert track.album == "Album Title"
        assert track.artists == ["Album Artist"]
        assert track.artwork_url == "a.jpg"

    def test_track_missing_id_raises(self, qobuz_provider: QobuzProvider) -> None:
        from harmony.errors import ProviderError

        with pytest.raises(ProviderError):
            qobuz_provider._track_from_raw({"title": "No id"})

    def test_album_from_raw(self, qobuz_provider: QobuzProvider) -> None:
        raw = {
            "id": 999,
            "title": "In Rainbows",
            "artist": {"name": "Radiohead"},
            "release_date_original": "2007-10-10",
            "tracks_count": 10,
            "image": {"large": "art.jpg"},
        }
        album = qobuz_provider._album_from_raw(raw)
        assert album.id == "999"
        assert album.artists == ["Radiohead"]
        assert album.year == 2007
        assert album.track_count == 10
        assert album.artwork_url == "art.jpg"

    def test_artist_from_raw(self, qobuz_provider: QobuzProvider) -> None:
        artist = qobuz_provider._artist_from_raw({"id": 42, "name": "Radiohead"})
        assert artist.id == "42"
        assert artist.name == "Radiohead"

    def test_playlist_from_raw(self, qobuz_provider: QobuzProvider) -> None:
        raw = {
            "id": 555,
            "name": "Road Trip",
            "description": "songs",
            "tracks_count": 20,
            "is_public": True,
            "owner": {"name": "marth"},
            "image": "cover.jpg",
        }
        playlist = qobuz_provider._playlist_from_raw(raw)
        assert playlist.id == "555"
        assert playlist.title == "Road Trip"
        assert playlist.public is True
        assert playlist.owner == "marth"
        assert playlist.artwork_url == "cover.jpg"


class TestQobuzChunkingAndErrors:
    def test_add_tracks_chunks_at_50(self, qobuz_provider: QobuzProvider, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[dict] = []
        monkeypatch.setattr(
            qobuz_provider, "_request", lambda method, path, **kw: (calls.append(kw.get("data")) or {})
        )

        track_ids = [str(i) for i in range(120)]
        qobuz_provider.add_tracks("PL1", track_ids)

        assert len(calls) == 3
        sizes = [len(c["track_ids"].split(",")) for c in calls]
        assert sizes == [50, 50, 20]

    def test_remove_tracks_resolves_playlist_track_ids(
        self, qobuz_provider: QobuzProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = {
            "tracks": {
                "items": [
                    {"id": 1, "playlist_track_id": 901},
                    {"id": 2, "playlist_track_id": 902},
                ],
                "total": 2,
            }
        }
        delete_calls: list[dict] = []

        def fake_request(method, path, *, params=None, data=None, **kw):
            if path == "playlist/get":
                return page
            if path == "playlist/deleteTracks":
                delete_calls.append(data)
                return {}
            raise AssertionError(f"unexpected path {path}")

        monkeypatch.setattr(qobuz_provider, "_request", fake_request)

        qobuz_provider.remove_tracks("PL1", ["2"])

        assert len(delete_calls) == 1
        assert delete_calls[0]["playlist_track_ids"] == "902"

    def test_similar_tracks_not_supported(self, qobuz_provider: QobuzProvider) -> None:
        from harmony.models import Track

        seed = Track(id="1", title="Song", service=Service.QOBUZ)
        with pytest.raises(NotSupportedError):
            qobuz_provider.similar_tracks(seed)

    def test_get_artist_top_tracks_not_supported(self, qobuz_provider: QobuzProvider) -> None:
        with pytest.raises(NotSupportedError):
            qobuz_provider.get_artist_top_tracks("artist1")


class TestQobuzRequestSig:
    def _expected(self, object_name: str, method: str, params: dict, ts: int, secret: str) -> str:
        param_str = "".join(f"{k}{v}" for k, v in sorted(params.items()))
        payload = f"{object_name}{method}{param_str}{ts}{secret}"
        return hashlib.md5(payload.encode("utf-8")).hexdigest()

    def test_matches_manual_computation(self) -> None:
        params = {"b": "2", "a": "1"}
        sig = request_sig("track", "getFileUrl", params, 1700000000, "supersecret")
        assert sig == self._expected("track", "getFileUrl", params, 1700000000, "supersecret")

    def test_param_order_independent(self) -> None:
        sig1 = request_sig("track", "getFileUrl", {"a": "1", "b": "2"}, 42, "s")
        sig2 = request_sig("track", "getFileUrl", {"b": "2", "a": "1"}, 42, "s")
        assert sig1 == sig2

    def test_changes_with_secret(self) -> None:
        sig1 = request_sig("track", "getFileUrl", {"a": "1"}, 42, "s1")
        sig2 = request_sig("track", "getFileUrl", {"a": "1"}, 42, "s2")
        assert sig1 != sig2

    def test_is_32_char_hex(self) -> None:
        sig = request_sig("track", "getFileUrl", {}, 1, "secret")
        assert len(sig) == 32
        int(sig, 16)  # raises ValueError if not hex


# --------------------------------------------------------------------------
# config.redact_secrets — credential-leak fix
# --------------------------------------------------------------------------


class TestRedactSecrets:
    def test_redacts_verified_qobuz_login_leak(self) -> None:
        # Exact shape of the verified real output from the bug report: a
        # urllib3 "Max retries exceeded" message embedding the full request
        # URL, including username/email/password=<md5>.
        text = (
            "HTTPSConnectionPool(host='www.qobuz.com', port=443): Max retries exceeded with "
            "url: /api.json/0.2/user/login?app_id=123456789&username=alice%40example.com"
            "&email=alice%40example.com&password=df09b9f54e5db483dc5ecc24dbeb3177 "
            "(Caused by NewConnectionError('...: Failed to establish a new connection'))"
        )
        redacted = redact_secrets(text)
        assert "df09b9f54e5db483dc5ecc24dbeb3177" not in redacted
        assert "alice%40example.com" not in redacted
        assert "alice@example.com" not in redacted
        # Structure survives — only the values are gone.
        assert "username=REDACTED" in redacted
        assert "email=REDACTED" in redacted
        assert "password=REDACTED" in redacted
        assert "user/login" in redacted

    def test_redacts_verified_lastfm_api_key_leak(self) -> None:
        text = (
            "Request to https://ws.audioscrobbler.com/2.0/ failed: "
            "HTTPSConnectionPool(host='ws.audioscrobbler.com', port=443): Max retries exceeded "
            "with url: /2.0/?method=track.getsimilar&format=json&api_key=9f8c2b6a1d4e7f0031bd5c9a44e2f1ab"
            "&artist=Boards+of+Canada&track=Roygbiv "
            "(Caused by NewConnectionError('...: Failed to establish a new connection'))"
        )
        redacted = redact_secrets(text)
        assert "9f8c2b6a1d4e7f0031bd5c9a44e2f1ab" not in redacted
        assert "api_key=REDACTED" in redacted

    def test_leaves_secret_free_text_untouched(self) -> None:
        text = "Qobuz API error 500 on catalog/search: internal server error"
        assert redact_secrets(text) == text

    def test_handles_empty_string(self) -> None:
        assert redact_secrets("") == ""

    def test_redacts_app_secret_and_user_auth_token(self) -> None:
        text = "url: /track/getFileUrl?user_auth_token=abc123&app_secret=topsecretvalue&track_id=5"
        redacted = redact_secrets(text)
        assert "abc123" not in redacted
        assert "topsecretvalue" not in redacted


# --------------------------------------------------------------------------
# qobuz.py — constructor purity, lazy credential setup, login transport
# --------------------------------------------------------------------------


class TestQobuzConstructorIsPure:
    def test_construction_never_touches_credential_store(self, credentials: FakeCredentialStore) -> None:
        settings = Settings(qobuz_app_id="test_app_id")
        QobuzProvider(settings, credentials)
        assert credentials.get_calls == []

    def test_construction_never_touches_network(
        self, credentials: FakeCredentialStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*a, **kw):
            raise AssertionError("construction must not perform network I/O")

        monkeypatch.setattr(requests.Session, "get", boom)
        monkeypatch.setattr(requests.Session, "request", boom)
        settings = Settings()  # blank qobuz_app_id -> would normally trigger a scrape
        QobuzProvider(settings, credentials)  # must not raise

    def test_is_authenticated_false_and_io_free_before_first_use(
        self, credentials: FakeCredentialStore
    ) -> None:
        settings = Settings(qobuz_app_id="test_app_id")
        provider = QobuzProvider(settings, credentials)
        assert provider.is_authenticated is False
        assert credentials.get_calls == []  # the property itself did no I/O either

    def test_app_credentials_loaded_lazily_exactly_once(self, credentials: FakeCredentialStore) -> None:
        settings = Settings(qobuz_app_id="test_app_id")
        credentials.set("qobuz.app_secret", "the-secret")
        provider = QobuzProvider(settings, credentials)
        assert credentials.get_calls == []  # nothing yet

        provider._ensure_ready()  # what every _request()/authenticate() call triggers first
        first_call_count = len(credentials.get_calls)
        assert first_call_count > 0  # app_secret (and token) were fetched on first use

        provider._ensure_ready()
        assert len(credentials.get_calls) == first_call_count  # not fetched again

    def test_app_id_scrape_deferred_until_first_use(
        self, credentials: FakeCredentialStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scrape_calls = {"n": 0}

        def fake_scrape(session):
            scrape_calls["n"] += 1
            return "scraped_app_id", "scraped_secret"

        monkeypatch.setattr("harmony.providers.qobuz._scrape_app_credentials", fake_scrape)
        settings = Settings()  # blank -> would scrape
        provider = QobuzProvider(settings, credentials)
        assert scrape_calls["n"] == 0  # not yet - construction is pure

        provider._ensure_ready()
        assert scrape_calls["n"] == 1
        assert provider._app_id == "scraped_app_id"

        provider._ensure_ready()
        assert scrape_calls["n"] == 1  # cached, not scraped again

    def test_construction_never_raises_when_qobuz_unreachable(
        self, credentials: FakeCredentialStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fresh install (blank app_id) used to scrape play.qobuz.com in
        __init__; if Qobuz was unreachable that raised AuthError out of
        construction, which build_providers() propagated, dropping the whole
        provider set. Construction must never touch the network at all now."""
        settings = Settings()
        provider = QobuzProvider(settings, credentials)  # must not raise
        assert provider._app_id == ""


class TestQobuzLoginTransport:
    def test_login_sends_credentials_as_post_body_not_query_params(
        self, qobuz_provider: QobuzProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict = {}

        def fake_request(method, url, *, params=None, data=None, headers=None, timeout=None):
            captured["method"] = method
            captured["url"] = url
            captured["params"] = params
            captured["data"] = data
            resp = requests.Response()
            resp.status_code = 200
            resp._content = b'{"user_auth_token": "tok123"}'
            return resp

        monkeypatch.setattr(qobuz_provider._session, "request", fake_request)

        qobuz_provider._login("alice@example.com", "hunter2")

        assert captured["method"] == "POST"
        assert captured["params"] is None  # nothing on the query string
        assert captured["data"]["email"] == "alice@example.com"
        digest = hashlib.md5(b"hunter2").hexdigest()
        assert captured["data"]["password"] == digest
        # The URL itself carries no credentials regardless of how it was built.
        assert "hunter2" not in captured["url"]
        assert digest not in captured["url"]


class TestQobuzLeakProof:
    def test_forced_connection_error_does_not_leak_credentials(
        self,
        qobuz_provider: QobuzProvider,
        credentials: FakeCredentialStore,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """End-to-end proof for the verified leak: force a transport failure
        during login and assert the password digest / email never survive
        into the raised exception's text or any captured log record."""
        password = "hunter2"
        digest = hashlib.md5(password.encode()).hexdigest()
        email = "alice@example.com"
        qobuz_provider._settings.qobuz_email = email
        credentials.set("qobuz.password", password)

        # Simulate exactly the verified failure mode: urllib3 embeds the full
        # request URL (with whatever was on it) in its own exception text.
        def raise_conn_error(method, url, **kw):
            leaking_url = (
                f"{url}?app_id=123456789&username={email}&email={email}&password={digest}"
                if "?" not in url
                else url
            )
            raise requests.exceptions.ConnectionError(
                f"HTTPSConnectionPool(host='www.qobuz.com', port=443): Max retries exceeded "
                f"with url: {leaking_url} (Caused by NewConnectionError(...))"
            )

        monkeypatch.setattr(qobuz_provider._session, "request", raise_conn_error)

        with caplog.at_level("DEBUG"):
            with pytest.raises(ProviderError) as exc_info:
                qobuz_provider.authenticate()

        exc_text = str(exc_info.value)
        assert digest not in exc_text
        assert email not in exc_text
        assert password not in exc_text

        for record in caplog.records:
            rendered = record.getMessage()
            assert digest not in rendered
            assert email not in rendered
            assert password not in rendered


# --------------------------------------------------------------------------
# providers/__init__.py — build_providers
# --------------------------------------------------------------------------


def test_build_providers_returns_both_services(credentials: FakeCredentialStore) -> None:
    from harmony.providers import QobuzProvider as QP
    from harmony.providers import YTMusicProvider as YP
    from harmony.providers import build_providers

    settings = Settings(qobuz_app_id="test_app_id")
    providers = build_providers(settings, credentials)

    assert set(providers) == {Service.YTMUSIC, Service.QOBUZ}
    assert isinstance(providers[Service.YTMUSIC], YP)
    assert isinstance(providers[Service.QOBUZ], QP)
