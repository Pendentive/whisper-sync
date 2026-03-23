"""Daily dictation history -- append-only markdown log per day."""

import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Dict

from .paths import get_dictation_log_dir

logger = logging.getLogger("whisper_sync")

_LOG_DIR = get_dictation_log_dir()
_lock = threading.Lock()

_HEADER_RE = re.compile(r"^## (\d{2}:\d{2}):\d{2} \|.*?\| (\d+) chars$")


def append(text: str, duration: float) -> None:
    """Append a dictation entry to today's log file.

    Args:
        text: The transcribed text (exactly as pasted).
        duration: Total pipeline time in seconds (stop -> paste).
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


def load_recent(count: int = 10) -> List[Dict]:
    """Load the most recent dictation entries from log files on disk.

    Reads daily log files in reverse chronological order and parses
    entries until *count* entries are collected.

    Args:
        count: Maximum number of entries to return.

    Returns:
        List of dicts matching the in-memory history format:
        ``{"text": str, "timestamp": str, "chars": int}``.
        Oldest first (same order as the in-memory list).
    """
    if not _LOG_DIR.exists():
        return []

    # Gather log files sorted newest-first
    log_files = sorted(_LOG_DIR.glob("*.md"), reverse=True)
    if not log_files:
        return []

    results: List[Dict] = []

    for log_file in log_files:
        if len(results) >= count:
            break
        try:
            entries = _parse_log_file(log_file)
            results.extend(entries)
        except Exception as e:
            logger.debug(f"Failed to parse dictation log {log_file}: {e}")
            continue  # skip corrupt files

    # Keep only the most recent *count*, oldest-first
    results = results[:count]
    results.reverse()
    return results


def _parse_log_file(path: Path) -> List[Dict]:
    """Parse a single daily log file into entry dicts (newest-first)."""
    text = path.read_text(encoding="utf-8")
    entries: List[Dict] = []
    current_timestamp = None
    current_chars = None
    body_lines: List[str] = []

    for line in text.splitlines():
        m = _HEADER_RE.match(line)
        if m:
            # Flush previous entry
            if current_timestamp is not None:
                body = "\n".join(body_lines).strip()
                if body:
                    entries.append({
                        "text": body,
                        "timestamp": current_timestamp,
                        "chars": current_chars,
                    })
            current_timestamp = m.group(1)
            current_chars = int(m.group(2))
            body_lines = []
        else:
            body_lines.append(line)

    # Flush last entry
    if current_timestamp is not None:
        body = "\n".join(body_lines).strip()
        if body:
            entries.append({
                "text": body,
                "timestamp": current_timestamp,
                "chars": current_chars,
            })

    # Return newest-first so the outer loop collects most-recent across files
    entries.reverse()
    return entries
