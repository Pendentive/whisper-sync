"""Tests for whisper_sync.idle_reset - scheduler-based return-to-idle.

The behavioral contract carried over from the thread version: never
overwrite a mode the user set during the delay, blink done/None three
times when asked, and abort the blink silently if a new recording
starts mid-chain.
"""

import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from whisper_sync import idle_reset
from whisper_sync.idle_reset import schedule_idle_reset
from whisper_sync.state_manager import IDLE


class _FakeState:
    """Records emits (and their thread) and tracks mode like StateManager."""

    def __init__(self, mode="done"):
        self.current = SimpleNamespace(mode=mode)
        self.events = []
        self.emit_threads = []

    def emit(self, event_type, mode=None, **_kw):
        self.events.append((event_type, mode))
        self.emit_threads.append(threading.current_thread())
        self.current.mode = mode


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class PlainDelayTests(unittest.TestCase):
    def test_resets_to_idle_after_delay(self):
        state = _FakeState(mode="done")
        schedule_idle_reset(state, 0.02)
        self.assertTrue(_wait_for(lambda: state.events))
        self.assertEqual(state.events, [(IDLE, None)])

    def test_does_not_overwrite_new_recording(self):
        state = _FakeState(mode="done")
        schedule_idle_reset(state, 0.05)
        state.current.mode = "dictation"  # user started something new
        time.sleep(0.15)
        self.assertEqual(state.events, [], "non-terminal mode must not be reset")

    def test_blink_true_without_done_mode_degrades_to_plain_delay(self):
        state = _FakeState(mode="error")
        schedule_idle_reset(state, 0.02, blink=True)
        self.assertTrue(_wait_for(lambda: state.events))
        self.assertEqual(state.events, [(IDLE, None)])


class BlinkChainTests(unittest.TestCase):
    def setUp(self):
        # Real blink timing is 2.1s; compress it for tests.
        patches = [
            mock.patch.object(idle_reset, "BLINK_ON_S", 0.01),
            mock.patch.object(idle_reset, "BLINK_OFF_S", 0.01),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_blink_emits_three_cycles_then_resets(self):
        state = _FakeState(mode="done")
        schedule_idle_reset(state, 0.02, blink=True)
        # 6 blink frames (done/None x3) + the final reset.
        self.assertTrue(
            _wait_for(lambda: len(state.events) >= 7),
            f"expected 7 emits, saw {state.events}",
        )
        modes = [m for _, m in state.events]
        self.assertEqual(modes, ["done", None, "done", None, "done", None, None])
        self.assertTrue(all(e == IDLE for e, _ in state.events))

    def test_blink_aborts_when_user_starts_new_recording(self):
        state = _FakeState(mode="done")
        schedule_idle_reset(state, 0.02, blink=True)
        self.assertTrue(_wait_for(lambda: len(state.events) >= 1))
        state.current.mode = "meeting"  # mid-chain interruption
        events_at_abort = len(state.events)
        time.sleep(0.3)
        self.assertLessEqual(
            len(state.events), events_at_abort + 1,
            "blink chain must stop once the mode leaves done/None",
        )
        self.assertLess(len(state.events), 7, "chain must not run to completion")
        self.assertNotEqual(
            state.current.mode, None,
            "aborted chain must not fire the final reset",
        )

    def test_caller_does_not_emit_synchronously(self):
        # The first frame goes through the scheduler: pipeline
        # finally-blocks must not re-enter state.emit on their own stack.
        # Assert on the emitting thread rather than on timing - the
        # scheduler may legitimately fire before the caller's next line.
        state = _FakeState(mode="done")
        schedule_idle_reset(state, 0.02, blink=True)
        self.assertTrue(_wait_for(lambda: state.events))
        self.assertNotIn(
            threading.current_thread(), state.emit_threads,
            "emits must never run on the caller's stack",
        )


if __name__ == "__main__":
    unittest.main()
