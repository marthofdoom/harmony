"""Lidarr "Get with Lidarr" tests — fully mocked (no live Lidarr).

Locks the matching (exact by MusicBrainz id, else fuzzy), the add payload shape
(monitored + search + the artist's profiles/root folder), the error surfaces
(unconfigured, bad key, no match), and the Engine request wiring.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from harmony.lidarr import LidarrClient, LidarrError, _best_album, _best_artist


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._payload = payload
        self.text = text
        self.content = b"x" if payload is not None else b""

    def json(self):
        return self._payload


def _mock_requests(monkeypatch, handler):
    """Route ``requests.request(method, url, ...)`` through ``handler(method, url, kwargs)``."""
    import harmony.lidarr as lid
    monkeypatch.setattr(lid.requests, "request", lambda method, url, **kw: handler(method, url, kw))


# -- matching ----------------------------------------------------------------


def test_best_album_prefers_exact_mbid():
    cands = [
        {"title": "Wrong", "foreignAlbumId": "aaa", "artist": {"artistName": "X"}},
        {"title": "Also wrong but similar name", "foreignAlbumId": "bbb", "artist": {"artistName": "Y"}},
        {"title": "Meteora", "foreignAlbumId": "MB-RG", "artist": {"artistName": "Linkin Park"}},
    ]
    pick = _best_album(cands, "totally different", "nobody", "MB-RG")
    assert pick["foreignAlbumId"] == "MB-RG"  # mbid wins regardless of text


def test_best_album_fuzzy_when_no_mbid():
    cands = [
        {"title": "Meteora", "foreignAlbumId": "1", "artist": {"artistName": "Linkin Park"}},
        {"title": "Meteora (Live)", "foreignAlbumId": "2", "artist": {"artistName": "Someone Else"}},
    ]
    pick = _best_album(cands, "Meteora", "Linkin Park", None)
    assert pick["foreignAlbumId"] == "1"  # artist tie-breaks to the real one


def test_best_album_none_below_threshold():
    cands = [{"title": "Completely Unrelated", "foreignAlbumId": "1", "artist": {"artistName": "Zzz"}}]
    assert _best_album(cands, "Meteora", "Linkin Park", None) is None


def test_best_artist_exact_and_fuzzy():
    cands = [{"artistName": "Linkin Park", "foreignArtistId": "LP"},
             {"artistName": "Linkin Parc tribute", "foreignArtistId": "T"}]
    assert _best_artist(cands, "x", "LP")["foreignArtistId"] == "LP"
    assert _best_artist(cands, "Linkin Park", None)["foreignArtistId"] == "LP"


# -- add payloads ------------------------------------------------------------


def test_add_album_builds_monitored_search_payload(monkeypatch):
    sent = {}

    def handler(method, url, kw):
        if url.endswith("/album/lookup"):
            return _Resp(payload=[{"title": "Meteora", "foreignAlbumId": "RG",
                                   "artist": {"artistName": "Linkin Park", "foreignArtistId": "LP"}}])
        if url.endswith("/rootfolder"):
            return _Resp(payload=[{"path": "/music"}])
        if url.endswith("/qualityprofile"):
            return _Resp(payload=[{"id": 1, "name": "Lossless"}])
        if url.endswith("/metadataprofile"):
            return _Resp(payload=[{"id": 2, "name": "Standard"}])
        if method == "POST" and url.endswith("/album"):
            sent.update(kw.get("json"))
            return _Resp(payload={"id": 99, "title": "Meteora"})
        raise AssertionError(f"unexpected {method} {url}")

    _mock_requests(monkeypatch, handler)
    client = LidarrClient("http://lidarr:8686", "KEY")
    added = client.add_album("Meteora", "Linkin Park", mbid="RG")
    assert added["title"] == "Meteora"
    assert sent["monitored"] is True
    assert sent["addOptions"]["searchForNewAlbum"] is True
    assert sent["artist"]["rootFolderPath"] == "/music"
    assert sent["artist"]["qualityProfileId"] == 1
    assert sent["artist"]["metadataProfileId"] == 2
    assert sent["artist"]["monitored"] is True


def test_add_artist_searches_missing_albums(monkeypatch):
    sent = {}

    def handler(method, url, kw):
        if url.endswith("/artist/lookup"):
            return _Resp(payload=[{"artistName": "Nirvana", "foreignArtistId": "NIRV"}])
        if url.endswith("/rootfolder"):
            return _Resp(payload=[{"path": "/music"}])
        if url.endswith("/qualityprofile"):
            return _Resp(payload=[{"id": 3}])
        if url.endswith("/metadataprofile"):
            return _Resp(payload=[{"id": 4}])
        if method == "POST" and url.endswith("/artist"):
            sent.update(kw.get("json"))
            return _Resp(payload={"id": 7, "artistName": "Nirvana"})
        raise AssertionError(f"unexpected {method} {url}")

    _mock_requests(monkeypatch, handler)
    client = LidarrClient("http://lidarr:8686", "KEY")
    added = client.add_artist("Nirvana", mbid="NIRV")
    assert added["artistName"] == "Nirvana"
    assert sent["monitored"] is True
    assert sent["addOptions"]["searchForMissingAlbums"] is True
    assert sent["rootFolderPath"] == "/music"


# -- errors ------------------------------------------------------------------


def test_unconfigured_raises():
    with pytest.raises(LidarrError, match="isn't configured"):
        LidarrClient("", "").status()
    with pytest.raises(LidarrError, match="isn't configured"):
        LidarrClient("http://x", "").status()


def test_bad_key_raises(monkeypatch):
    _mock_requests(monkeypatch, lambda m, u, kw: _Resp(status=401, text="unauthorized"))
    with pytest.raises(LidarrError, match="rejected the API key"):
        LidarrClient("http://lidarr", "BAD").status()


def test_no_match_raises(monkeypatch):
    def handler(method, url, kw):
        if url.endswith("/album/lookup"):
            return _Resp(payload=[{"title": "Something Unrelated", "foreignAlbumId": "z",
                                   "artist": {"artistName": "Nobody"}}])
        return _Resp(payload=[{"path": "/music"}])

    _mock_requests(monkeypatch, handler)
    with pytest.raises(LidarrError, match="couldn't find"):
        LidarrClient("http://lidarr", "KEY").add_album("Meteora", "Linkin Park", mbid=None)


# -- Engine wiring -----------------------------------------------------------


def test_engine_lidarr_request_album_and_artist(monkeypatch):
    from harmony.web.api import Engine

    calls = []

    class _FakeClient:
        def add_album(self, title, artist, *, mbid, **kw):
            calls.append(("album", title, artist, mbid))
            return {"title": title}

        def add_artist(self, name, *, mbid, **kw):
            calls.append(("artist", name, mbid))
            return {"artistName": name}

    eng = Engine()
    settings = SimpleNamespace(lidarr_root_folder="", lidarr_quality_profile_id=0,
                               lidarr_metadata_profile_id=0)
    monkeypatch.setattr(eng, "_lidarr_client", lambda: (_FakeClient(), settings))

    r = eng.lidarr_request("album", title="Meteora", artist="Linkin Park", mbid="RG")
    assert r == {"ok": True, "kind": "album", "title": "Meteora"}
    r = eng.lidarr_request("artist", artist="Nirvana", mbid="NIRV")
    assert r == {"ok": True, "kind": "artist", "title": "Nirvana"}
    assert calls == [("album", "Meteora", "Linkin Park", "RG"), ("artist", "Nirvana", "NIRV")]
    with pytest.raises(KeyError):
        eng.lidarr_request("track", title="x")


if __name__ == "__main__":
    raise SystemExit(json.dumps(pytest.main([__file__, "-q"])))
