"""Light offline test for the Route Audio page (constructing GTK needs a display,
covered by the ui-smoke job; here we just exercise the pure helper)."""

from __future__ import annotations

import pytest

pytest.importorskip("gi")

from harmony.ui import audio_route_page as arp  # noqa: E402


def test_local_ip_returns_a_string():
    ip = arp._local_ip()
    assert isinstance(ip, str) and ip


def test_ports_are_distinct():
    assert arp._SOURCE_PORT != arp._REPAIR_PORT
