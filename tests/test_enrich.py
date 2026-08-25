"""Offline tests for harmony.enrich and harmony.ai.claude.

Every test here parses canned payloads or drives fake collaborators — nothing
in this file touches the network or the real Anthropic API.
"""

from __future__ import annotations

import json

import anthropic
import httpx2
import pytest

from harmony.ai.claude import (
    DEFAULT_MODEL,
    PlaylistIdea,
    PlaylistPlanner,
    TrackIdea,
)
from harmony.db import Database
from harmony.enrich import lastfm as lastfm_mod
from harmony.enrich import listenbrainz, musicbrainz, recommender
from harmony.enrich.lastfm import SimilarArtist, SimilarTrack
from harmony.enrich.recommender import Recommender
from harmony.errors import (
    HarmonyError,
    MissingCredentialError,
    NotSupportedError,
    ProviderError,
    RateLimitedError,
)
from harmony.models import Service, Track

# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


class FakeProvider:
    """Minimal MusicProvider double: search() returns a fixed catalog."""

    def __init__(self, catalog: list[Track], similar: list[Track] | None = None, not_supported: bool = False):
        self.service = Service.YTMUSIC
        self.catalog = catalog
        self._similar = similar or []
        self._not_supported = not_supported
        self.similar_calls: list[Track] = []

    def search(self, query, *, kinds=("tracks",), limit=25):
        from harmony.models import SearchResults

        return SearchResults(tracks=list(self.catalog))

    def similar_tracks(self, track, *, limit=20):
        self.similar_calls.append(track)
        if self._not_supported:
            raise NotSupportedError("this provider has no recommendations")
        return self._similar


@pytest.fixture
def db() -> Database:
    database = Database(":memory:")
    yield database
    database.close()


# ---------------------------------------------------------------------------
# lastfm.py
# ---------------------------------------------------------------------------


def test_lastfm_missing_credential_raises(monkeypatch):
    monkeypatch.setattr(lastfm_mod.CredentialStore, "get", lambda self, key: None)
    with pytest.raises(MissingCredentialError):
        lastfm_mod.similar_tracks("Boards of Canada", "Roygbiv")


def test_lastfm_similar_tracks_coerces_single_result_to_list(monkeypatch):
    """Last.fm returns a bare object instead of a list for exactly one result."""
    monkeypatch.setattr(lastfm_mod, "_api_key", lambda: "test-key")
    payload = {
        "similartracks": {
            "track": {
                "name": "Music Is Math",
                "artist": {"name": "Boards of Canada"},
                "match": "0.856",
                "url": "https://last.fm/music/x",
                "mbid": "abc-123",
            }
        }
    }
    monkeypatch.setattr(lastfm_mod, "_get_json", lambda *a, **k: payload)

    result = lastfm_mod.similar_tracks("Boards of Canada", "Roygbiv")

    assert result == [
        SimilarTrack(
            name="Music Is Math",
            artist="Boards of Canada",
            match=0.856,
            url="https://last.fm/music/x",
            mbid="abc-123",
        )
    ]


def test_lastfm_similar_tracks_parses_list_defensively(monkeypatch):
    monkeypatch.setattr(lastfm_mod, "_api_key", lambda: "test-key")
    payload = {
        "similartracks": {
            "track": [
                {"name": "A", "artist": {"name": "Artist A"}, "match": "0.9"},
                {"name": "B", "artist": "Artist B (string form)", "match": "not-a-number"},
            ]
        }
    }
    monkeypatch.setattr(lastfm_mod, "_get_json", lambda *a, **k: payload)

    result = lastfm_mod.similar_tracks("Seed", "Title")

    assert len(result) == 2
    assert result[0].match == 0.9
    assert result[1].artist == "Artist B (string form)"
    # A non-numeric "match" degrades to 0.0 rather than raising.
    assert result[1].match == 0.0


