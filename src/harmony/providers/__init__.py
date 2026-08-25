"""Provider registry: one ``MusicProvider`` implementation per ``Service``."""

from __future__ import annotations

from ..config import CredentialStore, Settings
from ..models import Service
from .base import MusicProvider
from .qobuz import QobuzProvider
from .ytmusic import YTMusicProvider


def build_providers(settings: Settings, credentials: CredentialStore) -> dict[Service, MusicProvider]:
    """Construct one provider per known service, ready for unauthenticated use.

    Neither provider requires being signed in to be constructed — YTMusic
    falls back to public search, Qobuz just needs an app id (scraped or
    user-supplied) — so this never raises for missing credentials. Call
    ``provider.authenticate()`` separately once the user is ready to sign in.
    """
    return {
        Service.YTMUSIC: YTMusicProvider(settings, credentials),
        Service.QOBUZ: QobuzProvider(settings, credentials),
    }


__all__ = ["MusicProvider", "YTMusicProvider", "QobuzProvider", "build_providers"]
