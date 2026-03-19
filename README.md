# WhisperSync

Hotkey-triggered audio capture and transcription that runs 100% locally on your GPU. Two modes:

- **Dictation** — press a hotkey, speak, press again. Your words are transcribed and pasted into whatever app is focused. Sub-second turnaround with a fast model.
- **Meeting recording** — press a hotkey to record your mic + system audio (what you hear). Press again to stop, name the meeting, and get a full transcript with speaker labels saved to disk.

Runs as a system tray icon on Windows.

**What runs where:**
- **Dictation transcription** — 100% local GPU. No network calls.
- **Meeting transcription + speaker identification** — 100% local GPU. No network calls.
- **Meeting minutes / action items** — requires an LLM (e.g., Claude). The transcript stays on your machine; only the text you choose to send leaves it.

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

---

# Technical Reference (Developer & AI Assistant Guide)

Everything below is for developers modifying this app or for an AI assistant (like Claude) troubleshooting it on behalf of a user. If you're just using WhisperSync, you can stop reading here.

---

## Complete File Reference

### Python Files (whisper_sync/)

| File | Purpose | Key Classes/Functions |
|------|---------|----------------------|
| `__main__.py` | **App entry point.** Tray icon, hotkey listener, recording lifecycle, crash recovery. | `WhisperSync` class — `run()`, `_on_hotkey()`, `_start_dictation()`, `_start_meeting()`, `_do_transcribe()`, `_paste_result()`, `_recover_from_crash()` |
| `capture.py` | **Audio recording.** Multi-stream mic + speaker loopback via WASAPI. | `AudioCapture` class — `start()`, `stop()`, `_audio_callback()` |
| `transcribe.py` | **WhisperX engine.** Model loading, transcription, alignment, diarization. Two paths: fast (dictation) and full (meeting). | `transcribe_fast(audio_np, model)` — in-memory numpy → text; `transcribe(audio_path, diarize, model)` — file-based full pipeline; `_load_model()`, `_load_align_model()` |
| `worker.py` | **Subprocess entry point.** Runs transcription in isolated process (crash safety). Receives requests via Queue, returns results. | `worker_main()` — event loop handling `transcribe_fast`, `transcribe`, `reload_model`, `shutdown` requests |
| `worker_manager.py` | **Subprocess lifecycle.** Spawns/kills/restarts worker process. IPC via multiprocessing.Queue. Crash detection + respawn. | `TranscriptionWorker` class — `start()`, `wait_ready()`, `transcribe_fast()`, `transcribe()`, `reload_model()`, `restart()`, `is_alive()` |
| `streaming_wav.py` | **Crash-safe WAV writer.** Writes audio incrementally during recording. Can recover orphaned files after crash. | `StreamingWavWriter` class — `write()`, `close()`, `_write_header()`; `fix_orphan(path)` — validates + rewrites header; `cleanup_temp_files(dir)` |
| `config.py` | **Config manager.** Loads `config.defaults.json`, deep-merges with user `config.json` overrides. | `load()` → merged dict; `save(overrides)` → writes config.json |
| `paths.py` | **Path resolver.** Standalone vs repo mode detection, model cache dir, output dir. | `is_standalone()` — checks `.standalone` marker; `get_install_root()`, `get_model_cache()`, `get_default_output_dir()` |
| `model_status.py` | **Model management.** Download, cache validation, bootstrap (auto-download tiny+base, prompt for larger). | `get_model_status(name)` → bool; `download_model(name)` → downloads to HF cache; `bootstrap_models(config)` — first-run setup |
| `icons.py` | **Tray icon generator.** Creates colored 64x64 PNG circles with labels. No external assets needed. | `_circle_icon(color, label)` → PIL Image. Colors: idle=#888, recording=#F44, dictation=#4AF, saving=#FB3, transcribing=#DD3, done=#4D4, error=#F4F, queued=#F80 |
| `paste.py` | **Text output.** Routes transcribed text to clipboard+Ctrl+V or simulated keystrokes. | `paste(text, method)`, `paste_clipboard(text)`, `paste_keystrokes(text)` |
| `flatten.py` | **Transcript converter.** JSON → readable speaker-attributed text (~90% token reduction). | `flatten(transcript_path)` → writes `transcript-readable.txt`; `PAUSE_THRESHOLD = 2.0` seconds |
| `dictation_log.py` | **Dictation history.** Appends entries to daily markdown log files. | `append(text, duration)` → `logs/data/dictation/YYYY/YYYY-MM-DD.md` |
| `crash_diagnostics.py` | **Crash safety.** Global exception hook + Windows Event Log query for recent python.exe crashes. | `install_excepthook()` — registers handler; `check_previous_crash()` → str or None |
| `watchdog.py` | **Auto-restart daemon.** Monitors main process, respawns on non-zero exit. Gives up after 5 rapid crashes. | `main()` — loop with `MAX_RESTARTS=5`, `COOLDOWN_SECONDS=5`, `RESET_AFTER_SECONDS=300` |
| `logger.py` | **Logging setup.** File-based logging to `logs/app/whisper-sync-YYYY-MM-DD.log`. DEBUG to file, INFO to console. | `logger` singleton |
| `benchmark.py` | **Performance testing.** Tests all downloaded models in both meeting and dictation modes. | `python -m whisper_sync.benchmark [wav_path]` |
| `__init__.py` | Package marker. Empty. | — |

