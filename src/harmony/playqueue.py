"""The one play-queue model every Harmony surface shares.

Index-based: ``tracks`` is the whole active list (history + current + up
next) and ``index`` points at what's playing, so a Now Playing view can show
all of it and jump anywhere. Items are opaque (``Track`` objects on the desktop,
dicts on the server); identity (``is``) is how the current item is tracked
through reorders, so duplicates in a list are fine.

Pure data + rules, no I/O and no threads: the owner (desktop AppState, the
server's DeviceQueues) decides *when* to start a track; every op returns the
index to start now, or None to leave playback alone.
"""

from __future__ import annotations

import random
from typing import Any

REPEAT_MODES = ("off", "all", "one")
# Previous restarts the current track once it's this far in (every player does).
RESTART_AFTER_S = 3


class PlayQueue:
    def __init__(self) -> None:
        self.tracks: list[Any] = []
        self.index: int = -1
        self.original: list[Any] = []  # load order, restored when shuffle goes off
        self.shuffle: bool = False
        self.repeat: str = "off"

    # -- reads ---------------------------------------------------------------

    def current(self) -> Any | None:
        return self.tracks[self.index] if 0 <= self.index < len(self.tracks) else None

    def upcoming(self) -> list[Any]:
        return self.tracks[self.index + 1:] if self.index >= 0 else list(self.tracks)

    def has_next(self) -> bool:
        return self.following(manual=True) is not None

    def has_previous(self) -> bool:
        return self.index > 0 or (self.repeat == "all" and bool(self.tracks))

    def following(self, manual: bool = False) -> int | None:
        """Index after the current one (None = the queue is done). Repeat-one
        only holds on a natural track end — Next still moves on."""
        if not self.tracks:
            return None
        if self.repeat == "one" and not manual and self.index >= 0:
            return self.index
        if self.index + 1 < len(self.tracks):
            return self.index + 1
        if self.repeat == "all":
            return 0
        return None

    # -- ops (return the index to start, or None) ----------------------------

    def load(self, tracks: list[Any], start: int | None = 0, shuffle: bool | None = None,
             keep_order: bool = False, original: list[Any] | None = None) -> int | None:
        """Replace the queue with ``tracks``. ``start=None`` means "shuffle
        play": start at a random track (when shuffle is on). ``keep_order``
        takes the list as already arranged (a hand-off to another output keeps
        the listener's current, possibly shuffled, order)."""
        tracks = list(tracks)
        if not tracks:
            return None
        if shuffle is not None:
            self.shuffle = shuffle
        # A hand-off of a shuffled queue passes its real (load) order along, so
        # turning shuffle off on the new output still restores the album order.
        self.original = list(original) if original else list(tracks)
        if self.shuffle and not keep_order:
            if start is None:
                rest = list(tracks)
                random.shuffle(rest)
            else:
                start = max(0, min(start, len(tracks) - 1))
                rest = tracks[:start] + tracks[start + 1:]
                random.shuffle(rest)
                rest.insert(0, tracks[start])
            self.tracks, self.index = rest, 0
        else:
            self.tracks, self.index = tracks, max(0, min(start or 0, len(tracks) - 1))
        return self.index

    def jump(self, index: int) -> int | None:
        if not 0 <= index < len(self.tracks):
            return None
        self.index = index
        return index

    def advance(self, manual: bool = False) -> int | None:
        """Move to the following track (natural end or Next)."""
        nxt = self.following(manual=manual)
        if nxt is not None:
            self.index = nxt
        return nxt

    def previous(self, position_s: float | None = None) -> int | None:
        if not self.tracks:
            return None
        if self.index < 0:
            self.index = 0
            return 0
        if (position_s or 0) > RESTART_AFTER_S:
            return self.index
        if self.index > 0:
            self.index -= 1
        elif self.repeat == "all":
            self.index = len(self.tracks) - 1
        return self.index

    def enqueue(self, tracks: list[Any], idle: bool) -> int | None:
        """Append; if nothing is playing, start the first added track."""
        tracks = list(tracks)
        if not tracks:
            return None
        first = len(self.tracks)
        self.tracks.extend(tracks)
        self.original.extend(tracks)
        if idle:
            self.index = first
            return first
        return None

    def play_next(self, tracks: list[Any], idle: bool) -> int | None:
        """Insert right after the current track; if idle, start them now."""
        tracks = list(tracks)
        if not tracks:
            return None
        at = self.index + 1 if self.index >= 0 else 0
        self.tracks[at:at] = tracks
        self.original.extend(tracks)
        if idle:
            self.index = at
            return at
        return None

    def move(self, src: int, dst: int) -> None:
        n = len(self.tracks)
        if not (0 <= src < n and 0 <= dst < n) or src == dst:
            return
        cur = self.current()
        self.tracks.insert(dst, self.tracks.pop(src))
        self._reanchor(cur)
        if self.shuffle is False:
            self.original = list(self.tracks)  # a manual order IS the order now

    def remove(self, index: int) -> tuple[bool, int | None]:
        """Remove one item. Returns (removed_current, index_to_start)."""
        if not 0 <= index < len(self.tracks):
            return False, None
        item = self.tracks.pop(index)
        self.original = [t for t in self.original if t is not item]
        if index < self.index:
            self.index -= 1
            return False, None
        if index > self.index:
            return False, None
        # Removed what's playing: whatever slid into this slot plays next.
        if self.index < len(self.tracks):
            return True, self.index
        self.index = len(self.tracks) - 1
        return True, None

    def clear(self) -> None:
        """Drop everything but the current track (it keeps playing)."""
        cur = self.current()
        self.tracks = [cur] if cur is not None else []
        self.original = list(self.tracks)
        self.index = 0 if cur is not None else -1

    def reset(self) -> None:
        self.tracks, self.original, self.index = [], [], -1

    def set_shuffle(self, on: bool) -> None:
        if on == self.shuffle:
            return
        self.shuffle = on
        cur = self.current()
        if on:
            head = self.tracks[:self.index + 1] if cur is not None else []
            rest = self.upcoming()
            random.shuffle(rest)
            self.tracks = head + rest
        else:
            # Back to load order (plus anything added since), still on the same track.
            seen = {id(t) for t in self.original}
            self.tracks = list(self.original) + [t for t in self.tracks if id(t) not in seen]
            self._reanchor(cur)

    def set_repeat(self, mode: str) -> None:
        if mode not in REPEAT_MODES:
            raise ValueError("repeat must be off|all|one")
        self.repeat = mode

    def _reanchor(self, cur: Any | None) -> None:
        if cur is None:
            return
        for i, t in enumerate(self.tracks):
            if t is cur:
                self.index = i
                return
