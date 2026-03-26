# Technical Reference (Developer & AI Assistant Guide)

Everything below is for developers modifying this app or for an AI assistant (like Claude) troubleshooting it on behalf of a user. If you're just using WhisperSync, see [README.md](../README.md).

---

## Complete File Reference

### Python Files (whisper_sync/)

| File | Purpose | Key Classes/Functions |
|------|---------|----------------------|
| `__main__.py` | **App entry point.** Tray icon, hotkey listener, recording lifecycle, crash recovery. | `WhisperSync` class --`run()`, `_on_hotkey()`, `_start_dictation()`, `_start_meeting()`, `_do_transcribe()`, `_paste_result()`, `_recover_from_crash()` |
| `capture.py` | **Audio recording.** Multi-stream mic + speaker loopback via WASAPI. | `AudioRecorder` class --`start()`, `stop()`, `_mic_callback()`, `_speaker_callback()`, `start_streaming()`, `stop_streaming()` |
| `transcribe.py` | **WhisperX engine.** Model loading, transcription, alignment, diarization. Two paths: fast (dictation) and full (meeting). | `transcribe_fast(audio_np, model)` -- in-memory numpy to text; `transcribe(audio_path, diarize, model)` -- file-based full pipeline; `_load_model()`, `_load_align_model()` |
| `worker.py` | **Subprocess entry point.** Runs transcription in isolated process (crash safety). Receives requests via Queue, returns results. | `worker_main()` -- event loop handling `transcribe_fast`, `transcribe`, `reload_model`, `shutdown` requests |
| `worker_manager.py` | **Subprocess lifecycle.** Spawns/kills/restarts worker process. IPC via multiprocessing.Queue. Crash detection + respawn. | `TranscriptionWorker` class --`start()`, `wait_ready()`, `transcribe_fast()`, `transcribe()`, `reload_model()`, `restart()`, `is_alive()` |
| `streaming_wav.py` | **Crash-safe WAV writer.** Writes audio incrementally during recording. Can recover orphaned files after crash. | `StreamingWavWriter` class --`write()`, `close()`, `_write_header()`; `fix_orphan(path)` -- validates + rewrites header; `cleanup_temp_files(dir)` |
| `config.py` | **Config manager.** Loads `config.defaults.json`, deep-merges with user `config.json` overrides. | `load()` returns merged dict; `save(overrides)` writes config.json |
| `paths.py` | **Path resolver.** Repo mode path resolution, model cache dir, output dir. | `get_install_root()`, `get_model_cache()`, `get_default_output_dir()` |
| `model_status.py` | **Model management.** Download, cache validation, bootstrap (auto-download tiny+base, prompt for larger). | `get_model_status(name)` returns bool; `download_model(name)` downloads to HF cache; `bootstrap_models(config)` -- first-run setup |
| `icons.py` | **Tray icon generator.** Creates colored 64x64 PNG circles with labels. No external assets needed. | `_circle_icon(color, label)` returns PIL Image |
| `paste.py` | **Text output.** Routes transcribed text to clipboard+Ctrl+V or simulated keystrokes. | `paste(text, method)`, `paste_clipboard(text)`, `paste_keystrokes(text)` |
| `flatten.py` | **Transcript converter.** JSON to readable speaker-attributed text (~90% token reduction). | `flatten(transcript_path)` writes `transcript-readable.txt`; `PAUSE_THRESHOLD = 2.0` seconds |
| `dictation_log.py` | **Dictation history.** Appends entries to daily markdown log files. | `append(text, duration)` writes to `<output_dir>/.whispersync/dictation-logs/YYYY-MM-DD.md` |
| `crash_diagnostics.py` | **Crash safety.** Global exception hook + Windows Event Log query for recent python.exe crashes. | `install_excepthook()` -- registers handler; `check_previous_crash()` returns str or None |
| `watchdog.py` | **Auto-restart daemon.** Monitors main process, respawns on non-zero exit. Gives up after 5 rapid crashes. | `main()` -- loop with `MAX_RESTARTS=5`, `COOLDOWN_SECONDS=5`, `RESET_AFTER_SECONDS=300` |
| `logger.py` | **Logging setup.** File-based logging to `logs/app/whisper-sync-YYYY-MM-DD.log`. DEBUG to file, INFO to console. | `logger` singleton |
| `benchmark.py` | **Performance testing.** Tests all downloaded models in both meeting and dictation modes. | `python -m whisper_sync.benchmark [wav_path]` |
| `__init__.py` | Package marker. Empty. | --|

