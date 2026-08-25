from __future__ import annotations

import typing

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


# -- bug 1: version markers erased when co-located with an edition tag ----
#
# _strip_noise used to drop an ENTIRE bracket if it contained any noise term
# (e.g. "Remaster", "Audio"), and _version_markers ran on that already-
# stripped text. A marker sharing a bracket with an edition tag was silently
# deleted from both the title comparison and the mismatch penalty, so a
# live/karaoke/remix/etc. recording could score a false 1.00 "exact" against
# the studio version. Verified failing strings from the bug report:


def test_score_live_marker_sharing_bracket_with_remaster_is_not_erased() -> None:
    source = _track(title="Comfortably Numb", artists=["Pink Floyd"], duration_s=383)
    cand = _track(
        id="dst1",
        title="Comfortably Numb (Live at Earls Court - 2011 Remaster)",
        artists=["Pink Floyd"],
        duration_s=383,
        service=Service.QOBUZ,
    )
    value, reasons = matching.score(source, cand)
    assert value < 1.0
    assert any("version mismatch" in r for r in reasons)
    # The marker must survive into the title text too, not just the penalty.
    assert "live" in matching.normalize_title(cand.title)


def test_score_karaoke_marker_sharing_bracket_with_audio_tag_is_not_erased() -> None:
    source = _track(title="Shape of You", artists=["Ed Sheeran"], duration_s=233)
    cand = _track(
        id="dst1",
        title="Shape of You (Karaoke Audio)",
        artists=["Ed Sheeran"],
        duration_s=233,
        service=Service.QOBUZ,
    )
    value, reasons = matching.score(source, cand)
    assert value < 1.0
    assert any("version mismatch" in r for r in reasons)
    assert "karaoke" in matching.normalize_title(cand.title)


def test_match_track_live_remaster_bracket_never_scores_exact_or_high() -> None:
    source = _track(title="Comfortably Numb", artists=["Pink Floyd"], duration_s=383)
    cand = Track(
        id="dst1",
        title="Comfortably Numb (Live at Earls Court - 2011 Remaster)",
        service=Service.QOBUZ,
        artists=["Pink Floyd"],
        duration_s=383,
    )
    target = FakeProvider(Service.QOBUZ, catalog=[cand])
    result = matching.match_track(source, target)
    assert result.confidence not in ("exact", "high")


def test_bracket_pure_noise_still_fully_stripped_no_stray_marker() -> None:
    # Regression guard: "(Radio Edit)" must still be dropped entirely, not
    # misread as carrying an "edit" version marker (radio edit is packaging,
    # not a musically distinct edit/rework).
    assert matching.normalize_title("Song Title (Radio Edit)") == "song title"
    assert "edit" not in matching._version_markers("Song Title (Radio Edit)")


# -- bug 2: "exact" must be reserved for ISRC-verified identity -----------


def test_confidence_perfect_fuzzy_score_is_high_not_exact() -> None:
    source = _track(title="Comfortably Numb", artists=["Pink Floyd"], duration_s=383, isrc=None)
    cand = Track(
        id="dst1",
        title="Comfortably Numb",
        service=Service.QOBUZ,
        artists=["Pink Floyd"],
        duration_s=383,
        isrc=None,
    )
    value, _ = matching.score(source, cand)
    assert value == 1.0  # perfect fuzzy score, no ISRC involved

    target = FakeProvider(Service.QOBUZ, catalog=[cand])
    result = matching.match_track(source, target)
    assert result.best is not None
    assert result.best.score == 1.0
    assert result.confidence == "high"


def test_confidence_isrc_match_is_still_exact() -> None:
    source = _track(title="Totally Different Title", isrc="US1234567890")
    cand = Track(
        id="dst1",
        title="Nothing Alike",
        service=Service.QOBUZ,
        artists=["Someone Else"],
        isrc="us1234567890",
    )
    target = FakeProvider(Service.QOBUZ, catalog=[cand])
    result = matching.match_track(source, target)
    assert result.confidence == "exact"


# -- bug 3: search query must be normalised before hitting the provider ---


class _QueryCapturingProvider:
    """Records every query passed to ``search``; returns canned results per call."""

    def __init__(self, service: Service, responses: list[list[Track]]) -> None:
        self.service = service
        self._responses = list(responses)
        self.queries: list[str] = []

    def search(self, query: str, *, kinds=("tracks",), limit: int = 25):
        from harmony.models import SearchResults

        self.queries.append(query)
        tracks = self._responses.pop(0) if self._responses else []
        return SearchResults(tracks=list(tracks[:limit]))


