# Backup Dictation Subprocess Implementation Plan

> Status: SHIPPED (2026-03; backup_worker.py, always-available dictation). Committed 2026-07-03 (previously an untracked draft).

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the crashing in-process BackupTranscriber with a subprocess-based worker that can safely load CTranslate2 models.

**Architecture:** Spawn a second TranscriptionWorker subprocess (CPU, small model) on first meeting start. The backup worker uses the same worker_main entry point and queue protocol as the primary worker. Main process never imports torch/CTranslate2.

**Tech Stack:** Python multiprocessing (spawn context), existing TranscriptionWorker, existing worker_main

**Spec:** `docs/specs/2026-03-24-backup-subprocess-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `whisper_sync/backup_worker.py` | Rewrite | BackupTranscriber wraps TranscriptionWorker for CPU |
| `whisper_sync/__main__.py` | Modify | Wire overlay dictation to backup subprocess |

No changes to `worker_manager.py` or `worker.py`.

---

### Task 1: Rewrite BackupTranscriber as subprocess wrapper

**Files:**
- Rewrite: `whisper_sync/backup_worker.py`

- [ ] **Step 1: Replace BackupTranscriber class**

Replace the entire class with:

```python
"""Backup transcription worker for dictation during meetings.

Spawns a second TranscriptionWorker subprocess on CPU with a smaller model.
The subprocess uses the same worker_main entry point as the primary worker.
Main process never imports torch/CTranslate2 (avoids segfaults).
"""

import threading

import numpy as np

from .logger import logger
from . import config


class BackupTranscriber:
    """Manages a backup transcription subprocess for dictation during meetings.

    Spawned on first meeting start. Stays alive until app closes.
    Uses TranscriptionWorker (same as primary) but configured for CPU.
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._worker = None
        self._spawning = False
        self._spawn_lock = threading.Lock()

    def preload(self):
        """Spawn backup subprocess and pre-load model. Called on meeting start.

        Idempotent: does nothing if already spawned or spawning.
        Runs spawn in a background thread so it doesn't block meeting start.
        """
        if self._worker is not None or self._spawning:
            return

        def _do_spawn():
            with self._spawn_lock:
                if self._worker is not None:
                    return
                self._spawning = True
                try:
                    from .worker_manager import TranscriptionWorker

                    backup_model = self.cfg.get("backup_model", "base")
                    backup_cfg = {**self.cfg}
                    backup_cfg["device"] = "cpu"
                    backup_cfg["model"] = backup_model
                    backup_cfg["compute_type"] = "int8"

                    logger.info(f"Spawning backup worker (CPU, {backup_model})...")
                    worker = TranscriptionWorker(backup_cfg, preload_model=backup_model)
                    worker.start()

                    if worker.wait_ready(timeout=30):
                        self._worker = worker
                        logger.info(f"Backup worker ready (CPU, {backup_model})")
                    else:
                        logger.warning("Backup worker failed to start within 30s")
                        worker.stop()
                finally:
                    self._spawning = False

        threading.Thread(target=_do_spawn, daemon=True, name="backup-spawn").start()

    @property
    def is_loading(self) -> bool:
        """True while subprocess is spawning or model is loading."""
        return self._spawning

    @property
    def is_ready(self) -> bool:
        """True when backup worker is alive and model is loaded."""
        return self._worker is not None and self._worker.is_ready()

    def transcribe(self, audio_np: np.ndarray) -> str:
        """Transcribe audio using the backup subprocess.

        Sends a transcribe_fast request to the backup worker.
        Raises RuntimeError if backup worker is not available.
        """
        if self._worker is None:
            raise RuntimeError("Backup worker not started")
        if not self._worker.is_ready():
            raise RuntimeError("Backup worker not ready")

        return self._worker.transcribe_fast(audio_np)

    def stop(self):
        """Shut down the backup subprocess."""
        if self._worker is not None:
            logger.info("Stopping backup worker...")
            self._worker.stop()
            self._worker = None

    @staticmethod
    def is_enabled(cfg: dict = None) -> bool:
        """Check if always-available dictation is enabled."""
        if cfg is None:
            cfg = config.load()
        return cfg.get("always_available_dictation", True)
```

- [ ] **Step 2: Remove old imports and constants**

Remove any remaining `MODEL_VRAM_GB`, `VRAM_THRESHOLD`, `get_vram_warning`, `faster_whisper`, or `whisperx` imports that are no longer needed. The backup_worker.py should only import from `.logger`, `.config`, `.worker_manager`, `threading`, and `numpy`.

- [ ] **Step 3: Commit**

```bash
git add whisper_sync/backup_worker.py
git commit -m "feat: rewrite BackupTranscriber as subprocess wrapper"
```

---

### Task 2: Wire overlay dictation to backup subprocess

**Files:**
- Modify: `whisper_sync/__main__.py`

- [ ] **Step 1: Update _stop_overlay_dictation to use subprocess**

Find `_stop_overlay_dictation()`. The current code calls `self._backup.transcribe(audio_np)` which was the in-process call. This should now work unchanged because the new `BackupTranscriber.transcribe()` delegates to the subprocess. But verify:

1. The audio_np is passed correctly (float32 or int16)
2. The fallback path (when backup fails) still works - it should queue on the primary worker
3. The `RuntimeError` from `transcribe()` is caught and triggers the fallback

If the fallback catch is missing, add:

```python
try:
    text = self._backup.transcribe(audio_np)
except Exception as e:
    logger.warning(f"Backup transcription failed, falling back to primary: {e}")
    # Queue on primary worker instead
    text = self._worker.transcribe_fast(audio_np)
```

- [ ] **Step 2: Verify preload call in _start_meeting**

Confirm that `_start_meeting()` still calls `self._backup.preload()`. This was added in the previous PR and should still be there.

- [ ] **Step 3: Verify debounce in toggle_dictation**

Confirm that `toggle_dictation()` checks `self._backup.is_loading` before starting overlay dictation. This was added in the previous PR.

- [ ] **Step 4: Update app shutdown**

Find the app shutdown/cleanup code. Add `self._backup.stop()` to ensure the backup subprocess is terminated on app close. Look for where `self._worker.stop()` is called (primary worker shutdown) and add the backup stop nearby.

- [ ] **Step 5: Commit**

```bash
git add whisper_sync/__main__.py
git commit -m "feat: wire overlay dictation to backup subprocess"
```

---

### Task 3: Push PR and verify

- [ ] **Step 1: Push and create PR**

```bash
git push -u origin fix/backup-vram-debounce-icon
gh pr create --base dev --title "feat: backup dictation via subprocess (fixes segfault)" --body "..."
```

- [ ] **Step 2: Wait for Copilot review, address suggestions**

- [ ] **Step 3: Verify merge**