### Non-Python Files

| File | Location | Purpose |
|------|----------|---------|
| `config.defaults.json` | `whisper_sync/` | Default configuration. Merged under user overrides. Contains hotkeys, model defaults (large-v3), paste method, sample rate. |
| `config.json` | `whisper_sync/` | User overrides. Created on first settings change. Only contains keys the user changed — everything else falls back to defaults. |
| `whisper-capture.ico` | `whisper_sync/` | Application icon (64x64). Used in Windows shortcuts. |
| `.standalone` | `whisper_sync/` | Marker file. If present, app runs in standalone mode (output → `~/Documents/WhisperSync/`). If absent, repo mode (output → repo `meetings/local-transcriptions/`). Created by `install.ps1`. |
| `requirements.txt` | Top level | Pip dependencies: `whisperx`, `sounddevice`, `pystray`, `Pillow`, `keyboard`, `pyperclip`. |
| `install.ps1` | Top level | 10-step installer: Python check → GPU detect → venv → deps → CUDA PyTorch → marker → verify → HF token → shortcuts → model bootstrap. |
| `start.ps1` | Top level | Launcher: kills existing instances, starts `python -m whisper_sync` (or `-m whisper_sync.watchdog` with `-Watchdog` flag). |
| `build-dist.ps1` | `whisper_sync/` (dev only) | Builds distribution zip. Stages files to temp dir, adds `.standalone` marker, creates `whisper-sync-v1.0.zip`. |
| `setup-env.ps1` | `whisper_sync/` (dev only) | Dev environment bootstrap. Similar to install.ps1 but for repo mode development. |
| `README.md` | `whisper_sync/` | This file. |

---

## Architecture & Data Flow

### Module Dependency Map

```
__main__.py (entry point, UI, hotkeys)
├── config.py (settings)
├── paths.py (directories)
├── logger.py (logging)
├── crash_diagnostics.py (exception hooks)
├── icons.py (tray icon generation)
├── paste.py (text output)
├── capture.py (audio recording)
│   └── streaming_wav.py (crash-safe WAV)
├── worker_manager.py (subprocess lifecycle)
│   └── [spawns subprocess] → worker.py
│       ├── transcribe.py (WhisperX engine)
│       │   └── model_status.py (model cache)
│       └── logger.py
├── dictation_log.py (history)
└── flatten.py (post-processing)

watchdog.py (optional wrapper)
└── [spawns] → __main__.py

benchmark.py (standalone utility)
└── transcribe.py
```

### Dictation Flow

```
User presses Ctrl+Shift+Space
  → __main__._on_hotkey("dictation")
  → __main__._start_dictation()
    → capture.AudioCapture.start() [mic only, records to numpy array]
    → User presses hotkey again
    → capture.AudioCapture.stop() → numpy audio data
    → worker_manager.transcribe_fast(audio_np)
      → saves numpy to temp .npy file
      → sends request to worker subprocess via Queue
      → worker.py receives request
        → transcribe.transcribe_fast(audio_np) [WhisperX, no alignment]
        → returns text via Queue
    → paste.paste(text, method) → clipboard + Ctrl+V
    → dictation_log.append(text, duration)
    → icon → green (done) → gray (idle)
```

### Meeting Flow

