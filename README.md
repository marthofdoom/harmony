# Harmony

A GTK4/libadwaita desktop app for Linux that creates and syncs playlists across
**YouTube Music** and **Qobuz**, with unified search, cross-service track
matching, and recommendation tooling.

> **Status:** early. The sync engine, matching, and UI are functional; treat
> two-way sync against playlists you care about as experimental and keep the
> automatic snapshots turned on.

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
  restore, and playlists export to M3U / CSV / JSON.
- **Discovery** — similar artists and tracks blended from Last.fm,
  ListenBrainz/MusicBrainz, and each service's own recommendations.
- **Natural-language playlist building** (optional) — describe a playlist and
  have Claude plan it; every suggestion is then resolved against the real
  catalog, so nothing invented ever ends up in your library.

## Requirements

- Linux with GTK 4.12+ and libadwaita 1.4+
- Python 3.11+ with PyGObject

On Fedora / Silverblue-family systems the GTK stack is already present:

```bash
rpm -q python3-gobject gtk4 libadwaita
```

## Install (development)

```bash
git clone <this repo> harmony && cd harmony
python3 -m venv --system-site-packages .venv   # --system-site-packages gives the venv PyGObject
.venv/bin/python -m pip install -e ".[ai,dev]"
./run.sh
```

`--system-site-packages` matters: PyGObject is a system package with compiled
bindings against your system GTK, and pip-installing it into an isolated venv
usually fails or produces a mismatched build.

## Accounts

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

## Architecture

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for module contracts. The
short version: providers normalise each service into shared dataclasses,
`matching` resolves tracks across services, `sync` turns two playlists into a
reviewable plan before it writes anything, and the UI only ever talks to those
layers through a worker thread pool.

## Development

```bash
PYTHONPATH=src .venv/bin/python -m pytest      # tests are offline; no network
PYTHONPATH=src .venv/bin/python -m ruff check src tests
PYTHONPATH=src .venv/bin/python -m harmony     # run from source
```

## A note on the Qobuz API

The Qobuz client here is reverse-engineered from the public web player, as every
third-party Qobuz tool is. It can break whenever Qobuz changes their frontend,
and it is not endorsed by Qobuz. Harmony only uses it for catalog search and
playlist management on your own account — it does not download or decrypt audio.

## Licence

GPL-3.0-or-later.
