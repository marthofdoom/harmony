"""'Playing from <source>' text, the shared info widget, and the Library page pieces."""

from __future__ import annotations

import pytest

from harmony.ui.playing_info import (
    playing_from_text,
    quality_text,
    tier_pill,
)

HIRES = {"codec": "FLAC", "bit_depth": 24, "sample_rate": 96000, "tier": "hires",
         "label": "FLAC · 24-bit / 96 kHz · Hi-Res", "lossless": True}
CD = {"codec": "FLAC", "bit_depth": 16, "sample_rate": 44100, "tier": "lossless",
      "label": "FLAC · 16-bit / 44.1 kHz", "lossless": True}
LIB = {"service": "local", "label": "Library", "instance": "harmony-server", "own": True}


def test_playing_from_names_the_source_never_the_collection() -> None:
    assert playing_from_text(LIB) == "Playing from Library"
    assert playing_from_text({"service": "qobuz", "label": "Qobuz"}) == "Playing from Qobuz"
    assert playing_from_text({"service": "ytmusic", "label": "YouTube Music"}) == \
        "Playing from YouTube Music"
    assert playing_from_text(None) == ""


def test_quality_line_and_pills() -> None:
    assert quality_text(LIB, HIRES) == "Library · harmony-server · FLAC · 24-bit / 96 kHz"
    assert quality_text({"service": "qobuz", "label": "Qobuz"}, CD) == \
        "Qobuz · FLAC · 16-bit / 44.1 kHz"
    assert quality_text(None, None) == ""
    assert tier_pill(HIRES) == "Hi-Res" and tier_pill(CD) == "Lossless"
    assert tier_pill({"tier": "lossy"}) is None and tier_pill(None) is None


gi = pytest.importorskip("gi")


def test_info_widget_renders_source_quality_and_pill(monkeypatch, tmp_path) -> None:
    from gi.repository import GObject

    from harmony import config as config_module
    from harmony.ui.np_info import NowPlayingInfo
    from harmony.ui.state import AppState

    monkeypatch.setattr(config_module, "settings_path", lambda: tmp_path / "s.json")
    state = AppState.__new__(AppState)
    GObject.Object.__init__(state)
    state.settings = config_module.Settings.load()
    state._init_playback()
    info = NowPlayingInfo(state)
    info.update()
    assert not info.from_row.get_visible() and not info.quality_row.get_visible()

    pb = state.playback
    pb.source, pb.quality = LIB, HIRES
    pb.context = {"kind": "songs", "title": "Songs"}
    info.update()
    assert info.source.get_label() == "Playing from Library"
    assert info.quality.get_label() == "Library · harmony-server · FLAC · 24-bit / 96 kHz"
    assert info.pill.get_visible() and info.pill.get_label() == "Hi-Res"

    pb.source, pb.quality = {"service": "qobuz", "label": "Qobuz"}, {**CD}
    info.update()
    assert info.source.get_label() == "Playing from Qobuz"
    assert info.pill.get_label() == "Lossless"


def test_song_column_headers_requery_with_server_sort_keys() -> None:
    from harmony.ui.library_page import SONG_COLUMNS, SONG_SORTS
    from harmony.ui.widgets import build_track_column_view

    got: list[tuple[str, bool]] = []
    from gi.repository import Gtk

    cv, _store, _sel = build_track_column_view(columns=SONG_COLUMNS, sort_keys=SONG_SORTS,
                                               on_sort=lambda k, d: got.append((k, d)))
    cols = {c.get_title(): c for c in cv.get_columns()}
    assert {"Genre", "Year", "Quality"} <= set(cols)
    cv.sort_by_column(cols["Genre"], Gtk.SortType.ASCENDING)
    cv.sort_by_column(cols["Year"], Gtk.SortType.DESCENDING)
    assert got == [("genre", False), ("year", True)]


def test_pager_pages_and_drops_stale_results(monkeypatch) -> None:
    from harmony.ui import library_page

    pending: list = []
    monkeypatch.setattr(library_page, "run_async",
                        lambda fn, done, err: pending.append((fn, done, err)))
    monkeypatch.setattr(library_page, "PAGE", 2)
    seen: list[tuple[list, bool]] = []
    data = list(range(5))
    pager = library_page._Pager(lambda off, lim: (data[off:off + lim], 5),
                                lambda items, first: seen.append((items, first)), lambda e: None)
    pager.reset()
    pager.load_more()                    # busy: no second request
    assert len(pending) == 1
    fn, done, _ = pending.pop()
    done(fn())
    assert seen == [([0, 1], True)] and pager.offset == 2
    pager.load_more()
    stale_fn, stale_done, _ = pending.pop()
    pager.reset()                        # a re-sort while page 2 is in flight
    stale_done(stale_fn())               # dropped
    assert seen == [([0, 1], True)]
    fn, done, _ = pending.pop()
    done(fn())
    assert seen[-1] == ([0, 1], True)
