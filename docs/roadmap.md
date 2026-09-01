# Roadmap & status

A snapshot of what exists and what's next. Honest maturity, not aspiration — see
the legend in the [docs index](README.md). Current release: **0.7.17**.

## Done

- **Engine** — providers (YT Music, Qobuz), cross-service matching, sync
  (plan/apply with conservative data-loss guarantees), import/export,
  enrichment (Last.fm / MusicBrainz / ListenBrainz), AI playlist planner.
- **GTK desktop app** — search, playlists, sync, preferences, Route Audio.
- **Engine/frontend layering fence** — enforced by test + CI.
- **Headless server** (`harmony serve`) — the engine's full featureset over an
  HTTP API; holds credentials centrally (the federation home instance).
- **Web client / PWA** — installable, mobile bottom-nav, network-first service
  worker (self-updates — no manual unregister), search/playlists/sync/accounts,
  Devices tab, browser playback.
- **Android client** — Kotlin/Compose against the engine API: discover, connect,
  search, playlists, sync, cast, "Play here" (HTTP monitor pull), Material 3
  theme. *Audio receiving (bridge) deferred — needs local device testing.*
- **LAN mesh** (mDNS `_harmony._tcp`) — instances discover each other; multi-homed
  peers are reached on their most **direct** address (LAN over Tailscale/CGNAT).
- **Personal-key gate** — optional shared secret; constant-time comparison.
- **Credential custody** — light clients *use* an instance's credentials; full
  instances *copy* them, encrypted with the personal key (in transit and at rest).
- **WiiM/UPnP device control + SSDP discovery** — auto-discovered on the Devices
  tab and in the Now Playing picker; add-by-IP and manual peers for hosts mDNS
  can't reach.
- **Inter-instance audio routing** — ROC/RTP send+receive between instances with
  music/gaming latency presets.
- **Play-to-device relay** — resolves a signed stream and byte-forwards with
  `Range` passthrough.
- **Packaging** — Flatpak (desktop + self-host), `.deb` server (vendored wheels,
  systemd unit).

## Next up (concrete)

- **YT reconnect on the server** — its YouTube cookies are expired; needs a fresh
  connect flow surfaced in the UI.
- **TLS / reverse-proxy exposure guidance** (Caddy/nginx) in the docs.

## Federated playback gaps

- **Cross-LAN device reach** — an instance can't cast to a renderer on a *different*
  LAN; relay through an instance (or the phone) that's on the device's LAN.
- **Phone-as-a-device on desktop** — the Route Audio tab should list other
  instances (incl. the phone) as targets.
- **Multi-room routing** — send to several instances/devices at once.

## Output targets

- **Chromecast / Google TV** (`pychromecast`)
- **Generic DLNA renderers** — extend the UPnP cast path beyond WiiM
- **Bluetooth output** — surface BlueZ/PipeWire sinks in the device picker
- **Android Auto** — `MediaLibraryService` + `MediaSession`

## Music providers (tiered by API trust)

- **Tier 1 (self-host hub):** Subsonic (Navidrome/Gonic/Airsonic), Jellyfin, Plex
- **Tier 2 (yt-dlp):** SoundCloud, Bandcamp
- **Tier 3:** Tidal (`tidalapi`)
- **Tier 4:** Radio Browser
- **Spotify** — sync/metadata only (`spotipy`; no public full-track streaming)

## Distribution

- **Flathub submission** *(on hold — was too early).* Blockers: RTP-only build
  (drop `roc-toolkit` + `--share=network`), tag-pinned manifest, ≥1 screenshot +
  homepage/bugtracker URLs, `appstreamcli` + builder `--lint` in CI, and a
  justification for the browser-profile filesystem grants.
- **Full offline-vendored ROC for Flatpak** — fast-follow so the Flathub build
  isn't RTP-only.
- **RPM + AUR** (fpm spec / PKGBUILD)
- **GHCR container** ("for other people") + compose + publish-on-tag
- **pipx / PyPI** publish
- **Server-as-Flatpak** systemd *user* unit + `enable-linger` docs (Kinoite
  self-host)

## Known loose ends

- **Live-auth verification.** The YT OAuth device flow and the Qobuz WebKit token
  capture want confirmation against real logins; token/header paste is the
  reliable fallback.
- **Android audio receiving** — the "Play on this phone" bridge is unverified;
  needs a real device to test against.
