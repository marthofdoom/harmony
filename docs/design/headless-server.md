# Headless server + browser GUI

**Status: Planned.** Scope for running Harmony as a headless service on a server,
operated through its **existing GTK GUI served to a browser**, with credentials
held centrally. This is the reprioritized top of the [roadmap](../roadmap.md):
the credential-holding instance from [federation](federation.md) built first,
with a browser-reachable frontend, ahead of further desktop-only work.

## Goal

Run on a server with no attached display, **hold the Qobuz/YT credentials**, do
all provider/sync/playback work, cast to LAN devices — and be operated from any
browser by **mirroring the Flatpak's GUI** (not a separate frontend). It doubles
as the [federation](federation.md) "home instance" once the native remote clients
arrive (phase 2).

## The key decision: serve the real GUI, don't rebuild it

GTK ships an **HTML5 backend, Broadway** (`broadwayd` + `GDK_BACKEND=broadway`).
It renders any GTK4/libadwaita app in a browser over a websocket, with **no X or
Wayland** and **no application changes** — the app connects to `broadwayd` as its
display, the browser connects to `broadwayd`'s port. So the browser frontend *is*
the Flatpak GUI, literally.

This makes phase 1 **light on build effort** (no TUI, no web rewrite, no engine
extraction) and only moderately heavier at runtime (the image carries the GTK
stack). It's the direct answer to "mirror the Flatpak GUI, and it shouldn't be a
heavy build."

Frontend options, in order:

1. **GTK Broadway (recommended).** The existing GUI, unchanged, served to the
   browser. Lightest possible dev effort.
   - *Caveats to design around:* Broadway is **single-session** (multiple
     browsers share one mirrored session — fine for a solo home hub, not
     multi-user); it has **no built-in authentication**; and it's smooth on
     LAN/Tailscale but sluggish over high-latency links. WebKitGTK (the embedded
     Qobuz login) may render poorly under Broadway — keep token paste as the
     reliable path.
2. **Headless compositor + VNC/noVNC (fallback).** Run the GUI under a headless
   Wayland compositor (`cage`/`weston --headless`) or `Xvfb`, expose via
   `wayvnc`/`x11vnc`, and serve `noVNC` in the browser. Heavier (a VNC stack) but
   robust and multi-session-friendly; still the real GUI. Use if Broadway's
   limits bite.
3. **TUI (last resort).** A Textual TUI against the engine, served with
   `textual serve`. Only if serving the GUI proves genuinely too heavy — Broadway
   means it shouldn't, so this drops to a contingency, not the plan.

## How it slots into what exists

- [ADR 0001](../decisions/0001-engine-frontend-separation.md) keeps the engine
  GTK-free, so providers/sync/playback already run headless. Broadway means we
  **don't need to touch that seam for phase 1** — the whole GTK app (engine +
  `AppState` + UI) runs as-is under `broadwayd`.
- Credentials already degrade without a keyring: `CredentialStore` falls back to
  a `0600` JSON file (see [auth](auth.md)). That's the seam the server hardens.
- The EngineCore extraction + HTTP API (roadmap item 2, and the precondition for
  *native* remote clients and multi-user) moves to **phase 2** — no longer on the
  critical path for "run it on a server, operate from a browser."

## Architecture (phase 1)

```
   browser ── https (proxy/Tailscale) ──▶ broadwayd  ── connects ──▶  Harmony GTK app
                                          (HTML5 GTK        (unchanged: engine +
                                           display)          AppState + UI, creds,
                                                             relay → WiiM/UPnP)
```

`harmony serve` boots `broadwayd` and launches the existing GTK app against it
(`GDK_BACKEND=broadway`), plus the auth front (below). No new UI code.

## Credentials on a server

No Secret Service on a headless box, so the file fallback is the default — but a
plaintext `0600` file is too weak for a server holding paid-service tokens:

- **Encrypted secrets backend.** Encrypt the fallback store with a key from
  `HARMONY_SECRET_KEY` (raw key or scrypt-derived passphrase), a mounted key
  file, a `systemd` credential, or a Docker/Podman secret. Add it behind
  `CredentialStore`'s existing `get/set/delete` so no caller changes.
- **Direct env injection** for container use — `HARMONY_QOBUZ_TOKEN`,
  `HARMONY_YTMUSIC_HEADERS` — read at startup so a token can come from
  orchestration and never touch disk.
- **Onboarding in the served GUI.** Because the real GUI is what's in the
  browser, existing Preferences → Accounts (token paste, YT OAuth/header paste)
  works as-is. Embedded WebKit login is best-effort under Broadway; token paste
  stays the reliable route. A `harmony login` CLI (reusing
  `scripts/harmony-setup.py`) covers pre-seeding before first boot.

## Playback on a headless box

- Usually **no audio sink**, so "This computer" local playback is off by default
  (enabled only if a PipeWire/ALSA sink exists).
- The real path is **casting to LAN devices** via the existing relay
  (`playback/relay.py`) — headless-friendly, needs `ffmpeg` (YouTube stream-copy)
  and LAN reachability to the devices.
- **Route-audio (ROC)** stays optional (heavy native build + needs a sink):
  a separate image variant / package extra, not the base.

## Auth & exposure (security)

The browser GUI grants full control of the user's accounts, and **Broadway has no
auth of its own**, so exposure must be gated externally:

- **Front it** with a reverse proxy doing auth + TLS (Caddy/nginx: basic-auth or
  forward-auth, automatic HTTPS), or run it **only over Tailscale** (bind to the
  tailnet interface — the expected home-hub deployment, which the user already
  runs). Never bind `broadwayd` to a public interface directly.
- Default bind is localhost; `harmony serve --bind` to choose the interface.
- `redact_secrets` already exists — keep it on all log paths.

