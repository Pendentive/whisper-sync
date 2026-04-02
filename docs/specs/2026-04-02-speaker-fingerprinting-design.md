# Speaker Fingerprinting Pipeline

> Issue: #114
> Component: speaker-id
> Date: 2026-04-02

## Problem

Speaker identification relies on name callouts and a simple prompt. When callouts are absent or ambiguous, identification fails or produces low confidence. There is no persistent model of how each speaker sounds (in text terms: their speaking rate, filler patterns, vocabulary, address style). Meetings that run together are not detected. Meeting dynamics (interruption patterns, tone, intent) are not captured.

## Architecture

Two paths with a confidence gate:

```
Every meeting:
  transcript.json
    |
    +-> Python: compute_fingerprints() -> raw metrics per SPEAKER_XX
    +-> transcript-readable.txt (already exists, no speaker names)
    |
    v
  Sonnet: reads metrics + speaker-profiles.json + readable + config
    -> assigns speaker_map with confidence per speaker
    |
    +-> ALL speakers >= 95% confidence
    |     -> write speaker_map, flatten, generate minutes (done)
    |
    +-> ANY speaker < 95% confidence (auto-escalate)
          |
          v
        Opus: reads full raw transcript.json (word-level timing)
          -> writes/updates speaker-profiles.json
          -> writes speaker_map
          -> produces analysis.md (meeting dynamics, tone, boundaries)
          -> flatten, generate minutes
```

The "Deep Identify" button in the dialog triggers Opus directly (skips the gate).

## Components

### 1. Python Fingerprint Extraction

New function `compute_fingerprints(json_path)` in `speakers.py`.

Reads transcript.json word-level data and computes per-speaker:

| Metric | Source | What it reveals |
|--------|--------|-----------------|
| Words per second (WPS) | word start/end times | Speaking pace |
| Inter-word gap distribution | gap between consecutive word.end and next word.start | Pause patterns, hesitation |
| Word confidence distribution | word.score values | Enunciation clarity, accent strength |
| Filler word rate | count of uh/um/like/so/yeah/basically/actually/okay | Speech fluency |
| Top filler words | ranked filler list | Personal filler preference |
| Segment length distribution | words per segment avg/median/max | Monologue vs short response tendency |
| First/last appearance | segment timestamps | When they join/leave |
| Name callouts given | regex scan for "{Name}, can you" etc. | Who they address |
| Name callouts received | other speakers mentioning names directed at them | Who they are |
| Address patterns | "sir" usage, first name vs formal | Formality level |

Returns a dict keyed by SPEAKER_XX with all metrics.

This function is pure Python, deterministic, free. No LLM call.

### 2. Speaker Profiles

File: `.whispersync/speaker-profiles.json`

```json
{
  "colby": {
    "wps": {"avg": 3.9, "median": 3.5, "std": 0.8},
    "confidence_avg": 0.795,
    "filler_rate": 0.028,
    "top_fillers": ["so", "yeah", "like"],
    "avg_words_per_segment": 12.1,
    "address_pattern": "uses first names directly",
    "vocab_signals": ["sprint", "goal", "mcp", "ticket", "schedule"],
    "speaking_style": "Fast, clear enunciation, low fillers, PM vocabulary, leads agendas",
    "meetings_analyzed": ["0331_0619", "0330_0702"],
    "last_updated": "2026-04-02"
  },
  "abhi": {
    "wps": {"avg": 2.8, "median": 2.6, "std": 0.7},
    "confidence_avg": 0.774,
    "filler_rate": 0.062,
    "top_fillers": ["so", "like", "basically", "yeah", "actually"],
    "avg_words_per_segment": 12.2,
    "address_pattern": "addresses everyone by first name, delegates frequently",
    "vocab_signals": ["audience", "loop", "refine", "customer", "feedback"],
    "speaking_style": "Moderate pace, higher filler rate, 'basically' is distinctive, CEO directive tone",
    "meetings_analyzed": ["0331_0619"],
    "last_updated": "2026-04-02"
  }
}
```

**Rules:**
- Only Opus writes/updates profiles (from deep dive analysis)
- Sonnet reads profiles for matching but never modifies them
- Profiles accumulate across meetings (metrics averaged over multiple observations)
- Each profile tracks which meetings contributed to it

### 3. Sonnet Identification (Light Mode)

Updated `identify_speakers()`:

1. `compute_fingerprints(transcript.json)` -> raw metrics per SPEAKER_XX
2. Load `speaker-profiles.json` and `transcription-config.md`
3. Load `transcript-readable.txt` (for boundary detection and context)
4. Send to Sonnet: metrics + profiles + config + readable text
5. Sonnet matches SPEAKER_XX fingerprints against known profiles
6. Returns speaker_map with per-speaker confidence (0-100)
7. If all >= 95: accept
8. If any < 95: auto-escalate to Opus

