"""Tests for whisper_sync.power_events - suspend/resume dispatch.

Dispatch logic is driven directly through handle_power_event (no OS
registration needed); the registration itself gets a real
register/unregister roundtrip on Windows.
"""

import os
import unittest

from whisper_sync.power_events import (
    PowerEventListener, PBT_APMSUSPEND, PBT_APMRESUMEAUTOMATIC,
)

PBT_APMRESUMESUSPEND = 0x0007


class DispatchTests(unittest.TestCase):
    def test_suspend_dispatches_on_suspend(self):
        calls = []
        l = PowerEventListener(on_suspend=lambda: calls.append("s"),
                               on_resume=lambda: calls.append("r"))
        l.handle_power_event(PBT_APMSUSPEND)
        self.assertEqual(calls, ["s"])

    def test_resume_automatic_dispatches_on_resume(self):
        calls = []
        l = PowerEventListener(on_resume=lambda: calls.append("r"))
        l.handle_power_event(PBT_APMRESUMEAUTOMATIC)
        self.assertEqual(calls, ["r"])

    def test_resume_suspend_variant_not_double_dispatched(self):
        # Windows fires RESUMEAUTOMATIC for every wake and RESUMESUSPEND
        # additionally for user-present wake; only the former may
        # dispatch or a user wake would run on_resume twice.
        calls = []
        l = PowerEventListener(on_resume=lambda: calls.append("r"))
        l.handle_power_event(PBT_APMRESUMEAUTOMATIC)
        l.handle_power_event(PBT_APMRESUMESUSPEND)
        self.assertEqual(calls, ["r"])

    def test_unknown_event_ignored_and_callback_exceptions_swallowed(self):
        def _boom():
            raise RuntimeError("boom")
        l = PowerEventListener(on_suspend=_boom)
        l.handle_power_event(0x9999)   # ignored
        l.handle_power_event(PBT_APMSUSPEND)  # must not raise

    def test_no_callbacks_is_fine(self):
        PowerEventListener().handle_power_event(PBT_APMSUSPEND)


class RegistrationTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows-only registration")
    def test_register_unregister_roundtrip(self):
        l = PowerEventListener(on_suspend=lambda: None)
        self.assertTrue(l.start(), "registration must succeed on Windows")
        self.assertTrue(l.start(), "second start is an idempotent True")
        l.stop()
        l.stop()  # idempotent

    def test_non_windows_start_is_noop_false(self):
        from unittest import mock
        with mock.patch.object(os, "name", "posix"):
            self.assertFalse(PowerEventListener().start())


if __name__ == "__main__":
    unittest.main()
