"""Named long-lived single-thread executors with native-call accounting.

WhisperSync historically spawned a fresh ``threading.Thread(daemon=True)``
for almost every action (~20 spawn sites). That churn is a structural
cause of the app's Windows heap instability and makes it impossible to
know when the process is quiescent (which the idle GC collector needs).

This module replaces the spawn sites with a small fixed set of executors:

    from .executors import DICTATION, IO
    DICTATION.submit("transcribe", lambda: ...)
    IO.submit_native("claude-minutes", lambda: subprocess.run(...))

Rules:
- Each executor is ONE long-lived daemon thread processing jobs serially.
- Exceptions are logged and never kill the thread.
- ``submit_native`` marks a job as entering native/subprocess code; the
  global ``native_calls_in_flight()`` gauge counts these so the idle GC
  collector can prove no thread is mid-native-call before collecting.
- The queue is bounded as a backstop against runaway producers; rejects
  are logged loudly rather than blocking the caller.
"""

from __future__ import annotations

import queue
import threading
from typing import Callable

from .logger import logger

_QUEUE_MAX = 64

# Global gauge of jobs currently executing native/subprocess code across
# ALL executors. Guarded by its own lock; read via native_calls_in_flight().
_native_lock = threading.Lock()
_native_count = 0


def native_calls_in_flight() -> int:
    """Number of executor jobs currently inside native/subprocess code."""
    with _native_lock:
        return _native_count


def _native_enter() -> None:
    global _native_count
    with _native_lock:
        _native_count += 1


def _native_exit() -> None:
    global _native_count
    with _native_lock:
        _native_count -= 1


class native_call:
    """Context manager marking a native/subprocess section on ANY thread.

    Code that has not yet migrated onto an executor (github poller,
    speaker identification, Claude CLI calls from recovery threads) wraps
    its subprocess/native sections so the idle-GC quiescence check sees
    them:

        with native_call("claude-minutes"):
            subprocess.run([...])

    Without this, gc.collect() from the idle collector could race an
    active subprocess.communicate() on an unmigrated thread — the exact
    crash PR #135 removed the #134 checkpoints for.
    """

    __slots__ = ("label",)

    def __init__(self, label: str = "native"):
        self.label = label

    def __enter__(self):
        _native_enter()
        return self

    def __exit__(self, exc_type, exc, tb):
        _native_exit()
        return False


class _Job:
    __slots__ = ("fn", "label", "native")

    def __init__(self, fn: Callable[[], None], label: str, native: bool):
        self.fn = fn
        self.label = label
        self.native = native


class Executor:
    """A named single-thread serial executor."""

    _SENTINEL = object()

    def __init__(self, name: str, queue_max: int = _QUEUE_MAX):
        self.name = name
        self._queue: "queue.Queue" = queue.Queue(maxsize=queue_max)
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._active = threading.Event()  # set while a job is running
        self._shutdown = False

    # -- public API --------------------------------------------------------

    def submit(self, label: str, fn: Callable[[], None]) -> bool:
        """Queue ``fn`` for serial execution. Returns False if rejected."""
        return self._submit(_Job(fn, label, native=False))

    def submit_native(self, label: str, fn: Callable[[], None]) -> bool:
        """Queue a job that enters native/subprocess code.

        Counted in the global ``native_calls_in_flight`` gauge for the
        duration of the job so idle-GC can prove quiescence.
        """
        return self._submit(_Job(fn, label, native=True))

    def idle(self) -> bool:
        """True when no job is running and the queue is empty."""
        return not self._active.is_set() and self._queue.empty()

    def pending(self) -> int:
        """Approximate number of queued (not yet started) jobs."""
        return self._queue.qsize()

    def shutdown(self, timeout: float | None = 2.0) -> None:
        """Stop the executor thread after the current job. Idempotent.

        Pending jobs are dropped (the sentinel is pushed to the FRONT
        conceptually by draining first).
        """
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
        # Drain pending jobs so the sentinel is consumed next.
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass
        try:
            self._queue.put_nowait(self._SENTINEL)
        except queue.Full:
            pass
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)

    # -- internals ---------------------------------------------------------

    def _submit(self, job: _Job) -> bool:
        with self._lock:
            if self._shutdown:
                logger.warning(
                    "executor %s rejected %r: shut down", self.name, job.label
                )
                return False
            self._ensure_thread_locked()
        try:
            self._queue.put_nowait(job)
            return True
        except queue.Full:
            logger.error(
                "executor %s queue full (%d); rejected %r",
                self.name, _QUEUE_MAX, job.label,
            )
            return False

    def _ensure_thread_locked(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._run, name=f"ws-exec-{self.name}", daemon=True
            )
            self._thread.start()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is self._SENTINEL:
                return
            job: _Job = item
            self._active.set()
            if job.native:
                _native_enter()
            try:
                job.fn()
            except Exception:
                logger.exception(
                    "executor %s job %r raised", self.name, job.label
                )
            finally:
                if job.native:
                    _native_exit()
                self._active.clear()


# Process-wide named executors. Threads start lazily on first submit.
DICTATION = Executor("dictation")   # dictation + overlay transcription jobs
IO = Executor("io")                 # subprocess calls, file ops, recovery


def all_idle() -> bool:
    """True when every executor is idle and no native call is in flight."""
    return DICTATION.idle() and IO.idle() and native_calls_in_flight() == 0
