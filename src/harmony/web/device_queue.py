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
# Devices that can't say "I finished this track" (WiiM/LinkPlay) are advanced
# when their own position gets this close to their own reported duration.
END_EPSILON_S = 2
# Consecutive tracks that fail to start before the queue gives up (each try is a
# real connection attempt to the device).
MAX_START_FAILURES = 3
# Consecutive status polls that fail before a playing queue is declared lost
# (the device vanished); each poll of a dead cast costs a full connect attempt.
MAX_POLL_FAILURES = 3
# A queue left paused this long gives its device back (resume = Play reloads).
PAUSE_TIMEOUT_S = 30 * 60.0


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
        self.stream: dict[str, Any] | None = None  # {quality, source} of what's playing
        self.gen = 0  # bumped by every start and every halt
        self.failures = 0          # consecutive tracks that failed to start
        # Wall-clock play time of the current track: a Chromecast playing a
        # relayed stream reports a frozen position, so this stands in for it.
        self.clock = 0.0
        self.clock_at = 0.0
        self.raw_pos: Any = None   # the device's own last-reported position

    def halt(self) -> None:
        self.playing = False
        self.armed = False
        self.gen += 1  # a track still starting must not resume playback

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
            # "Playing from …" (what the queue was loaded from) and the current
            # stream's source + quality, so every client shows the same thing.
            "context": q.context,
            "stream": self.stream,
            **{k: self.last_status.get(k) for k in ("state", "position_s", "duration_s", "volume")},
        }


