"""A Wikipedia-style band member-chronology chart, drawn with Cairo.

The engine hands us a ``chronology`` dict (see the API contract): a year range,
one entry per member with dated ``spans``, and studio-album markers. This widget
lays that out as horizontal member lanes across a year axis, with vertical marker
lines at each album year — the same shape as the timeline graphics on a band's
Wikipedia page.

The geometry is split out into a pure :func:`layout_chronology` so it can be unit
-tested without a display; the widget only turns that layout into Cairo calls and
pulls its foreground colour from the current theme (so it reads in light *and*
dark). Member bars get distinct, deterministic hues so lanes stay distinguishable.
"""

from __future__ import annotations

import colorsys
from typing import Any

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # noqa: E402

# Layout constants (logical px). The widget grows with the data and scrolls
# horizontally inside its ScrolledWindow when the year span is wide.
_LANE_H = 34
_LANE_GAP = 6
_LABEL_W = 168          # left gutter for member names
_AXIS_H = 24            # top strip for year ticks
_MARKER_LABEL_H = 44    # bottom strip for rotated album titles
_PX_PER_YEAR = 26
_MIN_YEAR_SPAN_PX = 360


def _member_hue(index: int, total: int) -> tuple[float, float, float]:
    """A distinct, stable RGB for member ``index`` — evenly spaced hues."""
    hue = (index / max(total, 1)) % 1.0
    return colorsys.hls_to_rgb(hue, 0.55, 0.55)


def layout_chronology(chronology: dict[str, Any]) -> dict[str, Any] | None:
    """Turn a chronology dict into drawable geometry (pure; no GTK).

    Returns ``None`` when there's nothing to draw. Otherwise::

        {
          "start_year", "end_year", "span_years",
          "content_width", "content_height", "plot_width",
          "members": [{"name", "instrument", "index",
                       "bars": [(x0_frac, x1_frac)]}],   # fracs of the plot width
          "markers": [{"year", "title", "x_frac", "ref"}],
        }

    ``x_frac`` is a 0..1 fraction across the plotting area (start_year..end_year),
    so the widget maps it to pixels at whatever width it ends up with.
    """
    if not chronology:
        return None
    members = chronology.get("members") or []
    if not members:
        return None
    start = chronology.get("start_year")
    end = chronology.get("end_year")
    if start is None or end is None or end < start:
        return None
    span = max(end - start, 1)

    def frac(year: float) -> float:
        return min(max((year - start) / span, 0.0), 1.0)

    laid_members: list[dict[str, Any]] = []
    for i, m in enumerate(members):
        bars: list[tuple[float, float]] = []
        for pair in m.get("spans", []):
            s = pair[0] if pair[0] is not None else start
            e = pair[1] if pair[1] is not None else end
            if e < s:
                s, e = e, s
            bars.append((frac(s), frac(e)))
        instruments = m.get("instruments") or []
        laid_members.append({
            "name": m.get("name", ""),
            "instrument": instruments[0] if instruments else "",
            "index": i,
            "bars": bars,
        })

    markers: list[dict[str, Any]] = []
    for a in chronology.get("albums") or []:
        year = a.get("year")
        if year is None:
            continue
        markers.append({
            "year": year, "title": a.get("title", ""),
            "x_frac": frac(year), "ref": a.get("ref"),
        })

    plot_width = max(span * _PX_PER_YEAR, _MIN_YEAR_SPAN_PX)
    content_width = _LABEL_W + plot_width + 24
    content_height = _AXIS_H + len(members) * (_LANE_H + _LANE_GAP) + _MARKER_LABEL_H
    return {
        "start_year": start, "end_year": end, "span_years": span,
        "content_width": int(content_width), "content_height": int(content_height),
        "plot_width": int(plot_width), "members": laid_members, "markers": markers,
    }


class ChronologyChart(Gtk.DrawingArea):
    """Draws the member-chronology timeline for a group.

    Wrap it in a horizontally-scrolling ``Gtk.ScrolledWindow``; it requests a
    width wide enough for the full year span and scrolls when that exceeds the
    viewport. Call :meth:`set_chronology` with the engine's chronology dict.
    """

    def __init__(self) -> None:
        super().__init__()
        self._layout: dict[str, Any] | None = None
        self.set_draw_func(self._draw)
        self.set_content_height(120)

    def set_chronology(self, chronology: dict[str, Any] | None) -> None:
        self._layout = layout_chronology(chronology or {})
        if self._layout:
            self.set_content_width(self._layout["content_width"])
            self.set_content_height(self._layout["content_height"])
        self.queue_draw()

    def has_content(self) -> bool:
        return self._layout is not None

    def _draw(self, _area: Gtk.DrawingArea, cr: Any, width: int, height: int) -> None:
        layout = self._layout
        if not layout:
            return
        fg = self.get_color()  # current theme foreground (Gdk.RGBA)
        plot_x = _LABEL_W
        plot_w = width - _LABEL_W - 24
        if plot_w <= 0:
            return
        start, end, span = layout["start_year"], layout["end_year"], layout["span_years"]
        lanes_top = _AXIS_H

        def x_at(x_frac: float) -> float:
            return plot_x + x_frac * plot_w

        # Year axis: a tick every ~5 years (denser spans get more ticks).
        step = 5 if span > 12 else (2 if span > 4 else 1)
        cr.set_line_width(1)
        cr.select_font_face("Sans", 0, 0)
        cr.set_font_size(11)
        first = start - (start % step)
        year = first
        while year <= end:
            if year >= start:
                x = x_at((year - start) / span)
                cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.10)
                cr.move_to(x, lanes_top)
                cr.line_to(x, height - _MARKER_LABEL_H)
                cr.stroke()
                cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.55)
                cr.move_to(x + 2, 14)
                cr.show_text(str(year))
            year += step

        # Member lanes.
        total = len(layout["members"])
        for m in layout["members"]:
            row_y = lanes_top + m["index"] * (_LANE_H + _LANE_GAP)
            bar_y = row_y + 4
            bar_h = _LANE_H - 8
            r, g, b = _member_hue(m["index"], total)
            # Name + primary instrument in the left gutter.
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.95)
            cr.set_font_size(12)
            cr.move_to(8, row_y + _LANE_H / 2)
            cr.show_text(m["name"][:22])
            if m["instrument"]:
                cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.5)
                cr.set_font_size(9)
                cr.move_to(8, row_y + _LANE_H / 2 + 12)
                cr.show_text(m["instrument"][:26])
            # Tenure bars.
            for x0f, x1f in m["bars"]:
                x0, x1 = x_at(x0f), x_at(x1f)
                w = max(x1 - x0, 3)
                cr.set_source_rgba(r, g, b, 0.85)
                _rounded_rect(cr, x0, bar_y, w, bar_h, 4)
                cr.fill()

        # Album marker lines + rotated titles along the bottom.
        markers_top = lanes_top
        markers_bottom = height - _MARKER_LABEL_H
        for mk in layout["markers"]:
            x = x_at(mk["x_frac"])
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.35)
            cr.set_dash([2.0, 2.0], 0)
            cr.move_to(x, markers_top)
            cr.line_to(x, markers_bottom)
            cr.stroke()
            cr.set_dash([], 0)
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.8)
            cr.set_font_size(10)
            cr.save()
            cr.translate(x + 3, markers_bottom + 4)
            cr.rotate(0.6)
            cr.show_text(f"{mk['title'][:22]} ({mk['year']})")
            cr.restore()


def _rounded_rect(cr: Any, x: float, y: float, w: float, h: float, r: float) -> None:
    import math

    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()
