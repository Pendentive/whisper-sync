# Speaker Fingerprinting Pipeline - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a speaker fingerprinting system that extracts timing/vocabulary metrics from transcripts, uses Sonnet for matching with a 95% confidence gate, and auto-escalates to Opus for deep analysis with persistent profile updates.

**Architecture:** Python `compute_fingerprints()` extracts deterministic metrics from word-level timing data. Sonnet reads metrics + profiles for matching. If confidence < 95%, Opus reads full raw transcript for deep analysis, updates persistent profiles, and produces `analysis.md`. The "Deep Identify" button in the dialog triggers Opus directly.

**Tech Stack:** Python (metrics extraction), Claude CLI (sonnet/opus), JSON (profiles), WhisperSync pipeline integration

**Scope note:** This plan covers Tasks 1-3 (core functions + one-meeting validation). App integration (confidence gate in meeting_job, Deep Identify button rewiring) is deferred until profiles are validated on real data.

---

## File Structure

| File | Responsibility | Action |
|------|---------------|--------|
| `whisper_sync/speakers.py` | All speaker identification logic | Modify: add `compute_fingerprints()`, `opus_deep_identify()`, `load_profiles()`, `save_profiles()` |
| `whisper_sync/speaker_prompt.md` | Sonnet light prompt | Modify: add fingerprint matching instructions |
| `whisper_sync/speaker_prompt_deep.md` | Opus deep dive prompt | Rewrite: full raw transcript analysis with profile updates |
| `.whispersync/speaker-profiles.json` | Persistent speaker profiles | Created at runtime by Opus |

---

### Task 1: Add compute_fingerprints() to speakers.py

**Files:**
- Modify: `whisper_sync/speakers.py`

- [ ] **Step 1: Add compute_fingerprints() function**

Add this function after `distill_transcript()` and before `identify_speakers()` in `whisper_sync/speakers.py`:

```python
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

    # Name callout pattern
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
        # Word-level metrics
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

                # Inter-word gaps
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

        # Segment-level metrics
        seg_lengths = [len(seg.get("text", "").split()) for seg in segs]
        first_time = segs[0].get("start", 0)
        last_time = segs[-1].get("end", segs[-1].get("start", 0))

        # Name callouts given (this speaker says a name)
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

        # "sir" usage
        sir_count = sum(1 for w in all_words_lower if w == "sir")

        # Top non-filler, non-stop words for vocab signals
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
```

- [ ] **Step 2: Add profile load/save helpers**

Add these two functions after `compute_fingerprints()`:

```python
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
```

- [ ] **Step 3: Verify syntax**

Run: `./whisper-env/Scripts/python.exe -c "import py_compile; py_compile.compile('whisper_sync/speakers.py', doraise=True); print('OK')"`

Expected: `OK`

- [ ] **Step 4: Quick smoke test on real data**

Run: `./whisper-env/Scripts/python.exe -c "from whisper_sync.speakers import compute_fingerprints; import json; fp = compute_fingerprints('n:/Github/repos/icustomer/ic-product-mgmt/meetings/in-house/03-w5/0331_0619_Abhi-Sprint-Planning---other/transcript.json'); [print(f'{k}: {v[\"word_count\"]} words, WPS={v[\"wps\"][\"avg\"]}, fillers={v[\"filler_rate\"]}, sir={v[\"sir_count\"]}') for k,v in sorted(fp.items())]"`

Expected: metrics for SPEAKER_00 through SPEAKER_08

- [ ] **Step 5: Commit**

```bash
git add whisper_sync/speakers.py
git commit --author="Pendentive <pendentive.info@gmail.com>" -m "feat: add compute_fingerprints() and profile load/save (#114)"
```

---

### Task 2: Create Opus deep dive prompt

**Files:**
- Rewrite: `whisper_sync/speaker_prompt_deep.md`

- [ ] **Step 1: Replace speaker_prompt_deep.md**

Replace the entire contents of `whisper_sync/speaker_prompt_deep.md` with:

