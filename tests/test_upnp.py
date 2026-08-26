"""Offline tests for harmony.playback.upnp.

Standard UPnP AVTransport plumbing — duration parsing, DIDL-Lite building,
device-description parsing, and SOAP envelope/response handling — all exercised
with canned XML and a fake session. No network, no device.
"""

from __future__ import annotations

import pytest

from harmony.errors import ProviderError
from harmony.playback import upnp

# -- duration helpers -------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("0:03:42", 222),
        ("1:00:00", 3600),
        ("0:00:15", 15),
        ("00:01:15.500", 75),
        ("NOT_IMPLEMENTED", None),
        ("", None),
        (None, None),
        ("garbage", None),
    ],
)
def test_parse_duration(text, expected):
    assert upnp.parse_duration(text) == expected


@pytest.mark.parametrize("seconds,expected", [(0, "0:00:00"), (75, "0:01:15"), (3661, "1:01:01"), (None, "0:00:00")])
def test_format_duration(seconds, expected):
    assert upnp.format_duration(seconds) == expected


def test_duration_round_trips():
    for s in (0, 5, 59, 60, 222, 3600, 3661):
        assert upnp.parse_duration(upnp.format_duration(s)) == s


# -- DIDL-Lite --------------------------------------------------------------


def test_build_didl_includes_all_fields_and_escapes():
    didl = upnp.build_didl(
        title="Song & <Friends>",
        artist="A/B",
        album="The Album",
        art_url="http://art/x.jpg?a=1&b=2",
        media_url="http://host:9/play/tok?x=1&y=2",
        duration_s=222,
        mime="audio/mp4",
    )
    assert "<dc:title>Song &amp; &lt;Friends&gt;</dc:title>" in didl
    assert "<upnp:artist>A/B</upnp:artist>" in didl
    assert "<upnp:album>The Album</upnp:album>" in didl
    assert "albumArtURI>http://art/x.jpg?a=1&amp;b=2</upnp:albumArtURI>" in didl
    assert 'duration="0:03:42"' in didl
    assert "http-get:*:audio/mp4:*" in didl
    assert "play/tok?x=1&amp;y=2</res>" in didl
    assert "object.item.audioItem.musicTrack" in didl


def test_build_didl_omits_optional_fields():
    didl = upnp.build_didl(title="T", media_url="http://h/u")
    assert "upnp:artist" not in didl
    assert "upnp:album" not in didl
    assert "albumArtURI" not in didl
    assert "duration=" not in didl  # no duration attribute when unknown


# -- device description parsing ---------------------------------------------

_DESCRIPTION = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device>
    <friendlyName>WiiM Mini</friendlyName>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType>
        <controlURL>/RenderingControl/control</controlURL>
      </service>
      <service>
        <serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
        <controlURL>/AVTransport/control</controlURL>
      </service>
    </serviceList>
  </device>
</root>"""


class _FakeResponse:
    def __init__(self, content: bytes, status: int = 200) -> None:
        self.content = content
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"{self.status_code}")


class _FakeSession:
    def __init__(self, get_content: bytes = b"", post_content: bytes = b"") -> None:
        self._get_content = get_content
        self._post_content = post_content
        self.posts: list[dict] = []

    def get(self, url, **kw):
        return _FakeResponse(self._get_content)

    def post(self, url, *, data=None, headers=None, timeout=None):
        self.posts.append({"url": url, "data": data, "headers": headers})
        return _FakeResponse(self._post_content)


def test_find_avtransport_resolves_relative_control_url():
    session = _FakeSession(get_content=_DESCRIPTION.encode())
    svc = upnp.find_avtransport("http://192.168.1.5:49152/description.xml", session)
    assert svc is not None
    assert svc.control_url == "http://192.168.1.5:49152/AVTransport/control"
    assert svc.service_type == "urn:schemas-upnp-org:service:AVTransport:1"


def test_find_avtransport_none_when_absent():
    xml = b'<root xmlns="urn:schemas-upnp-org:device-1-0"><device><serviceList></serviceList></device></root>'
    assert upnp.find_avtransport("http://h/d.xml", _FakeSession(get_content=xml)) is None


# -- SOAP -------------------------------------------------------------------

_SERVICE = upnp.AvTransport("http://192.168.1.5:49152/AVTransport/control", "urn:schemas-upnp-org:service:AVTransport:1")

_POSITION_RESPONSE = b"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">
  <s:Body>
    <u:GetPositionInfoResponse xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
      <Track>1</Track>
      <TrackDuration>0:03:42</TrackDuration>
      <RelTime>0:01:15</RelTime>
      <TrackURI>http://host/play/tok</TrackURI>
    </u:GetPositionInfoResponse>
  </s:Body>
</s:Envelope>"""

_FAULT_RESPONSE = b"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">
  <s:Body><s:Fault>
    <faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring>
    <detail><UPnPError xmlns="urn:schemas-upnp-org:control-1-0">
      <errorCode>701</errorCode><errorDescription>Transition not available</errorDescription>
    </UPnPError></detail>
  </s:Fault></s:Body>
</s:Envelope>"""


def test_soap_sends_action_header_and_envelope():
    session = _FakeSession(post_content=_POSITION_RESPONSE)
    renderer = upnp.UpnpRenderer(_SERVICE, session=session)
    out = renderer.get_position_info()
    assert out["TrackDuration"] == "0:03:42"
    assert out["RelTime"] == "0:01:15"
    call = session.posts[0]
    assert call["headers"]["SOAPAction"] == '"urn:schemas-upnp-org:service:AVTransport:1#GetPositionInfo"'
    assert b"<u:GetPositionInfo " in call["data"]
    assert b"<InstanceID>0</InstanceID>" in call["data"]


def test_play_media_sets_uri_then_plays():
    session = _FakeSession(post_content=b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body/></s:Envelope>')
    renderer = upnp.UpnpRenderer(_SERVICE, session=session)
    renderer.play_media("http://h/play/tok", title="T", artist="A", duration_s=222, mime="audio/mp4")
    actions = [p["headers"]["SOAPAction"] for p in session.posts]
    assert actions == [
        '"urn:schemas-upnp-org:service:AVTransport:1#SetAVTransportURI"',
        '"urn:schemas-upnp-org:service:AVTransport:1#Play"',
    ]
    set_uri = session.posts[0]["data"]
    assert b"CurrentURI" in set_uri and b"CurrentURIMetaData" in set_uri
    assert b"&lt;DIDL-Lite" in set_uri  # DIDL is xml-escaped inside the SOAP arg


def test_soap_fault_raises_provider_error():
    session = _FakeSession(post_content=_FAULT_RESPONSE)
    renderer = upnp.UpnpRenderer(_SERVICE, session=session)
    with pytest.raises(ProviderError, match="Transition not available"):
        renderer.play()


def test_seek_formats_target():
    session = _FakeSession(post_content=b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body/></s:Envelope>')
    upnp.UpnpRenderer(_SERVICE, session=session).seek(75)
    assert b"<Target>0:01:15</Target>" in session.posts[0]["data"]
    assert b"<Unit>REL_TIME</Unit>" in session.posts[0]["data"]