## Install & distribution

Flatpak stays the **desktop** story. Server/native distribution, all **pulling
full runtime dependencies** (GTK4, libadwaita, PyGObject, gdk broadway backend,
ffmpeg, yt-dlp, requests/rapidfuzz/ytmusicapi/keyring):

1. **OCI container → GHCR (primary).** Multi-stage image (Fedora/Debian base with
   the GTK4 + broadway + libadwaita stack) running `broadwayd` + `harmony serve`
   + relay deps. One `/data` volume (config, sqlite, encrypted secrets), one port,
   secrets via env/secret. Published to `ghcr.io/marthofdoom/harmony` from CI on
   tag; Podman/rootless-friendly; ship a `compose.yaml`:
   ```
   docker run -d --name harmony \
     -p 8085:8085 -v harmony-data:/data \
     -e HARMONY_SECRET_KEY=... \
     ghcr.io/marthofdoom/harmony:latest
   ```
   A `:route-audio` variant adds the ROC build.
2. **Arch AUR.** A `PKGBUILD` (`harmony` from the release tarball, and/or
   `harmony-git`) with full `depends=(python gtk4 libadwaita python-gobject
   python-ytmusicapi python-requests python-rapidfuzz python-keyring
   python-platformdirs yt-dlp ffmpeg ...)`, installing the console scripts and a
   `harmony.service` user unit. Standard native install for Arch servers/desktops.
3. **Debian `.deb`.** Built in CI (`dpkg-buildpackage` or `fpm`) with proper
   `Depends:` (`python3, python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1,
   gir1.2-gdkpixbuf-2.0, ffmpeg, yt-dlp, python3-requests, python3-rapidfuzz,
   python3-keyring, python3-platformdirs, ...`) so `apt install ./harmony.deb`
   pulls everything. Ships the systemd unit + desktop file. Publish the `.deb` as
   a release asset (and optionally an apt repo later).
4. **pipx (convenience).** `pipx install "harmony[server]"` for non-packaged
   distros; requires the system GTK4/ffmpeg present. Documented, not primary.

Packaging work: `harmony serve`/`harmony login` console scripts; a `server`
extra; `Dockerfile` + `compose.yaml`; `PKGBUILD`(s); Debian packaging
(`debian/` or an `fpm` recipe); the systemd unit; CI jobs to build & publish the
container (GHCR) and the `.deb` (release asset) on tag. (AUR PKGBUILDs live in the
AUR, updated per release; CI can lint them.)

## Config

File + env (no separate headless config UI needed — the GUI is the config UI).
`Settings` is JSON-backed; add `HARMONY_*` env overrides and a documented `/data`
layout so a container mount is self-describing. `harmony serve --port/--bind/--data-dir`.

## Phasing

- **Phase 1 — served GUI + native packaging.** `harmony serve` (broadwayd + the
  existing GTK app); encrypted secrets + env injection; auth/exposure defaults +
  Tailscale/reverse-proxy docs; GHCR image, AUR PKGBUILD, `.deb`. Delivers the
  whole "run on a server, holds the creds, operate from a browser (mirroring the
  Flatpak GUI)" goal with **no new frontend and no engine surgery.**
- **Phase 2 — engine API + native/multi-user clients.** Extract EngineCore from
  `AppState` (gi-free orchestrator) and put an HTTP/WebSocket surface in front
  (roadmap item 2). Then the GTK desktop and a future phone client point at the
  same server — the [federation](federation.md) model — and multi-user / true
  web-client become possible beyond Broadway's single-session limit. Instance↔
  client auth is the open work ([ADR 0004](../decisions/0004-federation-for-credential-custody.md)).

## Testing / CI

- The engine already tests headless (offline job). No new frontend to test in
  phase 1.
- Add a **container smoke test**: boot the image, confirm `broadwayd` serves and
  the GTK app connects (health-check the Broadway HTTP port), on tag.
- Lint the `PKGBUILD` (`namcap`) and validate the `.deb` (`lintian`) in CI;
  publish both on tag.

## Risks & open questions

- **Broadway single-session + no auth** — fine for a solo home hub behind
  Tailscale/a proxy; multi-user needs phase 2. Get the exposure defaults right and
  document them loudly before anyone points it at the internet.
- **Broadway performance** over high-latency links is mediocre; acceptable on
  LAN/tailnet. If it's a dealbreaker, the VNC/noVNC fallback is the next rung.
- **Image size** — carrying GTK4 + libadwaita (+ optional WebKitGTK) is a few
  hundred MB. Heavier than a TUI image, still a normal app image; keep WebKit and
  ROC to optional variants.
- **WebKit embedded login under Broadway** may be janky — token paste is the
  documented fallback for the server.
- **Open (phase 2):** EngineCore extraction, instance↔client auth, multi-user —
  tracked in [federation](federation.md) / ADR 0004.

## Deliverables checklist (phase 1)

- [ ] `harmony serve` — launches `broadwayd` + the GTK app (`GDK_BACKEND=broadway`)
- [ ] `harmony login` — pre-seed credentials headlessly (reuses `harmony-setup.py`)
- [ ] encrypted secrets backend + env-injection + `HARMONY_SECRET_KEY`
- [ ] auth/exposure: bind defaults, reverse-proxy + Tailscale deployment docs
- [ ] gate "This computer" local playback when no audio sink is present
- [ ] `Dockerfile` (+ `:route-audio` variant) + `compose.yaml` + GHCR publish CI
- [ ] Arch `PKGBUILD` (`harmony` / `harmony-git`) with full deps + systemd unit
- [ ] Debian packaging + `.deb` built and published as a release asset (full `Depends:`)
- [ ] container smoke test + PKGBUILD/`.deb` lint in CI