```markdown
You are performing deep speaker analysis on a meeting transcript using Opus-level reasoning. You will receive:
1. Pre-computed fingerprint metrics per SPEAKER_XX (speaking rate, filler patterns, vocabulary, timing)
2. Existing speaker profiles from previous meetings (if any)
3. A known speakers database with names and voice notes
4. The FULL transcript with word-level timestamps
5. The meeting folder name

## Your job

### Speaker Identification
Match each SPEAKER_XX to a real person by comparing their fingerprint metrics against known profiles:
- WPS (words per second): each person has a characteristic speaking pace
- Filler patterns: distinctive filler word preferences ("basically" vs "uh" vs "sir")
- Word confidence distribution: indicates enunciation clarity
- Vocabulary signals: topic ownership (who talks about what)
- Address patterns: "sir" usage, first names, formal vs informal
- Name callouts: direct evidence ("Hey Dinesh", "Thanks Vinod")
- Name references directed at a speaker: if others say a name and only one SPEAKER_XX responds, that name belongs to them

IMPORTANT: Accent notes in the config are human-authored reference. You are analyzing TEXT, not audio. Do not use accent as an identification signal. Use timing patterns, vocabulary, and address style instead.

### Meeting Boundary Detection
Look for transitions between separate meetings:
- Farewell exchanges followed by pauses (>15s gap) and new greetings
- Speaker composition changes (someone leaves, someone new joins)
- Topic resets unrelated to previous discussion

### Meeting Analysis
Provide observations about meeting dynamics:
- Who led, who contributed, who was passive
- Interruption patterns (overlapping timestamps indicate interruptions)
- Tone and intent (productive, tense, brainstorming, status update)
- Meeting type classification

### Profile Updates
Based on this meeting's data, suggest updates to speaker profiles:
- Refined WPS averages (if this meeting's data differs from profile)
- New vocabulary signals observed
- Updated filler patterns
- Any new behavioral observations

Output ONLY valid JSON - no markdown fences, no explanation:

{
  "speaker_map": {
    "SPEAKER_00": "Colby",
    "SPEAKER_01": "Dinesh"
  },
  "confidence": {
    "SPEAKER_00": 98,
    "SPEAKER_01": 92
  },
  "reasoning": {
    "SPEAKER_00": "WPS 3.9 matches Colby profile (3.9). Filler rate 2.8% matches (2.8%). Called 'Colby' at 02:15, 15:30. PM vocabulary (sprint, goal, ticket). Leads agenda.",
    "SPEAKER_01": "Uses 'sir' 4x (unique pattern). WPS 2.7 matches Dinesh profile. Backend/API vocabulary. Called 'Dinesh' by SPEAKER_05 at 05:32."
  },
  "profile_updates": {
    "colby": {
      "wps_this_meeting": 3.9,
      "new_vocab": ["oauth", "vercel"],
      "notes": "Led sprint planning, assigned 8 action items"
    },
    "dinesh": {
      "wps_this_meeting": 2.7,
      "new_vocab": ["provider", "standalone"],
      "notes": "Used 'sir' consistently when addressing leadership"
    }
  },
  "meeting_boundaries": [
    {
      "split_seconds": 1815,
      "evidence": "Goodbye exchange at 30:10-30:13. 29s gap. New greeting at 30:42."
    }
  ],
  "analysis": {
    "dynamics": "Colby led agenda. Abhi directed product decisions.",
    "interruption_patterns": "Abhi interrupted 3x to redirect. Dinesh waited for explicit invitation.",
    "tone": "Productive and collaborative.",
    "meeting_type": "Sprint planning"
  },
  "config_updates": {
    "new_voice_notes": {},
    "new_speakers": [],
    "flagged_notes": []
  }
}

Confidence is 0-100 (not high/medium/low). 95+ means very confident. Below 80 means uncertain.

If any field is empty, use the appropriate empty value: [] for arrays, {} for objects.
```

- [ ] **Step 2: Commit**

```bash
git add whisper_sync/speaker_prompt_deep.md
git commit --author="Pendentive <pendentive.info@gmail.com>" -m "feat: rewrite deep prompt for Opus fingerprint analysis (#114)"
```

---

### Task 3: Add opus_deep_identify() function

