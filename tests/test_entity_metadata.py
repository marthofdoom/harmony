"""Pure-logic tests for the MusicBrainz/Wikipedia entity layer.

No network: every test feeds MB-shaped fixtures to the parsers/composers so the
membership-span grouping, the studio-album filter, the "performed during tenure"
window (the Chester rule), the chronology assembly, the performance-vs-writing
split, and the Wikipedia URL resolution are all locked down deterministically.
"""

from __future__ import annotations

from harmony.enrich import entities
from harmony.enrich import musicbrainz as mb
from harmony.enrich import wikipedia as wp


def _artist_payload(relations):
    return {"relations": relations}


def _member_rel(name, mbid, direction, begin=None, end=None, ended=False, attrs=None):
    return {
        "type": "member of band", "direction": direction, "ended": ended,
        "begin": begin, "end": end, "attributes": attrs or [],
        "artist": {"id": mbid, "name": name},
    }


# -- membership span grouping ------------------------------------------------


def test_members_group_repeat_stints_and_dedupe_spans():
    payload = _artist_payload([
        # Same member, two relations for the same tenure (different instruments)
        # plus a genuine second stint — should collapse to two distinct spans.
        _member_rel("Brad", "b", "backward", "1996", None, False, ["guitar"]),
        _member_rel("Brad", "b", "backward", "1996", None, False, ["vocals"]),
        _member_rel("Brad", "b", "backward", "2007", "2017", True, ["guitar"]),
        _member_rel("Chester", "c", "backward", "1999", "2017", True, ["lead vocals"]),
        # A "forward" relation must be ignored for a group lookup.
        _member_rel("Some Band", "x", "forward", "2000", None),
    ])
    members = mb.members_of(payload)
    names = [m["name"] for m in members]
    assert names == ["Brad", "Chester"]  # earliest-span-first ordering
    brad = members[0]
    assert brad["spans"] == [[1996, None], [2007, 2017]]  # deduped, sorted
    assert "guitar" in brad["instruments"] and "vocals" in brad["instruments"]
    assert brad["is_current"] is True
    # The lone "forward" relation is a band, not a member — direction split.
    assert [b["name"] for b in mb.bands_of(payload)] == ["Some Band"]


def test_bands_of_reads_forward_relations():
    payload = _artist_payload([
        _member_rel("Linkin Park", "lp", "forward", "1999", "2017", True),
        _member_rel("Grey Daze", "gd", "forward", "1993", "1998", True),
    ])
    bands = mb.bands_of(payload)
    assert {b["name"] for b in bands} == {"Linkin Park", "Grey Daze"}
    assert not mb.members_of(payload)


def test_original_attribute_excluded_from_instruments():
    payload = _artist_payload([_member_rel("Joe", "j", "backward", "1996", None, False, ["original", "turntable"])])
    joe = mb.members_of(payload)[0]
    assert joe["instruments"] == ["turntable"]


# -- studio-album filter -----------------------------------------------------


def test_is_studio_album_excludes_secondary_types():
    assert mb.is_studio_album({"primary": "Album", "secondary": []})
    assert not mb.is_studio_album({"primary": "Album", "secondary": ["Live"]})
    assert not mb.is_studio_album({"primary": "Album", "secondary": ["Compilation"]})
    assert not mb.is_studio_album({"primary": "Single", "secondary": []})
    assert not mb.is_studio_album({"primary": "EP", "secondary": []})


# -- the "performed during tenure" window (Chester rule) ---------------------


def test_year_in_spans_window():
    # Member from 1999 to 2017.
    spans = [[1999, 2017]]
    assert entities._year_in_spans(2000, spans)   # Hybrid Theory
    assert entities._year_in_spans(2017, spans)   # One More Light (boundary)
    assert not entities._year_in_spans(2024, spans)  # From Zero (after)
    assert not entities._year_in_spans(1998, spans)  # before joining


def test_year_in_spans_open_ended_and_unknown_year():
    assert entities._year_in_spans(2025, [[1996, None]])   # still a member
    assert entities._year_in_spans(None, [[1996, None]])   # unknown year, current member -> include
    assert not entities._year_in_spans(None, [[1996, 2003]])  # unknown year, ex-member -> exclude
    assert entities._year_in_spans(1994, [[None, 1998]])   # open start


# -- chronology assembly -----------------------------------------------------


def test_chronology_only_for_groups_with_dated_members():
    person = {"kind": "person", "members": [], "studio_albums": []}
    assert entities.chronology(person) is None
    undated = {"kind": "group", "members": [{"name": "X", "spans": [[None, None]]}], "studio_albums": []}
    assert entities.chronology(undated) is None


