"""Speaker identification and config management for WhisperSync.

Handles:
- LLM-based speaker identification via claude -p
- Writing speaker_map to transcript.json
- Updating transcription-config.md with new speaker data
"""

import json
import re
import subprocess
from pathlib import Path

from .logger import logger


def identify_speakers(
    transcript_json_path: str,
    config_path: str,
    folder_name: str,
) -> dict | None:
    """Identify speakers via Claude CLI.

    Args:
        transcript_json_path: Path to transcript.json
        config_path: Path to transcription-config.md
        folder_name: Meeting folder name (for pattern matching)

    Returns:
        Parsed JSON dict with speaker_map, confidence, reasoning, config_updates.
        None if identification failed.
    """
    prompt_path = Path(__file__).parent / "speaker_prompt.md"
    if not prompt_path.exists():
        logger.warning(f"Speaker prompt not found: {prompt_path}")
        return None

    # Load first 30 segments
    with open(transcript_json_path) as f:
        data = json.load(f)
    segments = data.get("segments", [])[:30]
    if not segments:
        return None

    # Format segments for the prompt
    seg_text = ""
    for s in segments:
        speaker = s.get("speaker", "UNKNOWN")
        text = s.get("text", "").strip()
        start = s.get("start", 0)
        mins, secs = int(start // 60), int(start % 60)
        seg_text += f"[{mins:02d}:{secs:02d}] {speaker}: {text}\n"

    # Load config
    config_text = ""
    config_file = Path(config_path)
    if config_file.exists():
        config_text = config_file.read_text(encoding="utf-8")

    # Load prompt template
    prompt_template = prompt_path.read_text(encoding="utf-8")

    # Build full prompt
    full_prompt = (
        f"{prompt_template}\n\n"
        f"---\n\n"
        f"Meeting folder: {folder_name}\n\n"
        f"Known speakers config:\n{config_text}\n\n"
        f"Transcript (first 30 segments):\n{seg_text}"
    )

    max_attempts = 2
    timeout_s = 90

    for attempt in range(max_attempts):
        try:
            result = subprocess.run(
                ["claude", "-p", "--model", "haiku"],
                input=full_prompt,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                cwd=str(Path(__file__).parent.parent.parent),
            )
            if result.returncode != 0:
                logger.warning(f"Speaker identification CLI failed: {result.returncode}")
                if result.stderr:
                    logger.debug(f"Claude CLI stderr (truncated): {result.stderr[:500]}")
                return None

            # Robust JSON extraction: find first { to last }
            response = result.stdout.strip()
            first_brace = response.find("{")
            last_brace = response.rfind("}")
            if first_brace == -1 or last_brace == -1 or last_brace <= first_brace:
                logger.warning("Speaker identification response contains no valid JSON object")
                logger.debug(f"Raw response: {response[:500]}")
                return None

            json_str = response[first_brace:last_brace + 1]
            return json.loads(json_str)

        except json.JSONDecodeError as e:
            logger.warning(f"Speaker identification returned invalid JSON: {e}")
            logger.debug(f"Raw response: {result.stdout[:500]}")
            return None
        except FileNotFoundError:
            logger.warning("Claude CLI not found - speaker identification skipped")
            return None
        except subprocess.TimeoutExpired:
            if attempt < max_attempts - 1:
                logger.warning(f"Speaker identification timed out ({timeout_s}s), retrying...")
                continue
            logger.warning(f"Speaker identification timed out after {max_attempts} attempts ({timeout_s}s each)")
            return None
        except Exception as e:
            logger.warning(f"Speaker identification failed: {e}")
            return None


def build_manual_stub(json_path: str, reason: str = "Enter name manually") -> dict | None:
    """Build a stub identification result with empty names for manual entry.

    Reads unique SPEAKER_XX labels from transcript.json and returns a dict
    compatible with _ask_speaker_confirmation().
    """
    import json as _json
    try:
        with open(json_path) as f:
            data = _json.load(f)
        unique_speakers = sorted(set(
            s.get("speaker", "UNKNOWN")
            for s in data.get("segments", [])
            if s.get("speaker")
        ))
        if not unique_speakers:
            return None
        return {
            "speaker_map": {spk: "" for spk in unique_speakers},
            "confidence": {spk: "low" for spk in unique_speakers},
            "reasoning": {spk: reason for spk in unique_speakers},
        }
    except Exception as e:
        logger.warning(f"Could not build manual speaker stub: {e}")
        return None


def write_speaker_map(transcript_json_path: str, speaker_map: dict) -> None:
    """Write speaker_map to transcript.json (additive — does not modify segments)."""
    with open(transcript_json_path) as f:
        data = json.load(f)

    data["speaker_map"] = speaker_map

    with open(transcript_json_path, "w") as f:
        json.dump(data, f, indent=2)

    logger.info(f"Speaker map written: {speaker_map}")


def update_config(config_path: str, speaker_map: dict, config_updates: dict | None) -> None:
    """Update transcription-config.md with new speaker data.

    - Adds new speakers to Known Speakers table
    - Appends new voice notes to existing speakers
    - Does NOT modify Meeting-to-Speaker Map (requires more context — future enhancement)
    """
    config_file = Path(config_path)
    if not config_file.exists():
        logger.warning(f"Config file not found: {config_path}")
        return

    if not config_updates:
        return

    text = config_file.read_text(encoding="utf-8")
    lines = text.splitlines()
    modified = False

    # --- Add new speakers ---
    new_speakers = config_updates.get("new_speakers", [])
    if new_speakers:
        # Find the end of the Known Speakers table
        table_end = None
        in_table = False
        for i, line in enumerate(lines):
            if "| ID | Name | Voice Notes |" in line:
                in_table = True
                continue
            if in_table and line.startswith("|"):
                table_end = i
                continue
            if in_table and not line.startswith("|"):
                break

        if table_end is not None:
            for speaker in new_speakers:
                # Handle both dict and string formats from LLM
                if isinstance(speaker, str):
                    name = speaker
                    notes = ""
                else:
                    name = speaker.get("name", "Unknown")
                    notes = speaker.get("notes", "")
                speaker_id = name.lower().replace(" ", "-")
                # Check if already exists by scanning ID column only
                already_exists = False
                for line in lines:
                    if line.startswith("|") and f"| {speaker_id} |" in line.lower():
                        already_exists = True
                        break
                if not already_exists:
                    new_row = f"| {speaker_id} | {name} | {notes} |"
                    lines.insert(table_end + 1, new_row)
                    table_end += 1
                    modified = True
                    logger.info(f"Added new speaker to config: {name}")

    # --- Update voice notes for existing speakers ---
    new_notes = config_updates.get("new_voice_notes", {})
    if new_notes:
        for speaker_id, note in new_notes.items():
            for i, line in enumerate(lines):
                if line.startswith("|") and f"| {speaker_id} |" in line.lower():
                    # Append note to existing voice notes column
                    parts = line.split("|")
                    if len(parts) >= 4:
                        existing_notes = parts[3].strip()
                        if note.lower() not in existing_notes.lower():
                            parts[3] = f" {existing_notes}; {note} "
                            lines[i] = "|".join(parts)
                            modified = True
                            logger.info(f"Updated voice notes for {speaker_id}: {note}")
                    break

    if modified:
        config_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info("transcription-config.md updated")


def get_config_path() -> str:
    """Return the path to transcription-config.md."""
    from .paths import get_speaker_config_path
    return str(get_speaker_config_path())
