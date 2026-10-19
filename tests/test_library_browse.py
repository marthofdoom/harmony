"""iTunes-style Library browsing, audio quality and "playing from" context.

Uses generated WAV files (and real FLACs when ffmpeg is present) — no network."""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import wave
from pathlib import Path

import pytest

from harmony.library import LibraryIndex
from harmony.playqueue import PlayQueue
from harmony.quality import best, describe


def _wav(path: Path, rate: int = 8000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00\x00\x00" * rate)
    return path


def _genre(path: Path, genre: str, **tags: str) -> None:
    mutagen_wave = pytest.importorskip("mutagen.wave")
    from mutagen.id3 import TALB, TCON, TIT2, TPE1, TPE2

    f = mutagen_wave.WAVE(str(path))
    f.add_tags()
    f.tags.add(TCON(encoding=3, text=genre))
    for key, frame in (("title", TIT2), ("artist", TPE1), ("album", TALB), ("albumartist", TPE2)):
        if key in tags:
            f.tags.add(frame(encoding=3, text=tags[key]))
    f.save()


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "music"
    a = _wav(root / "Nightwish" / "Once (2004)" / "01 - Nemo.wav", 44100)
    b = _wav(root / "Nightwish" / "Once (2004)" / "02 - Wish.wav", 44100)
    c = _wav(root / "Björk" / "Homogenic (1997)" / "01 - Hunter.wav", 48000)
    _genre(a, "Metal", title="Nemo", artist="Nightwish", album="Once", albumartist="Nightwish")
    _genre(b, "Metal", title="Wish", artist="Nightwish", album="Once", albumartist="Nightwish")
    _genre(c, "Electronic", title="Hunter", artist="Björk", album="Homogenic", albumartist="Björk")
    index = LibraryIndex(tmp_path / "library.db")
    index.scan([root])
    yield index
    index.close()


# -- quality --------------------------------------------------------------------


def test_quality_labels_every_kind_of_source() -> None:
    assert describe("FLAC", bit_depth=24, sample_rate=96000)["label"] == "FLAC · 24-bit / 96 kHz · Hi-Res"
    q = describe("FLAC", bit_depth=16, sample_rate=44.1)  # Qobuz reports kHz
    assert q["label"] == "FLAC · 16-bit / 44.1 kHz" and q["tier"] == "lossless"
    assert describe("AAC", bitrate=256, lossless=False)["label"] == "AAC · 256 kbps"
    assert describe(None) is None
    assert best([describe("MP3", bitrate=320), describe("FLAC", bit_depth=24, sample_rate=192000)])["tier"] == "hires"


def test_index_reads_genre_and_format(lib: LibraryIndex) -> None:
    rows, total = lib.songs(order="title")
    assert total == 3
    hunter = next(r for r in rows if r["title"] == "Hunter")
    assert hunter["genre"] == "Electronic" and hunter["codec"] == "WAV"
    assert hunter["sample_rate"] == 48000 and hunter["bit_depth"] == 16 and hunter["channels"] == 2


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_hires_flac_is_recognised(tmp_path: Path) -> None:
    pytest.importorskip("mutagen")
    f = tmp_path / "m" / "A" / "B (2020)" / "01 - T.flac"
    f.parent.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=1",
                    "-ar", "96000", "-sample_fmt", "s32", str(f)], check=True)
    idx = LibraryIndex(tmp_path / "l.db")
    idx.scan([tmp_path / "m"])
    from harmony.providers.local import row_quality

    q = row_quality(idx.songs()[0][0])
    assert q["codec"] == "FLAC" and q["sample_rate"] == 96000 and q["tier"] == "hires"
    idx.close()