def test_lastfm_similar_artists_parses(monkeypatch):
    monkeypatch.setattr(lastfm_mod, "_api_key", lambda: "test-key")
    payload = {
        "similarartists": {
            "artist": [
                {"name": "Autechre", "match": "1", "mbid": "m1"},
                {"name": "Aphex Twin", "match": "0.72"},
            ]
        }
    }
    monkeypatch.setattr(lastfm_mod, "_get_json", lambda *a, **k: payload)

    result = lastfm_mod.similar_artists("Boards of Canada")

    assert result == [
        SimilarArtist(name="Autechre", match=1.0, mbid="m1"),
        SimilarArtist(name="Aphex Twin", match=0.72),
    ]


def test_lastfm_top_tracks_for_artist_normalises_playcount_to_match(monkeypatch):
    monkeypatch.setattr(lastfm_mod, "_api_key", lambda: "test-key")
    payload = {
        "toptracks": {
            "track": [
                {"name": "Hit", "artist": {"name": "Artist"}, "playcount": "1000"},
                {"name": "Deep Cut", "artist": {"name": "Artist"}, "playcount": "250"},
            ]
        }
    }
    monkeypatch.setattr(lastfm_mod, "_get_json", lambda *a, **k: payload)

    result = lastfm_mod.top_tracks_for_artist("Artist")

    assert result[0].name == "Hit"
    assert result[0].match == pytest.approx(1.0)
    assert result[1].match == pytest.approx(0.25)


def test_lastfm_tags_for_track_and_artist(monkeypatch):
    monkeypatch.setattr(lastfm_mod, "_api_key", lambda: "test-key")
    monkeypatch.setattr(
        lastfm_mod,
        "_get_json",
        lambda *a, **k: {"toptags": {"tag": [{"name": "idm"}, {"name": "ambient"}]}},
    )
    assert lastfm_mod.tags_for_track("Boards of Canada", "Roygbiv") == ["idm", "ambient"]
    assert lastfm_mod.tags_for_artist("Boards of Canada") == ["idm", "ambient"]


def test_lastfm_top_tracks_for_tag_uses_tracks_key(monkeypatch):
    """tag.getTopTracks nests its list under "tracks", not "toptracks"."""
    monkeypatch.setattr(lastfm_mod, "_api_key", lambda: "test-key")
    payload = {"tracks": {"track": [{"name": "Song", "artist": {"name": "Artist"}, "match": "0.5"}]}}
    monkeypatch.setattr(lastfm_mod, "_get_json", lambda *a, **k: payload)

    result = lastfm_mod.top_tracks_for_tag("idm")

    assert result == [SimilarTrack(name="Song", artist="Artist", match=0.5)]


def test_lastfm_error_payload_degrades_to_empty(monkeypatch):
    monkeypatch.setattr(lastfm_mod, "_api_key", lambda: "test-key")
    monkeypatch.setattr(lastfm_mod, "_get_json", lambda *a, **k: {"error": 6, "message": "Artist not found"})
    assert lastfm_mod.similar_tracks("Nobody", "Nothing") == []


def test_lastfm_caches_through_db(monkeypatch, db):
    monkeypatch.setattr(lastfm_mod, "_api_key", lambda: "test-key")
    calls = {"n": 0}

    def fake_get_json(*args, **kwargs):
        calls["n"] += 1
        return {"similartracks": {"track": [{"name": "A", "artist": {"name": "B"}, "match": "0.5"}]}}

    monkeypatch.setattr(lastfm_mod, "_get_json", fake_get_json)

    first = lastfm_mod.similar_tracks("Seed Artist", "Seed Title", db=db)
    second = lastfm_mod.similar_tracks("Seed Artist", "Seed Title", db=db)

    assert first == second
    assert calls["n"] == 1  # second call served from db.cache_get


# ---------------------------------------------------------------------------
# musicbrainz.py
# ---------------------------------------------------------------------------


_MB_RECORDINGS = [
    {
        "id": "mbid-wrong-artist",
        "score": 100,
        "title": "Roygbiv",
        "artist-credit": [{"name": "Completely Different Act"}],
        "isrcs": ["GBAAA0000001"],
        "releases": [{"title": "Some Release", "date": "1999-01-01"}],
    },
    {
        "id": "mbid-correct",
        "score": 90,
        "title": "Roygbiv",
        "artist-credit": [{"name": "Boards of Canada"}],
        "isrcs": ["GBUM71029601"],
        "releases": [{"title": "Music Has the Right to Children", "date": "1998-04-20"}],
    },
    {
        "id": "mbid-no-isrc",
        "score": 80,
        "title": "Roygbiv (Live)",
        "artist-credit": [{"name": "Boards of Canada"}],
        "isrcs": [],
        "releases": [],
    },
]


