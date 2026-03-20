# WhisperSync — Agent Instructions

Local speech-to-text for Windows. GPU-accelerated transcription with speaker diarization, dictation mode, and meeting recording.

## Architecture

### Module Map

| Module | Purpose |
|--------|---------|
| `__main__.py` | Entry point. `WhisperSync` class — tray icon, hotkeys, mode state machine, orchestrates everything |
| `worker.py` | Worker subprocess entry point. Runs whisperX/CUDA in isolation. Receives requests via queue, returns results |
| `worker_manager.py` | `TranscriptionWorker` class — spawns/manages the worker subprocess, handles crashes and respawns |
| `capture.py` | `AudioRecorder` — mic + WASAPI loopback recording, device enumeration |
| `transcribe.py` | Transcription pipeline — model loading, `transcribe_fast` (dictation), staged pipeline (meeting: prepare → transcribe → align → diarize → finalize) |
| `config.py` | Three-tier config: `config.defaults.json` → `config.json` (user overrides). `load()` and `save()` |
| `paths.py` | Path resolution — standalone vs repo mode detection, model cache, output directory |
| `speakers.py` | Speaker identification via Claude CLI (`claude -p`), speaker map management, config persistence |
| `flatten.py` | Convert whisperX JSON transcript → readable text with speaker labels. Reduces tokens ~90% |
| `streaming_wav.py` | Crash-safe WAV writer — writes PCM chunks to disk incrementally during recording |
| `paste.py` | Paste transcription into focused window via clipboard or keystrokes |
| `icons.py` | Programmatic tray icon generation (PIL) — idle, recording, transcribing, done, error states |
| `logger.py` | File-based logging — persists across crashes, daily rotation |
| `dictation_log.py` | Append-only daily markdown log of all dictations |
| `model_status.py` | Model download/cache management, bootstrap for first-time setup |
| `benchmark.py` | Compare transcription speed across models (meeting + dictation modes) |
| `rebuild_index.py` | Generate INDEX.md files for meeting transcript folders |
| `split_meeting.py` | Split a recording into multiple meetings, preserving file metadata |
| `migrate_folders.py` | One-time migration to week-based folder structure |
| `crash_diagnostics.py` | Exception hooks, Windows Event Log checks for GPU crashes |
| `watchdog.py` | External watchdog — restarts WhisperSync if it crashes |

### Audio Pipeline

```
Recording:
  mic + speaker loopback → AudioRecorder → numpy array + streaming WAV (crash safety)

Dictation (fast path):
  numpy array → worker.transcribe_fast → text → paste to focused window

Meeting (staged pipeline):
  WAV file → worker.stage_prepare → stage_transcribe → stage_align → stage_diarize → stage_finalize
           → transcript.json → flatten.py → transcript-readable.txt
           → speakers.py (identify) → minutes.md (via Claude CLI, optional)
```

### Multiprocessing Model

```
Main Process (__main__.py)                Worker Process (worker.py)
┌─────────────────────────┐              ┌──────────────────────────┐
│ Tray icon + hotkeys     │              │ whisperX + CUDA          │
│ Audio recording         │  request_q → │ Model loading            │
│ Mode state machine      │              │ Transcription            │
│ Speaker identification  │ ← response_q │ Alignment + diarization  │
│ Minutes generation      │              │                          │
└─────────────────────────┘              └──────────────────────────┘
         ↕                                        spawned via
   TranscriptionWorker                     multiprocessing "spawn"
   (worker_manager.py)                     context (fresh Python)
```

**Why separate processes:** whisperX/CTranslate2/CUDA can segfault. Isolating them in a subprocess means the main process (tray icon, hotkeys) survives and can respawn the worker.

**Queue protocol:** Main sends `{type, request_id, ...}` on request_q, worker responds `{type, request_id, ...}` on response_q. Types: `transcribe_fast`, `transcribe`, `reload_model`, `shutdown`.

### Config System

Three tiers:
1. `config.defaults.json` — shipped defaults, never modified
2. `config.json` — user overrides, gitignored, created by installer or settings menu
3. Runtime — `config.load()` merges defaults + overrides

