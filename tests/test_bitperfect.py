"""Bit-perfect output: the PipeWire graph reader and the status decision.

``parse_graph`` reduces a ``pw-dump`` document to a StreamReport and
``evaluate`` turns (decoded caps, caps at the sink, report, volume, quality) into
the indicator state. Green must mean *verified*: every case below that breaks
one property must come out amber with that property named, and ``scripts/
e2e_bit_perfect.py`` checks the same against a real PipeWire daemon.
"""

from __future__ import annotations

import pytest

from harmony.audio import bitperfect as bp
from harmony.audio.bitperfect import OFF, PENDING, VERIFIED, WARNING, AudioFormat, evaluate
from harmony.audio.pipewire import STREAM_NODE_NAME, StreamReport, parse_graph

# -- a minimal pw-dump document ---------------------------------------------------


def _props(volume=1.0, soft=(1.0, 1.0), channel=(1.0, 1.0), soft_mute=False, mute=False) -> list[dict]:
    return [{"volume": volume, "mute": mute, "channelVolumes": list(channel),
             "softVolumes": list(soft), "softMute": soft_mute}]


# Real node props, as ``pw-dump`` prints them, for each kind of output.
ALSA = {"device.api": "alsa", "api.alsa.pcm.card": 1, "api.alsa.pcm.stream": "playback",
        "factory.name": "api.alsa.pcm.sink", "node.description": "USB DAC"}
BLUEZ = {"device.api": "bluez5", "api.bluez5.address": "00:11:22:33:44:55", "api.bluez5.profile": "a2dp-sink",
         "factory.name": "api.bluez5.a2dp.sink", "node.description": "Headphones"}
EASYEFFECTS = {"node.virtual": True, "factory.name": "support.null-audio-sink",
               "node.description": "Easy Effects Sink", "node.nick": "easyeffects_sink"}
LOOPBACK = {"factory.name": "support.null-audio-sink", "node.description": "Loopback sink",
            "node.group": "loopback-123", "node.link-group": "loopback-123"}
NULL_SINK = {"factory.name": "support.null-audio-sink", "node.description": "BP test sink"}


def _node(nid, name, cls, state="running", fmt=None, props=None, extra=None) -> dict:
    node_props = {"node.name": name, "media.class": cls, **(extra or {})}
    params = {"Props": props if props is not None else _props()}
    if fmt:
        params["Format"] = [{"mediaType": "audio", "mediaSubtype": "raw", **fmt}]
    return {"id": nid, "type": "PipeWire:Interface:Node",
            "info": {"state": state, "props": node_props, "params": params}}


def _link(a, b) -> dict:
    return {"type": "PipeWire:Interface:Link", "info": {"output-node-id": a, "input-node-id": b}}


def _settings(allowed="[ 44100, 48000, 88200, 96000, 176400, 192000 ]", force=0) -> dict:
    return {"type": "PipeWire:Interface:Metadata", "props": {"metadata.name": "settings"},
            "metadata": [{"key": "clock.allowed-rates", "value": allowed},
                         {"key": "clock.force-rate", "value": force}]}


def graph(*, stream_rate=96000, stream_fmt="S24_32LE", sink_rate=96000, sink_fmt="S32LE",
          allowed="[ 44100, 48000, 88200, 96000, 176400, 192000 ]", stream_props=None,
          sink_props=None, extra_streams=(), dont_remix=True, sink_channels=2, sink_extra=ALSA,
          sink_name="dac") -> list[dict]:
    dump = [
        _settings(allowed),
        _node(40, sink_name, "Audio/Sink", fmt={"format": sink_fmt, "rate": sink_rate, "channels": sink_channels},
              props=sink_props, extra=sink_extra),
        _node(44, STREAM_NODE_NAME, "Stream/Output/Audio",
              fmt={"format": stream_fmt, "rate": stream_rate, "channels": 2}, props=stream_props,
              extra={"node.dont-remix": "TRUE"} if dont_remix else {}),
        _link(44, 40), _link(44, 40),
    ]
    for i, name in enumerate(extra_streams):
        dump += [_node(50 + i, "x", "Stream/Output/Audio", fmt={"format": "S16LE", "rate": 48000, "channels": 2},
                       extra={"application.name": name}), _link(50 + i, 40)]
    return dump


# -- parse_graph --------------------------------------------------------------------