def test_musicbrainz_lookup_isrc_verifies_artist_before_accepting(monkeypatch):
    """The higher-scoring hit is by the wrong artist and must be rejected."""
    monkeypatch.setattr(musicbrainz, "_get_json", lambda *a, **k: {"recordings": _MB_RECORDINGS})

    isrc = musicbrainz.lookup_isrc("Boards of Canada", "Roygbiv")

    assert isrc == "GBUM71029601"


def test_musicbrainz_lookup_isrc_returns_none_when_no_candidate_qualifies(monkeypatch):
    monkeypatch.setattr(musicbrainz, "_get_json", lambda *a, **k: {"recordings": []})
    assert musicbrainz.lookup_isrc("Nobody", "Nothing") is None


def test_musicbrainz_canonical_metadata_parses_year_and_skips_unmatched_artist(monkeypatch):
    monkeypatch.setattr(musicbrainz, "_get_json", lambda *a, **k: {"recordings": _MB_RECORDINGS})

    meta = musicbrainz.canonical_metadata("Boards of Canada", "Roygbiv")

    assert meta == {
        "title": "Roygbiv",
        "artist": "Boards of Canada",
        "release": "Music Has the Right to Children",
        "year": 1998,
        "mbid": "mbid-correct",
        "isrcs": ["GBUM71029601"],
    }


def test_musicbrainz_canonical_metadata_none_when_nothing_matches(monkeypatch):
    monkeypatch.setattr(musicbrainz, "_get_json", lambda *a, **k: {"recordings": []})
    assert musicbrainz.canonical_metadata("Nobody", "Nothing") is None


def test_musicbrainz_recordings_by_isrc(monkeypatch):
    monkeypatch.setattr(
        musicbrainz,
        "_get_json",
        lambda *a, **k: {"isrc": "GBUM71029601", "recordings": [{"id": "mbid-correct"}]},
    )
    assert musicbrainz.recordings_by_isrc("GBUM71029601") == [{"id": "mbid-correct"}]


def test_musicbrainz_caches_through_db(monkeypatch, db):
    calls = {"n": 0}

    def fake_get_json(*args, **kwargs):
        calls["n"] += 1
        return {"recordings": _MB_RECORDINGS}

    monkeypatch.setattr(musicbrainz, "_get_json", fake_get_json)

    musicbrainz.lookup_isrc("Boards of Canada", "Roygbiv", db=db)
    musicbrainz.lookup_isrc("Boards of Canada", "Roygbiv", db=db)

    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# listenbrainz.py
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers: dict[str, str] = {}
        self.ok = 200 <= status_code < 300

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


def test_listenbrainz_similar_recordings_degrades_on_404(monkeypatch):
    monkeypatch.setattr(listenbrainz.requests, "get", lambda *a, **k: _FakeResponse(404))
    assert listenbrainz.similar_recordings("some-mbid") == []


def test_listenbrainz_similar_recordings_degrades_on_connection_error(monkeypatch):
    import requests

    def raise_connection_error(*args, **kwargs):
        raise requests.ConnectionError("no route to host")

    monkeypatch.setattr(listenbrainz.requests, "get", raise_connection_error)
    assert listenbrainz.similar_recordings("some-mbid") == []


def test_listenbrainz_similar_recordings_parses_payload(monkeypatch):
    payload = [
        {"recording_mbid": "r1", "recording_name": "Track One", "artist_name": "Artist One", "score": 5},
        {"recording_mbid": "r2", "recording_name": "Track Two", "artist_name": "Artist Two", "score": 3},
    ]
    monkeypatch.setattr(listenbrainz.requests, "get", lambda *a, **k: _FakeResponse(200, payload))

    result = listenbrainz.similar_recordings("some-mbid", limit=1)

    assert result == [payload[0]]  # limit applied client-side


