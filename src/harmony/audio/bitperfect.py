"""Bit-perfect output: what "bit-perfect" means for Harmony, and whether it holds.

Harmony's local player (GStreamer -> PipeWire) is bit-perfect when the samples
that leave the decoder reach the output device untouched. That takes five things,
all of which this module checks and none of which it assumes:

* **No software volume** -- the app's volume is fixed at 100% (and PipeWire isn't
  scaling the stream or the output device either).
* **No resampling** -- the PipeWire graph runs at the track's own rate, which
  needs that rate in the daemon's ``clock.allowed-rates``.
* **No mixing** -- nothing else is playing into the same output, and the
  channels aren't up/down-mixed.
* **No bit-depth loss** -- the output format carries every bit the decoder made.
* **No DSP** -- no dither, ReplayGain or normalisation (the local pipeline has
  none; see ``LocalPlayer``).

:func:`evaluate` folds what the player and ``pw-dump`` report into one
:class:`BitPerfectStatus`: green (``verified``) only when every check passes;
amber (``warning``) with the *first* thing that breaks it otherwise; grey when
off or not yet measurable. Pure and gi-free, so the logic is unit-testable.

A lossy source (AAC from YouTube) can still be passed on untouched -- it is
decoded without further processing -- but "bit-perfect" is a claim about the
*signal path*, so a lossy source is flagged amber ("not lossless, untouched")
after the path itself checks out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from harmony.audio.pipewire import StreamReport

OFF = "off"
PENDING = "pending"
VERIFIED = "verified"
WARNING = "warning"

#: ``default.clock.allowed-rates`` enabling the usual rates -- shown to the user
#: because a sandboxed app can't (and shouldn't) edit the host's PipeWire config.
ALLOWED_RATES_SNIPPET = (
    "# ~/.config/pipewire/pipewire.conf.d/10-bit-perfect.conf\n"
    "context.properties = {\n"
    "    default.clock.allowed-rates = [ 44100 48000 88200 96000 176400 192000 ]\n"
    "}\n"
)
ALLOWED_RATES_COMMAND = (
    "mkdir -p ~/.config/pipewire/pipewire.conf.d && cat > "
    "~/.config/pipewire/pipewire.conf.d/10-bit-perfect.conf <<'EOF'\n"
    "context.properties = {\n"
    "    default.clock.allowed-rates = [ 44100 48000 88200 96000 176400 192000 ]\n"
    "}\nEOF\n"
    "systemctl --user restart pipewire pipewire-pulse wireplumber"
)


@dataclass(frozen=True)
class AudioFormat:
    """A raw-audio format: sample ``rate`` (Hz), GStreamer/PipeWire sample
    ``format`` name (``S24_32LE``, ``F32LE``...), and ``channels``."""

    rate: int | None = None
    format: str | None = None
    channels: int | None = None

    @property
    def bits(self) -> int | None:
        return format_bits(self.format)

    @property
    def is_float(self) -> bool:
        return bool(self.format) and self.format.startswith("F")

    def same_samples(self, other: AudioFormat) -> bool:
        """The same sample *values*: rate, channels, significant bits and int/float
        kind agree. A repack inside a wider container (``S24_32LE`` <-> ``S24LE``)
        keeps every bit, so it is not a conversion; ``F32`` <-> ``S24`` or
        ``S32`` -> ``S24`` is."""
        return (self.rate == other.rate and self.channels == other.channels
                and self.bits == other.bits and self.is_float == other.is_float)

    def describe(self) -> str:
        parts = []
        if self.rate:
            parts.append(f"{khz(self.rate)} kHz")
        if self.bits:
            parts.append(f"{self.bits}-bit")
        if self.channels:
            parts.append(f"{self.channels}ch")
        return " · ".join(parts)


@dataclass(frozen=True)
class BitPerfectStatus:
    """The indicator: ``level`` picks the colour, ``label`` is the pill text and
    ``detail`` the tooltip (what is true, and what to do about it)."""

    level: str
    label: str
    detail: str

    @property
    def verified(self) -> bool:
        return self.level == VERIFIED


def khz(rate: float) -> str:
    return f"{rate / 1000:g}"


def format_bits(fmt: str | None) -> int | None:
    """Significant sample bits of a raw format name.

    ``S16LE`` 16, ``S24LE``/``S24_32LE`` 24, ``S32LE`` 32. Floats (``F32LE``,
    ``F32P``) carry a 24-bit mantissa, which holds any <=24-bit PCM exactly."""
    if not fmt:
        return None
    match = re.search(r"(\d+)", fmt)
    if not match:
        return None
    bits = int(match.group(1))
    if fmt.startswith("F"):
        return 24 if bits == 32 else 53 if bits == 64 else None
    return bits


def _off() -> BitPerfectStatus:
    return BitPerfectStatus(
        OFF, "Bit-perfect",
        "Off. Turn it on to play without software volume, resampling or mixing: the "
        "output opens at each track's own sample rate and the volume is left to your "
        "DAC or amp.")


def _warn(label: str, detail: str, fix: str = "") -> BitPerfectStatus:
    return BitPerfectStatus(WARNING, label, f"{detail}\n\n{fix}" if fix else detail)


def _rate_fix(rate: int) -> str:
    return (f"To let the output run at {khz(rate)} kHz, add it to PipeWire's "
            "default.clock.allowed-rates (Preferences > Audio shows the exact command; "
            "Harmony never edits your PipeWire configuration).")


def evaluate(
    *,
    enabled: bool,
    decoded: AudioFormat | None,
    at_sink: AudioFormat | None,
    report: StreamReport | None,
    probed: bool,
    volume: int | None,
    muted: bool = False,
    quality: dict[str, Any] | None = None,
) -> BitPerfectStatus:
    """The honest status of the local output.

    ``decoded`` is what the decoder produced; ``at_sink`` what the app handed to
    ``pipewiresink`` (they differ only if something converted in between);
    ``report`` is what PipeWire's graph says (None when it could not be read:
    ``probed`` tells "asked and failed" from "not asked yet"); ``volume`` the
    app's software volume in percent; ``quality`` the stream's quality dict.
    """
    if not enabled:
        return _off()
    if decoded is None or not decoded.rate:
        return BitPerfectStatus(PENDING, "Bit-perfect", "On. Waiting for audio to start, then the output is checked.")

    # 1. Software volume in the app.
    if muted or (volume is not None and volume != 100):
        level = "muted" if muted else f"{volume}%"
        return _warn("Volume below 100%", f"The app's volume is {level}: that scales every sample.",
                     "Bit-perfect mode keeps it at 100%; set the level on your DAC or amp.")

    # 2. A conversion inside the app (decoder format != what pipewiresink gets).
    if at_sink is not None and not decoded.same_samples(at_sink):
        return _warn("Converted in app",
                     "The audio is converted inside Harmony before it reaches PipeWire "
                     f"({decoded.describe()} → {at_sink.describe()}).")

    # 3. What PipeWire actually does.
    if not probed:
        return BitPerfectStatus(PENDING, "Bit-perfect", "On. Checking what PipeWire does with the stream…")
    if report is None:
        return _warn("Can't verify",
                     "Harmony couldn't read PipeWire's state (pw-dump isn't available or PipeWire "
                     "isn't running), so it can't confirm the output is unmodified.")
    if not report.linked or not report.stream_rate or not report.sink_rate:
        return BitPerfectStatus(PENDING, "Bit-perfect", "On. Waiting for PipeWire to connect the stream to the output…")

    out = report.sink_description or report.sink_name or "the output"
    rate = report.stream_rate

    if report.sink_kind == "bluetooth":
        return _warn("Re-encoded by Bluetooth",
                     f"{out} is a Bluetooth output: the link re-encodes the audio (SBC/AAC/aptX/LDAC), so it "
                     "can never be bit-perfect.")
    if report.sink_kind != "alsa":  # virtual, or not classified: never verified green
        return _warn(f"Can't verify past {out}",
                     f"{out} is a virtual output (an effects chain such as EasyEffects, a filter, loopback "
                     "or combined sink), so whatever it does downstream -- EQ, resampling, mixing -- can't be "
                     "seen from here.",
                     "Play to the hardware output directly for a verified bit-perfect signal.")

    if decoded.rate != rate:
        return _warn(f"Resampled {khz(decoded.rate)}→{khz(rate)} kHz",
                     f"The stream was opened at {khz(rate)} kHz although the track is {khz(decoded.rate)} kHz.")

    if report.sink_rate != rate:
        why = _why_not_native(report, rate)
        return _warn(f"Resampled {khz(rate)}→{khz(report.sink_rate)} kHz", why[0], why[1])

    channels = decoded.channels or report.stream_channels
    if (channels and report.sink_channels and report.sink_channels != channels
            and not report.dont_remix):
        return _warn(f"Channels mixed {channels}→{report.sink_channels}",
                     f"{out} has {report.sink_channels} channels and the {channels}-channel stream is being mixed to fit.")

    track_bits = decoded.bits
    sink_bits = format_bits(report.sink_format)
    if track_bits and sink_bits and sink_bits < track_bits:
        return _warn(f"Bit depth {track_bits}→{sink_bits}",
                     f"{out} runs at {sink_bits} bits but the track has {track_bits}; the extra bits are dropped.")

    if report.gains:
        what, factor = report.gains[0]
        return _warn("Volume below 100%",
                     f"PipeWire is applying a software {what} ({round(factor * 100)}%), scaling the samples.",
                     "Set it back to 100% (the output device's hardware volume is fine).")

    if report.others:
        names = ", ".join(report.others)
        return _warn("Mixed with other audio",
                     f"{names} is also playing to {out}, so the two are mixed together.",
                     "Pause it for a bit-perfect signal.")

    # The path is clean. A lossy source can't be *made* lossless, but it is untouched.
    path = (f"{decoded.describe()} reaches {out} unchanged: PipeWire runs at {khz(rate)} kHz, "
            "the software volume is 100%, and nothing is resampled, mixed or dithered.")
    if quality and quality.get("lossless") is False:
        codec = quality.get("codec") or "lossy"
        return _warn(f"Lossy source ({codec})",
                     f"Lossy source ({codec}) — decoded unmodified. {path} A lossy stream can't "
                     "be bit-perfect to the original recording.")
    return BitPerfectStatus(VERIFIED, "Bit-perfect", f"Verified: {path}")


def _why_not_native(report: StreamReport, rate: int) -> tuple[str, str]:
    """(detail, fix) for "the output runs at another rate than the track"."""
    sink_rate = report.sink_rate or 0
    if report.force_rate and report.force_rate != rate:
        return (f"PipeWire is forced to {khz(report.force_rate)} kHz (clock.force-rate), so the "
                f"{khz(rate)} kHz track is resampled.", "Remove the forced rate to follow the track.")
    if report.allowed_rates and rate not in report.allowed_rates:
        return (f"The output doesn't allow {khz(rate)} kHz (PipeWire is limited to "
                f"{', '.join(khz(r) for r in report.allowed_rates)} kHz), so it is resampled to "
                f"{khz(sink_rate)} kHz.", _rate_fix(rate))
    if report.others:
        names = ", ".join(report.others)
        return (f"{names} is playing at {khz(sink_rate)} kHz and holds the output's rate, so this "
                f"{khz(rate)} kHz track is resampled.", "Stop that audio and replay the track.")
    return (f"The output is running at {khz(sink_rate)} kHz instead of {khz(rate)} kHz, so the track is "
            "resampled.", _rate_fix(rate))
