# Development Guide

## Prerequisites

| Requirement | Details |
|-------------|---------|
| **Python** | 3.11 or newer (3.13 recommended) |
| **NVIDIA GPU** | With CUDA support (RTX 20/30/40/50 series, GTX 10-series) |
| **NVIDIA Drivers** | Up to date -- run `nvidia-smi` to verify |
| **Git** | For cloning and contributing |
| **Windows** | 10 or 11 (WASAPI audio capture is Windows-only) |

## Clone and Install from Source

```powershell
git clone https://github.com/pendentive/whisper-sync.git
cd whisper-sync
powershell -ExecutionPolicy Bypass -File install.ps1
```

The installer handles:
- GPU detection and CUDA version selection
- Virtual environment creation (`whisper-env/`)
- Dependency installation (whisperX, PyTorch with CUDA, sounddevice, pystray, etc.)
- PyTorch CUDA override (whisperX pulls CPU-only torch by default)
- Base model download (~225 MB for tiny + base)

## Running from Source

```powershell
# Using the launcher script (recommended -- handles orphan cleanup):
powershell -ExecutionPolicy Bypass -File start.ps1

# Or directly via Python:
.\whisper-env\Scripts\python.exe -m whisper_sync

# With the watchdog (auto-restart on crash):
powershell -ExecutionPolicy Bypass -File start.ps1 -Watchdog
```

When running without the `.standalone` marker file, WhisperSync operates in **repo mode**: transcription output goes to `<repo-root>/meetings/local-transcriptions/` instead of `~/Documents/WhisperSync/transcriptions/`.

## Configuration

### config.defaults.json vs config.json

WhisperSync uses a three-tier config system:

1. **`whisper_sync/config.defaults.json`** -- Shipped defaults. Checked into git. Never modify this for personal settings.
2. **`whisper_sync/config.json`** -- User overrides. Gitignored. Created automatically when you change settings via the tray menu. Only contains keys you have changed.
3. **Runtime merge** -- `config.load()` deep-merges defaults + overrides. User values win.

To reset all settings, delete `config.json`. To override a specific value, add just that key:

```json
{
  "batch_size": 8,
  "output_dir": "D:\\Meetings\\transcriptions"
}
```

Key settings: `model`, `dictation_model`, `output_dir`, `hotkeys`, `paste_method`, `batch_size` (`"auto"` for GPU-adaptive), `left_click`, `middle_click`.

Only keys listed in `_VALID_KEYS` in `config.py` are persisted. Adding a new config key requires updating both `config.defaults.json` and `_VALID_KEYS`.

## Sync Hook (ic-product-mgmt Maintainers)

WhisperSync source files are authored in the `ic-product-mgmt` repo under `scripts/whisper_sync/` and synced to this repo via a post-commit hook. The sync direction is one-way:

```
ic-product-mgmt (authoritative for .py, .ps1, config.defaults.json)
    → post-commit hook
        → copies to pendentive/whisper-sync
        → auto-commits with matching message
```

Files that are authoritative in this repo only (not synced back):
- `CLAUDE.md` -- agent instructions specific to this repo
- `.github/` -- CI workflows, issue templates
- `docs/plans/` -- design documents
- `README.md`, `CONTRIBUTING.md`, `docs/development.md`

If you are editing from `ic-product-mgmt`, your changes will appear here automatically on commit. If you edit directly in this repo, ensure the change is also reflected in `ic-product-mgmt` or it will be overwritten on the next sync.

## Debugging

### Log Files

Logs are written to `whisper_sync/logs/app/`:

```
whisper_sync/logs/app/whisper-sync-2026-03-20.log
```

- **File log level**: DEBUG (everything)
- **Console log level**: INFO (user-facing messages only)

Log rotation is daily. Each day gets a new file.

### Verbose Output

Run directly from the terminal to see real-time console output:

```powershell
.\whisper-env\Scripts\python.exe -m whisper_sync
```

All `[WhisperSync]`-prefixed messages in the console are user-facing. DEBUG-level messages (model loading details, queue operations, timing) appear only in the log file.

### GPU Monitoring

Monitor VRAM usage during transcription:

```powershell
# One-shot check:
nvidia-smi

# Continuous monitoring (updates every 2 seconds):
nvidia-smi -l 2

# Just VRAM usage:
nvidia-smi --query-gpu=memory.used,memory.total --format=csv
```

Expected behavior:
- **Idle**: VRAM allocated for preloaded model (~1-8 GB depending on model size)
- **Dictation**: brief VRAM spike during transcription, returns to baseline
- **Meeting**: sustained VRAM usage during transcription stages, returns to baseline after completion

If VRAM grows continuously across multiple dictations, there is a memory leak.

## Common Issues

### CUDA Out of Memory (OOM)

**Symptom**: `torch.cuda.OutOfMemoryError` during transcription, especially on long recordings.

**Cause**: Batch size too large for available VRAM, or VRAM fragmented from previous operations.

**Fix**:
1. WhisperSync has automatic OOM recovery (catch, empty cache, retry at half batch size, up to 3 retries)
2. If OOM persists, reduce batch size manually in `config.json`:
   ```json
   { "batch_size": 4 }
   ```
3. Use a smaller model for the failing mode (e.g., `medium` instead of `large-v3`)
4. Close other GPU-intensive applications (games, other ML workloads)

The automatic VRAM-tier sizing:
- 8 GB or less VRAM: `batch_size=4`
- 8-12 GB: `batch_size=8`
- 12 GB+: `batch_size=16`

### Orphan Worker Processes

**Symptom**: GPU memory stays allocated after WhisperSync exits. `nvidia-smi` shows a python.exe process using VRAM.

**Cause**: On Windows, `multiprocessing.spawn` workers can survive after the parent process dies.

**Fix**:
1. `start.ps1` automatically kills orphans on every launch (three detection methods: parent PID match, venv path match, dead parent detection)
2. Manual cleanup:
   ```powershell
   # Find python processes using GPU:
   nvidia-smi --query-compute-apps=pid,name --format=csv
   # Kill by PID:
   taskkill /F /PID <pid>
   ```

### File Locks on WAV Files

**Symptom**: "Permission denied" or "file in use" errors when trying to move or delete a recording.

**Cause**: The streaming WAV writer or audio capture still has the file open, or Windows indexing service has locked it.

**Fix**:
1. Wait a few seconds after transcription completes for file handles to close
2. Check if a worker process is still running (see orphan cleanup above)
3. If the file was from a crash, `streaming_wav.fix_orphan(path)` can repair the WAV header without needing the original process

### PyTorch Using CPU Instead of GPU

**Symptom**: Transcription is 5-10x slower than expected. Tray menu shows "CUDA: No".

**Cause**: The CUDA-enabled PyTorch was not installed, or was overwritten by a `pip install` that pulled the CPU version.

**Fix**:
```powershell
# Check current state:
.\whisper-env\Scripts\python.exe -c "import torch; print(torch.cuda.is_available())"

# Reinstall CUDA PyTorch (cu121 for RTX 20/30/40):
.\whisper-env\Scripts\pip.exe install torch torchaudio --index-url https://download.pytorch.org/whl/cu121 --force-reinstall --no-deps
```

See the CUDA Version table in README.md for your GPU family's correct index URL.