def test_listenbrainz_similar_recordings_raises_on_rate_limit(monkeypatch):
    response = _FakeResponse(429)
    response.headers = {"Retry-After": "2"}
    monkeypatch.setattr(listenbrainz.requests, "get", lambda *a, **k: response)
    with pytest.raises(RateLimitedError):
        listenbrainz.similar_recordings("some-mbid")


def test_listenbrainz_radio_from_artist_parses_jspf(monkeypatch):
    payload = {
        "payload": {
            "jspf": {
                "playlist": {
                    "track": [
                        {"identifier": ["x"], "title": "Song", "creator": "Artist"},
                    ]
                }
            }
        }
    }
    monkeypatch.setattr(listenbrainz.requests, "get", lambda *a, **k: _FakeResponse(200, payload))

    result = listenbrainz.radio_from_artist("some-artist-mbid")

    assert result == payload["payload"]["jspf"]["playlist"]["track"]


def test_listenbrainz_radio_from_artist_degrades_when_unreachable(monkeypatch):
    monkeypatch.setattr(listenbrainz.requests, "get", lambda *a, **k: _FakeResponse(404))
    assert listenbrainz.radio_from_artist("some-artist-mbid") == []


def test_listenbrainz_caches_through_db(monkeypatch, db):
    calls = {"n": 0}

    def fake_get(*args, **kwargs):
        calls["n"] += 1
        return _FakeResponse(200, [{"recording_name": "A", "artist_name": "B", "score": 1}])

    monkeypatch.setattr(listenbrainz.requests, "get", fake_get)

    listenbrainz.similar_recordings("mbid", db=db)
    listenbrainz.similar_recordings("mbid", db=db)

    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# recommender.py
# ---------------------------------------------------------------------------


def _track(id_, title, artist, duration_s=200):
    return Track(id=id_, title=title, service=Service.YTMUSIC, artists=[artist], duration_s=duration_s)


def test_recommender_blends_and_dedupes_across_sources(monkeypatch, db):
    seed = _track("s1", "Song A", "Artist A")

    monkeypatch.setattr(
        recommender.lastfm,
        "similar_tracks",
        lambda artist, title, **kw: [
            SimilarTrack(name="Song B", artist="Artist B", match=0.9),
            SimilarTrack(name="Purple Nebula", artist="Unknown Artist", match=0.5),
            # Same track as the seed, differently cased: must be excluded.
            SimilarTrack(name="song a", artist="artist a", match=0.99),
        ],
    )
    # Disable the MusicBrainz -> ListenBrainz path entirely for this test.
    monkeypatch.setattr(recommender.musicbrainz, "canonical_metadata", lambda *a, **k: None)

    catalog = [_track("cb", "Song B", "Artist B")]
    provider = FakeProvider(catalog, similar=[_track("pb", "Song B", "Artist B")])

    suggestions = Recommender(db=db).similar_to_tracks([seed], provider, limit=10)

    assert len(suggestions) == 1
    only = suggestions[0]
    assert only.artist == "Artist B"
    assert only.title == "Song B"
    assert sorted(only.sources) == ["lastfm", "provider"]
    # lastfm 1.0 * 0.9 + provider 0.9 * 1.0
    assert only.score == pytest.approx(1.8)
    assert only.resolved is not None
    assert only.resolved.id == "cb"
    # "Purple Nebula" had nothing resembling it in the catalog -> dropped.
    assert all(s.title != "Purple Nebula" for s in suggestions)


def test_recommender_respects_exclude_set(monkeypatch, db):
    seed = _track("s1", "Song A", "Artist A")
    monkeypatch.setattr(
        recommender.lastfm,
        "similar_tracks",
        lambda artist, title, **kw: [SimilarTrack(name="Song B", artist="Artist B", match=0.9)],
    )
    monkeypatch.setattr(recommender.musicbrainz, "canonical_metadata", lambda *a, **k: None)
    catalog = [_track("cb", "Song B", "Artist B")]
    provider = FakeProvider(catalog)

    from harmony import matching

    exclude = {(matching.normalize_artist("Artist B"), matching.normalize_title("Song B"))}
    suggestions = Recommender(db=db).similar_to_tracks([seed], provider, limit=10, exclude=exclude)

    assert suggestions == []


