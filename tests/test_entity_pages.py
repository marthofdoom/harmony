"""Engine tests for artist/album/track detail pages + smart search.

Fake providers supply playable data; the MusicBrainz overlay is stubbed (no
network) so these lock the *merge* logic: chronological ordering, the group
-discography vs person-"performed-on" split, ref wiring for navigation, the
performer passthrough, and the smart-search sectioning that encodes the three
search rules. Live MB behaviour is covered by tests/test_entity_metadata.py.
"""

from __future__ import annotations

import pytest

from harmony.models import Album, Artist, Service, Track
from harmony.web.api import Engine


class _FakeProvider:
    def __init__(self, service: Service) -> None:
        self.service = service
        self.albums_by_artist: dict[str, list[Album]] = {}
        self.top_by_artist: dict[str, list[Track]] = {}
        self.details: dict[str, Artist] = {}
        self.album_headers: dict[str, Album] = {}
        self.album_tracks: dict[str, list[Track]] = {}
        self.tracks: dict[str, Track] = {}
        self.search_albums: list[Album] = []
        self.search_artists: list[Artist] = []
        self.search_tracks: list[Track] = []

    def get_artist_detail(self, artist_id):
        return self.details.get(artist_id, Artist(id=artist_id, name="", service=self.service))

    def get_artist_albums(self, artist_id, *, limit=100):
        return self.albums_by_artist.get(artist_id, [])

    def get_artist_top_tracks(self, artist_id, *, limit=20):
        return self.top_by_artist.get(artist_id, [])

    def get_album_detail(self, album_id):
        return self.album_headers.get(album_id, Album(id=album_id, title="", service=self.service))

    def get_album_tracks(self, album_id):
        return self.album_tracks.get(album_id, [])

    def get_track(self, track_id):
        return self.tracks[track_id]

    def search(self, query, *, kinds=("tracks",), limit=25):
        from harmony.models import SearchResults
        r = SearchResults()
        if "albums" in kinds:
            r.albums = list(self.search_albums)
        if "artists" in kinds:
            r.artists = list(self.search_artists)
        if "tracks" in kinds:
            r.tracks = list(self.search_tracks)
        return r


def _engine_with(monkeypatch, provider, *, mb=True):
    eng = Engine()
    monkeypatch.setattr(eng, "_ensure_providers", lambda: {provider.service: provider})
    monkeypatch.setattr(eng, "_mb_enabled", lambda: mb)
    monkeypatch.setattr(eng, "_entity_db", lambda: None)
    return eng


def _album(id, title, year, service=Service.QOBUZ, artists=None, artist_ids=None):
    return Album(id=id, title=title, service=service, year=year,
                 artists=artists or ["Band"], artist_ids=artist_ids or [])


# -- artist page: group ------------------------------------------------------


def test_artist_page_group_sorts_albums_and_attaches_chronology(monkeypatch):
    prov = _FakeProvider(Service.QOBUZ)
    prov.details["A1"] = Artist(id="A1", name="The Band", service=Service.QOBUZ, image_url="img")
    prov.albums_by_artist["A1"] = [
        _album("r3", "Third", 2010), _album("r1", "First", 2000), _album("r2", "Second", 2005),
    ]
    overlay = {"kind": "group", "mbid": "mb1", "bio": {"text": "A rock band", "url": "u", "source": "wikipedia"},
               "members": [{"name": "X", "mbid": "x", "instruments": ["guitar"], "spans": [[2000, None]]}],
               "member_of": [], "studio_albums": []}
    monkeypatch.setattr("harmony.enrich.entities.artist_overlay", lambda name, **k: overlay)
    monkeypatch.setattr("harmony.enrich.entities.chronology", lambda ov: {"start_year": 2000, "end_year": 2020, "members": [], "albums": []})

    eng = _engine_with(monkeypatch, prov)
    page = eng.artist_page("qobuz", "A1")

    assert page["kind"] == "group"
    assert page["artist"]["name"] == "The Band"
    assert page["artist"]["image_url"] == "img"
    assert page["artist"]["bio"]["source"] == "wikipedia"
    assert [a["title"] for a in page["albums"]] == ["First", "Second", "Third"]  # chronological
    assert page["mbid"] == "mb1"
    assert page["chronology"]["start_year"] == 2000
    assert page["members"][0]["name"] == "X"


# -- artist page: person -----------------------------------------------------


def test_artist_page_person_uses_performed_discography(monkeypatch):
    prov = _FakeProvider(Service.QOBUZ)
    prov.details["P1"] = Artist(id="P1", name="Chester", service=Service.QOBUZ)
    # The provider *would* return solo/credited albums, but a person page must
    # instead show the performed-on discography mapped onto provider albums.
    prov.albums_by_artist["P1"] = [_album("solo", "Solo Thing", 2019)]
    prov.search_albums = [_album("lp1", "Hybrid Theory", 2000)]  # the map target
    overlay = {"kind": "person", "mbid": "mbp", "bio": None, "members": [], "member_of": [],
               "studio_albums": []}
    monkeypatch.setattr("harmony.enrich.entities.artist_overlay", lambda name, **k: overlay)
    monkeypatch.setattr("harmony.enrich.entities.chronology", lambda ov: None)
    monkeypatch.setattr("harmony.enrich.entities.performed_discography",
                        lambda name, **k: {"artist": overlay, "albums": [
                            {"title": "Hybrid Theory", "year": 2000, "mbid": "m", "band": "Linkin Park"}]})

    eng = _engine_with(monkeypatch, prov)
    page = eng.artist_page("qobuz", "P1")

    assert page["kind"] == "person"
    titles = [a["title"] for a in page["albums"]]
    assert titles == ["Hybrid Theory"]  # performed-on, not "Solo Thing"
    assert page["albums"][0]["id"] == "lp1"  # mapped to a playable provider album
    assert page["albums"][0]["artist"] == "Linkin Park"


