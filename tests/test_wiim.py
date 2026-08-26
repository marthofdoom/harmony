"""Offline tests for harmony.playback.wiim and .discovery.

Everything here drives a fake ``requests.Session`` double or a canned string
-- nothing touches the network or a real device.
"""

from __future__ import annotations

import pytest
import requests

from harmony.errors import ProviderError
from harmony.playback.base import DeviceInfo
from harmony.playback.discovery import _parse_ssdp_response
from harmony.playback.wiim import WiiMDevice, device_from_host


class FakeResponse:
    def __init__(self, status_code: int = 200, text: str | None = None, json_data=None) -> None:
        self.status_code = status_code
        self.text = text if text is not None else ("OK" if json_data is None else "{...}")
        self._json = json_data

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


class FakeSession:
    """A ``requests.Session`` double: ``handler(url, timeout, verify)`` decides the reply."""

    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls: list[dict] = []

    def get(self, url, timeout=None, verify=True):
        self.calls.append({"url": url, "timeout": timeout, "verify": verify})
        return self.handler(url, timeout=timeout, verify=verify)


def _hex(s: str) -> str:
    return s.encode("utf-8").hex()


def _device(handler, *, info: DeviceInfo | None = None) -> tuple[WiiMDevice, FakeSession]:
    session = FakeSession(handler)
    dev = WiiMDevice("192.168.1.50", session=session, info=info or DeviceInfo(id="x", name="x", host="192.168.1.50"))
    return dev, session


# ---------------------------------------------------------------------------
# getPlayerStatus -> PlaybackStatus
# ---------------------------------------------------------------------------


def test_status_maps_fields_and_decodes_hex_and_ms():
    payload = {
        "status": "play",
        "vol": "42",
        "mute": "0",
        "curpos": "1500",
        "totlen": "240000",
        "Title": _hex("Song Title"),
        "Artist": _hex("The Band"),
    }
    dev, _ = _device(lambda url, **kw: FakeResponse(json_data=payload))

    status = dev.status()

    assert status.state == "playing"
    assert status.volume == 42
    assert status.muted is False
    assert status.position_s == 1
    assert status.duration_s == 240
    assert status.title == "Song Title"
    assert status.artist == "The Band"
    assert status.raw == payload


def test_status_maps_pause_stop_and_unknown_states():
    for raw, expected in [("pause", "paused"), ("stop", "stopped"), ("garbage", "unknown")]:
        payload = {"status": raw, "vol": "10", "mute": "1", "curpos": "0", "totlen": "0"}
        dev, _ = _device(lambda url, payload=payload, **kw: FakeResponse(json_data=payload))
        status = dev.status()
        assert status.state == expected
        assert status.muted is True


def test_status_handles_missing_or_non_hex_title():
    payload = {"status": "stop", "vol": "0", "mute": "0", "curpos": "", "totlen": "", "Title": "not-hex!"}
    dev, _ = _device(lambda url, **kw: FakeResponse(json_data=payload))

    status = dev.status()

    assert status.position_s is None
    assert status.duration_s is None
    assert status.title == "not-hex!"
    assert status.artist is None


# ---------------------------------------------------------------------------
# volume clamping / command construction
# ---------------------------------------------------------------------------


def test_set_volume_clamps_high():
    dev, session = _device(lambda url, **kw: FakeResponse(text="OK"))
    dev.set_volume(150)
    assert session.calls[-1]["url"].endswith("setPlayerCmd:vol:100")


def test_set_volume_clamps_low():
    dev, session = _device(lambda url, **kw: FakeResponse(text="OK"))
    dev.set_volume(-5)
    assert session.calls[-1]["url"].endswith("setPlayerCmd:vol:0")


def test_set_muted_true_and_false():
    dev, session = _device(lambda url, **kw: FakeResponse(text="OK"))
    dev.set_muted(True)
    assert session.calls[-1]["url"].endswith("setPlayerCmd:mute:1")
    dev.set_muted(False)
    assert session.calls[-1]["url"].endswith("setPlayerCmd:mute:0")


def test_play_url_url_encodes_the_stream_url():
    dev, session = _device(lambda url, **kw: FakeResponse(text="OK"))
    dev.play_url("http://example.com/stream?a=b c")
    called = session.calls[-1]["url"]
    assert "setPlayerCmd:play:http%3A%2F%2Fexample.com%2Fstream%3Fa%3Db%20c" in called


