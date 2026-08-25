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
    confidence: Literal["exact", "high", "low", "manual", "none"]

def normalize_title(s: str) -> str      # strips "(Remastered 2011)", "feat. X", etc.
def normalize_artist(s: str) -> str
def score(source: Track, cand: Track) -> tuple[float, list[str]]
def match_track(source: Track, target: MusicProvider, *, limit: int = 8,
                 high_threshold: float = HIGH_THRESHOLD,
                 low_threshold: float = LOW_THRESHOLD) -> MatchResult
def match_tracks(sources, target, *, progress=None, db=None,
                  high_threshold: float = HIGH_THRESHOLD,
                  low_threshold: float = LOW_THRESHOLD) -> list[MatchResult]
```

Scoring: ISRC equality ⇒ 1.0, confidence `exact`. `exact` is reserved for
ISRC-verified identity — a merely perfect *fuzzy* score (title/artist/duration
all lining up with no ISRC on one or both sides) tops out at `high`, never
`exact`. Otherwise weighted rapidfuzz — title 0.5, artist 0.35, duration 0.15
(full credit ≤2s delta, zero ≥15s), +0.05 album bonus, penalty when one side
is live/remix/karaoke and the other is not. Thresholds: ≥0.88 `high`, ≥0.70
`low`, else `none`. `manual` is a fifth, separate confidence: a user-resolved
match (recorded via the sync UI's "Use this" flow), score 1.0, never
re-derived or downgraded once cached — a human already made this call and
`match_tracks`' db-cache lookup returns it as-is. Thresholds are user-facing
(`Settings.match_high_threshold` / `match_low_threshold`, edited in
Preferences → Sync) but callers must pass them in explicitly — this module
never imports `harmony.config`, to keep the scoring engine dependency-light
and independently testable.

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
    target_playlist_id: str = ""     # which playlist on `target`; see below
    needs_confirmation: bool = False # "add" only; see auto_accept_high below
@dataclass
class SyncPlan:
    source: Playlist; target: Playlist; actions: list[SyncAction]
    notes: list[str]
    target_track_ids: dict[tuple[Service, str], frozenset[str]]  # see below
    def summary(self) -> str
    def normalise(self) -> None

class SyncEngine:
    def __init__(self, providers: dict[Service, MusicProvider], db: Database,
                 *, high_threshold=..., low_threshold=..., auto_accept_high=True)
    def plan(self, a: Playlist, b: Playlist, direction, *, progress=None) -> SyncPlan
    def apply(self, plan: SyncPlan, *, progress=None) -> SyncReport
    def clone_playlist(self, src: Playlist, dst_service: Service, *, progress=None) -> SyncPlan
```

`SyncAction.target_playlist_id` disambiguates two actions that share the same
`target` service but point at two different playlists on it — e.g. a TWO_WAY
sync between two playlists that both happen to live on the same service.
Grouping/looking up actions by `target` alone would silently merge them;
`plan()` always fills this in, and it defaults to `""` only so hand-built
`SyncAction`s (tests, older callers) don't break.

Plan is pure (no writes). `apply` performs writes, records links in the db, and
snapshots both playlists first. `progress` is `Callable[[float, str], None]`
where float is 0..1. Two-way = union of both sides; never deletes on TWO_WAY.

### Classification

`plan()` classifies every source track exactly once into one of three states.
This is the *only* place classification happens — there is no other bypass or
special case anywhere else in this file, deliberately, after three prior
rounds of one-off patches each reopened a data-loss bug the last one had
closed:

- **RESOLVED_PRESENT** — the counterpart is known (`match.confidence` is one
  of `"exact" | "high" | "manual"`, the module-level `_CONFIDENT_ENOUGH` set)
  and it is already in the target playlist. No action is emitted. The target
  track counts as accounted for, not an orphan.
- **RESOLVED_MISSING** — the counterpart is known but not in the target.
  Emits an `"add"` action. Accounted for, same as RESOLVED_PRESENT.
- **UNDETERMINED** — the counterpart is *not* known: confidence is `"low"` or
  `"none"`, or there is no candidate at all. Emits an `"unmatched"` action for
  the UI to resolve.

"Known" is decided purely by `match.confidence in _CONFIDENT_ENOUGH` — never
by anything derived from `auto_accept_high`. `auto_accept_high` answers a
different question: whether to *write* a known match without asking, not
whether it's known. Collapsing the two (e.g. excluding `"high"` from the
"known" set when `auto_accept_high` is False) demotes an already-mirrored
`"high"` match to UNDETERMINED, which — via the removal rule below — silently
suppresses every removal for the direction for as long as the setting stays
off. `auto_accept_high` False only ever sets
`SyncAction.needs_confirmation = True` on a RESOLVED_MISSING `"high"` add (never
on `"exact"`/`"manual"`, which don't need asking, and never on RESOLVED_PRESENT,
which has nothing to write). `apply()` will not write an action with
`needs_confirmation` set; a caller clears the flag once it has obtained
confirmation, the same way the sync UI already flips an `"unmatched"`
action's `kind` to `"add"` in place after the user resolves it.

For a given direction, if **any** source track's match outcome is
UNDETERMINED, **all** removals for that direction are suppressed — not just
removals that a look at the undetermined tracks might implicate. A removal
means "this target track has no counterpart in the source," which is
unknowable while any source track's match is still undetermined; guessing
which removals would have been safe risks deleting something that an
unresolved match would have accounted for. The plan's `notes` explain the
suppression and its count so the UI can surface it. Once every unmatched row
is resolved (or the plan is re-run after resolution) and no source track is
UNDETERMINED, removals proceed normally for orphaned target tracks.

### Duplicate adds

Classification never special-cases "the match is already in the target" for
an UNDETERMINED track — doing so previously meant a low-confidence guess
whose top candidate happened to coincide with an existing target track was
dropped from the plan with no action, no note, and no way for the user to
know it had happened. Instead, `plan()` captures `SyncPlan.target_track_ids`:
for each `(target service, target playlist id)` touched by the plan, the
track ids that were already present at plan time. `apply()` treats any
`"add"` action whose track id is already in that set as a duplicate — it is
never sent to the provider, and is recorded in `SyncReport.skipped` instead
of `SyncReport.added`. This is what actually prevents a duplicate write,
including the case where the UI resolves an `"unmatched"` row onto a
candidate that turns out to already be in the target, without needing an
extra provider read at apply time.

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