def test_recommender_skips_provider_cleanly_on_not_supported(monkeypatch, db):
    seed = _track("s1", "Song A", "Artist A")
    monkeypatch.setattr(recommender.lastfm, "similar_tracks", lambda *a, **k: [])
    monkeypatch.setattr(recommender.musicbrainz, "canonical_metadata", lambda *a, **k: None)
    provider = FakeProvider([], not_supported=True)

    suggestions = Recommender(db=db).similar_to_tracks([seed], provider, limit=10)

    assert suggestions == []
    assert provider.similar_calls  # it was actually called, just raised NotSupportedError


def test_recommender_single_source_failure_is_not_fatal(monkeypatch, db):
    seed = _track("s1", "Song A", "Artist A")

    def boom(*args, **kwargs):
        raise ProviderError("Last.fm is down")

    monkeypatch.setattr(recommender.lastfm, "similar_tracks", boom)
    monkeypatch.setattr(recommender.musicbrainz, "canonical_metadata", lambda *a, **k: None)
    catalog = [_track("cb", "Song B", "Artist B")]
    provider = FakeProvider(catalog, similar=[_track("pb", "Song B", "Artist B")])

    # Must not raise even though Last.fm blew up.
    suggestions = Recommender(db=db).similar_to_tracks([seed], provider, limit=10)

    assert len(suggestions) == 1
    assert suggestions[0].sources == ["provider"]


def test_recommender_uses_listenbrainz_only_when_mbid_resolves(monkeypatch, db):
    seed = _track("s1", "Song A", "Artist A")
    monkeypatch.setattr(recommender.lastfm, "similar_tracks", lambda *a, **k: [])
    monkeypatch.setattr(recommender.musicbrainz, "canonical_metadata", lambda *a, **k: {"mbid": "the-mbid"})
    monkeypatch.setattr(
        recommender.listenbrainz,
        "similar_recordings",
        lambda mbid, **kw: [{"recording_name": "Song B", "artist_name": "Artist B", "score": 10}],
    )
    catalog = [_track("cb", "Song B", "Artist B")]
    provider = FakeProvider(catalog)

    suggestions = Recommender(db=db).similar_to_tracks([seed], provider, limit=10)

    assert len(suggestions) == 1
    assert suggestions[0].sources == ["listenbrainz"]
    assert suggestions[0].score == pytest.approx(0.8 * 10)


def test_recommender_similar_to_artist_ranks_by_lastfm_match(monkeypatch, db):
    monkeypatch.setattr(
        recommender.lastfm,
        "top_tracks_for_artist",
        lambda artist, **kw: [
            SimilarTrack(name="Deep Cut", artist=artist, match=0.3),
            SimilarTrack(name="Hit Song", artist=artist, match=1.0),
        ],
    )
    catalog = [_track("h", "Hit Song", "Boards of Canada"), _track("d", "Deep Cut", "Boards of Canada")]
    provider = FakeProvider(catalog)

    suggestions = Recommender(db=db).similar_to_artist("Boards of Canada", provider, limit=10)

    assert [s.title for s in suggestions] == ["Hit Song", "Deep Cut"]
    assert "Popular by Boards of Canada" in suggestions[0].reason


def test_recommender_expand_playlist_excludes_existing_tracks(monkeypatch, db):
    existing = [_track("e1", "Song A", "Artist A")]
    monkeypatch.setattr(
        recommender.lastfm,
        "similar_tracks",
        lambda artist, title, **kw: [
            SimilarTrack(name="Song A", artist="Artist A", match=0.99),  # already in playlist
            SimilarTrack(name="Song B", artist="Artist B", match=0.8),
        ],
    )
    monkeypatch.setattr(recommender.musicbrainz, "canonical_metadata", lambda *a, **k: None)
    catalog = [_track("cb", "Song B", "Artist B")]
    provider = FakeProvider(catalog)

    suggestions = Recommender(db=db).expand_playlist(existing, provider, limit=10)

    assert [s.title for s in suggestions] == ["Song B"]


