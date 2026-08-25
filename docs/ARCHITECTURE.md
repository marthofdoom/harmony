# Harmony — architecture & module contracts

Python 3.11+, GTK4 + libadwaita (PyGObject), package root `src/harmony/`.
All modules import domain types from `harmony.models` (`Service`, `Track`, `Album`,
`Artist`, `Playlist`, `SearchResults`). **These types are frozen — do not edit
`models.py`.** Everything below is the contract each module must satisfy.

```
harmony/
  models.py        (done)   domain dataclasses
  config.py        (done)   paths, Settings, keyring-backed credentials
  errors.py        (done)   exception hierarchy
  db.py                     sqlite: link table, playlist snapshots, cache
  providers/
    base.py                 MusicProvider ABC
    ytmusic.py              YouTube Music provider (ytmusicapi)
    qobuz.py                Qobuz provider (own reverse-engineered client)
  matching.py               fuzzy cross-service track matching
  sync.py                   sync engine (mirror / two-way), plan + apply
  enrich/
    lastfm.py               similar artists/tracks, tags
    musicbrainz.py          canonical metadata + ISRC lookup
    listenbrainz.py         open recommendations
    recommender.py          blends the above + native provider recs
  ai/claude.py              natural-language playlist planning (Anthropic SDK)
  io_formats.py             M3U / CSV / JSON import + export
  ui/                       GTK4 widgets (see ui contract at the bottom)
  __main__.py               entry point
```

## Threading rule

All provider/network calls are blocking. The UI must never call them on the main
loop. `harmony.tasks.run_async(fn, on_done, on_error)` (see `tasks.py`) runs `fn`
on a worker thread and marshals the result back via `GLib.idle_add`. Provider
objects must be safe to call from a single worker thread at a time.

## providers/base.py — `MusicProvider` ABC

```python
class MusicProvider(ABC):
    service: Service
    @property
    def is_authenticated(self) -> bool: ...
    def authenticate(self) -> None: ...            # raises AuthError
    def account_name(self) -> str | None: ...

    def search(self, query: str, *, kinds: Sequence[str] = ("tracks",),
               limit: int = 25) -> SearchResults: ...
    #   kinds subset of {"tracks","albums","artists","playlists"}

    def get_track(self, track_id: str) -> Track: ...
    def get_album_tracks(self, album_id: str) -> list[Track]: ...
    def get_artist_albums(self, artist_id: str, *, limit: int = 100) -> list[Album]: ...
    def get_artist_top_tracks(self, artist_id: str, *, limit: int = 20) -> list[Track]: ...

    def list_playlists(self) -> list[Playlist]: ...
    def get_playlist(self, playlist_id: str) -> Playlist: ...
    def get_playlist_tracks(self, playlist_id: str) -> list[Track]: ...
    def create_playlist(self, title: str, description: str = "",
                        public: bool = False) -> Playlist: ...
    def add_tracks(self, playlist_id: str, track_ids: Sequence[str]) -> None: ...
    def remove_tracks(self, playlist_id: str, track_ids: Sequence[str]) -> None: ...
    def delete_playlist(self, playlist_id: str) -> None: ...
    def rename_playlist(self, playlist_id: str, title: str,
                        description: str | None = None) -> None: ...

    def similar_tracks(self, track: Track, *, limit: int = 20) -> list[Track]: ...
    def liked_tracks(self, *, limit: int = 500) -> list[Track]: ...
```

Batching and rate-limit sleeps belong **inside** the provider (`add_tracks`
chunks: YT 100, Qobuz 50). Unimplementable operations raise
`NotSupportedError`, never silently no-op.

## matching.py

```python
@dataclass
class MatchCandidate:
    track: Track; score: float; reasons: list[str]

@dataclass
class MatchResult:
    source: Track
    best: MatchCandidate | None
    candidates: list[MatchCandidate]
    confidence: Literal["exact", "high", "low", "none"]

def normalize_title(s: str) -> str      # strips "(Remastered 2011)", "feat. X", etc.
def normalize_artist(s: str) -> str
def score(source: Track, cand: Track) -> tuple[float, list[str]]
def match_track(source: Track, target: MusicProvider, *, limit: int = 8) -> MatchResult
def match_tracks(sources, target, *, progress=None) -> list[MatchResult]
```

