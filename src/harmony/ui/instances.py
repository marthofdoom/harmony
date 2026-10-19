"""Other Harmony instances: parse an address, add/forget a peer, probe the list.

Pure helpers (no GTK): every function here blocks on the network and must run
in a ``run_async`` worker; the callers build widgets from the returned data in
their done callbacks.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

DEFAULT_PORT = 8080


def parse_address(text: str) -> tuple[str, int]:
    """``"host"`` / ``"host:port"`` / ``"http://host:port/"`` -> ``(host, port)``.

    Raises ``ValueError`` with a user-facing message."""
    t = (text or "").strip()
    for prefix in ("http://", "https://"):
        if t.lower().startswith(prefix):
            t = t[len(prefix):]
    t = t.split("/", 1)[0].strip()
    if not t:
        raise ValueError("Enter the server's address first.")
    host, port_s = t, ""
    if t.startswith("["):  # [v6]:port
        close = t.find("]")
        host, port_s = t[1:close], t[close + 1:].lstrip(":")
    elif t.count(":") == 1:
        host, _, port_s = t.partition(":")
    host = host.strip()
    if not host:
        raise ValueError("Enter the server's address first.")
    try:
        port = int(port_s) if port_s.strip() else DEFAULT_PORT
    except ValueError:
        raise ValueError("Port must be a number (use host or host:port).") from None
    if not 0 < port < 65536:
        raise ValueError("Port must be between 1 and 65535.")
    return host, port


def _engine() -> Any:
    from harmony.web.server import get_engine

    return get_engine()


def add_instance(text: str) -> dict[str, Any]:
    """Probe ``/healthz`` and remember the instance as a known peer (worker)."""
    host, port = parse_address(text)
    result = _engine().add_peer(host, port)
    if not result.get("ok"):
        reason = str(result.get("reason") or "no answer")
        if reason.startswith("unreachable") or reason == "no answer":
            raise RuntimeError(f"Couldn't reach {host}:{port} ({reason}).")
        # "not a Harmony server" / "that's this instance": say exactly that.
        raise RuntimeError(f"{host}:{port} — {reason}.")
    return result


def remember_instance(host: str, port: int) -> None:
    """Best-effort ``add_peer`` after a successful credential sync (worker)."""
    try:
        _engine().add_peer(host, port)
    except Exception:  # noqa: BLE001 - remembering the host is a courtesy
        pass


def forget_instance(host: str, port: int, instance_id: str | None = None,
                    saved: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Remove a saved instance: by id when known (all its saved addresses),
    and by each saved address of the row (saved before ids existed)."""
    engine = _engine()
    result = engine.remove_peer(host, port, instance_id=instance_id)
    for s in saved or []:
        if s.get("host") and s.get("port") and (s["host"], s["port"]) != (host, port):
            engine.remove_peer(s["host"], s["port"])
    return result


def version_tuple(v: str | None) -> tuple[int, ...]:
    out: list[int] = []
    for part in (v or "").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        if not digits:
            break
        out.append(int(digits))
    return tuple(out)


def needs_update(peer_version: str | None, own_version: str) -> bool:
    return bool(peer_version) and version_tuple(peer_version) < version_tuple(own_version)


def _probe_one(inst: dict[str, Any], key: str | None) -> dict[str, Any]:
    import requests

    host, port = inst.get("host"), inst.get("port")
    base = f"http://{f'[{host}]' if ':' in str(host) else host}:{port}"
    row = {"name": inst.get("name") or host, "host": host, "port": port,
           "source": inst.get("source", ""), "instance_id": inst.get("instance_id"),
           "saved": inst.get("saved") or [], "version": inst.get("version"),
           "tracks": None, "reachable": False}
    try:
        r = requests.get(base + "/healthz", timeout=2.5)
        if r.ok:
            row["reachable"] = True
            row["version"] = (r.json() or {}).get("version") or row["version"]
    except Exception:  # noqa: BLE001
        return row
    try:
        r = requests.get(base + "/api/library/own/status", timeout=2.5,
                         headers={"X-Harmony-Key": key} if key else {})
        if r.ok:
            st = r.json() or {}
            if st.get("enabled"):
                row["tracks"] = int((st.get("stats") or {}).get("tracks") or 0)
    except Exception:  # noqa: BLE001
        pass
    return row


def probe_instances() -> list[dict[str, Any]]:
    """Saved + discovered instances with version and library size (worker)."""
    from harmony.config import Settings

    insts = _engine().instances().get("instances", [])
    if not insts:
        return []
    key = Settings.load().personal_key or None
    with ThreadPoolExecutor(max_workers=min(8, len(insts))) as pool:
        return list(pool.map(lambda i: _probe_one(i, key), insts))
