"""Paths, persisted settings, and credential storage.

Secrets go to the system keyring when one is available (GNOME Keyring / KWallet
via the ``keyring`` package). When no backend is usable we fall back to a
0600-mode JSON file under the config dir and say so loudly, because silently
writing a password to a world-readable file would be worse than the warning.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
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

    # UI
    window_width: int = 1280
    window_height: int = 820
    window_maximized: bool = False
    last_page: str = "search"

    # Devices (WiiM/LinkPlay playback renderers). Non-secret: host, a
    # user/device-supplied display name, and a backend discriminator
    # ("wiim" today; future-proofed for other PlaybackDevice backends the
    # same way harmony.playback.DeviceInfo.kind is).
    known_devices: list[dict[str, Any]] = field(default_factory=list)

    _extra: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def load(cls) -> Settings:
        path = settings_path()
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Could not read %s (%s); starting from defaults", path, exc)
            return cls()
        known = {f for f in cls.__dataclass_fields__ if not f.startswith("_")}
        extra = {k: v for k, v in raw.items() if k not in known}
        return cls(**{k: v for k, v in raw.items() if k in known}, _extra=extra)

    def save(self) -> None:
        path = settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
        payload.update(self._extra)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), "utf-8")
        tmp.replace(path)


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

    def _file_secrets(self) -> dict[str, str]:
        path = _fallback_secrets_path()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_file_secret(self, key: str, value: str | None) -> None:
        with self._lock:
            path = _fallback_secrets_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            data = self._file_secrets()
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
            payload = json.dumps(data, indent=2).encode("utf-8")
            tmp = path.with_suffix(".json.tmp")
            # Create the temp file already restricted to 0600 (via os.open,
            # not pathlib's write_text -> builtin open(), which defaults to
            # 0666 and relies on the process umask to narrow it — leaving a
            # window where a concurrently-running umask-022 process sees a
            # world-readable secrets file). O_EXCL after an explicit unlink
            # also avoids following a pre-existing symlink at that path.
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