### Non-Python Files

| File | Location | Purpose |
|------|----------|---------|
| `config.defaults.json` | `whisper_sync/` | Default configuration. Merged under user overrides. |
| `config.json` | `<output_dir>/.whispersync/` (primary), `whisper_sync/` (legacy fallback) | User overrides. Created on first settings change. |
| `whisper-capture.ico` | `whisper_sync/` | Application icon (64x64). Used in Windows shortcuts. |
| `requirements.txt` | Top level | Pip dependencies. |
| `install.ps1` | Top level | CLI installer. |
| `start.ps1` | Top level | Launcher script. |
| `build-dist.ps1` | Top level (dev only) | Builds distribution zip. |
| `setup-env.ps1` | Top level (dev only) | Dev environment bootstrap. |

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
│   └── [spawns subprocess] -> worker.py
│       ├── transcribe.py (WhisperX engine)
│       │   └── model_status.py (model cache)
│       └── logger.py
├── dictation_log.py (history)
└── flatten.py (post-processing)

watchdog.py (optional wrapper)
└── [spawns] -> __main__.py

benchmark.py (standalone utility)
└── transcribe.py
```

### Dictation Flow

```
User presses Ctrl+Shift+Space
  -> __main__._on_hotkey("dictation")
  -> __main__._start_dictation()
    -> capture.AudioRecorder.start() [mic only, records to numpy array]
    -> User presses hotkey again
    -> capture.AudioRecorder.stop() -> numpy audio data
    -> worker_manager.transcribe_fast(audio_np)
      -> saves numpy to temp .npy file
      -> sends request to worker subprocess via Queue
      -> worker.py receives request
        -> transcribe.transcribe_fast(audio_np) [WhisperX, no alignment]
        -> returns text via Queue
    -> paste.paste(text, method) -> clipboard + Ctrl+V
    -> dictation_log.append(text, duration)
    -> icon -> green (done) -> gray (idle)
```

### Meeting Flow

```
User presses Ctrl+Shift+M
  -> __main__._on_hotkey("meeting")
  -> __main__._start_meeting()
    -> Prompts for meeting name (popup dialog)
    -> capture.AudioRecorder.start() [mic + speaker loopback]
      -> streaming_wav.StreamingWavWriter [incremental disk writes]
    -> User presses hotkey again
    -> capture.AudioRecorder.stop() -> WAV file on disk
    -> worker_manager.transcribe(audio_path, diarize=True)
      -> sends request to worker subprocess via Queue
      -> worker.py receives request
        -> transcribe.transcribe(audio_path, diarize=True)
          -> Step 1: Load whisperX model
          -> Step 2: Transcribe audio -> raw segments
          -> Step 3: Load alignment model + align timestamps
          -> Step 4: Load diarization model + assign speakers
          -> Step 5: Write transcript.json
        -> returns result via Queue
    -> icon -> green (done) -> gray (idle)
```

### Subprocess Architecture

The transcription engine runs in a **separate subprocess** (`worker.py`) for crash isolation. GPU operations (especially CUDA) can segfault -- a segfault in the worker doesn't kill the main UI process.

```
Main Process (__main__.py)          Worker Process (worker.py)
├── UI thread (pystray)             ├── Transcription engine
├── Hotkey thread (keyboard)        ├── WhisperX models in GPU memory
├── Audio capture                   └── Receives requests via Queue
└── IPC via multiprocessing.Queue
    ├── request_q: main -> worker
    └── response_q: worker -> main
