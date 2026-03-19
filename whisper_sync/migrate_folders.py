"""Migrate local-transcriptions to MM-wN week-based folder structure.

Usage:
    python scripts/whisper_sync/migrate_folders.py [--dry-run]

Handles migration from:
  - Year-based:  2026/YYYY-MM-DD_description/
  - Month-based: MM/MMDD_HHMM_description/
To:
  - Week-based:  MM-wN/MMDD_HHMM_description/

Week calculation: w1=days 1-7, w2=8-14, w3=15-21, w4=22-28, w5=29-31.
"""

import re
import shutil
import sys
import wave
from datetime import datetime, timedelta
from pathlib import Path


def get_recording_start_time(folder: Path) -> datetime | None:
    """Get recording start time: WAV mtime minus duration."""
    wav = folder / "recording.wav"
    if not wav.exists():
        return None
    mtime = datetime.fromtimestamp(wav.stat().st_mtime)
    try:
        with wave.open(str(wav), "rb") as w:
            duration = w.getnframes() / w.getframerate()
        return mtime - timedelta(seconds=duration)
    except Exception:
        return mtime


def week_dir_for_date(dt: datetime) -> str:
    """Return MM-wN folder name for a date. w1=days 1-7, w2=8-14, etc."""
    return f"{dt.strftime('%m')}-w{(dt.day - 1) // 7 + 1}"


# Regex patterns
HYBRID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_(\d{4})_(.+)$")
OLD_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(.+)$")
NEW_RE = re.compile(r"^(\d{2})(\d{2})_\d{4}_.+$")  # captures month AND day


def _resolve_folder(name: str, folder: Path) -> tuple[str, str, str] | None:
    """Parse a folder name and return (week_dir, new_name, format_label) or None."""

    # Hybrid: 2026-03-18_0934_description
    m = HYBRID_RE.match(name)
    if m:
        hhmm = m.group(1)
        description = m.group(2)
        dt = datetime.strptime(name[:10], "%Y-%m-%d")
        new_name = f"{dt.strftime('%m%d')}_{hhmm}_{description}"
        return week_dir_for_date(dt), new_name, "hybrid"

    # Old: 2026-03-17_description
    m = OLD_RE.match(name)
    if m:
        date_part = m.group(1)
        description = m.group(2)
        dt = get_recording_start_time(folder)
        if dt is None:
            dt = datetime.strptime(date_part, "%Y-%m-%d")
        new_name = f"{dt.strftime('%m%d_%H%M')}_{description}"
        return week_dir_for_date(dt), new_name, "old"

    # New: 0318_0934_description (already MMDD_HHMM format)
    m = NEW_RE.match(name)
    if m:
        month = int(m.group(1))
        day = int(m.group(2))
        # Construct a date for week calculation (use current year)
        dt = datetime(datetime.now().year, month, day)
        return week_dir_for_date(dt), name, "new"

    return None


def _move(src: Path, dest_dir: Path, new_name: str, fmt: str, dry_run: bool):
    dest = dest_dir / new_name
    print(f"  [{fmt:6s}] {src.name} -> {dest_dir.name}/{new_name}")
    if dry_run:
        return
    if dest.exists():
        print(f"           SKIP (already exists): {dest}")
        return
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))


def migrate(base_dir: Path, dry_run: bool = False):
    """Migrate all meeting folders to MM-wN week structure."""
    moved = 0

    # Phase 1: Migrate from 2026/ (year-based, old format)
    year_dir = base_dir / "2026"
    if year_dir.exists():
        print("Phase 1: Migrating from 2026/ (year-based)...")
        for folder in sorted(year_dir.iterdir()):
            if not folder.is_dir():
                continue
            result = _resolve_folder(folder.name, folder)
            if result is None:
                print(f"  SKIP (unknown format): {folder.name}")
                continue
            week_dir, new_name, fmt = result
            _move(folder, base_dir / week_dir, new_name, fmt, dry_run)
            moved += 1

        if not dry_run and year_dir.exists():
            remaining = list(year_dir.iterdir())
            if not remaining:
                year_dir.rmdir()
                print(f"  Removed empty: {year_dir.name}/")

    # Phase 2: Migrate from MM/ (month-based, flat)
    for month_dir in sorted(base_dir.iterdir()):
        if not month_dir.is_dir():
            continue
        # Match pure month folders like "03" but not "03-w1"
        if not re.match(r"^\d{2}$", month_dir.name):
            continue

        print(f"Phase 2: Migrating from {month_dir.name}/ (month-based)...")
        for folder in sorted(month_dir.iterdir()):
            if not folder.is_dir():
                continue
            result = _resolve_folder(folder.name, folder)
            if result is None:
                print(f"  SKIP (unknown format): {folder.name}")
                continue
            week_dir, new_name, fmt = result
            _move(folder, base_dir / week_dir, new_name, fmt, dry_run)
            moved += 1

        if not dry_run and month_dir.exists():
            remaining = list(month_dir.iterdir())
            if not remaining:
                month_dir.rmdir()
                print(f"  Removed empty: {month_dir.name}/")

    print(f"\nMigrated {moved} folders.")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    base = Path(__file__).parent.parent.parent / "meetings" / "local-transcriptions"
    if dry_run:
        print("=== DRY RUN (no changes) ===")
    migrate(base, dry_run=dry_run)
    print("Done.")
