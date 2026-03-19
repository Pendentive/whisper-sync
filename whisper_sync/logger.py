"""File-based logging for WhisperSync — persists across crashes."""

import logging
import sys
from datetime import datetime
from pathlib import Path

_LOG_DIR = Path(__file__).parent / "logs" / "app"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

_log_file = _LOG_DIR / f"whisper-sync-{datetime.now():%Y-%m-%d}.log"

# Root logger for the whisper_sync package
logger = logging.getLogger("whisper_sync")
logger.setLevel(logging.DEBUG)

# File handler — DEBUG level, persists everything
_fh = logging.FileHandler(str(_log_file), encoding="utf-8")
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
))
logger.addHandler(_fh)

# Console handler — INFO level
_ch = logging.StreamHandler(sys.stdout)
_ch.setLevel(logging.INFO)
_ch.setFormatter(logging.Formatter("[WhisperSync] %(message)s"))
logger.addHandler(_ch)


def get_log_path() -> Path:
    return _log_file
