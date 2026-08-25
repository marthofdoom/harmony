"""Fuzzy cross-service track matching.

Two catalog entries for "the same" recording rarely agree on punctuation,
featured-artist placement, or bracketed edition tags ("(2011 Remaster)",
"(Live)", "[Official Video]"...). This module normalises both sides before
comparing so that noise which does not change *what the recording is*
(remaster tags, video-platform cruft) does not tank the score, while noise
that *does* change the recording (a live take vs. the studio version) is
still penalised.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from statistics import mean
from typing import TYPE_CHECKING, Literal

from rapidfuzz import fuzz

from .db import Database
from .models import Track

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from .providers.base import MusicProvider

log = logging.getLogger(__name__)

# Thresholds mirror config.Settings' defaults (match_high_threshold /
# match_low_threshold) so the two stay in sync conceptually even though
# Settings is the user-facing knob and these are the hardcoded fallback.
HIGH_THRESHOLD = 0.88
LOW_THRESHOLD = 0.70

Confidence = Literal["exact", "high", "low", "manual", "none"]


@dataclass(slots=True)
class MatchCandidate:
    track: Track
    score: float
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MatchResult:
    source: Track
    best: MatchCandidate | None
    candidates: list[MatchCandidate]
    confidence: Confidence


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------

# Phrases that describe packaging/edition/platform metadata rather than a
# musically distinct version of the recording. These are safe to discard
# entirely when comparing titles.
_NOISE_TERMS = (
    r"remaster(?:ed)?(?:\s+\d{4})?",
    r"\d{4}\s+remaster(?:ed)?",
    r"mono\s+version",
    r"stereo\s+version",
    r"deluxe\s+edition",
    r"bonus\s+edition",
    r"expanded\s+edition",
    r"radio\s+edit",
    r"single\s+version",
    r"album\s+version",
    r"official\s+music\s+video",
    r"official\s+video",
    r"lyric\s+video",
    r"audio",
    r"hd",
    r"hq",
    r"explicit",
    r"clean",
)
_NOISE_ANY_RE = re.compile(r"(?i)\b(?:" + "|".join(_NOISE_TERMS) + r")\b")
_BRACKET_RE = re.compile(r"[\(\[]([^\(\)\[\]]*)[\)\]]")
_TRAILING_SUFFIX_RE = re.compile(r"(?i)\s*[-–—]\s*(?:" + "|".join(_NOISE_TERMS) + r")\s*$")
_BRACKET_JUNK_RE = re.compile(r"[-–—,:;]+")

_FEAT_START_RE = re.compile(r"(?i)[\(\[]?\s*(?:feat\.?|ft\.?|featuring)\s*[:.]?\s+")
_ARTIST_SPLIT_RE = re.compile(r"(?i)\s*(?:,|/|&|\band\b)\s*")

# Words that flag a title as a distinct rendition of the underlying song.
# Kept separate from _NOISE_TERMS: these change what the recording *is* and
# must not be silently discarded, only used for the version-mismatch penalty.
_VERSION_MARKERS = {
    "live": r"\blive\b",
    "acoustic": r"\bacoustic\b",
    "remix": r"\bre-?mix(?:ed)?\b",
    "instrumental": r"\binstrumental\b",
    "karaoke": r"\bkaraoke\b",
    "cover": r"\bcover\b",
    "demo": r"\bdemo\b",
    "edit": r"\bedit\b",
}


def _has_version_marker(text: str) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in _VERSION_MARKERS.values())


_PUNCT_RE = re.compile(r"[^\w\s']", re.UNICODE)
_STRAY_APOSTROPHE_RE = re.compile(r"(?<!\w)'|'(?!\w)")
_WHITESPACE_RE = re.compile(r"\s+")


def split_features(title: str) -> tuple[str, list[str]]:
    """Pull "feat./ft./featuring X[, Y]" out of a title.

    Returns ``(title_without_feature_clause, [artist, ...])``. The featured
    artists are wanted for artist-set comparison (a track credited only to
    "Artist A" should still match "Artist A feat. Artist B" reasonably well)
    but must not stay in the title text or they will drag down the title
    fuzzy-match score.
    """
    m = _FEAT_START_RE.search(title)
    if not m:
        return _WHITESPACE_RE.sub(" ", title).strip(), []
    rest = title[m.end() :]
    end = re.search(r"[\)\]]", rest)
    if end:
        clause, after = rest[: end.start()], rest[end.end() :]
    else:
        clause, after = rest, ""
    artists = [a.strip(" .") for a in _ARTIST_SPLIT_RE.split(clause) if a.strip(" .")]
    base = title[: m.start()] + after
    base = _WHITESPACE_RE.sub(" ", base).strip()
    return base, artists


def _clean_bracket_content(content: str) -> str:
    """Remove noise phrases from bracket ``content``, tidy leftover punctuation."""
    cleaned = _NOISE_ANY_RE.sub(" ", content)
    cleaned = _BRACKET_JUNK_RE.sub(" ", cleaned)
    return _WHITESPACE_RE.sub(" ", cleaned).strip()


def _bracket_sub(m: re.Match[str]) -> str:
    content = m.group(1)
    if not _NOISE_ANY_RE.search(content):
        # No packaging/edition noise in this bracket at all: leave it alone
        # (it might be a bare version marker like "(Live)", or just be
        # unrelated text we have no basis for discarding).
        return m.group(0)
    cleaned = _clean_bracket_content(content)
    if cleaned and _has_version_marker(cleaned):
        # The bracket mixes a real version marker with packaging noise, e.g.
        # "(Live at Earls Court - 2011 Remaster)". Strip only the noise
        # phrase(s) so the marker survives into both the title comparison
        # and _version_markers()'s mismatch penalty below.
        return f"({cleaned})"
    # Pure packaging/edition noise (e.g. "(Radio Edit)", "(2011 Remaster)"):
    # safe to discard the whole bracket, as before.
    return ""


def _strip_noise(title: str) -> str:
    """Drop feature clauses and edition/packaging noise, keep version words."""
    text, _ = split_features(title)
    while True:
        stripped = _BRACKET_RE.sub(_bracket_sub, text)
        if stripped == text:
            break
        text = stripped
    while True:
        stripped = _TRAILING_SUFFIX_RE.sub("", text)
        if stripped == text:
            break
        text = stripped
    return text


def _strip_punct(text: str) -> str:
    text = _PUNCT_RE.sub(" ", text)
    text = _STRAY_APOSTROPHE_RE.sub(" ", text)
    return text


def normalize_title(s: str) -> str:
    """Casefolded, noise-free, punctuation-light title for fuzzy comparison."""
    text = _strip_noise(s)
    text = _strip_punct(text)
    return _WHITESPACE_RE.sub(" ", text).strip().casefold()


def normalize_artist(s: str) -> str:
    text = s.casefold().strip()
    text = re.sub(r"^\s*the\s+", "", text)
    text = re.sub(r"\s*&\s*", " and ", text)
    text = _strip_punct(text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def _normalize_album(s: str) -> str:
    text = _strip_punct(s.casefold())
    return _WHITESPACE_RE.sub(" ", text).strip()


def _version_markers(title: str) -> set[str]:
    """Which rendition markers appear in ``title``, after noise is stripped.

    Noise stripping runs first so "Radio Edit" (a packaging label, already
    covered by _NOISE_TERMS) does not leave a stray "edit" behind to be
    mistaken for an actual edit/rework of the song.
    """
    text = _strip_noise(title).casefold()
    return {name for name, pattern in _VERSION_MARKERS.items() if re.search(pattern, text)}


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def _artist_pool(track: Track) -> set[str]:
    """All artist names attributable to ``track``: credited + featured-in-title."""
    _, featured = split_features(track.title)
    names = [*track.artists, *featured]
    return {normalize_artist(n) for n in names if n.strip()}


def _artist_similarity(source: Track, cand: Track) -> float:
    """Set-based artist comparison, robust to missing/extra collaborators.

    Combines two views and takes the max: a token_set_ratio over the joined,
    sorted artist strings (good when one side lists a superset of the
    other's artists), and a symmetric per-artist best-match average (good
    when names are spelled differently but pair up one-to-one).
    """
    set_a, set_b = _artist_pool(source), _artist_pool(cand)
    if not set_a or not set_b:
        return 0.0
    joined_a, joined_b = " ".join(sorted(set_a)), " ".join(sorted(set_b))
    set_ratio = fuzz.token_set_ratio(joined_a, joined_b) / 100

    def best_match_avg(xs: set[str], ys: set[str]) -> float:
        return mean(max(fuzz.WRatio(x, y) for y in ys) / 100 for x in xs)

    per_artist = mean([best_match_avg(set_a, set_b), best_match_avg(set_b, set_a)])
    return max(set_ratio, per_artist)


def _duration_score(a: int | None, b: int | None) -> float:
    """Full credit inside 2s, zero at/after 15s, linear in between.

    Missing duration on either side gets a neutral 0.7: not a full match
    (we genuinely don't know), but not zeroed out either, since duration is
    the least reliable field providers give us.
    """
    if a is None or b is None:
        return 0.7
    delta = abs(a - b)
    if delta <= 2:
        return 1.0
    if delta >= 15:
        return 0.0
    return 1.0 - (delta - 2) / 13


def score(source: Track, cand: Track) -> tuple[float, list[str]]:
    """Weighted similarity in [0, 1] plus human-readable reasons.

    ISRC equality is treated as ground truth and short-circuits everything
    else. Otherwise: title 0.5, artist 0.35, duration 0.15, +0.05 bonus if
    albums also match, then a 0.75x penalty if the two sides disagree on
    being a live/remix/acoustic/etc. rendition.
    """
    if source.isrc and cand.isrc and source.isrc.strip().upper() == cand.isrc.strip().upper():
        return 1.0, ["isrc exact match"]

    title_score = fuzz.token_sort_ratio(normalize_title(source.title), normalize_title(cand.title)) / 100
    artist_score = _artist_similarity(source, cand)
    duration_score = _duration_score(source.duration_s, cand.duration_s)

    reasons = [
        f"title~{title_score:.2f}",
        f"artist~{artist_score:.2f}",
        f"duration~{duration_score:.2f}",
    ]

    total = title_score * 0.5 + artist_score * 0.35 + duration_score * 0.15

    if source.album and cand.album and _normalize_album(source.album) == _normalize_album(cand.album):
        total += 0.05
        reasons.append("album match bonus")

    total = min(total, 1.0)

    markers_a, markers_b = _version_markers(source.title), _version_markers(cand.title)
    if markers_a != markers_b:
        total *= 0.75
        reasons.append(f"version mismatch: {sorted(markers_a)} vs {sorted(markers_b)}")

    return total, reasons


def _confidence(
    score_value: float,
    *,
    is_isrc: bool = False,
    high_threshold: float = HIGH_THRESHOLD,
    low_threshold: float = LOW_THRESHOLD,
) -> Confidence:
    """Map a numeric score to a confidence bucket.

    "exact" is reserved for ISRC-verified identity (``is_isrc=True``) — the UI
    and sync engine treat it as ground truth, so a merely perfect *fuzzy*
    score (title/artist/duration all lining up) must top out at "high"
    instead of impersonating an ISRC match.
    """
    if is_isrc:
        return "exact"
    if score_value >= high_threshold:
        return "high"
    if score_value >= low_threshold:
        return "low"
    return "none"


def _search_query(source: Track) -> str:
    """Primary search query: denoised title + primary artist only.

    Edition/platform cruft ("(Official Video)", "(2011 Remaster)", "feat. …")
    and comma-joining every credited artist confuses provider search engines
    and yields poor or empty result sets on real catalogs. A single primary
    artist plus a cleaned-up title reads like what a person would type into
    a search box, which is exactly what these are.
    """
    primary_artist = source.artists[0] if source.artists else ""
    title = _strip_noise(source.title)
    return _WHITESPACE_RE.sub(" ", f"{title} {primary_artist}").strip()


def _fallback_search_query(source: Track) -> str:
    """Fallback query: original, unstripped title + primary artist.

    Used only when the primary (denoised) query comes back empty, in case
    the aggressive cleanup stripped something the target's search index
    actually needed (e.g. a distinctive live/remix tag).
    """
    primary_artist = source.artists[0] if source.artists else ""
    return _WHITESPACE_RE.sub(" ", f"{source.title} {primary_artist}").strip()


def match_track(
    source: Track,
    target: MusicProvider,
    *,
    limit: int = 8,
    high_threshold: float = HIGH_THRESHOLD,
    low_threshold: float = LOW_THRESHOLD,
) -> MatchResult:
    """Search ``target`` for ``source`` and rank whatever comes back."""
    query = _search_query(source)
    results = target.search(query, kinds=("tracks",), limit=limit)
    pool = list(results.tracks) if results is not None else []

    if not pool:
        fallback = _fallback_search_query(source)
        if fallback and fallback != query:
            results = target.search(fallback, kinds=("tracks",), limit=limit)
            pool = list(results.tracks) if results is not None else []

    if not pool:
        return MatchResult(source=source, best=None, candidates=[], confidence="none")

    candidates = []
    for track in pool:
        s, reasons = score(source, track)
        candidates.append(MatchCandidate(track=track, score=s, reasons=reasons))
    candidates.sort(key=lambda c: c.score, reverse=True)
    best = candidates[0]
    is_isrc = bool(
        source.isrc and best.track.isrc and source.isrc.strip().upper() == best.track.isrc.strip().upper()
    )
    confidence = _confidence(best.score, is_isrc=is_isrc, high_threshold=high_threshold, low_threshold=low_threshold)
    return MatchResult(source=source, best=best, candidates=candidates, confidence=confidence)


def match_tracks(
    sources: Sequence[Track],
    target: MusicProvider,
    *,
    progress: Callable[[float, str], None] | None = None,
    db: Database | None = None,
    high_threshold: float = HIGH_THRESHOLD,
    low_threshold: float = LOW_THRESHOLD,
) -> list[MatchResult]:
    """Match every track in ``sources`` against ``target``.

    When ``db`` is given, a cached link short-circuits the network search
    entirely, and any newly found exact/high match is written back for next
    time. A cached link is authoritative regardless of how it got there: a
    "manual" link recorded by the sync engine for a user-resolved match is
    returned as-is (confidence "manual", score 1.0) and is never re-derived
    or downgraded — a human already made this call. The cached candidate's
    ``Track`` is a stand-in built from the source's own metadata (the db only
    stores the id/score/confidence triple, not a full payload) — good enough
    for callers that only need the id to add/remove tracks.
    """
    results: list[MatchResult] = []
    total = len(sources)
    for i, source in enumerate(sources):
        result = _match_one(source, target, db=db, high_threshold=high_threshold, low_threshold=low_threshold)
        results.append(result)
        if progress is not None:
            progress((i + 1) / total if total else 1.0, source.title)
    return results


def _match_one(
    source: Track,
    target: MusicProvider,
    *,
    db: Database | None,
    high_threshold: float = HIGH_THRESHOLD,
    low_threshold: float = LOW_THRESHOLD,
) -> MatchResult:
    if db is not None:
        link = db.get_link(source.service, source.id, target.service)
        if link is not None:
            stand_in = Track(
                id=link["dst_id"],
                title=source.title,
                service=target.service,
                artists=list(source.artists),
                album=source.album,
                duration_s=source.duration_s,
                isrc=source.isrc,
            )
            candidate = MatchCandidate(track=stand_in, score=link["score"], reasons=["cached link"])
            return MatchResult(
                source=source, best=candidate, candidates=[candidate], confidence=link["confidence"]
            )

    result = match_track(source, target, high_threshold=high_threshold, low_threshold=low_threshold)
    if db is not None and result.best is not None and result.confidence in ("exact", "high"):
        db.put_link(
            source.service, source.id, target.service, result.best.track.id, result.best.score, result.confidence
        )
    return result
