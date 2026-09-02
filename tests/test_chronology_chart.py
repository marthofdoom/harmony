"""Pure-geometry tests for the desktop member-chronology chart layout."""

from __future__ import annotations

import pytest

pytest.importorskip("gi")

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")

from harmony.ui.chronology_chart import _member_hue, layout_chronology  # noqa: E402


def test_layout_none_for_empty_or_bad_range():
    assert layout_chronology({}) is None
    assert layout_chronology({"members": [], "start_year": 2000, "end_year": 2010}) is None
    # end before start is nonsensical -> nothing to draw
    assert layout_chronology(
        {"members": [{"name": "X", "spans": [[2000, None]]}], "start_year": 2010, "end_year": 2000}
    ) is None


def test_layout_maps_spans_and_markers_to_fractions():
    ch = {
        "start_year": 1990, "end_year": 2010,
        "members": [{"name": "A", "instruments": ["guitar"], "spans": [[1990, 2000], [2005, None]]}],
        "albums": [{"title": "LP", "year": 2000, "ref": None}, {"title": "Undated", "year": None}],
    }
    layout = layout_chronology(ch)
    assert layout["span_years"] == 20
    member = layout["members"][0]
    assert member["instrument"] == "guitar"
    # 1990-2000 -> 0.0..0.5 ; 2005-open -> 0.75..1.0 (open end clamps to end_year)
    assert member["bars"] == [(0.0, 0.5), (0.75, 1.0)]
    # only the dated album becomes a marker, at the right fraction
    assert len(layout["markers"]) == 1
    assert layout["markers"][0]["x_frac"] == 0.5
    assert layout["content_width"] > 0 and layout["content_height"] > 0


def test_layout_open_start_clamps_to_range():
    ch = {"start_year": 2000, "end_year": 2020,
          "members": [{"name": "A", "spans": [[None, 2010]]}], "albums": []}
    layout = layout_chronology(ch)
    assert layout["members"][0]["bars"] == [(0.0, 0.5)]  # missing start -> from the beginning


def test_member_hue_is_valid_rgb():
    for i in range(5):
        r, g, b = _member_hue(i, 5)
        assert all(0.0 <= c <= 1.0 for c in (r, g, b))
