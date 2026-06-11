"""Tests for the Phase 6 worker response protocol (single reader thread).

Drives TranscriptionWorker's reader/request plumbing against a FAKE
process and plain queues - no subprocess, no numpy. The contract:
responses route to their waiter by request_id; stale responses are
dropped; worker death fails every pending request; a timeout kills the
wedged worker and raises WorkerCrashedError.
"""

import queue
import threading
import time
import unittest

from whisper_sync.worker_manager import TranscriptionWorker, WorkerCrashedError


class _FakeProcess:
    def __init__(self):
        self.alive = True
        self.killed = False
        self.exitcode = None
        self.pid = 12345

    def is_alive(self):
        return self.alive

    def kill(self):
        self.killed = True
        self.alive = False
        self.exitcode = -9


def _make_worker():
    """Worker wired to fake process + plain queues, reader running."""
    w = TranscriptionWorker(cfg={})
    w._process = _FakeProcess()
    w._request_q = queue.Queue()
    w._response_q = queue.Queue()
    w._reader = threading.Thread(
        target=w._reader_loop, args=(w._process, w._response_q), daemon=True
    )
    w._reader.start()
    return w


class WorkerProtocolTests(unittest.TestCase):
    def tearDown(self):
        # Let reader threads exit by killing their fake process.
        if hasattr(self, "w") and self.w._process is not None:
            self.w._process.alive = False

    def test_response_routed_to_matching_request(self):
        self.w = w = _make_worker()
        results = {}

        def _call(tag):
            results[tag] = w._request({"type": "transcribe_fast"}, timeout=5.0)

        t1 = threading.Thread(target=_call, args=("a",), daemon=True)
        t1.start()
        # Wait until the request is registered, then answer it.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not w._pending:
            time.sleep(0.01)
        rid = w._request_q.get(timeout=1.0)["request_id"]
        w._response_q.put({"type": "result", "text": "hello", "request_id": rid})
        t1.join(timeout=2.0)
        self.assertEqual(results["a"]["text"], "hello")

    def test_out_of_order_responses_route_correctly(self):
        # Two concurrent requests; responses arrive in REVERSE order. Each
        # waiter must receive its own response (the old shared-get design
        # let one consumer swallow the other's message).
        self.w = w = _make_worker()
        results = {}

        def _call(tag):
            results[tag] = w._request({"type": "transcribe_fast"}, timeout=5.0)

        threads = [
            threading.Thread(target=_call, args=(tag,), daemon=True)
            for tag in ("first", "second")
        ]
        for t in threads:
            t.start()
        # Collect both request ids in send order.
        rid1 = w._request_q.get(timeout=2.0)["request_id"]
        rid2 = w._request_q.get(timeout=2.0)["request_id"]
        # Answer in reverse order with distinguishable payloads.
        w._response_q.put({"type": "result", "text": f"resp-{rid2}", "request_id": rid2})
        w._response_q.put({"type": "result", "text": f"resp-{rid1}", "request_id": rid1})
        for t in threads:
            t.join(timeout=2.0)
        # Each result's text embeds the rid it was answered with; both
        # callers got SOME response and the two responses are distinct.
        texts = sorted(r["text"] for r in results.values())
        self.assertEqual(texts, sorted([f"resp-{rid1}", f"resp-{rid2}"]))

    def test_stale_response_dropped_without_breaking_reader(self):
        self.w = w = _make_worker()
        w._response_q.put({"type": "result", "text": "ghost", "request_id": 99999})
        # Reader must survive; a fresh request still works.
        results = {}
        t = threading.Thread(
            target=lambda: results.update(
                ok=w._request({"type": "transcribe_fast"}, timeout=5.0)
            ),
            daemon=True,
        )
        t.start()
        rid = w._request_q.get(timeout=2.0)["request_id"]
        w._response_q.put({"type": "result", "text": "real", "request_id": rid})
        t.join(timeout=2.0)
        self.assertEqual(results["ok"]["text"], "real")

    def test_ready_message_sets_state_and_wait_ready_passes(self):
        self.w = w = _make_worker()
        w._response_q.put({"type": "ready", "gpu_name": "FakeGPU", "device": "cuda"})
        self.assertTrue(w.wait_ready(timeout=2.0))
        self.assertEqual(w.gpu_name, "FakeGPU")
        self.assertEqual(w.device, "cuda")
        self.assertTrue(w.is_ready())

    def test_startup_error_makes_wait_ready_false(self):
        self.w = w = _make_worker()
        w._response_q.put({
            "type": "error", "message": "model preload failed",
            "request_id": "__init__",
        })
        self.assertFalse(w.wait_ready(timeout=2.0))

    def test_worker_death_fails_all_pending_requests(self):
        self.w = w = _make_worker()
        errors = []

        def _call():
            try:
                w._request({"type": "transcribe_fast"}, timeout=10.0)
            except WorkerCrashedError as e:
                errors.append(e)

        threads = [threading.Thread(target=_call, daemon=True) for _ in range(3)]
        for t in threads:
            t.start()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and len(w._pending) < 3:
            time.sleep(0.01)
        self.assertEqual(len(w._pending), 3)

        w._process.alive = False  # worker dies with requests in flight
        for t in threads:
            t.join(timeout=3.0)
        self.assertEqual(
            len(errors), 3,
            "every pending caller must get WorkerCrashedError on death",
        )
        self.assertEqual(w._pending, {}, "pending map must be drained")

    def test_timeout_kills_wedged_worker_and_raises(self):
        # Worker is alive but never answers (wedged). The request must
        # expire, kill the process, and raise - never hang forever.
        self.w = w = _make_worker()
        with self.assertRaises(WorkerCrashedError):
            w._request({"type": "transcribe_fast"}, timeout=0.2)
        self.assertTrue(w._process.killed, "wedged worker must be killed on timeout")
        self.assertEqual(w._pending, {}, "timed-out request must be unregistered")


if __name__ == "__main__":
    unittest.main()