Key settings: `model`, `dictation_model`, `output_dir`, `hotkeys`, `paste_method`, `batch_size` ("auto" = GPU-adaptive), `left_click`, `middle_click`.

### Path Resolution

`paths.py` detects two modes:
- **Standalone** (`.standalone` marker file exists): package dir is root, output to `~/Documents/whispersync-meetings`
- **Repo mode** (no marker): two levels up is repo root, output to `<repo>/meetings/local-transcriptions`

Model cache is always `whisper_sync/models/` regardless of mode.

## Conventions

- **Logging**: Always use `from .logger import logger` then `logger.info(...)`. Prefix user-facing console messages with `[WhisperSync]`.
- **Config changes**: Modify via `config.save(cfg)` — never write `config.json` directly. Only keys in `_VALID_KEYS` are persisted.
- **Worker communication**: Always use request/response queues with `request_id`. Never share state between processes via globals.
- **Error handling in worker**: Catch exceptions, send `{type: "error", ...}` response. Never let exceptions kill the worker silently.
- **Icons**: Generated programmatically in `icons.py`. No external image assets.
- **Warning suppression**: Scoped filters only — must specify `message=`, `category=`, and/or `module=`. Never use blanket `warnings.filterwarnings("ignore")`.
- **Commit messages**: `fix(whisper-sync):`, `feat(whisper-sync):`, `ci:`, `docs:` prefixes.

## Guardrails

### Do NOT
- Modify `config.defaults.json` without updating `_VALID_KEYS` in `config.py`
- Add blanket warning suppressions
- Change the multiprocessing context from "spawn" (required for CUDA isolation)
- Introduce shared mutable state between main and worker processes
- Add external image/font assets — icons are generated in code
- Delete or modify `streaming_wav.py` crash safety without understanding the failure mode
- Change hotkey registration logic without testing that hotkeys still work with the tray icon
- Commit `config.json`, `logs/`, `models/`, `whisper-env/`, or `__pycache__/`

### Always
- Test both dictation AND meeting modes after any transcribe.py or worker.py change
- Check that the tray icon still responds after any __main__.py change
- Verify GPU memory doesn't leak after multiple dictation/meeting cycles
- Use `--author="Pendentive <pendentive.info@gmail.com>"` for commits to this repo

## File Ownership

| Files | Authoritative in | Sync direction |
|-------|------------------|----------------|
| All `.py`, `.ps1`, `.txt`, `.md`, `.ico`, `config.defaults.json` | `ic-product-mgmt` | ic-product-mgmt → pendentive (auto via post-commit hook) |
| `CLAUDE.md`, `.github/`, `docs/plans/` | `pendentive` | pendentive only (not synced back) |
| `config.json`, `logs/`, `models/` | Neither (per-machine state) | gitignored |

## Testing Changes

No test suite exists yet. Manual verification:

1. **Dictation**: Press `Ctrl+Shift+Space`, speak for 3-5 seconds, press again. Text should appear in focused window.
2. **Meeting**: Press `Ctrl+Shift+M`, speak or play audio for 10+ seconds, press again. Follow save dialog. Check transcript output.
3. **GPU memory**: After 5+ dictations, GPU memory should be stable (not growing). Check with `nvidia-smi`.
4. **Crash recovery**: Kill the worker process (`taskkill /F /PID <worker_pid>`). Main process should detect and respawn.
5. **Config changes**: Modify settings via tray menu. Restart. Verify settings persisted.

## Dependencies

- **whisperX** — transcription + alignment + diarization (pulls in PyTorch, faster-whisper, pyannote-audio)
- **PyTorch with CUDA** — GPU acceleration (cu124 for RTX 20/30/40 series)
- **keyboard** — global hotkey registration
- **pystray** — system tray icon
- **sounddevice + PyAudioWPatch** — audio capture (WASAPI loopback)
- **pyperclip** — clipboard access for paste
- **Pillow** — icon generation
- **Claude CLI** (`claude -p`) — optional, for speaker identification and meeting minutes
