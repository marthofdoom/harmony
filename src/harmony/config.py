"""Paths, persisted settings, and credential storage.

Secrets go to the system keyring when one is available (GNOME Keyring / KWallet
via the ``keyring`` package). When no backend is usable we fall back to a
0600-mode JSON file under the config dir and say so loudly, because silently
writing a password to a world-readable file would be worse than the warning.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from platformdirs import PlatformDirs

from . import APP_NAME

log = logging.getLogger(__name__)
_dirs = PlatformDirs(appname="harmony", appauthor=False, ensure_exists=True)

KEYRING_SERVICE = "io.github.marthofdoom.Harmony"


# --------------------------------------------------------------------------
# Secret redaction
# --------------------------------------------------------------------------

#: Name *atoms* (regex, not literal strings) for the last underscore/hyphen
#: separated component of a credential-bearing field/header name. Matching is
#: deliberately *suffix*-anchored, not a bare substring test: a name matches
#: only when one of these atoms is its final component (optionally preceded
#: by an arbitrary ``prefix_`` / ``prefix-`` — see ``_SENSITIVE_NAME_RE``
#: below), never when the atom merely appears somewhere inside a longer name.
#: That's what lets ``access_token``, ``refresh_token``, ``client_secret``,
#: ``app-secret``, and header names like ``X-User-Auth-Token`` all get caught
#: by a single ``token``/``secret``/``key`` atom, while a name like
#: ``token_type`` (atom appears as the *first* component, not the last) or
#: ``monkey``/``keyword`` (atom appears mid-word, with no separator marking
#: it as a distinct component) is left alone — the value of a ``token_type``
#: field (e.g. ``"Bearer"``) isn't a secret, and redacting it on top of the
#: real token would just make the redacted output harder to debug for no
#: safety benefit.
_SENSITIVE_NAME_ATOMS = (
    r"password",
    r"username",
    r"email",
    r"authorization",
    r"token",
    r"secret",
    r"key",
)

# A full sensitive name: an optional run of word/dot/hyphen characters ending
# in "_" or "-" (the prefix, e.g. "access_", "x-user-auth-"), followed by one
# of the atoms above. Reused by all three shapes below (query param, JSON/
# dict-repr key, bare "Name: value" text) so they agree on what counts as a
# sensitive name.
_SENSITIVE_NAME_RE = r"(?:[\w.-]*[_-])?(?:" + "|".join(_SENSITIVE_NAME_ATOMS) + r")"

# Matches "?name=value" / "&name=value" for any sensitive name, up to the
# next delimiter. Deliberately loose about what a "value" looks like (it may
# be percent-encoded, e.g. "alice%40example.com") since the point is to strip
# it, not parse it. Anchored on the left by "?"/"&" and on the right by "=",
# so a path segment that merely contains a sensitive word (not followed
# immediately by "=") is never touched.
_SECRET_PARAM_RE = re.compile(r"(?i)([?&]" + _SENSITIVE_NAME_RE + r"=)[^&\s'\")]*")

# Matches a JSON-object or Python-dict-repr key/value pair for any sensitive
# name, e.g. '"api_key": "abc123"' or "'password': 'hunter2'" or
# "'X-User-Auth-Token': 'abc'" — response bodies, repr()'d request payloads,
# and dumped headers dicts all carry secrets this way rather than as a URL
# query string. Anchored by the surrounding quotes, so this can't match a
# sensitive word appearing mid-value rather than as the key.
_SECRET_JSON_RE = re.compile(
    r"(?i)(['\"]" + _SENSITIVE_NAME_RE + r"['\"]\s*:\s*)(['\"])[^'\"]*\2"
)

# Matches a bare (unquoted) "Name: value" header rendered into free text —
# urllib3's own debug logging, a raw header dump, etc. The negative lookbehind
# requires the name to start at a non-word/dot/hyphen boundary (start of
# string/line, a space, a brace, ...) so this can't match a sensitive atom
# appearing mid-identifier (e.g. it must not fire on "operationalsecret:"
# just because it ends in "secret:").
_SECRET_HEADER_RE = re.compile(r"(?i)(?<![\w.-])(" + _SENSITIVE_NAME_RE + r"\s*:\s*)[^\r\n'\")]+")


def redact_secrets(text: str) -> str:
    """Redact secret-bearing query-params, JSON/dict fields, and auth headers
    from ``text``.

    ``requests``/``urllib3`` embed the full request URL — including query
    params — in their own exception text (e.g. "Max retries exceeded with
    url: /user/login?...&password=..."). Since that text gets wrapped
    verbatim into our own exceptions and those exceptions get logged (and, in
    the Qobuz case, shown directly in the Preferences UI), every string that
    might contain a URL or response body built from credentials must be
    passed through this before it's used in a message. Safe to call on text
    with no secrets in it — it's then a no-op.
    """
    if not text:
        return text
    text = _SECRET_PARAM_RE.sub(lambda m: m.group(1) + "REDACTED", text)
    text = _SECRET_JSON_RE.sub(lambda m: m.group(1) + m.group(2) + "REDACTED" + m.group(2), text)
    text = _SECRET_HEADER_RE.sub(lambda m: m.group(1) + "REDACTED", text)
    return text


def redact_exception(exc: BaseException) -> BaseException:
    """Build a redaction-safe stand-in for ``exc`` to use as a chained ``__cause__``.

    ``raise ProviderError(redact_secrets(str(exc))) from exc`` still leaves
    the *original* ``exc`` object — with its unredacted message — reachable
    as ``__cause__``. Anything that renders the full chain (``log.exception``,
    ``traceback.format_exception``, an unhandled-exception hook) prints that
    original message verbatim, defeating the redaction done on the new
    exception's own message. Chain from this instead: same exception type,
    same *redacted* message, so the logged chain still shows what kind of
    transport failure happened without ever holding a live secret. Falls back
    to a plain ``RuntimeError`` if ``type(exc)`` can't be constructed from a
    single message string (some exception types need extra args/kwargs).
    """
    redacted_msg = redact_secrets(str(exc))
    try:
        return type(exc)(redacted_msg)
    except Exception:  # noqa: BLE001 - constructor shape varies across libs
        return RuntimeError(redacted_msg)


def config_dir() -> Path:
    return Path(_dirs.user_config_dir)


def data_dir() -> Path:
    return Path(_dirs.user_data_dir)


def cache_dir() -> Path:
    return Path(_dirs.user_cache_dir)


def settings_path() -> Path:
    return config_dir() / "settings.json"


def _fallback_secrets_path() -> Path:
    return config_dir() / "secrets.json"


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


@dataclass
class Settings:
    """User preferences. Non-secret values only — secrets live in the keyring."""

    # Accounts
    ytmusic_auth_file: str = ""          # path to browser.json / oauth.json
    ytmusic_auth_kind: str = "browser"   # "browser" | "oauth"
    ytmusic_oauth_client_id: str = ""
    qobuz_email: str = ""
    qobuz_auth_kind: str = "password"    # "password" | "token"
    qobuz_token_saved: bool = False      # non-secret "a token has been pasted" flag; the
                                          # token itself lives in the keyring (QOBUZ_TOKEN)
    qobuz_app_id: str = ""               # blank => scrape from the web player

    # Matching
    match_high_threshold: float = 0.88
    match_low_threshold: float = 0.70
    auto_accept_high: bool = True

    # Sync
    default_direction: str = "two-way"
    snapshot_before_sync: bool = True

    # Enrichment / AI
    lastfm_enabled: bool = True
    listenbrainz_enabled: bool = True
    musicbrainz_enabled: bool = True
    ai_enabled: bool = False
    ai_model: str = "claude-opus-5"
    contact_email: str = ""              # used in the MusicBrainz User-Agent

    # Lidarr acquisition ("Get with Lidarr"). URL of the Lidarr instance; the API
    # key is a secret (LIDARR_API_KEY in the keyring). Root folder + profile ids
    # are optional — blank means "use Lidarr's first configured value".
    lidarr_enabled: bool = False
    lidarr_url: str = ""                  # e.g. http://192.168.1.10:8686
    lidarr_root_folder: str = ""
    lidarr_quality_profile_id: int = 0    # 0 => first configured
    lidarr_metadata_profile_id: int = 0   # 0 => first configured
    # Lidarr's paths as seen from THIS machine, when they differ (Lidarr in a
    # container / on another host): [{"remote": "/music", "local": "/mnt/media/music"}].
    # Same idea as the *arr "remote path mappings". Applied to webhook paths and
    # to Lidarr's root folders when they're adopted as library folders.
    lidarr_path_map: list[dict[str, str]] = field(default_factory=list)
    # Without a Lidarr of its own, use the one configured on the mesh (the
    # server next to Lidarr). Off = no "Get with Lidarr" on this instance.
    lidarr_mesh: bool = True

    # Local library (harmony.library): music folders this instance indexes and
    # serves as the "local" service — where Lidarr's imports land. Server/web.
    library_enabled: bool = False
    library_paths: list[str] = field(default_factory=list)
    # Play a song from the Library (this instance's or a peer's) when it's
    # there, even when it was picked from a Qobuz/YouTube Music page.
    library_preferred: bool = True
    # Game source API (harmony.game_source, docs/api/game-source.md): lets a game
    # engine use this instance as a proxy music source. ``game_profiles`` maps a
    # profile name to its validated definition (music types -> selectors,
    # sources, WAV format); the WAV cache is capped at ``game_cache_max_mb``.
    game_source_enabled: bool = True
    game_profiles: dict[str, dict[str, Any]] = field(default_factory=dict)
    game_cache_max_mb: int = 2048
    # Candidate depth per streaming search for ``genre``/``query`` selectors
    # (clamped 10-300 when read), the extra "<genre>" query variants + YouTube
    # Music mood playlists, and the persisted no-repeat history (game-history.json).
    game_stream_search_limit: int = 100
    game_genre_variants: bool = True
    game_history_enabled: bool = True
    # Personal key: a shared secret the user sets identically on all their
    # Harmony instances/apps. In the federated mesh, a signed-out client
    # discovers instances on the LAN and may use one as its backend (sharing its
    # held credentials) only if the personal keys match. Blank = no key set.
    personal_key: str = ""
    # The API server (always on; the mesh backend) binds this port. Changing it
    # takes effect on next launch.
    server_port: int = 8080

    # UI
    window_width: int = 1280
    window_height: int = 820
    window_maximized: bool = False
    last_page: str = "search"
    theme: str = "system"                # id from harmony.ui.theming.THEMES

    # Devices (WiiM/LinkPlay playback renderers). Non-secret: host, a
    # user/device-supplied display name, and a backend discriminator
    # ("wiim" today; future-proofed for other PlaybackDevice backends the
    # same way harmony.playback.DeviceInfo.kind is).
    known_devices: list[dict[str, Any]] = field(default_factory=list)

    # Manually-registered peer instances (host, port, optional name). Complements
    # mDNS mesh discovery for peers that don't share a broadcast domain — a hub
    # reached over Tailscale/WireGuard/a routed subnet, where multicast never
    # arrives. Non-secret; adoption/routing still gate on the personal key.
    known_peers: list[dict[str, Any]] = field(default_factory=list)

    # Stable identity of THIS Harmony instance (uuid4, created on first need), so
    # peers can tell "same server, another address" from "another server".
    instance_id: str = ""

    _extra: dict[str, Any] = field(default_factory=dict, repr=False)
    # Field values as last read from / written to disk: the base of save()'s
    # three-way merge (None for a Settings built by hand).
    _base: dict[str, Any] | None = field(default=None, repr=False)
    # Public fields assigned since the last load/save/reload (see __setattr__).
    _dirty: set[str] = field(default_factory=set, repr=False, compare=False)
    # The settings file existed but couldn't be parsed when this was loaded:
    # we run on defaults and must not overwrite the (recoverable) file.
    _load_failed: bool = field(default=False, repr=False, compare=False)

    def __setattr__(self, name: str, value: Any) -> None:
        object.__setattr__(self, name, value)
        if not name.startswith("_"):
            dirty = self.__dict__.get("_dirty")
            if dirty is not None:
                dirty.add(name)

    def _snapshot(self) -> dict[str, Any]:
        return copy.deepcopy({k: getattr(self, k) for k in self.__dataclass_fields__
                              if not k.startswith("_")})

    @classmethod
    def _fresh(cls) -> Settings:
        inst = cls()
        inst._base = inst._snapshot()
        inst._dirty = set()
        return inst

    @classmethod
    def load(cls) -> Settings:
        path = settings_path()
        if not path.exists():
            return cls._fresh()
        try:
            text = path.read_text("utf-8")
            if not text.strip():
                return cls._fresh()  # a blank file holds nothing: safe to (re)write
            raw = json.loads(text)
            if not isinstance(raw, dict):
                raise ValueError("not a JSON object")
        except (OSError, ValueError) as exc:  # JSONDecodeError and UnicodeDecodeError are ValueErrors
            log.warning("Could not read %s (%s); starting from defaults", path, exc)
            inst = cls._fresh()
            inst._load_failed = True
            return inst
        known = {f for f in cls.__dataclass_fields__ if not f.startswith("_")}
        extra = {k: v for k, v in raw.items() if k not in known}
        inst = cls(**{k: v for k, v in raw.items() if k in known}, _extra=extra)
        inst._base = inst._snapshot()
        inst._dirty = set()
        return inst

    def reload(self) -> None:
        """Re-read the file into THIS object, in place.

        Something else (the in-process engine's credential sync) can write the
        settings file while a long-lived holder keeps its own copy; reloading in
        place keeps every reference current — and stops the next ``save()`` from
        clobbering the newer values on disk with stale ones."""
        fresh = type(self).load()
        for name in self.__dataclass_fields__:
            object.__setattr__(self, name, getattr(fresh, name))
        self._dirty = set()

    def _merge_disk(self) -> None:
        """Fields this object never changed since it last synced with the file
        take the file's (newer) value, so a long-lived copy saving its own
        change (a page switch) can't clobber what another writer — the engine
        adding a peer — put on disk meanwhile.

        "Changed" = assigned since the sync (even back to its original value)
        OR a value that differs from the synced one (in-place edits, e.g.
        ``known_peers.append``)."""
        if self._base is None:
            return
        try:
            disk = json.loads(settings_path().read_text("utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(disk, dict):
            return
        for k, base_v in self._base.items():
            if k in self._dirty or getattr(self, k) != base_v:
                continue
            if k in disk and disk[k] != base_v:
                object.__setattr__(self, k, disk[k])  # a merge isn't a user assignment

    def save(self) -> None:
        path = settings_path()
        with _SAVE_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            if _unreadable(path):
                _keep_corrupt_copy(path)
                log.warning("Not saving settings: %s exists but can't be read; "
                            "leaving it untouched (a copy is kept next to it)", path)
                return
            dirty = set(self._dirty)
            self._merge_disk()
            payload = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
            payload.update(self._extra)
            # A name unique to this process+thread: concurrent saves can't
            # clobber each other's temp file.
            tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), "utf-8")
                tmp.replace(path)
            finally:
                tmp.unlink(missing_ok=True)
            self._base = self._snapshot()
            self._dirty -= dirty


#: Serialises read-merge-write across threads of this process.
_SAVE_LOCK = threading.Lock()


def _unreadable(path: Path) -> bool:
    """The file exists, isn't empty, and can't be parsed as a JSON object."""
    try:
        text = path.read_text("utf-8")
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True
    if not text.strip():
        return False  # an empty file (an interrupted write) holds nothing to lose
    try:
        return not isinstance(json.loads(text), dict)
    except ValueError:
        return True


def _keep_corrupt_copy(path: Path) -> None:
    try:
        data = path.read_bytes()
        backup = path.with_name(path.name + ".corrupt")
        if backup.exists() and backup.read_bytes() != data:
            backup = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        if not backup.exists():
            backup.write_bytes(data)
    except OSError as exc:
        log.warning("Couldn't keep a copy of the unreadable %s: %s", path, exc)


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------


class CredentialStore:
    """Keyring-backed secret storage with an explicit, guarded file fallback."""

    _lock = threading.Lock()

    def __init__(self) -> None:
        self._backend_ok = self._probe_keyring()
        if not self._backend_ok:
            log.warning(
                "No usable system keyring found — secrets will be stored in %s "
                "with 0600 permissions. Install gnome-keyring or kwallet for "
                "encrypted storage.",
                _fallback_secrets_path(),
            )

    @staticmethod
    def _probe_keyring() -> bool:
        try:
            import keyring
            from keyring.backends.fail import Keyring as FailKeyring

            backend = keyring.get_keyring()
            if isinstance(backend, FailKeyring):
                return False
            # chainer with no working children also fails at set() time
            keyring.get_password(KEYRING_SERVICE, "__probe__")
            return True
        except Exception as exc:  # noqa: BLE001 - any backend failure means fallback
            log.debug("Keyring unavailable: %s", exc)
            return False

    @property
    def uses_keyring(self) -> bool:
        return self._backend_ok

    def get(self, key: str) -> str | None:
        # Environment injection wins: a server/container can supply secrets as
        # ``HARMONY_<KEY>`` without a keyring or a file on disk (used to seed a
        # headless instance). The credential keys use dotted names
        # (``qobuz.user_auth_token``); the env var uppercases them and replaces
        # ``.``/``-`` with ``_`` → ``HARMONY_QOBUZ_USER_AUTH_TOKEN``.
        env = os.environ.get("HARMONY_" + key.upper().replace(".", "_").replace("-", "_"))
        if env:
            return env
        if self._backend_ok:
            try:
                import keyring

                return keyring.get_password(KEYRING_SERVICE, key)
            except Exception as exc:  # noqa: BLE001
                log.warning("Keyring read failed for %s: %s", key, exc)
                return None
        return self._file_secrets().get(key)

    def set(self, key: str, value: str) -> None:
        if self._backend_ok:
            try:
                import keyring

                keyring.set_password(KEYRING_SERVICE, key, value)
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("Keyring write failed for %s: %s", key, exc)
        self._write_file_secret(key, value)

    def delete(self, key: str) -> None:
        if self._backend_ok:
            try:
                import keyring

                keyring.delete_password(KEYRING_SERVICE, key)
                return
            except Exception:  # noqa: BLE001 - absent entry is not an error
                pass
        self._write_file_secret(key, None)

    # -- file fallback ----------------------------------------------------

    def _passphrase(self) -> str | None:
        """The personal key, used to encrypt the file store at rest."""
        try:
            return Settings.load().personal_key or None
        except Exception:  # noqa: BLE001
            return None

    def _file_secrets(self) -> dict[str, str]:
        path = _fallback_secrets_path()
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        # Encrypted-at-rest envelope (see _write_all)? Decrypt with the key.
        if isinstance(raw, dict) and raw.get("v") and "token" in raw and "salt" in raw:
            passphrase = self._passphrase()
            if not passphrase:
                log.warning("secrets file is encrypted but no personal key is set")
                return {}
            try:
                from harmony.cryptobox import decrypt_json

                data = decrypt_json(raw, passphrase)
            except Exception:  # noqa: BLE001
                log.warning("couldn't decrypt the secrets file (personal key changed?)")
                return {}
            return data if isinstance(data, dict) else {}
        return raw if isinstance(raw, dict) else {}  # plaintext (no key / legacy)

    def all_secrets(self) -> dict[str, str]:
        """Every file-stored secret, decrypted — for re-encrypting on key change."""
        with self._lock:
            return dict(self._file_secrets())

    def replace_all(self, data: dict[str, str]) -> None:
        """Overwrite the whole file store, re-encrypting with the current key."""
        with self._lock:
            self._write_all(dict(data))

    def _write_file_secret(self, key: str, value: str | None) -> None:
        with self._lock:
            data = self._file_secrets()
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
            self._write_all(data)

    def _write_all(self, data: dict[str, str]) -> None:
        path = _fallback_secrets_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        passphrase = self._passphrase()
        if passphrase:
            # Encrypt the whole store with a key derived from the personal key,
            # so on-disk secrets aren't plaintext (matches the transport
            # encryption). Before a key is set, fall back to plaintext 0600.
            from harmony.cryptobox import encrypt_json

            payload = json.dumps(encrypt_json(data, passphrase), indent=2).encode("utf-8")
        else:
            payload = json.dumps(data, indent=2).encode("utf-8")
        tmp = path.with_suffix(".json.tmp")
        # Create the temp file already restricted to 0600 (via os.open, not
        # write_text -> open() at 0666-minus-umask), and O_EXCL after an unlink
        # so a pre-existing symlink at that path isn't followed.
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        tmp.replace(path)
        os.chmod(path, 0o600)


# Well-known credential keys
QOBUZ_PASSWORD = "qobuz.password"
QOBUZ_TOKEN = "qobuz.user_auth_token"
QOBUZ_APP_SECRET = "qobuz.app_secret"
YTMUSIC_OAUTH_SECRET = "ytmusic.oauth_client_secret"
LASTFM_API_KEY = "lastfm.api_key"
LIDARR_API_KEY = "lidarr.api_key"
ANTHROPIC_API_KEY = "anthropic.api_key"


def user_agent(contact_email: str | None = None) -> str:
    """Build the User-Agent every outbound HTTP call identifies itself with.

    MusicBrainz's API etiquette (and ListenBrainz's, which follows the same
    convention) explicitly asks for a contact address in the User-Agent so
    they have a way to reach an app's maintainer/user about problematic
    usage, not just a project URL. When the user has set one in Preferences
    → Integrations (``Settings.contact_email``), append it; otherwise this is
    unchanged from before.

    ``contact_email`` lets a caller that already holds a live ``Settings``
    object (e.g. a provider constructed with one, per the threading rule)
    pass it straight through with zero I/O. When omitted, this loads
    ``Settings`` fresh off disk to pick the value up anyway — safe because
    every call site that omits it (the shared enrich HTTP helper and
    whatever goes through it) only ever runs on a worker thread already,
    same as the network call it's building a header for; never at
    construction/startup on the main loop.
    """
    from . import __version__

    base = f"{APP_NAME}/{__version__} (+https://github.com/marthofdoom/harmony)"
    contact = contact_email if contact_email is not None else Settings.load().contact_email
    contact = (contact or "").strip()
    return f"{base} ( {contact} )" if contact else base