def test_recommender_disabled_source_is_skipped(monkeypatch, db):
    seed = _track("s1", "Song A", "Artist A")
    called = {"lastfm": False}

    def fake_similar(*args, **kwargs):
        called["lastfm"] = True
        return [SimilarTrack(name="Song B", artist="Artist B", match=0.9)]

    monkeypatch.setattr(recommender.lastfm, "similar_tracks", fake_similar)
    monkeypatch.setattr(recommender.musicbrainz, "canonical_metadata", lambda *a, **k: None)

    class Settings:
        lastfm_enabled = False
        listenbrainz_enabled = False
        musicbrainz_enabled = False

    provider = FakeProvider([])
    suggestions = Recommender(db=db, settings=Settings()).similar_to_tracks([seed], provider, limit=10)

    assert suggestions == []
    assert called["lastfm"] is False


# ---------------------------------------------------------------------------
# ai/claude.py
# ---------------------------------------------------------------------------


class _FakeBlock:
    def __init__(self, type_: str, text: str | None = None):
        self.type = type_
        self.text = text


class _FakeMessage:
    def __init__(self, stop_reason: str, content: list[_FakeBlock]):
        self.stop_reason = stop_reason
        self.content = content


class _FakeMessages:
    def __init__(self, response):
        self._response = response
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._response, BaseException):
            raise self._response
        return self._response


class _FakeClient:
    def __init__(self, response):
        self.messages = _FakeMessages(response)


def _fake_httpx_response(status_code: int) -> httpx2.Response:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return httpx2.Response(status_code, request=request, json={"error": {"type": "x", "message": "y"}})


