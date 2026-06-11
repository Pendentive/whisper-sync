"""Periodic heartbeat log line.

A background daemon thread writes a short DEBUG line every ``interval``
seconds. The purpose is forensic: if the app dies silently, the gap between
the last heartbeat and the next restart banner pins down the time-of-death.
"""

from __future__ import annotations

import logging
import os
import threading
import time


class Heartbeat:
    """Periodic heartbeat emitter. Start/stop are idempotent."""

    def __init__(self, logger: logging.Logger, interval: float = 60.0):
        self._logger = logger
        self._interval = float(interval)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at: float | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._started_at = time.monotonic()
        self._thread = threading.Thread(
            target=self._run, name="whisper-sync-heartbeat", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float | None = 2.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=timeout)
        self._thread = None

    def _run(self) -> None:
        pid = os.getpid()
        while not self._stop.wait(self._interval):
            start = self._started_at if self._started_at is not None else time.monotonic()
            uptime = time.monotonic() - start
            self._logger.debug(
                "heartbeat pid=%d uptime=%.1fs threads=%d rss=%dMB",
                pid,
                uptime,
                threading.active_count(),
                get_rss_mb(),
            )


def get_rss_mb() -> int:
    """Current process working-set size in MB. Returns 0 if unavailable.

    Uses Win32 GetProcessMemoryInfo via ctypes (no psutil dependency).
    Logged in every heartbeat so memory growth incidents can be diagnosed
    from the forensic log instead of guessing ("absorbing too much memory
    at certain times" needs a time series to pin down WHICH times).
    """
    if os.name != "nt":
        return 0
    try:
        import ctypes
        import ctypes.wintypes as w

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", w.DWORD),
                ("PageFaultCount", w.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi
        # Explicit prototypes matter: GetCurrentProcess returns the
        # pseudo-handle -1; without HANDLE restype/argtypes ctypes passes
        # it as a 32-bit int, which truncates on 64-bit and makes
        # GetProcessMemoryInfo fail with ERROR_INVALID_HANDLE.
        kernel32.GetCurrentProcess.restype = w.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            w.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), w.DWORD
        ]
        psapi.GetProcessMemoryInfo.restype = w.BOOL

        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        if psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            return int(counters.WorkingSetSize // (1024 * 1024))
    except Exception:
        pass
    return 0
