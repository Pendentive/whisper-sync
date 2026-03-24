# WhisperSync - Agent Instructions

Local speech-to-text for Windows. GPU-accelerated transcription with speaker diarization, dictation mode, and meeting recording.

## Key Documents

- **`docs/ui-spec.md`** - UI component inventory, routing map, state machine, dialog patterns, extension points. **Read this before any UI change.**
- **`docs/development.md`** - Local dev setup, debugging, common issues.
- **`docs/plans/`** - Implementation plans for major features.

## Architecture

### Module Map

| Module | Purpose |
|--------|---------|
| `__main__.py` | Entry point. `WhisperSync` class: tray icon, hotkeys, mode state machine, recording flows, GitHub status integration, incognito mode, session stats. Orchestrates everything |
| `notifications.py` | Windows toast notifications via `windows-toasts`. Supports buttons and click callbacks. Graceful fallback to logging when unavailable |
| `github_status.py` | Background GitHub PR polling via `gh` CLI. Parses Copilot review state, suggestion counts, complexity labels. Fires change callbacks for toast alerts |
| `logger.py` | Tiered logging with ANSI colors. Four tiers: off, normal, detailed, verbose. File handler (DEBUG, daily rotation) + console handler (tier-controlled). Custom TRANSCRIPT level for content previews |
| `icons.py` | Dual-ring tray icon generation via PIL. Outer ring reflects channel health (e.g. speaker loopback status), inner ring reflects app state. No external image assets |
| `config.py` | Config load/save. Merges `config.defaults.json` with user overrides from `.whispersync/config.json`. Falls back to legacy `whisper_sync/config.json`. Only `_VALID_KEYS` are persisted |
| `paths.py` | Data directory resolution. Two-phase bootstrap for output_dir. Standalone vs repo mode detection. All `.whispersync/` path accessors live here. Legacy path helpers for migration |
| `dictation_log.py` | Persistent dictation history. Append-only daily markdown logs in `.whispersync/dictation-logs/`. Supports `load_recent()` for the Recent Dictations menu |
| `transcribe.py` | WhisperX transcription with CPU/GPU device selection. Persistent model cache. `transcribe_fast` (dictation) and staged pipeline (meeting: prepare, transcribe, align, diarize, finalize) |
| `capture.py` | `AudioRecorder`: mic + WASAPI loopback recording via sounddevice/PyAudioWPatch. Device enumeration, channel health tracking |
| `speakers.py` | AI speaker identification via Claude CLI (`claude -p`). Speaker map management, transcription-config.md persistence |
| `model_status.py` | Model download and cache management. Bootstrap for first-time setup. Checks HuggingFace cache and torch hub |
| `paste.py` | Clipboard/keystroke paste into focused window via pyperclip |
| `worker.py` | Multiprocessing transcription worker. Runs whisperX/CUDA in isolation. Receives requests via queue, returns results. Staged pipeline with inter-stage priority checks |
| `worker_manager.py` | `TranscriptionWorker`: spawns/manages the worker subprocess. Crash detection and automatic respawn |
| `flatten.py` | Convert whisperX JSON transcript to readable text with speaker labels. ~90% token reduction |
| `streaming_wav.py` | Crash-safe WAV writer. Writes PCM chunks to disk incrementally during recording |
| `rebuild_index.py` | Generate INDEX.md files for meeting transcript folders |
| `split_meeting.py` | Split a long recording into multiple meetings, preserving file metadata |
| `crash_diagnostics.py` | Exception hooks, Windows Event Log checks for GPU crashes |
| `benchmark.py` | Compare transcription speed across models (meeting + dictation modes) |
| `watchdog.py` | External watchdog process. Restarts WhisperSync if it crashes |
| `migrate_folders.py` | One-time migration to week-based folder structure (legacy utility) |

### Audio Pipeline

```
Recording:
  mic + speaker loopback -> AudioRecorder -> numpy array + streaming WAV (crash safety)

Dictation (fast path):
  numpy array -> worker.transcribe_fast -> text -> paste to focused window

Meeting (staged pipeline):
  WAV file -> worker.stage_prepare -> stage_transcribe -> stage_align -> stage_diarize -> stage_finalize
           -> transcript.json -> flatten.py -> transcript-readable.txt
           -> speakers.py (identify) -> minutes.md (via Claude CLI, optional)
```

