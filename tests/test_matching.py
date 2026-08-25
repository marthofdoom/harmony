from __future__ import annotations

from conftest import FakeProvider

from harmony import matching
from harmony.models import Service, Track


def _track(**kwargs) -> Track:
    defaults = dict(id="src1", title="Song", service=Service.YTMUSIC, artists=["Artist"], duration_s=200)
    defaults.update(kwargs)
    return Track(**defaults)


# -- normalize_title -------------------------------------------------------


def test_normalize_title_strips_remaster_tag() -> None:
    assert matching.normalize_title("Come Together (Remastered 2009)") == "come together"
    assert matching.normalize_title("Come Together - Remastered 2011") == "come together"


def test_normalize_title_strips_platform_noise() -> None:
    assert matching.normalize_title("Song Title (Official Music Video)") == "song title"
    assert matching.normalize_title("Song Title [HD]") == "song title"
    assert matching.normalize_title("Song Title (Radio Edit)") == "song title"


def test_normalize_title_keeps_live_and_other_version_markers() -> None:
    # These change what the recording *is*; normalize_title must not erase them.
    assert "live" in matching.normalize_title("Song Title (Live)")
    assert "acoustic" in matching.normalize_title("Song Title (Acoustic)")


def test_normalize_title_strips_feature_clause() -> None:
    assert matching.normalize_title("Song (feat. Someone Else)") == "song"
    assert matching.normalize_title("Song feat. Someone Else") == "song"


def test_normalize_title_keeps_intraword_apostrophes_and_digits() -> None:
    assert matching.normalize_title("Don't Stop 2Night") == "don't stop 2night"


def test_normalize_title_collapses_whitespace_and_casefolds() -> None:
    assert matching.normalize_title("  SONG   Title  ") == "song title"


# -- split_features -------------------------------------------------------


def test_split_features_extracts_single_artist() -> None:
    base, feats = matching.split_features("Song (feat. Artist B)")
    assert base == "Song"
    assert feats == ["Artist B"]


def test_split_features_extracts_multiple_artists() -> None:
    base, feats = matching.split_features("Song ft. Artist B, Artist C")
    assert base == "Song"
    assert feats == ["Artist B", "Artist C"]


def test_split_features_no_match_returns_original() -> None:
    base, feats = matching.split_features("Plain Song Title")
    assert base == "Plain Song Title"
    assert feats == []


# -- normalize_artist -------------------------------------------------------


def test_normalize_artist_drops_leading_the() -> None:
    assert matching.normalize_artist("The Beatles") == "beatles"


def test_normalize_artist_normalizes_ampersand() -> None:
    assert matching.normalize_artist("Simon & Garfunkel") == "simon and garfunkel"


# -- score -------------------------------------------------------


def test_score_isrc_exact_match_short_circuits() -> None:
    a = _track(isrc="US1234567890", title="Totally Different Title")
    b = _track(id="dst1", isrc="us1234567890", title="Nothing Alike", service=Service.QOBUZ)
    value, reasons = matching.score(a, b)
    assert value == 1.0
    assert "isrc exact match" in reasons


def test_score_live_vs_studio_is_penalized() -> None:
    studio = _track(title="Song Title", artists=["Artist"])
    live = _track(id="dst1", title="Song Title (Live)", artists=["Artist"], service=Service.QOBUZ)
    studio_pair = _track(id="dst2", title="Song Title", artists=["Artist"], service=Service.QOBUZ)

    live_score, live_reasons = matching.score(studio, live)
    studio_score, _ = matching.score(studio, studio_pair)

    assert live_score < studio_score
    assert any("version mismatch" in r for r in live_reasons)


def test_score_featured_artist_extracted_from_title_helps_match() -> None:
    source = _track(title="Song (feat. Artist B)", artists=["Artist A"])
    cand = _track(id="dst1", title="Song", artists=["Artist A", "Artist B"], service=Service.QOBUZ)
    value, _ = matching.score(source, cand)
    assert value >= matching.HIGH_THRESHOLD


def test_score_artist_subset_scores_well() -> None:
    source = _track(title="Song", artists=["Artist A", "Artist B"])
    cand = _track(id="dst1", title="Song", artists=["Artist B"], service=Service.QOBUZ)
    value, _ = matching.score(source, cand)
    assert value >= matching.HIGH_THRESHOLD


def test_score_missing_duration_is_neutral_not_punishing() -> None:
    source = _track(title="Song", artists=["Artist"], duration_s=None)
    cand = _track(id="dst1", title="Song", artists=["Artist"], duration_s=None, service=Service.QOBUZ)
    value, _ = matching.score(source, cand)
    assert value > matching.HIGH_THRESHOLD


# -- match_track / match_tracks -------------------------------------------------------


def test_match_track_empty_search_returns_none_confidence() -> None:
    source = _track()
    target = FakeProvider(Service.QOBUZ, catalog=[])
    result = matching.match_track(source, target)
    assert result.confidence == "none"
    assert result.best is None
    assert result.candidates == []


def test_match_track_ranks_candidates_descending() -> None:
    source = _track(title="Song Title", artists=["Artist"], duration_s=200)
    good = Track(id="good", title="Song Title", service=Service.QOBUZ, artists=["Artist"], duration_s=201)
    bad = Track(id="bad", title="Completely Unrelated", service=Service.QOBUZ, artists=["Nobody"], duration_s=50)
    target = FakeProvider(Service.QOBUZ, catalog=[bad, good])
    result = matching.match_track(source, target)
    assert result.best is not None
    assert result.best.track.id == "good"
    assert result.confidence in ("exact", "high")
    assert result.candidates[0].score >= result.candidates[1].score


def test_match_tracks_uses_db_cache_and_skips_network(monkeypatch) -> None:
    from harmony.db import Database

    db = Database(":memory:")
    source = _track(id="src1", title="Song Title", artists=["Artist"], duration_s=200)
    target = FakeProvider(Service.QOBUZ, catalog=[])  # empty: a live search would find nothing

    db.put_link(Service.YTMUSIC, "src1", Service.QOBUZ, "cached-dst", 0.95, "high")

    results = matching.match_tracks([source], target, db=db)
    assert len(results) == 1
    assert results[0].best is not None
    assert results[0].best.track.id == "cached-dst"
    assert results[0].confidence == "high"
    db.close()


def test_match_tracks_writes_back_high_confidence_links() -> None:
    from harmony.db import Database

    db = Database(":memory:")
    source = _track(id="src1", title="Song Title", artists=["Artist"], duration_s=200)
    cand = Track(id="dst1", title="Song Title", service=Service.QOBUZ, artists=["Artist"], duration_s=200)
    target = FakeProvider(Service.QOBUZ, catalog=[cand])

    matching.match_tracks([source], target, db=db)

    link = db.get_link(Service.YTMUSIC, "src1", Service.QOBUZ)
    assert link is not None
    assert link["dst_id"] == "dst1"
    db.close()
