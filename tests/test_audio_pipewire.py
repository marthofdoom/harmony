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


# -- ROC receiver up/down (roc-recv subprocess mocked) -----------------------


class _FakeProc:
    def __init__(self, alive=True):
        self._alive = alive
        self.terminated = False
        self.killed = False
        self.returncode = None if alive else 1
        self.stderr = None if alive else _FakeStderr()

    def poll(self):
        return None if self._alive else 1

    def terminate(self):
        self.terminated = True
        self._alive = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True


class _FakeStderr:
    def read(self):
        return b"bad sink"


def test_roc_available(monkeypatch):
    monkeypatch.setattr(pw.shutil, "which", lambda _n: "/app/bin/roc-recv")
    assert pw.roc_available() is True
    monkeypatch.setattr(pw.shutil, "which", lambda _n: None)
    assert pw.roc_available() is False


def test_roc_receiver_up_spawns_roc_recv(monkeypatch, tmp_path):
    captured = {}

    def fake_popen(argv, **kw):
        captured["argv"] = argv
        return _FakeProc(alive=True)

    monkeypatch.setattr(pw.shutil, "which", lambda _n: "/app/bin/roc-recv")
    monkeypatch.setattr(pw.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(pw.time, "sleep", lambda _s: None)
    monkeypatch.setattr(pw, "_roc_log_path", lambda: tmp_path / "roc-recv.log")
    rx = pw.roc_receiver_up("alsa_output.usb-DAC", target_latency_ms=80)
    argv = captured["argv"]
    assert argv[0] == "/app/bin/roc-recv"
    assert "-o" in argv and "pulse://alsa_output.usb-DAC" in argv
    assert "--target-latency=80ms" in argv
    assert "--resampler-profile=high" in argv                       # quality resampler
    assert "--no-play-timeout=2s" in argv                           # ride-through watchdog
    assert "--choppy-play-timeout=4s" in argv
    assert any(a.startswith("--latency-tolerance=") for a in argv)  # jitter headroom
    assert any(a.startswith("rtp+rs8m://0.0.0.0:") for a in argv)   # source endpoint
    assert any(a.startswith("rs8m://0.0.0.0:") for a in argv)       # FEC repair endpoint
    assert rx.source_port == 10001
    rx.log_file.close()


def test_roc_receiver_up_raises_without_binary(monkeypatch):
    monkeypatch.setattr(pw.shutil, "which", lambda _n: None)
    try:
        pw.roc_receiver_up("dac")
        raise AssertionError("expected ProviderError")
    except ProviderError:
        pass


def test_roc_receiver_up_raises_if_process_dies(monkeypatch, tmp_path):
    monkeypatch.setattr(pw.shutil, "which", lambda _n: "/app/bin/roc-recv")
    monkeypatch.setattr(pw.subprocess, "Popen", lambda *a, **k: _FakeProc(alive=False))
    monkeypatch.setattr(pw.time, "sleep", lambda _s: None)
    monkeypatch.setattr(pw, "_roc_log_path", lambda: tmp_path / "roc-recv.log")
    try:
        pw.roc_receiver_up("dac")
        raise AssertionError("expected ProviderError")
    except ProviderError as exc:
        assert "exited immediately" in str(exc)


def test_roc_receiver_down_terminates():
    import io
    proc = _FakeProc(alive=True)
    pw.roc_receiver_down(pw.RocReceiver(
        process=proc, log_file=io.BytesIO(), log_path="x",
        source_port=1, repair_port=2, control_port=3,
    ))
    assert proc.terminated
