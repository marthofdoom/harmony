"""Offline unit tests for the pure helpers in the in-app local player.

The module imports without GStreamer (``_ensure_gst`` is lazy), so these
format-parsing helpers are testable in the CI smoke environment that has no
GStreamer typelib.
"""

from __future__ import annotations

import pytest

from harmony.ui.local_player import _bits_from_format


@pytest.mark.parametrize(
    ("fmt", "expected"),
    [
        ("S16LE", 16),
        ("S24LE", 24),
        ("S24_32LE", 24),  # 24 significant bits in a 32-bit container
        ("S32LE", 32),
        ("F32LE", 32),
        ("U8", 8),
        ("", None),
        ("garbage", None),
    ],
)
def test_bits_from_format(fmt: str, expected: int | None) -> None:
    assert _bits_from_format(fmt) == expected
