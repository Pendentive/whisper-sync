"""System suspend/resume detection via RegisterSuspendResumeNotification.

Before this module the app had no power-event handling at all: open
PortAudio streams, a live CUDA context in the worker, and in-flight
transcription all ran blindly across laptop suspend/resume - the single
most plausible trigger moment for a GPU driver fault (2026-07-03
hardware-resilience spec, H1).

Mechanism: the Win8+ callback registration
(``user32!RegisterSuspendResumeNotification`` with
``DEVICE_NOTIFY_CALLBACK``). A message window would NOT work here -
WM_POWERBROADCAST is a broadcast, and message-only (HWND_MESSAGE)
windows never receive broadcasts - and a visible top-level window is
unwanted in a tray app, so the callback registration is both the
simplest and the correct mechanism.

Dispatch:
- ``on_suspend`` at PBT_APMSUSPEND (system is about to sleep)
- ``on_resume`` at PBT_APMRESUMEAUTOMATIC (fires for every wake;
  PBT_APMRESUMESUSPEND additionally fires only for user-present wake,
  so dispatching only the AUTOMATIC one avoids double callbacks)

Callbacks run ON AN OS CALLBACK THREAD and must be cheap; wiring
offloads real work to the IO executor. Non-Windows: start() no-ops.
"""

from __future__ import annotations

import os
import threading
from typing import Callable, Optional

from .logger import logger

PBT_APMSUSPEND = 0x0004
PBT_APMRESUMEAUTOMATIC = 0x0012
DEVICE_NOTIFY_CALLBACK = 2


class PowerEventListener:
    """Suspend/resume notifications translated to python callbacks."""

    def __init__(self, on_suspend: Optional[Callable[[], None]] = None,
                 on_resume: Optional[Callable[[], None]] = None):
        self._on_suspend = on_suspend
        self._on_resume = on_resume
        self._registration = None
        self._callback_ref = None  # prevent GC of the ctypes callback
        self._params_ref = None
        self._lock = threading.Lock()

    def handle_power_event(self, event_type: int) -> None:
        """Route one power event. Separated from the OS callback so the
        dispatch logic is unit-testable without registration."""
        if event_type == PBT_APMSUSPEND:
            logger.info("power events: system suspending")
            if self._on_suspend:
                try:
                    self._on_suspend()
                except Exception:
                    logger.exception("power events: on_suspend raised")
        elif event_type == PBT_APMRESUMEAUTOMATIC:
            logger.info("power events: system resumed")
            if self._on_resume:
                try:
                    self._on_resume()
                except Exception:
                    logger.exception("power events: on_resume raised")

    def start(self) -> bool:
        """Register for notifications. True when active. No-op elsewhere."""
        if os.name != "nt":
            return False
        with self._lock:
            if self._registration is not None:
                return True
            import ctypes
            from ctypes import wintypes as w

            user32 = ctypes.WinDLL("user32", use_last_error=True)

            CBTYPE = ctypes.WINFUNCTYPE(
                w.DWORD, ctypes.c_void_p, w.ULONG, ctypes.c_void_p
            )

            def _cb(context, event_type, setting):
                try:
                    self.handle_power_event(int(event_type))
                except Exception:
                    logger.exception("power events: callback raised")
                return 0

            class SUBSCRIBE_PARAMS(ctypes.Structure):
                _fields_ = [("Callback", CBTYPE), ("Context", ctypes.c_void_p)]

            self._callback_ref = CBTYPE(_cb)
            self._params_ref = SUBSCRIBE_PARAMS(self._callback_ref, None)

            user32.RegisterSuspendResumeNotification.restype = w.HANDLE
            user32.RegisterSuspendResumeNotification.argtypes = (
                ctypes.c_void_p, w.DWORD,
            )
            handle = user32.RegisterSuspendResumeNotification(
                ctypes.byref(self._params_ref), DEVICE_NOTIFY_CALLBACK
            )
            if not handle:
                logger.warning(
                    f"power events: registration failed (err={ctypes.get_last_error()})"
                )
                self._callback_ref = None
                self._params_ref = None
                return False
            self._registration = handle
            logger.info("power events: suspend/resume notifications active")
            return True

    def stop(self) -> None:
        with self._lock:
            if self._registration is None:
                return
            import ctypes
            from ctypes import wintypes as w
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.UnregisterSuspendResumeNotification.restype = w.BOOL
            user32.UnregisterSuspendResumeNotification.argtypes = (w.HANDLE,)
            user32.UnregisterSuspendResumeNotification(self._registration)
            self._registration = None
            self._callback_ref = None
            self._params_ref = None