def test_match_track_query_is_denoised_and_uses_primary_artist_only() -> None:
    source = _track(
        title="Comfortably Numb (Official Video) [2011 Remaster]",
        artists=["Pink Floyd", "David Gilmour"],
    )
    cand = Track(id="dst1", title="Comfortably Numb", service=Service.QOBUZ, artists=["Pink Floyd"])
    target = _QueryCapturingProvider(Service.QOBUZ, responses=[[cand]])

    matching.match_track(source, target)

    assert len(target.queries) == 1
    query = target.queries[0]
    assert "Official Video" not in query
    assert "Remaster" not in query
    assert "David Gilmour" not in query  # only the primary artist is used
    assert "Comfortably Numb" in query
    assert "Pink Floyd" in query


def test_match_track_falls_back_to_raw_title_when_denoised_query_finds_nothing() -> None:
    source = _track(title="Comfortably Numb (Official Video)", artists=["Pink Floyd"])
    cand = Track(id="dst1", title="Comfortably Numb", service=Service.QOBUZ, artists=["Pink Floyd"])
    # First (denoised) query returns nothing, second (fallback) query finds it.
    target = _QueryCapturingProvider(Service.QOBUZ, responses=[[], [cand]])

    result = matching.match_track(source, target)

    assert len(target.queries) == 2
    assert target.queries[0] != target.queries[1]
    assert result.best is not None
    assert result.best.track.id == "dst1"


# -- bug 4: a cached "manual" link is authoritative ------------------------


def test_confidence_vocabulary_includes_manual() -> None:
    # "manual" must be part of the Confidence vocabulary, not just an
    # incidental string that happens to survive a db round-trip.
    assert "manual" in typing.get_args(matching.Confidence)


def test_match_tracks_cached_manual_link_is_returned_as_manual_not_downgraded() -> None:
    from harmony.db import Database

    db = Database(":memory:")
    source = _track(id="src1", title="Song Title", artists=["Artist"], duration_s=200)
    # Empty catalog: if the cache were bypassed a live search would find
    # nothing and the match would come back "none" instead of "manual".
    target = FakeProvider(Service.QOBUZ, catalog=[])

    db.put_link(Service.YTMUSIC, "src1", Service.QOBUZ, "user-picked-dst", 1.0, "manual")

    results = matching.match_tracks([source], target, db=db)
    assert len(results) == 1
    assert results[0].confidence == "manual"
    assert results[0].best is not None
    assert results[0].best.track.id == "user-picked-dst"
    assert results[0].best.score == 1.0
    db.close()


# -- bug 5: match thresholds must be injectable, not hardcoded only -------


def test_match_track_high_threshold_is_injectable() -> None:
    source = _track(title="Song Title", artists=["Artist"], duration_s=200)
    # A near-but-not-perfect match that lands in the default "high" band.
    cand = Track(id="dst1", title="Song Title", service=Service.QOBUZ, artists=["Artist"], duration_s=205)
    target = FakeProvider(Service.QOBUZ, catalog=[cand])

    default_result = matching.match_track(source, target)
    assert default_result.confidence == "high"

    # Raising the high threshold above the achieved score should push the
    # same match down a bucket, proving the threshold isn't hardcoded.
    strict_score = default_result.best.score
    strict_result = matching.match_track(source, target, high_threshold=strict_score + 0.01)
    assert strict_result.confidence == "low"


def test_match_tracks_thresholds_propagate_through_db_write_back() -> None:
    from harmony.db import Database

    db = Database(":memory:")
    source = _track(id="src1", title="Song Title", artists=["Artist"], duration_s=200)
    cand = Track(id="dst1", title="Song Title", service=Service.QOBUZ, artists=["Artist"], duration_s=205)
    target = FakeProvider(Service.QOBUZ, catalog=[cand])

    # With an unreachably high low_threshold, the match should not clear
    # even "low", and therefore must not be written back to the cache.
    matching.match_tracks([source], target, db=db, low_threshold=1.01, high_threshold=1.01)

    assert db.get_link(Service.YTMUSIC, "src1", Service.QOBUZ) is None
    db.close()


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