```

- Each request has a unique `request_id` to match responses
- Priority queue: dictation requests can interrupt pending meeting stages
- Worker sends `{"type": "ready"}` after model preload completes
- If worker dies (segfault), `worker_manager` detects via `is_alive()` and can respawn

### GPU Memory Resilience

Three layers prevent CUDA out-of-memory crashes:

1. **VRAM-tier batch sizing** -- detects total GPU memory and sets safe `batch_size` (4/8/16 based on VRAM)
2. **Audio-length reduction** -- long recordings get smaller batch_size (<60s: base, 60-180s: half, >180s: quarter)
3. **OOM catch-and-retry** -- catches CUDA OOM, runs `torch.cuda.empty_cache()`, retries at half batch_size (up to 3 retries)

Override with `"batch_size": 8` in `config.json`. Set `"batch_size": "auto"` to re-enable adaptive sizing.

### Meeting Recording -- Disk-Only Audio

Meeting recordings use **disk-only** audio capture for the mic channel. The mic callback streams audio to a WAV file on disk in real-time and does NOT accumulate audio in RAM (controlled by the `disk_only` flag in `start_streaming()`). The speaker loopback channel, however, still accumulates audio in RAM via `_speaker_data` for resampling and stereo merging at stop time. Dictation mode uses RAM for both channels (recordings are short).

### Orphan Worker Cleanup

On Windows, `multiprocessing.spawn` workers can survive after the parent process dies. The `start.ps1` script kills orphans on every launch using parent PID matching and dead-parent detection.

---

## Dependency Deep Dive

### Why PyTorch Needs `--force-reinstall --no-deps`

WhisperX's `pip install whisperx` pulls in a **CPU-only** version of PyTorch. For GPU acceleration, you must override it:

```powershell
pip install torch torchaudio --index-url https://download.pytorch.org/whl/{cudaVersion} --force-reinstall --no-deps
```

### CUDA Version Mapping

| GPU Family | CUDA Version | PyTorch Index |
|------------|:---:|---|
| RTX 50-series (Blackwell) | cu128 | `https://download.pytorch.org/whl/cu128` |
| RTX 20/30/40-series, A-series, L-series | cu121 | `https://download.pytorch.org/whl/cu121` |
| GTX 10-series, GTX 9-series | cu118 | `https://download.pytorch.org/whl/cu118` |
| No GPU / unknown | CPU | `https://download.pytorch.org/whl/cpu` |

### Full Dependency Tree

```
whisperx (speech-to-text)
├── faster-whisper (CTranslate2 backend)
├── torch (CPU version -- MUST be overridden with CUDA version)
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

After initial installation and model download, WhisperSync works fully offline. All models cached in `whisper_sync/models/` (the app sets `HF_HUB_CACHE` to this directory). No network calls during transcription.

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
- `mic_device` / `speaker_device`: `null` = system default
- `use_system_devices`: When `true`, ignores manual device selections and follows Windows defaults

Config merge: `config.defaults.json` loaded first, then user `config.json` deep-merged on top. The app checks `<output_dir>/.whispersync/config.json` first, then falls back to the legacy `whisper_sync/config.json`. If the config file is corrupted, the app crashes on startup. Fix: delete the config.json file to reset.

---

## Model Details

| Model | Download | Disk | VRAM | Purpose |
|-------|:---:|:---:|:---:|---------|
| whisper-tiny | ~39 MB | ~75 MB | ~1 GB | Fast dictation |
| whisper-base | ~140 MB | ~150 MB | ~1 GB | Default dictation |
| whisper-small | ~466 MB | ~500 MB | ~2 GB | Balanced |
| whisper-medium | ~1.5 GB | ~1.5 GB | ~4 GB | High accuracy |
| whisper-large-v3 | ~2.9 GB | ~3 GB | ~8 GB | Best (recommended) |
| wav2vec2-conformer | ~360 MB | ~400 MB | --| Word-level alignment |
| segmentation-3.0 | ~360 MB | ~400 MB | --| Speaker segmentation (gated) |
| speaker-diarization-3.1 | ~17 MB | ~20 MB | --| Speaker pipeline config (gated) |

Models are stored in `whisper_sync/models/` (the app sets `HF_HUB_CACHE` to this directory). Gated models (pyannote) require HF account + license acceptance.

---

## Log File Locations

| Log Type | Path | Content |
|----------|------|---------|
| App log | `whisper_sync/logs/app/whisper-sync-YYYY-MM-DD.log` | DEBUG-level application log |
| Dictation log | `<output_dir>/.whispersync/dictation-logs/YYYY-MM-DD.md` | Timestamped dictation history |
| Crash log | Windows Event Log (Application) | Native crashes (segfaults) |

---

## Troubleshooting Playbook (For AI Assistants)

### Quick Diagnostic Commands

```powershell
# 1. Check Python version
python --version