def test_parse_follows_stream_to_its_sink() -> None:
    r = parse_graph(graph())
    assert r.linked
    assert (r.stream_rate, r.stream_format, r.stream_channels) == (96000, "S24_32LE", 2)
    assert (r.sink_rate, r.sink_name, r.sink_description) == (96000, "dac", "USB DAC")
    assert r.allowed_rates == (44100, 48000, 88200, 96000, 176400, 192000)
    assert r.dont_remix and not r.others and not r.gains


def test_parse_reports_other_streams_mixed_into_the_same_sink() -> None:
    r = parse_graph(graph(extra_streams=("Firefox",)))
    assert r.others == ("Firefox",)


def test_parse_stream_not_in_the_graph_or_unlinked_is_not_linked() -> None:
    assert not parse_graph([]).linked
    assert not parse_graph("garbage").linked
    unlinked = [o for o in graph() if o.get("type") != "PipeWire:Interface:Link"]
    assert not parse_graph(unlinked).linked


def test_parse_gains_stream_volume_counts_but_a_hardware_mixer_does_not() -> None:
    # a sink's hardware volume lives in channelVolumes: not processing
    hw = parse_graph(graph(sink_props=_props(channel=(0.3, 0.3))))
    assert hw.gains == ()
    # ...but the sink's software volume is, as is a stream's own volume
    soft = parse_graph(graph(sink_props=_props(soft=(0.5, 0.5))))
    assert soft.gains == (("output device software volume", 0.5),)
    stream = parse_graph(graph(stream_props=_props(channel=(0.8, 0.8))))
    assert stream.gains == (("stream volume", 0.8),)
    muted = parse_graph(graph(sink_props=_props(soft_mute=True)))
    assert muted.gains and muted.gains[0][1] == 0.0


@pytest.mark.parametrize("value", ["[ 44100, 48000 ]", "[44100 48000]", [44100, 48000]])
def test_parse_allowed_rates_formats(value) -> None:
    assert parse_graph(graph(allowed=value)).allowed_rates == (44100, 48000)


def test_parse_follows_through_a_non_sink_node_to_the_sink() -> None:
    dump = [o for o in graph() if o.get("type") != "PipeWire:Interface:Link"]
    dump += [_node(60, "router", "Audio/Duplex"), _link(44, 60), _link(60, 40)]
    assert parse_graph(dump).sink_name == "dac"


def test_parse_a_recorder_linked_first_does_not_hide_the_device() -> None:
    """OBS capturing this app's audio links our stream to *its* stream node first."""
    dump = graph()
    dump.insert(1, _node(70, "OBS", "Stream/Input/Audio"))
    dump.insert(2, _link(44, 70))
    r = parse_graph(dump)
    assert r.linked and r.sink_name == "dac" and r.sink_kind == "alsa"


def test_parse_prefers_a_real_device_over_a_virtual_one() -> None:
    dump = graph()
    dump.insert(1, _node(71, "easyeffects_sink", "Audio/Sink", extra=EASYEFFECTS))
    dump.insert(2, _link(44, 71))
    assert parse_graph(dump).sink_kind == "alsa"


@pytest.mark.parametrize(("extra", "kind"), [
    (ALSA, "alsa"), (BLUEZ, "bluetooth"), (EASYEFFECTS, "virtual"), (LOOPBACK, "virtual"),
    (NULL_SINK, "virtual"), ({}, "virtual"),
])
def test_sink_kind_from_real_node_props(extra, kind) -> None:
    assert parse_graph(graph(sink_extra=extra)).sink_kind == kind


def test_bluez_sink_named_by_node_name_alone_is_bluetooth() -> None:
    assert parse_graph(graph(sink_extra={}, sink_name="bluez_output.00_11_22.1")).sink_kind == "bluetooth"


def test_the_test_sink_allowance_is_explicit_and_off_by_default() -> None:
    assert parse_graph(graph(sink_extra=NULL_SINK, sink_name="bp-test-sink")).sink_kind == "virtual"
    assert parse_graph(graph(sink_extra=NULL_SINK, sink_name="bp-test-sink"),
                       test_sinks=("bp-test-sink",)).sink_kind == "alsa"


