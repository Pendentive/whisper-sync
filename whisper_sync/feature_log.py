"""Feature suggestion log -- JSON-based with status tracking.

Single rolling file (features.json) containing all feature suggestions.
Each entry tracks raw voice transcription, Claude-formatted version,
and lifecycle status (pending -> in-progress -> completed).
"""

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional

from .paths import get_feature_log_dir

logger = logging.getLogger("whisper_sync")

_lock = threading.Lock()


def _log_dir() -> Path:
    """Resolve feature log directory at call time (respects runtime output_dir changes)."""
    return get_feature_log_dir()


def _log_file() -> Path:
    return _log_dir() / "features.json"


def _read() -> List[Dict]:
    """Read the feature log from disk."""
    path = _log_file()
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        # Preserve corrupt file to avoid silent data loss
        logger.warning(f"Could not read feature log {path}: {e}")
        try:
            timestamp = datetime.now().strftime("%H%M%S")
            corrupt_path = path.with_suffix(f".json.corrupt-{timestamp}")
            path.rename(corrupt_path)
            logger.warning(f"Renamed corrupt feature log to {corrupt_path}")
        except OSError as rename_err:
            logger.error(f"Failed to preserve corrupt feature log: {rename_err}")
        return []


def _write(entries: List[Dict]) -> None:
    """Write the feature log to disk."""
    log_dir = _log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    _log_file().write_text(
        json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def append_raw(text: str, duration: float) -> str:
    """Append a raw feature suggestion entry.

    Args:
        text: The raw voice transcription.
        duration: Pipeline time in seconds (stop -> transcribe).

    Returns:
        The entry ID (ISO timestamp string) for later updates.
    """
    now = datetime.now()
    entry_id = now.isoformat(timespec="microseconds")

    entry = {
        "id": entry_id,
        "timestamp": entry_id,
        "duration": round(duration, 2),
        "chars": len(text),
        "raw": text,
        "consolidated": None,
        "status": "pending",
        "pr": None,
    }

    with _lock:
        entries = _read()
        entries.append(entry)
        _write(entries)

    return entry_id


def update_consolidated(entry_id: str, consolidated: str) -> bool:
    """Set the Claude-formatted consolidated text for an entry.

    Args:
        entry_id: The ID returned by append_raw().
        consolidated: The formatted feature request text.

    Returns:
        True if the entry was found and updated.
    """
    with _lock:
        entries = _read()
        for entry in entries:
            if entry.get("id") == entry_id:
                entry["consolidated"] = consolidated
                _write(entries)
                return True
    logger.warning(f"Feature entry not found for consolidation: {entry_id}")
    return False


def update_status(entry_id: str, status: str, pr: Optional[str] = None) -> bool:
    """Update the status (and optional PR link) for an entry.

    Args:
        entry_id: The ID returned by append_raw().
        status: One of "pending", "in-progress", "completed".
        pr: Optional PR URL or branch name.

    Returns:
        True if the entry was found and updated.
    """
    with _lock:
        entries = _read()
        for entry in entries:
            if entry.get("id") == entry_id:
                entry["status"] = status
                if pr is not None:
                    entry["pr"] = pr
                _write(entries)
                return True
    logger.warning(f"Feature entry not found for status update: {entry_id}")
    return False


def load_all() -> List[Dict]:
    """Return all feature suggestion entries."""
    return _read()


def load_pending() -> List[Dict]:
    """Return entries with status 'pending'."""
    return [e for e in _read() if e.get("status") == "pending"]


def load_recent(count: int = 10) -> List[Dict]:
    """Return the most recent N entries (newest first)."""
    entries = _read()
    return list(reversed(entries[-count:]))
