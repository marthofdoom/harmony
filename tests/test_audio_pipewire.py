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


# -- ROC receiver up/down (pactl mocked) -------------------------------------

from harmony.audio import pipewire as pw  # noqa: E402
from harmony.errors import ProviderError  # noqa: E402


class _FakePactl:
    """Records pactl calls; load-module returns incrementing module ids."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self._next_id = 100
        self._fail_on = fail_on  # substring of a load-module that should fail

    def run(self, argv, **kw):
        args = argv[1:]  # drop "pactl"
        self.calls.append(args)
        if args[0] == "load-module":
            if self._fail_on and self._fail_on in " ".join(args):
                raise subprocess.CalledProcessError(1, argv, stderr="boom")
            self._next_id += 1
            return subprocess.CompletedProcess(argv, 0, stdout=str(self._next_id), stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def test_roc_receiver_up_loads_source_then_loopback(monkeypatch):
    fake = _FakePactl()
    monkeypatch.setattr(pw.subprocess, "run", fake.run)

    rx = pw.roc_receiver_up("alsa_output.usb-DAC", source_port=10001, latency_ms=25)

    assert rx.source_module == 101 and rx.loopback_module == 102
    assert fake.calls[0][:2] == ["load-module", "module-roc-source"]
    assert "sess_latency_msec=25" in fake.calls[0]
    assert "source_name=harmony-roc" in fake.calls[0]
    assert fake.calls[1][:2] == ["load-module", "module-loopback"]
    assert "source=harmony-roc" in fake.calls[1]
    assert "sink=alsa_output.usb-DAC" in fake.calls[1]


def test_roc_receiver_up_unloads_source_if_loopback_fails(monkeypatch):
    fake = _FakePactl(fail_on="module-loopback")
    monkeypatch.setattr(pw.subprocess, "run", fake.run)

    try:
        pw.roc_receiver_up("dac")
        raise AssertionError("expected ProviderError")
    except ProviderError:
        pass
    # source (id 101) must be unloaded so it isn't leaked
    assert ["unload-module", "101"] in fake.calls


def test_roc_receiver_down_unloads_loopback_then_source(monkeypatch):
    fake = _FakePactl()
    monkeypatch.setattr(pw.subprocess, "run", fake.run)
    pw.roc_receiver_down(pw.RocReceiver(source_module=5, loopback_module=9))
    assert fake.calls == [["unload-module", "9"], ["unload-module", "5"]]


def test_roc_up_raises_without_pactl(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError
    monkeypatch.setattr(pw.subprocess, "run", boom)
    try:
        pw.roc_receiver_up("dac")
        raise AssertionError("expected ProviderError")
    except ProviderError:
        pass
