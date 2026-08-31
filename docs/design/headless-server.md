# Harmony server: engine HTTP API + web client

**Status: Planned (current top priority).** A headless server whose primary job
is to be the **credential-holding backend that the mobile app (and other
clients) authenticate to** — the [federation](federation.md) "home instance." It
exposes Harmony's **full current featureset over an HTTP API**, and ships a
**web client** on that same API so the server is fully usable from a browser,
audio included. Distributed as a container (GHCR), an Arch AUR package, and a
`.deb` — not Flatpak, which stays the desktop path.

Supersedes the earlier Broadway idea (serving the GTK GUI to a browser): Broadway
can't carry audio and can't serve the mobile app, so it's dropped.

## Principles

- **The current featureset is the base, required for every client.** Search,
  playlists (browse + edit), cross-service matching, sync, discover/recommend,
  enrichment, play/queue, device control — the API must cover all of it so no
  client (web, mobile, desktop) is second-class. The API surface *is* the engine
  interface ([ADR 0001](../decisions/0001-engine-frontend-separation.md)).
- **Credential custody is the point.** Only the server stores Qobuz/YT tokens.
  Clients authenticate *to the server* and never hold streaming credentials —
  the [federation](federation.md) model ([ADR 0004](../decisions/0004-federation-for-credential-custody.md)).
- **Clients own playback.** Each client plays the resolved stream itself (phone
  audio, browser `<audio>`), so the server stays a stateless-ish facade and we
  **avoid extracting the playback orchestration out of `AppState`** — the one
  genuinely hard refactor. Queue/shuffle/repeat/position live client-side.

## Architecture

```
   web client (browser)  ─┐
   mobile app            ─┼─ HTTP/WS API ─▶  Harmony server
   (future) desktop      ─┘   (auth'd)        ├─ engine: providers, matching,
                                              │   sync, discover, enrich, db
                                              ├─ relay → stream bytes (Range)
                                              └─ credentials (server-only)
                                                   │
                                            Qobuz / YouTube Music
```

- The API is a **thin HTTP facade over the already-GTK-free engine modules**
  (`providers`, `sync`, `matching`, `db`, `enrich`, `recommender`, `playback`).
  Mostly mechanical wrapping, not a rewrite.
- **Audio:** the relay already emits browser-/client-playable streams (Qobuz
  FLAC, YouTube AAC/Opus). The server proxies those bytes **same-origin and
  Range-aware** (the relay already supports Range), so a client points its audio
  element / player at `GET /stream/<token>` and plays. No transcoding for the
  common cases.
- **Casting to LAN devices** (WiiM/UPnP) stays a *server-side* operation via the
  existing relay + `playback/` — exposed as an API action. The multi-track cast
  queue (today in `AppState`) is a later slice; single-track cast is trivial.

## Stack

Chosen for simple distribution (AUR/.deb/container) and local testability:

- **Server:** Python **stdlib threaded HTTP** for the first cut — zero heavy
  deps, fully unit-testable headless, trivial to package. (Deliberately *not*
  FastAPI/pydantic: it adds compiled deps that complicate packaging and can't
  even be built on some dev boxes.) Swap in an ASGI framework later only if scale
  demands it; the handlers stay small.
- **Frontend:** **build-free** vanilla JS + CSS (no npm/webpack), so the Python
  package just ships static assets. Styled to echo the Adwaita look.
- **Audio:** HTML5 `<audio>` ← the same-origin stream proxy.
- **Auth: off by default, opt-in; reachable by default.** No login screen out of
  the box, and the server binds all interfaces (`0.0.0.0`) by default — a home
  hub should Just Work on the LAN/Tailnet without config. The **network is the
  security boundary**: keep it on a trusted network or behind an authenticating
  proxy (a startup line says so; it does not block). Enabling auth (a configured
  password/token) turns on the web login and gates every API call and stream
  fetch on a client token (the [federation](federation.md) instance↔client auth;
  a mobile client presents the same token). `--address 127.0.0.1` restricts to
  localhost for anyone who wants it.

## API surface (the full featureset)

Grouped; all under `/api`, token-authenticated. Shapes mirror `harmony.models`.

- **auth** — `POST /auth/login` (→ client token), logout, token check.
- **accounts** — provider status (authed? account name), and the headless login
  flows (Qobuz token paste / YT header/OAuth) so the *server* can be seeded.
