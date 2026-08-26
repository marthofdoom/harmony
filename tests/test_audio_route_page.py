"""Light offline test for the Route Audio page (GTK construction covered by the
ui-smoke job; here we exercise the transport-dependent sender command)."""

from __future__ import annotations

import pytest

pytest.importorskip("gi")

from harmony.ui import audio_route_page as arp  # noqa: E402


def test_local_ip_returns_string():
    assert isinstance(arp._local_ip(), str)


def test_roc_sender_command_shape():
    page = arp.AudioRoutePage.__new__(arp.AudioRoutePage)
    page._roc = True
    cmd = page._sender_command("192.168.1.5")
    assert "roc-send" in cmd
    assert "rtp+rs8m://192.168.1.5:10001" in cmd
    assert "rs8m://192.168.1.5:10002" in cmd  # FEC repair


def test_rtp_sender_command_shape():
    page = arp.AudioRoutePage.__new__(arp.AudioRoutePage)
    page._roc = False
    assert "module-rtp-send" in page._sender_command()
