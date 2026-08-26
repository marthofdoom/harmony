"""Blends Last.fm, ListenBrainz, and provider-native recommendations.

Each source votes on ``(artist, title)`` pairs with a fixed weight; votes are
summed after normalising names through ``harmony.matching``, so "Beyoncé"
and "beyonce" (or a stray "The") don't split into two entries. The blended
ranking is then resolved against the real catalog — a suggestion nobody can
actually play is worse than no suggestion at all.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..errors import NotSupportedError
from ..models import Track
from . import lastfm, listenbrainz, musicbrainz

if TYPE_CHECKING:
    from ..db import Database
    from ..providers.base import MusicProvider

log = logging.getLogger(__name__)

LASTFM_WEIGHT = 1.0
LISTENBRAINZ_WEIGHT = 0.8
PROVIDER_WEIGHT = 0.9

# A source's raw "match"/"score" can legitimately be 0 for a still-relevant
# suggestion (e.g. Last.fm sometimes omits it); floor it so a source's vote
# always counts for something rather than vanishing entirely.
_MIN_SOURCE_WEIGHT = 0.01


@dataclass(slots=True)
class Suggestion:
    artist: str
    title: str
    sources: list[str] = field(default_factory=list)
    score: float = 0.0
    resolved: Track | None = None
    reason: str = ""


class _Ballot:
    """Accumulates weighted votes for ``(artist, title)`` pairs, deduped by
    normalised key but displayed using whichever spelling was seen first."""

    def __init__(self) -> None:
        self.scores: dict[tuple[str, str], float] = {}
        self.sources: dict[tuple[str, str], list[str]] = {}
        self.display: dict[tuple[str, str], tuple[str, str]] = {}

    def add(self, artist: str, title: str, weight: float, source: str, exclude: set[tuple[str, str]]) -> None:
        from .. import matching

        if not artist or not title:
            return
        key = (matching.normalize_artist(artist), matching.normalize_title(title))
        if key in exclude:
            return
        self.scores[key] = self.scores.get(key, 0.0) + weight
        bucket = self.sources.setdefault(key, [])
        if source not in bucket:
            bucket.append(source)
        self.display.setdefault(key, (artist, title))

    def ranked(self, limit: int) -> list[tuple[tuple[str, str], float]]:
        return sorted(self.scores.items(), key=lambda kv: kv[1], reverse=True)[:limit]


class Recommender:
    """Combines enrichment sources and a provider's native recs into ranked suggestions."""

    def __init__(self, db: Database | None = None, settings: Any | None = None) -> None:
        self.db = db
        self.settings = settings

    def _enabled(self, flag: str) -> bool:
        if self.settings is None:
            return True
        return bool(getattr(self.settings, flag, True))

    # -- per-source gathering, each failing closed and logging, never raising --

    def _gather_lastfm(self, seed: Track) -> list[tuple[str, str, float]]:
        if not self._enabled("lastfm_enabled") or not seed.artists:
            return []
        try:
            similar = lastfm.similar_tracks(seed.artist_name, seed.title, db=self.db)
        except Exception as exc:  # noqa: BLE001 - one source failing must never be fatal
            log.warning("Last.fm similar_tracks failed for %s - %s: %s", seed.artist_name, seed.title, exc)
            return []
        return [(t.artist, t.name, t.match) for t in similar]

    def _resolve_mbid(self, seed: Track) -> str | None:
        if not self._enabled("musicbrainz_enabled"):
            return None
        try:
            meta = musicbrainz.canonical_metadata(seed.artist_name, seed.title, db=self.db)
        except Exception as exc:  # noqa: BLE001
            log.debug("MusicBrainz lookup failed for %s - %s: %s", seed.artist_name, seed.title, exc)
            return None
        return meta.get("mbid") if meta else None

    def _gather_listenbrainz(self, seed: Track) -> list[tuple[str, str, float]]:
        if not self._enabled("listenbrainz_enabled"):
            return []
        mbid = self._resolve_mbid(seed)
        if not mbid:
            return []
        try:
            recordings = listenbrainz.similar_recordings(mbid, db=self.db)
        except Exception as exc:  # noqa: BLE001
            log.warning("ListenBrainz similar_recordings failed for %s: %s", seed.title, exc)
            return []
        results: list[tuple[str, str, float]] = []
        for rec in recordings:
            name = rec.get("recording_name") or rec.get("name")
            artist = rec.get("artist_name") or rec.get("artist")
            score = rec.get("score", 0)
            if name and artist:
                results.append((str(artist), str(name), float(score) if isinstance(score, (int, float)) else 0.0))
        return results

    def _gather_provider(self, seed: Track, provider: MusicProvider) -> list[tuple[str, str, float]]:
        try:
            tracks = provider.similar_tracks(seed)
        except NotSupportedError:
            return []
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "%s similar_tracks failed for %s: %s", getattr(provider, "service", provider), seed.title, exc
            )
            return []
        return [(t.artist_name, t.title, 1.0) for t in tracks]

    # -- resolution against the real catalog --

    def _resolve_ranked(
        self,
        ballot: _Ballot,
        ranked: list[tuple[tuple[str, str], float]],
        provider: MusicProvider,
        reason_prefix: str,
    ) -> list[Suggestion]:
        from .. import matching

        suggestions: list[Suggestion] = []
        for key, total_score in ranked:
            artist, title = ballot.display[key]
            candidate = Track(id="", title=title, service=provider.service, artists=[artist])
            result = matching.match_track(candidate, provider)
            if result.best is None or result.confidence == "none":
                continue
            sources = ballot.sources[key]
            suggestions.append(
                Suggestion(
                    artist=artist,
                    title=title,
                    sources=sources,
                    score=total_score,
                    resolved=result.best.track,
                    # Build the reason directly rather than str.format so an
                    # artist/title carrying a literal { or } can't raise.
                    reason=f"{reason_prefix} ({', '.join(sources)})",
                )
            )
        return suggestions

    def similar_to_tracks(
        self,
        seeds: list[Track],
        provider: MusicProvider,
        *,
        limit: int = 30,
        exclude: set[tuple[str, str]] | None = None,
        progress: Callable[[float, str], None] | None = None,
    ) -> list[Suggestion]:
        """Blend similar-track sources across every seed and resolve the top ``limit``."""
        from .. import matching

        ballot = _Ballot()
        exclude_keys = set(exclude or ())
        for seed in seeds:
            exclude_keys.add((matching.normalize_artist(seed.artist_name), matching.normalize_title(seed.title)))

        total = len(seeds) or 1
        for i, seed in enumerate(seeds):
            for artist, title, match in self._gather_lastfm(seed):
                ballot.add(artist, title, LASTFM_WEIGHT * max(match, _MIN_SOURCE_WEIGHT), "lastfm", exclude_keys)
            for artist, title, match in self._gather_listenbrainz(seed):
                ballot.add(
                    artist, title, LISTENBRAINZ_WEIGHT * max(match, _MIN_SOURCE_WEIGHT), "listenbrainz", exclude_keys
                )
            for artist, title, match in self._gather_provider(seed, provider):
                ballot.add(
                    artist, title, PROVIDER_WEIGHT * max(match, _MIN_SOURCE_WEIGHT), "provider", exclude_keys
                )
            if progress is not None:
                progress((i + 1) / total, f"Gathering recommendations for {seed.title}")

        ranked = ballot.ranked(limit)
        return self._resolve_ranked(ballot, ranked, provider, "Similar")

    def similar_to_artist(self, artist_name: str, provider: MusicProvider, *, limit: int = 30) -> list[Suggestion]:
        """Blend an artist's own popular tracks (currently Last.fm only) and resolve them."""
        ballot = _Ballot()
        if self._enabled("lastfm_enabled"):
            try:
                top = lastfm.top_tracks_for_artist(artist_name, limit=limit, db=self.db)
            except Exception as exc:  # noqa: BLE001
                log.warning("Last.fm top_tracks_for_artist failed for %s: %s", artist_name, exc)
                top = []
            for track in top:
                ballot.add(
                    track.artist or artist_name,
                    track.name,
                    LASTFM_WEIGHT * max(track.match, _MIN_SOURCE_WEIGHT),
                    "lastfm",
                    set(),
                )

        ranked = ballot.ranked(limit)
        return self._resolve_ranked(ballot, ranked, provider, f"Popular by {artist_name}")

    def expand_playlist(self, tracks: list[Track], provider: MusicProvider, *, limit: int = 25) -> list[Suggestion]:
        """Suggest additions to an existing playlist, treating its tracks as seeds."""
        return self.similar_to_tracks(tracks, provider, limit=limit)
