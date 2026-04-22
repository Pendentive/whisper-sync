"""Tests for notifications.is_toast_enabled helper.

Extracted so both the ToastListener and MeetingJob.step_notify can share
the same config-gate logic rather than each implementing its own
isinstance-check + membership-check pair.
"""

import unittest


class IsToastEnabledTests(unittest.TestCase):
    def test_event_in_config_list(self):
        from whisper_sync.notifications import is_toast_enabled
        self.assertTrue(
            is_toast_enabled("meeting_completed",
                             {"toast_events": ["meeting_completed", "error"]})
        )

    def test_event_absent_from_config(self):
        from whisper_sync.notifications import is_toast_enabled
        self.assertFalse(
            is_toast_enabled("meeting_completed",
                             {"toast_events": ["error"]})
        )

    def test_missing_config_falls_back_to_defaults(self):
        from whisper_sync.notifications import is_toast_enabled, DEFAULT_TOAST_EVENTS
        # Empty cfg => defaults apply; meeting_completed is in defaults
        self.assertIn("meeting_completed", DEFAULT_TOAST_EVENTS)
        self.assertTrue(is_toast_enabled("meeting_completed", {}))

    def test_garbage_config_value_falls_back_to_defaults(self):
        from whisper_sync.notifications import is_toast_enabled
        # Non-iterable junk should not crash; defaults apply instead
        self.assertTrue(
            is_toast_enabled("meeting_completed",
                             {"toast_events": "nonsense"})
        )
        self.assertTrue(
            is_toast_enabled("meeting_completed",
                             {"toast_events": None})
        )

    def test_none_cfg_tolerated(self):
        from whisper_sync.notifications import is_toast_enabled
        # step_notify may pass None/empty cfg defensively
        self.assertTrue(is_toast_enabled("meeting_completed", None))


if __name__ == "__main__":
    unittest.main()