def test_pause_resume_stop_next_previous_hit_expected_commands():
    dev, session = _device(lambda url, **kw: FakeResponse(text="OK"))
    dev.pause()
    dev.resume()
    dev.stop()
    dev.next()
    dev.previous()
    commands = [c["url"].split("command=")[1] for c in session.calls]
    assert commands == [
        "setPlayerCmd:pause",
        "setPlayerCmd:resume",
        "setPlayerCmd:stop",
        "setPlayerCmd:next",
        "setPlayerCmd:prev",
    ]


def test_set_command_raises_when_device_does_not_confirm_ok():
    dev, _ = _device(lambda url, **kw: FakeResponse(text="ERROR"))
    with pytest.raises(ProviderError):
        dev.pause()


# ---------------------------------------------------------------------------
# transport failure / http -> https fallback
# ---------------------------------------------------------------------------


def test_non_2xx_raises_provider_error():
    dev, _ = _device(lambda url, **kw: FakeResponse(status_code=500, text="boom"))
    with pytest.raises(ProviderError):
        dev.status()


def test_http_failure_falls_back_to_https_with_verification_disabled():
    payload = {"status": "play", "vol": "5", "mute": "0", "curpos": "0", "totlen": "0"}

    def handler(url, timeout=None, verify=True):
        if url.startswith("http://"):
            raise requests.ConnectionError("refused")
        assert url.startswith("https://")
        assert verify is False
        return FakeResponse(json_data=payload)

    dev, session = _device(handler)
    status = dev.status()

    assert status.volume == 5
    assert session.calls[0]["url"].startswith("http://")
    assert session.calls[1]["url"].startswith("https://")

    # The scheme sticks: a second call goes straight to https, no retry of http.
    dev.status()
    assert session.calls[2]["url"].startswith("https://")
    assert len(session.calls) == 3


def test_https_failure_after_fallback_raises_provider_error():
    def handler(url, timeout=None, verify=True):
        raise requests.ConnectionError("refused")

    dev, _ = _device(handler)
    with pytest.raises(ProviderError):
        dev.status()


def test_non_json_non_ok_body_raises_provider_error():
    dev, _ = _device(lambda url, **kw: FakeResponse(text="<html>not json</html>"))
    with pytest.raises(ProviderError):
        dev.status()


# ---------------------------------------------------------------------------
# getStatusEx -> DeviceInfo
# ---------------------------------------------------------------------------


def test_get_status_ex_populates_device_info_lazily():
    payload = {"uuid": "FF31F09E-1234", "DeviceName": "Living Room", "hardware": "WiiM Mini"}
    session = FakeSession(lambda url, **kw: FakeResponse(json_data=payload))
    dev = WiiMDevice("10.0.0.5", session=session)

    info = dev.info

    assert info.id == "FF31F09E-1234"
    assert info.name == "Living Room"
    assert info.host == "10.0.0.5"
    assert info.model == "WiiM Mini"
    assert session.calls[-1]["url"].endswith("command=getStatusEx")
    # Cached: fetching info again does not re-hit the device.
    _ = dev.info
    assert len(session.calls) == 1


def test_get_status_ex_falls_back_to_host_when_uuid_missing():
    session = FakeSession(lambda url, **kw: FakeResponse(json_data={"DeviceName": "Kitchen"}))
    dev = WiiMDevice("10.0.0.6", session=session)
    assert dev.info.id == "10.0.0.6"


def test_device_from_host_returns_a_wiim_device():
    dev = device_from_host("10.0.0.7")
    assert isinstance(dev, WiiMDevice)
    assert dev.host == "10.0.0.7"


# ---------------------------------------------------------------------------
# SSDP response parsing (discovery.py), no sockets involved
# ---------------------------------------------------------------------------


def test_parse_ssdp_response_extracts_host_from_location():
    response = (
        "HTTP/1.1 200 OK\r\n"
        "CACHE-CONTROL: max-age=1800\r\n"
        "LOCATION: http://192.168.1.42:49152/description.xml\r\n"
        "SERVER: Linux/3.10 UPnP/1.0 LinkPlay/1.0\r\n"
        "ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n"
        "USN: uuid:FF31F09E-1234::urn:schemas-upnp-org:device:MediaRenderer:1\r\n"
        "\r\n"
    )
    assert _parse_ssdp_response(response) == "192.168.1.42"


def test_parse_ssdp_response_returns_none_without_location():
    response = "HTTP/1.1 200 OK\r\nST: something\r\n\r\n"
    assert _parse_ssdp_response(response) is None
