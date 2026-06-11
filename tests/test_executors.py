"""Tests for whisper_sync.executors — named serial executors + native gauge."""

import threading
import time
import unittest

from whisper_sync.executors import Executor, native_call, native_calls_in_flight


class NativeCallContextTests(unittest.TestCase):
    def test_native_call_marks_gauge_on_any_thread(self):
        baseline = native_calls_in_flight()
        with native_call("test-span"):
            self.assertEqual(native_calls_in_flight(), baseline + 1)
        self.assertEqual(native_calls_in_flight(), baseline)

    def test_native_call_releases_gauge_on_exception(self):
        baseline = native_calls_in_flight()
        with self.assertRaises(RuntimeError):
            with native_call("test-span"):
                raise RuntimeError("kaboom")
        self.assertEqual(
            native_calls_in_flight(), baseline,
            "gauge must be released even when the body raises",
        )

    def test_nested_native_calls_count_independently(self):
        baseline = native_calls_in_flight()
        with native_call("outer"):
            with native_call("inner"):
                self.assertEqual(native_calls_in_flight(), baseline + 2)
            self.assertEqual(native_calls_in_flight(), baseline + 1)
        self.assertEqual(native_calls_in_flight(), baseline)


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.ex = Executor("test", queue_max=8)

    def tearDown(self):
        self.ex.shutdown(timeout=2.0)

    def test_jobs_run_serially_in_order(self):
        order = []
        done = threading.Event()

        def _make(tag):
            def _j():
                order.append(tag)
                if len(order) == 3:
                    done.set()
            return _j

        self.ex.submit("a", _make("a"))
        self.ex.submit("b", _make("b"))
        self.ex.submit("c", _make("c"))
        self.assertTrue(done.wait(timeout=2.0))
        self.assertEqual(order, ["a", "b", "c"])

    def test_jobs_share_one_thread(self):
        threads = set()
        done = threading.Event()

        def _j():
            threads.add(threading.current_thread().name)
            if len(threads) >= 1 and _j.count == 2:
                done.set()
        _j.count = 0

        def _job():
            _j.count += 1
            _j()

        self.ex.submit("x", _job)
        self.ex.submit("y", _job)
        self.ex.submit("z", _job)
        time.sleep(0.3)
        self.assertEqual(len(threads), 1, f"expected one executor thread, got {threads}")

    def test_exception_does_not_kill_executor(self):
        ok = threading.Event()
        self.ex.submit("boom", lambda: (_ for _ in ()).throw(RuntimeError("kaboom")))
        self.ex.submit("after", ok.set)
        self.assertTrue(ok.wait(timeout=2.0), "executor must survive raising jobs")

    def test_native_gauge_tracks_submit_native(self):
        gate = threading.Event()
        entered = threading.Event()

        def _native_job():
            entered.set()
            gate.wait(timeout=5.0)

        baseline = native_calls_in_flight()
        self.ex.submit_native("native", _native_job)
        self.assertTrue(entered.wait(timeout=2.0))
        self.assertEqual(
            native_calls_in_flight(), baseline + 1,
            "gauge must count the in-flight native job",
        )
        gate.set()
        # Wait for the job to finish and the gauge to drop
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and native_calls_in_flight() != baseline:
            time.sleep(0.01)
        self.assertEqual(native_calls_in_flight(), baseline)

    def test_plain_submit_does_not_touch_native_gauge(self):
        entered = threading.Event()
        gate = threading.Event()

        def _job():
            entered.set()
            gate.wait(timeout=5.0)

        baseline = native_calls_in_flight()
        self.ex.submit("plain", _job)
        self.assertTrue(entered.wait(timeout=2.0))
        self.assertEqual(native_calls_in_flight(), baseline)
        gate.set()

    def test_idle_reflects_running_and_queued_jobs(self):
        gate = threading.Event()
        entered = threading.Event()

        def _job():
            entered.set()
            gate.wait(timeout=5.0)

        self.assertTrue(self.ex.idle(), "fresh executor should be idle")
        self.ex.submit("blocker", _job)
        self.assertTrue(entered.wait(timeout=2.0))
        self.assertFalse(self.ex.idle(), "executor with running job is not idle")
        gate.set()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not self.ex.idle():
            time.sleep(0.01)
        self.assertTrue(self.ex.idle(), "executor should return to idle")

    def test_queue_full_rejects_without_blocking(self):
        gate = threading.Event()
        self.ex.submit("blocker", lambda: gate.wait(timeout=5.0))
        # Fill the queue (max 8)
        accepted = sum(
            1 for i in range(20)
            if self.ex.submit(f"fill-{i}", lambda: None)
        )
        self.assertLessEqual(accepted, 8, "submits beyond queue_max must be rejected")
        gate.set()

    def test_submit_after_shutdown_rejected(self):
        self.ex.shutdown(timeout=2.0)
        self.assertFalse(self.ex.submit("late", lambda: None))


if __name__ == "__main__":
    unittest.main()
