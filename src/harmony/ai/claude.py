"""Natural-language playlist planning via the Anthropic API.

The model only ever proposes ``(artist, title)`` pairs plus a one-line
rationale — it never sees or invents a provider track ID. ``resolve`` maps
those names onto real catalog tracks through ``harmony.matching``, exactly
like every other enrichment source in this package.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import anthropic

from .. import config
from ..config import CredentialStore
from ..errors import HarmonyError, MissingCredentialError, ProviderError, RateLimitedError
from ..models import Track

if TYPE_CHECKING:
    from ..providers.base import MusicProvider

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5"
MAX_TOKENS = 8000
DESCRIBE_MAX_TOKENS = 300

# additionalProperties: false + an explicit `required` on every object, per
# the structured-outputs contract for output_config.format.
_PLAYLIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "notes": {"type": "string"},
        "tracks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "artist": {"type": "string"},
                    "title": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["artist", "title", "why"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "description", "tracks", "notes"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = (
    "You are planning a playlist for a real streaming music catalog. Every track you "
    "propose must be a well-known recording that actually exists in commercial "
    "catalogs — never invent a track, artist, or remix, and never guess at a title "
    "you are not confident is real. Give the canonical primary artist name (not a "
    "'feat.'/collaborator credit list) exactly as it is commonly listed. For each "
    "pick, write one short sentence in `why` explaining why it fits the request."
)

DESCRIBE_SYSTEM_PROMPT = (
    "Write a single short, punchy sentence describing the vibe of a playlist, given "
    "its tracklist. No preamble, no quotation marks, just the sentence."
)


@dataclass(slots=True)
class TrackIdea:
    artist: str
    title: str
    why: str = ""


@dataclass(slots=True)
class PlaylistIdea:
    title: str
    description: str
    tracks: list[TrackIdea] = field(default_factory=list)
    notes: str = ""


class PlaylistPlanner:
    """Wraps the Anthropic SDK for NL playlist planning and catalog resolution."""

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL) -> None:
        self._explicit_api_key = api_key
        self.model = model
        self._client: anthropic.Anthropic | None = None

    def _resolve_api_key(self) -> str | None:
        if self._explicit_api_key:
            return self._explicit_api_key
        stored = CredentialStore().get(config.ANTHROPIC_API_KEY)
        if stored:
            return stored
        return os.environ.get("ANTHROPIC_API_KEY")

    @property
    def available(self) -> bool:
        """Whether a usable API key has been configured, without making a request."""
        return bool(self._resolve_api_key())

    def _client_or_raise(self) -> anthropic.Anthropic:
        if self._client is not None:
            return self._client
        api_key = self._resolve_api_key()
        if not api_key:
            raise MissingCredentialError(
                "Anthropic API key",
                hint="Add one in Preferences → Integrations, or set the ANTHROPIC_API_KEY environment variable.",
            )
        self._client = anthropic.Anthropic(api_key=api_key)
        return self._client

    @staticmethod
    def _call(client: anthropic.Anthropic, **kwargs: Any) -> anthropic.types.Message:
        """Call ``messages.create``, translating SDK exceptions to Harmony's hierarchy."""
        try:
            return client.messages.create(**kwargs)
        except anthropic.RateLimitError as exc:
            raise RateLimitedError(f"Anthropic API rate limited: {exc}") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError(f"Could not reach the Anthropic API: {exc}") from exc
        except anthropic.APIError as exc:
            raise ProviderError(f"Anthropic API request failed: {exc}") from exc

    @staticmethod
    def _extract_text(response: anthropic.types.Message) -> str:
        """Pull the first text block out of a response, handling refusal/truncation first."""
        if response.stop_reason == "refusal":
            raise HarmonyError("Claude declined this request (safety refusal).")
        if response.stop_reason == "max_tokens":
            raise HarmonyError("Claude's response was truncated (hit max_tokens).")
        text = next((block.text for block in response.content if block.type == "text"), None)
        if text is None:
            raise ProviderError("Anthropic response had no text content to parse.")
        return text

    def plan(
        self,
        prompt: str,
        *,
        count: int = 25,
        seed_tracks: list[str] | None = None,
        library_hint: list[str] | None = None,
    ) -> PlaylistIdea:
        """Ask Claude to design a playlist. Returns names only — never provider IDs."""
        client = self._client_or_raise()
        user_parts = [f"Build a playlist with about {count} tracks for this request:\n{prompt}"]
        if seed_tracks:
            user_parts.append("Seed tracks to draw inspiration from:\n" + "\n".join(seed_tracks))
        if library_hint:
            user_parts.append(
                "The listener's library already contains (favor variety over repeating these):\n"
                + "\n".join(library_hint)
            )
        user_message = "\n\n".join(user_parts)

        response = self._call(
            client,
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
            output_config={"format": {"type": "json_schema", "schema": _PLAYLIST_SCHEMA}},
        )
        text = self._extract_text(response)

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError(f"Anthropic returned unparseable JSON: {exc}") from exc

        tracks = [
            TrackIdea(artist=t.get("artist", ""), title=t.get("title", ""), why=t.get("why", ""))
            for t in data.get("tracks", [])
            if t.get("artist") and t.get("title")
        ]
        return PlaylistIdea(
            title=data.get("title", ""),
            description=data.get("description", ""),
            tracks=tracks,
            notes=data.get("notes", ""),
        )

    def resolve(
        self,
        idea: PlaylistIdea,
        provider: MusicProvider,
        *,
        progress: Callable[[float, str], None] | None = None,
    ) -> list[Track]:
        """Resolve each proposed (artist, title) to a real catalog track via matching."""
        from .. import matching

        resolved: list[Track] = []
        total = len(idea.tracks) or 1
        for i, pick in enumerate(idea.tracks):
            candidate = Track(id="", title=pick.title, service=provider.service, artists=[pick.artist])
            result = matching.match_track(candidate, provider)
            if result.best is not None and result.confidence != "none":
                resolved.append(result.best.track)
            else:
                log.info("Could not resolve suggested track %r by %r", pick.title, pick.artist)
            if progress is not None:
                progress((i + 1) / total, f"Resolving {pick.artist} - {pick.title}")
        return resolved

    def describe_playlist(self, tracks: list[Track]) -> str:
        """A short human blurb describing ``tracks``."""
        if not tracks:
            return "An empty playlist."
        client = self._client_or_raise()
        listing = "\n".join(f"- {t.artist_name} - {t.title}" for t in tracks[:50])
        response = self._call(
            client,
            model=self.model,
            max_tokens=DESCRIBE_MAX_TOKENS,
            system=DESCRIBE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": listing}],
        )
        return self._extract_text(response).strip()