### Multiprocessing Model

```
Main Process (__main__.py)                Worker Process (worker.py)
+--------------------------+              +----------------------------+
| Tray icon + hotkeys      |              | whisperX + CUDA            |
| Audio recording          |  request_q ->| Model loading              |
| Mode state machine       |              | Transcription              |
| Speaker identification   |<- response_q | Alignment + diarization    |
| Minutes generation       |              |                            |
| GitHub PR polling        |              |                            |
| Toast notifications      |              |                            |
+--------------------------+              +----------------------------+
         |                                        spawned via
   TranscriptionWorker                     multiprocessing "spawn"
   (worker_manager.py)                     context (fresh Python)
```

**Why separate processes:** whisperX/CTranslate2/CUDA can segfault. Isolating them in a subprocess means the main process (tray icon, hotkeys) survives and can respawn the worker.

**Queue protocol:** Main sends `{type, request_id, ...}` on request_q, worker responds `{type, request_id, ...}` on response_q. Types: `transcribe_fast`, `transcribe`, `reload_model`, `shutdown`.

### Config System

Two-phase bootstrap resolves where config lives:

1. **Legacy pointer** (`whisper_sync/config.json`): If present, provides `output_dir` to locate the real config. Existing installs keep this as a bootstrap pointer.
2. **Real config** (`output_dir/.whispersync/config.json`): User overrides live here. Created on first save. This is the authoritative config location.
3. **Defaults** (`whisper_sync/config.defaults.json`): Shipped defaults, never modified by the app. Merged as the base layer at load time.

Load order: defaults -> real config (if exists) -> legacy config (fallback if real missing).

Key settings: `model`, `dictation_model`, `output_dir`, `device` (auto/cpu/cuda), `hotkeys`, `paste_method`, `log_window` (off/normal/detailed/verbose), `incognito`, `github_repo`, `github_poll_interval`, `left_click`, `middle_click`.

### Data Directory Layout

All user data lives under `output_dir/.whispersync/`. Transcription output sits alongside in `output_dir/`.

```
output_dir/
  .whispersync/
    config.json              # user config overrides
    transcription-config.md  # speaker identification rules
    dictation-logs/          # daily dictation history (YYYY-MM-DD.md)
  INDEX.md                   # auto-generated meeting index
  03-w3/                     # week-based folders
    0320_1019_topic-name/
      recording.wav          # gitignored
      transcript.json
      transcript-readable.txt
      minutes.md
```

### Path Resolution

`paths.py` detects two modes:
- **Standalone** (`.standalone` marker file exists): package dir is root, output to `~/Documents/WhisperSync/transcriptions`
- **Repo mode** (no marker): two levels up is repo root, output to `<repo>/meetings/local-transcriptions`

Model cache is always `whisper_sync/models/` regardless of mode.

## Features

- **Tiered log window**: Four tiers (off, normal, detailed, verbose) control console output density. File logging always captures DEBUG level.
- **CPU/GPU device selection**: `device` config key. `auto` prefers CUDA if available, falls back to CPU. Explicit `cpu` or `cuda` supported.
- **Incognito mode**: When enabled, dictation audio is not saved to disk and dictation log entries are skipped. RAM-only processing.
- **Toast notifications**: Windows native toasts via `windows-toasts`. Used for meeting completion, GitHub PR state changes, and errors.
- **GitHub PR status**: Background polling of open PRs via `gh` CLI. Surfaces review state (pending, clean, suggestions, needs review) in the tray menu. Toast alerts on state changes.
- **Session stats**: Tracks dictation count, chars, time, meeting count, words, and duration for the current session. Shown in tray menu.
- **Recent Dictations**: Persistent history loaded from `.whispersync/dictation-logs/`. Accessible from tray menu.
- **Dual-ring tray icon**: Outer ring reflects channel health (speaker loopback status), inner ring reflects app state (idle, recording, transcribing, done, error).

## Conventions

