# Backup Dictation Subprocess Design

> Status: SHIPPED (2026-03; backup_worker.py). Committed 2026-07-03 (previously an untracked draft).

> **Date**: 2026-03-24
> **Status**: Approved
> **Scope**: Replace in-process BackupTranscriber with subprocess-based worker

## Problem

CTranslate2 cannot initialize in a process that already has a worker subprocess with its own CTranslate2 context. Both whisperx.load_model and faster_whisper.WhisperModel segfault when loaded in the main process. The backup model must run in its own subprocess.

## Architecture

```
Main process (UI, hotkeys, tray - never imports torch/CTranslate2)
  |
  +-- Primary worker subprocess (GPU, large-v3, meetings + normal dictation)
  |     request_queue / response_queue
  |
  +-- Backup worker subprocess (CPU, small, dictation-during-meeting only)
        backup_request_queue / backup_response_queue
        Spawned on first meeting start. Stays alive until app closes.
```

Both workers use the same `worker_main()` entry point from `worker.py`. The only difference is the config snapshot: backup gets `device=cpu`, `model=backup_model`, `compute_type=int8`.

## Lifecycle

1. App starts - only primary worker spawns
2. First meeting starts - backup subprocess spawns, pre-loads backup model
3. Dictation hotkey during meeting - request to backup worker queue
4. Backup worker transcribes on CPU, returns text via response queue
5. Meeting ends - backup worker stays alive
6. Next meeting - backup worker already ready
7. App closes - both workers shut down

## File changes

| File | Change |
|------|--------|
| `backup_worker.py` | Replace BackupTranscriber. New class wraps TranscriptionWorker configured for CPU. |
| `__main__.py` | Overlay dictation sends transcribe_fast to backup worker via queue. |
| `worker_manager.py` | No changes. Already supports configurable model/device. |
| `worker.py` | No changes. Already handles transcribe_fast. |

## BackupTranscriber rewrite

Thin wrapper around TranscriptionWorker:

- `preload()`: Spawn subprocess with CPU config, pre-load model. Idempotent.
- `is_loading`: True while spawning or model loading in subprocess.
- `transcribe(audio_np)`: Send transcribe_fast to subprocess, return text.
- `stop()`: Shut down subprocess.

Config snapshot for backup worker:
- `device`: "cpu"
- `model`: value of `backup_model` config (default "base")
- `compute_type`: "int8"
- All other config inherited from main config

## Error handling

- Backup worker crash: fall back to queueing on primary worker
- Timeout (>10s): fall back to primary worker
- Yellow flash during spawning/model load (existing)
- `is_loading` checks both spawning flag and worker ready state

## What this does NOT change

- Primary worker architecture (unchanged)
- worker.py / worker_main (unchanged)
- TranscriptionWorker class (unchanged)
- Icon behavior (unchanged, already implemented)
- Yellow flash (unchanged, already implemented)
- Debounce (unchanged, already implemented)
