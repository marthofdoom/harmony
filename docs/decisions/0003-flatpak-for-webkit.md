# 0003 — Flatpak to bundle WebKit without a user dependency

**Status:** Accepted.

## Context

The embedded [Qobuz login](../design/auth.md#qobuz) needs a browser engine.
WebKitGTK is the GTK-native choice, but `webkitgtk6.0` isn't installed on the
target (atomic Fedora), and the owner explicitly did not want users to install a
dependency separately. WebKitGTK also can't practically be vendored into an
rpm/deb — it's a large C++ library with a deep dependency tree.

## Decision

Package Harmony as a **Flatpak** against `org.gnome.Platform//49`. The GNOME
runtime already ships `WebKit-6.0.typelib` and `libwebkitgtk-6.0` (verified in
the installed runtime), so WebKit comes for free — the user installs one thing,
the Flatpak, and the embedded login just works. WebKit is soft-imported so a
bare source checkout still runs (login hidden, token paste available).

Python dependencies are pinned as prebuilt manylinux/abi3 wheels
(`swap-sdists-to-wheels.py` rewrites any sdist), so the offline flatpak-builder
build compiles nothing — no C++, no Rust, no network.

## Consequences

- "Bundle it, no user dep" is delivered — but it *is* the packaging effort, done
  now. The Flatpak becomes the primary distribution target.
- The sandbox stays tight: network + GPU + secrets portal only; import/export
  goes through the file-chooser portal, so no host filesystem access.
- The same runtime gives future native libraries (e.g. playback) a home without
  new user dependencies.
- A hosted web frontend gets no equivalent — it can't embed a login that reads
  `play.qobuz.com` storage (same-origin policy). That asymmetry is part of why
  [federation](../design/federation.md) (client → trusted desktop instance)
  looks better than hosted-holds-everything.
