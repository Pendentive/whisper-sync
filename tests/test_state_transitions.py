"""Tests for StateManager.try_transition (stability rebuild Phase 5).

The historical race: hotkey handlers read state.current.mode, branched,
then emitted - but worker threads completing in between could change mode,
so two starts could both pass the same check. try_transition makes
check-and-set atomic under the state lock.
"""

import threading
import unittest

from whisper_sync.state_manager import (
    StateManager, DICTATION_STARTED, MEETING_STARTED, IDLE,
)


def _make_state():
    return StateManager(tray=None, config={})


class TryTransitionTests(unittest.TestCase):
    def test_transition_applies_when_mode_allowed(self):
        sm = _make_state()
        ok = sm.try_transition(
            (None, "done", "error"), DICTATION_STARTED, mode="dictation"
        )
        self.assertTrue(ok)
        self.assertEqual(sm.current.mode, "dictation")

    def test_transition_rejected_when_mode_not_allowed(self):
        sm = _make_state()
        sm.emit(MEETING_STARTED, mode="meeting")
        ok = sm.try_transition(
            (None, "done", "error"), DICTATION_STARTED, mode="dictation"
        )
        self.assertFalse(ok)
        self.assertEqual(sm.current.mode, "meeting", "rejected transition must not mutate state")

    def test_rejected_transition_emits_no_event(self):
        sm = _make_state()
        sm.emit(MEETING_STARTED, mode="meeting")
        events = []
        sm.on_any(events.append)
        sm.try_transition((None,), DICTATION_STARTED, mode="dictation")
        self.assertEqual(events, [], "rejected transition must notify nobody")

    def test_accepted_transition_notifies_listeners(self):
        sm = _make_state()
        events = []
        sm.on_any(events.append)
        sm.try_transition((None,), DICTATION_STARTED, mode="dictation")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, DICTATION_STARTED)
        self.assertEqual(events[0].new_state.mode, "dictation")

    def test_exactly_one_winner_under_contention(self):
        # The race this API exists to close: N threads all try to claim
        # idle -> dictation simultaneously; exactly ONE may win.
        sm = _make_state()
        barrier = threading.Barrier(8)
        wins = []

        def _claim():
            barrier.wait(timeout=2.0)
            if sm.try_transition(
                (None,), DICTATION_STARTED, mode="dictation"
            ):
                wins.append(threading.current_thread().name)

        threads = [threading.Thread(target=_claim, daemon=True) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2.0)

        self.assertEqual(
            len(wins), 1,
            f"exactly one thread must win the transition, got {wins}",
        )
        self.assertEqual(sm.current.mode, "dictation")

    def test_other_state_fields_set_atomically_with_mode(self):
        sm = _make_state()
        ok = sm.try_transition(
            (None,), IDLE, mode=None, meeting_transcribing=True
        )
        self.assertTrue(ok)
        self.assertTrue(sm.current.meeting_transcribing)

    def test_emit_unchanged_for_existing_callers(self):
        # emit must behave exactly as before the refactor (shared internals).
        sm = _make_state()
        events = []
        sm.on(MEETING_STARTED, events.append)
        sm.emit(MEETING_STARTED, mode="meeting", data={"x": 1})
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].data, {"x": 1})
        self.assertEqual(events[0].old_state.mode, None)
        self.assertEqual(events[0].new_state.mode, "meeting")
        self.assertEqual(len(sm.history), 1)


if __name__ == "__main__":
    unittest.main()
