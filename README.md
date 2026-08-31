# Harmony

A **federated, self-hosted music hub** for Linux. Harmony links your machines
into one mesh that shares a single set of accounts, **routes audio between them
in real time**, casts to the speakers and DACs you already own, and plays
**Qobuz** (hi-res FLAC) and **YouTube Music** at the best quality each allows.
Cross-service **playlist sync** is along for the ride.

It runs as a GTK4/libadwaita desktop app, an installable web app, an Android
client, and a headless server — all thin clients over one GTK-free engine and
its HTTP API.

> **Status:** 0.7.0 — the mesh milestone. Instances discover each other over the
> LAN, share credentials behind a personal key, cast to WiiM/UPnP devices, and
> route audio machine-to-machine. Treat two-way playlist sync against libraries
> you care about as experimental and keep the automatic snapshots on.

> **Unofficial — use at your own risk.** Harmony is not affiliated with,
> endorsed by, or connected to YouTube Music, Qobuz, or any other service it
> talks to. It reaches those services through unofficial, reverse-engineered
> interfaces using **your own account credentials**, which likely violates each
> service's Terms of Service — accessing your own account through an unofficial
> client can get that account limited or terminated. Harmony manages playlists
> and metadata, and can relay a stream you are entitled to from the service to a
> speaker on your own network; **it does not download audio to disk, strip DRM,
> or redistribute anything to third parties**, and it does not circumvent any
> technical protection measure. You are responsible for your use of it. See
> [LICENSE](LICENSE) (GPL-3.0-or-later).

## Why Harmony?

Most Linux music apps are a single-box player for one service. Harmony is a hub
you run across your whole home. What that unlocks:

- **One login, every device.** Sign in to Qobuz and YouTube Music *once*, on the
  machine that holds the hub. Your laptop, phone, and living-room box discover it
  on the network and use those accounts — no re-pasting tokens on each device,
  and **credentials never leave the machine that stores them**. A single
  *personal key* you set on your instances is the gate: only apps that present
  it may use a hub's accounts.
  *Why you'd want it:* stop re-authenticating five devices; keep secrets in one
  place instead of scattered across every phone and laptop.

- **Whole-home audio, no proprietary ecosystem.** Route full audio from one
  instance to another in real time (ROC, with forward error correction over
  Wi-Fi) — start a track at your desk and send it to the living-room speakers,
  or pull a machine's output to the good DAC in another room. A **music-latency
  default** keeps it rock-solid, with an adjuster to **tune the latency down for
  gaming**.
  *Why you'd want it:* Sonos/AirPlay-style multi-room from any Linux box and any
  speakers, with no closed platform and no per-room hardware tax.

- **Cast to the gear you already own.** Discover WiiM/LinkPlay and UPnP
  renderers and send any track to them — play/pause, skip, volume, all from the
  app. (Chromecast/Google TV and generic DLNA are slated next.)
  *Why you'd want it:* your existing streamer, AVR, or smart speaker becomes a
  Harmony endpoint.

- **Audiophile playback from the services you already pay for.** Qobuz hi-res
  FLAC (24-bit) and YouTube's best audio, always negotiated to the highest
  quality a track allows, with bit-perfect local output.
  *Why you'd want it:* one hi-fi player across services instead of a separate app
  for each.

- **Keep libraries in sync across services.** Mirror one way or union two-way
  with fuzzy title/artist/duration matching and exact ISRC matching, a remembered
  match cache, before-state snapshots, and M3U/CSV/JSON/text import-export.
  *Why you'd want it:* move or straddle services without rebuilding playlists by
  hand — a kept feature, no longer the headline.

- **Control it from anything.** The same engine backs a GTK desktop app, an
  installable web app (add-to-home-screen on a phone), and a native Android
  client that discovers your hub and plays from it.

## Clients

| Client | What it is |
|---|---|
| **Desktop** | GTK4/libadwaita app; also runs the always-on API server + mesh node. |
| **Web** | Served by any instance; installable PWA — search, playlists, sync, accounts, cast, and play-in-browser. |
| **Android** | Native app that discovers a hub on the LAN, connects with the personal key, and streams. |
| **Server** | `harmony serve` — headless, GTK-free, the same engine over HTTP for the other clients. |

## Features

- **Federated mesh** — instances discover each other over mDNS and share
  accounts behind a personal key; light clients join without their own logins.
- **Inter-instance audio routing** — low-latency ROC/RTP audio between machines,
  music-default latency with a gaming adjuster.
- **Play to a device** — WiiM/LinkPlay/UPnP renderers, controlled from the app;
  Harmony relays the stream you're entitled to on your LAN (YouTube AAC, Qobuz
  FLAC).
- **Unified search** across services — tracks, albums, artists, playlists.
- **Playlist management** — create, rename, delete, add/remove/reorder on either
  service.
- **Cross-service sync** — one-way mirror or two-way union; fuzzy + ISRC
  matching; ambiguous matches surfaced, never guessed; a match cache for fast,
  stable repeat syncs.
- **Snapshots & portability** — every sync writes a restorable before-state;
  playlists export/import as M3U / CSV / JSON / plain text (`Artist - Title`,
  ISRC appended so it re-resolves exactly).
