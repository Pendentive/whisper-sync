"""Thread-safe session statistics.

The app previously kept session stats in a bare dict mutated concurrently
from the dictation worker, the overlay dictation worker, and the meeting
post-processing pipeline with no lock (stability rebuild plan section
3.2). Increments raced (`d[k] += v` is read-modify-write) and the menu
read mid-update values.

This class owns the counters behind a lock and exposes increment methods
plus an atomic ``snapshot()`` for the stats menu.
"""

from __future__ import annotations

import threading
from datetime import datetime


class SessionStats:
    """Lock-guarded per-session counters."""

    def __init__(self):
        self._lock = threading.Lock()
        self._session_start = datetime.now()
        self._counts = {
            "dictations": 0,
            "meetings": 0,
            "feature_suggestions": 0,
            "total_dictation_chars": 0,
            "total_dictation_time": 0.0,
            "total_meeting_seconds": 0,
            "total_meeting_words": 0,
        }

    # -- increments ---------------------------------------------------------

    def record_dictation(self, chars: int, duration_s: float) -> None:
        with self._lock:
            self._counts["dictations"] += 1
            self._counts["total_dictation_chars"] += chars
            self._counts["total_dictation_time"] += duration_s

    def record_meeting(self, seconds: int, words: int) -> None:
        with self._lock:
            self._counts["meetings"] += 1
            self._counts["total_meeting_seconds"] += int(seconds)
            self._counts["total_meeting_words"] += words

    def record_feature_suggestion(self) -> None:
        with self._lock:
            self._counts["feature_suggestions"] += 1

    # -- reads --------------------------------------------------------------

    @property
    def session_start(self) -> datetime:
        return self._session_start

    def snapshot(self) -> dict:
        """Atomic copy of all counters plus session_start."""
        with self._lock:
            snap = dict(self._counts)
        snap["session_start"] = self._session_start
        return snap
