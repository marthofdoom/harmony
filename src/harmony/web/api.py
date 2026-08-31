"""Engine-facing helpers for the web API: build providers, search, browse
playlists, resolve streams, and serialize models to JSON. GTK-free.

Provider calls are serialized behind a lock: the ``MusicProvider`` contract
allows one worker thread at a time, and the threaded HTTP server would otherwise
call them concurrently. Fine for a single-user hub; revisit if it ever needs
real concurrency.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

# Resolved provider stream URLs expire (Qobuz/YouTube sign them for ~minutes),
# so tokens the browser holds are short-lived and pruned.
_STREAM_TTL_S = 1800


def _artists(obj: Any) -> str:
    names = getattr(obj, "artists", None)
    return ", ".join(names) if names else ""


def track_to_dict(t: Any) -> dict[str, Any]:
    return {
        "id": t.id,
        "title": t.title,
        "service": t.service.value,
        "artist": t.artist_name,
        "album": t.album,
        "duration_s": t.duration_s,
        "artwork_url": t.artwork_url,
    }


def playlist_to_dict(p: Any) -> dict[str, Any]:
    return {
        "id": p.id,
        "title": p.title,
        "service": p.service.value,
        "track_count": p.track_count,
        "owner": p.owner,
        "artwork_url": p.artwork_url,
    }


def album_to_dict(a: Any) -> dict[str, Any]:
    return {"id": a.id, "title": a.title, "service": a.service.value,
            "artist": _artists(a), "year": a.year, "artwork_url": a.artwork_url}


def artist_to_dict(a: Any) -> dict[str, Any]:
    return {"id": a.id, "name": a.name, "service": a.service.value}


class Engine:
    """The web server's handle on the engine: providers + a stream-token table."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._providers: dict[Any, Any] | None = None
        self._streams: dict[str, dict[str, Any]] = {}

    # -- providers ----------------------------------------------------------

    def _ensure_providers(self) -> dict[Any, Any]:
        if self._providers is None:
            from harmony.config import CredentialStore, Settings
            from harmony.providers import build_providers

            providers = build_providers(Settings.load(), CredentialStore())
            # build_providers constructs but does not authenticate; load each
            # provider's stored credentials (e.g. the Qobuz token) so search,
            # is_authenticated, and streaming all reflect reality -- the desktop
            # does the same warm-up at startup. ``has_credentials`` is a
            # Qobuz-only *property* (YT lacks it), so read it via getattr with a
            # True default -- never call it -- and let authenticate() fail-fast
            # for unconfigured accounts.
            for svc, prov in providers.items():
                try:
                    if getattr(prov, "has_credentials", True):
                        prov.authenticate()
                except Exception as exc:  # noqa: BLE001 - one service failing is isolated
                    log.info("provider %s not authenticated: %s", svc.value, exc)
            self._providers = providers
            log.info(
                "providers ready: %s",
                [f"{s.value}={'authed' if p.is_authenticated else 'signed-out'}"
                 for s, p in providers.items()],
            )
        return self._providers

    def _provider(self, service_value: str) -> Any | None:
        for svc, prov in self._ensure_providers().items():
            if svc.value == service_value:
                return prov
        return None

    def accounts(self) -> dict[str, Any]:
        out = []
        with self._lock:
            for svc, prov in self._ensure_providers().items():
                try:
                    authed = bool(prov.is_authenticated)
                except Exception:  # noqa: BLE001
                    authed = False
                name = None
                if authed:
                    try:
                        name = prov.account_name()
                    except Exception:  # noqa: BLE001
                        name = None
                out.append({"service": svc.value, "authenticated": authed, "account": name})
        return {"accounts": out}

    # -- preferences --------------------------------------------------------

    def preferences(self) -> dict[str, Any]:
        from harmony.config import Settings

        s = Settings.load()
        return {"personal_key": s.personal_key}

    def set_preferences(self, personal_key: str | None = None) -> dict[str, Any]:
        from harmony.config import Settings

        s = Settings.load()
        if personal_key is not None:
            s.personal_key = personal_key.strip()
        s.save()
        return self.preferences()

    # -- credential management (seed the server; clients share these) -------

    def set_qobuz_token(self, token: str) -> dict[str, Any]:
        from harmony import config
        from harmony.config import CredentialStore, Settings

        settings = Settings.load()
        settings.qobuz_auth_kind = "token"
        settings.qobuz_token_saved = True
        settings.save()
        CredentialStore().set(config.QOBUZ_TOKEN, token.strip())
        self._providers = None  # re-warm with the new credential
        return self.accounts()

    def set_ytmusic_browser(self, headers_raw: str) -> dict[str, Any]:
        import ytmusicapi

        from harmony import config
        from harmony.config import Settings

        settings = Settings.load()
        path = settings.ytmusic_auth_file or str(config.config_dir() / "browser.json")
        ytmusicapi.setup(filepath=path, headers_raw=headers_raw)
        settings.ytmusic_auth_file = path
        settings.ytmusic_auth_kind = "browser"
        settings.save()
        self._providers = None
        return self.accounts()

    def signout(self, service_value: str) -> dict[str, Any]:
        from harmony import config
        from harmony.config import CredentialStore, Settings

        settings = Settings.load()
        if service_value == "qobuz":
            settings.qobuz_token_saved = False
            CredentialStore().delete(config.QOBUZ_TOKEN)
        elif service_value == "ytmusic":
            settings.ytmusic_auth_file = ""
        else:
            raise KeyError(service_value)
        settings.save()
        self._providers = None
        return self.accounts()

    # -- queries ------------------------------------------------------------

    def search(self, query: str, kinds: tuple[str, ...], limit: int = 25) -> dict[str, Any]:
        results: dict[str, list] = {"tracks": [], "albums": [], "artists": [], "playlists": []}
        with self._lock:
            for svc, prov in self._ensure_providers().items():
                try:
                    r = prov.search(query, kinds=kinds, limit=limit)
                except Exception as exc:  # noqa: BLE001 - one service failing must not kill search
                    log.warning("search failed for %s: %s", svc.value, exc)
                    continue
                results["tracks"] += [track_to_dict(t) for t in r.tracks]
                results["albums"] += [album_to_dict(a) for a in r.albums]
                results["artists"] += [artist_to_dict(a) for a in r.artists]
                results["playlists"] += [playlist_to_dict(p) for p in r.playlists]
        return results

    def playlists(self) -> dict[str, Any]:
        out = []
        with self._lock:
            for svc, prov in self._ensure_providers().items():
                try:
                    out += [playlist_to_dict(p) for p in prov.list_playlists()]
                except Exception as exc:  # noqa: BLE001
                    log.warning("list_playlists failed for %s: %s", svc.value, exc)
        return {"playlists": out}

    def playlist_tracks(self, service_value: str, playlist_id: str) -> dict[str, Any]:
        prov = self._provider(service_value)
        if prov is None:
            raise KeyError(service_value)
        with self._lock:
            tracks = prov.get_playlist_tracks(playlist_id)
        return {"tracks": [track_to_dict(t) for t in tracks]}

    # -- streaming ----------------------------------------------------------

    def resolve(self, service_value: str, track_id: str) -> dict[str, Any]:
        """Resolve a track to a short-lived stream token the browser can play."""
        prov = self._provider(service_value)
        if prov is None:
            raise KeyError(service_value)
        with self._lock:
            source = prov.resolve_stream(track_id, max_quality=True)
        token = secrets.token_urlsafe(16)
        self._streams[token] = {
            "url": source.url,
            "headers": dict(source.headers),
            "mime": source.mime_type,
            "at": time.monotonic(),
        }
        self._prune()
        return {"token": token, "mime": source.mime_type, "label": source.label}

    def stream_for(self, token: str) -> dict[str, Any] | None:
        meta = self._streams.get(token)
        if meta is None or time.monotonic() - meta["at"] > _STREAM_TTL_S:
            return None
        return meta

    def _prune(self) -> None:
        cutoff = time.monotonic() - _STREAM_TTL_S
        for token in [k for k, v in self._streams.items() if v["at"] < cutoff]:
            self._streams.pop(token, None)
