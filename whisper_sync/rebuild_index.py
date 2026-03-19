"""Rebuild INDEX.md files for local-transcriptions.

Generates:
  - Root INDEX.md: lists all week folders with date ranges and meeting counts
  - Per-week INDEX.md: lists all meetings with time, duration, speakers, one-line summary

Auto-called by WhisperSync after recording save and by split_meeting.py after split.
Can also be run standalone: python scripts/whisper_sync/rebuild_index.py

Reads the > Summary: line from minutes.md for each meeting. Falls back to folder name.
"""

import json
import re
import wave
from datetime import datetime
from pathlib import Path


WEEK_RE = re.compile(r"^(\d{2})-w(\d)$")  # e.g., 03-w3
MEETING_RE = re.compile(r"^(\d{2})(\d{2})_(\d{2})(\d{2})_(.+)$")  # e.g., 0318_0934_description

MONTH_NAMES = {
    "01": "Jan", "02": "Feb", "03": "Mar", "04": "Apr",
    "05": "May", "06": "Jun", "07": "Jul", "08": "Aug",
    "09": "Sep", "10": "Oct", "11": "Nov", "12": "Dec",
}

WEEK_DAY_RANGES = {1: (1, 7), 2: (8, 14), 3: (15, 21), 4: (22, 28), 5: (29, 31)}


def get_meeting_info(meeting_dir: Path) -> dict | None:
    """Extract metadata from a meeting folder."""
    m = MEETING_RE.match(meeting_dir.name)
    if not m:
        return None

    month, day, hour, minute, description = m.groups()

    info = {
        "folder": meeting_dir.name,
        "month": int(month),
        "day": int(day),
        "hour": int(hour),
        "minute": int(minute),
        "description": description,
        "duration": None,
        "speakers": None,
        "summary": None,
    }

    # Get duration from WAV
    wav = meeting_dir / "recording.wav"
    if wav.exists():
        try:
            with wave.open(str(wav), "rb") as w:
                info["duration"] = w.getnframes() / w.getframerate()
        except Exception:
            pass

    # Get speakers and summary from minutes.md
    minutes = meeting_dir / "minutes.md"
    if minutes.exists():
        try:
            text = minutes.read_text(encoding="utf-8")
            # Parse > Date: ... | Duration: ... | Speakers: ...
            for line in text.split("\n"):
                if line.startswith("> Date:") and "Speakers:" in line:
                    speakers_part = line.split("Speakers:")[-1].strip()
                    info["speakers"] = speakers_part
                    break
            # Parse > Summary: line (one-liner)
            for line in text.split("\n"):
                if line.startswith("> Summary:"):
                    info["summary"] = line[len("> Summary:"):].strip()
                    break
            # Fallback: use first Key Topics item as summary
            if not info["summary"]:
                in_topics = False
                for line in text.split("\n"):
                    if "### Key Topics" in line:
                        in_topics = True
                        continue
                    if in_topics and line.startswith("- "):
                        info["summary"] = line[2:].strip()
                        break
                    if in_topics and line.startswith("#"):
                        break
        except Exception:
            pass

    return info


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    mins = int(seconds / 60)
    if mins >= 60:
        return f"{mins // 60}h {mins % 60}m"
    return f"{mins}m"


def rebuild_week_index(week_dir: Path) -> int:
    """Rebuild INDEX.md for a single week folder. Returns meeting count."""
    m = WEEK_RE.match(week_dir.name)
    if not m:
        return 0

    month_num = m.group(1)
    week_num = int(m.group(2))
    month_name = MONTH_NAMES.get(month_num, month_num)
    day_start, day_end = WEEK_DAY_RANGES.get(week_num, (1, 7))

    meetings = []
    for item in sorted(week_dir.iterdir()):
        if item.is_dir() and not item.name.startswith("."):
            info = get_meeting_info(item)
            if info:
                meetings.append(info)

    if not meetings:
        # Remove empty INDEX if no meetings
        idx = week_dir / "INDEX.md"
        if idx.exists():
            idx.unlink()
        return 0

    lines = [
        f"# {week_dir.name} — {month_name} {day_start}-{day_end}",
        "",
        f"| Time | Meeting | Duration | Speakers | Summary |",
        f"|------|---------|----------|----------|---------|",
    ]

    for info in meetings:
        time_str = f"{info['month']:02d}{info['day']:02d} {info['hour']:02d}:{info['minute']:02d}"
        dur = format_duration(info["duration"])
        speakers = info["speakers"] or "—"
        summary = info["summary"] or info["description"].replace("-", " ")
        # Truncate speakers only (summaries kept full for LLM search)
        if len(speakers) > 50:
            speakers = speakers[:47] + "..."
        name = info["description"]
        lines.append(f"| {time_str} | {name} | {dur} | {speakers} | {summary} |")

    lines.append("")

    (week_dir / "INDEX.md").write_text("\n".join(lines), encoding="utf-8")
    return len(meetings)


def rebuild_root_index(base_dir: Path):
    """Rebuild the root INDEX.md that lists all week folders."""
    weeks = []
    for item in sorted(base_dir.iterdir()):
        if item.is_dir() and WEEK_RE.match(item.name):
            m = WEEK_RE.match(item.name)
            month_num = m.group(1)
            week_num = int(m.group(2))
            month_name = MONTH_NAMES.get(month_num, month_num)
            day_start, day_end = WEEK_DAY_RANGES.get(week_num, (1, 7))

            count = rebuild_week_index(item)
            if count > 0:
                weeks.append({
                    "name": item.name,
                    "dates": f"{month_name} {day_start}-{day_end}",
                    "count": count,
                })

    # Determine current week
    now = datetime.now()
    current_week = f"{now.strftime('%m')}-w{(now.day - 1) // 7 + 1}"

    lines = [
        "# Local Transcriptions Index",
        "",
        "Navigate: root INDEX (this file) → week INDEX → meeting transcript.",
        "",
        "| Week | Dates | Meetings | |",
        "|------|-------|----------|-|",
    ]

    for w in reversed(weeks):  # Most recent first
        note = "current" if w["name"] == current_week else ""
        lines.append(f"| [{w['name']}]({w['name']}/INDEX.md) | {w['dates']} | {w['count']} | {note} |")

    lines.append("")

    (base_dir / "INDEX.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"Root INDEX: {len(weeks)} weeks, {sum(w['count'] for w in weeks)} meetings total")


def rebuild_all(base_dir: Path):
    """Rebuild all INDEX.md files."""
    rebuild_root_index(base_dir)
    for item in sorted(base_dir.iterdir()):
        if item.is_dir() and WEEK_RE.match(item.name):
            count = rebuild_week_index(item)
            print(f"  {item.name}: {count} meetings")


if __name__ == "__main__":
    base = Path(__file__).parent.parent.parent / "meetings" / "local-transcriptions"
    rebuild_all(base)
    print("Done.")