The Sonnet prompt receives pre-computed metrics (not raw word data) so it focuses on matching, not arithmetic. It also detects meeting boundaries from the readable text.

### 4. Opus Deep Dive

New function `opus_deep_identify(json_path, config_path, folder_name)`:

1. Reads full `transcript.json` including word-level timing
2. Reads existing `speaker-profiles.json`
3. Reads `transcription-config.md`
4. Sends everything to Opus via `claude -p --model opus`
5. Opus performs thorough analysis:
   - Word-level timing pattern matching against profiles
   - Interruption detection (who talks over whom)
   - Name callout scanning with timestamps
   - Topic ownership tracking
   - Meeting boundary detection with gap analysis
   - Tone/intent observations
   - New speaker detection (names mentioned but directed at only one SPEAKER_XX)

Returns:
```json
{
  "speaker_map": {"SPEAKER_00": "Colby"},
  "confidence": {"SPEAKER_00": 98},
  "reasoning": {"SPEAKER_00": "WPS 3.9 matches Colby profile (3.9). Filler rate 2.8% matches (2.8%). Called 'Colby' at 02:15, 15:30. PM vocabulary consistent."},
  "profile_updates": {
    "colby": {"wps": {"avg": 3.85}, "new_vocab": ["oauth"]},
    "abhi": {"filler_rate": 0.065}
  },
  "meeting_boundaries": [{"split_seconds": 1815, "evidence": "..."}],
  "analysis": {
    "dynamics": "Colby led agenda. Abhi directed product decisions. Vinod managed engineering scope.",
    "interruption_patterns": "Abhi frequently interrupted to redirect. Dinesh waited for explicit invitation to speak.",
    "tone": "Productive and collaborative. Slight tension around timeline for CLI integration.",
    "meeting_type": "Sprint planning with product direction overlay"
  }
}
```

### 5. analysis.md (Opus-produced)

Third file per meeting, only created when Opus deep dive runs:

```markdown
# Meeting Analysis - {folder_name}
> Generated by Opus deep dive | Date: YYYY-MM-DD

## Speaker Dynamics
{who led, who contributed, who was passive}

## Interruption Patterns
{who interrupted whom, frequency}

## Tone and Intent
{collaborative/tense/productive, key moments}

## Meeting Boundaries
{if multiple meetings detected, split points with evidence}

## Profile Updates
{what was learned about each speaker from this meeting}

## Meeting Type
{sprint planning / 1-on-1 / architecture review / etc.}
```

This file can be formatted and sent to Slack alongside minutes.

### 6. Confidence Gate Integration

In `meeting_job.py` `step_speaker_id`:

```
1. Python: compute_fingerprints()
2. Sonnet: identify with metrics + profiles
3. Check confidence:
   a. All >= 95%: show confirmation dialog, proceed
   b. Any < 95%: toast "Escalating to Opus for deeper analysis"
      -> Opus: deep identify with full transcript
      -> Update profiles
      -> Write analysis.md
      -> Show confirmation dialog with Opus results
4. User confirms/edits
5. Write speaker_map, flatten, minutes
```

The "Deep Identify" button in the dialog bypasses step 2-3 and goes straight to Opus.

## Files Modified/Created

| File | Action | Purpose |
|------|--------|---------|
| `speakers.py` | Modify | Add `compute_fingerprints()`, `opus_deep_identify()`, update `identify_speakers()` |
| `speaker_prompt.md` | Modify | Add fingerprint matching instructions for Sonnet |
| `speaker_prompt_deep.md` | Modify | Full Opus deep dive prompt with profile updates |
| `.whispersync/speaker-profiles.json` | Create (runtime) | Persistent speaker profiles, Opus-managed |
| `meeting_job.py` | Modify | Confidence gate, auto-escalation |
| `__main__.py` | Modify | Deep Identify button triggers Opus path |
| `.claude/rules/audio-pipeline.md` | Modify | Document fingerprinting pipeline |

## What Doesn't Change

- `transcript.json` segment structure
- `write_speaker_map()` (still additive)
- `flatten()` / minutes pipeline
- `split_meeting()` function
- `_ask_speaker_confirmation()` dialog (just the Deep Identify wiring changes)
- `transcription-config.md` (human-edited, read-only for this system)

## Iteration Plan

1. First: run fingerprint extraction + Opus on ONE meeting (0331_0619) to validate profiles
2. Review profiles, tune metrics
3. Run on remaining meetings from last week
4. Integrate into WhisperSync app as the new deep identify implementation
