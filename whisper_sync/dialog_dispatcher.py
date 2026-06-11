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

Second-generation fix (stability rebuild Phase 3): the dispatcher also
owns ONE persistent hidden ``tk.Tk()`` root. Dialogs submitted with
``wants_root=True`` receive it and build ``tk.Toplevel`` children instead
of creating/destroying a full Tk (Tcl interpreter) per dialog. The
create/destroy churn was the dominant crash family in the May-June 2026
production logs: access violations in ``tkinter __del__`` and
interpreter-shutdown GC faults over orphaned Tcl objects. The root is
created lazily on the dispatcher thread and destroyed exactly once, on
that same thread, at shutdown.

Scope: this covers the TRAY APP runtime (every dialog in __main__.py).
installer_gui.py is a separate standalone installer process with its own
single mainloop and is unaffected by per-dialog interpreter churn.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Callable

logger = logging.getLogger(__name__)


class _DialogRequest:
    """Internal: a single dialog call queued for the dispatcher thread."""

    __slots__ = ("fn", "label", "wants_root", "result", "exc", "done")

    def __init__(self, fn: Callable[..., Any], label: str, wants_root: bool):
        self.fn = fn
        self.label = label
        self.wants_root = wants_root
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

    def __init__(self, name: str = "dialog-dispatcher", tk_factory=None):
        self._name = name
        self._queue: "queue.Queue[Any]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._started = False
        self._lock = threading.Lock()
        # Persistent hidden Tk root. Created lazily ON the dispatcher
        # thread at the first wants_root dialog and destroyed exactly once
        # at shutdown. Eliminates the tk.Tk() create/destroy churn that
        # leaves Tcl interpreter objects whose __del__ crashes with access
        # violations (the dominant crash family in May-June 2026 logs:
        # "tkinter/__init__.py line 414 in __del__" + shutdown-GC faults).
        self._root = None
        # Test seam: returns a hidden root window. Defaults to real tkinter.
        self._tk_factory = tk_factory if tk_factory is not None else self._default_tk_factory

    @staticmethod
    def _default_tk_factory():
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()  # never shown; dialogs are Toplevel children
        return root

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
        """Signal the dispatcher to stop and wait for it to exit.

        State is cleared ONLY when the thread has actually exited. If the
        join times out (e.g. a dialog is still open), the dispatcher stays
        marked started: clearing state would let a later start() spawn a
        SECOND dispatcher thread that reuses the persistent Tk root created
        on the first thread - violating Tk thread-affinity and
        reintroducing the exact crash class this class exists to prevent.
        """
        with self._lock:
            if not self._started:
                return
            self._queue.put(self._SENTINEL)
        t = self._thread
        if t is not None:
            t.join(timeout=timeout)
            if t.is_alive():
                logger.warning(
                    "DialogDispatcher shutdown timed out (dialog still open?); "
                    "keeping dispatcher state intact"
                )
                return
        with self._lock:
            self._started = False
            self._thread = None

    def run(self, fn: Callable[..., Any], label: str = "dialog",
            wants_root: bool = False) -> Any:
        """Submit a dialog callable to the dispatcher and wait for its result.

        The callable runs on the dispatcher thread. Two calling styles:

        - ``wants_root=True`` (preferred): ``fn(root)`` receives the
          persistent hidden Tk root. The dialog builds a ``tk.Toplevel``
          child, runs ``root.wait_window(dlg)``, and never creates or
          destroys a Tk/Tcl interpreter.
        - ``wants_root=False`` (legacy): ``fn()`` manages its own tkinter
          lifecycle. Still serialized, but churns a Tcl interpreter per
          call - migrate these.

        Whatever fn returns becomes the return value of ``run``. If it
        raises, the exception is re-raised in the caller's thread.
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
            if wants_root:
                return fn(self._ensure_root())
            return fn()

        req = _DialogRequest(fn, label, wants_root)
        self._queue.put(req)
        # No timeout: dialogs intentionally have no hard cap. The user may
        # take as long as they need to fill in a meeting name or confirm
        # speakers. This matches the prior behavior of ``event.wait()``
        # without an argument.
        req.done.wait()
        if req.exc is not None:
            raise req.exc
        return req.result

    def _ensure_root(self):
        """Create the persistent hidden root if needed. Dispatcher thread only."""
        if self._root is None:
            self._root = self._tk_factory()
            logger.debug("DialogDispatcher created persistent Tk root")
        return self._root

    def _destroy_root(self) -> None:
        """Destroy the persistent root exactly once. Dispatcher thread only."""
        if self._root is not None:
            try:
                self._root.destroy()
                logger.debug("DialogDispatcher destroyed persistent Tk root")
            except Exception:
                logger.debug("Persistent Tk root destroy failed", exc_info=True)
            self._root = None

    def _run_loop(self) -> None:
        try:
            while True:
                item = self._queue.get()
                if item is self._SENTINEL:
                    logger.debug("DialogDispatcher received shutdown sentinel")
                    return
                req: _DialogRequest = item
                try:
                    if req.wants_root:
                        req.result = req.fn(self._ensure_root())
                    else:
                        req.result = req.fn()
                except BaseException as e:  # noqa: BLE001 - hand exception to caller
                    req.exc = e
                    logger.exception("DialogDispatcher: %r raised", req.label)
                finally:
                    req.done.set()
        finally:
            # The root must die on ITS OWN thread, deterministically -
            # never via interpreter-shutdown GC on some other thread
            # (that is exactly the crash mode this class exists to fix).
            self._destroy_root()
