"""Daily dictation history -- append-only JSON log per day.

Each daily file (YYYY-MM-DD.json) contains a JSON array of entries:
    [{"timestamp": "2026-03-25T11:30:45", "duration": 1.28, "chars": 9, "text": "..."}]

Backwards compatible: load_recent() reads both .json (new) and .md (legacy) files.
"""

import json
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

# Legacy markdown header regex for backwards-compatible reading
_HEADER_RE = re.compile(r"^## (\d{2}:\d{2}):(\d{2}) \|.*?\| (\d+) chars$")


def append(text: str, duration: float) -> None:
    """Append a dictation entry to today's JSON log file.

    Args:
        text: The transcribed text (exactly as pasted).
        duration: Total pipeline time in seconds (stop -> paste).
    """
    if not text or not text.strip():
        return

    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    log_file = _LOG_DIR / f"{now:%Y-%m-%d}.json"

    entry = {
        "timestamp": now.isoformat(timespec="seconds"),
        "duration": round(duration, 2),
        "chars": len(text),
        "text": text,
    }

    with _lock:
        entries = []
        if log_file.exists():
            try:
                entries = json.loads(log_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                logger.debug(f"Could not read existing log {log_file}, starting fresh")
        entries.append(entry)
        log_file.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_recent(count: int = 10) -> List[Dict]:
    """Load the most recent dictation entries from log files on disk.

    Reads daily log files (JSON and legacy markdown) in reverse chronological
    order and collects entries until *count* entries are gathered.

    Args:
        count: Maximum number of entries to return.

    Returns:
        List of dicts: ``{"text": str, "timestamp": str, "chars": int}``.
        Oldest first (same order as the in-memory list).
    """
    if not _LOG_DIR.exists():
        return []

    # Gather all log files (json + legacy md), sorted newest-first by stem
    json_files = {f.stem: f for f in _LOG_DIR.glob("*.json")}
    md_files = {f.stem: f for f in _LOG_DIR.glob("*.md")}

    # Merge: prefer .json over .md for the same date
    all_dates = sorted(set(json_files) | set(md_files), reverse=True)
    if not all_dates:
        return []

    results: List[Dict] = []

    for date_stem in all_dates:
        if len(results) >= count:
            break
        try:
            if date_stem in json_files:
                entries = _parse_json_file(json_files[date_stem])
            else:
                entries = _parse_md_file(md_files[date_stem])
            results.extend(entries)
        except Exception as e:
            logger.debug(f"Failed to parse dictation log {date_stem}: {e}")
            continue

    # Keep only the most recent *count*, oldest-first
    results = results[:count]
    results.reverse()
    return results


def _parse_json_file(path: Path) -> List[Dict]:
    """Parse a JSON daily log file into entry dicts (newest-first)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = []
    for item in data:
        ts_raw = item.get("timestamp", "")
        # Extract HH:MM for display (matches legacy format)
        try:
            ts_display = datetime.fromisoformat(ts_raw).strftime("%H:%M")
        except (ValueError, TypeError):
            ts_display = ts_raw[:5] if len(ts_raw) >= 5 else ts_raw
        entries.append({
            "text": item.get("text", ""),
            "timestamp": ts_display,
            "chars": item.get("chars", 0),
        })
    # Return newest-first
    entries.reverse()
    return entries


def _parse_md_file(path: Path) -> List[Dict]:
    """Parse a legacy markdown daily log file into entry dicts (newest-first)."""
    text = path.read_text(encoding="utf-8")
    entries: List[Dict] = []
    current_timestamp = None
    current_chars = None
    body_lines: List[str] = []

    for line in text.splitlines():
        m = _HEADER_RE.match(line)
        if m:
            if current_timestamp is not None:
                body = "\n".join(body_lines).strip()
                if body:
                    entries.append({
                        "text": body,
                        "timestamp": current_timestamp,
                        "chars": current_chars,
                    })
            current_timestamp = m.group(1)
            current_chars = int(m.group(3))
            body_lines = []
        else:
            body_lines.append(line)

    if current_timestamp is not None:
        body = "\n".join(body_lines).strip()
        if body:
            entries.append({
                "text": body,
                "timestamp": current_timestamp,
                "chars": current_chars,
            })

    # Return newest-first
    entries.reverse()
    return entries
