# WhisperSync

Hotkey-triggered audio capture and transcription that runs 100% locally on your GPU. Two modes:

- **Dictation** — press a hotkey, speak, press again. Your words are transcribed and pasted into whatever app is focused. Sub-second turnaround with a fast model.
- **Meeting recording** — press a hotkey to record your mic + system audio (what you hear). Press again to stop, name the meeting, and get a full transcript with speaker labels saved to disk.

Runs as a system tray icon on Windows. No cloud services, no subscriptions, no data leaves your machine.

---

## How It Works

| Component | Purpose |
|-----------|---------|
| [WhisperX](https://github.com/m-bain/whisperX) (faster-whisper) | Speech-to-text transcription |
| wav2vec2 alignment model | Word-level timing accuracy |
| [pyannote](https://github.com/pyannote/pyannote-audio) speaker diarization | Identifies who said what (meeting mode) |
| WASAPI loopback | Captures system audio without virtual cables |
| pystray | System tray icon and menu |

Models download once on first run and are cached locally in a `models/` folder. Subsequent launches load from cache — works fully offline.

---

## System Requirements

| Requirement | Details |
|-------------|---------|
| **OS** | Windows 10 or 11 |
| **Python** | 3.10 or newer (3.13 recommended) |
| **GPU** | NVIDIA with CUDA support (RTX 20/30/40/50 series, GTX 10-series) |
| **VRAM** | 2 GB minimum (tiny/base models), 4 GB+ recommended, 8 GB+ for large-v3 |
| **Disk** | ~200 MB base install + model sizes (see table below) |
| **HF Account** | Free [Hugging Face](https://huggingface.co) account (required for meeting mode speaker diarization) |

---

## Installation

1. **Install Python** if you don't have it: [python.org/downloads](https://www.python.org/downloads/). Check "Add to PATH" during install.

2. **Extract the zip** to wherever you want (e.g., `C:\Tools\whisper-sync\`).

3. **Open PowerShell** in the extracted folder (right-click → "Open in Terminal" or Shift+right-click → "Open PowerShell window here").

4. **Run the installer:**
   ```powershell
   powershell -ExecutionPolicy Bypass -File install.ps1
   ```

5. **Follow the prompts.** The installer will:
   - Detect your GPU and pick the right CUDA version
   - Create a Python virtual environment
   - Install all dependencies
   - Download base transcription models (~225 MB)
   - Optionally create a Windows startup shortcut

6. **Launch:**
   ```powershell
   powershell -ExecutionPolicy Bypass -File start.ps1
   ```
   Or just double-click the startup shortcut if you created one.

---

## Hugging Face Token Setup (Meeting Mode)

Speaker diarization (identifying who said what) requires a free Hugging Face token. Skip this if you only need dictation mode.

1. **Create an account** at [huggingface.co/join](https://huggingface.co/join)

2. **Accept the model license terms** — visit both pages and click "Agree":
   - [pyannote/segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0)
   - [pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1)

3. **Generate a token** at [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens)
   - Click "New token", name it anything, select **Read** access

4. **Save the token** to a file on your machine:
   ```powershell
   # Create the directory and save your token
   mkdir "$env:USERPROFILE\.huggingface" -Force
   "hf_YOUR_TOKEN_HERE" | Out-File -Encoding ASCII "$env:USERPROFILE\.huggingface\token" -NoNewline
   ```
   Replace `hf_YOUR_TOKEN_HERE` with your actual token.

---

## Usage

### Tray Icon

After launch, a small circle appears in your system tray (bottom-right near the clock). The color tells you the current state:

| Color | State |
|-------|-------|
| Gray | Idle — ready to record |
| Blue | Dictating — recording your voice |
| Red | Meeting — recording mic + system audio |
| Orange | Saving — writing audio to disk |
| Yellow | Transcribing — running the AI model |
| Green | Done — transcription complete |
| Magenta | Error — check logs |

### Dictation Mode

1. Press **Ctrl+Shift+Space** (default hotkey)
2. Speak naturally
3. Press the hotkey again (or click the tray icon)
4. Text is transcribed and auto-pasted into whatever app is focused

**To discard:** Left-click the tray icon while dictating (blue) to cancel without transcribing.

### Meeting Mode

1. Press **Ctrl+Shift+M** (default hotkey)
2. Your microphone and system audio (what comes out of your speakers) are recorded simultaneously
3. Press the hotkey again to stop
4. A popup asks you to name the meeting (or leave blank for a default name)
5. Audio is saved as WAV, then transcribed with speaker labels
6. Output goes to your configured transcriptions folder as:
   - `recording.wav` — the audio file
   - `transcript.json` — detailed transcript with timestamps and speaker IDs

### Click Actions

| Click | Default Action |
|-------|---------------|
| Left-click | Toggle meeting recording (or discard if dictating) |
| Middle-click | Toggle dictation |
| Right-click | Open settings menu |

Click actions are configurable in the Settings menu.

---

## Settings

All settings are accessible via right-click on the tray icon → **Settings**. Changes are saved automatically.

| Setting | Description |
|---------|-------------|
| **Dictation Hotkey** | Keyboard shortcut to start/stop dictation |
| **Meeting Hotkey** | Keyboard shortcut to start/stop meeting recording |
| **Paste Method** | `clipboard` (Ctrl+V) or `keystrokes` (simulated typing) |
| **Left Click** | What left-clicking the tray icon does (meeting/dictation/none) |
| **Middle Click** | What middle-clicking does (meeting/dictation/none) |
| **Dictation Model** | AI model for dictation (smaller = faster, see table) |
| **Meeting Model** | AI model for meeting transcription (larger = more accurate) |

**Recommended setup for speed:** Use `tiny` or `base` for dictation (instant paste) and `large-v3` for meetings (best accuracy).

### Audio Devices

By default, WhisperSync uses your system's default microphone and speakers. You can override this in the tray menu:

- **Always Use System Devices** — checked by default, follows Windows audio settings
- Uncheck to manually select specific mic/speaker devices
- **Device Filter** — filters the device list by audio API (WASAPI recommended on Windows)

---

## Model Comparison

All models run locally on your GPU. Larger models are more accurate but slower and use more VRAM.

### Dictation Speed (45 seconds of speech)

How long you wait after pressing the hotkey to stop dictation until the text appears:

| Model | Size | Wait Time | Feels Like | Quality | Best For |
|-------|:---:|:---:|:---:|:---:|----------|
| **tiny** | ~75 MB | ~0.3s | Instant | Basic | Quick notes, low-accuracy OK |
| **base** | ~150 MB | ~0.3s | Instant | Good | Everyday dictation (recommended) |
| **small** | ~500 MB | ~0.5s | Instant | Better | Balanced speed/quality |
| **medium** | ~1.5 GB | ~0.7s | Instant | Great | When accuracy matters |
| **large-v3** | ~3 GB | ~1.2s | Snappy | Best | Maximum accuracy dictation |

### Meeting Speed (30-minute recording)

How long you wait after stopping a meeting recording until the transcript is ready:

| Model | Size | Wait Time | Speed vs Audio | Quality | Best For |
|-------|:---:|:---:|:---:|:---:|----------|
| **tiny** | ~75 MB | ~28s | 65x realtime | Basic | Quick draft, re-transcribe later |
| **base** | ~150 MB | ~28s | 65x realtime | Good | Fast turnaround meetings |
| **small** | ~500 MB | ~30s | 60x realtime | Better | Balanced |
| **medium** | ~1.5 GB | ~33s | 55x realtime | Great | Important meetings |
| **large-v3** | ~3 GB | ~39s | 46x realtime | Best | High-accuracy transcripts (recommended) |

*Benchmarked on NVIDIA RTX 3090 with float16 compute. Your speeds will vary by GPU. Run `python -m whisper_sync.benchmark` to test on your hardware.*

### GPU & VRAM

| Model | VRAM Required |
|-------|:---:|
| tiny / base | ~1 GB |
| small | ~2 GB |
| medium | ~4 GB |
| large-v3 | ~8 GB |

WhisperSync uses your NVIDIA GPU via CUDA for fast transcription. Without a GPU, it falls back to CPU mode which is 5-10x slower.

- **float16** compute (default): Full GPU speed, requires NVIDIA GPU
- **int8** compute: Automatic fallback when running on CPU

You can check your GPU status in the tray menu under **Settings** — the bottom of the menu shows your detected GPU, model status, and whether CUDA is active.

---

## Transcription Output

### Dictation
Text is pasted directly into the focused application. A history of all dictations is saved to daily log files at `whisper_sync/logs/data/dictation/YYYY-MM-DD.md` for recovery and review.

### Meeting
Files are saved to your transcriptions folder (default: `Documents\WhisperSync\transcriptions\`), organized by year and meeting name:

```
transcriptions/
  2026/
    2026-03-11_standup/
      recording.wav         # Audio file (mic + system audio as stereo)
      transcript.json       # Detailed JSON with timestamps + speaker IDs
```

The JSON transcript includes per-segment data:
```json
{
  "segments": [
    {
      "speaker": "SPEAKER_00",
      "start": 0.5,
      "end": 3.2,
      "text": "Let's start with the status update."
    }
  ]
}
```

### Changing Save Locations

Meeting recordings save to `Documents\WhisperSync\transcriptions\` by default. To change this:

1. Open `whisper_sync\config.json` in a text editor (created after first settings change)
2. Add or edit the `output_dir` key:
   ```json
   {
     "output_dir": "D:\\Meetings\\transcriptions"
   }
   ```
3. Use an **absolute path** (e.g., `D:\Meetings\transcriptions`) to save anywhere, or a **relative path** (e.g., `my-transcriptions`) which resolves relative to the WhisperSync install folder
4. Restart WhisperSync

You can also open the current output folder at any time via right-click tray menu -> **Open Output Folder**.

---

## AI-Powered Speaker Identification & Meeting Minutes

Out of the box, WhisperSync labels speakers as `SPEAKER_00`, `SPEAKER_01`, etc. With [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (Anthropic's AI coding assistant) or any AI tool that can read files, you can get:

- **Automatic speaker name resolution** — AI analyzes the transcript for name callouts ("Hey David", "Thanks Sarah") and maps generic speaker IDs to real names
- **Readable transcript** — compact text format (~90% smaller than raw JSON)
- **Meeting minutes** — action items, decisions, key topics
- **Persistent speaker memory** — once identified, speakers are remembered across future meetings

### Quick Start (Claude Code)

After a meeting recording, open a terminal in your WhisperSync directory and run:

```
claude
```

Then paste this prompt (replace the path with your actual transcript):

```
Read the transcript at Documents\WhisperSync\transcriptions\2026\2026-03-11_standup\transcript.json.

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

That's it. Claude identifies speakers, saves the mapping, flattens the transcript, and generates structured minutes — all in one shot.

### Step-by-Step (Manual)

If you prefer to do it without Claude Code:

**1. Find your transcript** — meeting recordings are in your transcriptions folder (default: `Documents\WhisperSync\transcriptions\`):
```
transcriptions/2026/2026-03-11_standup/
  recording.wav
  transcript.json     <-- this is what you need
```

**2. Flatten the transcript** — the raw JSON is large (~850KB for a 30-min meeting). Convert it:
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

**3. Identify speakers** — scan the readable transcript for name mentions:
- "Hey David, can you..."
- "Thanks, Sarah"
- "Dinesh is joining now"

**4. Save speaker map** — add a `speaker_map` to your `transcript.json`:
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

### Speaker Config (Recurring Meetings)

For recurring meetings with the same people, create a `transcription-config.md` file in your WhisperSync folder:

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

When using Claude Code, add to your prompt:
```
Cross-reference against the known speakers in transcription-config.md
```

This helps narrow down speaker candidates, especially when name callouts are ambiguous.

### Claude Code Skill (Copy-Paste Ready)

If you use Claude Code regularly, you can install this as a reusable skill so you just say `transcribe recording` and the entire workflow runs automatically — speaker identification, transcript flattening, and structured minutes generation.

**Setup:**
1. Create the file `.claude/skills/transcribe-recording/SKILL.md` in any project directory
2. Paste the full skill content below
3. From then on, just say `transcribe recording` in Claude Code

**Full skill file** — copy everything below into `.claude/skills/transcribe-recording/SKILL.md`:

````markdown
---
name: transcribe-recording
description: Transcribe a WhisperSync meeting recording with speaker diarization, generate meeting minutes with action items and decisions. Use when hearing "transcribe recording", "process the recording", "transcribe the meeting", or "what did we say in the recording?".
---

# Transcribe Recording

Transcribes a WhisperSync meeting recording with speaker diarization, identifies speakers by name, and generates structured meeting minutes.

## Arguments
$ARGUMENTS
(Optional: path to a specific transcript.json or recording.wav. If blank, scans the transcriptions folder for the most recent recording.)

## Prerequisites

1. **WhisperSync venv** — `whisper-env\Scripts\python.exe -c "import whisperx; print('OK')"`
2. **Hugging Face token** — required for speaker diarization. Check `~/.huggingface/token`.

## Steps

1. **Find the recording**:
   - If argument provided, use that path.
   - If argument is a `.json` file, use its parent folder as the meeting folder. Skip to Step 3.
   - When scanning without argument, find the most recent `transcript.json` in the transcriptions folder:
     ```bash
     find ~/Documents/WhisperSync/transcriptions -name "transcript.json" -printf '%T@ %p\n' | sort -rn | head -1
     ```
   - Show the filename and ask user to confirm: "Found `{filename}` ({date}). Process this?"

2. **Transcribe** (if only recording.wav exists, no transcript.json yet):
   ```bash
   whisper-env/Scripts/python.exe -c "
   from whisper_sync.transcribe import transcribe
   transcribe('{wav_path}', diarize=True)
   "
   ```

3. **Auto-identify speakers**:
   - If `transcription-config.md` exists in the WhisperSync directory, load it for known speakers and meeting-to-speaker mappings.
   - Parse the diarized transcript for name callouts in the first ~20 segments: patterns like "Hey {Name}", "{Name}, can you", "Thanks {Name}", "{Name} is joining".
   - Match callouts against known speakers from the config before falling back to generic labels.
   - Present the auto-mapping for user confirmation:
     ```
     Speaker mapping:
       SPEAKER_00 → Alice (matched: "Hey Alice" spoken by different speaker)
       SPEAKER_01 → Bob (matched: called by name 3x)
     Confirm or adjust?
     ```
   - Apply confirmed names to the transcript.
   - **Update speaker config**: After confirmation, update `transcription-config.md`:
     - Add any new speakers to the Known Speakers table.
     - Update Meeting-to-Speaker Map if new participants appeared.

4. **Persist speaker map to transcript.json**:
   - Add a top-level `speaker_map` key with the confirmed mapping:
     ```json
     {
       "speaker_map": {
         "SPEAKER_00": "Alice",
         "SPEAKER_01": "Bob"
       },
       "segments": [...]
     }
     ```
   - Write back to the same file. Do NOT modify segment data — the map is additive only.

5. **Flatten transcript** (token optimization):
   ```bash
   whisper-env/Scripts/python.exe -m whisper_sync.flatten "{meeting_folder}/transcript.json"
   ```
   - Produces `transcript-readable.txt` (~15KB vs ~850KB JSON) with speaker names resolved.

6. **Generate minutes.md**:
   - Read `transcript-readable.txt` (NOT the full JSON — saves ~98% tokens).
   - Generate minutes.md using this template:

     ```markdown
     # Meeting Minutes — {meeting name from folder}
     > Date: {YYYY-MM-DD} | Duration: {MM:SS} | Speakers: {resolved names}
     > Source: local recording via WhisperSync
     > Transcript: transcript.json

     ## Summary

     ### Action Items
     - [ ] **{Assignee}**: {specific action with enough detail to execute}

     ### Decisions Made
     - {Decision with specifics — field names, values, approach chosen}

     ### Key Topics
     - {Topic}: {1-2 sentence summary}

     ---

     ## Detailed Notes

     ### {Topic heading}
     {Structured breakdown with speaker attribution and selective quotes}
     ```

   - Write minutes.md to the meeting folder (next to transcript.json).

7. **Inform user**:
   "Minutes saved to `{meeting_folder}/minutes.md`. Transcript flattened to `transcript-readable.txt`."

## Notes

- Each meeting folder contains: `recording.wav`, `transcript.json`, `minutes.md`, and the transient `transcript-readable.txt`.
- The original recording is never modified or moved.
- WhisperSync saves meetings to `Documents\WhisperSync\transcriptions\{year}\{date}_{name}\` by default.
````

### Tips

- **Best model for meetings**: Use `large-v3` (set via tray menu -> Settings -> Meeting Model). Most accurate for multi-speaker audio.
- **Re-transcribe**: Changed your mind on the model? Re-run transcription on the saved WAV — the original recording is always preserved.
- **Token optimization**: Always use `transcript-readable.txt` (not the raw JSON) when feeding into AI tools — it's ~90% smaller.
- **Speaker limits**: If you know the exact speaker count, WhisperX can use that constraint for better diarization accuracy.

---

## Benchmarking

To compare model speeds on your specific GPU, run:

```powershell
# From the WhisperSync directory:
.\whisper-env\Scripts\python.exe -m whisper_sync.benchmark
```

Or with a specific WAV file:
```powershell
.\whisper-env\Scripts\python.exe -m whisper_sync.benchmark "path\to\recording.wav"
```

This tests all downloaded models with both meeting mode (30 min, full pipeline) and dictation mode (45 sec, fast path).

---

## Troubleshooting

### "CUDA not available" / GPU not detected
- Ensure NVIDIA drivers are up to date: [nvidia.com/drivers](https://www.nvidia.com/drivers)
- Run `nvidia-smi` in a terminal — if this fails, your drivers need updating
- The installer auto-detects your GPU and installs the matching CUDA toolkit

### No audio captured
- Check Windows Sound Settings → ensure your microphone is set as default
- For meeting mode: system audio capture requires WASAPI (Windows default)
- Try toggling "Always Use System Devices" off, then manually selecting your devices

### Slow transcription
- Use a smaller model for dictation (`tiny` or `base`)
- Check Task Manager → GPU usage should spike during transcription
- If GPU shows 0% during transcription, CUDA may not be working (see above)

### Hotkey not working
- Some apps intercept global hotkeys. Try a different hotkey combination via Settings
- The `keyboard` module may need admin privileges on some systems. Try running as Administrator.

### Hugging Face / diarization errors
- Verify `~/.huggingface/token` exists and contains your token
- **"Diarization model access denied"** — you must accept the license terms on BOTH pages:
  - [pyannote/segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0) — click "Agree"
  - [pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1) — click "Agree"
- WhisperSync will show a popup with these exact URLs if it detects the access error
- The diarization model downloads on first meeting transcription (~500 MB)

### Logs
Check the `logs/app/` folder inside the WhisperSync directory for dated log files:
```
whisper_sync/logs/app/whisper-sync-2026-03-11.log
```
Dictation history is saved to `logs/data/dictation/YYYY-MM-DD.md`.

---

## Updating

To update to a newer version:
1. Download the new zip
2. Extract over your existing installation
3. Your settings (`config.json`), models (`models/`), and logs (`logs/`) are preserved
4. Only re-run `install.ps1` if the release notes mention dependency changes

---

## Architecture (For the Curious)

```
whisper_sync/
  __main__.py           # Tray icon, hotkey listener, recording lifecycle
  capture.py            # Audio recording (WASAPI loopback for system audio)
  transcribe.py         # WhisperX transcription engine, model caching
  config.py             # Config loader (defaults + user overrides)
  paste.py              # Clipboard and keystroke paste methods
  icons.py              # Tray icon colors (generated, no external assets)
  model_status.py       # Model download management
  flatten.py            # Convert JSON transcript to readable text
  paths.py              # Path resolution (standalone vs development mode)
  logger.py             # File-based logging
  benchmark.py          # Model speed comparison utility
  dictation_log.py      # Daily dictation history (append-only markdown)
  streaming_wav.py      # Crash-safe streaming WAV writer for meetings
  crash_diagnostics.py  # Exception hooks + Windows Event Log crash detection
```
