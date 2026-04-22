"""Tests for exit-reason constants in whisper_sync.lifecycle.

The lifecycle module is the grep anchor for forensic log analysis. Exit
reasons scattered as raw strings across call sites drift over time; these
tests pin the canonical values so greppers and dashboards don't break
silently.
"""

import unittest


class ExitReasonConstantsTests(unittest.TestCase):
    def test_all_known_reasons_exported(self):
        from whisper_sync import lifecycle
        expected = {
            "REASON_UNKNOWN": "unknown",
            "REASON_USER_QUIT": "user_quit",
            "REASON_USER_RESTART": "user_restart",
            "REASON_ATEXIT": "atexit",
            "REASON_SIGNAL": "signal",
            "REASON_EXCEPTION": "exception",
            "REASON_SYSTEM_EXIT": "system_exit",
        }
        for name, value in expected.items():
            self.assertTrue(
                hasattr(lifecycle, name),
                f"lifecycle.{name} is not exported",
            )
            self.assertEqual(getattr(lifecycle, name), value)

    def test_default_get_exit_reason_uses_constant(self):
        # Sanity: the default sentinel matches the exported constant
        from whisper_sync import lifecycle
        lifecycle._reset_for_tests()
        reason, _ = lifecycle.get_exit_reason()
        self.assertEqual(reason, lifecycle.REASON_UNKNOWN)


if __name__ == "__main__":
    unittest.main()
