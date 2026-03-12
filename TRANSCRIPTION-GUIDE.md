# AI-Powered Transcription with Speaker Identification

This guide explains how to use Claude Code (or any AI assistant) to turn WhisperSync's raw meeting transcripts into **named speaker transcripts** and **structured meeting minutes**.

Out of the box, WhisperSync labels speakers as `SPEAKER_00`, `SPEAKER_01`, etc. This guide shows how to resolve those to real names and generate actionable meeting notes.

---

## What You Get

1. **Speaker name resolution** — generic IDs mapped to real names (e.g., `SPEAKER_00` -> `Alice`)
2. **Readable transcript** — compact text format (~90% smaller than raw JSON)
3. **Meeting minutes** — action items, decisions, ticket candidates, key topics

---

## Prerequisites

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (Anthropic's AI coding assistant) — or any AI tool that can read files
- A completed meeting recording with `transcript.json` (WhisperSync creates this automatically)

---

## Quick Start (Claude Code)

After a meeting recording, open a terminal in your WhisperSync directory and run:

```
claude
```

Then paste this prompt:

```
Read the transcript at [path/to/transcript.json].

1. Scan the first ~20 segments for name callouts (e.g., "Hey David", "Thanks Sarah")
2. Present a speaker mapping for my confirmation (e.g., SPEAKER_00 -> David)
3. Save the confirmed mapping as a "speaker_map" key in transcript.json
4. Run: python -m whisper_sync.flatten [path/to/transcript.json]
5. Read the resulting transcript-readable.txt
6. Generate a minutes.md file next to the transcript with:
   - Action Items (with assignee)
   - Decisions Made
   - Key Topics (1-2 sentence summaries)
```

Replace `[path/to/transcript.json]` with the actual path, e.g.:
```
Documents\WhisperSync\transcriptions\2026\2026-03-11_standup\transcript.json
```

---

## Step-by-Step (Manual)

### 1. Find Your Transcript

Meeting recordings are saved to your transcriptions folder (default: `Documents\WhisperSync\transcriptions\`):

```
transcriptions/
  2026/
    2026-03-11_standup/
      recording.wav
      transcript.json     <-- this is what you need
```

### 2. Flatten the Transcript

The raw `transcript.json` is large (~850KB for a 30-min meeting). Convert it to a readable text format:

```powershell
.\whisper-env\Scripts\python.exe -m whisper_sync.flatten "path\to\transcript.json"
```

This creates `transcript-readable.txt` next to the JSON:
```
Duration: 12:34 | Speakers: SPEAKER_00, SPEAKER_01

[SPEAKER_00] Let's start with the status update.

[SPEAKER_01] Sure. The API integration is done, we're waiting on QA.
We should have results by Thursday.
```

### 3. Identify Speakers

Open `transcript-readable.txt` and scan for name mentions in the conversation. Common patterns:
- "Hey David, can you..."
- "Thanks, Sarah"
- "Dinesh is joining now"

### 4. Save Speaker Map

Add a `speaker_map` to your `transcript.json`:

```json
{
  "speaker_map": {
    "SPEAKER_00": "Alice",
    "SPEAKER_01": "Bob"
  },
  "segments": [...]
}
```

Then re-run flatten to get named speakers:
```powershell
.\whisper-env\Scripts\python.exe -m whisper_sync.flatten "path\to\transcript.json"
```

Now the output shows real names:
```
[Alice] Let's start with the status update.

[Bob] Sure. The API integration is done...
```

---

## Speaker Config (Optional)

For recurring meetings, create a file called `transcription-config.md` in your WhisperSync folder:

```markdown
# Transcription Config

## Known Speakers

| ID | Name | Voice Notes |
|----|------|-------------|
| alice | Alice Johnson | Team lead, American accent |
| bob | Bob Smith | Engineer, discusses backend |

## Meeting-to-Speaker Map

| Meeting Pattern | Likely Speakers | Typical Count |
|-----------------|-----------------|---------------|
| standup | Alice, Bob, Carol | 3 |
| sprint-planning | Alice, Bob, Carol, Dave | 4 |
```

When using Claude Code, reference this file in your prompt:
```
Cross-reference against the known speakers in transcription-config.md
```

This helps the AI narrow down speaker candidates, especially when name callouts are ambiguous.

---

## Claude Code Skill (Advanced)

If you use Claude Code regularly with a project repo, you can install WhisperSync's transcription as a reusable skill:

1. Create `.claude/skills/transcribe-recording/SKILL.md` in your project
2. Copy the prompt template from the Quick Start section above
3. Add your speaker config path and output preferences
4. Invoke with: `transcribe recording` in Claude Code

The [ic-product-mgmt repo](https://github.com/icustomer/ic-product-mgmt) has a full implementation of this as the `pm-transcribe-recording` skill that auto-identifies speakers, generates PM-oriented minutes with ticket candidates, and maintains a persistent speaker database.

---

## Tips

- **Best model for meetings**: Use `large-v3` for meeting transcription (set via tray menu). It's the most accurate for multi-speaker audio.
- **Speaker limits**: If you know the exact number of speakers, you can improve diarization accuracy by setting min/max speakers in the whisperX config.
- **Re-transcribe**: You can re-transcribe a meeting with a different model by running WhisperSync's transcribe function directly on the saved WAV file.
- **Token optimization**: The flattened text file is ~90% smaller than the JSON. Always use `transcript-readable.txt` when feeding into AI tools.
- **Logs**: Check `whisper_sync/logs/app/` for detailed transcription logs if something goes wrong.
