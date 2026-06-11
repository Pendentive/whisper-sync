"""Tests for the Phase 1b ephemeral-thread migration.

Covers the two new mechanisms: ``submit_or_spawn`` (executor with
one-shot-thread fallback so work is never dropped) and the
scheduler-driven IconAnimator (animations no longer burn a fresh
sleeping thread per state change).
"""

import threading
import time
import unittest

from whisper_sync.executors import Executor, submit_or_spawn
from whisper_sync.icons import IconAnimator


class SubmitOrSpawnTests(unittest.TestCase):
    def test_runs_on_executor_when_accepted(self):
        ex = Executor("t-accept")
        done = threading.Event()
        names = []

        def _job():
            names.append(threading.current_thread().name)
            done.set()

        submit_or_spawn(ex, "job", _job)
        self.assertTrue(done.wait(timeout=2.0))
        self.assertEqual(names[0], "ws-exec-t-accept")
        ex.shutdown()

    def test_falls_back_to_thread_when_queue_full(self):
        # Saturate a tiny executor: one job running, one queued. The next
        # submit is rejected and must still run (on a fallback thread).
        ex = Executor("t-full", queue_max=1)
        release = threading.Event()
        fallback_done = threading.Event()
        names = []

        ex.submit("blocker", lambda: release.wait(timeout=5.0))
        time.sleep(0.05)  # let the blocker start so the queue slot frees
        ex.submit("queued", lambda: None)

        def _job():
            names.append(threading.current_thread().name)
            fallback_done.set()

        submit_or_spawn(ex, "overflow-job", _job)
        self.assertTrue(
            fallback_done.wait(timeout=2.0),
            "rejected work must run on a fallback thread, not be dropped",
        )
        self.assertNotEqual(names[0], "ws-exec-t-full")
        self.assertEqual(names[0], "overflow-job")
        release.set()
        ex.shutdown()

    def test_fallback_honors_native_gauge(self):
        # Regression for Copilot review on PR #150: a native job that
        # falls back to a thread must still register in the idle-GC
        # gauge, or collection could race the subprocess.
        from whisper_sync.executors import native_calls_in_flight
        ex = Executor("t-native-fb")
        ex.submit("warmup", lambda: None)
        ex.shutdown()  # force the fallback path

        release = threading.Event()
        observed = []
        started = threading.Event()

        def _job():
            observed.append(native_calls_in_flight())
            started.set()
            release.wait(timeout=5.0)

        submit_or_spawn(ex, "native-fb", _job, native=True)
        self.assertTrue(started.wait(timeout=2.0))
        self.assertGreaterEqual(
            observed[0], 1,
            "fallback native job must be counted in native_calls_in_flight",
        )
        release.set()

    def test_fallback_logs_exceptions_instead_of_dying_silently(self):
        ex = Executor("t-exc-fb")
        ex.submit("warmup", lambda: None)
        ex.shutdown()
        ran = threading.Event()

        def _boom():
            ran.set()
            raise RuntimeError("fallback exception")

        # Must not raise out of submit_or_spawn and must execute the job.
        submit_or_spawn(ex, "boom", _boom)
        self.assertTrue(ran.wait(timeout=2.0))

    def test_falls_back_after_shutdown(self):
        ex = Executor("t-shut")
        ex.submit("warmup", lambda: None)
        ex.shutdown()
        done = threading.Event()
        submit_or_spawn(ex, "post-shutdown", done.set)
        self.assertTrue(done.wait(timeout=2.0))


class _FakeTray:
    def __init__(self):
        self.icon = "ORIGINAL"
        self.title = "t"
        self.sets = []  # (icon, title) observations after each assignment

    def __setattr__(self, name, value):
        object.__setattr__(self, name, value)
        if name in ("icon", "title") and hasattr(self, "sets"):
            self.sets.append((self.icon, self.title))


class IconAnimatorSchedulerTests(unittest.TestCase):
    def _wait_for(self, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

    def test_flash_alternates_and_ends_on_original(self):
        tray = _FakeTray()
        anim = IconAnimator(tray)
        anim.flash(count=2, interval_ms=5)
        # 4 frames: flash, original, flash, original.
        self.assertTrue(
            self._wait_for(lambda: len(tray.sets) >= 4),
            f"expected 4 frames, saw {len(tray.sets)}",
        )
        self.assertEqual(tray.icon, "ORIGINAL", "animation must restore the original icon")
        icons = [icon for icon, _ in tray.sets[:4]]
        self.assertEqual(icons[1], "ORIGINAL")
        self.assertNotEqual(icons[0], "ORIGINAL")

    def test_cancel_stops_chain(self):
        tray = _FakeTray()
        anim = IconAnimator(tray)
        anim.flash(count=50, interval_ms=20)
        self.assertTrue(self._wait_for(lambda: len(tray.sets) >= 1))
        anim.cancel()
        time.sleep(0.1)
        frames_at_cancel = len(tray.sets)
        time.sleep(0.2)
        self.assertLessEqual(
            len(tray.sets), frames_at_cancel + 1,
            "cancel must stop the frame chain at the next step",
        )
        self.assertLess(len(tray.sets), 100, "chain must not run to completion")

    def test_no_thread_spawned_per_animation(self):
        tray = _FakeTray()
        anim = IconAnimator(tray)
        before = {t.name for t in threading.enumerate()}
        anim.flash(count=1, interval_ms=1)
        self.assertTrue(self._wait_for(lambda: len(tray.sets) >= 2))
        after = {t.name for t in threading.enumerate()}
        new = {n for n in after - before if n != "ws-scheduler"}
        self.assertEqual(new, set(), f"animation must not spawn threads, got {new}")


if __name__ == "__main__":
    unittest.main()
