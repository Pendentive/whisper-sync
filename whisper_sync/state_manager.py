"""Centralized state management with typed semantic events.

StateManager is the single source of truth for app mode, icon state, and
notification dispatch.  Any component (tray icon, toast notifications, a
future API server or web shell) subscribes via on() or on_any() and reacts
to typed StateEvent objects.

Thread-safe: emit() acquires a lock before mutating state.  Listeners are
called outside the lock so they can safely read state.current without
deadlocking.
"""

import logging
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field, replace
from typing import Callable

_logger = logging.getLogger("whisper_sync.state")

# ---------------------------------------------------------------------------
# Event type constants
# ---------------------------------------------------------------------------

MEETING_STARTED = "meeting_started"
MEETING_STOPPED = "meeting_stopped"
MEETING_COMPLETED = "meeting_completed"
DICTATION_STARTED = "dictation_started"
DICTATION_COMPLETED = "dictation_completed"
DICTATION_DISCARDED = "dictation_discarded"
TRANSCRIPTION_STARTED = "transcription_started"
TRANSCRIPTION_PROGRESS = "transcription_progress"
TRANSCRIPTION_COMPLETED = "transcription_completed"
# Future - not yet emitted by codebase:
SUMMARIZATION_STARTED = "summarization_started"
SUMMARIZATION_COMPLETED = "summarization_completed"
ERROR = "error"
MODEL_LOADING = "model_loading"
MODEL_DOWNLOADING = "model_downloading"
MODEL_READY = "model_ready"
PR_STATUS_CHANGED = "pr_status_changed"
SPEAKER_HEALTH_CHANGED = "speaker_health_changed"
QUEUED = "queued"
IDLE = "idle"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class AppState:
    """Snapshot of the application's visual/logical state."""

    mode: str | None = None
    """None, "meeting", "dictation", "saving", "transcribing", "done", "error"."""

    meeting_transcribing: bool = False
    """True while meeting transcription runs in the background."""

    dictation_overlay: bool = False
    """True while dictation is active during a meeting."""

    speaker_ok: bool = True
    """Speaker loopback health (outer ring indicator)."""

    progress: float | None = None
    """0.0-1.0 for progress ring, None = no progress shown."""


@dataclass
class StateEvent:
    """A typed, timestamped state transition."""

    type: str
    """Semantic event name (one of the constants above)."""

    timestamp: float
    """time.time() when the event was created."""

    old_state: AppState
    """State snapshot before the transition."""

    new_state: AppState
    """State snapshot after the transition."""

    data: dict = field(default_factory=dict)
    """Event-specific payload (words, progress, message, etc.)."""


# ---------------------------------------------------------------------------
# StateManager
# ---------------------------------------------------------------------------

class StateManager:
    """Observable state machine for WhisperSync.

    Usage::

        state = StateManager(tray, config)
        state.emit(MEETING_STARTED, mode="meeting")
        state.emit(TRANSCRIPTION_PROGRESS, progress=0.45, data={"stage": "alignment"})
        state.on(MEETING_COMPLETED, lambda e: print(e.data))
    """

    def __init__(self, tray, config: dict):
        self._state = AppState()
        self._lock = threading.Lock()
        self._event_log: deque[StateEvent] = deque(maxlen=100)
        self._typed_listeners: dict[str, list[Callable]] = defaultdict(list)
        self._global_listeners: list[Callable] = []
        self._tray = tray
        self._config = config

    # -- Core API ----------------------------------------------------------

    def emit(self, event_type: str, *, data: dict | None = None, **state_changes):
        """Emit a typed event, update state, notify all listeners.

        Thread-safe.  Listeners are called outside the lock.  Listeners
        MUST NOT call emit() recursively (would deadlock).  If a listener
        needs to trigger a follow-up state change, schedule it on a
        background thread.
        """
        with self._lock:
            old = replace(self._state)
            for k, v in state_changes.items():
                if hasattr(self._state, k):
                    setattr(self._state, k, v)
            event = StateEvent(
                type=event_type,
                timestamp=time.time(),
                old_state=old,
                new_state=replace(self._state),
                data=data or {},
            )
            self._event_log.append(event)

        # Notify outside lock
        for cb in self._typed_listeners.get(event_type, []):
            try:
                cb(event)
            except Exception:
                _logger.exception("Typed listener error for %s", event_type)
        for cb in self._global_listeners:
            try:
                cb(event)
            except Exception:
                _logger.exception("Global listener error for %s", event_type)

    def on(self, event_type: str, callback: Callable[[StateEvent], None]):
        """Subscribe to a specific event type."""
        self._typed_listeners[event_type].append(callback)

    def on_any(self, callback: Callable[[StateEvent], None]):
        """Subscribe to all events (for API server, dashboard, etc.)."""
        self._global_listeners.append(callback)

    # -- Properties --------------------------------------------------------

    @property
    def current(self) -> AppState:
        """Thread-safe snapshot of current state."""
        with self._lock:
            return replace(self._state)

    @property
    def history(self) -> list[StateEvent]:
        """Copy of the event log (most recent 100 events)."""
        with self._lock:
            return list(self._event_log)