- **Logging**: Always use `from .logger import logger` then `logger.info(...)`. Prefix user-facing console messages with `[WhisperSync]` only in verbose tier.
- **Config changes**: Modify via `config.save(cfg)`. Never write config files directly. Only keys in `_VALID_KEYS` are persisted.
- **Worker communication**: Always use request/response queues with `request_id`. Never share state between processes via globals.
- **Error handling in worker**: Catch exceptions, send `{type: "error", ...}` response. Never let exceptions kill the worker silently.
- **Icons**: Generated programmatically in `icons.py`. No external image assets.
- **Warning suppression**: Scoped filters only. Must specify `message=`, `category=`, and/or `module=`. Never use blanket `warnings.filterwarnings("ignore")`.
- **Commit messages**: `fix(whisper-sync):`, `feat(whisper-sync):`, `ci:`, `docs:` prefixes.
- **Text style**: Do not use em dash characters. Use single hyphens for asides, standard CLI flags (like --force) are fine.

## Guardrails

### Do NOT
- Modify `config.defaults.json` without updating `_VALID_KEYS` in `config.py`
- Add blanket warning suppressions
- Change the multiprocessing context from "spawn" (required for CUDA isolation)
- Introduce shared mutable state between main and worker processes
- Add external image/font assets. Icons are generated in code
- Delete or modify `streaming_wav.py` crash safety without understanding the failure mode
- Change hotkey registration logic without testing that hotkeys still work with the tray icon
- Commit `config.json`, `logs/`, `models/`, `whisper-env/`, or `__pycache__/`
- Write data files to the old `scripts/` location. That path no longer exists in ic-product-mgmt
- Store user data outside of `.whispersync/`. All persistent data belongs in `output_dir/.whispersync/`
- Use em dash characters in any file (single hyphens and CLI flags like --force are fine)

### Always
- Test both dictation AND meeting modes after any transcribe.py or worker.py change
- Check that the tray icon still responds after any __main__.py change
- Verify GPU memory does not leak after multiple dictation/meeting cycles
- Use `--author="Pendentive <pendentive.info@gmail.com>"` for commits to this repo
- Wait for Copilot review before merging any PR
- Update docs in the same PR as behavior changes

## File Ownership

| Files | Authoritative in | Sync direction |
|-------|------------------|----------------|
| All `.py`, `.ps1`, `.txt`, `.md`, `.ico`, `config.defaults.json` | `pendentive/whisper-sync` | pendentive is the source of truth |
| `CLAUDE.md`, `.github/`, `docs/plans/` | `pendentive` | pendentive only (not synced back) |
| `config.json`, `logs/`, `models/` | Neither (per-machine state) | gitignored |

## Testing Changes

No test suite exists yet. Manual verification:

1. **Dictation**: Press `Ctrl+Shift+Space`, speak for 3-5 seconds, press again. Text should appear in focused window.
2. **Meeting**: Press `Ctrl+Shift+M`, speak or play audio for 10+ seconds, press again. Follow save dialog. Check transcript output.
3. **GPU memory**: After 5+ dictations, GPU memory should be stable (not growing). Check with `nvidia-smi`.
4. **Crash recovery**: Kill the worker process (`taskkill /F /PID <worker_pid>`). Main process should detect and respawn.
5. **Config changes**: Modify settings via tray menu. Restart. Verify settings persisted in `.whispersync/config.json`.
6. **Incognito**: Enable incognito, dictate. Verify no WAV saved and no dictation log entry created.
7. **Log tiers**: Switch log window tier via tray menu. Verify console output changes immediately.

## Dependencies

- **whisperX** - transcription + alignment + diarization (pulls in PyTorch, faster-whisper, pyannote-audio)
- **PyTorch with CUDA** - GPU acceleration (cu124 for RTX 20/30/40 series)
- **keyboard** - global hotkey registration
- **pystray** - system tray icon
- **sounddevice + PyAudioWPatch** - audio capture (WASAPI loopback)
- **pyperclip** - clipboard access for paste
- **Pillow** - icon generation
- **windows-toasts** - Windows native toast notifications
- **Claude CLI** (`claude -p`) - optional, for speaker identification and meeting minutes
