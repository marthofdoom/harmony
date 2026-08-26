"""UPnP AVTransport control for LinkPlay/WiiM (and any DLNA MediaRenderer).

The LinkPlay ``httpapi.asp`` ``setPlayerCmd:play:<url>`` path plays audio but
carries no rich metadata, so the device shows the bare URL and can't report a
track duration. A DLNA renderer's *own* now-playing display (and the WiiM app,
and a usable position/duration for a progress bar) is driven instead by **UPnP
AVTransport**: ``SetAVTransportURI(uri, <DIDL-Lite>)`` then ``Play`` — the
DIDL-Lite XML carries title/artist/album/art/duration, and ``GetPositionInfo``
reports elapsed/total time.

This module is the control plane for that: locate a device's AVTransport
service, build DIDL-Lite, and issue the SOAP actions. Engine-only, no GTK.

The SOAP/DIDL/description-parsing here is standard UPnP and unit-tested offline.
Locating the service description on a specific device is the one device-fuzzy
part (``description_url_for``) and wants verification against real hardware.
"""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import requests

from ..errors import ProviderError

log = logging.getLogger(__name__)

_TIMEOUT_S = 5.0

# Common LinkPlay UPnP description location, tried if unicast SSDP finds nothing.
_FALLBACK_DESC = "http://{host}:49152/description.xml"


@dataclass(frozen=True)
class AvTransport:
    """A resolved AVTransport service endpoint on a renderer."""

    control_url: str
    service_type: str


# --------------------------------------------------------------------------
# Time helpers (UPnP uses H:MM:SS strings)
# --------------------------------------------------------------------------


def parse_duration(text: str | None) -> int | None:
    """Parse a UPnP ``H:MM:SS`` (or ``HH:MM:SS.mmm``) time into whole seconds."""
    if not text:
        return None
    text = text.strip()
    if not text or text in ("NOT_IMPLEMENTED", "0:00:00"):
        return 0 if text == "0:00:00" else None
    parts = text.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    seconds = 0.0
    for value in nums:  # most-significant first: H, M, S
        seconds = seconds * 60 + value
    return int(seconds)


def format_duration(seconds: int | None) -> str:
    """Format whole seconds as UPnP ``H:MM:SS`` (used for Seek targets/DIDL res)."""
    total = max(0, int(seconds or 0))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


# --------------------------------------------------------------------------
# DIDL-Lite metadata
# --------------------------------------------------------------------------


def build_didl(
    *,
    title: str,
    artist: str | None = None,
    album: str | None = None,
    art_url: str | None = None,
    media_url: str,
    duration_s: int | None = None,
    mime: str = "audio/mpeg",
) -> str:
    """Build a DIDL-Lite document describing one audio item for a renderer.

    The renderer shows ``title``/``artist``/``album``/album art from this, and
    ``duration`` (when known) lets it render a progress bar and total time. The
    ``<res>`` protocolInfo advertises the transport/mime so the device knows how
    to fetch and decode ``media_url``.
    """
    duration_attr = f' duration="{escape(format_duration(duration_s))}"' if duration_s else ""
    protocol_info = f"http-get:*:{mime}:*"
    parts = [
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"',
        ' xmlns:dc="http://purl.org/dc/elements/1.1/"',
        ' xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">',
        '<item id="0" parentID="-1" restricted="1">',
        f"<dc:title>{escape(title or 'Unknown')}</dc:title>",
        "<upnp:class>object.item.audioItem.musicTrack</upnp:class>",
    ]
    if artist:
        parts.append(f"<upnp:artist>{escape(artist)}</upnp:artist>")
        parts.append(f"<dc:creator>{escape(artist)}</dc:creator>")
    if album:
        parts.append(f"<upnp:album>{escape(album)}</upnp:album>")
    if art_url:
        parts.append(f"<upnp:albumArtURI>{escape(art_url)}</upnp:albumArtURI>")
    parts.append(f'<res protocolInfo="{escape(protocol_info)}"{duration_attr}>{escape(media_url)}</res>')
    parts.append("</item></DIDL-Lite>")
    return "".join(parts)


# --------------------------------------------------------------------------
# Service discovery (parse the device description for AVTransport)
# --------------------------------------------------------------------------


def _local(tag: str) -> str:
    """Strip an XML namespace: ``{ns}serviceType`` -> ``serviceType``."""
    return tag.rsplit("}", 1)[-1]


def find_avtransport(description_url: str, session: requests.Session) -> AvTransport | None:
    """Fetch a device description and return its AVTransport control endpoint.

    ``controlURL`` in the description is relative to the description URL's
    origin, so it's resolved against it. Returns None if the device exposes no
    AVTransport service (or the description can't be fetched/parsed).
    """
    try:
        resp = session.get(description_url, timeout=_TIMEOUT_S)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except (requests.RequestException, ET.ParseError) as exc:
        log.debug("UPnP: could not read device description at %s: %s", description_url, exc)
        return None
    return _avtransport_from_description(root, description_url)


