"""Offline unit tests for the provider layer.

Nothing here touches the network: YTMusicProvider is constructed with no auth
file (which is safe — ``YTMusic()`` with no auth performs no I/O), and
QobuzProvider is constructed with a pre-set ``qobuz_app_id`` so it never
scrapes the web player. Fixture dicts stand in for real API responses.
"""

from __future__ import annotations

import hashlib
import logging

import pytest
import requests

from harmony.config import Settings, redact_exception, redact_secrets
from harmony.errors import AuthError, NotSupportedError, ProviderError, RateLimitedError
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


class TestQobuzArtistTopTracks:
    """``get_artist_top_tracks`` via ``artist/page`` -- verified live against
    Radiohead (artist_id 43840): top_tracks entries have ``performer: None``,
    so the artist name must come from the payload's top-level ``name``.
    """

    def test_parses_realistic_payload_with_null_performer(
        self, qobuz_provider: QobuzProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = {
            "id": 43840,
            "name": "Radiohead",
            "top_tracks": [
                {
                    "id": 33933680,
                    "title": "Creep",
                    "performer": None,
                    "duration": 238,
                    "parental_warning": False,
                    "album": {
                        "title": "Pablo Honey",
                        "release_date_original": "1993-02-22",
                        "image": {"large": "creep.jpg"},
                    },
                },
                {
                    "id": 33933681,
                    "title": "Karma Police",
                    "performer": None,
                    "duration": 261,
                    "album": {
                        "title": "OK Computer",
                        "release_date_original": "1997-05-21",
                        "image": {"large": "kp.jpg"},
                    },
                },
            ],
        }
        monkeypatch.setattr(qobuz_provider, "_request", lambda method, path, **kw: payload)

        tracks = qobuz_provider.get_artist_top_tracks("43840")

        assert len(tracks) == 2
        assert tracks[0].id == "33933680"
        assert isinstance(tracks[0].id, str)
        assert tracks[0].title == "Creep"
        assert tracks[0].service is Service.QOBUZ
        assert tracks[0].artists == ["Radiohead"]  # filled from the payload's top-level name
        assert tracks[0].album == "Pablo Honey"
        assert tracks[0].year == 1993
        assert tracks[1].artists == ["Radiohead"]

    def test_respects_limit_by_slicing(
        self, qobuz_provider: QobuzProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = {
            "name": "Radiohead",
            "top_tracks": [{"id": i, "title": f"Track {i}", "performer": None} for i in range(25)],
        }
        monkeypatch.setattr(qobuz_provider, "_request", lambda method, path, **kw: payload)

        tracks = qobuz_provider.get_artist_top_tracks("43840", limit=5)

        assert [t.id for t in tracks] == ["0", "1", "2", "3", "4"]

    def test_missing_top_tracks_key_returns_empty_list(
        self, qobuz_provider: QobuzProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(qobuz_provider, "_request", lambda method, path, **kw: {"name": "Radiohead"})

        assert qobuz_provider.get_artist_top_tracks("43840") == []

    def test_empty_top_tracks_list_returns_empty_list(
        self, qobuz_provider: QobuzProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = {"name": "Radiohead", "top_tracks": []}
        monkeypatch.setattr(qobuz_provider, "_request", lambda method, path, **kw: payload)

        assert qobuz_provider.get_artist_top_tracks("43840") == []

    def test_sends_expected_request(self, qobuz_provider: QobuzProvider, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict = {}

        def fake_request(method, path, *, params=None, data=None, **kw):
            captured["method"] = method
            captured["path"] = path
            captured["params"] = params
            return {"name": "Radiohead", "top_tracks": []}

        monkeypatch.setattr(qobuz_provider, "_request", fake_request)

        qobuz_provider.get_artist_top_tracks("43840")

        assert captured == {
            "method": "GET",
            "path": "artist/page",
            "params": {"artist_id": "43840", "sort": "relevant"},
        }


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

    def test_redacts_json_body_double_quoted(self) -> None:
        text = '{"api_key": "9f8c2b6a1d4e7f0031bd5c9a44e2f1ab", "artist": "Boards of Canada"}'
        redacted = redact_secrets(text)
        assert "9f8c2b6a1d4e7f0031bd5c9a44e2f1ab" not in redacted
        assert '"api_key": "REDACTED"' in redacted
        assert "Boards of Canada" in redacted  # non-secret fields survive

    def test_redacts_python_dict_repr_single_quoted(self) -> None:
        text = "{'password': 'hunter2', 'username': 'alice'}"
        redacted = redact_secrets(text)
        assert "hunter2" not in redacted
        assert "alice" not in redacted
        assert "'password': 'REDACTED'" in redacted
        assert "'username': 'REDACTED'" in redacted

    def test_redacts_authorization_header_value(self) -> None:
        text = "headers: {'Authorization': 'Bearer sk-abc123topsecret', 'Accept': 'application/json'}"
        redacted = redact_secrets(text)
        assert "sk-abc123topsecret" not in redacted
        assert "Accept" in redacted  # unrelated header survives

    def test_redacts_hyphenated_param_name(self) -> None:
        text = "url: /endpoint?app-secret=hyphensecretvalue&other=1"
        redacted = redact_secrets(text)
        assert "hyphensecretvalue" not in redacted
        assert "app-secret=REDACTED" in redacted

    # -- suffix-anchored matching (previously prefix-anchored and missed
    # -- these real secret names) ------------------------------------------

    def test_redacts_access_token_query_param(self) -> None:
        text = "GET /oauth/callback?access_token=abc123&state=xyz"
        redacted = redact_secrets(text)
        assert "abc123" not in redacted
        assert "access_token=REDACTED" in redacted

    def test_redacts_refresh_token_query_param(self) -> None:
        text = "?refresh_token=abc123&client_id=harmony"
        redacted = redact_secrets(text)
        assert "abc123" not in redacted
        assert "refresh_token=REDACTED" in redacted

    def test_redacts_client_secret_query_param(self) -> None:
        text = "?client_secret=abc123&grant_type=password"
        redacted = redact_secrets(text)
        assert "abc123" not in redacted
        assert "client_secret=REDACTED" in redacted

    def test_redacts_x_user_auth_token_header_dict_repr(self) -> None:
        # This is the header Qobuz actually uses (see qobuz.py's
        # X-User-Auth-Token) -- no live path puts headers into exception text
        # today, but the module's own docstring claims this is covered, so it
        # must actually be.
        text = "{'X-User-Auth-Token': 'abc', 'X-App-Id': '123456789'}"
        redacted = redact_secrets(text)
        assert "'X-User-Auth-Token': 'abc'" not in redacted
        assert "'X-User-Auth-Token': 'REDACTED'" in redacted
        assert "'X-App-Id': '123456789'" in redacted  # unrelated header survives

    def test_redacts_x_user_auth_token_bare_header_text(self) -> None:
        text = "X-User-Auth-Token: abc123topsecret\r\nX-App-Id: 123456789"
        redacted = redact_secrets(text)
        assert "abc123topsecret" not in redacted
        assert "X-App-Id: 123456789" in redacted

    # -- deliberately NOT redacted: suffix anchoring must not over-redact ---

    def test_does_not_redact_token_type_field(self) -> None:
        # "token_type" has the sensitive word as its *first* component, not
        # its last -- and its value ("Bearer") isn't a secret at all.
        text = '{"token_type": "Bearer", "access_token": "abc123"}'
        redacted = redact_secrets(text)
        assert '"token_type": "Bearer"' in redacted
        assert '"access_token": "REDACTED"' in redacted

    def test_does_not_redact_url_path_segment_containing_token(self) -> None:
        # A path segment that merely contains the word "token" is not a
        # query-param/JSON-key/header *name* and must be left alone.
        text = "Max retries exceeded with url: /api/tokens/refresh (Caused by ...)"
        assert redact_secrets(text) == text

    def test_does_not_redact_word_containing_key_mid_string(self) -> None:
        # "monkey"/"keyword" contain "key" mid-word with no separator marking
        # it as a distinct name component -- must not be treated as a secret.
        text = '{"keyword": "monkey business", "nickname": "Turnkey"}'
        assert redact_secrets(text) == text


class TestRedactException:
    def test_redacted_stand_in_keeps_type_and_scrubs_message(self) -> None:
        original = requests.exceptions.ConnectionError(
            "Max retries exceeded with url: /login?password=hunter2secret"
        )
        stand_in = redact_exception(original)
        assert isinstance(stand_in, requests.exceptions.ConnectionError)
        assert "hunter2secret" not in str(stand_in)
        assert "password=REDACTED" in str(stand_in)

    def test_falls_back_when_type_is_not_reconstructible(self) -> None:
        class Weird(Exception):
            def __init__(self, a, b):  # requires two positional args
                super().__init__(a, b)
                self.a, self.b = a, b

        # Exception.__str__ with >1 arg renders repr(self.args) -- the query
        # string is still recognisable to the param regex despite the extra
        # quoting, so the fallback path still scrubs it.
        original = Weird("url: /x?password=hunter2secret&y=1", "extra")
        stand_in = redact_exception(original)
        assert "hunter2secret" not in str(stand_in)
        assert isinstance(stand_in, RuntimeError)


class TestUserAgentContactEmail:
    """config.user_agent() ⇐ Settings.contact_email.

    MusicBrainz/ListenBrainz's API etiquette explicitly wants a contact
    address in the User-Agent. This was a Settings field the Preferences UI
    let the user edit but that no code ever read — pure dead surface.
    """

    def test_includes_contact_email_when_set(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        from harmony import config as config_module

        monkeypatch.setattr(config_module, "settings_path", lambda: tmp_path / "settings.json")
        config_module.Settings(contact_email="me@example.com").save()

        ua = config_module.user_agent()
        assert "me@example.com" in ua
        assert ua.startswith(f"{config_module.APP_NAME}/")

    def test_explicit_contact_email_param_skips_disk_load(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """QobuzProvider passes settings.contact_email straight through so
        construction stays I/O-free; this proves that path never touches
        Settings.load() at all."""
        from harmony import config as config_module

        def boom():
            raise AssertionError("must not read Settings from disk when contact_email is given")

        monkeypatch.setattr(config_module.Settings, "load", staticmethod(boom))

        ua = config_module.user_agent("me@example.com")
        assert "me@example.com" in ua

    def test_omits_contact_clause_when_blank(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        from harmony import config as config_module

        monkeypatch.setattr(config_module, "settings_path", lambda: tmp_path / "settings.json")
        # No settings.json written at all -> Settings.load() falls back to
        # defaults, where contact_email == "".
        base_ua = config_module.user_agent()
        assert "@" not in base_ua

        config_module.Settings(contact_email="   ").save()  # whitespace-only
        assert config_module.user_agent() == base_ua  # stripped to blank, unchanged


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

    def test_construction_never_reads_settings_from_disk(
        self, credentials: FakeCredentialStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """user_agent(settings.contact_email) must be called with the
        in-memory value construction already has -- not user_agent(), which
        would fall back to Settings.load() and reintroduce disk I/O into a
        path that's explicitly documented as I/O-free."""
        from harmony.config import Settings as SettingsCls

        def boom():
            raise AssertionError("construction must not read Settings from disk")

        monkeypatch.setattr(SettingsCls, "load", staticmethod(boom))
        settings = Settings(qobuz_app_id="test_app_id", contact_email="me@example.com")
        provider = QobuzProvider(settings, credentials)
        assert "me@example.com" in provider._session.headers["User-Agent"]

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


class TestQobuzWarmUpCost:
    """Regression coverage for the startup-cost bug: warm-up used to always
    perform a full network login (or, for an unconfigured account, a
    multi-MB web-player scrape) because ``is_authenticated`` can never be
    True before the first real call, so ``AppState._warm_up``'s
    ``if provider.is_authenticated: continue`` guard never fired.
    """

    def test_authenticate_unconfigured_performs_zero_http(
        self, qobuz_provider: QobuzProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``qobuz_provider`` has no email/password configured (Settings()
        defaults) -- authenticate() must fail instantly with no scraping and
        no login attempt."""

        def boom(*a, **kw):
            raise AssertionError("unconfigured authenticate() must not perform HTTP")

        monkeypatch.setattr(requests.Session, "get", boom)
        monkeypatch.setattr(requests.Session, "request", boom)

        with pytest.raises(AuthError):
            qobuz_provider.authenticate()

    def test_has_credentials_false_when_unconfigured_and_io_free(
        self, qobuz_provider: QobuzProvider, credentials: FakeCredentialStore
    ) -> None:
        assert qobuz_provider.has_credentials is False
        assert credentials.get_calls == []  # in-memory settings check only

    def test_has_credentials_true_once_email_is_set(self, credentials: FakeCredentialStore) -> None:
        settings = Settings(qobuz_app_id="test_app_id", qobuz_email="alice@example.com")
        provider = QobuzProvider(settings, credentials)
        assert provider.has_credentials is True
        assert credentials.get_calls == []  # still I/O-free -- password isn't checked here

    def test_warm_up_skips_unconfigured_provider_with_zero_http(
        self, qobuz_provider: QobuzProvider, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from harmony.ui.state import AppState

        def boom(*a, **kw):
            raise AssertionError("warm-up must not attempt authenticate() on an unconfigured provider")

        monkeypatch.setattr(requests.Session, "get", boom)
        monkeypatch.setattr(requests.Session, "request", boom)

        errors: dict[Service, str] = {}
        AppState._warm_up({Service.QOBUZ: qobuz_provider}, errors)

        assert qobuz_provider.is_authenticated is False
        assert Service.QOBUZ not in errors  # skipped cleanly, not "attempted and failed"

    def test_cached_valid_token_skips_login_on_authenticate(
        self, credentials: FakeCredentialStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from harmony import config as config_module

        settings = Settings(qobuz_app_id="test_app_id", qobuz_email="alice@example.com")
        credentials.set(config_module.QOBUZ_PASSWORD, "hunter2")
        credentials.set(config_module.QOBUZ_TOKEN, "cached-tok")
        provider = QobuzProvider(settings, credentials)

        calls: list[str] = []

        def fake_request(method, path, *, params=None, data=None, authed=True, _retry_on_401=True):
            calls.append(path)
            if path == "user/login":
                raise AssertionError("a valid cached token must not trigger a fresh login")
            if path == "user/get":
                return {"user": {"display_name": "Cached User"}}
            raise AssertionError(f"unexpected path {path}")

        monkeypatch.setattr(provider, "_request", fake_request)

        provider.authenticate()

        assert calls == ["user/get"]  # validated the cached token, nothing else
        assert provider.is_authenticated is True
        assert provider.account_name() == "Cached User"

    def test_warm_up_with_cached_token_does_not_issue_login_request(
        self, credentials: FakeCredentialStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from harmony import config as config_module
        from harmony.ui.state import AppState

        settings = Settings(qobuz_app_id="test_app_id", qobuz_email="alice@example.com")
        credentials.set(config_module.QOBUZ_PASSWORD, "hunter2")
        credentials.set(config_module.QOBUZ_TOKEN, "cached-tok")
        provider = QobuzProvider(settings, credentials)

        calls: list[str] = []

        def fake_request(method, path, *, params=None, data=None, authed=True, _retry_on_401=True):
            calls.append(path)
            if path == "user/login":
                raise AssertionError("warm-up must not re-login when a cached token is present")
            if path == "user/get":
                return {"user": {"display_name": "Cached User"}}
            raise AssertionError(f"unexpected path {path}")

        monkeypatch.setattr(provider, "_request", fake_request)

        errors: dict[Service, str] = {}
        AppState._warm_up({Service.QOBUZ: provider}, errors)

        assert "user/login" not in calls
        assert provider.is_authenticated is True
        assert Service.QOBUZ not in errors

    def test_stale_cached_token_falls_back_to_login(
        self, credentials: FakeCredentialStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cached token must not be trusted forever: if validation reports
        it's dead, authenticate() still falls back to a real login rather
        than leaving the provider stuck unauthenticated."""
        from harmony import config as config_module

        settings = Settings(qobuz_app_id="test_app_id", qobuz_email="alice@example.com")
        credentials.set(config_module.QOBUZ_PASSWORD, "hunter2")
        credentials.set(config_module.QOBUZ_TOKEN, "stale-tok")
        provider = QobuzProvider(settings, credentials)

        calls: list[str] = []

        def fake_request(method, path, *, params=None, data=None, authed=True, _retry_on_401=True):
            calls.append(path)
            if path == "user/get":
                raise ProviderError("Qobuz API error 401 on user/get: invalid token")
            if path == "user/login":
                return {"user_auth_token": "fresh-tok", "user": {"display_name": "Refreshed"}}
            raise AssertionError(f"unexpected path {path}")

        monkeypatch.setattr(provider, "_request", fake_request)

        provider.authenticate()

        assert calls == ["user/get", "user/login"]
        assert provider._auth_token == "fresh-tok"
        assert provider.account_name() == "Refreshed"


class TestQobuzTokenAuth:
    """Token-mode authentication: for accounts with no password (e.g. signed
    in to Qobuz via Google OAuth), authenticate() must validate a pasted
    token instead of logging in with a password."""

    def test_token_mode_authenticates_with_no_login_call(
        self, credentials: FakeCredentialStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from harmony import config as config_module

        settings = Settings(qobuz_app_id="test_app_id", qobuz_auth_kind="token")
        credentials.set(config_module.QOBUZ_TOKEN, "pasted-tok")
        provider = QobuzProvider(settings, credentials)

        calls: list[str] = []

        def fake_request(method, path, *, params=None, data=None, authed=True, _retry_on_401=True):
            calls.append(path)
            if path == "user/login":
                raise AssertionError("token mode must never call user/login")
            if path == "user/get":
                return {"user": {"display_name": "Pasted User"}}
            raise AssertionError(f"unexpected path {path}")

        monkeypatch.setattr(provider, "_request", fake_request)

        provider.authenticate()

        assert calls == ["user/get"]
        assert provider.is_authenticated is True
        assert provider.account_name() == "Pasted User"
        assert provider._auth_token == "pasted-tok"

    def test_token_mode_missing_token_raises_and_performs_zero_http(
        self, credentials: FakeCredentialStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = Settings(qobuz_app_id="test_app_id", qobuz_auth_kind="token")
        provider = QobuzProvider(settings, credentials)

        def boom(*a, **kw):
            raise AssertionError("a missing token must fail with zero HTTP")

        monkeypatch.setattr(requests.Session, "get", boom)
        monkeypatch.setattr(requests.Session, "request", boom)

        with pytest.raises(AuthError, match="No Qobuz session token is saved"):
            provider.authenticate()

    def test_token_mode_never_reads_or_writes_password(
        self, credentials: FakeCredentialStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from harmony import config as config_module

        settings = Settings(qobuz_app_id="test_app_id", qobuz_auth_kind="token")
        credentials.set(config_module.QOBUZ_TOKEN, "pasted-tok")
        provider = QobuzProvider(settings, credentials)

        def fake_request(method, path, *, params=None, data=None, authed=True, _retry_on_401=True):
            return {"user": {"display_name": "Pasted User"}}

        monkeypatch.setattr(provider, "_request", fake_request)
        provider.authenticate()

        assert config_module.QOBUZ_PASSWORD not in credentials.get_calls

    def test_token_mode_invalid_token_raises_reauth_message(
        self, credentials: FakeCredentialStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An expired/revoked pasted token is the routine failure here -- the
        error must point the user at re-pasting a fresh one, not read as a
        generic/broken-app failure."""
        from harmony import config as config_module

        settings = Settings(qobuz_app_id="test_app_id", qobuz_auth_kind="token")
        credentials.set(config_module.QOBUZ_TOKEN, "dead-tok")
        provider = QobuzProvider(settings, credentials)

        def fake_request(method, path, *, params=None, data=None, authed=True, _retry_on_401=True):
            if path == "user/get":
                raise ProviderError("Qobuz API error 401 on user/get: invalid token")
            raise AssertionError(f"unexpected path {path}")

        monkeypatch.setattr(provider, "_request", fake_request)

        with pytest.raises(AuthError, match="no longer accepted"):
            provider.authenticate()

        assert provider.is_authenticated is False

    def test_has_credentials_token_mode_true_only_when_saved_flag_set(
        self, credentials: FakeCredentialStore
    ) -> None:
        not_saved = Settings(qobuz_app_id="test_app_id", qobuz_auth_kind="token", qobuz_token_saved=False)
        provider = QobuzProvider(not_saved, credentials)
        assert provider.has_credentials is False

        saved = Settings(qobuz_app_id="test_app_id", qobuz_auth_kind="token", qobuz_token_saved=True)
        provider = QobuzProvider(saved, credentials)
        assert provider.has_credentials is True

        assert credentials.get_calls == []  # I/O-free in both cases -- no keyring read

    def test_has_credentials_token_mode_ignores_qobuz_email(self, credentials: FakeCredentialStore) -> None:
        """Token mode must not fall back to checking qobuz_email -- an
        account mid-switch from password to token mode with a stale email
        still set shouldn't be reported as configured until a token is
        actually saved."""
        settings = Settings(
            qobuz_app_id="test_app_id",
            qobuz_auth_kind="token",
            qobuz_email="alice@example.com",
            qobuz_token_saved=False,
        )
        provider = QobuzProvider(settings, credentials)
        assert provider.has_credentials is False
        assert credentials.get_calls == []

    def test_has_credentials_password_mode_unaffected_by_token_fields(
        self, credentials: FakeCredentialStore
    ) -> None:
        settings = Settings(qobuz_app_id="test_app_id", qobuz_auth_kind="password", qobuz_token_saved=True)
        provider = QobuzProvider(settings, credentials)
        assert provider.has_credentials is False  # no email set
        assert credentials.get_calls == []

    def test_password_mode_authenticate_unchanged(
        self, credentials: FakeCredentialStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sanity check that adding token mode didn't disturb the default
        (password) branch of authenticate()."""
        from harmony import config as config_module

        settings = Settings(qobuz_app_id="test_app_id", qobuz_email="alice@example.com")
        assert settings.qobuz_auth_kind == "password"
        credentials.set(config_module.QOBUZ_PASSWORD, "hunter2")
        provider = QobuzProvider(settings, credentials)

        calls: list[str] = []

        def fake_request(method, path, *, params=None, data=None, authed=True, _retry_on_401=True):
            calls.append(path)
            if path == "user/login":
                return {"user_auth_token": "fresh-tok", "user": {"display_name": "Alice"}}
            raise AssertionError(f"unexpected path {path}")

        monkeypatch.setattr(provider, "_request", fake_request)

        provider.authenticate()

        assert calls == ["user/login"]
        assert provider._auth_token == "fresh-tok"


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
        into the raised exception's text OR the fully formatted log output.

        ``record.getMessage()`` (the previous version of this test) only
        renders the ``%``-formatted message string -- by definition it never
        includes ``record.exc_info``, so a leak that only lives in the
        exception chain (exactly this bug: the redacted message is fine, but
        ``raise ... from exc`` keeps the unredacted original reachable as
        ``__cause__``) could never fail this assertion. This formats each
        record the way a real handler (and ``tasks.run_async``'s
        ``log.exception``) would, cause chain included, via
        ``logging.Formatter().format()``.
        """
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

        formatter = logging.Formatter("%(levelname)s %(name)s %(message)s")

        with caplog.at_level("DEBUG"):
            with pytest.raises(ProviderError) as exc_info:
                qobuz_provider.authenticate()

            exc_text = str(exc_info.value)
            assert digest not in exc_text
            assert email not in exc_text
            assert password not in exc_text

            # Mirror tasks.run_async._settle(): the worker's exception is
            # caught and rendered with log.exception(), which pulls the live
            # exc_info (and therefore the full __cause__ chain) implicitly.
            try:
                qobuz_provider.authenticate()
            except ProviderError:
                logging.getLogger("harmony.tasks").exception("Background task failed")

        assert caplog.records  # sanity: we actually captured something
        for record in caplog.records:
            rendered = formatter.format(record)  # includes exc_info/cause chain
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