Scoring: ISRC equality ⇒ 1.0 `exact`. Otherwise weighted rapidfuzz —
title 0.5, artist 0.35, duration 0.15 (full credit ≤2s delta, zero ≥15s),
+0.05 album bonus, penalty when one side is live/remix/karaoke and the other
is not. Thresholds: ≥0.88 `high`, ≥0.70 `low`, else `none`.

## db.py — `Database`

sqlite3 at `config.data_dir()/harmony.db`, `check_same_thread=False` + a lock.
Tables:
- `track_links(src_service, src_id, dst_service, dst_id, score, confidence, created_at)`
  PK `(src_service, src_id, dst_service)` — the durable match cache, symmetric writes.
- `playlist_links(local_id, ytmusic_id, qobuz_id, title, created_at)` — pairs a
  playlist across services so sync knows what mirrors what.
- `snapshots(id, service, playlist_id, taken_at, payload_json)` — backup/diff.
- `kv(key, value_json)` — misc cache (search results, enrichment) with TTL.

Methods: `get_link/put_link/forget_link`, `link_playlists/get_playlist_link/
list_playlist_links/unlink_playlists`, `save_snapshot/list_snapshots/load_snapshot`,
`cache_get(key, max_age_s)/cache_put(key, value)`.

## sync.py

```python
class SyncDirection(Enum): MIRROR_A_TO_B; MIRROR_B_TO_A; TWO_WAY
@dataclass
class SyncAction:  # kind: "add" | "remove" | "unmatched"
    kind: str; target: Service; track: Track; match: MatchResult | None
@dataclass
class SyncPlan:
    source: Playlist; target: Playlist; actions: list[SyncAction]
    def summary(self) -> str

class SyncEngine:
    def __init__(self, providers: dict[Service, MusicProvider], db: Database)
    def plan(self, a: Playlist, b: Playlist, direction, *, progress=None) -> SyncPlan
    def apply(self, plan: SyncPlan, *, progress=None) -> SyncReport
    def clone_playlist(self, src: Playlist, dst_service: Service, *, progress=None) -> SyncPlan
```

Plan is pure (no writes). `apply` performs writes, records links in the db, and
snapshots both playlists first. `progress` is `Callable[[float, str], None]`
where float is 0..1. Two-way = union of both sides; never deletes on TWO_WAY.

## enrich/

Each module is stateless functions over plain strings, HTTP via `requests`,
results cached through `Database.cache_*` (7-day TTL). No API key ⇒ raise
`MissingCredentialError`; MusicBrainz/ListenBrainz need none but require a
descriptive `User-Agent` and ≥1 req/sec rate limiting.

`recommender.Recommender.similar_to_tracks(seed_tracks, provider, limit)` blends
Last.fm similar tracks, ListenBrainz recs, and the provider's own
`similar_tracks`, dedupes, then resolves names back to real catalog tracks via
`matching`.

## ai/claude.py

```python
class PlaylistPlanner:
    def __init__(self, api_key: str | None = None, model: str = "claude-opus-5")
    def plan(self, prompt: str, *, count: int = 25,
             library_hint: list[str] | None = None) -> PlaylistIdea
```
Uses the Anthropic SDK with `output_config={"format": {"type": "json_schema", ...}}`
returning `{title, description, tracks: [{artist, title, why}]}`. The returned
names are then resolved against the real catalog by `matching` — the LLM never
invents IDs. Missing key ⇒ `MissingCredentialError`; the UI degrades gracefully.

## ui/ contract

`ui/window.py` exposes `HarmonyWindow(Adw.ApplicationWindow)` built from an
`Adw.NavigationSplitView`: sidebar rows → `Adw.ViewStack` pages.
Pages, each its own module exporting one `Gtk.Widget` subclass taking
`(app_state)` as its only ctor arg:
- `search_page.SearchPage` — unified search, service toggle, add-to-playlist
- `playlists_page.PlaylistsPage` — browse/create/edit/delete, track list
- `sync_page.SyncPage` — pair playlists, preview plan, resolve ambiguous matches
- `discover_page.DiscoverPage` — similar artists/tracks + NL playlist builder
- `prefs.PreferencesDialog` — accounts, API keys, sync defaults

`AppState` (in `ui/state.py`) holds `providers`, `db`, `sync_engine`,
`settings`, `recommender`, `planner`, and emits GObject signals
`providers-changed`, `playlists-changed`, `toast`.
