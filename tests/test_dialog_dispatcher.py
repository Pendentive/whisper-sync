"""Unit tests for whisper_sync.dialog_dispatcher.

These tests do not touch tkinter. They just verify the threading and
return/exception contract that dialog code relies on.
"""

from __future__ import annotations

import threading
import time
import unittest

from whisper_sync.dialog_dispatcher import DialogDispatcher


class DialogDispatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.d = DialogDispatcher(name="test-dispatcher")
        self.d.start()

    def tearDown(self) -> None:
        self.d.shutdown(timeout=2.0)

    def test_run_returns_callable_result(self) -> None:
        result = self.d.run(lambda: 42, label="answer")
        self.assertEqual(result, 42)

    def test_run_propagates_exceptions(self) -> None:
        class Boom(RuntimeError):
            pass

        def fn():
            raise Boom("kaboom")

        with self.assertRaises(Boom):
            self.d.run(fn, label="boom")

    def test_callable_runs_on_dispatcher_thread(self) -> None:
        thread_id_holder: dict[str, int] = {}

        def fn():
            thread_id_holder["tid"] = threading.get_ident()
            return None

        # Two calls back to back must hit the SAME thread. This is the
        # property tkinter on Windows depends on.
        self.d.run(fn, label="first")
        first = thread_id_holder["tid"]
        thread_id_holder.clear()
        self.d.run(fn, label="second")
        second = thread_id_holder["tid"]

        self.assertEqual(first, second)
        self.assertNotEqual(first, threading.get_ident())

    def test_calls_are_serialized(self) -> None:
        # If the dispatcher accidentally ran callables in parallel, the
        # sleeps below would overlap and total elapsed time would be ~0.1s.
        # Serialized execution makes it ~0.3s.
        per_call = 0.1
        n = 3

        def fn():
            time.sleep(per_call)
            return time.monotonic()

        start = time.monotonic()
        results = []
        # Submit from multiple worker threads. They should still run one at
        # a time on the dispatcher.
        threads = []
        bucket: list = []
        lock = threading.Lock()

        def submit():
            r = self.d.run(fn, label="parallel")
            with lock:
                bucket.append(r)

        for _ in range(n):
            t = threading.Thread(target=submit)
            threads.append(t)
            t.start()
        for t in threads:
            t.join()

        elapsed = time.monotonic() - start
        self.assertEqual(len(bucket), n)
        self.assertGreaterEqual(elapsed, per_call * n * 0.8)

        # Returned timestamps must be strictly monotonically increasing
        # (within tolerance) since the calls were serial.
        sorted_bucket = sorted(bucket)
        for a, b in zip(sorted_bucket, sorted_bucket[1:]):
            self.assertGreaterEqual(b - a, per_call * 0.8)

    def test_lazy_start_when_run_called_first(self) -> None:
        d = DialogDispatcher(name="lazy")
        # Don't call .start(); .run() should auto-start.
        try:
            self.assertEqual(d.run(lambda: "ok"), "ok")
        finally:
            d.shutdown(timeout=2.0)

    def test_shutdown_is_idempotent(self) -> None:
        d = DialogDispatcher(name="shutdown")
        d.start()
        d.shutdown(timeout=2.0)
        # Second shutdown must not raise or hang.
        d.shutdown(timeout=2.0)

    def test_reentrant_call_runs_inline(self) -> None:
        # If a dialog callback wants to invoke another dialog, that nested
        # call must NOT be re-queued (it would deadlock waiting on itself).
        # The dispatcher should detect same-thread calls and run inline.
        outer_thread: dict[str, int] = {}
        inner_thread: dict[str, int] = {}

        def inner():
            inner_thread["tid"] = threading.get_ident()
            return "inner-result"

        def outer():
            outer_thread["tid"] = threading.get_ident()
            return self.d.run(inner, label="inner")

        result = self.d.run(outer, label="outer")
        self.assertEqual(result, "inner-result")
        self.assertEqual(outer_thread["tid"], inner_thread["tid"])


if __name__ == "__main__":
    unittest.main()
