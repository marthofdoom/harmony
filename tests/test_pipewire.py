"""Guards for the audio transport shell-outs (no real PipeWire needed)."""

from __future__ import annotations

import pytest

from harmony.audio import pipewire


def test_rtp_sender_uses_unicast_ip_port_and_be_format(monkeypatch: pytest.MonkeyPatch) -> None:
    # PipeWire's module-rtp-send defaults to multicast on :46000; the phone
    # receiver binds :5004 and decodes 16-bit big-endian PCM. Regression guard
    # for the bug where it shipped `destination=` (ignored) with the default
    # multicast port, so nothing reached the receiver.
    captured: dict = {}
    monkeypatch.setattr(pipewire, "_load_module",
                        lambda name, *args: captured.update(name=name, args=args) or 7)
    monkeypatch.setattr(pipewire, "default_sink", lambda: "dac")

    sender = pipewire.rtp_sender_up("192.168.1.7")

    assert sender.module == 7
    assert captured["name"] == "module-rtp-send"
    joined = " ".join(captured["args"])
    assert "source=dac.monitor" in joined
    assert "destination_ip=192.168.1.7" in joined  # NOT the ignored `destination=`
    assert "port=5004" in joined                    # NOT the multicast default 46000
    assert "format=s16be" in joined and "rate=44100" in joined and "channels=2" in joined
