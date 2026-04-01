# Audio Pipeline Rules

## Recording Architecture

WhisperSync captures stereo audio: microphone on channel 0, system loopback (speaker output) on channel 1. The `AudioRecorder` class in `capture.py` handles both streams via sounddevice (mic) and PyAudioWPatch (WASAPI loopback). Channel health is tracked and reflected in the tray icon's outer ring.

## Diarization Strategy (configurable method order)

`transcribe.py` `stage_diarize()` uses a configurable fallback chain for speaker identification. The order is controlled by three config keys: `diarize_primary`, `diarize_fallback`, `diarize_last_resort`. Each method returns a result on success or `None` to trigger the next method.

**Available methods** (defined in `DIARIZE_METHODS`):

1. **Balanced Mix** (`balanced_mix`): `_create_balanced_mono()` normalizes each channel's voiced RMS energy independently before mixing to mono, so PyAnnote can hear all speakers equally. Best for meetings with 3+ remote participants on loopback setups. Default primary.
2. **Per-Channel** (`per_channel`): Each stereo channel is transcribed independently via the full pipeline (transcribe, align, diarize), then merged using energy ratio, timestamp overlap, and text similarity in `channel_merge.py`. Quality is validated before accepting results. Best for dual-mic setups with one local speaker. Default fallback.
3. **Raw Audio** (`raw_audio`): PyAnnote diarization on the original audio file without preprocessing. Default last resort.

**Per-meeting override**: The Save Meeting dialog includes a diarization method selector. When a non-default method is chosen, it is passed through `MeetingJob.diarize_method` -> `worker_manager.transcribe(diarize_method=)` -> `worker.py` -> `stage_diarize(force_method=)`, bypassing the fallback chain.

**Settings UI**: Settings > Diarization (Speaker Detection) exposes Primary, Fallback, and Last Resort slots. Each slot shows a submenu of available methods. Selecting a method already assigned to another slot triggers a swap to enforce uniqueness.

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

## Critical Rules

- **Never change the multiprocessing context from "spawn"** - required for CUDA isolation.
- **Never introduce shared mutable state between main and worker processes** - use queues only.
- **Never block the main thread with transcription** - all transcription runs in the worker subprocess or on background threads.
- **Always test both dictation AND meeting modes** after any change to transcribe.py, worker.py, or channel_merge.py.
- **Verify GPU memory stability** after multiple dictation/meeting cycles (check with `nvidia-smi`).
