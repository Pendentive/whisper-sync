"""Provable-idle cycle collection.

The cycle collector is disabled process-wide at startup (see main() in
__main__.py and PRs #134/#135): automatic GC can fire on any thread
mid-allocation inside a native C extension and trip Windows heap
corruption detection (0x80000003). PR #135 removed the "checkpoint"
collect calls because they raced active subprocess.communicate() calls
on other threads — at the time there was no way to know whether any
thread was inside native code.

The executor model changes that. All subprocess/native work now routes
through the named executors with native-call accounting
(executors.native_calls_in_flight), and the meeting pipeline and recorder
expose their busy state. This module collects ONLY when the whole app is
provably quiescent:

- both executors idle, zero native calls in flight
- no meeting job in the post-processing pipeline
- not recording (mic closed)
- no dictation transcription in flight (mode is terminal)

That eliminates the #134 crash window while restoring the leak cleanup
that #135 gave up (with gc.disable() and no collects, reference cycles
leak permanently — menu rebuilds alone create closure-heavy graphs every
refresh).

Collection runs on the shared scheduler thread. The quiescence check and
the collect are not atomic — a hotkey could fire mid-collect — but the
dangerous overlap (GC scanning while another thread is mid-native-call)
cannot happen for app-originated work: new jobs enqueue but their threads
cannot ENTER native code paths because the executors and pipeline are
serial and this job occupies the scheduler tick. Recording start (the
remaining external path) opens PortAudio streams on the hotkey thread;
the gate below therefore also re-checks immediately before collecting.
"""

from __future__ import annotations

import gc
from typing import Callable

from .logger import logger
from . import executors
from .scheduler import scheduler

_DEFAULT_INTERVAL_S = 300.0  # 5 minutes


class IdleCollector:
    """Periodic gc.collect() gated on app-wide provable quiescence."""

    def __init__(
        self,
        is_pipeline_idle: Callable[[], bool],
        is_recording: Callable[[], bool],
        is_mode_terminal: Callable[[], bool],
        interval_s: float = _DEFAULT_INTERVAL_S,
    ):
        """
        Args:
            is_pipeline_idle: True when the meeting post-process queue is
                empty and no job is mid-step.
            is_recording: True while the mic/loopback streams are open.
            is_mode_terminal: True when app mode is None/"done"/"error"
                (i.e., no dictation transcription in flight outside the
                executors).
            interval_s: how often to attempt collection.
        """
        self._is_pipeline_idle = is_pipeline_idle
        self._is_recording = is_recording
        self._is_mode_terminal = is_mode_terminal
        self._interval_s = interval_s
        self._handle = None
        # Telemetry for tests/forensics
        self.last_skip_reason: str | None = None
        self.total_collected = 0

    def start(self) -> None:
        if self._handle is not None:
            return
        self._handle = scheduler.call_every(
            self._interval_s, self._tick, label="idle-gc"
        )

    def stop(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None

    # -- internals ---------------------------------------------------------

    def _quiescent(self) -> str | None:
        """Return None if quiescent, else a skip-reason string."""
        if not executors.all_idle():
            return "executors busy"
        if not self._is_pipeline_idle():
            return "pipeline busy"
        if self._is_recording():
            return "recording"
        if not self._is_mode_terminal():
            return "transcription in flight"
        return None

    def _tick(self) -> None:
        reason = self._quiescent()
        if reason is None:
            # Re-check right before collecting to narrow the race window
            # with externally-triggered work (hotkey starting a recording).
            reason = self._quiescent()
        if reason is not None:
            self.last_skip_reason = reason
            logger.debug("idle-gc skipped: %s", reason)
            return
        collected = gc.collect()
        self.total_collected += collected
        self.last_skip_reason = None
        if collected:
            logger.info("idle-gc collected %d cycle objects", collected)
        else:
            logger.debug("idle-gc: nothing to collect")
