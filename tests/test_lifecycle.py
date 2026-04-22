"""Tests for whisper_sync.lifecycle."""

import io
import logging
import unittest


class RecordExitReasonTests(unittest.TestCase):
    def setUp(self):
        from whisper_sync import lifecycle
        lifecycle._reset_for_tests()

    def test_default_reason_is_unknown(self):
        from whisper_sync import lifecycle
        reason, extra = lifecycle.get_exit_reason()
        self.assertEqual(reason, "unknown")
        self.assertEqual(extra, {})

    def test_recording_reason_sets_state(self):
        from whisper_sync import lifecycle
        lifecycle.record_exit_reason("user_quit", {"menu": "tray"})
        reason, extra = lifecycle.get_exit_reason()
        self.assertEqual(reason, "user_quit")
        self.assertEqual(extra, {"menu": "tray"})

    def test_first_reason_wins(self):
        # Once set, later calls should not overwrite the first recorded reason
        # (prevents late-firing atexit callbacks from masking the real cause).
        from whisper_sync import lifecycle
        lifecycle.record_exit_reason("crash_worker", {"step": "transcribe"})
        lifecycle.record_exit_reason("atexit")
        reason, extra = lifecycle.get_exit_reason()
        self.assertEqual(reason, "crash_worker")
        self.assertEqual(extra, {"step": "transcribe"})


class LogExitBannerTests(unittest.TestCase):
    def setUp(self):
        from whisper_sync import lifecycle
        lifecycle._reset_for_tests()
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        self.logger = logging.getLogger("test_lifecycle_exit")
        self.logger.handlers.clear()
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.DEBUG)

    def test_exit_banner_uses_recorded_reason(self):
        from whisper_sync import lifecycle
        lifecycle.record_exit_reason("user_quit", {"menu": "tray"})
        lifecycle.log_exit_banner(self.logger)
        output = self.stream.getvalue()
        self.assertIn("WhisperSync exiting", output)
        self.assertIn("reason=user_quit", output)

    def test_exit_banner_default_reason(self):
        from whisper_sync import lifecycle
        lifecycle.log_exit_banner(self.logger)
        output = self.stream.getvalue()
        self.assertIn("reason=unknown", output)


class StartupBannerTests(unittest.TestCase):
    def setUp(self):
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        self.logger = logging.getLogger("test_lifecycle_startup")
        self.logger.handlers.clear()
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.DEBUG)

    def test_startup_banner_includes_pid_and_python(self):
        from whisper_sync import lifecycle
        lifecycle.log_startup_banner(self.logger)
        output = self.stream.getvalue()
        self.assertIn("pid=", output)
        self.assertIn("python=", output)

    def test_startup_banner_includes_git_sha_when_available(self):
        # Banner should at least attempt a git SHA; missing is OK (returns 'unknown')
        from whisper_sync import lifecycle
        lifecycle.log_startup_banner(self.logger)
        output = self.stream.getvalue()
        self.assertIn("git=", output)


if __name__ == "__main__":
    unittest.main()
