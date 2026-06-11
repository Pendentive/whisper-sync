"""Scheduler-based return-to-idle with optional done-blink.

Retires the last per-event ephemeral thread from the stability rebuild
plan (Phase 1b part 2): ``_schedule_idle`` spawned a fresh sleeping
thread after every completed dictation and meeting just to wait out a
delay (and optionally blink the done icon) before resetting state. The
delay and each blink frame are now short scheduler jobs.

Semantics preserved exactly from the thread version: the mode is only
reset if still terminal (done/error/None) - a recording the user
started during the delay must never be overwritten - and the blink
chain aborts silently as soon as the mode leaves done/None.
"""

from __future__ import annotations

from .logger import logger
from .scheduler import scheduler
from .state_manager import IDLE

BLINK_CYCLES = 3
BLINK_ON_S = 0.4
BLINK_OFF_S = 0.3


def schedule_idle_reset(state, seconds: float, blink: bool = False) -> None:
    """Return ``state`` to idle, plain-delayed or via the done-blink.

    With ``blink=False``, resets after ``seconds``. With ``blink=True``
    and the mode currently ``done``, runs the fixed blink choreography
    instead - ``seconds`` is ignored and the total time is the
    BLINK_CYCLES * (BLINK_ON_S + BLINK_OFF_S) chain (matching the
    historical thread version). ``blink=True`` in any other mode
    degrades to the plain ``seconds`` delay.
    """

    def _finish():
        if state.current.mode in ("done", "error", None):
            state.emit(IDLE, mode=None)
        else:
            logger.debug(
                f"idle reset skipped - mode is '{state.current.mode}' (not terminal)"
            )

    if not (blink and state.current.mode == "done"):
        scheduler.call_later(seconds, _finish, label="idle-reset")
        return

    total_steps = BLINK_CYCLES * 2

    def _step(i: int):
        if i >= total_steps:
            _finish()
            return
        if state.current.mode not in ("done", None):
            return  # user started something new - abort the blink
        if i % 2 == 0:
            state.emit(IDLE, mode="done")
            delay = BLINK_ON_S
        else:
            state.emit(IDLE, mode=None)
            delay = BLINK_OFF_S
        scheduler.call_later(delay, lambda: _step(i + 1), label="idle-blink")

    # First frame goes through the scheduler too, so the caller (often a
    # pipeline finally-block) never emits synchronously.
    scheduler.call_later(0.0, lambda: _step(0), label="idle-blink")
