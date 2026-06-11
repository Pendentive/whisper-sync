"""Tests for whisper_sync.session_stats — lock-guarded session counters.

The previous bare-dict stats raced: `d[k] += v` is read-modify-write and
was executed concurrently from the dictation worker, the overlay worker,
and the meeting pipeline.
"""

import threading
import unittest

from whisper_sync.session_stats import SessionStats


class SessionStatsTests(unittest.TestCase):
    def test_record_dictation_accumulates(self):
        s = SessionStats()
        s.record_dictation(100, 1.5)
        s.record_dictation(50, 0.5)
        snap = s.snapshot()
        self.assertEqual(snap["dictations"], 2)
        self.assertEqual(snap["total_dictation_chars"], 150)
        self.assertAlmostEqual(snap["total_dictation_time"], 2.0)

    def test_record_meeting_accumulates(self):
        s = SessionStats()
        s.record_meeting(3600, 5000)
        s.record_meeting(1800, 2500)
        snap = s.snapshot()
        self.assertEqual(snap["meetings"], 2)
        self.assertEqual(snap["total_meeting_seconds"], 5400)
        self.assertEqual(snap["total_meeting_words"], 7500)

    def test_snapshot_includes_session_start_and_is_a_copy(self):
        s = SessionStats()
        snap = s.snapshot()
        self.assertIn("session_start", snap)
        snap["dictations"] = 999  # mutating the snapshot...
        self.assertEqual(s.snapshot()["dictations"], 0, "...must not affect the source")

    def test_concurrent_increments_do_not_lose_updates(self):
        # The bug this class fixes: bare-dict `+=` from many threads loses
        # increments. 8 threads x 250 dictations each must total exactly 2000.
        s = SessionStats()
        per_thread = 250
        barrier = threading.Barrier(8)

        def _hammer():
            barrier.wait(timeout=2.0)
            for _ in range(per_thread):
                s.record_dictation(10, 0.01)
                s.record_feature_suggestion()

        threads = [threading.Thread(target=_hammer, daemon=True) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        snap = s.snapshot()
        self.assertEqual(snap["dictations"], 8 * per_thread)
        self.assertEqual(snap["feature_suggestions"], 8 * per_thread)
        self.assertEqual(snap["total_dictation_chars"], 8 * per_thread * 10)
        self.assertAlmostEqual(
            snap["total_dictation_time"], 8 * per_thread * 0.01, places=2
        )


if __name__ == "__main__":
    unittest.main()
