"""Offline tests for harmony.audio.pipewire (pactl mocked; no real audio stack)."""

from __future__ import annotations

import json
import subprocess

from harmony.audio import pipewire

_SINKS = json.dumps([
    {"index": 0, "name": "alsa_output.usb-DAC.analog-stereo", "description": "USB DAC"},
    {"index": 1, "name": "raop_sink.wiim", "description": "WiiM Mini (AirPlay)"},
    {"index": 2, "description": "no name — skipped"},
])


def test_list_sinks_parses_name_and_description(monkeypatch):
    monkeypatch.setattr(
        pipewire.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=_SINKS, stderr=""),
    )
    sinks = pipewire.list_sinks()
    assert [s.name for s in sinks] == ["alsa_output.usb-DAC.analog-stereo", "raop_sink.wiim"]
    assert sinks[1].description == "WiiM Mini (AirPlay)"  # nameless entry dropped


def test_list_falls_back_to_description_of_name(monkeypatch):
    payload = json.dumps([{"name": "src1"}])  # no description
    monkeypatch.setattr(
        pipewire.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=payload, stderr=""),
    )
    assert pipewire.list_sources()[0].description == "src1"


def test_missing_pactl_returns_empty(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("no pactl")
    monkeypatch.setattr(pipewire.subprocess, "run", boom)
    assert pipewire.list_sinks() == []


def test_bad_json_returns_empty(monkeypatch):
    monkeypatch.setattr(
        pipewire.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="not json", stderr=""),
    )
    assert pipewire.list_sinks() == []


# -- RTP receiver up/down (pactl mocked) -------------------------------------

from harmony.audio import pipewire as pw  # noqa: E402
from harmony.errors import ProviderError  # noqa: E402


class _FakePactl:
    def __init__(self, fail=False):
        self.calls = []
        self._fail = fail

    def run(self, argv, **kw):
        args = argv[1:]
        self.calls.append(args)
        if args[0] == "load-module":
            if self._fail:
                raise subprocess.CalledProcessError(1, argv, stderr="boom")
            return subprocess.CompletedProcess(argv, 0, stdout="536870916", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def test_rtp_receiver_up_loads_module_rtp_recv(monkeypatch):
    fake = _FakePactl()
    monkeypatch.setattr(pw.subprocess, "run", fake.run)
    rx = pw.rtp_receiver_up("alsa_output.usb-DAC", latency_ms=25)
    assert rx.module == 536870916
    assert fake.calls[0][:2] == ["load-module", "module-rtp-recv"]
    assert "sink=alsa_output.usb-DAC" in fake.calls[0]
    assert "latency_msec=25" in fake.calls[0]


def test_rtp_receiver_down_unloads(monkeypatch):
    fake = _FakePactl()
    monkeypatch.setattr(pw.subprocess, "run", fake.run)
    pw.rtp_receiver_down(pw.RtpReceiver(module=42))
    assert fake.calls == [["unload-module", "42"]]


def test_rtp_up_raises_on_pactl_failure(monkeypatch):
    monkeypatch.setattr(pw.subprocess, "run", _FakePactl(fail=True).run)
    try:
        pw.rtp_receiver_up("dac")
        raise AssertionError("expected ProviderError")
    except ProviderError:
        pass


def test_rtp_up_raises_without_pactl(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError
    monkeypatch.setattr(pw.subprocess, "run", boom)
    try:
        pw.rtp_receiver_up("dac")
        raise AssertionError("expected ProviderError")
    except ProviderError:
        pass
