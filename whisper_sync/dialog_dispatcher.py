"""Single-thread dialog dispatcher for tkinter UI calls.

Tkinter on Windows is not safe to use across short-lived rotating threads.
Creating ``tk.Tk()`` on a fresh worker thread, running ``mainloop()``, and
letting the thread exit corrupts heap/Win32 state in subtle ways that crash
the next garbage collection cycle in whichever thread happens to allocate
next. We saw this surface as Windows fatal exception ``0x80000003``
(``STATUS_BREAKPOINT``) at ``speakers.py:541`` during ``json.load`` inside
the meeting post-processing worker thread.

The fix here is to funnel all tkinter dialog work through ONE long-lived
thread (the dispatcher). Worker threads submit a callable, block on a
per-request ``Event``, and read back the result. Because every dialog runs
on the same thread, tkinter's thread-local state stays consistent across
dialogs and never gets orphaned.

This module deliberately knows nothing about the dialog implementations.
Callers pass a no-arg callable that performs the full ``tk.Tk()`` /
``mainloop()`` / ``root.destroy()`` cycle and returns whatever value the
dialog produced (or raises). The dispatcher captures the return value or
exception and hands it back to the caller's thread.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Callable

logger = logging.getLogger(__name__)


class _DialogRequest:
    """Internal: a single dialog call queued for the dispatcher thread."""

    __slots__ = ("fn", "label", "result", "exc", "done")

    def __init__(self, fn: Callable[[], Any], label: str):
        self.fn = fn
        self.label = label
        self.result: Any = None
        self.exc: BaseException | None = None
        self.done = threading.Event()


class DialogDispatcher:
    """Runs tkinter dialog callables serially on a single dedicated thread.

    Lifecycle:
        d = DialogDispatcher()
        d.start()
        ...
        result = d.run(my_dialog_fn, label="ask_meeting_name")
        ...
        d.shutdown()

    The dispatcher thread is a daemon, so the process can exit without an
    explicit ``shutdown()`` call. ``shutdown()`` is provided for clean
    teardown in tests and for graceful application shutdown.
    """

    _SENTINEL = object()

    def __init__(self, name: str = "dialog-dispatcher"):
        self._name = name
        self._queue: "queue.Queue[Any]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._started = False
        self._lock = threading.Lock()

    def start(self) -> None:
        """Start the dispatcher thread. Idempotent."""
        with self._lock:
            if self._started:
                return
            self._thread = threading.Thread(
                target=self._run_loop,
                name=self._name,
                daemon=True,
            )
            self._thread.start()
            self._started = True
            logger.debug("DialogDispatcher started")

    def shutdown(self, timeout: float | None = 5.0) -> None:
        """Signal the dispatcher to stop and wait for it to exit."""
        with self._lock:
            if not self._started:
                return
            self._queue.put(self._SENTINEL)
        t = self._thread
        if t is not None:
            t.join(timeout=timeout)
        with self._lock:
            self._started = False
            self._thread = None

    def run(self, fn: Callable[[], Any], label: str = "dialog") -> Any:
        """Submit a dialog callable to the dispatcher and wait for its result.

        The callable runs on the dispatcher thread. It must perform its own
        tkinter setup, mainloop, and teardown. Whatever it returns becomes
        the return value of ``run``. If it raises, the exception is
        re-raised in the caller's thread.

        ``label`` is used for diagnostic logging only.
        """
        if not self._started:
            # Lazy auto-start so callers don't have to remember to call
            # start() before the first dialog.
            self.start()

        # Reentrancy guard: if the caller IS the dispatcher thread (e.g. a
        # nested dialog request from within a dialog callback), just run
        # inline. Submitting to our own queue would deadlock.
        if threading.current_thread() is self._thread:
            return fn()

        req = _DialogRequest(fn, label)
        self._queue.put(req)
        # No timeout: dialogs intentionally have no hard cap. The user may
        # take as long as they need to fill in a meeting name or confirm
        # speakers. This matches the prior behavior of ``event.wait()``
        # without an argument.
        req.done.wait()
        if req.exc is not None:
            raise req.exc
        return req.result

    def _run_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is self._SENTINEL:
                logger.debug("DialogDispatcher received shutdown sentinel")
                return
            req: _DialogRequest = item
            try:
                req.result = req.fn()
            except BaseException as e:  # noqa: BLE001 - hand exception to caller
                req.exc = e
                logger.exception("DialogDispatcher: %r raised", req.label)
            finally:
                req.done.set()