def test_planner_available_reflects_credential_presence(monkeypatch):
    monkeypatch.setattr("harmony.ai.claude.CredentialStore.get", lambda self, key: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert PlaylistPlanner().available is False
    assert PlaylistPlanner(api_key="explicit-key").available is True


def test_planner_falls_back_to_env_var(monkeypatch):
    monkeypatch.setattr("harmony.ai.claude.CredentialStore.get", lambda self, key: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
    assert PlaylistPlanner().available is True


def test_planner_raises_missing_credential_without_any_key(monkeypatch):
    monkeypatch.setattr("harmony.ai.claude.CredentialStore.get", lambda self, key: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    planner = PlaylistPlanner()
    with pytest.raises(MissingCredentialError):
        planner.plan("a chill evening playlist")


def test_plan_builds_prompt_schema_and_parses_response():
    payload = {
        "title": "Late Night Drive",
        "description": "Moody synths for a night drive",
        "notes": "leans instrumental",
        "tracks": [
            {"artist": "Tycho", "title": "A Walk", "why": "Warm, driving instrumental"},
            {"artist": "M83", "title": "Midnight City", "why": "Iconic synth hook"},
        ],
    }
    response = _FakeMessage(stop_reason="end_turn", content=[_FakeBlock("text", json.dumps(payload))])
    planner = PlaylistPlanner(api_key="test-key")
    planner._client = _FakeClient(response)

    idea = planner.plan(
        "moody late night drive",
        count=10,
        seed_tracks=["Tycho - A Walk"],
        library_hint=["M83 - Midnight City"],
    )

    assert idea == PlaylistIdea(
        title="Late Night Drive",
        description="Moody synths for a night drive",
        tracks=[
            TrackIdea(artist="Tycho", title="A Walk", why="Warm, driving instrumental"),
            TrackIdea(artist="M83", title="Midnight City", why="Iconic synth hook"),
        ],
        notes="leans instrumental",
    )

    call = planner._client.messages.calls[0]
    assert call["model"] == DEFAULT_MODEL
    assert call["max_tokens"] == 8000
    for forbidden in ("temperature", "top_p", "top_k"):
        assert forbidden not in call
    assert "content" not in call or True  # no assistant prefill message present
    assert all(m["role"] != "assistant" for m in call["messages"])

    schema = call["output_config"]["format"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"title", "description", "tracks", "notes"}
    item_schema = schema["properties"]["tracks"]["items"]
    assert item_schema["additionalProperties"] is False
    assert set(item_schema["required"]) == {"artist", "title", "why"}

    user_content = call["messages"][0]["content"]
    assert "moody late night drive" in user_content
    assert "Tycho - A Walk" in user_content
    assert "M83 - Midnight City" in user_content


def test_plan_drops_incomplete_track_ideas():
    payload = {
        "title": "T",
        "description": "D",
        "notes": "",
        "tracks": [
            {"artist": "", "title": "Missing Artist", "why": "x"},
            {"artist": "Real Artist", "title": "Real Title", "why": "y"},
        ],
    }
    response = _FakeMessage(stop_reason="end_turn", content=[_FakeBlock("text", json.dumps(payload))])
    planner = PlaylistPlanner(api_key="k")
    planner._client = _FakeClient(response)

    idea = planner.plan("anything")

    assert len(idea.tracks) == 1
    assert idea.tracks[0].artist == "Real Artist"


def test_plan_raises_harmony_error_on_refusal():
    response = _FakeMessage(stop_reason="refusal", content=[])
    planner = PlaylistPlanner(api_key="k")
    planner._client = _FakeClient(response)
    with pytest.raises(HarmonyError):
        planner.plan("anything")


def test_plan_raises_harmony_error_on_max_tokens():
    response = _FakeMessage(stop_reason="max_tokens", content=[_FakeBlock("text", "{")])
    planner = PlaylistPlanner(api_key="k")
    planner._client = _FakeClient(response)
    with pytest.raises(HarmonyError):
        planner.plan("anything")


def test_plan_raises_provider_error_on_unparseable_json():
    response = _FakeMessage(stop_reason="end_turn", content=[_FakeBlock("text", "not json")])
    planner = PlaylistPlanner(api_key="k")
    planner._client = _FakeClient(response)
    with pytest.raises(ProviderError):
        planner.plan("anything")


def test_plan_wraps_rate_limit_error():
    err = anthropic.RateLimitError(
        "slow down", response=_fake_httpx_response(429), body={"error": {"type": "rate_limit_error"}}
    )
    planner = PlaylistPlanner(api_key="k")
    planner._client = _FakeClient(err)
    with pytest.raises(RateLimitedError):
        planner.plan("anything")


def test_plan_wraps_connection_error():
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.APIConnectionError(request=request)
    planner = PlaylistPlanner(api_key="k")
    planner._client = _FakeClient(err)
    with pytest.raises(ProviderError):
        planner.plan("anything")


def test_resolve_maps_ideas_to_catalog_tracks_via_matching():
    idea = PlaylistIdea(
        title="T",
        description="D",
        tracks=[
            TrackIdea(artist="Artist B", title="Song B", why="fits"),
            TrackIdea(artist="Nobody At All", title="Total Nonsense Track", why="won't resolve"),
        ],
    )
    catalog = [_track("cb", "Song B", "Artist B")]
    provider = FakeProvider(catalog)
    planner = PlaylistPlanner(api_key="k")

    progress_calls = []
    resolved = planner.resolve(idea, provider, progress=lambda frac, msg: progress_calls.append((frac, msg)))

    assert len(resolved) == 1
    assert resolved[0].id == "cb"
    assert len(progress_calls) == 2  # called once per idea, even the unresolved one


def test_describe_playlist_returns_placeholder_for_empty_list():
    planner = PlaylistPlanner(api_key="k")
    assert planner.describe_playlist([]) == "An empty playlist."


def test_describe_playlist_calls_model_and_strips_text():
    response = _FakeMessage(stop_reason="end_turn", content=[_FakeBlock("text", "  Moody late-night drive vibes.  ")])
    planner = PlaylistPlanner(api_key="k")
    planner._client = _FakeClient(response)

    blurb = planner.describe_playlist([_track("cb", "Song B", "Artist B")])

    assert blurb == "Moody late-night drive vibes."
    call = planner._client.messages.calls[0]
    assert "Artist B - Song B" in call["messages"][0]["content"]
    assert "output_config" not in call
