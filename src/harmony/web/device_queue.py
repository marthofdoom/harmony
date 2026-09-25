"""Server-owned play queues for network devices (WiiM/UPnP/Chromecast).

A cast device plays one URL at a time; something has to notice the track ended
and send the next one. A browser tab or a phone can't be that something — it
sleeps, closes, or loses the network. So the instance that can reach the device
owns its queue: clients hand it the whole list once, and a poller here advances
it. For a device reached "via" a peer, the peer owns the queue (the engine
forwards the whole list there), so the queue always lives next to the device.

The queue rules are the shared :class:`harmony.playqueue.PlayQueue`; this adds
the device loop. GTK-free; one daemon poll thread per playing device.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from harmony.playqueue import PlayQueue

log = logging.getLogger(__name__)

POLL_S = 2.0
# After a track starts, a device reports transitional stop/idle status for a
# few seconds; ignore end-detection during that window (mirrors the desktop).
SETTLE_S = 6.0
END_EPSILON_S = 2


def _playable(tracks: Any) -> list[dict[str, Any]]:
    return [t for t in (tracks or []) if isinstance(t, dict) and t.get("service") and t.get("id")]


class DeviceQueue:
    def __init__(self) -> None:
        self.q = PlayQueue()
        self.playing = False       # queue is running (not stopped/finished)
        self.armed = False         # saw mid-track playback -> end detection live
        self.settle_until = 0.0
        self.prev_state = ""
        self.last_status: dict[str, Any] = {}
        self.error: str | None = None
        self.failures = 0          # consecutive tracks that failed to start
        # Wall-clock play time of the current track: a Chromecast playing a
        # relayed stream reports a frozen position, so this stands in for it.
        self.clock = 0.0
        self.clock_at = 0.0
        self.raw_pos: Any = None   # the device's own last-reported position

    def halt(self) -> None:
        self.playing = False
        self.armed = False

    def snapshot(self) -> dict[str, Any]:
        q = self.q
        return {
            "tracks": list(q.tracks),
            "index": q.index,
            "current": q.current(),
            "shuffle": q.shuffle,
            "repeat": q.repeat,
            "playing": self.playing,
            "error": self.error,
            **{k: self.last_status.get(k) for k in ("state", "position_s", "duration_s", "volume")},
        }


class DeviceQueues:
    """Queue + auto-advance per device host.

    ``play(host, track)`` starts one track on the device; ``status(host)``
    returns ``{state, position_s, duration_s, volume}``. Both are the engine's
    direct (non-via) calls.
    """

    def __init__(self, play: Callable[[str, dict[str, Any]], Any],
                 status: Callable[[str], dict[str, Any]]) -> None:
        self._play = play
        self._status = status
        self._lock = threading.RLock()
        self._queues: dict[str, DeviceQueue] = {}
        self._threads: dict[str, threading.Thread] = {}

    def snapshot(self, host: str) -> dict[str, Any]:
        with self._lock:
            dq = self._queues.get(host)
            return dq.snapshot() if dq else DeviceQueue().snapshot()

    def stop(self, host: str) -> None:
        """Halt the queue for ``host`` (the device itself is stopped by the caller)."""
        with self._lock:
            dq = self._queues.get(host)
            if dq:
                dq.halt()

    def note_seek(self, host: str, position_s: int) -> None:
        """Keep the play clock (the frozen-position fallback) in step with a seek."""
        with self._lock:
            dq = self._queues.get(host)
            if dq:
                dq.clock = float(position_s)
                dq.clock_at = time.monotonic()
                dq.raw_pos = None
                dq.armed = False  # re-arm on the next mid-track reading
                dq.last_status = {**dq.last_status, "position_s": int(position_s)}

    def op(self, host: str, name: str, body: dict[str, Any]) -> dict[str, Any]:
        """Apply one queue operation (the single HTTP entry point)."""
        with self._lock:
            dq = self._queues.setdefault(host, DeviceQueue())
            q, idle = dq.q, not dq.playing
            start: int | None = None
            if name == "load":
                tracks = _playable(body.get("tracks"))
                if not tracks:
                    raise ValueError("no playable tracks")
                if body.get("repeat") is not None:
                    q.set_repeat(str(body["repeat"]))
                shuffle = body.get("shuffle")
                start = q.load(tracks, None if body.get("start") is None else int(body["start"]),
                               shuffle=None if shuffle is None else bool(shuffle))
            elif name == "jump":
                start = q.jump(int(body.get("index", -1)))
                if start is None:
                    raise ValueError("index out of range")
            elif name == "next":
                start = q.advance(manual=True)
                if start is None:
                    dq.halt()
            elif name == "prev":
                start = q.previous(dq.last_status.get("position_s"))
            elif name == "enqueue":
                start = q.enqueue(_playable(body.get("tracks")), idle=idle)
            elif name == "play_next":
                start = q.play_next(_playable(body.get("tracks")), idle=idle)
            elif name == "move":
                q.move(int(body.get("from", -1)), int(body.get("to", -1)))
            elif name == "remove":
                removed_current, start = q.remove(int(body.get("index", -1)))
                if not removed_current or idle:
                    start = None
                if removed_current and start is None and not idle:
                    dq.halt()  # removed the last, playing track
            elif name == "clear":
                q.clear()
            elif name == "shuffle":
                q.set_shuffle(bool(body.get("on")))
            elif name == "repeat":
                q.set_repeat(str(body.get("mode")))
            else:
                raise ValueError(f"unknown queue op {name!r}")
        if start is not None:
            self._start_track(host, start)
        return self.snapshot(host)

    # -- device loop ---------------------------------------------------------

    def _start_track(self, host: str, index: int) -> None:
        with self._lock:
            dq = self._queues.get(host)
            if dq is None or dq.q.jump(index) is None:
                return
            track = dq.q.current()
            dq.playing = True
            dq.armed = False
            dq.error = None
            dq.settle_until = time.monotonic() + SETTLE_S
            dq.clock, dq.clock_at = 0.0, time.monotonic()
            dq.last_status = {}
            dq.raw_pos = None
            dq.prev_state = ""
        try:
            self._play(host, track)
        except Exception as exc:  # noqa: BLE001 - surface, then try the next one
            log.warning("queue: couldn't play %s on %s: %s", track.get("title"), host, exc)
            with self._lock:
                dq.error = str(exc) or exc.__class__.__name__
                dq.failures += 1
                nxt = dq.q.following(manual=True)
                if nxt is None or dq.failures >= len(dq.q.tracks):
                    dq.halt()  # nothing left that plays — don't spin
                    return
            self._start_track(host, nxt)
            return
        with self._lock:
            dq.failures = 0
        self._ensure_poller(host)

    def _ensure_poller(self, host: str) -> None:
        with self._lock:
            t = self._threads.get(host)
            if t is not None and t.is_alive():
                return
            t = threading.Thread(target=self._poll_loop, args=(host,), daemon=True,
                                 name=f"harmony-queue-{host}")
            self._threads[host] = t
        t.start()

    def _poll_loop(self, host: str) -> None:
        while True:
            time.sleep(POLL_S)
            with self._lock:
                dq = self._queues.get(host)
                if dq is None or not dq.playing:
                    self._threads.pop(host, None)
                    return
            try:
                st = self._status(host) or {}
            except Exception as exc:  # noqa: BLE001 - a missed poll is harmless
                log.debug("queue poll %s: %s", host, exc)
                continue
            nxt = self.after_status(host, st)
            if nxt is not None:
                self._start_track(host, nxt)

    def after_status(self, host: str, st: dict[str, Any]) -> int | None:
        """Record a status reading; return the index to start if the track ended.

        Same rules as the desktop: progress reaching the duration (armed by
        mid-track playback, so one reading advances once), else a
        playing→stopped edge when the device reports no duration. Pure logic."""
        with self._lock:
            dq = self._queues.get(host)
            if dq is None:
                return None
            before = dq.last_status
            state = (st.get("state") or "").lower()
            prev, dq.prev_state = dq.prev_state, state
            now = time.monotonic()
            if prev == "playing" and dq.clock_at:
                dq.clock += now - dq.clock_at
            dq.clock_at = now
            pos, dur = st.get("position_s"), st.get("duration_s")
            if not dur:
                cur = dq.q.current() or {}
                dur = cur.get("duration_s") if isinstance(cur, dict) else None
            # Trust the device's position when it moves; otherwise the play clock.
            raw_before, dq.raw_pos = dq.raw_pos, pos
            if pos is None or (pos == raw_before and state == "playing"):
                pos = max(pos or 0, int(dq.clock))
            dq.last_status = {**st, "position_s": pos, "duration_s": dur}
            if not dq.playing or time.monotonic() < dq.settle_until:
                return None
            has_dur = bool(dur and dur > 0 and pos is not None)
            near_end = has_dur and pos >= dur - END_EPSILON_S
            if has_dur and not near_end:
                dq.armed = True
            ended = False
            if near_end and dq.armed:
                dq.armed = False
                ended = True
            elif dq.armed and state in ("stopped", "idle") and prev == "playing":
                # Finished between two polls: the device went idle and dropped its
                # position. Only an end if the last reading was close to the end —
                # a stop mid-track (someone used the device's own button) is not.
                bpos = max(before.get("position_s") or 0, int(dq.clock))
                ended = bool(dur and bpos >= dur - (POLL_S * 2 + END_EPSILON_S))
                if not ended:
                    dq.halt()  # stopped from the device itself: respect it
                    return None
            elif not has_dur and prev == "playing" and state == "stopped":
                ended = True
            if not ended:
                return None
            nxt = dq.q.following()
            if nxt is None:
                dq.halt()
            return nxt
