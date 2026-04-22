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
                "heartbeat pid=%d uptime=%.1fs threads=%d",
                pid,
                uptime,
                threading.active_count(),
            )
