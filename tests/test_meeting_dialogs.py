"""Tests for whisper_sync.meeting_dialogs (extraction 2a of hardening item 6).

The Tk layouts themselves need a display and a user; what is pinned here
is everything around them: name sanitization, the ABORT sentinel
contract, and the abort-on-crash behavior when the dialog dispatcher
fails (callers must never hang or see an exception where the old
in-class methods returned ABORT/None).
"""

import unittest

from whisper_sync.meeting_dialogs import (
    ABORT, MeetingDialogs, sanitize_name,
)


class _CrashingDispatcher:
    def run(self, fn, label=None, wants_root=False):
        raise RuntimeError("tk exploded")


class _FakeApp:
    def __init__(self):
        self.cfg = {}
        self._dialog_dispatcher = _CrashingDispatcher()


class SanitizeNameTests(unittest.TestCase):
    def test_spaces_become_hyphens(self):
        self.assertEqual(sanitize_name("weekly sync notes"), "weekly-sync-notes")

    def test_special_chars_removed(self):
        self.assertEqual(sanitize_name("q3: plan / review!"), "q3-plan--review")

    def test_allowed_chars_kept(self):
        self.assertEqual(sanitize_name("a-b_c 1"), "a-b_c-1")

    def test_empty_stays_empty(self):
        self.assertEqual(sanitize_name(""), "")


class DispatcherCrashTests(unittest.TestCase):
    """Every dialog must swallow a dispatcher crash and return its
    documented abort value - a Tk failure must never abort a meeting
    save or a recovery loop."""

    def setUp(self):
        self.dialogs = MeetingDialogs(_FakeApp())

    def test_ask_meeting_name_returns_abort(self):
        self.assertIs(self.dialogs.ask_meeting_name(), ABORT)

    def test_ask_recovery_name_returns_abort(self):
        self.assertIs(self.dialogs.ask_recovery_name("x.wav", "1m 2s"), ABORT)

    def test_show_llm_unavailable_returns_false(self):
        self.assertFalse(self.dialogs.show_llm_unavailable())

    def test_ask_speaker_confirmation_returns_none(self):
        result = self.dialogs.ask_speaker_confirmation(
            {"speaker_map": {"SPEAKER_00": "Alice"}})
        self.assertIsNone(result)


class AbortSentinelTests(unittest.TestCase):
    def test_abort_is_not_none_or_falsy_string(self):
        # Callers distinguish "user aborted" from "empty name"; the
        # sentinel must never compare equal to either.
        self.assertIsNot(ABORT, None)
        self.assertNotEqual(ABORT, "")


if __name__ == "__main__":
    unittest.main()