- **Discovery** — similar artists/tracks blended from Last.fm,
  ListenBrainz/MusicBrainz, and each service's own recommendations.
- **Natural-language playlist building** (optional) — describe a playlist and
  have Claude plan it; every suggestion is resolved against the real catalog, so
  nothing invented reaches your library.

## Requirements

- Linux with GTK 4.12+ and libadwaita 1.4+ (desktop client)
- Python 3.11+ with PyGObject (desktop); the **server** path is GTK-free
- Optional: `ffmpeg` for casting/transcode; `roc-recv`/`roc-send` for ROC audio
  routing (RTP is used as a fallback)

On Fedora / Silverblue-family systems the GTK stack is already present:

```bash
rpm -q python3-gobject gtk4 libadwaita
```

## Install (development)

```bash
git clone https://github.com/marthofdoom/harmony.git && cd harmony
python3 -m venv --system-site-packages .venv   # --system-site-packages gives the venv PyGObject
.venv/bin/python -m pip install -e ".[ai,dev]"
./run.sh
```

`--system-site-packages` matters: PyGObject is a system package with compiled
bindings against your system GTK, and pip-installing it into an isolated venv
usually fails or produces a mismatched build.

### Run as a server

The engine runs headless with no display stack:

```bash
PYTHONPATH=src .venv/bin/python -m harmony serve --port 8080
```

It binds all interfaces by default and has **no authentication out of the box** —
keep it on a trusted network (LAN/Tailnet) or behind an authenticating proxy, and
set a personal key to gate credential sharing. The desktop app runs this same
server automatically, so a desktop install is already a hub.

## Accounts

Connect your accounts in **Preferences → Accounts** (desktop) or the **Accounts**
page (web). The hub holds them on behalf of every client — clients never store
them.

### YouTube Music

- **One-click (easiest).** Harmony detects a signed-in YouTube session from a
  browser on the hub's machine (native or Flatpak) — no pasting. First, sign in
  to music.youtube.com in a browser on that machine, then click **Connect
  YouTube**.
- **Advanced.** Paste request headers from a logged-in music.youtube.com tab, or
  set up a Google *TV and Limited Input* OAuth client for longer-lived auth.

### Qobuz

Qobuz publishes no official API; Harmony talks to the same private API the web
player uses, on a bearer session token. Sign in at play.qobuz.com, open
devtools → Network, click any request to `www.qobuz.com/api.json/0.2/`, and copy
the `X-User-Auth-Token` header into **Preferences → Accounts → Qobuz**. Tokens
expire eventually; paste a fresh one when Harmony reports the saved token is no
longer accepted. A paid subscription is required for playlists worth syncing.

### Guided setup

The standalone [`harmony-setup.py`](scripts/harmony-setup.py) walks you through
both services and seeds credentials where a source or Flatpak install reads them —
no clone required:

```bash
curl -fsSL https://raw.githubusercontent.com/marthofdoom/harmony/main/scripts/harmony-setup.py | python3
```

### Optional integrations

| Integration | Needed for | Key |
|---|---|---|
| Last.fm | similar tracks/artists, tags | free key from last.fm/api |
| MusicBrainz | canonical metadata, ISRC lookup | none (set a contact email) |
| ListenBrainz | open recommendations | none |
| Anthropic | natural-language playlist building | API key |

Secrets go to your system keyring (GNOME Keyring / KWallet). With no keyring
backend, Harmony falls back to a `0600` JSON file under `~/.config/harmony/` and
tells you it did. On a headless host you can also inject any credential via a
`HARMONY_<KEY>` environment variable.

## Documentation

The [`docs/`](docs/README.md) directory is the map — architecture, design
(vision, federation, auth, playback), packaging, roadmap, and decision records.

The short version: providers normalise each service into shared dataclasses,
`matching` resolves tracks across services, `sync` turns two playlists into a
reviewable plan before it writes anything, and clients only ever talk to those
layers through the engine (in-process on desktop, over HTTP everywhere else). The
engine never imports GTK, so it backs the desktop app, the web/Android clients,
and a headless federated instance from one codebase. Module contracts live in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Development

```bash
PYTHONPATH=src .venv/bin/python -m pytest      # tests are offline; no network
PYTHONPATH=src .venv/bin/python -m ruff check src tests
PYTHONPATH=src .venv/bin/python -m harmony     # run from source
```

## A note on the service APIs

Neither YouTube Music nor Qobuz publishes an official public API for this, so
both clients are reverse-engineered from the services' own web players — the
same approach every third-party tool in this space uses (ytmusicapi, yt-dlp,
Music Assistant, streamrip, …). They can break whenever a service changes its
frontend, and neither service endorses this.

Harmony uses these interfaces for **catalog search and playlist/library
management on your own account, authenticated with your own credentials**, and
for an optional **play-to-device** feature that relays a stream you're entitled
to from the service to a renderer on your own network. It does **not** download
audio to disk, strip DRM, or redistribute anything to third parties, and it does
not circumvent any DRM or technical protection measure. See the "unofficial —
use at your own risk" notice at the top of this file.

## Licence

GPL-3.0-or-later.
