"""Tests for whisper_sync.scheduler — the single timer thread.

These tests use their own Scheduler instances (not the module singleton)
so they can shut down cleanly and not leak threads between tests.
"""

import threading
import time
import unittest

from whisper_sync.scheduler import Scheduler


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.sched = Scheduler(name="test-scheduler")

    def tearDown(self):
        self.sched.shutdown(timeout=2.0)

    def test_call_later_fires_once(self):
        fired = threading.Event()
        calls = []

        def _job():
            calls.append(time.monotonic())
            fired.set()

        self.sched.call_later(0.05, _job, label="once")
        self.assertTrue(fired.wait(timeout=2.0), "job should fire")
        time.sleep(0.15)  # ensure no repeat
        self.assertEqual(len(calls), 1, "call_later must fire exactly once")

    def test_call_later_cancel_prevents_fire(self):
        calls = []
        handle = self.sched.call_later(0.1, lambda: calls.append(1), label="cancelled")
        handle.cancel()
        time.sleep(0.25)
        self.assertEqual(calls, [], "cancelled job must not fire")

    def test_call_every_repeats_until_cancelled(self):
        calls = []
        done = threading.Event()

        def _tick():
            calls.append(1)
            if len(calls) >= 3:
                done.set()

        handle = self.sched.call_every(0.03, _tick, label="periodic")
        self.assertTrue(done.wait(timeout=2.0), "periodic job should tick 3 times")
        handle.cancel()
        count_at_cancel = len(calls)
        time.sleep(0.15)
        self.assertLessEqual(
            len(calls), count_at_cancel + 1,
            "at most one in-flight tick may land after cancel",
        )

    def test_job_exception_does_not_kill_thread(self):
        ok = threading.Event()

        def _boom():
            raise RuntimeError("kaboom")

        self.sched.call_later(0.01, _boom, label="boom")
        self.sched.call_later(0.05, ok.set, label="after-boom")
        self.assertTrue(
            ok.wait(timeout=2.0),
            "scheduler must survive a raising job and run later jobs",
        )

    def test_all_jobs_share_one_thread(self):
        threads = set()
        done = threading.Event()

        def _record():
            threads.add(threading.current_thread().name)
            if len(threads) >= 1 and _record.count == 4:
                done.set()
        _record.count = 0

        def _job():
            _record.count += 1
            _record()

        for i in range(5):
            self.sched.call_later(0.01 + i * 0.01, _job, label=f"j{i}")
        time.sleep(0.5)
        self.assertEqual(
            len(threads), 1,
            f"all jobs must run on the single scheduler thread, got {threads}",
        )

    def test_ordering_earlier_deadline_first(self):
        order = []
        done = threading.Event()

        def _make(tag):
            def _j():
                order.append(tag)
                if len(order) == 2:
                    done.set()
            return _j

        # Schedule the LATER one first to prove heap ordering.
        self.sched.call_later(0.10, _make("late"), label="late")
        self.sched.call_later(0.02, _make("early"), label="early")
        self.assertTrue(done.wait(timeout=2.0))
        self.assertEqual(order, ["early", "late"])

    def test_schedule_after_shutdown_returns_cancelled_handle(self):
        self.sched.shutdown()
        calls = []
        handle = self.sched.call_later(0.01, lambda: calls.append(1), label="dead")
        self.assertTrue(handle.cancelled)
        time.sleep(0.1)
        self.assertEqual(calls, [])

    def test_shutdown_drops_pending_jobs(self):
        calls = []
        self.sched.call_later(0.5, lambda: calls.append(1), label="pending")
        self.sched.shutdown(timeout=2.0)
        time.sleep(0.1)
        self.assertEqual(calls, [], "pending jobs must be dropped on shutdown")


if __name__ == "__main__":
    unittest.main()
