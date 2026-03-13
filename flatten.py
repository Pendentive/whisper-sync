"""Flatten whisperX transcript JSON into speaker-attributed readable text.

Produces a lightweight text file that Claude reads instead of the full JSON,
reducing token consumption by ~90% while preserving conversational structure
including interruptions, speaker changes, and natural pause boundaries.

Usage:
    python -m whisper_sync.flatten <transcript.json>

Writes transcript-readable.txt next to the JSON file.
"""

import json
import sys
from pathlib import Path

# Seconds of silence between consecutive same-speaker segments
# that triggers a paragraph break (preserves topic shifts / pauses)
PAUSE_THRESHOLD = 2.0


def flatten(json_path: str) -> str:
    """Flatten transcript JSON into readable speaker-attributed text.

    Returns the output file path.
    """
    path = Path(json_path)
    with open(path) as f:
        data = json.load(f)

    speaker_map = data.get("speaker_map", {})
    segments = data.get("segments", [])

    if not segments:
        return ""

    lines: list[str] = []
    prev_speaker = None
    prev_end = 0.0

    for seg in segments:
        raw_speaker = seg.get("speaker", "UNKNOWN")
        name = speaker_map.get(raw_speaker, raw_speaker)
        text = seg.get("text", "").strip()
        start = seg.get("start", 0.0)

        if not text:
            continue

        speaker_changed = (name != prev_speaker)
        long_pause = (name == prev_speaker and (start - prev_end) > PAUSE_THRESHOLD)

        if speaker_changed:
            # New speaker tag on a new line
            if lines:
                lines.append("")  # blank line between speakers
            lines.append(f"[{name}] {text}")
        elif long_pause:
            # Same speaker, but a significant pause — new paragraph
            lines.append("")
            lines.append(f"[{name}] {text}")
        else:
            # Same speaker, continuation — append to current block
            lines.append(text)

        prev_speaker = name
        prev_end = seg.get("end", start)

    # Add duration line at the top
    last_end = segments[-1].get("end", 0)
    minutes = int(last_end // 60)
    seconds = int(last_end % 60)
    speakers = ", ".join(sorted(set(speaker_map.values()))) if speaker_map else "unknown"

    header = f"Duration: {minutes:02d}:{seconds:02d} | Speakers: {speakers}"

    output = header + "\n\n" + "\n".join(lines) + "\n"

    out_path = path.parent / "transcript-readable.txt"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(output)

    return str(out_path)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m whisper_sync.flatten <transcript.json>")
        sys.exit(1)

    result = flatten(sys.argv[1])
    if result:
        print(f"Written: {result}")
    else:
        print("No segments found.")
        sys.exit(1)
