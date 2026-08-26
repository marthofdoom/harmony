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
