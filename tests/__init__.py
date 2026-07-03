"""Test-run isolation, applied on package import.

The tray app runs FROM this repo checkout, so before this seam existed
test runs wrote into the live app log (whisper_sync/logs/app/) and the
live data dir (worker-pids.json picked up test pids, gpu-guard.jsonl got
test events). Pointing WS_LOG_DIR and WS_DATA_DIR at a disposable temp
dir keeps production diagnostics clean.

It lives here, not only in conftest.py, because the venv suite runs via
``python -m unittest tests.<module>`` where pytest conftest never loads;
both runners import this package before any test module, and
whisper_sync.logger resolves its log dir at import time. The env vars
propagate to subprocesses, so the opt-in E2E test (WS_E2E=1) spawns its
production worker with the same isolation.

An explicitly set WS_LOG_DIR / WS_DATA_DIR is respected, so a developer
can still point a test run at a specific location.
"""

import os
import tempfile

_root = None


def _default_env(name: str, subdir: str) -> None:
    global _root
    if os.environ.get(name):
        return
    if _root is None:
        _root = tempfile.mkdtemp(prefix="whispersync-tests-")
    os.environ[name] = os.path.join(_root, subdir)


_default_env("WS_LOG_DIR", "logs")
_default_env("WS_DATA_DIR", "data")