# 2. Check PyTorch and CUDA
.\whisper-env\Scripts\python.exe -c "import torch; print(f'PyTorch: {torch.__version__}, CUDA: {torch.cuda.is_available()}, Device: {torch.cuda.get_device_name(0) if torch.cuda.is_available() else \"N/A\"}')"

# 3. Check whisperX import
.\whisper-env\Scripts\python.exe -c "import whisperx; print('whisperX OK')"

# 4. Check audio devices
.\whisper-env\Scripts\python.exe -c "import sounddevice; print(sounddevice.query_devices())"

# 5. Check cached models
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

# 9. Check recent logs
Get-ChildItem "whisper_sync\logs\app\" | Sort-Object LastWriteTime -Descending | Select-Object -First 3
```

### Decision Trees

**App Won't Start:**
1. `python` not recognized -> Install Python 3.10+, check "Add to PATH"
2. `No module named 'whisper_sync'` -> Run from the whisper-sync directory
3. `ModuleNotFoundError` -> Run `install.ps1` or `pip install -r requirements.txt`
4. CUDA errors -> Reinstall PyTorch with correct CUDA version (see mapping above)
5. PermissionError -> Run as Administrator
6. Silent crash -> Check log files, run `python -m whisper_sync` directly
7. Invalid JSON -> Delete `<output_dir>/.whispersync/config.json` to reset

**Transcription Fails:**
1. Magenta icon immediately -> Model not downloaded, use `download_model()`
2. Long wait then error -> Timeout, use smaller model or verify GPU usage
3. Gibberish output -> Set language to `en`, try larger model
4. Worker segfault -> GPU driver issue, try smaller model or CPU mode
5. Empty result -> Audio not captured, check mic devices

**Diarization Fails:**
1. 401 Unauthorized -> HF token missing or expired
2. 403 Forbidden -> Accept license on both pyannote model pages
3. No module `pyannote` -> Reinstall whisperx
4. All segments SPEAKER_00 -> Single speaker detected or poor audio quality

### How to Reset

```powershell
# Delete venv (models stay cached globally)
Remove-Item -Recurse -Force .\whisper-env\

# Reset config (path depends on your output_dir setting)
Remove-Item "<output_dir>\.whispersync\config.json" -ErrorAction SilentlyContinue

# Reinstall
powershell -ExecutionPolicy Bypass -File install.ps1
```

To also clear models: `Remove-Item -Recurse -Force ".\whisper_sync\models\*"`

### Key Design Decisions

1. **Subprocess isolation**: Transcription in child process because CUDA segfaults would otherwise kill the entire app
2. **Streaming WAV**: Audio written to disk incrementally; crash recovery can salvage partial recordings
3. **PyTorch override**: whisperX pulls CPU-only torch; installer forces CUDA torch
4. **Config merge**: Defaults + user overrides, deep merged; user config.json only contains changed keys
5. **Path resolution**: All paths resolve relative to the repo root (two levels up from `whisper_sync/`)

### Important Constants

- Dictation timeout: 60 seconds (`worker_manager.py`)
- Meeting timeout: 600 seconds (`worker_manager.py`)
- Worker ready timeout: 120 seconds (`worker_manager.py`)
- Crash recovery: WAV must be >= 5 seconds (`streaming_wav.py`)
- Watchdog: 5 max restarts, 5s cooldown, 300s stability reset (`watchdog.py`)
- Pause threshold for paragraph breaks: 2.0 seconds (`flatten.py`)
