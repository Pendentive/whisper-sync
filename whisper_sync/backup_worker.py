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