def test_inspect_stream_reads_the_test_allowance_from_the_environment(monkeypatch) -> None:
    import json
    import subprocess

    from harmony.audio import pipewire

    dump = json.dumps(graph(sink_extra=NULL_SINK, sink_name="bp-test-sink"))
    monkeypatch.setattr(pipewire.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=dump, stderr=""))
    monkeypatch.delenv(pipewire.TEST_SINKS_ENV, raising=False)
    assert pipewire.inspect_stream().sink_kind == "virtual"
    monkeypatch.setenv(pipewire.TEST_SINKS_ENV, "bp-test-sink")
    assert pipewire.inspect_stream().sink_kind == "alsa"


# -- evaluate -------------------------------------------------------------------------

HIRES = AudioFormat(rate=96000, format="S24_32LE", channels=2)
CD = AudioFormat(rate=44100, format="S16LE", channels=2)
LOSSLESS = {"codec": "FLAC", "lossless": True, "tier": "hires"}


def ev(report=None, decoded=HIRES, at_sink=None, enabled=True, probed=True, volume=100,
       muted=False, quality=LOSSLESS) -> bp.BitPerfectStatus:
    return evaluate(enabled=enabled, decoded=decoded, at_sink=at_sink or decoded, report=report,
                    probed=probed, volume=volume, muted=muted, quality=quality)


def test_off_is_grey_whatever_else_is_true() -> None:
    assert ev(parse_graph(graph(sink_rate=48000)), enabled=False).level == OFF


def test_verified_only_when_everything_checks_out() -> None:
    s = ev(parse_graph(graph()))
    assert s.level == VERIFIED and s.label == "Bit-perfect"
    assert "96 kHz" in s.detail and "24-bit" in s.detail and "USB DAC" in s.detail


def test_cd_track_at_44k1_verified() -> None:
    assert ev(parse_graph(graph(stream_rate=44100, stream_fmt="S16LE", sink_rate=44100, sink_fmt="S16LE")),
              decoded=CD).level == VERIFIED


def test_waiting_states_are_not_green() -> None:
    assert ev(None, decoded=None).level == PENDING                       # nothing decoded yet
    assert ev(None, probed=False).level == PENDING                        # not asked PipeWire yet
    assert ev(parse_graph([])).level == PENDING                           # stream not linked yet


def test_bluetooth_is_never_green_whatever_the_rates() -> None:
    s = ev(parse_graph(graph(sink_extra=BLUEZ)))
    assert s.level == WARNING and s.label == "Re-encoded by Bluetooth"


@pytest.mark.parametrize("extra", [EASYEFFECTS, LOOPBACK, NULL_SINK, {}])
def test_virtual_outputs_cannot_verify_green(extra) -> None:
    s = ev(parse_graph(graph(sink_extra=extra, sink_name="easyeffects_sink")))
    assert s.level == WARNING and s.label.startswith("Can't verify past")
    assert "downstream" in s.detail


def test_cannot_read_pipewire_is_amber_never_green() -> None:
    s = ev(None, probed=True)
    assert s.level == WARNING and s.label == "Can't verify"


def test_resample_because_the_device_does_not_allow_the_rate() -> None:
    s = ev(parse_graph(graph(sink_rate=48000, allowed="[ 48000 ]", sink_fmt="S32LE")))
    assert s.level == WARNING
    assert s.label == "Resampled 96→48 kHz"
    assert "doesn't allow 96 kHz" in s.detail
    assert "allowed-rates" in s.detail          # how to fix it


def test_resample_because_another_app_holds_the_rate() -> None:
    s = ev(parse_graph(graph(sink_rate=48000, extra_streams=("Firefox",))))
    assert s.level == WARNING and s.label == "Resampled 96→48 kHz"
    assert "Firefox" in s.detail and "holds the output's rate" in s.detail


def test_resample_because_the_rate_is_forced() -> None:
    dump = graph(sink_rate=48000)
    dump[0]["metadata"][1]["value"] = 48000
    s = ev(parse_graph(dump))
    assert "forced to 48 kHz" in s.detail


def test_app_volume_below_100_wins_over_a_clean_path() -> None:
    s = ev(parse_graph(graph()), volume=60)
    assert s.level == WARNING and s.label == "Volume below 100%" and "60%" in s.detail
    assert ev(parse_graph(graph()), muted=True).label == "Volume below 100%"


