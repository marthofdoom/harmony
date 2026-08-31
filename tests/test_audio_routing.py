"""Unit tests for the inter-instance AudioRouter (no real audio or network).

The ROC/RTP transports and the peer HTTP calls are faked, so these exercise the
router's state machine and the two-halves ``route()`` orchestration.
"""

from __future__ import annotations

import pytest

from harmony.errors import ProviderError
from harmony.web.audio_routing import AudioRouter


class _FakeAudio:
    """Stand-in for the harmony.audio module."""

    def __init__(self) -> None:
        self.events: list[tuple] = []

    def roc_available(self) -> bool:
        return True

    def default_sink(self) -> str:
        return "default-dac"

    def list_sinks(self) -> list:
        from harmony.audio import AudioNode

        return [AudioNode(name="dac", description="USB DAC")]

    def roc_receiver_up(self, sink, *, target_latency_ms=150):
        self.events.append(("recv_up", sink, target_latency_ms))
        return ("recv", sink)

    def roc_receiver_down(self, receiver):
        self.events.append(("recv_down", receiver))

    def roc_sender_up(self, host):
        self.events.append(("send_up", host))
        return ("send", host)

    def roc_sender_down(self, sender):
        self.events.append(("send_down", sender))


@pytest.fixture
def fake_audio(monkeypatch: pytest.MonkeyPatch) -> _FakeAudio:
    fake = _FakeAudio()
    # The router does `from harmony import audio` then `audio.<fn>` — patch the
    # module's attributes so the fakes are used.
    import harmony.audio as audio_mod

    for name in ("roc_available", "default_sink", "list_sinks", "roc_receiver_up",
                 "roc_receiver_down", "roc_sender_up", "roc_sender_down"):
        monkeypatch.setattr(audio_mod, name, getattr(fake, name))
    return fake


def test_receive_then_stop(fake_audio: _FakeAudio) -> None:
    r = AudioRouter()
    out = r.receive(sink="dac", latency_ms=40)
    assert out["sink"] == "dac" and out["transport"] == "roc"
    assert r.status()["receiving"] is True
    r.stop()
    assert r.status()["receiving"] is False
    assert ("recv_up", "dac", 40) in fake_audio.events
    assert any(e[0] == "recv_down" for e in fake_audio.events)


def test_receive_defaults_to_default_sink(fake_audio: _FakeAudio) -> None:
    r = AudioRouter()
    assert r.receive(sink=None)["sink"] == "default-dac"


def test_send_sets_state(fake_audio: _FakeAudio) -> None:
    r = AudioRouter()
    r.send("192.168.1.5")
    st = r.status()
    assert st["sending"] is True and st["peer"] == "192.168.1.5"


def test_route_receive_starts_local_and_asks_peer_to_send(
    fake_audio: _FakeAudio, monkeypatch: pytest.MonkeyPatch
) -> None:
    posts: list[tuple] = []

    class _Resp:
        status_code = 200
        content = b"{}"
        ok = True

        def json(self):
            return {}

    def fake_post(url, json=None, headers=None, timeout=None):
        posts.append((url, json, headers))
        return _Resp()

    monkeypatch.setattr("requests.post", fake_post)
    r = AudioRouter()
    out = r.route("receive", "192.168.1.5", 8080, key="secret", sink="dac", latency_ms=40)
    assert out["direction"] == "receive"
    # local receiver started, and the peer was told to send to us with our key
    assert ("recv_up", "dac", 40) in fake_audio.events
    url, body, headers = posts[0]
    assert url.endswith("/api/audio/send") and "to_host" in body
    assert headers["X-Harmony-Key"] == "secret"


def test_route_send_asks_peer_to_receive_then_sends(
    fake_audio: _FakeAudio, monkeypatch: pytest.MonkeyPatch
) -> None:
    posts: list[str] = []

    class _Resp:
        status_code = 200
        content = b""
        ok = True

    monkeypatch.setattr("requests.post",
                        lambda url, **kw: (posts.append(url), _Resp())[1])
    r = AudioRouter()
    r.route("send", "192.168.1.5", 8080, key=None, latency_ms=150)
    assert posts[0].endswith("/api/audio/receive")
    assert ("send_up", "192.168.1.5") in fake_audio.events


def test_route_peer_key_mismatch_cleans_up(
    fake_audio: _FakeAudio, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Resp401:
        status_code = 401
        content = b'{"error":"personal key required"}'
        ok = False

        def json(self):
            return {"error": "personal key required"}

    monkeypatch.setattr("requests.post", lambda url, **kw: _Resp401())
    r = AudioRouter()
    with pytest.raises(ProviderError, match="personal key"):
        r.route("receive", "192.168.1.5", 8080, key="wrong", sink="dac")
    # the orphaned local receiver must have been torn down
    assert r.status()["receiving"] is False
    assert any(e[0] == "recv_down" for e in fake_audio.events)