```
User presses Ctrl+Shift+M
  → __main__._on_hotkey("meeting")
  → __main__._start_meeting()
    → Prompts for meeting name (popup dialog)
    → capture.AudioCapture.start() [mic + speaker loopback]
      → streaming_wav.StreamingWavWriter [incremental disk writes]
    → User presses hotkey again
    → capture.AudioCapture.stop() → WAV file on disk
    → worker_manager.transcribe(audio_path, diarize=True)
      → sends request to worker subprocess via Queue
      → worker.py receives request
        → transcribe.transcribe(audio_path, diarize=True)
          → Step 1: Load whisperX model
          → Step 2: Transcribe audio → raw segments
          → Step 3: Load alignment model + align timestamps
          → Step 4: Load diarization model + assign speakers
          → Step 5: Write transcript.json
        → returns result via Queue
    → icon → green (done) → gray (idle)
```

### Subprocess Architecture

The transcription engine runs in a **separate subprocess** (`worker.py`) for crash isolation. GPU operations (especially CUDA) can segfault — a segfault in the worker doesn't kill the main UI process.

```
Main Process (__main__.py)          Worker Process (worker.py)
├── UI thread (pystray)             ├── Transcription engine
├── Hotkey thread (keyboard)        ├── WhisperX models in GPU memory
├── Audio capture                   └── Receives requests via Queue
└── IPC via multiprocessing.Queue
    ├── request_q: main → worker
    └── response_q: worker → main
```

- Each request has a unique `request_id` to match responses
- Priority queue: dictation requests can interrupt pending meeting stages
- Worker sends `{"type": "ready"}` after model preload completes
- If worker dies (segfault), `worker_manager` detects via `is_alive()` and can respawn

---

## Dependency Deep Dive

### Why PyTorch Needs `--force-reinstall --no-deps`

WhisperX's `pip install whisperx` pulls in a **CPU-only** version of PyTorch. For GPU acceleration, you must override it:

```powershell
pip install torch torchaudio --index-url https://download.pytorch.org/whl/{cudaVersion} --force-reinstall --no-deps
```

- `--force-reinstall`: Overwrites the CPU-only torch
- `--no-deps`: Prevents re-pulling whisperX's dependency chain
- The `--index-url` points to PyTorch's CUDA-specific wheel repository

**If this step is skipped or fails**, transcription will work but run on CPU (5-10x slower). The symptom is `torch.cuda.is_available()` returning `False`.

### CUDA Version → GPU Family Mapping

| GPU Family | CUDA Version | PyTorch Index |
|------------|:---:|---|
| RTX 50-series (Blackwell) | cu128 | `https://download.pytorch.org/whl/cu128` |
| RTX 20/30/40-series, A-series, L-series | cu121 | `https://download.pytorch.org/whl/cu121` |
| GTX 10-series, GTX 9-series | cu118 | `https://download.pytorch.org/whl/cu118` |
| No GPU / unknown | CPU | `https://download.pytorch.org/whl/cpu` |

The installer detects the GPU name via `nvidia-smi --query-gpu=name --format=csv,noheader` and matches against regex patterns. Users can override manually.

### Full Dependency Tree

```
whisperx (speech-to-text)
├── faster-whisper (CTranslate2 backend)
├── torch (CPU version — MUST be overridden with CUDA version)
├── torchaudio
├── transformers (HuggingFace model loading)
├── pyannote.audio (speaker diarization, gated models)
└── numpy, scipy, etc.

sounddevice (audio I/O, WASAPI loopback)
pystray (system tray icon)
Pillow (icon image generation)
keyboard (global hotkey listener)
pyperclip (clipboard access)
```

### Offline Operation

After initial installation and model download, WhisperSync works fully offline:
- All models cached in `~/.cache/huggingface/hub/`
- No network calls during transcription
- The only network-dependent step is first-run model downloads

---

## Model Management

### Model Cache Location

Models are stored in the HuggingFace cache directory:

```
%USERPROFILE%\.cache\huggingface\hub\
├── models--openai--whisper-tiny\
├── models--openai--whisper-base\
├── models--openai--whisper-small\
├── models--openai--whisper-medium\
├── models--openai--whisper-large-v2\
├── models--openai--whisper-large-v3\
├── models--facebook--wav2vec2-conformer-rel-pos-large\   (alignment)
├── models--pyannote--segmentation-3.0\                    (diarization, gated)
└── models--pyannote--speaker-diarization-3.1\             (diarization, gated)
```

