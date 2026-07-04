"""Auto-sleep - unload the model from VRAM when idle, wake on demand.

The transcription worker holds the model in VRAM for the whole session.
AutoSleep frees that memory two ways:

- **Automatic**: after ``auto_sleep_minutes`` (default 30, 0 disables)
  with no dictation/meeting/transcription activity, the worker
  subprocess is stopped - process exit releases ALL of its VRAM.
- **Manual**: double-clicking the tray icon toggles sleep (for gaming
  and other VRAM-hungry work). ClickRouter below distinguishes the
  double click from the configured single-click action.

While asleep the tray shows the "sleep" icon (deeper gray middle,
normal gray outer ring). Any dictation or meeting toggle wakes the
model with the usual yellow loading double-flash AND starts recording
immediately - both flows are disk-first, so capture never waits on the
model (exception: whisper/incognito dictation is RAM-only and still
requires a loaded model). Meetings transcribe at their own pipeline step; dictation waits
for readiness at stop time (the yellow state simply lasts longer).

Follows the GPU Guard feature pattern: one owning module, flat config
key, fully inert when disabled. Sleep and wake events land in
gpu-guard.jsonl so VRAM lifecycle stays on the crash-correlation
timeline.
"""

import threading
import time

from .executors import IO, submit_or_spawn
from .logger import logger
from .notifications import notify
from .state_manager import (
    SLEEP_STARTED, SLEEP_ENDED,
    DICTATION_STARTED, DICTATION_COMPLETED, DICTATION_DISCARDED,
    MEETING_STARTED, MEETING_STOPPED, MEETING_COMPLETED,
    TRANSCRIPTION_STARTED, TRANSCRIPTION_PROGRESS, TRANSCRIPTION_COMPLETED,
    QUEUED,
)

# Events that count as "the model is in use" for the idle clock.
# Deliberately excludes PR_STATUS_CHANGED (GitHub polling), MODEL_* and
# SPEAKER_HEALTH_CHANGED - none of them mean the user needs the model.
_ACTIVITY_EVENTS = frozenset({
    DICTATION_STARTED, DICTATION_COMPLETED, DICTATION_DISCARDED,
    MEETING_STARTED, MEETING_STOPPED, MEETING_COMPLETED,
    TRANSCRIPTION_STARTED, TRANSCRIPTION_PROGRESS, TRANSCRIPTION_COMPLETED,
    QUEUED,
})

_CHECK_INTERVAL_S = 60.0

# Windows' default double-click time is 500ms; the single action is
# deferred this long so a second click can claim the pair. Left-click
# actions (toggle meeting/dictation, discard) are not latency-critical -
# hotkeys are the fast path.
DOUBLE_CLICK_WINDOW_S = 0.45


class ClickRouter:
    """Route tray-icon clicks: single fires one action, double another.

    pystray's Win32 backend activates the default menu item on every
    WM_LBUTTONUP, so a double click arrives as two activations in quick
    succession. The first schedules the single action after
    DOUBLE_CLICK_WINDOW_S; a second click inside the window cancels it
    and fires the double action instead. Thread-safe: activations come
    from the pystray pump, the timer from the scheduler thread.
    """

    def __init__(self, single, double, window_s: float = DOUBLE_CLICK_WINDOW_S):
        self._single = single
        self._double = double
        self._window_s = window_s
        self._lock = threading.Lock()
        self._pending = None  # scheduler handle for the deferred single
        self._deadline = 0.0  # monotonic time the pending single expires

    def click(self):
        from .scheduler import scheduler
        now = time.monotonic()
        with self._lock:
            # The deadline guard makes a stale pending harmless: if the
            # scheduler was shut down (teardown) the handle never fires
            # and never clears, but once the window has passed the next
            # click is a fresh single, not a phantom double.
            if self._pending is not None and now < self._deadline:
                self._pending.cancel()
                self._pending = None
                fire_double = True
            else:
                fire_double = False
                self._pending = scheduler.call_later(
                    self._window_s, self._fire_single, label="tray-click")
                self._deadline = now + self._window_s
        if fire_double:
            self._double()

    def _fire_single(self):
        with self._lock:
            self._pending = None
        self._single()


