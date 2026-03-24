"""Lightweight backup transcriber for dictation during meetings.

When the main worker subprocess is busy with meeting transcription,
this module provides a fallback path using a smaller model loaded
directly in the main process. It runs in a background thread (not a
separate process) since dictation audio is short (5-30s) and
transcription completes in 1-3s on CPU or <1s on GPU with a small model.

The model is loaded lazily on first use and kept in memory for
subsequent calls. Call unload() to free it explicitly.

NOTE: WhisperX is loaded in the main process thread for simplicity.
The backup model is small and transcription is brief (~1-3s). If
stability issues arise, migrate to a subprocess model.
"""

import threading

import numpy as np

from .logger import logger

# Approximate VRAM usage per model in GB (float16 on GPU)
MODEL_VRAM_GB = {
    "tiny": 1.0,
    "base": 1.0,
    "small": 2.0,
    "medium": 4.0,
    "large-v2": 3.0,
    "large-v3": 3.0,
}

VRAM_THRESHOLD = 0.80  # warn if combined usage exceeds 80% of total


class BackupTranscriber:
    """Lightweight backup transcriber for dictation during meetings.

    Loads lazily on first use. Runs in the calling thread (main process).
    Uses a smaller model on CPU or GPU depending on config.
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._model = None
        self._device = None
        self._compute_type = None
        self._model_name = None
        self._loading = False
        self._lock = threading.Lock()

    @property
    def is_loading(self) -> bool:
        return self._loading

    def is_enabled(self) -> bool:
        return self.cfg.get("always_available_dictation", True)

    def preload(self):
        """Pre-load backup model in background thread. Called when meeting starts."""
        if self._model is not None or self._loading:
            return

        def _do_preload():
            self._loading = True
            try:
                self._load()
            finally:
                self._loading = False

        threading.Thread(target=_do_preload, daemon=True, name="backup-preload").start()

    @property
    def device(self) -> str:
        """Current device string, or 'not loaded' if model is not loaded."""
        return self._device or "not loaded"

    def transcribe(self, audio_np: np.ndarray) -> str:
        """Transcribe audio using the backup model. Loads model on first call.

        Args:
            audio_np: Raw audio as float32 or int16 numpy array (16kHz mono).

        Returns:
            Transcribed text. Raises on failure (caller handles).
        """
        with self._lock:
            if self._model is None:
                self._load()

            import whisperx

            # Normalize audio to the format whisperx expects
            if audio_np.dtype == np.int16:
                audio_np = audio_np.astype(np.float32) / 32768.0
            audio_np = np.ascontiguousarray(audio_np.flatten(), dtype=np.float32)

            language = self.cfg.get("language", "en")
            batch_size = 8 if self._device == "cpu" else 16

            logger.info(
                f"Backup transcribe [{self._device}] {self._model_name} "
                f"(batch={batch_size})..."
            )
            result = self._model.transcribe(
                audio_np, batch_size=batch_size, language=language
            )
            text = " ".join(
                seg.get("text", "") for seg in result.get("segments", [])
            ).strip()
            return text

    def _load(self):
        """Lazily load the backup model."""
        import whisperx

        model_name = self.cfg.get("backup_model", "base")
        device = self._resolve_device()
        compute_type = "int8" if device == "cpu" else self.cfg.get("compute_type", "float16")

        from .paths import get_model_cache
        model_cache = get_model_cache()

        logger.info(f"Backup model loading [{device}] {model_name} ({compute_type})...")
        self._model = whisperx.load_model(
            model_name,
            device=device,
            compute_type=compute_type,
            language=self.cfg.get("language", "en"),
            download_root=str(model_cache),
        )
        self._device = device
        self._compute_type = compute_type
        self._model_name = model_name
        logger.info(f"Backup model ready [{device}] {model_name}")

    def _resolve_device(self) -> str:
        """Determine device for backup model. Always CPU unless explicitly overridden."""
        backup_device = self.cfg.get("backup_device", "cpu")

        if backup_device in ("gpu", "cuda"):
            main_device = self.cfg.get("device", "auto")
            if main_device != "cpu":
                logger.warning("Backup model on GPU while main model also on GPU - may cause VRAM pressure")
            return "cuda"

        return "cpu"

    def unload(self):
        """Free the backup model from memory."""
        with self._lock:
            if self._model is not None:
                logger.info("Unloading backup model")
                self._model = None
                self._device = None
                self._compute_type = None
                self._model_name = None
                # Try to free GPU memory
                try:
                    import torch
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception:
                    pass

    def needs_reload(self) -> bool:
        """Check if config changed and model needs reloading."""
        if self._model is None:
            return False
        return (
            self._model_name != self.cfg.get("backup_model", "base")
            or self._device != self._resolve_device()
        )

    def reload_if_needed(self):
        """Reload the backup model if config has changed."""
        if self.needs_reload():
            self.unload()
            # Will lazy-load on next transcribe()

    def get_vram_warning(self, primary_model: str, backup_model: str) -> str | None:
        """Check if primary + backup would exceed 80% VRAM.

        Returns warning string or None if OK.
        """
        try:
            import torch
            if not torch.cuda.is_available():
                return None  # no GPU, no VRAM concern

            total_vram = torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
            primary_vram = MODEL_VRAM_GB.get(primary_model, 3.0)
            backup_vram = MODEL_VRAM_GB.get(backup_model, 1.0)
            combined = primary_vram + backup_vram
            threshold = total_vram * VRAM_THRESHOLD

            if combined > threshold:
                backup_device = self.cfg.get("backup_device", "auto")
                if backup_device == "auto":
                    advice = "Backup will use CPU in auto mode."
                else:
                    advice = "Consider switching to a smaller model or CPU to avoid OOM."
                return (
                    f"{primary_model} + {backup_model} need ~{combined:.1f} GB VRAM "
                    f"({total_vram:.1f} GB total, {threshold:.1f} GB safe limit). "
                    f"{advice}"
                )
        except Exception:
            pass
        return None
