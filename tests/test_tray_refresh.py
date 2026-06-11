"""Tests for whisper_sync.tray_refresh — debounced single-owner menu refresh."""

import threading
import time
import unittest

from whisper_sync.scheduler import Scheduler
from whisper_sync.tray_refresh import MenuRefresher


class MenuRefresherTests(unittest.TestCase):
    def setUp(self):
        self.sched = Scheduler(name="test-tray-sched")
        self.builds = []
        self.applied = []
        self.build_threads = set()
        self.apply_threads = set()

    def tearDown(self):
        self.sched.shutdown(timeout=2.0)

    def _make(self, debounce_s=0.05, build_fn=None):
        def _build():
            self.build_threads.add(threading.current_thread().name)
            menu = object()
            self.builds.append(menu)
            return menu

        def _apply(menu):
            self.apply_threads.add(threading.current_thread().name)
            self.applied.append(menu)

        return MenuRefresher(
            build_menu=build_fn or _build,
            apply_menu=_apply,
            debounce_s=debounce_s,
            sched=self.sched,
        )

    def _wait_for(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_burst_of_requests_coalesces_to_one_rebuild(self):
        r = self._make(debounce_s=0.08)
        for _ in range(10):
            r.request()
        self.assertTrue(self._wait_for(lambda: len(self.applied) == 1))
        time.sleep(0.2)  # ensure no extra rebuilds trail in
        self.assertEqual(len(self.builds), 1, "10 burst requests must build once")
        self.assertEqual(len(self.applied), 1)

    def test_request_after_completion_triggers_new_rebuild(self):
        r = self._make(debounce_s=0.03)
        r.request()
        self.assertTrue(self._wait_for(lambda: len(self.applied) == 1))
        r.request()
        self.assertTrue(self._wait_for(lambda: len(self.applied) == 2))
        self.assertEqual(len(self.builds), 2)

    def test_applied_menu_is_the_built_menu(self):
        r = self._make(debounce_s=0.02)
        r.request()
        self.assertTrue(self._wait_for(lambda: len(self.applied) == 1))
        self.assertIs(self.applied[0], self.builds[0])

    def test_build_and_apply_run_on_scheduler_thread_not_caller(self):
        r = self._make(debounce_s=0.02)
        caller = threading.current_thread().name
        r.request()
        self.assertTrue(self._wait_for(lambda: len(self.applied) == 1))
        self.assertEqual(len(self.build_threads), 1)
        self.assertEqual(self.build_threads, self.apply_threads)
        self.assertNotIn(caller, self.build_threads,
                         "rebuild must not run on the requesting thread")

    def test_build_exception_does_not_break_future_requests(self):
        calls = []

        def _flaky_build():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("kaboom")
            menu = object()
            self.builds.append(menu)
            return menu

        r = self._make(debounce_s=0.02, build_fn=_flaky_build)
        r.request()
        self.assertTrue(self._wait_for(lambda: len(calls) == 1))
        time.sleep(0.05)
        self.assertEqual(len(self.applied), 0, "failed build must not apply")
        r.request()
        self.assertTrue(self._wait_for(lambda: len(self.applied) == 1),
                        "refresher must recover after a failed build")

    def test_concurrent_requests_from_many_threads_coalesce(self):
        r = self._make(debounce_s=0.08)
        start = threading.Barrier(8)

        def _spam():
            start.wait(timeout=2.0)
            for _ in range(5):
                r.request()

        threads = [threading.Thread(target=_spam, daemon=True) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2.0)
        self.assertTrue(self._wait_for(lambda: len(self.applied) >= 1))
        time.sleep(0.2)
        # 40 requests across 8 threads within the debounce window: at most
        # 2 rebuilds can land (one if all fall in a single window; two if a
        # request arrives while the first rebuild is mid-flight).
        self.assertLessEqual(len(self.builds), 2,
                             f"expected coalescing, got {len(self.builds)} rebuilds")

    def test_rebuild_count_telemetry(self):
        r = self._make(debounce_s=0.02)
        self.assertEqual(r.rebuild_count, 0)
        r.request()
        self.assertTrue(self._wait_for(lambda: r.rebuild_count == 1))

    def test_pending_released_when_scheduler_shut_down(self):
        # Regression for Copilot review on PR #138: a shut-down scheduler
        # returns a pre-cancelled handle; the pending flag must be released
        # or every later request becomes a permanent no-op.
        r = self._make(debounce_s=0.02)
        self.sched.shutdown(timeout=2.0)
        r.request()  # handle is pre-cancelled; must not wedge the flag
        self.assertFalse(
            r._pending,
            "pending flag must be released when scheduling cannot run",
        )
        # A working scheduler must be able to serve future requests.
        from whisper_sync.scheduler import Scheduler
        self.sched = Scheduler(name="test-tray-sched-2")
        r._sched = self.sched
        r.request()
        self.assertTrue(self._wait_for(lambda: len(self.applied) == 1),
                        "refresher must recover once scheduling works again")


if __name__ == "__main__":
    unittest.main()
