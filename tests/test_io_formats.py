"""Round-trip and error-handling coverage for ``harmony.io_formats``.

Export writes real ``Track``/``Playlist`` objects; import is deliberately
"loose" and hands back ``(descriptors, warnings)`` tuples of plain dicts
rather than ``Track`` objects (see the module docstring). This file exercises
that contract directly — no GTK involved — plus ``resolve_imported``, which
re-resolves descriptors against a provider via ``matching.match_track``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import FakeProvider

from harmony import io_formats
from harmony.matching import MatchResult
from harmony.models import Playlist, Service, Track


def _track(**kwargs) -> Track:
    defaults = dict(
        id="t1",
        title="Come Together",
        service=Service.YTMUSIC,
        artists=["The Beatles"],
        album="Abbey Road",
        duration_s=259,
        isrc="GBAYE0601696",
    )
    defaults.update(kwargs)
    return Track(**defaults)


def _playlist(**kwargs) -> Playlist:
    defaults = dict(id="p1", title="My Playlist", service=Service.YTMUSIC, description="desc")
    defaults.update(kwargs)
    return Playlist(**defaults)


# --------------------------------------------------------------------------
# Export/import round trips
# --------------------------------------------------------------------------


def test_m3u_round_trip(tmp_path: Path) -> None:
    tracks = [
        _track(id="t1", title="Come Together", artists=["The Beatles"], duration_s=259),
        _track(id="t2", title="Something", artists=["The Beatles"], duration_s=182, isrc=None),
    ]
    path = tmp_path / "playlist.m3u"

    io_formats.export_m3u(tracks, path)
    text = path.read_text(encoding="utf-8")
    assert text.startswith("#EXTM3U")
    assert "Come Together" in text
    assert "ytmusic:t1" in text

    result = io_formats.import_m3u(path)
    assert isinstance(result, tuple)
    descriptors, warnings = result
    assert warnings == []
    assert len(descriptors) == 2
    assert descriptors[0]["title"] == "Come Together"
    assert descriptors[0]["artists"] == ["The Beatles"]
    assert descriptors[0]["duration_s"] == 259
    # M3U cannot carry album/isrc — importer never invents them.
    assert descriptors[0]["album"] is None
    assert descriptors[0]["isrc"] is None


def test_m3u_round_trip_missing_duration(tmp_path: Path) -> None:
    tracks = [_track(id="t1", duration_s=None)]
    path = tmp_path / "playlist.m3u"
    io_formats.export_m3u(tracks, path)
    descriptors, warnings = io_formats.import_m3u(path)
    assert warnings == []
    assert descriptors[0]["duration_s"] is None


def test_csv_round_trip(tmp_path: Path) -> None:
    tracks = [
        _track(id="t1", title="Come Together", artists=["The Beatles"], album="Abbey Road",
               duration_s=259, isrc="GBAYE0601696"),
        _track(id="t2", title="Yesterday", artists=["The Beatles", "Paul McCartney"], album=None,
               duration_s=None, isrc=None),
    ]
    path = tmp_path / "playlist.csv"

    io_formats.export_csv(tracks, path)
    result = io_formats.import_csv(path)
    assert isinstance(result, tuple)
    descriptors, warnings = result
    assert warnings == []
    assert len(descriptors) == 2

    first = descriptors[0]
    assert first["title"] == "Come Together"
    assert first["artists"] == ["The Beatles"]
    assert first["album"] == "Abbey Road"
    assert first["duration_s"] == 259
    assert first["isrc"] == "GBAYE0601696"

    second = descriptors[1]
    assert second["artists"] == ["The Beatles", "Paul McCartney"]
    assert second["album"] is None
    assert second["duration_s"] is None
    assert second["isrc"] is None


def test_json_round_trip_preserves_full_fidelity(tmp_path: Path) -> None:
    playlist = _playlist()
    tracks = [
        _track(id="t1", title="Come Together", artists=["The Beatles"], album="Abbey Road",
               duration_s=259, isrc="GBAYE0601696", year=1969, track_number=1, explicit=False),
    ]
    path = tmp_path / "playlist.json"

    io_formats.export_json(playlist, tracks, path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["playlist"]["title"] == "My Playlist"
    assert payload["tracks"][0]["id"] == "t1"

    result = io_formats.import_json(path)
    assert isinstance(result, tuple)
    descriptors, warnings = result
    assert warnings == []
    assert len(descriptors) == 1
    assert descriptors[0]["title"] == "Come Together"
    assert descriptors[0]["artists"] == ["The Beatles"]
    assert descriptors[0]["album"] == "Abbey Road"
    assert descriptors[0]["duration_s"] == 259
    assert descriptors[0]["isrc"] == "GBAYE0601696"


def test_json_import_accepts_bare_list(tmp_path: Path) -> None:
    path = tmp_path / "bare.json"
    path.write_text(json.dumps([{"title": "Solo Track", "artists": ["Someone"]}]), encoding="utf-8")
    descriptors, warnings = io_formats.import_json(path)
    assert warnings == []
    assert descriptors == [
        {"title": "Solo Track", "artists": ["Someone"], "album": None, "duration_s": None, "isrc": None}
    ]


# --------------------------------------------------------------------------
# Malformed input produces warnings, never exceptions
# --------------------------------------------------------------------------


def test_import_m3u_malformed_rows_produce_warnings(tmp_path: Path) -> None:
    path = tmp_path / "broken.m3u"
    path.write_text(
        "\n".join(
            [
                "#EXTM3U",
                "#EXTINF:not-a-number-no-comma",  # malformed: no comma
                "ytmusic:orphan1",
                "orphan-location-with-no-extinf",  # location with no preceding EXTINF
                "#EXTINF:200,NoTitleSeparator",  # no " - " separator
                "ytmusic:orphan2",
                "#EXTINF:200, - ",  # empty title after split
                "ytmusic:orphan3",
                "#EXTINF:210,Real Artist - Real Title",
                "ytmusic:good1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    descriptors, warnings = io_formats.import_m3u(path)
    assert len(descriptors) == 1
    assert descriptors[0]["title"] == "Real Title"
    assert len(warnings) >= 3
    assert all(isinstance(w, str) for w in warnings)


def test_import_csv_malformed_rows_produce_warnings(tmp_path: Path) -> None:
    path = tmp_path / "broken.csv"
    path.write_text(
        "title,artists,album,duration_s,isrc,service,id\n"
        "Good Title,Artist A,Album,200,,ytmusic,t1\n"
        ",Missing Title,Album,200,,ytmusic,t2\n"
        "Bad Duration,Artist,Album,not-a-number,,ytmusic,t3\n",
        encoding="utf-8",
    )
    descriptors, warnings = io_formats.import_csv(path)
    assert len(descriptors) == 1
    assert descriptors[0]["title"] == "Good Title"
    assert len(warnings) == 2
    assert "row 3" in warnings[0]
    assert "row 4" in warnings[1]


def test_import_json_malformed_items_produce_warnings(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text(
        json.dumps(
            {
                "tracks": [
                    {"title": "Good Track"},
                    {"artists": ["No Title Here"]},
                    {"title": "   "},
                    None,
                ]
            }
        ),
        encoding="utf-8",
    )
    descriptors, warnings = io_formats.import_json(path)
    assert len(descriptors) == 1
    assert descriptors[0]["title"] == "Good Track"
    assert len(warnings) == 3


def test_import_json_invalid_shape_produces_warning_not_exception(tmp_path: Path) -> None:
    path = tmp_path / "invalid_shape.json"
    path.write_text(json.dumps({"nope": "not a track list"}), encoding="utf-8")
    descriptors, warnings = io_formats.import_json(path)
    assert descriptors == []
    assert len(warnings) == 1


def test_import_json_unreadable_file_produces_warning_not_exception(tmp_path: Path) -> None:
    path = tmp_path / "not_json_at_all.json"
    path.write_text("{not valid json", encoding="utf-8")
    descriptors, warnings = io_formats.import_json(path)
    assert descriptors == []
    assert len(warnings) == 1


def test_import_csv_missing_file_raises(tmp_path: Path) -> None:
    # Unlike malformed *rows*, a missing file is an operational error the
    # caller (the import UI) needs to see, not something to silently skip.
    with pytest.raises(OSError):
        io_formats.import_csv(tmp_path / "does_not_exist.csv")


# --------------------------------------------------------------------------
# resolve_imported: descriptors -> MatchResult against a live provider
# --------------------------------------------------------------------------


def test_resolve_imported_matches_against_provider_catalog() -> None:
    catalog = [
        Track(id="c1", title="Come Together", service=Service.QOBUZ, artists=["The Beatles"],
              duration_s=259, isrc="GBAYE0601696"),
    ]
    provider = FakeProvider(Service.QOBUZ, catalog=catalog)
    descriptors = [
        {"title": "Come Together", "artists": ["The Beatles"], "album": None,
         "duration_s": 259, "isrc": "GBAYE0601696"},
    ]

    results = io_formats.resolve_imported(descriptors, provider)
    assert len(results) == 1
    assert isinstance(results[0], MatchResult)
    assert results[0].best is not None
    assert results[0].best.track.id == "c1"
    assert results[0].confidence == "exact"


def test_resolve_imported_no_match_when_catalog_empty() -> None:
    provider = FakeProvider(Service.QOBUZ, catalog=[])
    descriptors = [{"title": "Nothing Like This", "artists": ["Nobody"], "album": None,
                     "duration_s": None, "isrc": None}]

    results = io_formats.resolve_imported(descriptors, provider)
    assert len(results) == 1
    assert results[0].best is None
    assert results[0].confidence == "none"


def test_full_round_trip_export_import_resolve(tmp_path: Path) -> None:
    """Export a real playlist, import it back, and resolve to (mostly) the same tracks."""
    source_tracks = [
        Track(id="src1", title="Come Together", service=Service.YTMUSIC, artists=["The Beatles"],
              album="Abbey Road", duration_s=259, isrc="GBAYE0601696"),
        Track(id="src2", title="Yesterday", service=Service.YTMUSIC, artists=["The Beatles"],
              album="Help!", duration_s=125, isrc="GBAYE0601697"),
    ]
    path = tmp_path / "export.json"
    io_formats.export_json(_playlist(), source_tracks, path)

    descriptors, warnings = io_formats.import_json(path)
    assert warnings == []
    assert len(descriptors) == 2

    # Same catalog, but on Qobuz — simulates matching an imported playlist
    # against a different service.
    target_catalog = [
        Track(id="q1", title="Come Together", service=Service.QOBUZ, artists=["The Beatles"],
              duration_s=259, isrc="GBAYE0601696"),
        Track(id="q2", title="Yesterday", service=Service.QOBUZ, artists=["The Beatles"],
              duration_s=125, isrc="GBAYE0601697"),
    ]
    provider = FakeProvider(Service.QOBUZ, catalog=target_catalog)
    results = io_formats.resolve_imported(descriptors, provider)

    assert len(results) == 2
    assert all(r.best is not None for r in results)
    resolved_ids = {r.best.track.id for r in results}
    assert resolved_ids == {"q1", "q2"}
