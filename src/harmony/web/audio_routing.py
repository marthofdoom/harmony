"""Inter-instance audio routing: send/receive full system audio between hubs.

Engine-adjacent orchestration (no GTK). Each instance can run one local
**receiver** (play a peer's audio into a sink) and/or one local **sender**
(broadcast this machine's output to a peer), on top of ``harmony.audio``'s ROC
(preferred) / RTP transports.

``route()`` drives *both* halves of a session across two instances: to pull a
peer's audio here it starts a local receiver and calls the peer's
``/api/audio/send``; to push our audio to a peer it calls the peer's
``/api/audio/receive`` and starts a local sender. Cross-instance calls carry the
local [[personal key]], so a peer only cooperates when the keys match — the same
gate as every other shared capability.
"""

from __future__ import annotations

import logging
import socket
import threading
from typing import Any

from harmony.errors import ProviderError

log = logging.getLogger(__name__)


def _local_ip_towards(host: str) -> str:
    """The source IP this machine would use to reach ``host`` — what the peer
    should send audio back to."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((host, 9))  # no packet sent; just selects the egress iface
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


class AudioRouter:
    """Owns this instance's single audio route (one receiver and/or sender)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._receiver: Any | None = None
        self._sender: Any | None = None
        self._recv_kind: str | None = None  # "roc" | "rtp"
        self._send_kind: str | None = None
        self._sink: str | None = None
        self._peer: str | None = None
        self._latency_ms: int | None = None

    def _roc(self) -> bool:
        from harmony.audio import roc_available

        return roc_available()

    # -- local halves -------------------------------------------------------

    def sinks(self) -> dict[str, Any]:
        from harmony.audio import list_sinks

        return {"sinks": [{"name": s.name, "description": s.description} for s in list_sinks()]}

    def receive(self, sink: str | None = None, latency_ms: int = 150) -> dict[str, Any]:
        """Start playing an incoming peer stream into ``sink`` (default sink if None)."""
        from harmony import audio

        with self._lock:
            self._stop_receiver_locked()
            target = sink or audio.default_sink()
            if not target:
                raise ProviderError("No output sink to play into (is PipeWire/Pulse running?).")
            if self._roc():
                self._receiver = audio.roc_receiver_up(target, target_latency_ms=latency_ms)
                self._recv_kind = "roc"
            else:
                self._receiver = audio.rtp_receiver_up(target, latency_ms=latency_ms)
                self._recv_kind = "rtp"
            self._sink = target
            self._latency_ms = latency_ms
        return {"ok": True, "sink": target, "transport": self._recv_kind, "latency_ms": latency_ms}

    def send(self, to_host: str, latency_ms: int = 150, transport: str | None = None) -> dict[str, Any]:
        """Start broadcasting this machine's audio to ``to_host``.

        ``transport`` forces "roc" or "rtp"; None auto-picks (ROC if available).
        A phone (which receives plain RTP, no ROC) asks for "rtp" so it can play
        the stream without a native ROC library.
        """
        from harmony import audio

        if not to_host:
            raise ProviderError("missing to_host")
        use_rtp = transport == "rtp" or (transport is None and not self._roc())
        with self._lock:
            self._stop_sender_locked()
            if use_rtp:
                self._sender = audio.rtp_sender_up(to_host)
                self._send_kind = "rtp"
            else:
                self._sender = audio.roc_sender_up(to_host)
                self._send_kind = "roc"
            self._peer = to_host
            self._latency_ms = latency_ms
        return {"ok": True, "to_host": to_host, "transport": self._send_kind}

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._stop_receiver_locked()
            self._stop_sender_locked()
            self._peer = None
        return {"ok": True}

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "receiving": self._receiver is not None,
                "sending": self._sender is not None,
                "sink": self._sink,
                "peer": self._peer,
                "latency_ms": self._latency_ms,
                "transport": self._recv_kind or self._send_kind,
                "roc": self._roc(),
            }

    # -- both halves, across two instances ----------------------------------

    def route(
        self,
        direction: str,
        peer_host: str,
        peer_port: int,
        *,
        key: str | None = None,
        sink: str | None = None,
        latency_ms: int = 150,
    ) -> dict[str, Any]:
        """Set up a full session with a peer.

        ``receive``: pull the peer's audio here (local receiver + ask the peer to
        send to us). ``send``: push our audio to the peer (ask the peer to
        receive, then send to it).
        """
        if not peer_host or not peer_port:
            raise ProviderError("missing peer host/port")
        if direction == "receive":
            # Start our receiver, then tell the peer to send in *our* transport —
            # otherwise the peer picks by its own ROC availability and a ROC-less
            # receiver paired with a ROC sender just hears silence.
            self.receive(sink=sink, latency_ms=latency_ms)
            with self._lock:
                recv_transport = self._recv_kind
            try:
                my_host = _local_ip_towards(peer_host)
                self._peer_post(peer_host, peer_port, key, "send",
                                {"to_host": my_host, "latency_ms": latency_ms,
                                 "transport": recv_transport})
            except ProviderError:
                self.stop()  # don't leave an orphan receiver running
                raise
            with self._lock:
                self._peer = peer_host
            return {"ok": True, "direction": "receive", "peer": peer_host}
        if direction == "send":
            # The peer starts its receiver first and tells us which transport it
            # used, so our sender matches it.
            resp = self._peer_post(peer_host, peer_port, key, "receive", {"latency_ms": latency_ms})
            self.send(peer_host, latency_ms=latency_ms, transport=resp.get("transport"))
            return {"ok": True, "direction": "send", "peer": peer_host}
        raise ProviderError(f"unknown direction {direction!r}")

    def _peer_post(self, host: str, port: int, key: str | None, endpoint: str,
                   body: dict[str, Any]) -> dict[str, Any]:
        import requests

        headers = {"Content-Type": "application/json"}
        if key:
            headers["X-Harmony-Key"] = key
        url = f"http://{host}:{port}/api/audio/{endpoint}"
        try:
            resp = requests.post(url, json=body, headers=headers, timeout=8)
        except requests.RequestException as exc:
            raise ProviderError(f"couldn't reach peer {host}:{port}: {exc}") from exc
        if resp.status_code == 401:
            raise ProviderError("peer rejected the personal key — set the same key on both instances.")
        if not resp.ok:
            detail = ""
            try:
                detail = resp.json().get("error", "")
            except Exception:  # noqa: BLE001
                detail = resp.text[:200]
            raise ProviderError(f"peer {host} error: {detail or resp.status_code}")
        return resp.json() if resp.content else {}

    # -- teardown helpers (call with the lock held) -------------------------

    def _stop_receiver_locked(self) -> None:
        recv, kind = self._receiver, self._recv_kind
        self._receiver = self._recv_kind = None
        self._sink = None
        if recv is None:
            return
        from harmony import audio

        try:
            if kind == "roc":
                audio.roc_receiver_down(recv)
            else:
                audio.rtp_receiver_down(recv)
        except Exception:  # noqa: BLE001 - best-effort teardown
            log.debug("receiver teardown failed", exc_info=True)

    def _stop_sender_locked(self) -> None:
        send, kind = self._sender, self._send_kind
        self._sender = self._send_kind = None
        if send is None:
            return
        from harmony import audio

        try:
            if kind == "roc":
                audio.roc_sender_down(send)
            else:
                audio.rtp_sender_down(send)
        except Exception:  # noqa: BLE001 - best-effort teardown
            log.debug("sender teardown failed", exc_info=True)