class DeviceQueues:
    """Queue + auto-advance per device host.

    ``play(host, track)`` starts one track on the device; ``status(host)``
    returns ``{state, position_s, duration_s, volume}``. Both are the engine's
    direct (non-via) calls.
    """

    def __init__(self, play: Callable[[str, dict[str, Any]], Any],
                 status: Callable[[str], dict[str, Any]],
                 stop: Callable[[str], Any] | None = None,
                 async_start: bool = False,
                 on_inactive: Callable[[str], Any] | None = None) -> None:
        self._play = play
        # Called the moment a host's queue stops running (stop, finished, error
        # halt): the engine drops its device connection there.
        self._on_inactive = on_inactive
        self._status = status
        self._stop = stop
        # Start tracks on a worker so an HTTP op returns at once (the snapshot
        # already shows the new index); a slow cast doesn't stall the client.
        self._async_start = async_start
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

    def _inactive(self, host: str) -> None:
        if self._on_inactive is None:
            return
        try:
            self._on_inactive(host)
        except Exception as exc:  # noqa: BLE001 - best effort
            log.debug("queue: releasing %s failed: %s", host, exc)

    def busy_hosts(self) -> set[str]:
        """Hosts whose queue is running (the cast idle reaper leaves these alone)."""
        with self._lock:
            return {h for h, dq in self._queues.items() if dq.playing}

    def note_control(self, host: str, action: str, level: int | None = None) -> None:
        """Reflect pause/resume/volume in the snapshot now, not at the next poll."""
        with self._lock:
            dq = self._queues.get(host)
            if dq is None:
                return
            if action == "pause":
                dq.last_status = {**dq.last_status, "state": "paused"}
                dq.prev_state = "paused"
            elif action == "resume":
                dq.last_status = {**dq.last_status, "state": "playing"}
                dq.clock_at = time.monotonic()
                dq.prev_state = "playing"
            elif action == "volume" and level is not None:
                dq.last_status = {**dq.last_status, "volume": int(level)}

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
        stop_device = False
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
                keep = bool(body.get("keep_order"))
                original = _playable(body.get("original")) if keep else []
                if original:
                    # Identity matters (shuffle-off re-anchors by `is`): reuse the
                    # tracks' dicts for matching entries of the original order.
                    pool: dict[tuple, list[dict[str, Any]]] = {}
                    for t in tracks:
                        pool.setdefault((t["service"], str(t["id"])), []).append(t)
                    remapped = []
                    for o in original:
                        same = pool.get((o["service"], str(o["id"])))
                        remapped.append(same.pop(0) if same else o)
                    original = remapped
                start = q.load(tracks, None if body.get("start") is None else int(body["start"]),
                               shuffle=None if shuffle is None else bool(shuffle),
                               keep_order=keep, original=original or None,
                               context=body["context"] if isinstance(body.get("context"), dict) else None)
            elif name == "jump":
                start = q.jump(int(body.get("index", -1)))
                if start is None:
                    raise ValueError("index out of range")
            elif name == "next":
                start = q.advance(manual=True)
                if start is None:
                    dq.halt()
                    stop_device = True  # Next past the end: silence the last track too
            elif name == "prev":
                # A stopped queue has no live position: step back, don't "restart".
                start = q.previous(dq.last_status.get("position_s") if dq.playing else 0)
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
            elif name == "stop":
                dq.halt()
            elif name == "clear":
                q.clear()
            elif name == "shuffle":
                q.set_shuffle(bool(body.get("on")))
            elif name == "repeat":
                q.set_repeat(str(body.get("mode")))
            else:
                raise ValueError(f"unknown queue op {name!r}")
        if start is not None:
            with self._lock:
                dq.failures = 0  # a user action starts a fresh run of attempts
            if self._async_start and not body.get("sync"):
                with self._lock:  # show it as starting right away
                    dq.q.jump(start)
                    dq.playing = True
                    dq.settle_until = time.monotonic() + SETTLE_S
                threading.Thread(target=self._start_track, args=(host, start), daemon=True,
                                 name=f"harmony-queue-start-{host}").start()
            else:
                self._start_track(host, start)
        else:
            if stop_device and self._stop is not None:
                # Stop while still connected; releasing first would make the
                # stop reconnect to the device just to silence it.
                try:
                    self._stop(host)
                except Exception as exc:  # noqa: BLE001 - the queue is halted either way
                    log.debug("queue: stopping %s failed: %s", host, exc)
            if not dq.playing:
                self._inactive(host)  # e.g. a stop/remove/next-past-end
        return self.snapshot(host)

    # -- device loop ---------------------------------------------------------

    def _start_track(self, host: str, index: int) -> None:
        with self._lock:
            dq = self._queues.get(host)
            if dq is None or dq.q.jump(index) is None:
                return
            track = dq.q.current()
            dq.gen += 1
            gen = dq.gen
            dq.playing = True
            dq.armed = False
            dq.error = None
            dq.settle_until = time.monotonic() + SETTLE_S
            dq.clock, dq.clock_at = 0.0, time.monotonic()
            dq.last_status = {}
            dq.raw_pos = None
            dq.prev_state = ""
        with self._lock:
            dq.stream = None
        try:
            played = self._play(host, track)
        except Exception as exc:  # noqa: BLE001 - surface, then try the next one
            log.warning("queue: couldn't play %s on %s: %s", track.get("title"), host, exc)
            with self._lock:
                if dq.gen != gen:
                    return  # stopped or superseded while starting: don't move on
                dq.error = str(exc) or exc.__class__.__name__
                dq.failures += 1
                nxt = dq.q.following(manual=True)
                if dq.failures >= MAX_START_FAILURES:
                    dq.error = f"Couldn't start {dq.failures} tracks in a row on {host} — stopped"
                    dq.halt()
                    stop = True
                elif nxt is None:
                    dq.halt()  # nothing left that plays — don't spin
                    stop = True
                else:
                    stop = False
            if stop:
                self._inactive(host)
                return
            self._start_track(host, nxt)
            return
        with self._lock:
            halted = dq.gen != gen and not dq.playing
            if dq.gen == gen:
                dq.failures = 0
                if isinstance(played, dict) and isinstance(played.get("stream"), dict):
                    dq.stream = played["stream"]
        if halted:
            # Stop was pressed while the device was still starting this track
            # (a Chromecast takes seconds): it began anyway — silence it.
            if self._stop is not None:
                try:
                    self._stop(host)
                except Exception as exc:  # noqa: BLE001 - best effort
                    log.debug("queue: late stop on %s failed: %s", host, exc)
            self._inactive(host)
            return
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
        failures = 0
        paused_since: float | None = None
        while True:
            time.sleep(POLL_S)
            with self._lock:
                dq = self._queues.get(host)
                if dq is None or not dq.playing:
                    self._threads.pop(host, None)
                    return
                gen = dq.gen
                if dq.prev_state == "paused":
                    paused_since = paused_since or time.monotonic()
                    if time.monotonic() - paused_since >= PAUSE_TIMEOUT_S:
                        dq.halt()
                        gen = dq.gen  # what a new load would bump past
                        dq.last_status = {**dq.last_status, "state": "stopped"}
                        dq.error = None
                        self._threads.pop(host, None)
                        expired = True
                    else:
                        expired = False
                else:
                    paused_since, expired = None, False
            if expired:
                log.info("queue: %s paused too long — releasing the device", host)
                with self._lock:
                    restarted = dq.gen != gen  # a new load began meanwhile: leave it
                if restarted:
                    return
                if self._stop is not None:
                    try:
                        self._stop(host)
                    except Exception as exc:  # noqa: BLE001 - released either way
                        log.debug("queue: stopping idle %s failed: %s", host, exc)
                self._inactive(host)
                return
            try:
                st = self._status(host) or {}
                failures = 0
            except Exception as exc:  # noqa: BLE001 - one missed poll is harmless
                failures += 1
                log.debug("queue poll %s failed (%d): %s", host, failures, exc)
                if failures < MAX_POLL_FAILURES:
                    continue
                with self._lock:
                    dq = self._queues.get(host)
                    if dq is not None and dq.playing and dq.gen == gen:
                        dq.error = f"Lost the connection to {host} — stopped"
                        dq.halt()
                    self._threads.pop(host, None)
                self._inactive(host)
                return
            nxt = self.after_status(host, st)
            if nxt is not None:
                self._start_track(host, nxt)
            else:
                with self._lock:
                    dq = self._queues.get(host)
                    over = dq is None or not dq.playing
                if over:
                    self._inactive(host)  # finished / stopped on the device itself

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
            # Only a duration the DEVICE reports may declare "near the end": the
            # track metadata's can be shorter than the real file (YouTube) and
            # would cut the tail off.
            dev_dur = dur
            # A device that reports its own natural end (Chromecast: IDLE +
            # FINISHED) is advanced on that, never on a guess from the position.
            reports_end = st.get("ended") is not None
            dev_ended = st.get("ended") is True
            if not dur:
                cur = dq.q.current() or {}
                dur = cur.get("duration_s") if isinstance(cur, dict) else None
            # Trust the device's position when it moves; otherwise the play clock.
            raw_before, dq.raw_pos = dq.raw_pos, pos
            clocked = pos is None or (pos == raw_before and state == "playing")
            if clocked:
                # round() first: the clock is a sum of float deltas, so 96s of
                # play can read 95.99999999999989 and must not truncate to 95.
                pos = max(pos or 0, int(round(dq.clock, 6)))
            dq.last_status = {**st, "position_s": pos, "duration_s": dur}
            if not dq.playing or time.monotonic() < dq.settle_until:
                return None
            has_dur = bool(dur and dur > 0 and pos is not None)
            # Only the device's own clock may declare "near the end": the play
            # clock runs ahead while a cast buffers, so a clock-driven track ends
            # on the device going idle instead (the branch below).
            near_end = (has_dur and not clocked and not reports_end
                        and bool(dev_dur and dev_dur > 0) and pos >= dev_dur - END_EPSILON_S)
            if has_dur and not near_end and state == "playing":
                dq.armed = True
            ended = False
            if dev_ended:
                dq.armed = False
                ended = True
            elif st.get("idle_reason") == "ERROR" and state in ("stopped", "idle"):
                # The receiver gave up on the stream (a stalled/refused relay
                # fetch): say so, rather than sit "playing" a silent device.
                dq.error = f"{host} couldn't play {(dq.q.current() or {}).get('title') or 'the track'}"
                dq.halt()
                return None
            elif near_end and dq.armed:
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
