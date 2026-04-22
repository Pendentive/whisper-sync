"""Tests for crash_diagnostics.install_faulthandler helper.

Extracted from __main__.py so worker.py can use the same fix. The key
property is that the opened file object is retained at module scope so
it cannot be garbage-collected (which was the historic silent-death bug
that lost native crash dumps).
"""

import tempfile
import unittest
from pathlib import Path


class InstallFaulthandlerTests(unittest.TestCase):
    def test_returns_a_file_object_that_is_retained(self):
        from whisper_sync import crash_diagnostics
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "fh.log"
            f = crash_diagnostics.install_faulthandler(log_path)
            try:
                self.assertIsNotNone(f, "should return the opened file")
                self.assertFalse(f.closed, "file must be open")
                # The module should keep a strong ref so GC can't close it.
                self.assertIs(crash_diagnostics._FAULTHANDLER_FILE, f)
            finally:
                crash_diagnostics._reset_faulthandler_for_tests()

    def test_returns_none_on_bad_path_and_falls_back(self):
        from whisper_sync import crash_diagnostics
        # A subpath of a temp directory whose intermediate segment does not
        # exist guarantees open("a") fails regardless of platform or prior
        # filesystem state.
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "does-not-exist" / "fh.log"
            result = crash_diagnostics.install_faulthandler(bad)
            try:
                self.assertIsNone(result)
            finally:
                crash_diagnostics._reset_faulthandler_for_tests()

    def test_second_call_closes_previous_file_no_fd_leak(self):
        # Regression for PR #127 review #4: calling install_faulthandler
        # twice must not leak the previously retained file descriptor.
        from whisper_sync import crash_diagnostics
        with tempfile.TemporaryDirectory() as tmp:
            first = crash_diagnostics.install_faulthandler(Path(tmp) / "first.log")
            try:
                self.assertIsNotNone(first)
                self.assertFalse(first.closed)
                second = crash_diagnostics.install_faulthandler(Path(tmp) / "second.log")
                self.assertIsNotNone(second)
                # The first handle must now be closed by the helper.
                self.assertTrue(
                    first.closed,
                    "previous faulthandler file must be closed on re-install",
                )
                # And the module should only retain the newest one.
                self.assertIs(crash_diagnostics._FAULTHANDLER_FILE, second)
            finally:
                crash_diagnostics._reset_faulthandler_for_tests()


if __name__ == "__main__":
    unittest.main()
