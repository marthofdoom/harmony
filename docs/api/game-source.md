# Game source API

**Status: built and verified** (unit tests + a real `harmony serve` end to end).

Harmony can act as a **proxy music source for a game engine**. The game asks
for a track for a *music type* (`explore`, `combat`, `dungeon`, ...), downloads
it as plain PCM WAV and plays it with its own audio engine. Harmony does the
picking (profiles and shuffled "bags"), the sourcing (your Library, Qobuz,
YouTube Music) and the transcoding (ffmpeg, cached). The game needs no codecs,
no streaming-service logins and no knowledge of where the music lives.

The API is part of the shared engine server, so it is available both from the
desktop app's embedded server and from the headless `.deb` server
(`harmony serve`). Nothing here is Skyrim-specific; a Skyrim SKSE plugin is
just the first intended client.

- [Quick start: no accounts needed](#quick-start-no-accounts-needed)
- [Auth](#auth)
- [Concepts](#concepts): sources, profiles, music types, selectors, candidate depth, the bag, no-repeat memory
- [Endpoints](#endpoints)
- [WAV format](#wav-format)
- [FLAC (lossless, about half the size)](#flac-lossless-about-half-the-size)
- [Caching, transcoding and 202](#caching-transcoding-and-202)
- [Range and HEAD](#range-and-head)
- [Errors](#errors)
- [Settings](#settings)
- [Integrating a game engine](#integrating-a-game-engine)

## Quick start: no accounts needed

You do not need a Qobuz account, a YouTube login or even a music library.
**By default Harmony uses every active source, and YouTube Music's public
catalogue is always one of them**; it covers
almost any music a game could want, with no sign-in at all (public search via
`ytmusicapi`, playback via `yt-dlp`). Install Harmony, start `harmony serve`
(or the desktop app), then:

```sh
H=http://localhost:8080
# 1. a profile: "ambient music" while exploring, "epic battle music" in combat
curl -X PUT $H/api/game/profiles/skyrim -d '{
  "types": {"explore": {"genre": "ambient"},
            "combat":  {"query": "epic battle music orchestral"},
            "default": {"genre": "ambient"}},
  "normalize": true
}'
# 2. ask for a track and download it as a WAV
curl -X POST $H/api/game/next -d '{"profile": "skyrim", "type": "explore"}'
curl -o track.wav "$H/api/game/audio/ytmusic/<id from the response>.wav?rate=48000&bits=16&channels=2&normalize=1"
```

(Add `-H 'X-Harmony-Key: ...'` if you set a [personal key](#auth).) No
`sources` is given, so the default applies: **every active source**. Signed-out
Qobuz and an empty or disabled Library are skipped silently; they never make a
request fail. Sign in to Qobuz or add a Library later and the same profile
simply starts mixing them in (Library copies win). To stay inside your own
Library, pass `"sources": "library"`.

Limits of the no-account route:

- The source is YouTube's **lossy AAC/Opus (roughly 128-160 kbps)**, converted to
  PCM WAV for the game. The WAV is exact and uncompressed, but it cannot be better
  than its source; the Library and Qobuz give lossless/hi-res when you have them.
- Some tracks are age-restricted or region-blocked and cannot be resolved without
  an account. Harmony skips them: a failed `GET /api/game/audio/...` (`502
  source_unavailable`) removes that track from `next` for 30 minutes, so just
  call `next` again.
- Streaming tracks shorter than 30 seconds or longer than 15 minutes (2-hour
  "sleep music" videos, jingles) are not offered; Library tracks are never filtered.
- A cold download takes a few seconds (resolve + decode; a 5-minute track
  with `normalize` took about 15 s in testing) and is then cached, so prefetch
  the next track while the current one plays.
- YouTube changes now and then; keep `yt-dlp` up to date on the server.

## Auth

Every `/api/game/*` request is gated like the rest of `/api`: if the instance
has a **personal key** set, send it as a header

```
X-Harmony-Key: <your personal key>
```

or, for clients that cannot set headers, as `?key=<your personal key>` on the
URL. With no personal key set the API is open to anyone who can reach the port
(a trusted LAN / Tailnet; see `docs/design/headless-server.md`). A missing or
wrong key answers `401 {"error": "personal key required"}`.

The examples below assume:

```sh
H=http://harmony.lan:8080            # your Harmony server
K='X-Harmony-Key: my-personal-key'   # omit -H "$K" if no key is set
```

## Concepts

### Sources: `all` (default), `library` or a list

**`sources` defaults to every active source** (`all`); pass `sources=library`
to stay in your Library, or a comma-separated subset of `library`, `ytmusic`,
`qobuz` such as `"library,ytmusic"` (no Qobuz). Every request can be addressed to
one of these source sets:

| `sources` | Where tracks come from |
|-----------|------------------------|
| `library` | **Only the Library**: this instance's own music folders plus every key-matching federated peer's Library. No streaming service is contacted. |
| `all` (default) | Every **active** source: the Library (if there is one), YouTube Music (**signed in or not**: its public catalogue works with no account) and Qobuz (only when signed in). A source that is not usable (no Library, Qobuz signed out) is skipped silently. A streaming track the Library also has is replaced by the Library copy (the same *library preferred* matching the apps use, `library_preferred` setting), and a song the Library and a service both list appears once. |

| `library,ytmusic`, `ytmusic`, `qobuz,ytmusic`... | Exactly the named services (still skipped silently when unusable). The names are normalised to the order `library,ytmusic,qobuz` without duplicates and are case-insensitive, so `"Qobuz, library,library"` is `"library,qobuz"`. An unknown name, an empty entry or `all` inside a list is a `400` naming the path (`sources: unknown source 'spotify'; ...`). Pools, the bag and the [no-repeat memory](#no-repeat-memory) are keyed by the normalised value. |

**Excluding a service server-wide.** The `game_excluded_services` setting (a list
of `"library"`, `"ytmusic"`, `"qobuz"`, default empty) removes services from
`all` and from every list, whatever the client asks for (a Qobuz account that
only serves 30 s previews: exclude `qobuz` once on the server). Changing it
rebuilds the affected pools. `GET /api/game/status` reports what is usable
right now.

The value comes from the request's `sources` field if given, else the
profile's `sources`, else `"all"`. (Profiles saved while the default was
`library` keep the `"library"` they were stored with; PUT them again without
`sources` to follow the default.)

### Profiles

A **profile** is a named, persisted description of what a game (or one save, or
one mod list) wants to hear. Profiles live in Harmony's settings
(`game_profiles`), so they survive restarts and ship with the instance.

```json
{
  "sources": "all",
  "types": {
    "explore": {"genre": "Ambient"},
    "combat":  [{"genre": "Metal"}, {"query": "battle drums"}],
    "dungeon": {"playlist": {"service": "qobuz", "id": "123456"}},
    "tavern":  {"album":    {"service": "local", "id": "9f2c1ab04d77e310"}},
    "town":    {"artist":   {"service": "ytmusic", "id": "UC1234567890abcdefghij"}},
    "default": {"genre": "Ambient"}
  },
  "format": {"rate": 48000, "bits": 16, "channels": 2, "container": "wav"},
  "normalize": true,
  "trim_silence": false
}
```

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `sources` | `"library"` \| `"all"` \| list | `"all"` | Default [source set](#sources-all-default-library-or-a-list) for this profile: every active source, only the Library, or e.g. `"library,ytmusic"`. |
| `types` | object | required, 1-64 entries | Music type name -> [selector](#selectors). Names are 1-64 characters of letters, digits, `_`, `-`, `.` (starting with a letter or digit). |
| `format.rate` | `44100` \| `48000` | `48000` | WAV sample rate (Hz). |
| `format.bits` | `16` \| `24` | `16` | Bits per sample. |
| `format.channels` | `1` \| `2` | `2` | Mono or stereo (a stereo source is downmixed for mono). |
| `format.container` | `"wav"` \| `"flac"` | `"wav"` | File format of the `audio_url` that `next` returns ([FLAC](#flac-lossless-about-half-the-size)). |
| `normalize` | bool | `false` | Loudness-normalise every track ([details](#loudness-normalisation)). |
| `trim_silence` | bool | `false` | Cut leading and trailing silence ([details](#silence-trimming)). |

Validation is strict: unknown keys, wrong types and out-of-range values are
rejected with `400` and a message naming the offending path (for example
`types.combat[1].playlist.service: must be one of local, qobuz, ytmusic`).

### Music types

A **music type** is whatever vocabulary your game uses: the profile maps each
type to a selector. A request for a type the profile does not define falls back
to the profile's `"default"` type if it has one; otherwise it is a
`404 unknown_type` listing the available types. Type lookup is exact first,
then case-insensitive (`Explore` finds `explore`).

### Selectors

A selector says which tracks belong to a music type. It is an object with
**exactly one** key, or a **list** of such objects (their union):

| Selector | Library | `sources=all` |
|----------|---------|---------------|
| `{"genre": "Ambient"}` | Library tracks tagged with that genre (case-insensitive, whole-genre match). | Same, **plus** deep YouTube Music / Qobuz track searches for `"<genre> music"`, `"best <genre> songs"` and `"<genre> playlist"`, and YouTube Music's Moods & Genres playlists named like the genre ([depth](#candidate-depth)). |
| `{"query": "dark ambient"}` | Library track search for the text. | Track search on the Library, YouTube Music and (signed in) Qobuz, [deep](#candidate-depth) (no variants). |
| `{"playlist": {"service", "id"}}` | n/a (the Library has no playlists). | The playlist's tracks on that service (public YouTube Music playlists and albums work without an account). |
| `{"album": {"service", "id"}}` | The album's tracks (`service: "local"`). | The album's tracks on that service. |
| `{"artist": {"service", "id"}}` | The artist's tracks (`service: "local"`). | The artist's top tracks (up to 100). |

`service` is one of `local` (the Library), `qobuz`, `ytmusic`. Ids are the ones
the apps and `/api/search` return. With `sources=library`, a selector that names
a streaming service contributes nothing (it is skipped, not an error); if a type
ends up with no tracks at all, `next` answers `404 no_tracks`.

Candidate lists are cached in memory (and rebuilt immediately when the profile
is edited), so a busy game does not search on every request. A list with only
Library selectors is refreshed every 10 minutes; one with **streaming** selectors
every `game_stream_pool_ttl_s` seconds (default 3600, clamped 600-86400), and that
refresh is **stale-while-revalidate**: `next` keeps serving the existing
candidates immediately while ONE background thread per pool rebuilds the list and
swaps it in (the bag keeps its cycle; see [the bag](#the-bag)). Only a **cold**
build (nothing cached yet: first request after a start, a profile edit or a
changed `sources`/exclusion) is synchronous. An **empty** Library list (still
being scanned) is retried after about 3 seconds; an empty streaming list after 30
seconds.

**Timeouts for clients.** A cold streaming build can make `next` take **10-12
seconds** (the 8 s request budget plus the call in flight), so give `next` a timeout of
**at least 30 seconds** (later calls answer in milliseconds). To avoid a long
first wait, call `next` with `"peek": true` for each music type at game start.

### Candidate depth

A streaming search used to give about 25 tracks per service. For `genre` and
`query` selectors that include streaming services Harmony now asks for
`game_stream_search_limit` tracks (default 100, clamped to 10-300) per query:

- **YouTube Music**: one search with that `limit` (ytmusicapi follows the result
  continuations itself, verified in its source). Songs-filtered, as before.
- **Qobuz** (signed in): pages of 50 using the catalogue `offset`.
- **Genre variants** (`game_genre_variants`, default `true`; `false` turns off
  both the variants and the mood playlists): `genre` selectors also search
  `"best <genre> songs"` and `"<genre> playlist"` (half the depth each) and, on
  YouTube Music, add up to 3 Moods & Genres playlists (50 tracks each) whose
  category title equals the genre name case-insensitively (`Chill`, `Focus`,
  `Workout`...; no sign-in needed). Results are merged and de-duplicated.
- The existing 30 s - 15 min duration filter for streaming tracks still applies;
  the Library branch is unchanged.
- **Time budget**: a candidate build spends at most about 8 seconds on streaming
  requests (a request already running is not interrupted). If the budget runs
  out or a service call fails, the pool is built from what arrived and rebuilt
  after exactly **60 seconds** (instead of the normal TTL), reusing the results
  already fetched, so it completes over the next refreshes. That reuse stops after
  5 consecutive partial builds or 10 minutes (the fetched results are dropped and
  fetched afresh), so one call that keeps failing cannot freeze the rest. A
  partial rebuild never makes an existing pool smaller; tracks that arrive late
  and were served before the restart are queued behind the current cycle, least
  recently served first (see [no-repeat memory](#no-repeat-memory)).
- Requests are sequential (never parallel) within a build. **Be aware:** every
  streaming call holds the engine-wide provider lock (the same lock the app's
  own YouTube Music / Qobuz calls take), so while a build runs the UI's provider
  calls wait their turn behind each search; this is why warm refreshes are done in
  the background and spread out by the long TTL.
- **Back-off and request rate.** After a rate-limit error, or 3 failures in a
  row, a service gets no game-source requests for 30 s, doubling to 10 minutes
  on each repeat; one success resets it. This covers every search, playlist,
  album and artist call the game source makes. Worst case without failures:
  one build per pool per TTL, and a build is at most ~3 searches per streaming
  service (genre: primary, 2 variants; Qobuz in pages of 50), plus the mood
  lookup (2 browse requests + up to 3 playlist requests) on YouTube Music, each
  YouTube search being several continuation requests. With the defaults that is
  well under 100 YouTube requests per genre type per hour; a partly failing
  build is retried every 60 s but gated by the back-off.

**One song once.** Within a candidate list, tracks with the same normalised
(artist, title) collapse to one, preferring the **Library** copy; between two streaming services the one seen first is kept.
Normalisation is conservative: case, punctuation and spacing are ignored, and
only pure noise tags are dropped: `(Official Video)`, `[Official Audio]`,
`(Remastered)` / `- 2011 Remaster`, `(feat. X)` / `ft. X`, and a YouTube
`- Topic` artist suffix. Anything meaningful stays distinct: `(Live)`, `(Remix)`,
`(Acoustic)`, `(Instrumental)`, `(Radio Edit)`, `(Part 2)`, a bare year, or a
different artist. Tracks without a title or artist are never merged.

### The bag

For every `(profile, music type)` Harmony keeps a **shuffled bag**: each
candidate track is served once, in random order, before any repeats; when the
bag is empty it is reshuffled, and the first track of the new cycle is never
the last one of the previous cycle (when there is more than one track). A type
with a single playable track simply repeats it. The cycle is
[remembered across restarts](#no-repeat-memory). When a refresh adds candidates,
unseen ones are shuffled into the rest of the cycle, ones served before wait
behind it.

The first track of a reshuffled cycle is also never one of the last
`min(50, pool_size / 2)` tracks served.

### No-repeat memory

Harmony remembers recently served tracks per `(profile, type, sources)` in
`game-history.json` next to `settings.json` (`~/.config/harmony/` for a normal
install). Format: `{"version": 1, "pools": {"<profile>/<type>/<sources>":
{"fp": "<selector fingerprint>", "t": <unix time>, "served": ["service:id", ...]}}}`,
oldest first, written atomically after every consuming `next`. Bounds: the last
**2000** tracks per pool and **20000** in total (the least recently used pools go
first). A missing, unreadable or corrupt file is ignored (start fresh); a file that
cannot be written is only logged.

When a pool is built cold, for example after a restart, its first cycle is the
never-served candidates (shuffled) followed by **every served one, least
recently served first**, so nothing you just heard comes back before everything
else has played (no floor; also true after the first full pass through a big
pool). A track that arrives later (a pool completed after a partial build) is
queued the same way if it was served before. `"peek": true` never writes memory.

Editing the selectors (or `sources`) of a music type resets that type's memory
(the fingerprint changes); editing other types or the format keeps it.
**Reset it** by `DELETE /api/game/profiles/<name>` (clears the profile's
memory), by `DELETE /api/game/history?profile=<name>[&type=<type>]`
(`{"ok": true, "cleared": <pools>}`; `type` is matched case-insensitively; also
restarts the live bags), by deleting the
file, or by setting `game_history_enabled` to `false` (no memory is read or
written).

`POST /api/game/next` **advances the bag by `count`** and always returns
`count` tracks as long as at least one candidate is playable (a small pool wraps
into the next cycle). To look ahead without using tracks up, pass
`"peek": true`: the response is exactly what the next consuming call(s) will
return, including across a cycle boundary.

**Blocked tracks.** A streaming track Harmony could not resolve (region- or
age-blocked, removed...) is skipped for 30 minutes. It is simply passed over, so
the cycle gets shorter, and in a small pool the remaining tracks come back
around sooner than "once per cycle of the full pool". Still, the same track is
never handed out twice in a row unless it is the only playable one left.

## Endpoints

All bodies are JSON (`Content-Type: application/json`). All errors have the form
`{"error": "<message>", "code": "<machine code>"}` ([list](#errors)).

### `GET /api/game/profiles`

```sh
curl -H "$K" $H/api/game/profiles
```
```json
{"profiles": [{"name": "skyrim", "sources": "all", "types": {"explore": {"genre": "Ambient"}},
               "format": {"rate": 48000, "bits": 16, "channels": 2, "container": "wav"},
               "normalize": false, "trim_silence": false}]}
```

### `GET /api/game/profiles/{name}`

The same object for one profile (`404 unknown_profile` if it does not exist).

### `PUT /api/game/profiles/{name}`

Create or replace a profile. The body is the profile (a `"name"` key is
accepted only if it equals the URL's). Missing optional fields are filled with
their defaults and the stored profile is returned.

```sh
curl -X PUT -H "$K" -H 'Content-Type: application/json' $H/api/game/profiles/skyrim -d '{
  "sources": "library",
  "types": {"explore": {"genre": "Ambient"}, "combat": {"genre": "Metal"},
            "default": {"query": "calm"}},
  "normalize": true
}'
```

Up to **32** profiles; the name is 1-64 characters of letters, digits, `_`, `-`, `.`.
Size limits keep `settings.json` small: one profile may be at most **32 KB**
serialised and all profiles together at most **256 KB**; beyond that the answer
is `413` (`profile_too_large` / `profiles_too_large`). A realistic profile (a
dozen types, a few selectors each) is well under 4 KB.

### `DELETE /api/game/profiles/{name}`

```sh
curl -X DELETE -H "$K" $H/api/game/profiles/skyrim      # {"ok": true, "deleted": "skyrim"}
```

### `DELETE /api/game/history?profile={name}[&type={type}]`

Clears the [no-repeat memory](#no-repeat-memory) of a profile, or of one music
type, and restarts the live bags: `{"ok": true, "profile": "skyrim", "type": null, "cleared": 3}`.
`profile` is required.

### `POST /api/game/next`

Pick the next track(s) for a music type.

| Field | Required | Meaning |
|-------|----------|---------|
| `profile` | yes | Profile name. |
| `type` | yes | Music type (falls back to the profile's `default`). |
| `sources` | no | `"library"`, `"all"` or a comma-separated list such as `"library,ytmusic"`; overrides the profile's (default `"all"`, every active source). The response echoes the normalised value. |
| `count` | no | 1-10 (default 1): how many tracks to return **and consume**. |
| `peek` | no | `true` returns the next `count` tracks **without consuming** them (default `false`). |
| `container` | no | `"wav"` or `"flac"`; overrides the profile's `format.container` (default `"wav"`). It only decides the extension of `audio_url`. |

```sh
curl -X POST -H "$K" $H/api/game/next -d '{"profile": "skyrim", "type": "explore", "count": 2}'
```
```json
{
  "profile": "skyrim", "type": "explore", "requested_type": "explore",
  "sources": "all", "peek": false, "pool_size": 42,
  "tracks": [
    {
      "service": "local", "id": "87fbeaab3e0ef480",
      "title": "Slow Fog", "artist": "Ambient Artist", "album": "Drifts",
      "duration_s": 247,
      "quality": {"codec": "FLAC", "bit_depth": 24, "sample_rate": 96000, "bitrate": 846,
                  "channels": 2, "lossless": true, "tier": "hires",
                  "label": "FLAC · 24-bit / 96 kHz · Hi-Res"},
      "source": {"service": "local", "label": "Library", "instance": "media-server",
                 "own": true, "from_library": false},
      "audio_url": "/api/game/audio/local/87fbeaab3e0ef480.wav?rate=48000&bits=16&channels=2&normalize=1&trim=0"
    }
  ]
}
```

- `type` is the music type that was actually used (`"default"` after a
  fallback); `requested_type` is what you asked for.
- `quality` is the *source's* format (it can be `null`); the WAV or FLAC you
  download is always the format in `audio_url`.
- `source.from_library` is `true` when a streaming pick was swapped for the
  Library copy.
- `audio_url` is relative to the server and already carries the profile's
  format (it ends in `.wav` or `.flac` according to `container`).
  If your client cannot send headers, append `&key=<personal key>`.
- A pool smaller than `count` repeats tracks within one response (a one-track
  type returns it `count` times).

### `GET /api/game/audio/{service}/{id}.wav`

Downloads the track as PCM WAV (for FLAC, see [`GET /api/game/audio/{service}/{id}.flac`](#get-apigameaudioserviceidflac)). Query parameters (all optional):

| Parameter | Values | Default |
|-----------|--------|---------|
| `rate` | `44100`, `48000` | `48000` |
| `bits` | `16`, `24` | `16` |
| `channels` | `1`, `2` | `2` |
| `normalize` | `0`/`1`/`true`/`false` | `0` |
| `trim` | `0`/`1`/`true`/`false` | `0` |
| `wait` | seconds, 0-120 | `30` (see [202](#caching-transcoding-and-202)) |

```sh
curl -H "$K" -o track.wav "$H/api/game/audio/local/87fbeaab3e0ef480.wav?rate=48000&bits=16&channels=2"
```

Responds `200` with `Content-Type: audio/wav` and an exact `Content-Length`,
`206` for a `Range`, or `202` while the file is still being prepared.
`service` must be `local`, `qobuz` or `ytmusic` and `id` a plain provider id
(letters, digits, `_ . : @ + = ~ - [ ]`, at most 200 characters); anything else
is `400` before the engine is touched. Only tracks the engine can resolve are
ever fetched: the endpoint takes no URLs.

### `GET /api/game/audio/{service}/{id}.flac`

The same track as lossless FLAC (see [FLAC](#flac-lossless-about-half-the-size)).
It takes exactly the query parameters, auth, validation, errors, `202`/`Retry-After`
behaviour, `Range` and `HEAD` of the `.wav` endpoint above and answers `200` with
`Content-Type: audio/flac`.

```sh
curl -H "$K" -o track.flac "$H/api/game/audio/local/87fbeaab3e0ef480.flac?rate=48000&bits=16&channels=2"
```

### `POST /api/game/playing`

Tell Harmony what the game started playing. Harmony records it as a simple
"game" now-playing item and history entry (shown by `/api/game/status`; it is not
wired into the player bar).

```sh
curl -X POST -H "$K" $H/api/game/playing \
  -d '{"profile": "skyrim", "type": "explore", "service": "local", "id": "87fbeaab3e0ef480"}'
```
```json
{"ok": true, "now_playing": {"profile": "skyrim", "type": "explore", "service": "local",
  "id": "87fbeaab3e0ef480", "title": "Slow Fog", "artist": "Ambient Artist", "album": "Drifts",
  "duration_s": 247, "quality": {"...": "..."}, "started_at": 1793079882.0, "age_s": 0.0}}
```

Title/artist/album are filled in from the last `next` response that listed the
track (otherwise they are `null`). The record counts as "now playing" for
`duration_s + 60` seconds (1 hour if the duration is unknown) so a game that
quit does not play forever; it stays in the history.

### `GET /api/game/status`

```sh
curl -H "$K" $H/api/game/status
```
```json
{
  "enabled": true,
  "ffmpeg": true,
  "containers": ["wav", "flac"],
  "cache": {"bytes": 1291990, "files": 2, "in_progress_bytes": 0, "cap": 2147483648},
  "now_playing": {"profile": "skyrim", "type": "explore", "service": "local", "id": "87fbeaab3e0ef480",
                  "title": "Slow Fog", "age_s": 12.4, "...": "..."},
  "history": [{"profile": "skyrim", "type": "explore", "service": "local", "id": "87fbeaab3e0ef480",
               "title": "Slow Fog", "artist": "Ambient Artist", "started_at": 1793079882.0}],
  "profiles": ["skyrim"],
  "services": {"library": true, "ytmusic": true, "qobuz": false},
  "excluded_services": ["qobuz"]
}
```

`enabled` mirrors the `game_source_enabled` setting, `ffmpeg` says whether
ffmpeg was found (audio needs it), `containers` lists the audio file formats this
server can produce (`.wav` and `.flac` downloads, the `container` option of
`next`; a server without the field only does WAV, so a client should treat a
missing `containers` as `["wav"]`), `cache` is the audio cache (`bytes` of finished
files, `in_progress_bytes` of half-written ones, `cap` in bytes),
`history` the last 20 `playing` records. `services` says which sources `sources: "all"`
would use right now: the Library when it is enabled and non-empty, YouTube Music
always, Qobuz only when signed in; a service in `excluded_services` (setting
`game_excluded_services`) is always `false`.

## WAV format

Every download is a **canonical 44-byte-header RIFF/WAVE** file:

```
 0  "RIFF"   4  RIFF size = file size - 8
 8  "WAVE"
12  "fmt "   4  16
20  2  format tag 1 (PCM)            22  2  channels
24  4  sample rate                   28  4  byte rate = rate * block
32  2  block align = channels*bits/8 34  2  bits per sample (16 or 24)
36  "data"   4  data size
44  PCM samples, little-endian, interleaved
```

- Always `WAVE_FORMAT_PCM` (tag 1), also for 24-bit: no `WAVE_FORMAT_EXTENSIBLE`,
  no `LIST`/metadata chunks, so even naive parsers read it (`data` always starts
  at byte 44).
- Sizes are exact: `Content-Length` equals the file size, the `RIFF` and `data`
  sizes describe the whole file (an odd data size gets the mandatory RIFF pad
  byte after it), and the sample count is a whole number of frames.
- Frames = `rate * duration` of the source (to within a few ms, a little less
  when `normalize` or `trim` are on). Sources longer than 2 hours are cut at 2 h
  (WAV's 4 GiB limit).
- 16-bit output is dithered (triangular HP); 24-bit is plain truncation of the
  32-bit pipeline. A 24-bit/96 kHz FLAC is resampled to the requested rate and
  depth; nothing is ever served at a rate/depth you did not ask for.

## FLAC (lossless, about half the size)

`.flac` instead of `.wav` returns the **identical audio** as native FLAC: lossless
compression of exactly the PCM the WAV would hold, so a client that can decode
FLAC downloads roughly half the bytes (a 133 s 44.1 kHz/16-bit stereo track is
23 MB as WAV). There is deliberately no lossy container: the sources are lossy
(YouTube AAC/Opus) already and are never re-encoded a second time.

- Same pipeline as WAV: `normalize`, `trim`, `rate`, `bits` and `channels` behave
  exactly as described here (the WAV is produced first and then only re-wrapped
  losslessly), including the 16-bit dither and the 24-bit rule. Decoding the FLAC
  gives the same samples as the `.wav` of the same query.
- `bits=16` is a 16-bit FLAC, `bits=24` a 24-bit FLAC (stored in 32-bit sample
  slots only inside ffmpeg; the file's STREAMINFO says 24 bits). Mono and stereo,
  44.1 and 48 kHz.
- Encoded with ffmpeg's `flac` encoder (`-fflags +bitexact`, so no version-dependent encoder string) at compression level 1: nearly as small
  as the slowest settings and fast (a 3-minute stereo track encodes in well under
  a second).
- Layout: the `fLaC` marker, a STREAMINFO block with the exact total sample count,
  a small tag block written by ffmpeg (just its vendor string; no title, artist
  or other track metadata), a padding block, then the frames. The exact bytes can
  differ between ffmpeg versions, although the `ETag` is the cache key and does
  not change with them.
- `Content-Type: audio/flac`. `Range`/`HEAD` work on the **file as stored**, but
  a byte offset in a FLAC file is not a sample position (frames are variable
  size): **WAV is the only container where byte offsets map to sample frames**.
  Use `Range` on FLAC only to resume or split a download, never to seek.
- It is cached next to the WAV (a different cache entry, never mixed up with it)
  and shares the same size cap, single-flight, concurrency limits and `202`
  behaviour. While a FLAC is being built it needs roughly 1.5-1.7 times the
  WAV's size in free cache space (the scratch WAV plus the growing FLAC).

### Loudness normalisation

`normalize=1` runs ffmpeg's EBU R128 `loudnorm` in a single pass at
**-16 LUFS integrated, -1.5 dBTP true peak, LRA 11**. Single-pass loudnorm is
dynamic (a quiet track is raised, a hot one is brought down, within limits);
it is a good "same loudness across a playlist" tool, not a mastering step.

### Silence trimming

`trim=1` removes leading and trailing silence (below -50 dB; interior pauses are
kept). The head is cut by a streaming ffmpeg filter and the tail by scanning the
finished PCM backwards from the end, so memory stays flat (ffmpeg stays around
70 MB whatever the track length) and the cost is proportional to the length of
the silence removed. Whatever the source, at most 2 hours are read: the cap is
applied to ffmpeg's *input*, so a long or endless stream is never downloaded in
full.

## Caching, transcoding and 202

Harmony decodes the source (a Library `file://`, a federated peer's stream, or a
Qobuz/YouTube Music stream URL with the headers it needs) with **ffmpeg** and
writes the finished WAV (or FLAC) into a cache:

- Location: `<user cache dir>/harmony/game-audio/` (for example
  `~/.cache/harmony/game-audio/`; in the Flatpak, `~/.var/app/io.github.marthofdoom.Harmony/cache/harmony/game-audio/`).
- Key: `(source actually used, rate, bits, channels, normalize, trim, container)` (hashed, so `.wav` and `.flac` of one query are separate entries;
  file names are never derived from request text, so there is no path
  traversal). The source is the one that gets transcoded: when the Library has a
  copy of a streaming track it is the Library copy, and it is cached apart from
  the streamed one; if that copy turns out to be unavailable and the stream is
  used instead, the file is stored under the stream's key.
- **LRU size cap**: `game_cache_max_mb` (default 2048, minimum 64). When the cache
  grows past it the least-recently-used WAVs are deleted. Half-written files
  count toward the cap. The effective cap is never below twice the largest
  cached file, so a tiny cap cannot make two conversions evict each other; a
  file that was made or used in the last 2 minutes, or is being downloaded
  right now, is never evicted. If the cache is over its cap and nothing can be
  evicted, new conversions are refused with `503 cache_full` until space frees
  up (raise `game_cache_max_mb`).
- **Bounded work**: at most 3 conversions (ffmpeg, plus a yt-dlp resolve for
  streams) run at once. Requests for further ones wait their turn and get the
  usual `202` + `Retry-After`; if more than 32 are already waiting, new ones get
  `503 busy` (with `Retry-After`).
- **Never partial**: a transcode writes to a temp file and is renamed into place
  only when ffmpeg exited cleanly, so a half-written or failed file is never
  served; a torn or foreign file found in the cache is discarded and rebuilt.
  A failed transcode answers `502` and is not retried for 20 seconds.
- **Single flight**: concurrent requests for the same key share one transcode
  (and one provider resolve). It runs on a server thread, so a client that gives
  up does not abort it.
- A cache hit is a plain file read (no resolve, no ffmpeg) and still works with
  ffmpeg missing.

### Blocking and `202`

An uncached file cannot be served until it is complete (the exact
`Content-Length` and `RIFF` sizes need the whole track). The request therefore
**blocks until the file is ready, for up to `wait` seconds (default 30)**. If it
is not ready by then, the answer is

```
HTTP/1.1 202 Accepted
Retry-After: 2

{"status": "transcoding", "retry_after_s": 2}
```

and the client simply asks again (the transcode keeps running; the next request
joins it or hits the cache). `wait=0` makes the endpoint purely non-blocking
(202 until ready), which suits an engine that polls from a game thread. A local
FLAC is typically ready in well under a second; a streaming track depends on
the provider (a few seconds). `HEAD` behaves the same way (it needs the real
length).

## Range and HEAD

`Accept-Ranges: bytes` is always sent. A single `Range: bytes=a-b`, `a-` or `-n`
gets `206 Partial Content` with `Content-Range: bytes a-b/total` and the exact
`Content-Length` of the slice; an unsatisfiable range gets `416` with
`Content-Range: bytes */total`; multiple ranges are ignored (full `200`). Seeking
inside a WAV is a byte offset: `44 + frame * channels * bits/8` (a `.flac`
supports the same `Range` over the file as stored, but its byte offsets are **not**
sample positions; see [FLAC](#flac-lossless-about-half-the-size)). An invalid
range such as `bytes=10-5` is ignored (full `200`, RFC 9110). `HEAD` returns
the headers of the `GET` (`Content-Type`, `Content-Length`, `ETag`) with no body.
Responses carry `ETag: "<cache key>"` and `Cache-Control: private, max-age=3600`.

## Errors

| HTTP | `code` | When |
|------|--------|------|
| 400 | `invalid_profile` | Profile body failed validation (the message names the path, e.g. `format.container`). |
| 400 | `invalid_name` | Bad profile name. |
| 400 | `bad_request` | Bad `next` / `playing` fields (including a `container` other than `"wav"`/`"flac"`), bad `service` or `id`. |
| 400 | `bad_format` | Bad `rate` / `bits` / `channels` / `normalize` / `trim` / `wait`. |
| 400 | `bad_request` | (also) a missing-or-malformed `Content-Length` on a request with a body. |
| 400 | `too_many_profiles` | More than 32 profiles. |
| 401 | (none) | Missing or wrong personal key. |
| 403 | `disabled` | `game_source_enabled` is false. |
| 404 | `unknown_profile` | No such profile. |
| 404 | `unknown_type` | Type not in the profile and no `default` type. |
| 404 | `no_tracks` | The type's selectors matched nothing playable for the chosen `sources`. |
| 404 | `unknown_service` | The engine has no such service configured. |
| 404 | `not_found` | Unknown route / not an `<id>.wav` or `<id>.flac` path. |
| 405 | `method_not_allowed` | Wrong method for the route. |
| 409 | `invalid_profile` | A stored profile (edited by hand) no longer validates; PUT it again. |
| 411 | `length_required` | A chunked (`Transfer-Encoding`) request body; send `Content-Length`. |
| 413 | `too_large` | Request body over 1 MB (every game `POST`/`PUT`/`DELETE`). |
| 413 | `profile_too_large` / `profiles_too_large` | One profile over 32 KB serialised, or all profiles over 256 KB. |
| 416 | (none) | Unsatisfiable `Range`. |
| 502 | `source_unavailable` | The engine could not resolve the track (signed out, offline, file gone). |
| 502 | `transcode_failed` / `bad_source` / `too_long` | ffmpeg failed, produced nothing, or the source is not a file / http(s) stream. |
| 503 | `ffmpeg_missing` | ffmpeg is not installed on the server. |
| 503 | `busy` | Too many conversions already queued; retry after `Retry-After`. |
| 503 | `cache_full` | The audio cache is over its cap and nothing can be evicted yet; retry later or raise `game_cache_max_mb`. |
| 503 | `cache_busy` | The cached file vanished while being served (rare); retry. |
| 504 | `transcode_timeout` | ffmpeg ran over 15 minutes. |

`202` is not an error: see [above](#blocking-and-202).

## Settings

In Harmony's `settings.json` (managed by the server; edit with the server stopped):

| Key | Default | Meaning |
|-----|---------|---------|
| `game_source_enabled` | `true` | `false` answers `403 disabled` to everything except `GET /api/game/status`. |
| `game_profiles` | `{}` | The profiles (use the API rather than editing). |
| `game_cache_max_mb` | `2048` | Audio (WAV and FLAC) cache size cap in MB (minimum 64; in effect never below twice the largest cached file). |
| `game_stream_search_limit` | `100` | Tracks fetched per streaming query for `genre`/`query` selectors (clamped 10-300; [details](#candidate-depth)). |
| `game_genre_variants` | `true` | Extra `genre` query variants and YouTube Music mood playlists. |
| `game_history_enabled` | `true` | Persist the [no-repeat memory](#no-repeat-memory) (`game-history.json`). |
| `game_stream_pool_ttl_s` | `3600` | Seconds before a candidate list with streaming selectors is refreshed in the background (clamped 600-86400; Library-only lists: 600). |
| `game_excluded_services` | `[]` | Services removed from `all` and from every `sources` list: any of `"library"`, `"ytmusic"`, `"qobuz"`. |

ffmpeg is required: the `.deb` depends on it, and the Flatpak's GNOME runtime
ships `/usr/bin/ffmpeg` (verified: 7.1 with `loudnorm`, `silenceremove`, and the
FLAC/AAC/MP3/Opus/Vorbis decoders). A bare `pip` install needs ffmpeg on `PATH`;
`/api/game/status` reports `"ffmpeg": false` when it is missing.

## Integrating a game engine

Recommended flow for a music system (for example an SKSE plugin replacing the
game's music selection):

1. **Start-up.** `GET /api/game/status`. Check `enabled` and `ffmpeg`. Make sure
   the profile exists (`GET /api/game/profiles/<name>`, or `PUT` it from your
   plugin's bundled default so a fresh server just works).
2. **Prime.** When a music type is first needed (the game enters exploration),
   `POST /api/game/next {"profile", "type", "count": 2}`. You get the track to
   play now and the one after it.
3. **Download ahead.** Download `tracks[0].audio_url` (and `tracks[1]`
   straight after, or while track 0 plays). Download on a worker thread, never the
   game thread: a cold request can block for seconds (or use `wait=0` and poll
   on a timer if you prefer 202s).
4. **Play.** Hand the finished WAV to the engine's audio system, then
   `POST /api/game/playing` with the `service` and `id` of the track.
5. **Keep one ahead.** Each time a track starts, `POST next {"count": 1}` and
   download it while the current one plays. Because `next` consumes, the
   pre-downloaded track is never asked for twice. To look ahead without
   consuming (say a music-type change makes you throw the prefetch away), use
   `"peek": true` and consume only when you actually play.
6. **Music type changes** (combat starts): `POST next` for the new type and play
   it when ready; the old type's bag is untouched, so returning to it continues
   where it left off. A 202/timeout must never silence the game: keep the
   current track (or the game's own music) until the new file exists.
7. **Errors.** On `404 no_tracks` / `unknown_type`, fall back to the game's own
   music. On `5xx`, retry with backoff and ask `next` for another track. Treat
   transport errors as "Harmony is away" and keep playing locally.

### The double-slot cache-file pattern

Many engines (Skyrim's included) play music from **files on disk**, not from a
memory buffer, and a file that is being played must not be overwritten. Use two
slots:

```
Data/Music/Harmony/slot_a.wav
Data/Music/Harmony/slot_b.wav
```

- The engine plays slot A. The plugin downloads the *next* track into slot B:
  write to `slot_b.wav.tmp`, `fsync`/close, then **rename** over `slot_b.wav`
  (the server's `Content-Length` lets you verify the byte count before the
  rename, so a cut download is never played).
- When the engine finishes A (or crossfades), point it at B, and the *next*
  download goes into A. The slot being played is never written.
- Keep `track.json`-style bookkeeping per slot (service, id, duration) so you can
  `POST /api/game/playing` when a slot actually starts.
- On startup, delete stale `.tmp` files; if you reuse a cached slot after a
  restart, `HEAD` the `audio_url` and compare `ETag`/`Content-Length` first.

For engines that can stream from memory, skip the files and decode the
download directly; the WAV header is always the plain 44-byte kind.

Seeking, if the engine wants it, is plain `Range` arithmetic (see
[Range and HEAD](#range-and-head)). Loudness (`normalize`) and format are
per-profile, so every track of a profile comes out the same shape.
