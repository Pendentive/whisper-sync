"""Tests for whisper_sync.notifications.

Focused on the toast registry. ``meeting_completed`` toasts are fired
directly from ``MeetingJob.step_notify`` (which uses an explicit body
and attaches an Open Folder button). The state-driven
``ToastListener`` previously also had a template entry, which fired on
the ``MEETING_COMPLETED`` state event without the ``{words}``/``{speakers}``
keys and produced ``KeyError`` noise at DEBUG. The registry entry is
the source of that noise, so it is removed.
"""

import unittest


class ToastRegistryTests(unittest.TestCase):
    def test_meeting_completed_not_in_registry(self):
        from whisper_sync.notifications import TOAST_REGISTRY
        self.assertNotIn(
            "meeting_completed",
            TOAST_REGISTRY,
            "meeting_completed toast is owned by MeetingJob.step_notify; "
            "the template path was misfiring and must be absent.",
        )

    def test_error_template_still_present(self):
        # Guardrail: ensure the trim didn't accidentally drop other entries.
        from whisper_sync.notifications import TOAST_REGISTRY
        self.assertIn("error", TOAST_REGISTRY)
        self.assertIn("dictation_completed", TOAST_REGISTRY)


if __name__ == "__main__":
    unittest.main()
