"""Keeping saved devices in step with discovery (GTK-free, no I/O).

A saved device stores a host, and a DHCP lease can move the device to another
address. Cast devices carry a stable uuid, so a discovered device whose uuid
matches a saved one *is* that device at its new address.
"""

from __future__ import annotations

from typing import Any


def _kind(d: dict[str, Any]) -> str:
    return d.get("kind") or "wiim"


def _dkind(d: dict[str, Any]) -> str:
    """A discovered device's kind; only Cast devices carry a uuid, so a bare one is a cast."""
    return d.get("kind") or "cast"


def reconcile_known(known: list[dict[str, Any]], discovered: list[dict[str, Any]]) -> bool:
    """Update ``known`` (saved device dicts) in place from ``discovered``
    (dicts with ``host``, ``kind`` and optional ``uuid``): store a missing uuid,
    follow a device to its new host, and drop saved entries that are the same
    device (same uuid). Only devices of the same kind are ever matched, and a
    host shared by two different devices is never "deduped" away.
    Returns True when anything changed (the caller persists)."""
    by_uuid: dict[str, dict[str, Any]] = {}
    by_host: dict[tuple[str, str], dict[str, Any]] = {}
    for d in discovered:
        if d.get("uuid") and d.get("host"):
            by_uuid.setdefault(d["uuid"], d)  # first sighting wins
            by_host.setdefault((_dkind(d), d["host"]), d)
    changed = False
    # 1. a saved device without a uuid learns it from the same-kind device found at its host
    for entry in known:
        if not entry.get("uuid"):
            match = by_host.get((_kind(entry), entry.get("host")))
            if match:
                entry["uuid"] = match["uuid"]
                changed = True
    # 2. the same device saved twice: keep the entry discovery confirmed
    #    (its host is where the device was just seen), else the first
    kept: list[dict[str, Any]] = []
    winner: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in known:
        uid = entry.get("uuid")
        if not uid:
            kept.append(entry)
            continue
        key = (_kind(entry), uid)
        prev = winner.get(key)
        if prev is None:
            winner[key] = entry
            kept.append(entry)
            continue
        seen_at = by_uuid.get(uid, {}).get("host")
        if entry.get("host") == seen_at and prev.get("host") != seen_at:
            kept[kept.index(prev)] = entry
            winner[key] = entry
        changed = True
    # 3. follow a device to its new host
    for entry in kept:
        uid = entry.get("uuid")
        match = by_uuid.get(uid) if uid else None
        if match and _dkind(match) == _kind(entry) and match["host"] != entry.get("host"):
            entry["host"] = match["host"]
            changed = True
    if len(kept) != len(known):
        known[:] = kept
    return changed
