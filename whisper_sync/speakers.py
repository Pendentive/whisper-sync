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


def distill_transcript(json_path: str) -> str:
    """Distill full transcript into a compact per-speaker summary.

    Groups all segments by speaker, extracts representative samples
    (first/last appearances, name callouts, longest segments) so that
    every speaker is represented regardless of when they appear in the meeting.
    Returns a formatted text block suitable for LLM consumption.
    """
    with open(json_path) as f:
        data = json.load(f)
    segments = data.get("segments", [])
    if not segments:
        return ""

    from collections import defaultdict
    by_speaker: dict[str, list[dict]] = defaultdict(list)
    for s in segments:
        spk = s.get("speaker", "UNKNOWN")
        by_speaker[spk].append(s)

    name_patterns = re.compile(
        r"\b(?:hey|hi|thanks|thank you|okay|sorry|right)\s+([A-Z][a-z]+)\b"
        r"|(?:^|\W)([A-Z][a-z]+),?\s+(?:can you|could you|do you|what do|please|are you)",
        re.IGNORECASE,
    )

    lines = []
    for spk in sorted(by_speaker.keys()):
        segs = by_speaker[spk]
        total_time = sum(s.get("end", 0) - s.get("start", 0) for s in segs)
        lines.append(f"\n=== {spk} ({len(segs)} segments, ~{total_time / 60:.1f} min) ===")

        def _fmt(s):
            start = s.get("start", 0)
            m, sec = int(start // 60), int(start % 60)
            return f"  [{m:02d}:{sec:02d}] {s.get('text', '').strip()}"

        lines.append("  -- First appearances:")
        for s in segs[:5]:
            lines.append(_fmt(s))

        if len(segs) > 10:
            lines.append("  -- Last appearances:")
            for s in segs[-5:]:
                lines.append(_fmt(s))

        callouts = [s for s in segs if name_patterns.search(s.get("text", ""))]
        if callouts:
            lines.append("  -- Name callouts:")
            for s in callouts[:5]:
                lines.append(_fmt(s))

        longest = sorted(segs, key=lambda s: len(s.get("text", "")), reverse=True)[:3]
        lines.append("  -- Richest content:")
        for s in longest:
            lines.append(_fmt(s))

    return "\n".join(lines)


def compute_fingerprints(json_path: str) -> dict:
    """Extract speaking pattern metrics from word-level transcript data.

    Pure Python, deterministic, no LLM call. Returns a dict keyed by
    SPEAKER_XX with timing, vocabulary, and behavioral metrics.
    """
    from collections import Counter, defaultdict
    import statistics

    with open(json_path) as f:
        data = json.load(f)
    segments = data.get("segments", [])
    if not segments:
        return {}

    FILLERS = {"uh", "um", "like", "so", "yeah", "basically", "actually", "okay", "right", "you know"}

    name_pattern = re.compile(
        r"\b(?:hey|hi|thanks|thank you|okay|sorry)\s+([A-Z][a-z]+)\b"
        r"|(?:^|\W)([A-Z][a-z]+),?\s+(?:can you|could you|do you|what do|please|are you)",
        re.IGNORECASE,
    )

    by_speaker: dict[str, list[dict]] = defaultdict(list)
    for s in segments:
        by_speaker[s.get("speaker", "UNKNOWN")].append(s)

    profiles = {}
    for spk, segs in sorted(by_speaker.items()):
        all_wps = []
        all_gaps = []
        all_scores = []
        word_count = 0
        filler_counts = Counter()
        all_words_lower = []

        for seg in segs:
            words = seg.get("words", [])
            timed_words = [w for w in words if "start" in w and "end" in w]

            if len(timed_words) >= 2:
                seg_start = timed_words[0]["start"]
                seg_end = timed_words[-1]["end"]
                duration = seg_end - seg_start
                if duration > 0:
                    all_wps.append(len(timed_words) / duration)

                for i in range(1, len(timed_words)):
                    gap = timed_words[i]["start"] - timed_words[i - 1]["end"]
                    if gap >= 0:
                        all_gaps.append(gap)

            for w in words:
                if "score" in w:
                    all_scores.append(w["score"])
                word_text = w.get("word", "").strip().lower().strip(".,!?;:'\"")
                if word_text:
                    all_words_lower.append(word_text)
                    word_count += 1
                    if word_text in FILLERS:
                        filler_counts[word_text] += 1

        seg_lengths = [len(seg.get("text", "").split()) for seg in segs]
        first_time = segs[0].get("start", 0)
        last_time = segs[-1].get("end", segs[-1].get("start", 0))

        names_given = []
        for seg in segs:
            text = seg.get("text", "")
            for m in name_pattern.finditer(text):
                name = m.group(1) or m.group(2)
                if name:
                    names_given.append({
                        "name": name,
                        "time": seg.get("start", 0),
                        "context": text.strip()[:80],
                    })

        sir_count = sum(1 for w in all_words_lower if w == "sir")

        stop_words = {
            "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
            "of", "is", "it", "that", "this", "was", "be", "have", "has", "had",
            "do", "does", "did", "will", "would", "could", "should", "can", "may",
            "not", "no", "with", "from", "by", "as", "are", "were", "been", "being",
            "i", "you", "we", "they", "he", "she", "me", "my", "your", "our",
            "what", "which", "who", "how", "when", "where", "why", "if", "then",
            "about", "just", "also", "more", "some", "there", "here", "all", "one",
            "two", "three", "its", "than", "other", "into", "out", "up", "down",
            "very", "much", "well", "going", "know", "think", "want", "need",
            "get", "got", "go", "come", "make", "take", "see", "say", "said",
            "thing", "things",
        }
        vocab = Counter(w for w in all_words_lower if w not in stop_words and w not in FILLERS and len(w) > 2)
        total_fillers = sum(filler_counts.values())

        profiles[spk] = {
            "segment_count": len(segs),
            "word_count": word_count,
            "wps": {
                "avg": round(statistics.mean(all_wps), 2) if all_wps else 0,
                "median": round(statistics.median(all_wps), 2) if all_wps else 0,
                "std": round(statistics.stdev(all_wps), 2) if len(all_wps) > 1 else 0,
            },
            "inter_word_gap_ms": {
                "avg": round(statistics.mean(all_gaps) * 1000, 0) if all_gaps else 0,
                "median": round(statistics.median(all_gaps) * 1000, 0) if all_gaps else 0,
            },
            "word_confidence": {
                "avg": round(statistics.mean(all_scores), 3) if all_scores else 0,
                "median": round(statistics.median(all_scores), 3) if all_scores else 0,
                "low_rate": round(sum(1 for s in all_scores if s < 0.5) / len(all_scores), 3) if all_scores else 0,
            },
            "filler_rate": round(total_fillers / word_count, 3) if word_count > 0 else 0,
            "top_fillers": [w for w, _ in filler_counts.most_common(5)],
            "sir_count": sir_count,
            "words_per_segment": {
                "avg": round(statistics.mean(seg_lengths), 1) if seg_lengths else 0,
                "median": round(statistics.median(seg_lengths), 0) if seg_lengths else 0,
                "max": max(seg_lengths) if seg_lengths else 0,
            },
            "first_appears_s": round(first_time, 1),
            "last_appears_s": round(last_time, 1),
            "names_given": names_given[:10],
            "top_vocab": [w for w, _ in vocab.most_common(15)],
        }

    return profiles


def load_profiles() -> dict:
    """Load speaker profiles from .whispersync/speaker-profiles.json."""
    from .paths import get_data_dir
    path = get_data_dir() / "speaker-profiles.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}


def save_profiles(profiles: dict) -> None:
    """Save speaker profiles to .whispersync/speaker-profiles.json."""
    from .paths import get_data_dir
    path = get_data_dir() / "speaker-profiles.json"
    with open(path, "w") as f:
        json.dump(profiles, f, indent=2)
    logger.info(f"Speaker profiles saved: {list(profiles.keys())}")


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

    # Distill transcript into per-speaker summary
    distilled = distill_transcript(transcript_json_path)
    if not distilled:
        return None

    # Load readable transcript for boundary detection
    readable_path = Path(transcript_json_path).parent / "transcript-readable.txt"
    readable_text = ""
    if readable_path.exists():
        readable_text = readable_path.read_text(encoding="utf-8")

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
        f"Speaker summary (distilled from full transcript):\n{distilled}"
    )
    if readable_text:
        full_prompt += f"\n\n---\n\nFull readable transcript (for boundary detection):\n{readable_text}"

    max_attempts = 2
    timeout_s = 90

    for attempt in range(max_attempts):
        try:
            result = subprocess.run(
                ["claude", "-p", "--model", "sonnet"],
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


def opus_deep_identify(
    transcript_json_path: str,
    config_path: str,
    folder_name: str,
    progress_callback=None,
) -> dict | None:
    """Opus deep speaker identification with fingerprint analysis.

    Sends pre-computed metrics + full transcript to Opus for thorough
    analysis. Updates speaker profiles. Produces meeting analysis data.

    Args:
        transcript_json_path: Path to transcript.json
        config_path: Path to transcription-config.md
        folder_name: Meeting folder name
        progress_callback: Optional callable(phase: str, pct: float) for UI updates

    Returns:
        Parsed JSON dict with speaker_map, confidence, reasoning,
        profile_updates, meeting_boundaries, analysis. None if failed.
    """
    prompt_path = Path(__file__).parent / "speaker_prompt_deep.md"
    if not prompt_path.exists():
        logger.warning(f"Deep speaker prompt not found: {prompt_path}")
        return None

    if progress_callback:
        progress_callback("Computing fingerprints...", 0.05)

    # Step 1: Compute fingerprints (Python, instant)
    fingerprints = compute_fingerprints(transcript_json_path)
    if not fingerprints:
        return None
    fingerprint_text = json.dumps(fingerprints, indent=2)

    if progress_callback:
        progress_callback("Preparing transcript...", 0.10)

    # Step 2: Load full transcript segments
    with open(transcript_json_path) as f:
        data = json.load(f)
    segments = data.get("segments", [])
    if not segments:
        return None

    # Format with timestamps - include all segments for Opus
    seg_text = ""
    for i, s in enumerate(segments):
        speaker = s.get("speaker", "UNKNOWN")
        text = s.get("text", "").strip()
        start = s.get("start", 0)
        mins, secs = int(start // 60), int(start % 60)

        gap_note = ""
        if i > 0:
            prev_end = segments[i - 1].get("end", segments[i - 1].get("start", 0))
            gap = start - prev_end
            if gap > 5:
                gap_note = f" [gap={gap:.0f}s]"

        seg_text += f"[{mins:02d}:{secs:02d}]{gap_note} {speaker}: {text}\n"

    # Step 3: Load existing profiles
    existing_profiles = load_profiles()
    profiles_text = json.dumps(existing_profiles, indent=2) if existing_profiles else "No existing profiles."

    # Step 4: Load config
    config_text = ""
    config_file = Path(config_path)
    if config_file.exists():
        config_text = config_file.read_text(encoding="utf-8")

    # Step 5: Build prompt
    prompt_template = prompt_path.read_text(encoding="utf-8")

    full_prompt = (
        f"{prompt_template}\n\n"
        f"---\n\n"
        f"Meeting folder: {folder_name}\n\n"
        f"Pre-computed fingerprint metrics:\n{fingerprint_text}\n\n"
        f"Existing speaker profiles:\n{profiles_text}\n\n"
        f"Known speakers config:\n{config_text}\n\n"
        f"Full transcript ({len(segments)} segments):\n{seg_text}"
    )

    if progress_callback:
        progress_callback("Analyzing with Opus...", 0.20)

    # Step 6: Call Opus
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", "opus"],
            input=full_prompt,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(Path(__file__).parent.parent.parent),
        )

        if progress_callback:
            progress_callback("Processing results...", 0.90)

        if result.returncode != 0:
            logger.warning(f"Opus deep ID CLI failed: {result.returncode}")
            if result.stderr:
                logger.debug(f"Claude CLI stderr: {result.stderr[:500]}")
            return None

        response = result.stdout.strip()
        first_brace = response.find("{")
        last_brace = response.rfind("}")
        if first_brace == -1 or last_brace == -1 or last_brace <= first_brace:
            logger.warning("Opus deep ID response contains no valid JSON")
            logger.debug(f"Raw response: {response[:500]}")
            return None

        json_str = response[first_brace:last_brace + 1]
        parsed = json.loads(json_str)

        # Step 7: Apply profile updates
        if parsed.get("profile_updates"):
            _apply_profile_updates(existing_profiles, parsed["profile_updates"], folder_name)
            save_profiles(existing_profiles)

        if progress_callback:
            progress_callback("Complete", 1.0)

        return parsed

    except json.JSONDecodeError as e:
        logger.warning(f"Opus deep ID returned invalid JSON: {e}")
        return None
    except FileNotFoundError:
        logger.warning("Claude CLI not found - Opus deep ID skipped")
        return None
    except subprocess.TimeoutExpired:
        logger.warning("Opus deep ID timed out (300s)")
        return None
    except Exception as e:
        logger.warning(f"Opus deep ID failed: {e}")
        return None


def _apply_profile_updates(profiles: dict, updates: dict, folder_name: str) -> None:
    """Merge Opus profile updates into existing profiles."""
    from datetime import date

    for name, update in updates.items():
        name_lower = name.lower()
        if name_lower not in profiles:
            profiles[name_lower] = {
                "meetings_analyzed": [],
                "last_updated": str(date.today()),
            }

        profile = profiles[name_lower]

        # Update WPS (running average)
        if "wps_this_meeting" in update:
            new_wps = update["wps_this_meeting"]
            if "wps" in profile:
                old_avg = profile["wps"].get("avg", new_wps)
                n = len(profile.get("meetings_analyzed", []))
                profile["wps"]["avg"] = round((old_avg * n + new_wps) / (n + 1), 2)
            else:
                profile["wps"] = {"avg": round(new_wps, 2)}

        # Append new vocab (deduplicated)
        if "new_vocab" in update:
            existing = set(profile.get("vocab_signals", []))
            existing.update(update["new_vocab"])
            profile["vocab_signals"] = sorted(existing)

        # Update notes
        if "notes" in update:
            profile["speaking_style"] = update["notes"]

        # Track meeting
        if folder_name not in profile.get("meetings_analyzed", []):
            profile.setdefault("meetings_analyzed", []).append(folder_name)

        profile["last_updated"] = str(date.today())


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