def test_pipewire_software_volume_is_flagged() -> None:
    s = ev(parse_graph(graph(stream_props=_props(channel=(0.5, 0.5)))))
    assert s.level == WARNING and s.label == "Volume below 100%" and "stream volume" in s.detail


def test_conversion_inside_the_app_is_flagged() -> None:
    f32 = AudioFormat(rate=96000, format="F32LE", channels=2)
    s = ev(parse_graph(graph()), at_sink=f32)
    assert s.level == WARNING and s.label == "Converted in app"


def test_repacking_24_bit_in_another_container_is_not_a_conversion() -> None:
    """flacdec makes S24_32LE, PipeWire's stream takes S24LE: same 24 bits, repacked."""
    packed = AudioFormat(rate=96000, format="S24LE", channels=2)
    assert ev(parse_graph(graph()), at_sink=packed).level == VERIFIED
    # ...but dropping to 16 bits, or going through float, is
    assert ev(parse_graph(graph()), at_sink=AudioFormat(96000, "S16LE", 2)).label.startswith("Converted")
    assert ev(parse_graph(graph()), at_sink=AudioFormat(96000, "S32LE", 2)).label.startswith("Converted")


def test_stream_opened_at_another_rate_than_the_track() -> None:
    s = ev(parse_graph(graph(stream_rate=48000, sink_rate=48000)))
    assert s.label == "Resampled 96→48 kHz"


def test_channel_mix_is_flagged_unless_remix_is_off() -> None:
    mixed = ev(parse_graph(graph(sink_channels=6, dont_remix=False)))
    assert mixed.level == WARNING and "mixed" in mixed.label.lower()
    assert ev(parse_graph(graph(sink_channels=6, dont_remix=True))).level == VERIFIED


def test_bit_depth_loss_is_flagged_but_float_graph_is_fine() -> None:
    s = ev(parse_graph(graph(sink_fmt="S16LE")))
    assert s.level == WARNING and s.label == "Bit depth 24→16"
    assert ev(parse_graph(graph(sink_fmt="F32P"))).level == VERIFIED  # 24-bit mantissa holds 24-bit PCM


def test_other_audio_mixed_in_is_flagged() -> None:
    s = ev(parse_graph(graph(extra_streams=("Firefox",))))
    assert s.level == WARNING and s.label == "Mixed with other audio" and "Firefox" in s.detail


def test_lossy_source_is_amber_but_says_untouched() -> None:
    aac = {"codec": "AAC", "lossless": False, "tier": "lossy"}
    s = ev(parse_graph(graph(stream_rate=44100, stream_fmt="F32LE", sink_rate=44100)), decoded=AudioFormat(44100, "F32LE", 2),
           quality=aac)
    assert s.level == WARNING and s.label == "Lossy source (AAC)"
    assert "decoded unmodified" in s.detail


def test_first_broken_thing_is_reported() -> None:
    # volume AND resampling are wrong: the volume is named first
    s = ev(parse_graph(graph(sink_rate=48000)), volume=50)
    assert s.label == "Volume below 100%"


def test_format_bits() -> None:
    assert [bp.format_bits(f) for f in ("S16LE", "S24LE", "S24_32LE", "S32LE", "F32LE", "F32P", None, "x")] == \
        [16, 24, 24, 32, 24, 24, None, None]


def test_allowed_rates_command_is_copy_paste_safe() -> None:
    assert "44100 48000 88200 96000 176400 192000" in bp.ALLOWED_RATES_COMMAND
    assert "systemctl --user restart pipewire" in bp.ALLOWED_RATES_COMMAND


def test_streamreport_default_is_unlinked() -> None:
    assert not StreamReport().linked


# -- inspect_stream: never raises ------------------------------------------------------


def test_inspect_stream_degrades_to_none_without_pw_dump(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    from harmony.audio import pipewire

    def boom(*_a, **_k):
        raise FileNotFoundError("pw-dump")

    monkeypatch.setattr(pipewire.subprocess, "run", boom)
    assert pipewire.inspect_stream() is None
    monkeypatch.setattr(pipewire.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="not json", stderr=""))
    assert pipewire.inspect_stream() is None


def test_inspect_stream_parses_pw_dump_output(monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    import subprocess

    from harmony.audio import pipewire

    monkeypatch.setattr(pipewire.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=json.dumps(graph()), stderr=""))
    report = pipewire.inspect_stream()
    assert report is not None and report.linked and report.sink_rate == 96000
