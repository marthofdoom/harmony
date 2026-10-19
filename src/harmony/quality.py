"""Audio quality, described the same way for every source.

A library file, a Qobuz stream and a YouTube stream all report their format as
one small dict, so every client can show "FLAC · 24-bit / 96 kHz · Hi-Res" or
"AAC · 256 kbps" without knowing where the audio came from:

    {"codec": "FLAC", "bit_depth": 24, "sample_rate": 96000, "bitrate": 2304,
     "channels": 2, "lossless": True, "tier": "hires", "label": "..."}

``sample_rate`` is in Hz, ``bitrate`` in kbps; unknown fields are ``None``.
``tier`` is ``"hires"`` (lossless above CD: >16-bit or >48 kHz), ``"lossless"``
or ``"lossy"``. Pure functions, gi-free.
"""

from __future__ import annotations

from typing import Any

_LOSSLESS = {"FLAC", "ALAC", "WAV", "AIFF", "WAVPACK", "APE", "PCM", "DSD"}

_MIME_CODECS = {
    "audio/flac": "FLAC", "audio/x-flac": "FLAC", "audio/mpeg": "MP3",
    "audio/aac": "AAC", "audio/ogg": "Vorbis", "audio/opus": "Opus", "audio/webm": "Opus",
    "audio/wav": "WAV", "audio/x-wav": "WAV", "audio/aiff": "AIFF", "audio/x-aiff": "AIFF",
    "audio/x-wavpack": "WavPack", "audio/x-ape": "APE",
}


def codec_for_mime(mime: str | None) -> str | None:
    if not mime:
        return None
    return _MIME_CODECS.get(mime.split(";")[0].strip().lower())


def _khz(hz: int) -> str:
    return f"{hz / 1000:g} kHz"


def describe(codec: str | None, *, bit_depth: int | None = None,
             sample_rate: int | float | None = None, bitrate: int | float | None = None,
             channels: int | None = None, lossless: bool | None = None) -> dict[str, Any] | None:
    """The quality dict for a stream, or None when nothing is known."""
    codec = (codec or "").strip() or None
    rate = None
    if sample_rate:
        rate_f = float(sample_rate)
        # Given in kHz (Qobuz reports 44.1 / 96 / 192) or Hz.
        rate = int(round(rate_f * 1000)) if rate_f < 1000 else int(round(rate_f))
    kbps = int(round(bitrate)) if bitrate else None
    depth = int(bit_depth) if bit_depth else None
    if codec is None and not (depth or rate or kbps):
        return None
    if lossless is None:
        lossless = (codec or "").upper() in _LOSSLESS
    if lossless:
        tier = "hires" if ((depth or 0) > 16 or (rate or 0) > 48000) else "lossless"
    else:
        tier = "lossy"
    parts = [codec or "Audio"]
    if lossless and (depth or rate):
        fmt = " / ".join(p for p in (f"{depth}-bit" if depth else "", _khz(rate) if rate else "") if p)
        parts.append(fmt)
    elif kbps:
        parts.append(f"{kbps} kbps")
    elif rate:
        parts.append(_khz(rate))
    if tier == "hires":
        parts.append("Hi-Res")
    return {"codec": codec, "bit_depth": depth, "sample_rate": rate, "bitrate": kbps,
            "channels": channels, "lossless": bool(lossless), "tier": tier,
            "label": " · ".join(parts)}


def best(qualities: list[dict[str, Any] | None]) -> dict[str, Any] | None:
    """The highest quality among an album's tracks (for an album badge)."""
    rank = {"hires": 2, "lossless": 1, "lossy": 0}
    known = [q for q in qualities if q]
    if not known:
        return None
    return max(known, key=lambda q: (rank.get(q.get("tier"), 0), q.get("bit_depth") or 0,
                                     q.get("sample_rate") or 0, q.get("bitrate") or 0))


__all__ = ["best", "codec_for_mime", "describe"]
