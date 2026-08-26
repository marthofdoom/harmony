"""Light offline test for the Route Audio page (GTK construction is covered by
the ui-smoke job; here we exercise module-level constants)."""

from __future__ import annotations

import pytest

pytest.importorskip("gi")

from harmony.ui import audio_route_page as arp  # noqa: E402


def test_sender_command_uses_rtp_send():
    assert "module-rtp-send" in arp._SENDER_CMD