Each model folder contains:
```
models--openai--whisper-large-v3\
├── blobs\         # Actual model weights (large binary files)
├── refs\          # Branch/tag references
│   └── main       # Text file containing the current commit hash
└── snapshots\     # Versioned snapshots
    └── {hash}\    # Contains config.json, model.safetensors, etc.
```

### Model Sizes

| Model | Download Size | Disk Size | VRAM | Purpose |
|-------|:---:|:---:|:---:|---------|
| whisper-tiny | ~39 MB | ~75 MB | ~1 GB | Fast dictation |
| whisper-base | ~140 MB | ~150 MB | ~1 GB | Default dictation |
| whisper-small | ~466 MB | ~500 MB | ~2 GB | Balanced |
| whisper-medium | ~1.5 GB | ~1.5 GB | ~4 GB | High accuracy |
| whisper-large-v2 | ~2.9 GB | ~3 GB | ~8 GB | Best (older) |
| whisper-large-v3 | ~2.9 GB | ~3 GB | ~8 GB | Best (recommended) |
| wav2vec2-conformer | ~360 MB | ~400 MB | — | Word-level alignment |
| segmentation-3.0 | ~360 MB | ~400 MB | — | Speaker segmentation (gated) |
| speaker-diarization-3.1 | ~17 MB | ~20 MB | — | Speaker pipeline config (gated) |

### Manual Model Download (When Automated Download Fails)

If `bootstrap_models` or automatic download fails (firewall, proxy, disk space), you can manually place model files:

**Step 1: Download from HuggingFace web UI**

Go to the model page (e.g., `https://huggingface.co/openai/whisper-large-v3`) and click "Files and versions". Download the entire model by clicking the download button, or download individual files.

**Step 2: Place in the correct cache location**

The cache expects this structure:
```
%USERPROFILE%\.cache\huggingface\hub\models--openai--whisper-large-v3\
├── refs\
│   └── main              # Text file: just the commit hash (e.g., "a4f242...")
└── snapshots\
    └── {commit_hash}\    # Folder named with the commit hash
        ├── config.json
        ├── model.safetensors
        ├── preprocessor_config.json
        ├── tokenizer.json
        ├── vocabulary.json
        └── ... (other model files)
```

**Step 3: Verify the model is detected**

```powershell
.\whisper-env\Scripts\python.exe -c "from whisper_sync.model_status import get_model_status; print(get_model_status('large-v3'))"
```

Should print `True`.

**Alternative: Use `huggingface-cli`**

```powershell
.\whisper-env\Scripts\python.exe -m pip install huggingface-hub
.\whisper-env\Scripts\huggingface-cli download openai/whisper-large-v3
```

### Gated Models (Diarization)

The pyannote diarization models are **gated** — you must:
1. Have a HuggingFace account
2. Visit each model page and click "Agree" to accept the license
3. Have a valid HF token saved at `~/.huggingface/token`

Without this, dictation works fine but meeting mode won't have speaker identification.

---

## Configuration Reference

### config.defaults.json (all keys)

```json
{
  "hotkeys": {
    "dictation_toggle": "ctrl+shift+space",
    "meeting_toggle": "ctrl+shift+m"
  },
  "paste_method": "clipboard",
  "language": "en",
  "model": "large-v3",
  "dictation_model": "large-v3",
  "compute_type": "float16",
  "output_dir": "transcriptions",
  "mic_device": null,
  "speaker_device": null,
  "sample_rate": 16000,
  "use_system_devices": true,
  "left_click": "meeting",
  "middle_click": "dictation"
}
```

- `model`: Used for meeting transcription
- `dictation_model`: Used for dictation (can differ from meeting model for speed)
- `compute_type`: `float16` (GPU) or `int8` (CPU fallback). Auto-selected.
- `output_dir`: Relative to install root unless absolute path
- `mic_device` / `speaker_device`: `null` = system default. Set to device index (int) or name (string) to override.
- `use_system_devices`: When `true`, ignores manual device selections and follows Windows defaults

### Config Merge Behavior

`config.py` loads `config.defaults.json` first, then deep-merges `config.json` on top. Deep merge means nested objects (like `hotkeys`) are merged key-by-key, not replaced wholesale.

