# Harmony

A GTK4/libadwaita desktop app for Linux that creates and syncs playlists across
**YouTube Music** and **Qobuz**, with unified search, cross-service track
matching, and recommendation tooling.

> **Status:** 0.5.0 — first tagged release. The sync engine, matching, search,
> discovery, and the desktop UI are functional, and WiiM/LinkPlay devices can be
> controlled from the app, and you can play a track to one. Treat two-way sync
> against playlists you care about as experimental and keep the automatic
> snapshots turned on.

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

## Features

- **Unified search** across both services — tracks, albums, artists, playlists.
- **Playlist management** — create, rename, delete, reorder membership, remove
  tracks, on either service.
- **Cross-service sync** — mirror one way or union two-way, with fuzzy matching
  on title/artist/duration and exact matching on ISRC where available.
  Ambiguous matches are surfaced for manual resolution instead of guessed at.
- **Match cache** — once you resolve a track across services, the link is
  remembered, so repeat syncs are fast and stable.
- **Snapshots** — every sync writes a before-state snapshot you can inspect or
  restore, and playlists export to and import from M3U / CSV / JSON, or a plain
  human-readable text list (`Artist - Title` per line, with the ISRC appended
  where known so it re-resolves exactly). The format is chosen by file extension.
- **Discovery** — similar artists and tracks blended from Last.fm,
  ListenBrainz/MusicBrainz, and each service's own recommendations.
- **Natural-language playlist building** (optional) — describe a playlist and
  have Claude plan it; every suggestion is then resolved against the real
  catalog, so nothing invented ever ends up in your library.
- **Play to a device** — discover WiiM/LinkPlay renderers on your network,
  control them (play/pause, skip, stop, volume, mute) from the **Devices** page,
  and send any search result straight to one with **Play on Device**. Harmony
  relays the stream on your LAN, so the speaker plays your own entitled audio
  (YouTube via an AAC stream, Qobuz via FLAC).
- **Guided account setup** — a standalone [`harmony-setup.py`](scripts/harmony-setup.py)
  that interactively obtains your YouTube Music and Qobuz credentials and seeds
  them where a source or Flatpak install reads them (see below).

## Requirements

- Linux with GTK 4.12+ and libadwaita 1.4+
- Python 3.11+ with PyGObject

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

Then connect your accounts — the quickest way needs no clone at all, just one
line:

```bash
curl -fsSL https://raw.githubusercontent.com/marthofdoom/harmony/main/scripts/harmony-setup.py | python3
```

See [Accounts](#accounts) below for what it does and the alternatives.

## Accounts

**Easiest path — the setup tool.** Rather than pasting headers by hand, run the
standalone bootstrapper, which walks you through both services and writes the
credentials where your install expects them. It needs no clone — run it straight
from GitHub:

```bash
# with uv (manages the script's dependencies automatically):
uv run https://raw.githubusercontent.com/marthofdoom/harmony/main/scripts/harmony-setup.py

# or with plain python (offers to build a throwaway venv if deps are missing):
curl -fsSL https://raw.githubusercontent.com/marthofdoom/harmony/main/scripts/harmony-setup.py | python3
```

From a clone, `uv run scripts/harmony-setup.py --target source` does the same and
lets you pick the target install (`source` / `flatpak` / `auto`).

It has no dependency on the app (constants are inlined), imports its heavier
dependencies lazily, and offers a throwaway virtualenv if any are missing. The
manual per-service steps below still work and are what the tool automates.

### YouTube Music

Harmony uses [`ytmusicapi`](https://ytmusicapi.readthedocs.io/). Two options,
both configured in **Preferences → Accounts**:

- **Browser headers (easiest).** Open music.youtube.com while logged in, open
  devtools → Network, find a `POST /youtubei/v1/browse` request, copy the
  request headers, and paste them into the "Paste browser headers" dialog.
  Harmony writes them to `browser.json`. These credentials expire after a few
  months and need re-pasting.
- **OAuth.** Create a *TV and Limited Input device* OAuth client in a Google
  Cloud project with the YouTube Data API enabled, then enter the client ID and
  secret. Longer-lived, more setup.

### Qobuz

Qobuz publishes no official API; Harmony talks to the same private API the web
player uses. Everything past sign-in runs on a bearer session token, so
Preferences → Accounts → Qobuz offers two ways to obtain one:

- **Email and password (default).** Enter your account email and password —
  the password is hashed before it leaves the machine and only the resulting
  session token is stored.
- **Paste session token.** Required if your Qobuz account was created through
  Google or another social sign-in: those accounts have no password for
  Harmony to hash, so the password method can't authenticate them. Sign in at
  play.qobuz.com, open devtools → Network, click any request to
  `www.qobuz.com/api.json/0.2/`, and copy the `X-User-Auth-Token` request
  header into the token field. Qobuz's session tokens do eventually expire;
  when Harmony reports the saved token is no longer accepted, repeat these
  steps and paste a fresh one.

The app ID is scraped once from the public web player bundle; if that ever
breaks you can paste an app ID manually — it's the `X-App-Id` header on that
same devtools request. A paid Qobuz subscription is required for the account
to have playlists worth syncing.

### Optional integrations

| Integration | Needed for | Key |
|---|---|---|
| Last.fm | similar tracks/artists, tags | free key from last.fm/api |
| MusicBrainz | canonical metadata, ISRC lookup | none (set a contact email) |
| ListenBrainz | open recommendations | none |
| Anthropic | natural-language playlist building | API key |

Secrets go to your system keyring (GNOME Keyring / KWallet). If no keyring
backend is available, Harmony falls back to a `0600` JSON file under
`~/.config/harmony/` and tells you it did.

## Documentation

The [`docs/`](docs/README.md) directory is the map — architecture, design
(vision, federation, auth, playback), packaging, roadmap, and decision records.

The short version: providers normalise each service into shared dataclasses,
`matching` resolves tracks across services, `sync` turns two playlists into a
reviewable plan before it writes anything, and the UI only ever talks to those
layers through a worker thread pool. The engine never imports GTK, so it can
back a desktop app today and a web/Android client or a federated instance later.
Module contracts live in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

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
