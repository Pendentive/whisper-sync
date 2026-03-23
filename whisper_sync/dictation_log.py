"""Daily dictation history — append-only markdown log per day."""

import threading
from datetime import datetime
from pathlib import Path

_LOG_DIR = Path(__file__).parent / "logs" / "data" / "dictation"
_lock = threading.Lock()


def append(text: str, duration: float) -> None:
    """Append a dictation entry to today's log file.

    Args:
        text: The transcribed text (exactly as pasted).
        duration: Total pipeline time in seconds (stop → paste).
    """
    if not text or not text.strip():
        return

    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    log_file = _LOG_DIR / f"{now:%Y-%m-%d}.md"

    entry = f"\n## {now:%H:%M:%S} | {duration:.2f}s | {len(text)} chars\n{text}\n"

    with _lock:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(entry)