If `config.json` is corrupted (invalid JSON), the app **will crash on startup**. Fix: delete `config.json` to reset to defaults.

---

## Log File Locations

| Log Type | Path | Content |
|----------|------|---------|
| App log | `whisper_sync/logs/app/whisper-sync-YYYY-MM-DD.log` | DEBUG-level application log |
| Dictation log | `whisper_sync/logs/data/dictation/YYYY/YYYY-MM-DD.md` | Timestamped dictation history |
| Crash log | Windows Event Log (Application) | Native crashes (segfaults) |

---

## Troubleshooting Playbook (For AI Assistants)

This section is designed for an AI assistant (Claude, etc.) to systematically diagnose and fix WhisperSync issues. Follow the decision trees below.

### Quick Diagnostic Commands

Run these first to understand the system state:

```powershell
# 1. Check Python version
python --version

# 2. Check if venv exists and has correct packages
.\whisper-env\Scripts\python.exe -c "import torch; print(f'PyTorch: {torch.__version__}, CUDA: {torch.cuda.is_available()}, Device: {torch.cuda.get_device_name(0) if torch.cuda.is_available() else \"N/A\"}')"

# 3. Check whisperX import
.\whisper-env\Scripts\python.exe -c "import whisperx; print('whisperX OK')"

# 4. Check audio devices
.\whisper-env\Scripts\python.exe -c "import sounddevice; print(sounddevice.query_devices())"

# 5. Check which models are cached
.\whisper-env\Scripts\python.exe -c "
from whisper_sync.model_status import get_model_status
for m in ['tiny','base','small','medium','large-v2','large-v3']:
    print(f'  {m}: {\"cached\" if get_model_status(m) else \"NOT cached\"}')"

# 6. Check HuggingFace token
.\whisper-env\Scripts\python.exe -c "
from pathlib import Path
t = Path.home() / '.huggingface' / 'token'
print(f'HF token: {\"exists (\" + str(len(t.read_text().strip())) + \" chars)\" if t.exists() else \"MISSING\"}')"

# 7. Check GPU
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader

# 8. Check config
.\whisper-env\Scripts\python.exe -c "from whisper_sync import config; import json; print(json.dumps(config.load(), indent=2))"

# 9. Check for recent crash logs
Get-ChildItem "whisper_sync\logs\app\" | Sort-Object LastWriteTime -Descending | Select-Object -First 3
```

### Decision Tree: App Won't Start

```
SYMPTOM: App doesn't launch or crashes immediately

1. "python" not recognized
   → Python not in PATH
   → Fix: Install Python 3.10+ from python.org, check "Add to PATH"
   → Or: Use full path to venv python: .\whisper-env\Scripts\python.exe

2. "No module named 'whisper_sync'"
   → Not running from correct directory
   → Fix: cd to the whisper-sync folder (parent of whisper_sync/)
   → Or: Run via start.ps1 which sets the correct working directory

3. "ModuleNotFoundError: No module named 'whisperx'" (or sounddevice, pystray, etc.)
   → Dependencies not installed or wrong Python
   → Fix: Run install.ps1 again, or manually:
     .\whisper-env\Scripts\pip.exe install -r requirements.txt

4. "CUDA error" / "RuntimeError: CUDA"
   → PyTorch/CUDA version mismatch
   → Diagnose: .\whisper-env\Scripts\python.exe -c "import torch; print(torch.version.cuda)"
   → Fix: Reinstall PyTorch with correct CUDA version:
     .\whisper-env\Scripts\pip.exe install torch torchaudio --index-url https://download.pytorch.org/whl/cu121 --force-reinstall --no-deps
   → Use cu118 for GTX 10-series, cu121 for RTX 20/30/40, cu128 for RTX 50-series

5. "PermissionError" or hotkey registration fails
   → keyboard module needs elevated privileges
   → Fix: Run PowerShell as Administrator, then start.ps1

6. Crashes silently (no error visible)
   → Check log file: whisper_sync\logs\app\whisper-sync-YYYY-MM-DD.log
   → Check Windows Event Log: Get-EventLog -LogName Application -Source "Application Error" -Newest 5
   → Try running directly to see console output:
     .\whisper-env\Scripts\python.exe -m whisper_sync

7. "Invalid JSON" / config crash
   → config.json is corrupted
   → Fix: Delete whisper_sync\config.json (resets to defaults)
```

### Decision Tree: Transcription Fails

