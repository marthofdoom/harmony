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
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from platformdirs import PlatformDirs

from . import APP_NAME

log = logging.getLogger(__name__)
_dirs = PlatformDirs(appname="harmony", appauthor=False, ensure_exists=True)

KEYRING_SERVICE = "io.github.marthofdoom.Harmony"


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
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2), "utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(path)
            os.chmod(path, 0o600)


# Well-known credential keys
QOBUZ_PASSWORD = "qobuz.password"
QOBUZ_TOKEN = "qobuz.user_auth_token"
QOBUZ_APP_SECRET = "qobuz.app_secret"
YTMUSIC_OAUTH_SECRET = "ytmusic.oauth_client_secret"
LASTFM_API_KEY = "lastfm.api_key"
ANTHROPIC_API_KEY = "anthropic.api_key"


def user_agent() -> str:
    from . import __version__

    return f"{APP_NAME}/{__version__} (+https://github.com/marthofdoom/harmony)"