# -- MB disabled -------------------------------------------------------------


def test_artist_page_without_musicbrainz_is_provider_only(monkeypatch):
    prov = _FakeProvider(Service.QOBUZ)
    prov.details["A1"] = Artist(id="A1", name="The Band", service=Service.QOBUZ)
    prov.albums_by_artist["A1"] = [_album("r2", "Second", 2005), _album("r1", "First", 2000)]
    eng = _engine_with(monkeypatch, prov, mb=False)
    page = eng.artist_page("qobuz", "A1")
    assert page["kind"] == "unknown"
    assert page["chronology"] is None
    assert page["members"] == []
    assert [a["title"] for a in page["albums"]] == ["First", "Second"]


# -- album page --------------------------------------------------------------


def test_album_page_header_tracks_and_artist_ref(monkeypatch):
    prov = _FakeProvider(Service.QOBUZ)
    prov.album_headers["AL"] = Album(id="AL", title="Meteora", service=Service.QOBUZ, year=2003,
                                     date="2003-03-25", artists=["Linkin Park"], artist_ids=["LP"])
    prov.album_tracks["AL"] = [
        Track(id="t1", title="Foreword", service=Service.QOBUZ, track_number=1, album_id="AL"),
        Track(id="t2", title="Don't Stay", service=Service.QOBUZ, track_number=2, album_id="AL"),
    ]
    eng = _engine_with(monkeypatch, prov, mb=False)
    page = eng.album_page("qobuz", "AL")
    assert page["album"]["title"] == "Meteora"
    assert page["album"]["date"] == "2003-03-25"
    assert page["album"]["track_count"] == 2  # filled from tracklist
    assert page["artist_ref"] == {"service": "qobuz", "id": "LP", "name": "Linkin Park"}
    assert [t["track_number"] for t in page["tracks"]] == [1, 2]


# -- track page --------------------------------------------------------------


def test_track_page_performers_and_refs(monkeypatch):
    prov = _FakeProvider(Service.QOBUZ)
    prov.tracks["T"] = Track(id="T", title="Numb", service=Service.QOBUZ, artists=["Linkin Park"],
                             artist_ids=["LP"], album="Meteora", album_id="AL", isrc="X")
    monkeypatch.setattr("harmony.enrich.entities.performers",
                        lambda **k: [{"name": "Chester Bennington", "mbid": "c", "roles": ["lead vocals"]}])
    eng = _engine_with(monkeypatch, prov)
    page = eng.track_page("qobuz", "T")
    assert page["track"]["title"] == "Numb"
    assert page["album_ref"] == {"service": "qobuz", "id": "AL", "title": "Meteora"}
    assert page["artist_refs"] == [{"service": "qobuz", "id": "LP", "name": "Linkin Park"}]
    assert page["performers"][0]["roles"] == ["lead vocals"]


def test_track_page_performers_empty_when_mb_off(monkeypatch):
    prov = _FakeProvider(Service.QOBUZ)
    prov.tracks["T"] = Track(id="T", title="Numb", service=Service.QOBUZ, artists=["LP"], artist_ids=[])
    eng = _engine_with(monkeypatch, prov, mb=False)
    page = eng.track_page("qobuz", "T")
    assert page["performers"] == []
    assert page["artist_refs"] == []  # no artist_ids -> no refs


# -- smart search ------------------------------------------------------------


def test_smart_search_group_section_and_incidental(monkeypatch):
    prov = _FakeProvider(Service.QOBUZ)
    prov.search_artists = [Artist(id="A1", name="The Band", service=Service.QOBUZ)]
    prov.search_albums = [_album("r2", "Second", 2005), _album("r1", "First", 2000)]
    prov.search_tracks = [Track(id="t1", title="A song", service=Service.QOBUZ)]
    prov.albums_by_artist["A1"] = [_album("d2", "DiscoB", 2008), _album("d1", "DiscoA", 2001)]
    overlay = {"kind": "group", "mbid": "mb", "bio": None, "members": [], "member_of": [], "studio_albums": []}
    monkeypatch.setattr("harmony.enrich.entities.artist_overlay", lambda name, **k: overlay)

    eng = _engine_with(monkeypatch, prov)
    res = eng.search_smart("The Band")

    assert res["artist"]["ref"]["name"] == "The Band"
    assert res["artist"]["kind"] == "group"
    assert [a["title"] for a in res["artist"]["albums"]] == ["DiscoA", "DiscoB"]  # chronological discography
    assert [a["title"] for a in res["albums"]] == ["First", "Second"]  # album matches, chronological
    assert res["incidental"]["tracks"][0]["title"] == "A song"
    # The chosen artist is not repeated in incidental artists.
    assert all(a["id"] != "A1" for a in res["incidental"]["artists"])


def test_smart_search_no_artist_match(monkeypatch):
    prov = _FakeProvider(Service.QOBUZ)
    prov.search_artists = [Artist(id="A1", name="Totally Different", service=Service.QOBUZ)]
    prov.search_albums = [_album("r1", "Some Album", 1999)]
    eng = _engine_with(monkeypatch, prov)
    res = eng.search_smart("zzzzz unmatchable")
    assert res["artist"] is None
    assert [a["title"] for a in res["albums"]] == ["Some Album"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
