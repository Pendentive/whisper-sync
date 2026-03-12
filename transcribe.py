"""Abstracted transcription backend — in-process whisperX with persistent models."""

import json
import os
import threading
from pathlib import Path

import numpy as np

from . import config
from .paths import get_model_cache, is_standalone

# Local model cache — keeps models offline
_MODEL_CACHE = get_model_cache()

# Point all model caches here — works offline
os.environ["HF_HUB_CACHE"] = str(_MODEL_CACHE)
os.environ["TORCH_HOME"] = str(_MODEL_CACHE / "torch")

_lock = threading.Lock()
_models = {}  # Cache: {"large-v3:float16:cuda": model, ...}
_align_model = None
_align_metadata = None
_diarize_pipeline = None
_last_device = None


def _get_device():
    import torch
    return "cuda" if torch.cuda.is_available() else "cpu"


def _check_device_changed():
    """If GPU availability changed (e.g. eco mode), clear cached models."""
    global _last_device, _models, _align_model, _align_metadata, _diarize_pipeline
    device = _get_device()
    if _last_device is not None and device != _last_device:
        print(f"[WhisperSync] GPU changed: {_last_device} → {device}, reloading models...")
        _models.clear()
        _align_model = None
        _align_metadata = None
        _diarize_pipeline = None
    _last_device = device
    return device


def _load_whisper_model(model_name: str, compute_type: str, language: str):
    """Load or return cached whisperX model."""
    global _models
    device = _check_device_changed()
    # CPU doesn't support float16
    effective_compute = "int8" if device == "cpu" and compute_type == "float16" else compute_type
    key = f"{model_name}:{effective_compute}:{device}"
    if key not in _models:
        import whisperx
        print(f"[WhisperSync] Loading model {model_name} ({effective_compute}) on {device}...")
        _models[key] = whisperx.load_model(
            model_name, device=device, compute_type=effective_compute, language=language,
            download_root=str(_MODEL_CACHE),
        )
        print(f"[WhisperSync] Model {model_name} loaded on {device}")
    return _models[key]


def _load_align_model(language: str):
    """Load or return cached alignment model."""
    global _align_model, _align_metadata
    if _align_model is None:
        import whisperx
        print("[WhisperSync] Loading alignment model...")
        _align_model, _align_metadata = whisperx.load_align_model(
            language_code=language, device=_get_device(),
        )
        print("[WhisperSync] Alignment model loaded")
    return _align_model, _align_metadata


_PYANNOTE_ACCEPT_URLS = [
    "https://huggingface.co/pyannote/segmentation-3.0",
    "https://huggingface.co/pyannote/speaker-diarization-3.1",
]


def _load_diarize_pipeline():
    """Load or return cached diarization pipeline."""
    global _diarize_pipeline
    if _diarize_pipeline is None:
        from whisperx.diarize import DiarizationPipeline
        hf_token_path = Path.home() / ".huggingface" / "token"
        if not hf_token_path.exists():
            hint = "See README.md for setup instructions." if is_standalone() else "Run: pm-get-secret hugging-face_read"
            raise FileNotFoundError(
                f"HF token not found at {hf_token_path}. {hint}"
            )
        token = hf_token_path.read_text().strip()
        print("[WhisperSync] Loading diarization model...")
        try:
            _diarize_pipeline = DiarizationPipeline(token=token, device=_get_device())
        except Exception as e:
            err_str = str(e).lower()
            # Detect gated model access errors (403 Forbidden, access restricted)
            if "403" in err_str or "gated" in err_str or "access" in err_str or "restricted" in err_str:
                urls = "\n  ".join(_PYANNOTE_ACCEPT_URLS)
                raise PermissionError(
                    f"Diarization model access denied. You must accept the license terms.\n"
                    f"Visit BOTH of these pages and click 'Agree':\n  {urls}\n"
                    f"Then restart WhisperSync."
                ) from e
            raise
        print("[WhisperSync] Diarization model loaded")
    return _diarize_pipeline


def preload(model_name: str = None, compute_type: str = None, language: str = None):
    """Preload models at startup so first transcription is fast."""
    cfg = config.load()
    model_name = model_name or cfg.get("dictation_model", cfg["model"])
    compute_type = compute_type or cfg["compute_type"]
    language = language or cfg["language"]

    with _lock:
        _load_whisper_model(model_name, compute_type, language)
        _load_align_model(language)


def transcribe_fast(audio_np: np.ndarray, model_override: str = None) -> str:
    """Fast dictation path — transcribe numpy audio, skip alignment and file I/O.

    Args:
        audio_np: Raw audio as float32 numpy array (16kHz mono).
        model_override: Use a specific model instead of config default.

    Returns:
        Transcribed text string.
    """
    import whisperx

    cfg = config.load()
    language = cfg["language"]
    compute_type = cfg["compute_type"]
    model_name = model_override or cfg["model"]

    with _lock:
        model = _load_whisper_model(model_name, compute_type, language)

    # Convert to the exact format whisperx.load_audio() returns:
    # 1D contiguous float32 numpy array, normalized to [-1, 1]
    if audio_np.dtype == np.int16:
        audio_np = audio_np.astype(np.float32) / 32768.0
    audio_np = np.ascontiguousarray(audio_np.flatten(), dtype=np.float32)

    print(f"[WhisperSync] Transcribing fast ({model_name})...")
    result = model.transcribe(audio_np, batch_size=16, language=language)

    return " ".join(seg.get("text", "") for seg in result.get("segments", [])).strip()


def transcribe(audio_path: str, diarize: bool = False, model_override: str = None) -> dict:
    """Transcribe audio file using in-process whisperX.

    Args:
        audio_path: Path to WAV file.
        diarize: If True, run speaker diarization.
        model_override: Use a specific model instead of config default.

    Returns:
        dict with keys:
            'text': full transcript string
            'segments': list of {speaker, start, end, text} (if diarize=True)
            'json_path': path to raw whisperX JSON output (if diarize=True)
    """
    import whisperx

    cfg = config.load()
    language = cfg["language"]
    compute_type = cfg["compute_type"]
    model_name = model_override or cfg["model"]

    with _lock:
        model = _load_whisper_model(model_name, compute_type, language)
        align_model, align_metadata = _load_align_model(language)

    # Load audio
    audio = whisperx.load_audio(audio_path)

    # Transcribe
    print(f"[WhisperSync] Transcribing ({model_name})...")
    result = model.transcribe(audio, batch_size=16, language=language)

    # Align
    print("[WhisperSync] Aligning...")
    result = whisperx.align(
        result["segments"], align_model, align_metadata, audio, _get_device(),
        return_char_alignments=False,
    )

    # Diarize if requested
    if diarize:
        with _lock:
            pipeline = _load_diarize_pipeline()
        print("[WhisperSync] Diarizing...")
        diarize_segments = pipeline(audio_path)
        result = whisperx.assign_word_speakers(diarize_segments, result)

    # Build output
    text = " ".join(seg.get("text", "") for seg in result.get("segments", []))
    output = {"text": text.strip()}

    if diarize:
        # Save JSON next to the audio file
        audio_p = Path(audio_path)
        json_path = audio_p.parent / "transcript.json"
        with open(json_path, "w") as f:
            json.dump(result, f, indent=2, default=str)

        output["json_path"] = str(json_path)
        output["segments"] = [
            {
                "speaker": seg.get("speaker", "UNKNOWN"),
                "start": seg.get("start", 0),
                "end": seg.get("end", 0),
                "text": seg.get("text", "").strip(),
            }
            for seg in result.get("segments", [])
        ]

    return output
