"""Provider-independent entity overlays for the detail pages.

Composes :mod:`musicbrainz` + :mod:`wikipedia` into plain dicts the engine
merges onto provider data (playable albums/tracks with artwork and native ids).
Everything here is gi-free, network-only through the shared ``enrich`` helpers,
and week-cached — so a warm artist page makes zero live requests.

The engine owns provider IDs and artwork; this module owns *identity and
relationships* (is this a band or a person, who was in it and when, what did a
person perform on, who played on this recording). Kept apart so it stays unit
-testable without a running provider and reusable by every surface via the API.
"""

from __future__ import annotations

import datetime as _dt
import logging
from typing import TYPE_CHECKING, Any

from . import musicbrainz as mb
from . import wikipedia as wp

if TYPE_CHECKING:
    from ..db import Database

log = logging.getLogger(__name__)


def _current_year() -> int:
    return _dt.date.today().year


def artist_overlay(name: str, *, db: Database | None = None,
                   prefer_type: str | None = None) -> dict[str, Any] | None:
    """MusicBrainz identity for ``name`` — kind, bio, members, memberships, discography.

    Returns ``None`` when MusicBrainz has no confident match (the caller then
    renders a provider-only page). ``prefer_type`` is ``"Person"``/``"Group"``.

    Shape::

        {mbid, name, kind: "group"|"person"|"unknown", bio: {..}|None,
         members: [Member], member_of: [Band], urls: {rel: url},
         release_groups: [rg], studio_albums: [rg]}
    """
    match = mb.resolve_artist(name, db=db, prefer_type=prefer_type)
    if not match:
        return None
    mbid = match["mbid"]
    payload = mb.artist_lookup(mbid, db=db)
    mb_type = match.get("type") or payload.get("type")
    kind = {"Group": "group", "Person": "person"}.get(mb_type or "", "unknown")
    urls = mb.artist_urls(payload)
    rgs = mb.release_groups(mbid, db=db)
    return {
        "mbid": mbid,
        "name": match["name"] or payload.get("name", name),
        "kind": kind,
        "bio": wp.bio_from_urls(urls, db=db),
        "members": mb.members_of(payload) if kind == "group" else [],
        "member_of": mb.bands_of(payload) if kind == "person" else [],
        "urls": urls,
        "release_groups": rgs,
        "studio_albums": [rg for rg in rgs if mb.is_studio_album(rg)],
    }


def _year_in_spans(year: int | None, spans: list[list[int | None]]) -> bool:
    """True if ``year`` falls within any ``[start, end|None]`` membership span.

    A missing start means "from the beginning"; a ``None`` end means "still in
    the band". A release with no known year is included (better a false include
    than dropping a real album) only when the person was ever a current member.
    """
    if not spans:
        return False
    if year is None:
        return any(s[1] is None for s in spans)
    for start, end in spans:
        if (start is None or year >= start) and (end is None or year <= end):
            return True
    return False


def performed_discography(name: str, *, db: Database | None = None) -> dict[str, Any] | None:
    """A person's *performed-on* albums, chronological — the "Chester" query.

    For each band the person was a member of, take that band's studio albums
    released during the person's tenure. This is exactly the data the member
    -chronology chart encodes, so "Chester Bennington" yields the Linkin Park
    (and STP, Dead by Sunrise) records he was actually on — not the ones after
    his death or before he joined.

    Returns ``{artist: overlay, albums: [{title, year, mbid, band, band_mbid}]}``
    or ``None`` when the name doesn't resolve to a MusicBrainz person.
    """
    overlay = artist_overlay(name, db=db, prefer_type="Person")
    if not overlay or overlay["kind"] != "person":
        return None
    albums: list[dict[str, Any]] = []
    seen: set[str] = set()
    for band in overlay["member_of"]:
        band_mbid = band.get("mbid")
        if not band_mbid:
            continue
        spans = band.get("spans") or []
        for rg in mb.release_groups(band_mbid, db=db):
            if not mb.is_studio_album(rg):
                continue
            if not _year_in_spans(rg.get("year"), spans):
                continue
            dedup = f"{rg['title'].lower()}|{rg.get('year')}"
            if dedup in seen:
                continue
            seen.add(dedup)
            albums.append({
                "title": rg["title"], "year": rg.get("year"), "mbid": rg["mbid"],
                "band": band["name"], "band_mbid": band_mbid,
            })
    albums.sort(key=lambda a: (a["year"] is None, a["year"] or 0, a["title"]))
    return {"artist": overlay, "albums": albums}


def chronology(overlay: dict[str, Any]) -> dict[str, Any] | None:
    """Timeline-chart data from a *group* overlay: member spans + album markers.

    ``None`` unless the group has at least one member with a dated span (nothing
    to draw otherwise).
    """
    if overlay.get("kind") != "group":
        return None
    members = overlay.get("members") or []
    dated = [m for m in members if any(s[0] is not None for s in m.get("spans", []))]
    if not dated:
        return None
    studio = overlay.get("studio_albums") or []
    now = _current_year()

    starts = [s[0] for m in members for s in m["spans"] if s[0] is not None]
    ends = [s[1] for m in members for s in m["spans"] if s[1] is not None]
    album_years = [rg["year"] for rg in studio if rg.get("year")]
    ongoing = any(s[1] is None for m in members for s in m["spans"])

    start_year = min(starts + album_years) if (starts or album_years) else now
    end_year = now if ongoing else max(ends + album_years + [start_year])

    return {
        "start_year": start_year,
        "end_year": end_year,
        "members": [
            {"name": m["name"], "mbid": m.get("mbid"),
             "instruments": m.get("instruments", []), "spans": m["spans"]}
            for m in members
        ],
        "albums": [{"title": rg["title"], "year": rg["year"], "mbid": rg["mbid"]}
                   for rg in studio if rg.get("year")],
    }


# How many candidate recordings to look up before giving up on performer credits.
# MB attaches per-musician credits to specific recording MBIDs (often just the
# original studio take), not every re-release, so the first search hit frequently
# has none — but each lookup costs a rate-limited request, so this is bounded.
_PERFORMER_CANDIDATES = 5


def performers(*, isrc: str | None, artist: str, title: str,
               db: Database | None = None) -> list[dict[str, Any]]:
    """Who *performed* on a recording (vocals/instruments), not who wrote it.

    Resolution order: the exact recording behind ``isrc`` first, then the
    best-scoring artist+title recordings. Because MB stores performer credits on
    individual recordings, the search hits are tried in score order and the first
    one carrying real performer relations wins. Empty list when none is credited
    (common for pop/rock catalogue entries; richest for classical and jazz).
    """
    candidate_mbids: list[str] = []
    if isrc:
        for rec in mb.recordings_by_isrc(isrc, db=db):
            if rec.get("id"):
                candidate_mbids.append(rec["id"])
    for rec in mb.search_recordings(artist, title, db=db)[:_PERFORMER_CANDIDATES]:
        mbid = rec.get("id")
        if mbid and mbid not in candidate_mbids:
            candidate_mbids.append(mbid)

    for mbid in candidate_mbids[:_PERFORMER_CANDIDATES]:
        found = mb.performers_of(mb.recording_lookup(mbid, db=db))
        if found:
            return found
    return []