class AutoSleep:
    """Owns model sleep/wake; reads shared services through ``app``."""

    def __init__(self, app):
        self.app = app
        self._last_activity = time.monotonic()

    # -- Wiring (called from run()) -------------------------------------------

    def start(self, scheduler):
        """Subscribe to activity events and start the idle checker."""
        self.app.state.on_any(self._on_event)
        scheduler.call_every(_CHECK_INTERVAL_S, self._check, label="auto-sleep")

    def _on_event(self, event):
        if event.type in _ACTIVITY_EVENTS:
            self._last_activity = time.monotonic()

    # -- State ----------------------------------------------------------------

    @property
    def sleeping(self) -> bool:
        state = self.app.state
        return bool(state and state.current.sleeping)

    def _busy(self) -> bool:
        """True while sleeping now would interrupt real work."""
        current = self.app.state.current if self.app.state else None
        if current is None:
            return True  # not fully started; never sleep during startup
        return (
            current.mode not in (None, "done", "error")
            or current.meeting_transcribing
            or current.dictation_overlay
            or self.app.recorder.is_recording
        )

    def _check(self):
        """Idle checker (scheduler, every minute). Cheap by contract."""
        try:
            minutes = float(self.app.cfg.get("auto_sleep_minutes", 30) or 0)
            if minutes <= 0 or self.sleeping:
                return
            if self._busy():
                # Busy periods emit no mid-flight events; keep the clock
                # fresh so the timeout starts when the work ends.
                self._last_activity = time.monotonic()
                return
            if time.monotonic() - self._last_activity >= minutes * 60.0:
                self.sleep(reason="idle")
        except Exception:
            logger.debug("auto-sleep check failed", exc_info=True)

    # -- Sleep / wake -----------------------------------------------------------

    def toggle(self):
        """Double-click entry point: sleep if awake, wake if asleep."""
        if self.sleeping:
            self.wake(reason="double_click")
        else:
            self.sleep(reason="double_click")

    def sleep(self, reason: str):
        """Stop the worker subprocess, freeing its VRAM."""
        if self.sleeping:
            return
        if self._busy():
            logger.info(f"Sleep refused ({reason}): recording or transcription active")
            if reason != "idle":
                notify("Can't sleep now", "A recording or transcription is active.")
            return
        logger.info(f"Model going to sleep ({reason}); unloading worker from VRAM")
        self.app.state.emit(SLEEP_STARTED, sleeping=True)
        try:
            self.app._gpu_guard.log_external_event("model_sleep", reason=reason)
        except Exception:
            pass

        def _stop():
            try:
                self.app.worker.stop()
                logger.info("Worker stopped; VRAM released")
            except Exception:
                logger.warning("Worker stop during sleep failed", exc_info=True)
            self.app._refresh_menu()

        # Off-thread: worker.stop() joins the subprocess and must not
        # block a hotkey/menu/scheduler thread.
        submit_or_spawn(IO, "sleep-stop-worker", _stop, native=True)

    def wake(self, reason: str):
        """Respawn the worker and reload the model (yellow flash while loading)."""
        if not self.sleeping:
            return
        logger.info(f"Model waking up ({reason}); reloading worker")
        # A wake IS activity: without this, a manual wake after a long
        # idle stretch would be auto-slept again on the next check tick.
        self._last_activity = time.monotonic()
        self.app.state.emit(SLEEP_ENDED, sleeping=False)
        try:
            self.app._gpu_guard.log_external_event("model_wake", reason=reason)
        except Exception:
            pass
        self.app._yellow_flash()

        def _start():
            try:
                self.app.worker.start()
                if self.app.worker.wait_ready(timeout=120):
                    logger.info("Model reloaded after sleep")
                else:
                    logger.warning("Worker failed to become ready after wake")
                    notify("Wake failed", "Model did not reload; check the log.")
            except Exception:
                logger.warning("Worker start during wake failed", exc_info=True)
            self.app._refresh_menu()

        submit_or_spawn(IO, "wake-start-worker", _start, native=True)
