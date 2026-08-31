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