```
SYMPTOM: Press hotkey, speak, but no text appears / error icon

1. Icon turns magenta (error) immediately
   → Model not loaded
   → Check: Are models downloaded?
     .\whisper-env\Scripts\python.exe -c "from whisper_sync.model_status import get_model_status; print(get_model_status('large-v3'))"
   → Fix: Download model:
     .\whisper-env\Scripts\python.exe -c "from whisper_sync.model_status import download_model; download_model('large-v3')"
   → Or use a smaller model that IS cached (change in Settings menu)

2. Long wait then error
   → Timeout (60s dictation, 600s meeting)
   → Check log for "TimeoutError"
   → Fix: Use smaller model, or check if GPU is actually being used

3. Text appears but is gibberish / wrong language
   → Wrong model or language setting
   → Fix: Set language to "en" in config.json
   → Try a larger model (small → medium → large-v3)

4. Worker process segfaults (crash mid-transcription)
   → GPU driver issue or VRAM exhaustion
   → Check: nvidia-smi (look at memory usage)
   → Fix: Try smaller model, update GPU drivers, or force CPU mode:
     Set "compute_type": "int8" in config.json

5. Empty transcription result
   → Audio not captured properly
   → Test mic: Record a short dictation and check if logs show audio data
   → Check: .\whisper-env\Scripts\python.exe -c "import sounddevice; print(sounddevice.query_devices())"
```

### Decision Tree: Audio Issues

```
SYMPTOM: No audio captured, wrong device, or poor quality

1. No microphone audio
   → Check Windows Sound Settings → Input → ensure correct mic is default
   → List devices:
     .\whisper-env\Scripts\python.exe -c "import sounddevice as sd; print(sd.query_devices())"
   → If manually set device in config.json, the device ID may have changed
   → Fix: Delete "mic_device" from config.json to reset to system default

2. No speaker/system audio (meeting mode)
   → WASAPI loopback not available
   → This is Windows-native, no virtual cable needed
   → Check: Is the speaker device visible in the device list?
   → Fix: Ensure "use_system_devices" is true in config, or manually set "speaker_device"

3. Audio is mono when expecting stereo (or vice versa)
   → Normal behavior: mic and speaker are captured separately, combined into stereo WAV
   → Left channel = mic, Right channel = speaker (in meeting mode)

4. Very quiet audio / bad transcription
   → Mic volume too low in Windows settings
   → Speaker volume doesn't affect loopback capture (WASAPI captures the digital signal)
```

### Decision Tree: Diarization (Speaker ID) Fails

```
SYMPTOM: Meeting transcription works but no speaker labels, or diarization errors

1. "401 Unauthorized" / "Invalid token"
   → HuggingFace token missing or expired
   → Check: Does ~/.huggingface/token exist?
   → Fix: Get new token from huggingface.co/settings/tokens, save to file

2. "403 Forbidden" / "Access denied"
   → Model license not accepted
   → Fix: Visit BOTH pages and click "Agree":
     https://huggingface.co/pyannote/segmentation-3.0
     https://huggingface.co/pyannote/speaker-diarization-3.1

3. "No module named 'pyannote'"
   → whisperX installation incomplete
   → Fix: pip install pyannote.audio (or reinstall whisperx)

4. Diarization runs but all segments are SPEAKER_00
   → Audio has only one detectable speaker
   → Or: Audio quality too low for speaker separation
   → Not a bug — pyannote's limitation with poor audio

5. Wrong speaker count / speakers mixed up
   → Normal pyannote behavior — it estimates speaker count
   → No fix in WhisperSync (pyannote limitation)
   → Tip: Re-transcribe and manually edit speaker_map in transcript.json
```

### Decision Tree: Installation Issues

