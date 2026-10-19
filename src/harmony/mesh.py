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

import ipaddress
import logging
import socket
import threading
import time
from typing import Any

from harmony import __version__

log = logging.getLogger(__name__)

SERVICE_TYPE = "_harmony._tcp.local."

# RFC 6598 shared address space — Tailscale and carrier-grade NAT live here. A
# peer reachable on both this and a real LAN address should be reached on the LAN
# (a direct hop), not routed over the overlay.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


_NETS_TTL_S = 10.0
_nets_cache: tuple[float, list[ipaddress.IPv4Network]] | None = None
_nets_lock = threading.Lock()


def _local_networks() -> list[ipaddress.IPv4Network]:
    """The IPv4 networks this host is directly attached to (ip/prefix per
    interface). A peer address inside one of these is on a shared subnet — a
    direct hop — so we prefer it over a VPN/overlay address for the same peer.
    Cached for a few seconds: interface enumeration is slow and it's asked per
    row of every browse (a laptop changing network is picked up within 10 s)."""
    global _nets_cache
    now = time.monotonic()
    with _nets_lock:
        if _nets_cache is not None and now - _nets_cache[0] < _NETS_TTL_S:
            return list(_nets_cache[1])
    nets = _enumerate_local_networks()
    with _nets_lock:
        _nets_cache = (now, nets)
    return list(nets)


def _enumerate_local_networks() -> list[ipaddress.IPv4Network]:
    nets: list[ipaddress.IPv4Network] = []
    try:
        import ifaddr

        for adapter in ifaddr.get_adapters():
            for ip in adapter.ips:
                if isinstance(ip.ip, str) and not ip.ip.startswith("127."):
                    try:
                        nets.append(ipaddress.ip_network(f"{ip.ip}/{ip.network_prefix}", strict=False))
                    except ValueError:
                        pass
    except Exception:  # noqa: BLE001
        pass
    return nets


def _address_rank(addr: str, local_nets: list[ipaddress.IPv4Network]) -> int:
    """Preference for a peer address — lower is better. Direct (same-subnet LAN)
    wins; a VPN/CGNAT overlay is the last routable resort."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return 9
    if ip.version != 4:
        return 6  # deprioritise IPv6 for now; the app speaks IPv4 LAN
    if ip.is_loopback:
        return 8
    if ip.is_link_local:  # 169.254/16 — not routable to a peer
        return 7
    if any(ip in net for net in local_nets):
        return 0  # same subnet as one of our interfaces — the direct hop
    if ip in _CGNAT:
        return 5  # Tailscale / CGNAT overlay
    if ip.is_private:
        return 1  # RFC1918 but not a shared subnet — still a LAN address
    return 3  # public/other


def _best_address(addresses: list[str], local_nets: list[ipaddress.IPv4Network]) -> str | None:
    """Pick the most-direct address a peer advertises (see _address_rank)."""
    if not addresses:
        return None
    return min(addresses, key=lambda a: (_address_rank(a, local_nets), a))


def host_rank(host: str, local_nets: list[ipaddress.IPv4Network] | None = None) -> int:
    """Directness of a peer host for picking among addresses of ONE instance.
    IP literals use _address_rank; a name resolves unknown, so it ranks between a
    LAN address and a public/overlay one."""
    try:
        ipaddress.ip_address(str(host))
    except ValueError:
        return 2
    return _address_rank(str(host), _local_networks() if local_nets is None else local_nets)


def dedupe_instances(insts: list[dict[str, Any]], own_id: str | None = None,
                     own_port: int | None = None) -> list[dict[str, Any]]:
    """One row per Harmony instance. Rows carrying an ``instance_id`` collapse by
    it (keeping the most direct address, LAN over Tailscale); rows without one
    (older peers) collapse by host:port. Our own id is dropped -- but only when
    the row's port is ours too (``own_port``, if given): two instances sharing a
    copied config share an id, and must not hide each other.

    Each row also carries ``addresses`` (every ``host:port`` the instance is
    known by, most direct first) and ``saved`` (``[{host, port}]`` the user
    saved by hand). A collapsed row whose members were partly saved reports
    ``source: "both"`` so it can still be removed."""
    nets = _local_networks()
    groups: dict[str, list[dict[str, Any]]] = {}
    for p in insts:
        iid = p.get("instance_id")
        if iid and iid == own_id and (own_port is None or _int(p.get("port")) == own_port):
            continue
        key = f"id:{iid}" if iid else f"{p.get('host')}:{p.get('port')}"
        groups.setdefault(key, []).append(p)

    def order(p: dict[str, Any]) -> tuple[int, str, str]:
        return (host_rank(p.get("host"), nets), str(p.get("host")), str(p.get("port")))

    out: list[dict[str, Any]] = []
    for members in groups.values():
        ranked = sorted(members, key=order)
        row = dict(ranked[0])
        addrs: list[str] = []
        for m in ranked:
            a = f"{m.get('host')}:{m.get('port')}"
            if a not in addrs:
                addrs.append(a)
        saved = [{"host": m.get("host"), "port": m.get("port")}
                 for m in ranked if m.get("source") in ("manual", "both")]
        row["addresses"] = addrs
        row["saved"] = saved
        if saved:
            row["source"] = "manual" if all(m.get("source") == "manual" for m in ranked) else "both"
        out.append(row)
    return out


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def is_local_address(host: str) -> bool:
    """Is ``host`` an address (or a name resolving to one) of THIS machine?"""
    mine = {"127.0.0.1", "::1", "localhost", *_all_ipv4()}
    host = (host or "").strip().strip("[]")
    if host in mine:
        return True
    try:
        return socket.gethostbyname(host) in mine or socket.gethostbyname(host).startswith("127.")
    except OSError:
        return False


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

    def __init__(self, name: str, port: int, instance_id: str = "") -> None:
        self._name = name
        self._instance_id = instance_id
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
                properties={"name": self._name, "version": __version__,
                            **({"iid": self._instance_id} if self._instance_id else {})},
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
        # A multi-homed peer advertises every address it has (LAN + Tailscale +
        # …) in arbitrary order. Reach it on the most direct one.
        host = _best_address(addresses, _local_networks())
        props = {k.decode(): (v.decode() if isinstance(v, bytes) else v)
                 for k, v in (info.properties or {}).items() if k}
        peer = {
            "name": props.get("name") or name.split(".")[0],
            "host": host,
            "port": info.port,
            "version": props.get("version"),
            "instance_id": props.get("iid") or None,
        }
        if self._instance_id and peer["instance_id"] == self._instance_id and info.port == self._port:
            return  # our own advertisement under another service name
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
