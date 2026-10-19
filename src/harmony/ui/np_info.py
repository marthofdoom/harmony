"""The "Playing from <source>" + stream-quality block shared by the Now Playing
bar and page.

    Playing from Library  ·  <album>            <- the album/playlist is a link
    Library · harmony-server · FLAC · 24-bit / 96 kHz   [Hi-Res]

Only libadwaita style classes and named theme colours are used (``dim-label``,
``caption``, ``link``, ``@accent_*``, ``@window_fg_color``) so every Harmony
theme, light or dark, restyles it.
"""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, Gtk  # noqa: E402

from harmony.ui.playing_info import (  # noqa: E402
    context_action,
    context_text,
    playing_from_text,
    quality_text,
    tier_pill,
)
from harmony.ui.state import AppState  # noqa: E402

# Colours are all named theme colours (the Harmony themes redefine accent_*;
# light/dark redefine the rest), never literals.
_CSS = """
.tier-pill {
  border-radius: 99px; padding: 0 7px; font-size: 0.8em; font-weight: bold;
  background: alpha(@window_fg_color, 0.12); color: @window_fg_color;
}
.tier-pill.hires { background: alpha(@accent_bg_color, 0.25); color: @accent_color; }
.ctx-link { padding: 0; min-height: 0; min-width: 0; }
.hires-badge { border-radius: 99px; padding: 1px 7px; font-size: 0.75em; font-weight: bold; }
"""
_css_installed = False


def install_css() -> None:
    """Install the pill/badge styles once (needs a display)."""
    global _css_installed
    display = Gdk.Display.get_default()
    if _css_installed or display is None:
        return
    provider = Gtk.CssProvider()
    if hasattr(provider, "load_from_string"):
        provider.load_from_string(_CSS)
    else:  # pragma: no cover - GTK < 4.12
        provider.load_from_data(_CSS.encode())
    Gtk.StyleContext.add_provider_for_display(display, provider,
                                              Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _css_installed = True


class NowPlayingInfo(Gtk.Box):
    """``Playing from <source>`` + a small album/playlist link, then the source /
    format line with a tier pill. The live local-output readout ("Output: …") is
    the quality line's tooltip, and stands in for the line while the stream's
    format isn't known yet."""

    def __init__(self, state: AppState, *, large: bool = False,
                 open_page: Callable[[str], None] | None = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2 if not large else 4)
        self.state = state
        self.open_page = open_page
        self._action: Callable[[], None] | None = None
        install_css()
        css = "body" if large else "caption"

        self.from_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4, visible=False)
        self.source = Gtk.Label(xalign=0.0, ellipsize=3)
        self.source.add_css_class("dim-label")
        self.source.add_css_class(css)
        self.link_label = Gtk.Label(ellipsize=3)
        self.link = Gtk.Button(child=self.link_label, visible=False, valign=Gtk.Align.CENTER)
        for c in ("link", "flat", "ctx-link", css):
            self.link.add_css_class(c)
        self.link.set_has_frame(False)
        self.link.connect("clicked", self._on_context_clicked)
        self.from_row.append(self.source)
        self.from_row.append(self.link)
        self.append(self.from_row)

        self.quality_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, visible=False)
        self.quality = Gtk.Label(xalign=0.0, ellipsize=3)
        self.quality.add_css_class("dim-label")
        self.quality.add_css_class(css)
        self.pill = Gtk.Label(visible=False)
        self.pill.add_css_class("tier-pill")
        self.quality_row.append(self.quality)
        self.quality_row.append(self.pill)
        self.append(self.quality_row)

    def _on_context_clicked(self, *_a: object) -> None:
        if self._action is not None:
            self._action()

    def update(self) -> None:
        pb = self.state.playback
        src = playing_from_text(pb.source)
        self.source.set_label(src)
        self.source.set_visible(bool(src))

        link = context_text(pb.context)
        self._action = context_action(pb.context, getattr(self.state, "navigator", None),
                                      self.open_page)
        # Show the collection as a link only when it can be opened; otherwise as
        # plain dim text (still useful: "Songs", a search).
        self.link.set_visible(bool(link))
        self.link_label.set_label(("· " if src else "") + link)
        self.link.set_sensitive(self._action is not None)
        self.link.set_tooltip_text(f"Open {link}" if self._action and link else None)
        self.from_row.set_visible(bool(src or link))

        output = self.state.local_audio_label()
        line = quality_text(pb.source, pb.quality) or (output or "")
        self.quality.set_label(line)
        pill = tier_pill(pb.quality)
        self.pill.set_label(pill or "")
        self.pill.set_visible(bool(pill))
        for c in ("hires",):
            if pill == "Hi-Res":
                self.pill.add_css_class(c)
            else:
                self.pill.remove_css_class(c)
        self.quality_row.set_visible(bool(line))
        self.quality_row.set_tooltip_text(f"Output: {output}" if output else None)