```
SYMPTOM: install.ps1 fails or produces broken installation

1. "running scripts is disabled on this system"
   → PowerShell execution policy blocks scripts
   → Fix: Run with bypass:
     powershell -ExecutionPolicy Bypass -File install.ps1

2. GPU detected but CUDA test fails after install
   → Wrong CUDA version selected
   → Diagnose: .\whisper-env\Scripts\python.exe -c "import torch; print(torch.version.cuda)"
   → Fix: Reinstall PyTorch manually:
     .\whisper-env\Scripts\pip.exe install torch torchaudio --index-url https://download.pytorch.org/whl/cu121 --force-reinstall --no-deps

3. "No GPU detected" but GPU is present
   → nvidia-smi not in PATH
   → Fix: Install/update NVIDIA drivers from nvidia.com/drivers
   → Verify: Run nvidia-smi directly in PowerShell

4. venv creation fails
   → Python installation missing venv module
   → Fix: Reinstall Python with "pip" and "tcl/tk" options checked

5. pip install hangs or fails
   → Network/firewall issue
   → Fix: Try with --proxy if behind corporate firewall
   → Or: Download wheels manually and install offline

6. Model bootstrap fails
   → Network issue during download
   → Fix: See "Manual Model Download" section above
```

### How to Reset Everything

If troubleshooting gets stuck, a clean reinstall:

```powershell
# 1. Delete the venv (keeps models cached globally)
Remove-Item -Recurse -Force .\whisper-env\

# 2. Delete local config (resets to defaults)
Remove-Item .\whisper_sync\config.json -ErrorAction SilentlyContinue

# 3. Re-run installer
powershell -ExecutionPolicy Bypass -File install.ps1

# Models at ~/.cache/huggingface/ are preserved — no re-download needed
```

To also clear cached models (full reset):
```powershell
Remove-Item -Recurse -Force "$env:USERPROFILE\.cache\huggingface\hub\models--openai--whisper-*"
```

---

## Instructions for AI Assistants

If you are an AI assistant (Claude, GPT, etc.) helping a user troubleshoot WhisperSync, follow this protocol:

### Step 1: Gather System State

Run the **Quick Diagnostic Commands** above (all 9 commands). This tells you:
- Python and venv health
- GPU/CUDA status
- Which models are available
- HF token status
- Audio device state
- Current configuration
- Recent logs

### Step 2: Identify the Symptom Category

Match the user's problem to one of these categories:
- **Won't start** → Decision Tree: App Won't Start
- **Transcription broken** → Decision Tree: Transcription Fails
- **Audio problems** → Decision Tree: Audio Issues
- **Speaker labels wrong/missing** → Decision Tree: Diarization Fails
- **Installation failed** → Decision Tree: Installation Issues

### Step 3: Follow the Decision Tree

Each tree is ordered by most likely cause first. Work through it top to bottom.

### Step 4: If Nothing Works

1. Read the latest log file: `whisper_sync/logs/app/whisper-sync-YYYY-MM-DD.log`
2. Look for Python tracebacks — the last exception is usually the root cause
3. If the error is in `transcribe.py`, the issue is usually model/GPU related
4. If the error is in `capture.py`, the issue is usually audio device related
5. If the error is in `__main__.py`, the issue is usually configuration or hotkey related

### Step 5: Modifying Code

If you need to edit Python files to fix an issue:

- **Entry point**: `__main__.py` — UI logic, hotkeys, recording lifecycle
- **Audio**: `capture.py` — device selection, WASAPI setup
- **Transcription**: `transcribe.py` — model loading, whisperX pipeline
- **Subprocess**: `worker.py` + `worker_manager.py` — IPC, crash recovery
- **Config**: `config.py` — settings loading/saving
- **Paths**: `paths.py` — standalone vs repo mode detection

**Important constants to know:**
- Dictation timeout: 60 seconds (`worker_manager.py`)
- Meeting timeout: 600 seconds (`worker_manager.py`)
- Worker ready timeout: 120 seconds (`worker_manager.py`)
- Crash recovery: WAV must be ≥ 5 seconds (`streaming_wav.py`)
- Watchdog: 5 max restarts, 5s cooldown, 300s stability reset (`watchdog.py`)
- Pause threshold for paragraph breaks: 2.0 seconds (`flatten.py`)

### Key Design Decisions

1. **Subprocess isolation**: Transcription runs in a child process because CUDA segfaults would otherwise kill the entire app. The worker can be restarted without losing the UI.
2. **Streaming WAV**: Audio is written to disk incrementally during recording. This means crash recovery can salvage partial recordings.
3. **PyTorch override**: whisperX pulls CPU-only torch. The installer forces CUDA torch. If CUDA stops working, this override is the first thing to check.
4. **Config merge**: Defaults + user overrides, deep merged. User config.json only contains changed keys. Deleting it resets everything.
5. **Standalone marker**: The `.standalone` file determines path resolution. Without it, the app assumes it's running inside the development repo.
