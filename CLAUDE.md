# WhisperSync - Agent Instructions

Local speech-to-text for Windows. GPU-accelerated transcription with speaker diarization, dictation mode, and meeting recording.

## Key Documents

- **`docs/ui-spec.md`** - UI component inventory, routing map, state machine, dialog patterns
- **`docs/development.md`** - Local dev setup, debugging, common issues
- **`docs/plans/`** - Implementation plans for major features
- **`.claude/rules/audio-pipeline.md`** - Stereo recording, diarization tiers, VRAM management, worker model
- **`.claude/rules/ui-patterns.md`** - Tray menu ordering, dialog conventions, pystray limitations
- **`.claude/rules/testing.md`** - Manual test checklist for all modes
- **`.github/governance/policy.yaml`** - Auto-merge policy, path protection, review thresholds

## Module Map

| Module | Purpose |
|--------|---------|
| `__main__.py` | Entry point. Tray icon, hotkeys, mode state machine, recording flows, GitHub status, incognito, session stats |
| `transcribe.py` | WhisperX with persistent model cache. Fast path (dictation) and staged pipeline (meeting) |
| `worker.py` | Multiprocessing transcription worker. CUDA isolation via spawn context |
| `worker_manager.py` | Spawns/manages worker subprocess. Crash detection and auto-respawn |
| `capture.py` | Mic + WASAPI loopback recording via sounddevice/PyAudioWPatch |
| `channel_merge.py` | Per-channel diarization with confidence fusion for stereo recordings |
| `config.py` | Config load/save. Merges defaults with user overrides. Only `_VALID_KEYS` persisted |
| `paths.py` | Data directory resolution. Standalone vs repo mode. All `.whispersync/` accessors |
| `logger.py` | Tiered logging (off/normal/detailed/verbose). File handler always DEBUG |
| `icons.py` | Dual-ring tray icon generation via PIL. No external image assets |
| `notifications.py` | Windows toast notifications with button callbacks |
| `github_status.py` | Background PR polling via `gh` CLI. Copilot review state tracking |
| `speakers.py` | AI speaker identification via Claude CLI |
| `dictation_log.py` | Append-only daily markdown logs in `.whispersync/dictation-logs/` |
| `paste.py` | Clipboard/keystroke paste into focused window |
| `model_status.py` | Model download and cache management |
| `flatten.py` | WhisperX JSON to readable text. ~90% token reduction |
| `streaming_wav.py` | Crash-safe WAV writer. Incremental PCM chunks |
| `backup_worker.py` | Lightweight backup transcriber for dictation during meetings |
| `crash_diagnostics.py` | Exception hooks, Windows Event Log checks |
| `rebuild_index.py` | Generate INDEX.md for meeting folders |
| `split_meeting.py` | Split long recordings into multiple meetings |

## Config System

Two-phase bootstrap:
1. **Legacy pointer** (`whisper_sync/config.json`): provides `output_dir` for existing installs
2. **Real config** (`output_dir/.whispersync/config.json`): user overrides, authoritative location
3. **Defaults** (`whisper_sync/config.defaults.json`): shipped defaults, never modified

Load order: defaults -> real config -> legacy config (fallback).

Key settings: `model`, `dictation_model`, `output_dir`, `device` (auto/cpu/cuda), `hotkeys`, `paste_method`, `log_window`, `incognito`, `github_repo`, `left_click`, `middle_click`.

## Data Directory

All user data under `output_dir/.whispersync/`. Transcriptions alongside in `output_dir/`.

```
output_dir/
  .whispersync/
    config.json
    transcription-config.md
    dictation-logs/
  INDEX.md
  03-w3/
    0320_1019_topic-name/
      recording.wav    # gitignored
      transcript.json
      transcript-readable.txt
      minutes.md
```

`paths.py` modes: **Standalone** (`.standalone` marker) outputs to `~/Documents/WhisperSync/transcriptions`. **Repo mode** outputs to `<repo>/meetings/local-transcriptions`.

## Conventions

- **Logging**: `from .logger import logger` then `logger.info(...)`. Prefix with `[WhisperSync]` only in verbose tier.
- **Config changes**: `config.save(cfg)` only. Never write config files directly.
- **Worker comms**: Request/response queues with `request_id`. No shared state.
- **Worker errors**: Catch exceptions, send `{type: "error", ...}`. Never let exceptions kill silently.
- **Icons**: Generated in `icons.py`. No external image/font assets.
- **Warning suppression**: Scoped only. Must specify `message=`, `category=`, and/or `module=`.
- **Commits**: `fix(whisper-sync):`, `feat(whisper-sync):`, `ci:`, `docs:` prefixes.
- **Text style**: No em dash characters. Use single hyphens for asides.
- **Author**: `--author="Pendentive <pendentive.info@gmail.com>"` for all commits.
- **PRs**: Always wait for Copilot review before merging. Update docs in the same PR.
- **Merging**: NEVER merge manually (gh pr merge, GitHub UI). All merges go through auto-merge workflow after Copilot review. No exceptions without explicit user approval in conversation first. See `.github/governance/policy.yaml`.
- **Spec first**: Use superpowers:brainstorming to define spec, get user approval, then implement via PR.

## Guardrails

### Do NOT
- Merge PRs manually (policy violation - logged by review-logger)
- Modify `config.defaults.json` without updating `_VALID_KEYS` in `config.py`
- Add blanket warning suppressions
- Change multiprocessing context from "spawn"
- Introduce shared mutable state between processes
- Add external image/font assets
- Delete `streaming_wav.py` crash safety without understanding the failure mode
- Change hotkey registration without testing with tray icon
- Commit `config.json`, `logs/`, `models/`, `whisper-env/`, `__pycache__/`
- Store user data outside `.whispersync/`

### Always
- Test both dictation AND meeting modes after transcribe.py/worker.py changes
- Check tray icon responsiveness after __main__.py changes
- Verify GPU memory stability after multiple cycles

## File Ownership

| Files | Source of truth |
|-------|-----------------|
| `.py`, `.ps1`, `.txt`, `.md`, `.ico`, `config.defaults.json` | `pendentive/whisper-sync` |
| `CLAUDE.md`, `.github/`, `docs/plans/` | pendentive only |
| `config.json`, `logs/`, `models/` | Per-machine (gitignored) |

## Dependencies

whisperX (PyTorch+CUDA), keyboard, pystray, sounddevice, PyAudioWPatch, pyperclip, Pillow, windows-toasts, Claude CLI (optional).
