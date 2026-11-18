"""The album + source/stream-quality block shared by the Now Playing bar and page
(same layout as web and Android; title and artist sit above it).

    Night Air                                           (page only; the bar folds it into the artist line)
    Library · harmony-server · FLAC · 24-bit / 96 kHz   [Hi-Res]

Only libadwaita style classes and named theme colours are used (``dim-label``,
``caption``, ``@accent_*``, ``@window_fg_color``) so every Harmony
theme, light or dark, restyles it.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, Gtk  # noqa: E402

from harmony.audio.bitperfect import VERIFIED, WARNING  # noqa: E402
from harmony.ui.playing_info import (  # noqa: E402
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
.hires-badge { border-radius: 99px; padding: 1px 7px; font-size: 0.75em; font-weight: bold; }
.bp-pill {
  border-radius: 99px; padding: 0 8px; min-height: 0; min-width: 0;
  font-size: 0.8em; font-weight: bold; border: none; box-shadow: none;
  background: alpha(@window_fg_color, 0.1); color: alpha(@window_fg_color, 0.55);
}
.bp-pill.bp-verified { background: alpha(@success_color, 0.2); color: @success_color; }
.bp-pill.bp-warning { background: alpha(@warning_color, 0.22); color: @warning_color; }
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
    """An optional album line, then the source / format line with a tier pill.
    The live local-output readout ("Output: …") is the quality line's tooltip,
    and stands in for the line while the stream's format isn't known yet."""

    def __init__(self, state: AppState, *, large: bool = False, show_album: bool = False) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2 if not large else 4)
        self.state = state
        install_css()
        css = "body" if large else "caption"

        self.show_album = show_album
        self.album = Gtk.Label(xalign=0.0, ellipsize=3, visible=False)
        self.album.add_css_class("dim-label")
        self.album.add_css_class(css)
        self.append(self.album)

        self.quality_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, visible=False)
        self.quality = Gtk.Label(xalign=0.0, ellipsize=3)
        self.quality.add_css_class("dim-label")
        self.quality.add_css_class(css)
        self.pill = Gtk.Label(visible=False)
        self.pill.add_css_class("tier-pill")
        self.quality_row.append(self.quality)
        self.quality_row.append(self.pill)
        # Bit-perfect indicator + switch for the local output: green only when
        # the output is verified unmodified, amber with the reason otherwise, grey
        # when off. The same widget on the bar and the Now Playing page.
        self.bp = Gtk.Button(visible=False, valign=Gtk.Align.CENTER)
        self.bp.add_css_class("bp-pill")
        self.bp_label = Gtk.Label(label="Bit-perfect")  # never ellipsised: the quality text yields first
        self.bp.set_child(self.bp_label)
        self.bp.connect("clicked", self._on_bp_clicked)
        self.quality_row.append(self.bp)
        self.append(self.quality_row)

    def update(self) -> None:
        pb = self.state.playback
        album = (getattr(pb.track, "album", None) or "") if self.show_album else ""
        self.album.set_label(album)
        self.album.set_visible(bool(album))

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
        self._update_bit_perfect()
        self.quality_row.set_visible(bool(line) or self.bp.get_visible())
        self.quality_row.set_tooltip_text(f"Output: {output}" if output else None)

    def _on_bp_clicked(self, _button: Gtk.Button) -> None:
        # Off is one click away. On is not: it pins the volume to 100% (a jump of
        # ~14 dB from a quiet 20%), so the pill opens the Audio settings, where the
        # switch and its explanation are, instead of enabling on a stray click.
        if self.state.bit_perfect:
            self.state.set_bit_perfect(False)
        else:
            from harmony.ui.widgets import open_preferences

            open_preferences(self, "audio")

    def _update_bit_perfect(self) -> None:
        show = self.state.local_output_active()
        self.bp.set_visible(show)
        if not show:
            return
        status = self.state.bit_perfect_status()
        self.bp_label.set_label(f"● {status.label}")
        for css, level in (("bp-verified", VERIFIED), ("bp-warning", WARNING)):
            if status.level == level:
                self.bp.add_css_class(css)
            else:
                self.bp.remove_css_class(css)
        action = "Click to turn bit-perfect mode off." if self.state.bit_perfect \
            else "Click to open the audio settings, where bit-perfect mode is switched on."
        self.bp.set_tooltip_text(f"{status.detail}\n\n{action}")
