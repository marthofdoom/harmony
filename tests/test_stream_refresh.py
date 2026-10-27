"""The stream token must survive an expired provider/CDN URL (late seeks)."""

from __future__ import annotations

import pytest

from harmony.web.api import Engine


class _Src:
    def __init__(self, url: str) -> None:
        self.url = url
        self.headers: dict = {}
        self.mime_type = "audio/flac"
        self.label = "x"


def test_refresh_stream_reresolves_provider_url(monkeypatch: pytest.MonkeyPatch) -> None:
    e = Engine()
    e._streams["t"] = {"url": "stale", "headers": {}, "mime": "audio/flac",
                       "at": 0.0, "service": "qobuz", "id": "42"}
    monkeypatch.setattr(e, "_resolve_source", lambda sv, tid: _Src(f"https://fresh/{sv}/{tid}"))
    meta = e.refresh_stream("t")
    assert meta is not None and meta["url"] == "https://fresh/qobuz/42"


def test_refresh_stream_unknown_token_is_none() -> None:
    assert Engine().refresh_stream("nope") is None


def test_a_self_contained_resolve_does_not_hold_the_engine_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """YouTube's yt-dlp extraction takes seconds and uses none of the provider's
    session state: it must not stall every other provider call (or the relay's
    mid-song re-resolve) behind the engine-wide lock. Other services still serialise."""
    e = Engine()
    seen: dict[str, bool] = {}

    class Prov:
        def __init__(self, free: bool) -> None:
            self.resolve_is_self_contained = free

        def resolve_stream(self, track_id: str, max_quality: bool = False) -> _Src:
            seen[track_id] = e._lock.locked()
            return _Src("https://cdn/" + track_id)

    provs = {"ytmusic": Prov(True), "qobuz": Prov(False)}
    monkeypatch.setattr(e, "_provider", lambda sv: provs[sv])
    e._resolve_source("ytmusic", "yt")
    e._resolve_source("qobuz", "qb")
    assert seen == {"yt": False, "qb": True}


def test_ytmusic_provider_declares_its_resolve_self_contained() -> None:
    from harmony.providers.ytmusic import YTMusicProvider

    assert YTMusicProvider.resolve_is_self_contained is True
