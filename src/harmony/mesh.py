"""LAN mesh: advertise this Harmony instance and discover peers via mDNS.

Shared by every instance -- the headless server and the desktop app both run a
``Mesh`` so they find each other on the network (the desktop is a first-class
node, not just a client). A signed-out client then picks a discovered instance
and authenticates with its [[personal key]].

GTK-free (engine layer). ``python-zeroconf`` is an **optional** dependency: if it
isn't installed the mesh degrades to a no-op (no advertising, no peers) and the
rest of the app runs unaffected -- so a minimal/offline build still works.
"""

from __future__ import annotations

import logging
import socket
import threading
from typing import Any

from harmony import __version__

log = logging.getLogger(__name__)

SERVICE_TYPE = "_harmony._tcp.local."


def _primary_ipv4() -> str:
    """This host's primary LAN IPv4 (best-effort), for the mDNS advertisement."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))  # no packet is sent; just picks the egress iface
        return sock.getsockname()[0]
    except Exception:  # noqa: BLE001
        return "127.0.0.1"
    finally:
        sock.close()


def _all_ipv4() -> list[str]:
    """Every non-loopback IPv4 on this host, so a multi-homed/VPN'd instance
    advertises all the addresses a client might reach it on — not just the
    default-route one (which can be the wrong interface). Falls back to the
    primary address if interface enumeration isn't available."""
    addrs: list[str] = []
    try:
        import ifaddr  # ships with python-zeroconf

        for adapter in ifaddr.get_adapters():
            for ip in adapter.ips:
                # ifaddr represents IPv4 as a str, IPv6 as a tuple.
                if isinstance(ip.ip, str) and not ip.ip.startswith("127."):
                    if ip.ip not in addrs:
                        addrs.append(ip.ip)
    except Exception:  # noqa: BLE001
        pass
    if not addrs:
        primary = _primary_ipv4()
        if primary != "127.0.0.1":
            addrs.append(primary)
    return addrs


class Mesh:
    """Advertises this instance on ``_harmony._tcp`` and tracks discovered peers.

    ``start()`` is safe to call when zeroconf is missing (it just logs and does
    nothing). ``peers()`` returns the currently-known other instances.
    """

    def __init__(self, name: str, port: int) -> None:
        self._name = name
        self._port = port
        self._zc: Any | None = None
        self._info: Any | None = None
        self._browser: Any | None = None
        self._lock = threading.Lock()
        self._peers: dict[str, dict[str, Any]] = {}  # service name -> {name, host, port, version}
        self._service_name = f"{name}.{SERVICE_TYPE}"

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        try:
            from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf
        except ImportError:
            log.info("python-zeroconf not installed; LAN mesh disabled")
            return
        try:
            self._zc = Zeroconf()
            addresses = [socket.inet_aton(a) for a in _all_ipv4()] or [socket.inet_aton("127.0.0.1")]
            self._info = ServiceInfo(
                SERVICE_TYPE,
                self._service_name,
                addresses=addresses,
                port=self._port,
                properties={"name": self._name, "version": __version__},
            )
            self._zc.register_service(self._info)
            self._browser = ServiceBrowser(self._zc, SERVICE_TYPE, _Listener(self))
            log.info("mesh: advertising %s on :%s and browsing for peers", self._name, self._port)
        except Exception as exc:  # noqa: BLE001 - mesh is best-effort; never break startup
            log.warning("mesh: could not start (%s); LAN discovery disabled", exc)
            self.stop()

    def stop(self) -> None:
        zc = self._zc
        self._zc = self._browser = None
        if zc is not None:
            try:
                if self._info is not None:
                    zc.unregister_service(self._info)
                zc.close()
            except Exception:  # noqa: BLE001
                pass

    # -- peers --------------------------------------------------------------

    def peers(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted(self._peers.values(), key=lambda p: p.get("name", ""))

    def _resolve(self, zc: Any, type_: str, name: str) -> None:
        if name == self._service_name:
            return  # ignore our own advertisement
        try:
            info = zc.get_service_info(type_, name, timeout=2000)
        except Exception:  # noqa: BLE001
            info = None
        if info is None:
            return
        addresses = []
        try:
            addresses = info.parsed_addresses()
        except Exception:  # noqa: BLE001
            addresses = []
        host = addresses[0] if addresses else None
        props = {k.decode(): (v.decode() if isinstance(v, bytes) else v)
                 for k, v in (info.properties or {}).items() if k}
        peer = {
            "name": props.get("name") or name.split(".")[0],
            "host": host,
            "port": info.port,
            "version": props.get("version"),
        }
        with self._lock:
            self._peers[name] = peer

    def _remove(self, name: str) -> None:
        with self._lock:
            self._peers.pop(name, None)


class _Listener:
    """zeroconf ServiceBrowser listener → forwards changes to the Mesh."""

    def __init__(self, mesh: Mesh) -> None:
        self._mesh = mesh

    def add_service(self, zc: Any, type_: str, name: str) -> None:
        self._mesh._resolve(zc, type_, name)

    def update_service(self, zc: Any, type_: str, name: str) -> None:
        self._mesh._resolve(zc, type_, name)

    def remove_service(self, zc: Any, type_: str, name: str) -> None:
        self._mesh._remove(name)