def test_old_index_is_migrated_and_rescanned(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE tracks (id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, size INTEGER, "
                "mtime REAL, title TEXT, artist TEXT, artist_id TEXT, album TEXT, album_artist TEXT, "
                "album_artist_id TEXT, album_id TEXT, track_no INTEGER, disc_no INTEGER, year INTEGER, "
                "date TEXT, duration_s INTEGER, isrc TEXT, mb_releasegroup TEXT, mb_release TEXT, "
                "mb_artist TEXT, mime TEXT NOT NULL, haystack TEXT NOT NULL, added_at REAL NOT NULL)")
    con.execute("INSERT INTO tracks VALUES ('x','/m/a.wav',1,123.0,'T','A','a','Al','A','a','al',"
                "1,1,2000,'2000',1,NULL,NULL,NULL,NULL,'audio/wav','t',0)")
    con.commit()
    con.close()
    idx = LibraryIndex(db)
    row = idx.track("x")
    assert "genre" in row and row["mtime"] == 0  # columns added; row will be re-read
    idx.close()


# -- browse views -------------------------------------------------------------------


def test_genres_artists_albums_songs_views(lib: LibraryIndex) -> None:
    genres = {g["name"]: g for g in lib.genres()}
    assert genres["Metal"]["album_count"] == 1 and genres["Metal"]["track_count"] == 2
    assert [a["name"] for a in lib.artists(genre="Metal")] == ["Nightwish"]
    assert lib.count_albums(genre="Electronic") == 1
    assert [a["album"] for a in lib.albums(order="title")] == ["Homogenic", "Once"]
    titles = [r["title"] for r in lib.songs(order="title", desc=True)[0]]
    assert titles == ["Wish", "Nemo", "Hunter"]
    page, total = lib.songs(order="title", limit=1, offset=1)
    assert total == 3 and [r["title"] for r in page] == ["Nemo"]
    by_artist = lib.artists()[0]
    assert by_artist["track_count"] >= 1 and by_artist["art_album_id"]


def test_engine_library_view_over_the_provider(lib: LibraryIndex) -> None:
    from harmony.library import ArtSigner
    from harmony.library_federation import FederatedLibraryProvider
    from harmony.providers.local import LocalLibraryProvider

    own = LocalLibraryProvider(lib, ArtSigner(b"k" * 32))
    fed = FederatedLibraryProvider(own, lambda: [], lambda: None)
    songs = fed.browse_view("songs", order="artist")
    assert [t.title for t in songs["songs"]] == ["Hunter", "Nemo", "Wish"] and songs["total"] == 3
    assert songs["songs"][0].quality["label"].startswith("WAV · 16-bit / 48 kHz")
    albums = fed.browse_view("albums", order="artist", genre="Metal")
    assert [a.title for a in albums["albums"]] == ["Once"] and albums["albums"][0].genre == "Metal"
    assert {g["name"] for g in fed.browse_view("genres")["genres"]} == {"Metal", "Electronic"}
    nightwish = next(a for a in fed.browse_view("artists")["artists"] if a["name"] == "Nightwish")
    assert [t.title for t in fed.browse_view("songs", artist_id=nightwish["id"])["songs"]] == ["Nemo", "Wish"]


# -- "playing from" ------------------------------------------------------------------


def test_queue_context_travels_with_a_load_and_into_device_snapshots(monkeypatch) -> None:
    q = PlayQueue()
    q.load([{"service": "local", "id": "1"}], 0, context={"kind": "album", "title": "Once"})
    assert q.context == {"kind": "album", "title": "Once"}
    from harmony.web import device_queue as dqmod
    from harmony.web.device_queue import DeviceQueues

    monkeypatch.setattr(dqmod, "SETTLE_S", 0.0)
    stream = {"quality": describe("FLAC", bit_depth=24, sample_rate=96000),
              "source": {"service": "local", "label": "Library", "instance": "server"}}
    queues = DeviceQueues(lambda h, t: {"ok": True, "stream": stream}, lambda h: {})
    monkeypatch.setattr(queues, "_ensure_poller", lambda host: None)
    snap = queues.op("wiim", "load", {"tracks": [{"service": "local", "id": "1"}], "start": 0,
                                      "context": {"kind": "genre", "title": "Metal"}})
    assert snap["context"] == {"kind": "genre", "title": "Metal"}
    assert snap["stream"]["quality"]["tier"] == "hires" and snap["stream"]["source"]["instance"] == "server"


@pytest.mark.parametrize("order", ["title", "artist", "album", "year", "genre", "duration", "recent", "quality"])
def test_every_song_sort_works_both_ways(lib: LibraryIndex, order: str) -> None:
    """Descending used to split the clause inside COALESCE( (a SQL error for
    Quality) and flip the null-sink instead of the key for Year/Time."""
    up = [r["id"] for r in lib.songs(order=order)[0]]
    down = [r["id"] for r in lib.songs(order=order, desc=True)[0]]
    assert sorted(up) == sorted(down) and len(up) == 3


def test_descending_reverses_the_key_not_the_nulls(lib: LibraryIndex) -> None:
    rates = [r["sample_rate"] for r in lib.songs(order="quality")[0]]
    assert rates == sorted(rates, reverse=True)          # best first by default
    assert [r["sample_rate"] for r in lib.songs(order="quality", desc=True)[0]] == sorted(rates)
    titles = [r["title"] for r in lib.songs(order="title", desc=True)[0]]
    assert titles == ["Wish", "Nemo", "Hunter"]