def _avtransport_from_description(root: ET.Element, description_url: str) -> AvTransport | None:
    from urllib.parse import urljoin

    for service in root.iter():
        if _local(service.tag) != "service":
            continue
        fields = {_local(child.tag): (child.text or "").strip() for child in service}
        service_type = fields.get("serviceType", "")
        if "AVTransport" not in service_type:
            continue
        control = fields.get("controlURL")
        if not control:
            continue
        return AvTransport(control_url=urljoin(description_url, control), service_type=service_type)
    return None


def description_url_for(host: str, timeout: float = _TIMEOUT_S) -> str | None:
    """Best-effort: find a host's UPnP description URL via unicast SSDP.

    Sends an M-SEARCH straight to the device and reads the ``LOCATION`` header
    from its reply. Falls back to the common LinkPlay description path. Device
    behaviour varies here, so this is the part to confirm against real hardware.
    """
    msearch = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {host}:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        "ST: urn:schemas-upnp-org:service:AVTransport:1\r\n"
        "MX: 1\r\n"
        "\r\n"
    ).encode()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        try:
            sock.sendto(msearch, (host, 1900))
            data, _addr = sock.recvfrom(2048)
        finally:
            sock.close()
        for line in data.decode("utf-8", "ignore").splitlines():
            if line.lower().startswith("location:"):
                return line.split(":", 1)[1].strip()
    except OSError as exc:
        log.debug("UPnP: unicast SSDP to %s failed: %s", host, exc)
    return _FALLBACK_DESC.format(host=host)


# --------------------------------------------------------------------------
# SOAP control
# --------------------------------------------------------------------------


class UpnpRenderer:
    """Issues AVTransport SOAP actions against one resolved service endpoint."""

    def __init__(self, service: AvTransport, session: requests.Session | None = None) -> None:
        self._service = service
        self._session = session or requests.Session()

    def _soap(self, action: str, args: dict[str, str]) -> dict[str, str]:
        """POST a SOAP action; return the response's out-arguments as a flat dict."""
        body_args = "".join(f"<{k}>{escape(v)}</{k}>" for k, v in args.items())
        envelope = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
            ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            f'<s:Body><u:{action} xmlns:u="{self._service.service_type}">'
            f"{body_args}</u:{action}></s:Body></s:Envelope>"
        ).encode()
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"{self._service.service_type}#{action}"',
        }
        try:
            resp = self._session.post(
                self._service.control_url, data=envelope, headers=headers, timeout=_TIMEOUT_S
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise ProviderError(f"UPnP {action} failed: {exc}") from exc
        return _parse_soap_response(resp.content, action)

    def set_av_transport_uri(self, url: str, didl_metadata: str) -> None:
        self._soap(
            "SetAVTransportURI",
            {"InstanceID": "0", "CurrentURI": url, "CurrentURIMetaData": didl_metadata},
        )

    def set_next_av_transport_uri(self, url: str, didl_metadata: str) -> None:
        """Queue the following track for gapless advance (used for playlist playback)."""
        self._soap(
            "SetNextAVTransportURI",
            {"InstanceID": "0", "NextURI": url, "NextURIMetaData": didl_metadata},
        )

    def play(self) -> None:
        self._soap("Play", {"InstanceID": "0", "Speed": "1"})

    def pause(self) -> None:
        self._soap("Pause", {"InstanceID": "0"})

    def stop(self) -> None:
        self._soap("Stop", {"InstanceID": "0"})

    def seek(self, position_s: int) -> None:
        self._soap("Seek", {"InstanceID": "0", "Unit": "REL_TIME", "Target": format_duration(position_s)})

    def get_position_info(self) -> dict[str, str]:
        """Return GetPositionInfo out-args (TrackDuration, RelTime, TrackURI, ...)."""
        return self._soap("GetPositionInfo", {"InstanceID": "0"})

    def play_media(
        self,
        url: str,
        *,
        title: str,
        artist: str | None = None,
        album: str | None = None,
        art_url: str | None = None,
        duration_s: int | None = None,
        mime: str = "audio/mpeg",
    ) -> None:
        """Set the URI (with DIDL-Lite metadata) and start playback."""
        didl = build_didl(
            title=title,
            artist=artist,
            album=album,
            art_url=art_url,
            media_url=url,
            duration_s=duration_s,
            mime=mime,
        )
        self.set_av_transport_uri(url, didl)
        self.play()


def _parse_soap_response(content: bytes, action: str) -> dict[str, str]:
    """Extract the ``<action>Response`` out-arguments (or raise on a SOAP Fault)."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ProviderError(f"UPnP {action}: unparsable response") from exc
    fault = next((el for el in root.iter() if _local(el.tag) == "Fault"), None)
    if fault is not None:
        desc = next(
            (el.text for el in fault.iter() if _local(el.tag) == "errorDescription" and el.text), ""
        )
        raise ProviderError(f"UPnP {action} fault: {desc or 'unknown error'}")
    response = next((el for el in root.iter() if _local(el.tag) == f"{action}Response"), None)
    if response is None:
        return {}
    return {_local(child.tag): (child.text or "") for child in response}


__all__ = [
    "AvTransport",
    "UpnpRenderer",
    "build_didl",
    "description_url_for",
    "find_avtransport",
    "format_duration",
    "parse_duration",
]
