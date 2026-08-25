"""Worker-thread plumbing so the GTK main loop never blocks on network I/O."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="harmony-worker")


def shutdown(wait: bool = False) -> None:
    _executor.shutdown(wait=wait, cancel_futures=True)


class Cancelled(Exception):
    """Raised inside a worker when its token has been cancelled."""


class CancelToken:
    """Cooperative cancellation — workers check it between units of work."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise Cancelled()


def run_async(
    fn: Callable[..., T],
    on_done: Callable[[T], Any] | None = None,
    on_error: Callable[[BaseException], Any] | None = None,
    *args: Any,
    on_cancelled: Callable[[], Any] | None = None,
    **kwargs: Any,
) -> Future:
    """Run ``fn`` off the main loop; deliver the result back on the main loop.

    ``on_done`` / ``on_error`` / ``on_cancelled`` are all invoked via
    ``GLib.idle_add`` so they are safe to touch widgets from. A cancelled task
    never counts as an error (it's not logged as one) but callers that put up
    UI while the task runs — a ``ProgressDialog``, say — need a callback to
    tear that UI down; that's what ``on_cancelled`` is for. It's optional so
    fire-and-forget callers don't need to care.
    """
    from gi.repository import GLib

    def _work() -> T:
        return fn(*args, **kwargs)

    def _settle(fut: Future) -> None:
        try:
            result = fut.result()
        except Cancelled:
            if on_cancelled is not None:
                GLib.idle_add(lambda: (on_cancelled(), False)[1], priority=GLib.PRIORITY_DEFAULT)
            return
        except BaseException as exc:  # noqa: BLE001 - surfaced to the caller
            log.exception("Background task failed: %s", fn)
            if on_error is not None:
                # Bind now: Python unbinds `exc` when the except block exits, so a
                # lambda that closed over the name would fire with it already gone.
                err = exc
                GLib.idle_add(lambda: (on_error(err), False)[1], priority=GLib.PRIORITY_DEFAULT)
            return
        if on_done is not None:
            GLib.idle_add(lambda: (on_done(result), False)[1], priority=GLib.PRIORITY_DEFAULT)

    future = _executor.submit(_work)
    future.add_done_callback(_settle)
    return future


def on_main(fn: Callable[..., Any], *args: Any) -> None:
    """Schedule ``fn`` on the GTK main loop from any thread."""
    from gi.repository import GLib

    GLib.idle_add(lambda: (fn(*args), False)[1], priority=GLib.PRIORITY_DEFAULT)


class RateLimiter:
    """Simple monotonic spacing between calls; used for MusicBrainz et al."""

    def __init__(self, min_interval_s: float) -> None:
        self.min_interval = min_interval_s
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        import time

        with self._lock:
            now = time.monotonic()
            delay = self.min_interval - (now - self._last)
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()
