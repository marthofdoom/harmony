# Product vision

**Status: Idea / direction.** Recorded so current decisions don't foreclose it.

Harmony aims to be a **Music-Assistant-class** music hub, done well: aggregate
multiple streaming providers into one library, manage and sync playlists across
them, discover music, and play to devices — but as first-class **standalone
apps**, not a Home Assistant add-on.

## Shape

- **Multiple clients.** GTK desktop (current), Android (planned), hosted web
  (planned). Each is a real app, not a thin shell.
- **Standalone by default.** An instance works fully on its own, no server
  required.
- **Federated when you want it.** Instances can be pointed at one another; a
  client authenticates *to a trusted instance* and uses it as a backend. See
  [federation](federation.md).

## What "done well" means here

Music Assistant is powerful but is a HASS add-on with the UX that implies.
Harmony's differentiators:

- Native, standalone apps per platform rather than an embedded web panel.
- Cross-service **sync** as a first-class feature (mirror / two-way), with a
  reviewable plan and conservative data-loss guarantees — not just aggregated
  browsing.
- Federation as the credential-custody and multi-device story (below), rather
  than every client holding every credential.

## In scope over time

- Providers beyond YT Music and Qobuz.
- [Play-to-device](playback.md): WiiM/LinkPlay, UPnP/DLNA, AirPlay, Chromecast —
  push a stream to a renderer rather than decoding audio in-app.
- Discovery/recommendation blending (already present in the engine).

## Explicitly not committed

Nothing above is built. This document exists to keep the engine's
[API boundary](../architecture/layering.md) clean enough that these become
additions rather than rewrites.
