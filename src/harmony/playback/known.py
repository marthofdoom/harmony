"""Keeping saved devices in step with discovery (GTK-free, no I/O).

A saved device stores a host, and a DHCP lease can move the device to another
address. Cast devices carry a stable uuid, so a discovered device whose uuid
matches a saved one *is* that device at its new address.
"""

from __future__ import annotations

from typing import Any


def reconcile_known(known: list[dict[str, Any]], discovered: list[dict[str, Any]]) -> bool:
    """Update ``known`` (saved device dicts) in place from ``discovered``
    (dicts with ``host`` and optional ``uuid``): store a missing uuid, follow a
    device to its new host, and drop saved duplicates that end up at one host.
    Returns True when anything changed (the caller persists)."""
    by_uuid: dict[str, dict[str, Any]] = {}
    by_host: dict[str, dict[str, Any]] = {}
    for d in discovered:
        if d.get("uuid") and d.get("host"):
            by_uuid.setdefault(d["uuid"], d)  # first sighting wins
            by_host.setdefault(d["host"], d)
    changed = False
    for entry in known:
        uid = entry.get("uuid")
        if not uid:
            match = by_host.get(entry.get("host"))
            if match:
                entry["uuid"] = match["uuid"]
                changed = True
            continue
        match = by_uuid.get(uid)
        if match and match["host"] != entry.get("host"):
            entry["host"] = match["host"]
            changed = True
    seen: set[Any] = set()
    kept = []
    for entry in known:
        if entry.get("host") in seen:
            changed = True
            continue
        seen.add(entry.get("host"))
        kept.append(entry)
    if len(kept) != len(known):
        known[:] = kept
    return changed
