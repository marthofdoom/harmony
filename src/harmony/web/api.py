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

from harmony.errors import ProviderError

log = logging.getLogger(__name__)


def _classify_yt_auth(text: str | None) -> str | None:
    """Infer the YouTube auth kind from an auth-file blob: ``"oauth"`` when it
    holds an OAuth token, ``"browser"`` when it's request headers, else ``None``.

    Credential sharing keys off this so a copy can never label an instance
    ``ytmusic_auth_kind=oauth`` without an actual OAuth token behind it — the
    mismatch that leaves an instance "authenticated" against a dead cookie file.
    """
    if not text:
        return None
    import json

    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("refresh_token") or data.get("access_token"):
        return "oauth"
    if any(isinstance(k, str) and k.lower() in ("cookie", "authorization") for k in data):
        return "browser"
    return None

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
        self._cast: Any | None = None
        self._db: Any | None = None
        self._plans: dict[str, Any] = {}
        self._mesh: Any | None = None
        self._onboard: Any | None = None
        self._audio_router: Any | None = None
        self._devices_cache: tuple[float, list[dict[str, Any]]] | None = None

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
        from harmony.config import Settings

        settings = Settings.load()
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
                # A provider is "stale" when the user configured it but the
                # session no longer works, so the UI can prompt a reconnect
                # instead of lying. YouTube's is_authenticated only means "a
                # client was built", not that the cookies are valid -- an expired
                # session yields no account name. Qobuz: a token was saved but
                # authentication failed (revoked/expired token).
                if svc.value == "ytmusic":
                    stale = authed and name is None
                elif svc.value == "qobuz":
                    stale = not authed and settings.qobuz_token_saved
                else:
                    stale = False
                out.append({"service": svc.value, "authenticated": authed,
                            "account": name, "stale": stale})
        return {"accounts": out}

    # -- preferences --------------------------------------------------------

    def preferences(self) -> dict[str, Any]:
        from harmony.config import Settings

        s = Settings.load()
        return {"personal_key": s.personal_key}

    def check_key(self, provided: str | None) -> bool:
        """Authorize a request against the instance's personal key.

        Open when no key is set (the default -- fully public on a trusted
        network). When a key is set, a client must present the matching key
        (the mesh's credential-sharing gate); this is how a signed-out app uses
        an instance whose key it has been given.
        """
        import hmac

        from harmony.config import Settings

        required = Settings.load().personal_key
        if not required:
            return True
        if not provided:
            return False
        # Constant-time compare so a network attacker can't recover the key one
        # byte at a time from response timing.
        return hmac.compare_digest(provided, required)

    def set_preferences(self, personal_key: str | None = None) -> dict[str, Any]:
        from harmony.config import CredentialStore, Settings

        s = Settings.load()
        key_changed = personal_key is not None and personal_key.strip() != s.personal_key
        # The file store is encrypted with the personal key, so a key change
        # means re-encrypting it: read under the old key first, rewrite under the
        # new one (otherwise the store would be unreadable afterwards).
        old_creds = CredentialStore().all_secrets() if key_changed else None
        if personal_key is not None:
            s.personal_key = personal_key.strip()
        s.save()
        if key_changed and old_creds:
            CredentialStore().replace_all(old_creds)
        # A full instance with a fresh matching key and no accounts of its own
        # pulls credentials from a peer — the "full clients copy creds" model.
        if key_changed and s.personal_key:
            import threading

            threading.Thread(target=self.maybe_adopt_credentials, daemon=True).start()
        return self.preferences()

    # -- credential custody: full instances copy creds when keys match ------

    _CRED_KEYS = (
        "qobuz.user_auth_token", "qobuz.app_secret", "qobuz.password",
        "ytmusic.oauth_client_secret", "lastfm.api_key", "anthropic.api_key",
    )
    _CRED_SETTINGS = (
        "qobuz_auth_kind", "qobuz_token_saved",
        "ytmusic_auth_kind", "ytmusic_oauth_client_id",
    )

    def _collect_credentials(self) -> dict[str, Any]:
        """Everything a full instance needs to become an independent credential
        holder: the secrets, provider settings, and the YouTube auth file."""
        from pathlib import Path

        from harmony.config import CredentialStore, Settings

        cs = CredentialStore()
        s = Settings.load()
        secrets = {k: v for k in self._CRED_KEYS if (v := cs.get(k))}
        yt_auth = None
        if s.ytmusic_auth_file:
            try:
                yt_auth = Path(s.ytmusic_auth_file).read_text("utf-8")
            except OSError:
                pass
        settings = {f: getattr(s, f) for f in self._CRED_SETTINGS if hasattr(s, f)}
        # Only share a YouTube auth kind that matches the token we're actually
        # sending — never a phantom "oauth" label without a token behind it.
        kind = _classify_yt_auth(yt_auth)
        if kind:
            settings["ytmusic_auth_kind"] = kind
        else:
            settings.pop("ytmusic_auth_kind", None)
            settings.pop("ytmusic_oauth_client_id", None)
            yt_auth = None
        return {"secrets": secrets, "settings": settings, "ytmusic_auth": yt_auth}

    def export_credentials(self) -> dict[str, Any]:
        """Encrypted envelope of the credentials, keyed by the personal key — so
        the secrets are confidential on the wire even over plain HTTP. Only a
        holder of the matching key can decrypt it (and the route already refuses
        callers without it)."""
        from harmony.config import Settings
        from harmony.cryptobox import encrypt_json

        key = Settings.load().personal_key
        if not key:
            raise ProviderError("a personal key is required to share credentials")
        return encrypt_json(self._collect_credentials(), key)

    def import_credentials(self, data: dict[str, Any]) -> dict[str, Any]:
        """Write credentials pulled from a peer into this instance's own store."""
        from pathlib import Path

        from harmony.config import CredentialStore, Settings, config_dir

        cs = CredentialStore()
        for key, value in (data.get("secrets") or {}).items():
            if value:
                cs.set(key, value)
        s = Settings.load()

        # Never relabel our YouTube auth 'oauth' without a real token in the
        # payload — the mismatch that left a peer authed against a dead file.
        yt_auth = data.get("ytmusic_auth")
        kind = _classify_yt_auth(yt_auth)
        incoming = dict(data.get("settings") or {})
        if not kind:
            incoming.pop("ytmusic_auth_kind", None)
            incoming.pop("ytmusic_oauth_client_id", None)
            yt_auth = None
        for field, value in incoming.items():
            if hasattr(s, field):
                setattr(s, field, value)

        if yt_auth and kind:
            old_path = s.ytmusic_auth_file
            path = config_dir() / "ytmusic-auth.json"
            path.write_text(yt_auth, "utf-8")
            s.ytmusic_auth_file = str(path)
            s.ytmusic_auth_kind = kind  # keep the kind consistent with the token we wrote
            # Drop a superseded auth file we own, so no stale cookie file lingers.
            if old_path and old_path != str(path):
                old = Path(old_path)
                try:
                    if old.is_file() and config_dir() in old.parents:
                        old.unlink()
                except OSError:
                    pass
        s.save()
        self._reset_providers()
        return {"ok": True, "imported": sorted((data.get("secrets") or {}).keys())}

    def adopt_from_peer(self, host: str, port: int) -> dict[str, Any]:
        """Pull a peer's (encrypted) credentials, decrypt with our personal key,
        and store them locally."""
        import requests

        from harmony.config import Settings
        from harmony.cryptobox import decrypt_json

        key = Settings.load().personal_key or None
        if not key:
            raise ProviderError("set a personal key before adopting credentials.")
        try:
            resp = requests.get(f"http://{host}:{port}/api/credentials/export",
                                headers={"X-Harmony-Key": key}, timeout=10)
        except requests.RequestException as exc:
            raise ProviderError(f"couldn't reach peer {host}:{port}: {exc}") from exc
        if resp.status_code in (401, 403):
            raise ProviderError("peer refused — set the same personal key on both instances.")
        resp.raise_for_status()
        try:
            payload = decrypt_json(resp.json(), key)
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(
                "couldn't decrypt the peer's credentials — the personal keys don't match."
            ) from exc
        return self.import_credentials(payload)

    def maybe_adopt_credentials(self) -> dict[str, Any]:
        """If this instance has a key but no working accounts, adopt from the
        first key-matching peer that has them."""
        from harmony.config import Settings

        if not Settings.load().personal_key:
            return {"ok": False, "reason": "no personal key set"}
        try:
            if any(a["authenticated"] and not a["stale"] for a in self.accounts()["accounts"]):
                return {"ok": False, "reason": "already have accounts"}
        except Exception:  # noqa: BLE001
            pass
        for peer in self.instances().get("instances", []):
            host, port = peer.get("host"), peer.get("port")
            if not host or not port:
                continue
            try:
                result = self.adopt_from_peer(host, int(port))
                log.info("adopted credentials from peer %s:%s", host, port)
                return result
            except Exception as exc:  # noqa: BLE001 - try the next peer
                log.debug("adopt from %s:%s failed: %s", host, port, exc)
        return {"ok": False, "reason": "no peer with matching key and accounts found"}

    # -- account onboarding (delegated to harmony.web.onboarding) -----------

    def _onboarding(self) -> Any:
        if self._onboard is None:
            from harmony.web.onboarding import Onboarding

            self._onboard = Onboarding(on_change=self._reset_providers, status=self.accounts)
        return self._onboard

    def _reset_providers(self) -> None:
        self._providers = None  # force a re-warm with freshly-saved credentials

    def set_qobuz_token(self, token: str) -> dict[str, Any]:
        return self._onboarding().set_qobuz_token(token)

    def set_ytmusic_browser(self, headers_raw: str) -> dict[str, Any]:
        return self._onboarding().set_ytmusic_browser(headers_raw)

    def ytmusic_autodetect(self, browser: str | None = None) -> dict[str, Any]:
        return self._onboarding().ytmusic_autodetect(browser)

    def set_ytmusic_oauth_client(self, client_id: str, client_secret: str) -> dict[str, Any]:
        return self._onboarding().set_ytmusic_oauth_client(client_id, client_secret)

    def ytmusic_oauth_start(self) -> dict[str, Any]:
        return self._onboarding().ytmusic_oauth_start()

    def ytmusic_oauth_poll(self, poll_token: str) -> dict[str, Any]:
        return self._onboarding().ytmusic_oauth_poll(poll_token)

    def signout(self, service_value: str) -> dict[str, Any]:
        return self._onboarding().signout(service_value)

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

    # -- playlist editing ---------------------------------------------------

    def create_playlist(self, service_value: str, title: str) -> dict[str, Any]:
        prov = self._provider(service_value)
        if prov is None:
            raise KeyError(service_value)
        with self._lock:
            pl = prov.create_playlist(title)
        return {"playlist": playlist_to_dict(pl)}

    def add_tracks(self, service_value: str, playlist_id: str, track_ids: list[str]) -> dict[str, Any]:
        prov = self._provider(service_value)
        if prov is None:
            raise KeyError(service_value)
        with self._lock:
            prov.add_tracks(playlist_id, track_ids)
        return {"ok": True, "added": len(track_ids)}

    def remove_tracks(self, service_value: str, playlist_id: str, track_ids: list[str]) -> dict[str, Any]:
        prov = self._provider(service_value)
        if prov is None:
            raise KeyError(service_value)
        with self._lock:
            prov.remove_tracks(playlist_id, track_ids)
        return {"ok": True, "removed": len(track_ids)}

    def rename_playlist(self, service_value: str, playlist_id: str, title: str) -> dict[str, Any]:
        prov = self._provider(service_value)
        if prov is None:
            raise KeyError(service_value)
        with self._lock:
            prov.rename_playlist(playlist_id, title)
        return {"ok": True, "title": title}

    def delete_playlist(self, service_value: str, playlist_id: str) -> dict[str, Any]:
        prov = self._provider(service_value)
        if prov is None:
            raise KeyError(service_value)
        with self._lock:
            prov.delete_playlist(playlist_id)
        return {"ok": True}

    # -- sync ---------------------------------------------------------------

    _DIRECTIONS = {"a_to_b": "MIRROR_A_TO_B", "b_to_a": "MIRROR_B_TO_A", "two_way": "TWO_WAY"}

    def _sync_engine(self) -> Any:
        from harmony.config import Settings
        from harmony.db import Database
        from harmony.sync import SyncEngine

        if self._db is None:
            self._db = Database()
        s = Settings.load()
        return SyncEngine(
            self._ensure_providers(), self._db,
            high_threshold=s.match_high_threshold, low_threshold=s.match_low_threshold,
            auto_accept_high=s.auto_accept_high,
        )

    @staticmethod
    def _plan_counts(plan: Any) -> dict[str, int]:
        kinds = [a.kind for a in plan.actions]
        return {"adds": kinds.count("add"), "removes": kinds.count("remove"),
                "unmatched": kinds.count("unmatched")}

    def sync_plan(self, source: dict[str, str], target: dict[str, str], direction: str) -> dict[str, Any]:
        from harmony.sync import SyncDirection

        prov_s, prov_t = self._provider(source["service"]), self._provider(target["service"])
        if prov_s is None:
            raise KeyError(source["service"])
        if prov_t is None:
            raise KeyError(target["service"])
        d = SyncDirection[self._DIRECTIONS.get(direction, "MIRROR_A_TO_B")]
        with self._lock:
            a = prov_s.get_playlist(source["id"])
            b = prov_t.get_playlist(target["id"])
            plan = self._sync_engine().plan(a, b, d)
        token = secrets.token_urlsafe(12)
        self._plans[token] = plan
        return {"token": token, "notes": list(plan.notes), **self._plan_counts(plan)}

    def sync_apply(self, token: str) -> dict[str, Any]:
        from harmony.config import Settings

        plan = self._plans.pop(token, None)
        if plan is None:
            raise KeyError("plan token")
        with self._lock:
            report = self._sync_engine().apply(
                plan, snapshot_before_sync=Settings.load().snapshot_before_sync
            )
        return {"added": len(report.added), "removed": len(report.removed),
                "failed": len(report.failed), "messages": list(report.messages)}

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
            # Keep the source ref so an expired provider/CDN URL (they're signed
            # for ~minutes) can be re-resolved on a late seek instead of 403ing.
            "service": service_value,
            "id": track_id,
        }
        self._prune()
        return {"token": token, "mime": source.mime_type, "label": source.label}

    def refresh_stream(self, token: str) -> dict[str, Any] | None:
        """Re-resolve a token's provider URL (its CDN URL expired mid-playback)."""
        meta = self._streams.get(token)
        if meta is None:
            return None
        try:
            source = self._resolve_source(meta["service"], meta["id"])
        except Exception as exc:  # noqa: BLE001
            log.warning("stream refresh failed for %s: %s", meta.get("service"), exc)
            return None
        meta.update(url=source.url, headers=dict(source.headers), mime=source.mime_type,
                    at=time.monotonic())
        return meta

    def _resolve_source(self, service_value: str, track_id: str) -> Any:
        prov = self._provider(service_value)
        if prov is None:
            raise KeyError(service_value)
        with self._lock:
            return prov.resolve_stream(track_id, max_quality=True)

    # -- LAN mesh -----------------------------------------------------------

    def start_mesh(self, port: int, name: str | None = None,
                   bind_host: str = "0.0.0.0") -> None:  # noqa: S104
        """Advertise this instance and start discovering peers (best-effort).

        A server bound to loopback isn't reachable on the LAN, so it must not
        advertise its LAN IP on the mesh (that would make clients discover a
        dead address). The advertised name includes the port so two instances
        on one host don't collide on the same mDNS service name.
        """
        if self._mesh is not None:
            return
        import socket

        if bind_host in ("127.0.0.1", "::1", "localhost"):
            log.info("server bound to loopback (%s); not advertising on the LAN mesh", bind_host)
            return

        from harmony.mesh import Mesh

        self._mesh = Mesh(name or f"harmony-{socket.gethostname()}-{port}", port)
        self._mesh.start()

    def instances(self) -> dict[str, Any]:
        """Reachable Harmony instances: mDNS-discovered peers plus manually
        registered ones (Tailscale/routed hosts multicast can't reach)."""
        from harmony.config import Settings

        by_addr: dict[str, dict[str, Any]] = {}
        discovered = self._mesh.peers() if self._mesh is not None else []
        for p in discovered:
            by_addr[f"{p.get('host')}:{p.get('port')}"] = {**p, "source": "mdns"}
        for p in Settings.load().known_peers:
            host, port = p.get("host"), p.get("port")
            if not host or not port:
                continue
            key = f"{host}:{port}"
            if key in by_addr:
                by_addr[key]["source"] = "both"
            else:
                by_addr[key] = {
                    "name": p.get("name") or host,
                    "host": host,
                    "port": port,
                    "source": "manual",
                }
        return {"instances": list(by_addr.values())}

    def add_peer(self, host: str, port: int, name: str | None = None) -> dict[str, Any]:
        """Register a peer instance by address, verifying it answers /healthz."""
        import requests

        from harmony.config import Settings

        host = (host or "").strip()
        try:
            port = int(port)
        except (TypeError, ValueError):
            return {"ok": False, "reason": "invalid port"}
        if not host:
            return {"ok": False, "reason": "no host"}
        info: dict[str, Any] = {}
        try:
            resp = requests.get(f"http://{host}:{port}/healthz", timeout=4)
            info = resp.json() if resp.ok else {}
        except Exception as exc:  # noqa: BLE001 - reachability is best-effort
            return {"ok": False, "reason": f"unreachable: {exc}"}
        settings = Settings.load()
        peers = [p for p in settings.known_peers if not (p.get("host") == host and p.get("port") == port)]
        peers.append({"host": host, "port": port, "name": name or info.get("name") or host})
        settings.known_peers = peers
        settings.save()
        return {"ok": True, "peer": peers[-1], "healthz": info}

    def remove_peer(self, host: str, port: int) -> dict[str, Any]:
        from harmony.config import Settings

        try:
            port = int(port)
        except (TypeError, ValueError):
            return {"ok": False, "reason": "invalid port"}
        settings = Settings.load()
        before = len(settings.known_peers)
        settings.known_peers = [
            p for p in settings.known_peers if not (p.get("host") == host and p.get("port") == port)
        ]
        settings.save()
        return {"ok": True, "removed": before - len(settings.known_peers)}

    # -- cast to LAN devices ------------------------------------------------

    def _caster(self) -> Any:
        if self._cast is None:
            from harmony.web.cast import CastController

            self._cast = CastController(self._resolve_source)
        return self._cast

    def devices(self, refresh: bool = False) -> dict[str, Any]:
        """Known + auto-discovered playback renderers, deduped by host.

        Manually-configured devices (Settings.known_devices) are always present;
        SSDP-discovered WiiM/UPnP renderers on the instance's LAN are merged in
        from a short-lived cache so the page isn't a 3s multicast scan per call.
        """
        from harmony.config import Settings

        by_host: dict[str, dict[str, Any]] = {}
        for d in Settings.load().known_devices:
            host = d.get("host")
            if host:
                by_host[host] = {
                    "host": host,
                    "name": d.get("name") or host,
                    "kind": d.get("kind", "wiim"),
                    "source": "saved",
                }
        for d in self._discovered_devices(refresh=refresh):
            by_host.setdefault(d["host"], d)
        return {"devices": list(by_host.values())}

    def _discovered_devices(self, refresh: bool = False) -> list[dict[str, Any]]:
        now = time.monotonic()
        if not refresh and self._devices_cache and now - self._devices_cache[0] < 60:
            return self._devices_cache[1]
        found: list[dict[str, Any]] = []

        def scan_wiim() -> None:
            try:
                from harmony.playback.discovery import discover_wiim

                found.extend(
                    {"host": d.host, "name": d.name, "kind": d.kind, "source": "discovered"}
                    for d in discover_wiim(timeout=3.0)
                )
            except Exception as exc:  # noqa: BLE001 - discovery is best-effort
                log.debug("WiiM/UPnP discovery failed: %s", exc)

        def scan_cast() -> None:
            try:
                from harmony.playback.chromecast import discover_cast

                found.extend(
                    {"host": d.host, "name": d.name, "kind": d.kind, "source": "discovered",
                     "port": d.raw.get("port"), "uuid": d.raw.get("uuid")}
                    for d in discover_cast(timeout=4.0)
                )
            except Exception as exc:  # noqa: BLE001 - Cast dep is optional
                log.debug("Chromecast discovery failed: %s", exc)

        # Run the two multicast scans concurrently so a refresh is ~4s, not ~7s.
        threads = [threading.Thread(target=fn, daemon=True) for fn in (scan_wiim, scan_cast)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=6)
        self._devices_cache = (now, found)
        return found

    def _device_kind(self, host: str) -> tuple[str, dict[str, Any]]:
        """Resolve a host's backend ('wiim'|'cast'|…) and its extra info, from
        the saved + discovered device lists. Defaults to WiiM."""
        from harmony.config import Settings

        for d in Settings.load().known_devices:
            if d.get("host") == host:
                return d.get("kind", "wiim"), d
        for d in self._discovered_devices():
            if d["host"] == host:
                return d.get("kind", "wiim"), d
        return "wiim", {}

    # -- federated devices (cast to a peer's LAN device through that peer) ---

    def _split_hostport(self, via: str, default_port: int = 8080) -> tuple[str, int]:
        via = via.strip()
        if via.count(":") == 1:
            host, _, port = via.partition(":")
            try:
                return host, int(port)
            except ValueError:
                return host, default_port
        return via, default_port

    def _peer_call(self, via: str, method: str, path: str,
                   body: dict[str, Any] | None = None, timeout: float = 15.0) -> dict[str, Any]:
        """Authenticated HTTP call to a peer instance (forwards our personal key)."""
        import requests

        from harmony.config import Settings

        host, port = self._split_hostport(via)
        key = Settings.load().personal_key
        headers = {"X-Harmony-Key": key} if key else {}
        url = f"http://{host}:{port}{path}"
        resp = requests.request(method, url, json=body, headers=headers, timeout=timeout)
        try:
            return resp.json()
        except ValueError:
            return {"ok": resp.ok}

    def federated_devices(self, refresh: bool = False) -> dict[str, Any]:
        """This instance's devices plus every mesh peer's, so you can cast to a
        renderer on another LAN through the peer that can reach it. A device we
        can reach directly wins over the same host offered via a peer."""
        by_key: dict[str, dict[str, Any]] = {}
        for d in self.devices(refresh=refresh)["devices"]:
            by_key[d["host"]] = d
        for peer in self.instances()["instances"]:
            via = f"{peer.get('host')}:{peer.get('port')}"
            try:
                peer_devs = self._peer_call(via, "GET", "/api/devices", timeout=6).get("devices", [])
            except Exception as exc:  # noqa: BLE001 - a peer being down mustn't break the list
                log.debug("peer %s devices unavailable: %s", via, exc)
                continue
            for d in peer_devs:
                host = d.get("host")
                if not host or host in by_key:  # prefer a directly-reachable device
                    continue
                key = f"{via}/{host}"
                by_key[key] = {**d, "via": via, "via_name": peer.get("name"), "source": "peer"}
        return {"devices": list(by_key.values())}

    def cast(self, host: str, service_value: str, track_id: str, meta: dict[str, Any] | None = None,
             via: str | None = None) -> dict[str, Any]:
        if via:
            return self._peer_call(via, "POST", f"/api/devices/{host}/play",
                                   {"service": service_value, "id": track_id, "meta": meta or {}})
        kind, info = self._device_kind(host)
        return self._caster().cast(host, service_value, track_id, meta, kind=kind, device_info=info)

    def device_control(self, host: str, action: str, level: int | None = None,
                       via: str | None = None) -> dict[str, Any]:
        if via:
            return self._peer_call(via, "POST", f"/api/devices/{host}/{action}",
                                   {"level": level} if level is not None else {})
        kind, info = self._device_kind(host)
        return self._caster().control(host, action, level, kind=kind, device_info=info)

    def device_status(self, host: str, via: str | None = None) -> dict[str, Any]:
        if via:
            return self._peer_call(via, "GET", f"/api/devices/{host}/status", timeout=6)
        kind, info = self._device_kind(host)
        return self._caster().status(host, kind=kind, device_info=info)

    # -- inter-instance audio routing --------------------------------------

    def _router(self) -> Any:
        if self._audio_router is None:
            from harmony.web.audio_routing import AudioRouter

            self._audio_router = AudioRouter()
        return self._audio_router

    def audio_sinks(self) -> dict[str, Any]:
        return self._router().sinks()

    def audio_status(self) -> dict[str, Any]:
        return self._router().status()

    def audio_receive(self, sink: str | None, latency_ms: int = 150) -> dict[str, Any]:
        return self._router().receive(sink=sink, latency_ms=latency_ms)

    def audio_send(self, to_host: str, latency_ms: int = 150,
                   transport: str | None = None) -> dict[str, Any]:
        return self._router().send(to_host, latency_ms=latency_ms, transport=transport)

    def audio_stop(self) -> dict[str, Any]:
        return self._router().stop()

    def audio_monitor_argv(self) -> list[str] | None:
        """ffmpeg command to stream this machine's live output as MP3 (for a
        light client to pull the hub's audio over HTTP)."""
        from harmony.audio import monitor_ffmpeg_argv

        return monitor_ffmpeg_argv()

    def audio_route(self, direction: str, peer_host: str, peer_port: int,
                    sink: str | None = None, latency_ms: int = 150) -> dict[str, Any]:
        """Set up a full send/receive session with a peer, presenting our key."""
        from harmony.config import Settings

        key = Settings.load().personal_key or None
        return self._router().route(direction, peer_host, int(peer_port),
                                    key=key, sink=sink, latency_ms=latency_ms)

    def stream_for(self, token: str) -> dict[str, Any] | None:
        meta = self._streams.get(token)
        if meta is None or time.monotonic() - meta["at"] > _STREAM_TTL_S:
            return None
        return meta

    def _prune(self) -> None:
        cutoff = time.monotonic() - _STREAM_TTL_S
        for token in [k for k, v in self._streams.items() if v["at"] < cutoff]:
            self._streams.pop(token, None)
