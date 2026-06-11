"""Tests for whisper_sync.idle_gc — provable-idle cycle collection."""

import threading
import time
import unittest
from unittest import mock

from whisper_sync.idle_gc import IdleCollector
from whisper_sync import executors


def _make_collector(pipeline_idle=True, recording=False, mode_terminal=True):
    return IdleCollector(
        is_pipeline_idle=lambda: pipeline_idle,
        is_recording=lambda: recording,
        is_mode_terminal=lambda: mode_terminal,
        interval_s=999,  # never auto-ticks in tests; we call _tick directly
    )


class IdleCollectorTests(unittest.TestCase):
    def test_collects_when_fully_quiescent(self):
        c = _make_collector()
        with mock.patch("whisper_sync.idle_gc.gc.collect", return_value=42) as m:
            c._tick()
        m.assert_called_once()
        self.assertIsNone(c.last_skip_reason)
        self.assertEqual(c.total_collected, 42)

    def test_skips_when_pipeline_busy(self):
        c = _make_collector(pipeline_idle=False)
        with mock.patch("whisper_sync.idle_gc.gc.collect") as m:
            c._tick()
        m.assert_not_called()
        self.assertEqual(c.last_skip_reason, "pipeline busy")

    def test_skips_when_recording(self):
        c = _make_collector(recording=True)
        with mock.patch("whisper_sync.idle_gc.gc.collect") as m:
            c._tick()
        m.assert_not_called()
        self.assertEqual(c.last_skip_reason, "recording")

    def test_skips_when_mode_not_terminal(self):
        c = _make_collector(mode_terminal=False)
        with mock.patch("whisper_sync.idle_gc.gc.collect") as m:
            c._tick()
        m.assert_not_called()
        self.assertEqual(c.last_skip_reason, "transcription in flight")

    def test_skips_when_native_call_in_flight(self):
        # Occupy the IO executor with a native job, then tick.
        gate = threading.Event()
        entered = threading.Event()

        def _native():
            entered.set()
            gate.wait(timeout=5.0)

        ex = executors.Executor("idle-gc-test")
        try:
            ex.submit_native("hold", _native)
            self.assertTrue(entered.wait(timeout=2.0))
            c = _make_collector()
            with mock.patch("whisper_sync.idle_gc.gc.collect") as m:
                c._tick()
            m.assert_not_called()
            self.assertEqual(c.last_skip_reason, "executors busy")
        finally:
            gate.set()
            ex.shutdown(timeout=2.0)
        # Gauge must drain so later tests see a quiescent world.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and executors.native_calls_in_flight():
            time.sleep(0.01)
        self.assertEqual(executors.native_calls_in_flight(), 0)

    def test_skips_when_named_executor_busy(self):
        gate = threading.Event()
        entered = threading.Event()
        # DICTATION is one of the gauged process-wide executors.
        executors.DICTATION.submit("hold", lambda: (entered.set(), gate.wait(timeout=5.0)))
        try:
            self.assertTrue(entered.wait(timeout=2.0))
            c = _make_collector()
            with mock.patch("whisper_sync.idle_gc.gc.collect") as m:
                c._tick()
            m.assert_not_called()
            self.assertEqual(c.last_skip_reason, "executors busy")
        finally:
            gate.set()
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not executors.DICTATION.idle():
                time.sleep(0.01)

    def test_start_stop_idempotent(self):
        c = _make_collector()
        c.start()
        first_handle = c._handle
        c.start()  # second start is a no-op
        self.assertIs(c._handle, first_handle)
        c.stop()
        self.assertIsNone(c._handle)
        c.stop()  # second stop is a no-op


if __name__ == "__main__":
    unittest.main()
