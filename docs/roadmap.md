# Roadmap & status

A snapshot of what exists and the intended sequence. Honest maturity, not
aspiration — see the legend in the [docs index](README.md). Current release:
**0.5.0**.

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
- **WiiM/LinkPlay device control** (`harmony/playback/` + Devices page).
  *Built & verified* (offline test suite) — add-by-IP, best-effort SSDP
  discovery, and transport/volume/mute/now-playing from the UI. Controls what a
  device is already playing; pushing a library track *to* a device is not yet
  wired (that's play-to-device, below). See [playback](design/playback.md).
- **Account-setup bootstrapper** (`scripts/harmony-setup.py`). *Built & verified*
  (offline test suite) — standalone interactive tool that obtains YT Music and
  Qobuz credentials (browser-cookie extraction, OAuth device flow, token paste)
  and seeds them into a source or Flatpak install.
- **Play-to-device (passive relay)** — `MusicProvider.resolve_stream` (Qobuz
  signed `getFileUrl` → FLAC; YouTube via yt-dlp → AAC itag 140), `RelayServer`
  (`playback/relay.py`) byte-forwarding with `Range` passthrough, and a **Play
  on Device** action on search results. *Built & unit-tested; end-to-end against
  a real device pending.* See [playback](design/playback.md).

## In progress

- **Embedded WebKit Qobuz login.** *Built, token capture unverified* — needs one
  real interactive login to confirm the `localStorage` extraction; token paste
  is the reliable method until then. See [auth](design/auth.md#qobuz).

## Planned (sequence)

0. **[Headless server + browser GUI](design/headless-server.md)** — *current top
   priority.* Run Harmony on a server, holding the credentials, operated from a
   browser by serving the existing GTK GUI via GTK's Broadway backend (no
   rewrite). Distributed as a GHCR container, an Arch AUR package, and a `.deb`
   (not Flatpak, which stays the desktop path). Delivers the credential-holding
   [federation](design/federation.md) instance first; the Engine HTTP API below
   moves to its phase 2 (needed only for native/multi-user clients).
1. **Confirm/repair the WebKit token capture** against a live login; add
   cookie/IndexedDB/network-header fallbacks if `localStorage` scanning misses.
2. **Engine HTTP API** — expose the engine interface over HTTP so a client can
   consume a remote instance identically to a local one. Precondition for the
   native/multi-user clients below (phase 2 of the headless server).
   See [layering](architecture/layering.md).
3. **Web client** — hosted frontend against the engine API. (Qobuz login there
   is a browser extension or token paste, not embedded WebKit — see
   [auth](design/auth.md).)
4. **Android client** — native app against the engine API.
5. **[Federation](design/federation.md)** — instance-to-client auth so a client
   can point at a trusted instance; the credential-custody answer.
6. **Play-to-device — remaining work.** The relay and a search-page **Play on
   Device** action shipped (see Done); what's left: verify end-to-end against a
   real WiiM, extend the action to playlists/library, add per-device codec
   negotiation, and reuse the relay as a **federation primitive** (a spoke pulls
   playback from the credential-holding instance). See [playback](design/playback.md).

## Known loose ends

- **Live-auth verification.** The YT OAuth "Sign in with Google" device flow and
  the Qobuz WebKit token capture have not been confirmed against real logins;
  token/header paste remains the reliable fallback.
- **Sync against live accounts.** `Settings.auto_accept_high` and match
  thresholds are wired but want a real-world pass — mirror a 50+ track playlist
  and count the removals in the preview before applying.
- **Device control against real hardware.** The WiiM backend is covered by the
  offline suite but should be exercised against a physical Mini (discovery,
  transport, and a direct `play_url` with a real stream URL).
