# Roadmap & status

A snapshot of what exists and the intended sequence. Honest maturity, not
aspiration — see the legend in the [docs index](README.md).

## Done

- **Engine** — providers (YT Music, Qobuz), cross-service matching, sync
  (plan/apply with conservative data-loss guarantees), import/export,
  enrichment (Last.fm / MusicBrainz / ListenBrainz), AI playlist planner.
  *Built & verified* (offline test suite).
- **GTK desktop app** — search, playlists, sync, discover, preferences.
  *Built & verified* (runs; window/flows exercised).
- **Engine/frontend layering fence** — enforced by test + CI. *Built & verified.*
- **Qobuz token auth + password auth.** *Built & verified* (token);
  password works only for non-social accounts.
- **Flatpak packaging.** *Built & verified* (builds, installs, launches; WebKit
  and secrets portal confirmed in-sandbox).

## In progress

- **WiiM/LinkPlay playback backend** (`harmony/playback/`). *Built & verified*
  (offline test suite) — device control (status/play/pause/volume/mute/skip)
  and best-effort SSDP discovery, no UI wiring yet. See
  [playback](design/playback.md).
- **Embedded WebKit Qobuz login.** *Built, token capture unverified* — needs one
  real interactive login to confirm the `localStorage` extraction; token paste
  is the reliable method until then. See [auth](design/auth.md#qobuz).

## Planned (sequence)

1. **Confirm/repair the WebKit token capture** against a live login; add
   cookie/IndexedDB/network-header fallbacks if `localStorage` scanning misses.
2. **Engine HTTP API** — expose the engine interface over HTTP so a client can
   consume a remote instance identically to a local one. Precondition for
   everything below. See [layering](architecture/layering.md).
3. **Web client** — hosted frontend against the engine API. (Qobuz login there
   is a browser extension or token paste, not embedded WebKit — see
   [auth](design/auth.md).)
4. **Android client** — native app against the engine API.
5. **[Federation](design/federation.md)** — instance-to-client auth so a client
   can point at a trusted instance; the credential-custody answer.
6. **[Play-to-device](design/playback.md)** — WiiM/LinkPlay + UPnP; push streams
   to renderers rather than decoding in-app.

## Known loose ends

- This session's later commits (artist navigation, Preferences reachability,
  WebKit login, Flatpak) have **not been diff-reviewed** yet; every prior batch
  this session surfaced a seam defect on review.
- `Settings.auto_accept_high` and match thresholds are now wired; re-confirm
  against live accounts (a mirror sync of a real 50+ track playlist — count the
  removals in the preview before applying).