**Files:**
- Modify: `whisper_sync/speakers.py`

- [ ] **Step 1: Replace deep_identify_speakers() with opus_deep_identify()**

Find `deep_identify_speakers()` in `whisper_sync/speakers.py` (starts around line 178). Replace the ENTIRE function with:

```python
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
    # (Opus can handle larger context than Sonnet)
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
            timeout=300,  # 5 min for Opus on large transcripts
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
```

- [ ] **Step 2: Verify syntax**

Run: `./whisper-env/Scripts/python.exe -c "import py_compile; py_compile.compile('whisper_sync/speakers.py', doraise=True); print('OK')"`

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add whisper_sync/speakers.py
git commit --author="Pendentive <pendentive.info@gmail.com>" -m "feat: add opus_deep_identify() with profile updates (#114)"
```

---

### Task 4: Validate on one meeting

This task runs the full pipeline manually on `0331_0619_Abhi-Sprint-Planning---other` to validate fingerprints and Opus analysis.

**Files:**
- No code changes. Uses the functions built in Tasks 1-3.

- [ ] **Step 1: Run compute_fingerprints and review output**

```bash
cd n:/Github/repos/pendentive/whisper-sync
./whisper-env/Scripts/python.exe -c "
from whisper_sync.speakers import compute_fingerprints
import json

fp = compute_fingerprints('n:/Github/repos/icustomer/ic-product-mgmt/meetings/in-house/03-w5/0331_0619_Abhi-Sprint-Planning---other/transcript.json')
print(json.dumps(fp, indent=2, default=str))
"
```

Review: Are the metrics sensible? Do different speakers have distinguishable fingerprints?

- [ ] **Step 2: Run Opus deep identify**

```bash
cd n:/Github/repos/pendentive/whisper-sync
./whisper-env/Scripts/python.exe -c "
from whisper_sync.speakers import opus_deep_identify, get_config_path
import json

result = opus_deep_identify(
    'n:/Github/repos/icustomer/ic-product-mgmt/meetings/in-house/03-w5/0331_0619_Abhi-Sprint-Planning---other/transcript.json',
    get_config_path(),
    '0331_0619_Abhi-Sprint-Planning---other',
)
if result:
    print(json.dumps(result, indent=2, default=str))
else:
    print('FAILED')
"
```

Review:
- Are speaker identifications correct?
- Are confidence scores reasonable?
- Were profiles created in `.whispersync/speaker-profiles.json`?
- Does the analysis section capture useful meeting dynamics?

- [ ] **Step 3: Review generated profiles**

```bash
cat "n:/Github/repos/icustomer/ic-product-mgmt/meetings/in-house/.whispersync/speaker-profiles.json"
```

Review: Do the profiles look like realistic fingerprints? Are WPS, filler rates, and vocab signals distinguishing?

- [ ] **Step 4: If validation passes, write speaker_map, flatten, and regenerate minutes**

Only after reviewing the Opus output:

```bash
cd n:/Github/repos/pendentive/whisper-sync
./whisper-env/Scripts/python.exe -c "
from whisper_sync.speakers import write_speaker_map
# Use the speaker_map from the Opus result (copy from Step 2 output)
write_speaker_map(
    'n:/Github/repos/icustomer/ic-product-mgmt/meetings/in-house/03-w5/0331_0619_Abhi-Sprint-Planning---other/transcript.json',
    {'SPEAKER_00': 'Colby', 'SPEAKER_05': 'Abhi', 'SPEAKER_08': 'Vinod', 'SPEAKER_07': 'Dinesh', 'SPEAKER_06': 'Keerthana', 'SPEAKER_01': 'Unknown', 'SPEAKER_02': 'Unknown', 'SPEAKER_03': 'Unknown', 'SPEAKER_04': 'Unknown'}
)
from whisper_sync.flatten import flatten
flatten('n:/Github/repos/icustomer/ic-product-mgmt/meetings/in-house/03-w5/0331_0619_Abhi-Sprint-Planning---other/transcript.json')
print('Done')
"
```

Then regenerate minutes via Claude CLI (same approach as existing `_generate_minutes`).
