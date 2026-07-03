"""The WS_LOG_DIR / WS_DATA_DIR isolation seam (tests/__init__.py).

Pins that pytest runs are redirected away from the live app log and
live data dir - the tray app executes from this repo checkout, so a
regression here silently pollutes production diagnostics again.
"""

import os
import unittest
from pathlib import Path

from whisper_sync import logger, paths


class PathIsolationTests(unittest.TestCase):
    def test_isolation_seam_set_both_overrides(self):
        self.assertTrue(os.environ.get("WS_LOG_DIR"))
        self.assertTrue(os.environ.get("WS_DATA_DIR"))

    def test_app_log_dir_is_redirected(self):
        # logger resolves its dir at import time; tests/__init__.py ran
        # first (both runners import the tests package before any test
        # module).
        self.assertEqual(logger._LOG_DIR, Path(os.environ["WS_LOG_DIR"]))
        repo_log_dir = Path(logger.__file__).parent / "logs" / "app"
        self.assertNotEqual(logger._LOG_DIR, repo_log_dir)

    def test_data_dir_is_redirected(self):
        data_dir = paths.get_data_dir()
        self.assertEqual(data_dir, Path(os.environ["WS_DATA_DIR"]))
        self.assertTrue(data_dir.is_dir())

    def test_data_dir_override_is_read_per_call(self):
        # get_data_dir honors the env var at call time, not import time,
        # so a test may retarget it with mock.patch.dict if needed.
        original = os.environ["WS_DATA_DIR"]
        try:
            alt = str(Path(original) / "alt")
            os.environ["WS_DATA_DIR"] = alt
            self.assertEqual(paths.get_data_dir(), Path(alt))
        finally:
            os.environ["WS_DATA_DIR"] = original


if __name__ == "__main__":
    unittest.main()
