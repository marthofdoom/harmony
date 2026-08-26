"""Selectable color themes: a small registry plus the logic to apply one.

Each ``Theme`` pairs an Adwaita light/dark/system color scheme with an
optional accent color override. Applying a theme is two independent GTK
operations — ``Adw.StyleManager`` owns light/dark, a single app-level
``Gtk.CssProvider`` owns the accent — done together here so callers (startup,
Preferences) never have to remember both steps or their ordering.

The palettes are original: they take cues from the general "dark UI with a
bold accent" aesthetic common across music players, but use bespoke names
and hand-picked hex values, not any real product's branding or colors.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")

from gi.repository import Adw, Gdk, Gtk  # noqa: E402

log = logging.getLogger(__name__)

#: Matches a strict lowercase ``#rrggbb`` hex color, the only accent shape
#: this module accepts (no shorthand ``#rgb``, no alpha, no named colors) —
#: keeping the CSS this module emits trivially predictable.
_HEX_COLOR_RE = re.compile(r"^#[0-9a-f]{6}$")

#: The Adwaita scheme names ``apply_theme`` accepts. Anything else is a bug
#: in this module's own registry, not user input.
_VALID_SCHEMES = ("system", "light", "dark")


@dataclass(frozen=True)
class Theme:
    """One selectable color theme.

    ``scheme`` maps to an ``Adw.ColorScheme`` (system/light/dark forced).
    ``accent`` is a ``#rrggbb`` hex string used to override libadwaita's
    named accent colors, or ``None`` to leave Adwaita's own default accent
    alone. ``extra_css`` is appended verbatim after the accent override for
    themes that want a little more flair than a recolored accent.
    """

    id: str
    name: str
    scheme: str
    accent: str | None
    extra_css: str = field(default="")


THEMES: list[Theme] = [
    Theme("system", "System", "system", None),
    Theme("midnight", "Midnight", "dark", "#1db07a"),
    Theme("ember", "Ember", "dark", "#e2584d"),
    Theme("tide", "Tide", "dark", "#2aa8b8"),
    Theme("aurora", "Aurora", "light", "#8a63d2"),
    Theme("slate", "Slate", "dark", "#5b7ba3"),
]

_THEMES_BY_ID: dict[str, Theme] = {t.id: t for t in THEMES}

#: The provider that owns every accent-override rule this module ever
#: writes. Created lazily on first use (not at import time, so importing
#: this module never requires a display to already exist) and reused for
#: every subsequent ``apply_theme`` call — reusing it, and calling
#: ``load_from_string`` again rather than adding a second provider, is what
#: makes switching themes live *replace* the override instead of stacking
#: an ever-growing pile of providers on the display.
_accent_provider: Gtk.CssProvider | None = None

_SCHEME_MAP = {
    "system": Adw.ColorScheme.DEFAULT,
    "light": Adw.ColorScheme.FORCE_LIGHT,
    "dark": Adw.ColorScheme.FORCE_DARK,
}


def theme_ids() -> list[tuple[str, str]]:
    """Return ``(id, name)`` pairs in display order, for populating a selector."""
    return [(t.id, t.name) for t in THEMES]


def theme_index(theme_id: str) -> int:
    """Index of ``theme_id`` within :data:`THEMES`, or ``0`` ("System") if unknown."""
    for i, t in enumerate(THEMES):
        if t.id == theme_id:
            return i
    return 0


def get_theme(theme_id: str) -> Theme:
    """Look up a theme by id, falling back to "system" for an unknown id.

    Never raises — an unrecognized id (a stale value from an older release,
    hand-edited settings.json, ...) degrades to the safe default rather than
    crashing preferences or startup.
    """
    theme = _THEMES_BY_ID.get(theme_id)
    if theme is None:
        log.warning("Unknown theme id %r; falling back to 'system'", theme_id)
        theme = _THEMES_BY_ID["system"]
    return theme


def _accent_css(theme: Theme) -> str:
    """Build the CSS ``apply_theme`` loads into the shared provider for ``theme``.

    Empty for the no-accent case (``accent is None``) so the provider's
    content is cleared and Adwaita's own default accent shows through again.
    """
    if theme.accent is None:
        return theme.extra_css
    if not _HEX_COLOR_RE.match(theme.accent):
        # Only reachable via a bad hand-edit of THEMES itself (the registry
        # tests below guard against that), not from any user-facing input —
        # theme ids are chosen from a fixed list, never typed. Fail safe by
        # dropping the malformed accent rather than emitting broken CSS.
        log.warning("Theme %r has a malformed accent %r; skipping accent CSS", theme.id, theme.accent)
        return theme.extra_css
    css = (
        f"@define-color accent_bg_color {theme.accent};\n"
        f"@define-color accent_color {theme.accent};\n"
        "@define-color accent_fg_color #ffffff;\n"
    )
    if theme.extra_css:
        css += theme.extra_css
    return css


def apply_theme(theme_id: str) -> None:
    """Apply the theme identified by ``theme_id``: color scheme + accent override.

    Safe to call repeatedly (e.g. once at startup, then again every time the
    user picks a different theme in Preferences) — the accent CSS provider
    is a module-level singleton whose content is replaced in place rather
    than a new provider being stacked on top of the old one each time.

    Also safe to call in a headless/test environment: if there is no
    ``Gdk.Display`` (no display server reachable), the accent step is
    skipped with a log message instead of raising, though the color-scheme
    step still runs since ``Adw.StyleManager`` needs no display.
    """
    theme = get_theme(theme_id)

    scheme = _SCHEME_MAP.get(theme.scheme, Adw.ColorScheme.DEFAULT)
    Adw.StyleManager.get_default().set_color_scheme(scheme)

    display = Gdk.Display.get_default()
    if display is None:
        log.info("No Gdk.Display available; skipping accent CSS for theme %r", theme.id)
        return

    global _accent_provider
    if _accent_provider is None:
        _accent_provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(
            display, _accent_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    _accent_provider.load_from_string(_accent_css(theme))
    log.debug("Applied theme %r (scheme=%s, accent=%s)", theme.id, theme.scheme, theme.accent)
