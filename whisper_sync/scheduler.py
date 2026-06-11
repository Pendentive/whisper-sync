"""Single-thread timer scheduler for the whole app.

WhisperSync historically spawned a fresh daemon thread for every delayed or
periodic action (_schedule_idle resets, icon-flash resets, deferred
restart/quit, the stats flush loop, clipboard restore delays). That
thread-churn pattern is one of the structural causes of the app's native
heap instability on Windows (see docs/plans/2026-05-11-stability-rebuild.md).

This module provides ONE long-lived daemon thread that owns every timer:

    from .scheduler import scheduler
    handle = scheduler.call_later(2.0, reset_idle, label="idle-reset")
    handle.cancel()                       # safe at any time, idempotent
    scheduler.call_every(60.0, flush, label="stats-flush")

Design constraints:
- Jobs run ON the scheduler thread. They must be short and non-blocking
  (sub-second). Long work should be submitted to an Executor from within
  the job.
- Exceptions in jobs are logged and never kill the thread.
- ``call_every`` reschedules AFTER the job completes (fixed delay, not
  fixed rate) so a slow tick cannot pile up.
- ``shutdown()`` is graceful and idempotent; pending jobs are dropped.
"""

from __future__ import annotations

import heapq
import itertools
import threading
import time
from typing import Callable

from .logger import logger


class TimerHandle:
    """Cancellation handle for a scheduled job. Thread-safe, idempotent."""

    __slots__ = ("_cancelled", "label")

    def __init__(self, label: str):
        self._cancelled = threading.Event()
        self.label = label

    def cancel(self) -> None:
        self._cancelled.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()


class Scheduler:
    """One timer thread for all delayed/periodic jobs in the process."""

    def __init__(self, name: str = "ws-scheduler"):
        self._name = name
        self._lock = threading.Lock()
        self._wakeup = threading.Event()
        self._heap: list[tuple[float, int, TimerHandle, Callable[[], None], float | None]] = []
        self._counter = itertools.count()  # heap tiebreaker
        self._thread: threading.Thread | None = None
        self._shutdown = False

    # -- public API --------------------------------------------------------

    def call_later(self, delay_s: float, fn: Callable[[], None],
                   label: str = "timer") -> TimerHandle:
        """Run ``fn`` once after ``delay_s`` seconds on the scheduler thread."""
        return self._schedule(delay_s, fn, label, interval=None)

    def call_every(self, interval_s: float, fn: Callable[[], None],
                   label: str = "periodic") -> TimerHandle:
        """Run ``fn`` every ``interval_s`` seconds (fixed delay between runs).

        The first run happens after ``interval_s``, not immediately.
        """
        return self._schedule(interval_s, fn, label, interval=interval_s)

    def shutdown(self, timeout: float | None = 2.0) -> None:
        """Stop the scheduler thread. Pending jobs are dropped. Idempotent."""
        with self._lock:
            self._shutdown = True
            self._heap.clear()
        self._wakeup.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)

    # -- internals ---------------------------------------------------------

    def _schedule(self, delay_s: float, fn: Callable[[], None], label: str,
                  interval: float | None) -> TimerHandle:
        handle = TimerHandle(label)
        when = time.monotonic() + max(0.0, float(delay_s))
        with self._lock:
            if self._shutdown:
                # Post-shutdown scheduling is a no-op; return a pre-cancelled
                # handle so callers don't need their own shutdown checks.
                handle.cancel()
                return handle
            heapq.heappush(self._heap, (when, next(self._counter), handle, fn, interval))
            self._ensure_thread_locked()
        self._wakeup.set()
        return handle

    def _ensure_thread_locked(self) -> None:
        """Start the scheduler thread lazily. Caller holds self._lock."""
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._run, name=self._name, daemon=True
            )
            self._thread.start()

    def _run(self) -> None:
        while True:
            with self._lock:
                if self._shutdown:
                    return
                if not self._heap:
                    timeout = None
                else:
                    now = time.monotonic()
                    when = self._heap[0][0]
                    timeout = max(0.0, when - now)
                    if timeout == 0.0:
                        when, _seq, handle, fn, interval = heapq.heappop(self._heap)
                        # Fall through to run outside the lock
                        job = (handle, fn, interval)
                    else:
                        job = None
                if timeout is None or timeout > 0.0:
                    job = None
            if job is None:
                self._wakeup.wait(timeout=timeout)
                self._wakeup.clear()
                continue

            handle, fn, interval = job
            if handle.cancelled:
                continue
            try:
                fn()
            except Exception:
                logger.exception("scheduler job %r raised", handle.label)
            if interval is not None and not handle.cancelled:
                when = time.monotonic() + interval
                with self._lock:
                    if not self._shutdown:
                        heapq.heappush(
                            self._heap,
                            (when, next(self._counter), handle, fn, interval),
                        )


# Process-wide singleton. Import-time creation is cheap (thread starts
# lazily on first schedule).
scheduler = Scheduler()
