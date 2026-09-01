"""Mesh address selection: reach a multi-homed peer on the most direct path."""

from __future__ import annotations

import ipaddress

from harmony.mesh import _address_rank, _best_address


def _nets(*cidrs: str) -> list[ipaddress.IPv4Network]:
    return [ipaddress.ip_network(c, strict=False) for c in cidrs]


def test_prefers_same_subnet_lan_over_tailscale() -> None:
    # The real bug: the server advertised its Tailscale address first and its LAN
    # address second; picking addresses[0] routed over the overlay.
    local = _nets("192.168.12.21/24")
    assert _best_address(["100.71.217.4", "192.168.12.70"], local) == "192.168.12.70"
    # Order must not matter.
    assert _best_address(["192.168.12.70", "100.71.217.4"], local) == "192.168.12.70"


def test_ranks_direct_below_cgnat_below_linklocal() -> None:
    local = _nets("192.168.12.21/24")
    assert _address_rank("192.168.12.70", local) < _address_rank("100.71.217.4", local)
    assert _address_rank("100.71.217.4", local) < _address_rank("169.254.1.1", local)


def test_falls_back_to_overlay_when_no_direct_path() -> None:
    # Peer only reachable via Tailscale (no shared subnet) — still usable.
    local = _nets("10.0.0.5/24")
    assert _best_address(["100.71.217.4"], local) == "100.71.217.4"


def test_private_but_offsubnet_beats_cgnat() -> None:
    # A LAN-class address we can't prove is same-subnet still beats the overlay.
    local: list[ipaddress.IPv4Network] = []
    assert _best_address(["100.71.217.4", "192.168.9.9"], local) == "192.168.9.9"


def test_empty_addresses_is_none() -> None:
    assert _best_address([], _nets("192.168.1.2/24")) is None
