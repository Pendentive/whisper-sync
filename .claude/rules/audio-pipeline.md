# Audio Pipeline Rules

## Recording Architecture

WhisperSync captures stereo audio: microphone on channel 0, system loopback (speaker output) on channel 1. The `AudioRecorder` class in `capture.py` handles both streams via sounddevice (mic) and PyAudioWPatch (WASAPI loopback). Channel health is tracked and reflected in the tray icon's outer ring.

## Diarization Strategy (3-tier fallback)

`transcribe.py` `stage_diarize()` uses a tiered approach for speaker identification:

1. **Tier 1 - Per-channel transcription + confidence fusion**: For stereo recordings, each channel is transcribed independently via the full pipeline (transcribe, align, diarize), then merged using energy ratio, timestamp overlap, and text similarity in `channel_merge.py`. Quality is validated before accepting results.
2. **Tier 2 - Balanced mono + PyAnnote**: `_create_balanced_mono()` normalizes each channel's voiced RMS energy independently before mixing, so PyAnnote can hear both speakers equally. Used when Tier 1 fails quality checks.
3. **Tier 3 - Raw audio + PyAnnote**: Falls back to the original audio file if balanced mono creation fails.

## Model Loading and VRAM Management

- Models are cached in a global dict keyed by `"{model}:{compute_type}:{device}"`. Reloading only happens on device change or explicit reload request.
- CPU mode forces `int8` compute type (float16 not supported on CPU).
- `_check_device_changed()` clears all cached models and calls `torch.cuda.empty_cache()` when switching from GPU.
- Base batch size is determined by VRAM tier: <=8GB=4, <=12GB=8, >12GB=16, CPU=16.
- `_compute_batch_size()` further reduces batch size for long audio (>60s halves, >180s quarters).
- `_transcribe_with_retry()` catches OOM errors and retries at half batch size, up to 3 times.

## Worker Subprocess Model

`worker.py` runs all whisperX/CTranslate2/CUDA operations in an isolated subprocess spawned via `multiprocessing.spawn` context. This is required because:

- CTranslate2 and CUDA can segfault, killing only the worker while the main process survives.
- `worker_manager.py` (`TranscriptionWorker`) detects crashes and automatically respawns.
- Communication uses request/response queues with `request_id` for correlation.
- Between meeting pipeline stages, `_drain_priority()` processes pending dictation requests, allowing dictation during meeting transcription.

## Backup Model Lifecycle

- The backup model pre-loads on meeting start (background thread, CPU, ~1s load time).
- Once loaded, the backup model stays in memory until the app closes. There is no unload timer.
- `backup_device` is always CPU unless explicitly overridden to GPU in config.
- Default backup_model is `base`. The installer can override to `small` or `tiny` based on detected VRAM during installation. No runtime auto-selection.
- Model merging: dictation and meeting share the primary model instance. The backup model is always a separate instance.

## Speaker Identification Recovery

`identify_speakers()` calls `claude -p --model haiku` with a 90s timeout and 1 retry. If both attempts fail, `step_speaker_id` in `meeting_job.py` builds a stub with empty names and still shows `_ask_speaker_confirmation()` so the user can enter names manually. A toast notification fires before the dialog to indicate that automatic speaker identification failed and that names can be entered manually.

The **Meetings** tray menu (above Recent Dictations) shows the 10 most recent meetings with their speaker status. Clicking any meeting re-enters the speaker ID flow: `identify_speakers()` -> `_ask_speaker_confirmation()` -> `write_speaker_map()` -> `flatten()` -> optionally regenerate minutes. This handles recovery from timeouts, accidental skips, and retroactive speaker assignment.

## Critical Rules

- **Never change the multiprocessing context from "spawn"** - required for CUDA isolation.
- **Never introduce shared mutable state between main and worker processes** - use queues only.
- **Never block the main thread with transcription** - all transcription runs in the worker subprocess or on background threads.
- **Always test both dictation AND meeting modes** after any change to transcribe.py, worker.py, or channel_merge.py.
- **Verify GPU memory stability** after multiple dictation/meeting cycles (check with `nvidia-smi`).
