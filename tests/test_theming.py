"""Offline tests for the color-theme registry and apply logic.

Nothing here touches a real display or the user's real config dir:
``apply_theme`` is exercised as-is (it already guards ``Gdk.Display.get_default()``
being ``None``, which is exactly the case in this headless test environment,
so calling it must be a clean no-op past the color-scheme step rather than
raising), and the settings round-trip points ``Settings`` at a tmp file the
same way ``tests/test_devices.py`` does.
"""

from __future__ import annotations

import re

import pytest

# theming.py imports gi (Gtk/Adw/Gdk). The multi-version offline CI job has
# no GTK, so skip cleanly there; the GTK ui-smoke job runs these.
pytest.importorskip("gi")

from harmony import config as config_module  # noqa: E402
from harmony.ui.theming import THEMES, apply_theme, get_theme, theme_ids, theme_index  # noqa: E402

_HEX_RE = re.compile(r"^#[0-9a-f]{6}$")
_KEBAB_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


# -- registry shape -----------------------------------------------------------


def test_registry_includes_system() -> None:
    ids = [t.id for t in THEMES]
    assert "system" in ids


def test_registry_has_multiple_original_themes() -> None:
    # "system" plus a handful of originals.
    assert len(THEMES) >= 5


def test_theme_ids_are_unique() -> None:
    ids = [t.id for t in THEMES]
    assert len(ids) == len(set(ids))


def test_theme_ids_are_kebab_case() -> None:
    for t in THEMES:
        assert _KEBAB_RE.match(t.id), f"{t.id!r} is not kebab-case"


def test_theme_schemes_are_valid() -> None:
    for t in THEMES:
        assert t.scheme in {"system", "light", "dark"}, f"{t.id!r} has bad scheme {t.scheme!r}"


def test_system_theme_has_no_accent() -> None:
    system = get_theme("system")
    assert system.accent is None


def test_non_system_themes_have_valid_hex_accents() -> None:
    for t in THEMES:
        if t.id == "system":
            continue
        assert t.accent is not None, f"{t.id!r} should define an accent"
        assert _HEX_RE.match(t.accent), f"{t.id!r} has a malformed accent {t.accent!r}"


def test_theme_names_are_distinct_and_non_generic() -> None:
    names = [t.name for t in THEMES]
    assert len(names) == len(set(names))
    # None of these should be named after a real product.
    banned = {"spotify", "apple music", "tidal", "youtube music", "amazon music", "deezer"}
    for name in names:
        assert name.lower() not in banned


# -- theme_ids / theme_index --------------------------------------------------


def test_theme_ids_matches_registry_order() -> None:
    assert theme_ids() == [(t.id, t.name) for t in THEMES]


def test_theme_index_finds_known_id() -> None:
    idx = theme_index("system")
    assert THEMES[idx].id == "system"


def test_theme_index_falls_back_to_zero_for_unknown_id() -> None:
    assert theme_index("not-a-real-theme") == 0


# -- get_theme -----------------------------------------------------------------


def test_get_theme_returns_matching_theme() -> None:
    for t in THEMES:
        assert get_theme(t.id) is t


def test_get_theme_falls_back_to_system_for_unknown_id() -> None:
    assert get_theme("not-a-real-theme").id == "system"


# -- apply_theme (headless: no Gdk.Display) ------------------------------------


def test_apply_theme_unknown_id_does_not_raise() -> None:
    apply_theme("not-a-real-theme")


def test_apply_theme_known_ids_do_not_raise() -> None:
    for t in THEMES:
        apply_theme(t.id)


# -- Settings round-trip --------------------------------------------------------


def test_settings_theme_round_trips_through_save_and_load(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(config_module, "settings_path", lambda: tmp_path / "settings.json")

    original = config_module.Settings(theme="midnight")
    original.save()

    loaded = config_module.Settings.load()

    assert loaded.theme == "midnight"


def test_settings_theme_defaults_to_system(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(config_module, "settings_path", lambda: tmp_path / "settings.json")

    assert config_module.Settings.load().theme == "system"
