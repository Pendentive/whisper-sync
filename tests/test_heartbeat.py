"""Tests for whisper_sync.heartbeat."""

import io
import logging
import time
import unittest


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        self.logger = logging.getLogger(f"test_heartbeat.{self.id()}")
        self.logger.handlers.clear()
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.DEBUG)

    def test_heartbeat_emits_lines_at_interval(self):
        from whisper_sync.heartbeat import Heartbeat
        hb = Heartbeat(self.logger, interval=0.05)
        hb.start()
        try:
            time.sleep(0.18)
        finally:
            hb.stop(timeout=0.5)
        lines = [ln for ln in self.stream.getvalue().splitlines() if "heartbeat" in ln]
        # Expect at least 2 heartbeats in ~180ms with 50ms interval
        self.assertGreaterEqual(len(lines), 2)

    def test_heartbeat_stops_cleanly(self):
        from whisper_sync.heartbeat import Heartbeat
        hb = Heartbeat(self.logger, interval=0.05)
        hb.start()
        time.sleep(0.08)
        hb.stop(timeout=0.5)
        # After stop, no more heartbeats should fire
        baseline = self.stream.getvalue().count("heartbeat")
        time.sleep(0.15)
        self.assertEqual(self.stream.getvalue().count("heartbeat"), baseline)

    def test_start_twice_is_safe(self):
        from whisper_sync.heartbeat import Heartbeat
        hb = Heartbeat(self.logger, interval=0.05)
        hb.start()
        hb.start()  # second start must not raise or leak a second thread
        hb.stop(timeout=0.5)

    def test_heartbeat_line_has_uptime(self):
        from whisper_sync.heartbeat import Heartbeat
        hb = Heartbeat(self.logger, interval=0.05)
        hb.start()
        time.sleep(0.1)
        hb.stop(timeout=0.5)
        output = self.stream.getvalue()
        self.assertIn("uptime=", output)

    def test_uptime_computed_even_when_started_at_is_zero(self):
        # Regression for review #5: a 0.0 value for _started_at must not
        # be treated as falsey (which `or` would do) and force uptime
        # to collapse to ~0 forever.
        from whisper_sync.heartbeat import Heartbeat
        hb = Heartbeat(self.logger, interval=0.05)
        hb.start()
        # Overwrite after start() so the worker thread sees 0.0
        hb._started_at = 0.0
        time.sleep(0.12)
        hb.stop(timeout=0.5)
        import re
        matches = re.findall(r"uptime=([\d.]+)s", self.stream.getvalue())
        self.assertTrue(matches, "no heartbeat uptime lines captured")
        # With _started_at=0.0 and monotonic typically >>0, uptime should
        # be a large positive number. If `or` were still in place, the
        # fallback would use time.monotonic() and uptime would collapse
        # to ~0s each tick. With `is None`, it stays anchored at 0.
        self.assertGreater(
            float(matches[-1]), 1.0,
            "uptime collapsed to ~0s — the `or` fallback is back",
        )


if __name__ == "__main__":
    unittest.main()


class RssGaugeTests(unittest.TestCase):
    def test_get_rss_mb_returns_nonnegative_int(self):
        from whisper_sync.heartbeat import get_rss_mb
        rss = get_rss_mb()
        self.assertIsInstance(rss, int)
        self.assertGreaterEqual(rss, 0)

    def test_get_rss_mb_positive_on_windows(self):
        import os
        if os.name != "nt":
            self.skipTest("Windows-only gauge")
        from whisper_sync.heartbeat import get_rss_mb
        self.assertGreater(
            get_rss_mb(), 0,
            "a running Python process must report nonzero working set",
        )

    def test_heartbeat_line_includes_rss(self):
        import io
        import logging
        import time
        from whisper_sync.heartbeat import Heartbeat
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        lg = logging.getLogger(f"test_heartbeat_rss.{self.id()}")
        lg.handlers.clear()
        lg.addHandler(handler)
        lg.setLevel(logging.DEBUG)
        hb = Heartbeat(lg, interval=0.05)
        hb.start()
        try:
            time.sleep(0.12)
        finally:
            hb.stop(timeout=0.5)
        out = stream.getvalue()
        self.assertIn(
            "rss=", out,
            "heartbeat must log RSS so memory incidents are diagnosable "
            "from the forensic log",
        )
