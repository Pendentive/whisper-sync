"""Persistent weekly stats with RAM buffering and lifetime tracking.

Stats accumulate in memory and flush to disk every 60 seconds and on
shutdown.  The on-disk format is a single JSON file at
``{data_dir}/stats/weekly-stats.json`` containing per-week buckets and a
lifetime aggregate.
"""

import json
import threading
import time
from datetime import datetime
from pathlib import Path

from .logger import logger
from .paths import get_stats_dir

_lock = threading.Lock()

# Buffered deltas since last flush
_buffer: dict = {}

_flush_interval = 60  # seconds
_last_flush: float = 0.0

_EMPTY_BUCKET = {
    "dictations": 0,
    "total_dictation_chars": 0,
    "total_dictation_time": 0.0,
    "meetings": 0,
    "total_meeting_seconds": 0,
    "total_meeting_words": 0,
    "feature_suggestions": 0,
}


def _stats_file() -> Path:
    return get_stats_dir() / "weekly-stats.json"


def _current_week() -> str:
    return datetime.now().strftime("%G-W%V")


def _read() -> dict:
    """Read stats from disk. Returns empty structure if file missing."""
    path = _stats_file()
    if not path.exists():
        return {"weeks": {}, "lifetime": {}}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(f"Could not read weekly stats: {exc}")
        return {"weeks": {}, "lifetime": {}}


def _write(data: dict) -> None:
    """Write stats to disk (atomic via temp file)."""
    path = _stats_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp.replace(path)
    except OSError as exc:
        logger.warning(f"Could not write weekly stats: {exc}")


def _add_to_bucket(bucket: dict, key: str, value) -> None:
    bucket[key] = bucket.get(key, 0) + value


def _buffer_event(**kwargs) -> None:
    """Add deltas to the in-memory buffer, flushing if interval elapsed."""
    global _last_flush
    with _lock:
        for k, v in kwargs.items():
            _buffer[k] = _buffer.get(k, 0) + v
        now = time.monotonic()
        if now - _last_flush >= _flush_interval:
            _flush_locked()
            _last_flush = now


def _flush_locked() -> None:
    """Flush buffer to disk. Caller must hold _lock."""
    global _buffer
    if not _buffer:
        return
    buf = _buffer.copy()
    _buffer = {}

    data = _read()
    week = _current_week()

    # Update weekly bucket
    if week not in data["weeks"]:
        data["weeks"][week] = dict(_EMPTY_BUCKET)
    for k, v in buf.items():
        _add_to_bucket(data["weeks"][week], k, v)

    # Update lifetime bucket
    lt = data.get("lifetime", {})
    if "first_session" not in lt:
        lt["first_session"] = datetime.now().isoformat()
    for k, v in buf.items():
        _add_to_bucket(lt, k, v)
    data["lifetime"] = lt

    _write(data)


# ---- Public API ---------------------------------------------------------

def record_dictation(chars: int, duration: float) -> None:
    """Record a dictation event. Buffered in RAM."""
    _buffer_event(dictations=1, total_dictation_chars=chars, total_dictation_time=duration)


def record_meeting(seconds: int, words: int) -> None:
    """Record a meeting event. Buffered in RAM."""
    _buffer_event(meetings=1, total_meeting_seconds=seconds, total_meeting_words=words)


def record_feature_suggestion() -> None:
    """Record a feature suggestion event. Buffered in RAM."""
    _buffer_event(feature_suggestions=1)


def flush() -> None:
    """Flush buffered stats to disk. Called periodically and on shutdown."""
    global _last_flush
    with _lock:
        _flush_locked()
        _last_flush = time.monotonic()


def get_current_week() -> dict:
    """Get this week's stats (disk + buffer merged)."""
    with _lock:
        data = _read()
        week = _current_week()
        result = dict(data.get("weeks", {}).get(week, _EMPTY_BUCKET))
        for k, v in _buffer.items():
            result[k] = result.get(k, 0) + v
        return result


def get_lifetime() -> dict:
    """Get lifetime stats (disk + buffer merged)."""
    with _lock:
        data = _read()
        result = dict(data.get("lifetime", {}))
        for k, v in _buffer.items():
            result[k] = result.get(k, 0) + v
        return result


def get_weekly_average(metric: str, weeks: int = 4) -> float:
    """Compute rolling average for a metric over the last N completed weeks.

    The current (partial) week is excluded from the average so results
    are stable throughout the week.
    """
    with _lock:
        data = _read()
        all_weeks = sorted(data.get("weeks", {}).keys())
        current = _current_week()
        completed = [w for w in all_weeks if w != current]
        recent = completed[-weeks:] if len(completed) >= weeks else completed
        if not recent:
            return 0.0
        total = sum(data["weeks"][w].get(metric, 0) for w in recent)
        return total / len(recent)