- **search** — `GET /search?q=&kinds=` → tracks/albums/artists/playlists.
- **catalog** — track/album/artist detail, album tracks, artist top/albums.
- **playlists** — list, get, tracks, create, add, remove, rename, delete.
- **matching** — find-on-other-service for a track.
- **sync** — plan (preview) and apply (mirror/clone), with progress over WS/SSE.
- **discover / recommend / enrich** — similar tracks/artists, recommendations.
- **stream** — `POST /resolve` (track → playable token + format) and
  `GET /stream/<token>` (Range-aware byte proxy of the relay).
- **devices** — list known devices, control (transport/volume), cast a
  track/collection to a device.
- **now-playing** — client-owned; the server exposes only device-cast status.

## Slices (each shippable; all permanent, all consumed by mobile too)

1. **Listen in the browser, end to end.** API: auth, search, `resolve`+`stream`,
   list/get playlist. Web client: log in → search → browse a playlist → **play a
   track in the browser** with a real transport bar (play/pause/seek/next,
   client-side queue). Proves the API + credential custody + browser audio.
2. **Manage, sync, cast.** Playlist editing, find-on-other, sync plan/apply with
   progress, cast-to-device; client-side shuffle/repeat/queue.
3. **Parity.** Discover, AI planner, accounts/prefs management, polish.

The **mobile app** is a separate client built against this same API (its own
effort), in parallel with or after slice 1 — the server is the shared backend.

## Distribution

Full runtime deps in every package (the engine's: ytmusicapi, requests,
rapidfuzz, yt-dlp, ffmpeg for casting; **no GTK** — the server is headless).

1. **GHCR container (primary)** — `python:3.x-slim` + `ffmpeg` + the engine;
   runs `harmony serve` (the web+API server). `/data` volume (config, sqlite,
   encrypted secrets), one port, secrets via env/secret. Published on tag;
   Podman/rootless; `compose.yaml`.
2. **Arch AUR** — `PKGBUILD` (`harmony` / `harmony-git`) with full `depends` +
   a `harmony.service` user unit.
3. **Debian `.deb`** — built in CI with full `Depends:`; published as a release
   asset.
4. **pipx** — convenience for unpackaged distros.

`harmony serve` = the web+API server (`--port/--address/--data-dir`, TLS opts).

## Credentials & security

- **Server-side secrets:** encrypt the keyring-less fallback store with a key
  from `HARMONY_SECRET_KEY` (passphrase/keyfile/systemd-cred/Docker-secret);
  allow direct env token injection (`HARMONY_QOBUZ_TOKEN`, …). Headless seeding
  via the accounts API / a `harmony login` CLI.
- **Exposure:** the server holds real credentials and controls accounts. Auth is
  off by default and it binds all interfaces by default (see Stack), so the
  network is the security boundary — keep it on a trusted network (LAN/Tailnet)
  or front it with TLS + auth (Caddy/nginx). A startup line notes the no-auth
  state; it does not block. When auth is enabled, rate-limit login.
  `redact_secrets` on all log paths.

## What we explicitly avoid (and why it stays small)

- **No `AppState`/EngineCore extraction for client playback** — clients own
  playback, so the hard orchestration refactor isn't on the critical path. (It
  returns only if/when the *server itself* needs a rich multi-track cast queue.)
- **No GTK in the server** — enforced by the existing layering fence; the API
  imports only engine modules.
- **No frontend build toolchain** — static assets ship in the package.

## Testing / CI

- API handlers are gi-free and stdlib → unit-testable in the existing offline
  (no-GTK) job; the layering fence covers the new `harmony/web` (or `harmony/api`)
  package.
- Container smoke test (`harmony serve` boots, `/healthz` + an unauthenticated
  API call respond) on tag; `PKGBUILD`/`.deb` lint.

## Open questions

- **Client↔server auth model (when enabled)** — accounts (single-user vs multi),
  token issuance/revocation, pairing/consent for a phone. Start single-user (one
  optional login token) and grow. Auth is off by default. See
  [federation](federation.md).
- **Server-side cast queue** — reuse a small extracted queue helper when slice 2
  adds multi-track casting from the API.
- **Discovery** — manual URL first; mDNS for the phone to find a home instance later.