def test_chronology_bounds_and_markers():
    overlay = {
        "kind": "group",
        "members": [
            {"name": "A", "mbid": "a", "instruments": ["guitar"], "spans": [[1996, None]]},
            {"name": "B", "mbid": "b", "instruments": ["drums"], "spans": [[1996, 2017]]},
        ],
        "studio_albums": [
            {"title": "First", "year": 2000, "mbid": "r1"},
            {"title": "Last", "year": 2014, "mbid": "r2"},
            {"title": "Undated", "year": None, "mbid": "r3"},
        ],
    }
    ch = entities.chronology(overlay)
    assert ch["start_year"] == 1996
    assert ch["end_year"] == entities._current_year()  # ongoing band -> now
    assert len(ch["members"]) == 2
    assert [a["title"] for a in ch["albums"]] == ["First", "Last"]  # undated marker dropped


def test_chronology_defunct_band_ends_at_last_activity():
    overlay = {
        "kind": "group",
        "members": [{"name": "A", "mbid": "a", "instruments": [], "spans": [[1970, 1980]]}],
        "studio_albums": [{"title": "LP", "year": 1978, "mbid": "r"}],
    }
    ch = entities.chronology(overlay)
    assert ch["start_year"] == 1970
    assert ch["end_year"] == 1980  # not the current year — the band ended


# -- performance vs. writing split -------------------------------------------


def test_performers_of_keeps_performance_drops_writing():
    payload = {"relations": [
        {"type": "vocal", "artist": {"id": "1", "name": "Singer"}, "attributes": ["lead vocals"]},
        {"type": "instrument", "artist": {"id": "2", "name": "Guitarist"}, "attributes": ["guitar"]},
        {"type": "composer", "artist": {"id": "3", "name": "Writer"}, "attributes": []},
        {"type": "producer", "artist": {"id": "4", "name": "Producer"}, "attributes": []},
        # recording->work link carries no artist; must be skipped, not crash.
        {"type": "performance", "work": {"id": "w"}},
    ]}
    perf = mb.performers_of(payload)
    names = [p["name"] for p in perf]
    assert names == ["Singer", "Guitarist"]  # writers/producers excluded
    assert perf[0]["roles"] == ["lead vocals"]
    assert perf[1]["roles"] == ["guitar"]


def test_performers_of_merges_multiple_roles_per_person():
    payload = {"relations": [
        {"type": "instrument", "artist": {"id": "1", "name": "Multi"}, "attributes": ["guitar"]},
        {"type": "instrument", "artist": {"id": "1", "name": "Multi"}, "attributes": ["bass"]},
        {"type": "vocal", "artist": {"id": "1", "name": "Multi"}, "attributes": []},
    ]}
    perf = mb.performers_of(payload)
    assert len(perf) == 1
    assert perf[0]["roles"] == ["guitar", "bass", "vocals"]


# -- Wikipedia/Wikidata URL resolution ---------------------------------------


def test_parse_wikipedia_url():
    assert wp._parse_wikipedia_url("https://en.wikipedia.org/wiki/Linkin_Park") == ("en", "Linkin_Park")
    assert wp._parse_wikipedia_url("https://de.wikipedia.org/wiki/Kraftwerk#History") == ("de", "Kraftwerk")
    assert wp._parse_wikipedia_url("https://example.com/x") is None


def test_wikidata_id_extraction():
    assert wp._wikidata_id("https://www.wikidata.org/wiki/Q168407") == "Q168407"
    assert wp._wikidata_id("https://www.wikidata.org/wiki/P123") is None
    assert wp._wikidata_id("https://en.wikipedia.org/wiki/X") is None


def test_artist_urls_first_wins():
    payload = {"relations": [
        {"type": "wikidata", "url": {"resource": "https://www.wikidata.org/wiki/Q1"}},
        {"type": "wikipedia", "url": {"resource": "https://en.wikipedia.org/wiki/A"}},
        {"type": "wikidata", "url": {"resource": "https://www.wikidata.org/wiki/Q2"}},
        {"type": "official homepage", "url": {"resource": "https://a.example"}},
    ]}
    urls = mb.artist_urls(payload)
    assert urls["wikidata"] == "https://www.wikidata.org/wiki/Q1"  # first wins
    assert urls["wikipedia"] == "https://en.wikipedia.org/wiki/A"


def test_lucene_escape():
    assert mb._lucene_escape("AC/DC") == r"AC\/DC"
    assert mb._lucene_escape("Sunn O)))") == r"Sunn O\)\)\)"
    assert mb._lucene_escape("plain") == "plain"
