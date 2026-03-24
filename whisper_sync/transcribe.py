"""Abstracted transcription backend — in-process whisperX with persistent models."""

import json
import os
import threading
from pathlib import Path

import numpy as np

from . import config
from .logger import logger
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
_base_batch_size = None  # Set on first use via _get_base_batch_size()


def _get_base_batch_size() -> int:
    """Determine base batch_size from available GPU VRAM.

    Tiers:
        CPU:    16  (system RAM, not constrained)
        ≤8GB:   4   (~3GB model + limited headroom)
        ≤12GB:  8   (~3GB model + moderate headroom)
        >12GB:  16  (plenty of room)
    """
    global _base_batch_size
    if _base_batch_size is not None:
        return _base_batch_size

    device = _get_device()
    if device == "cpu":
        _base_batch_size = 16  # CPU uses system RAM, not VRAM -- no memory constraint
        logger.info(f"CPU mode -- base batch_size={_base_batch_size}")
        return _base_batch_size

    import torch
    props = torch.cuda.get_device_properties(0)
    total_gb = props.total_memory / (1024 ** 3)
    if total_gb <= 8:
        _base_batch_size = 4
    elif total_gb <= 12:
        _base_batch_size = 8
    else:
        _base_batch_size = 16

    logger.info(f"GPU: {props.name} ({total_gb:.1f} GB) -- base batch_size={_base_batch_size}")
    return _base_batch_size


def _compute_batch_size(audio_np: np.ndarray, base: int) -> int:
    """Reduce batch_size for long audio to prevent OOM.

    Args:
        audio_np: Audio array at 16kHz.
        base: Base batch_size from VRAM tier.

    Returns:
        Adjusted batch_size.
    """
    duration_s = len(audio_np) / 16000  # 16kHz sample rate
    if duration_s > 180:
        return max(1, base // 4)
    elif duration_s > 60:
        return max(2, base // 2)
    return base


def _transcribe_with_retry(model, audio, batch_size: int, language: str,
                           max_retries: int = 3) -> dict:
    """Transcribe with OOM catch-and-retry at decreasing batch sizes."""
    device = _get_device()
    for attempt in range(max_retries + 1):
        try:
            return model.transcribe(audio, batch_size=batch_size, language=language)
        except RuntimeError as e:
            if "out of memory" not in str(e).lower() or attempt == max_retries:
                raise
            if device == "cuda":
                import torch
                torch.cuda.empty_cache()
            batch_size = max(1, batch_size // 2)
            logger.warning(f"OOM -- retrying with batch_size={batch_size}")
    raise RuntimeError("Exhausted OOM retries")


def _resolve_batch_size(audio_np: np.ndarray) -> int:
    """Get the effective batch_size: config override or adaptive."""
    cfg = config.load()
    cfg_batch = cfg.get("batch_size", "auto")
    if cfg_batch != "auto":
        return int(cfg_batch)
    base = _get_base_batch_size()
    return _compute_batch_size(audio_np, base)


def _get_device():
    """Resolve device from config: auto/gpu/cuda -> 'cuda' or 'cpu', cpu -> 'cpu'."""
    cfg = config.load()
    device_pref = cfg.get("device", "auto").lower()

    if device_pref == "cpu":
        return "cpu"

    import torch
    if device_pref in ("gpu", "cuda"):
        if not torch.cuda.is_available():
            logger.warning("GPU requested but CUDA not available -- falling back to CPU")
            return "cpu"
        return "cuda"

    # auto: detect GPU, fall back to CPU
    return "cuda" if torch.cuda.is_available() else "cpu"


def get_gpu_name() -> str | None:
    """Return the GPU device name, or None if CUDA is not available."""
    try:
        import torch
        if torch.cuda.is_available():
            return torch.cuda.get_device_name(0)
    except Exception:
        pass
    return None


def _check_device_changed():
    """If device changed (config switch or GPU availability), clear cached models."""
    global _last_device, _models, _align_model, _align_metadata, _diarize_pipeline, _base_batch_size
    device = _get_device()
    if _last_device is not None and device != _last_device:
        logger.info(f"Device changed: {_last_device} -> {device}, reloading models...")
        # Clean up CUDA memory when switching FROM GPU
        if _last_device == "cuda":
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                    logger.info("CUDA memory cache cleared")
            except Exception:
                pass
        _models.clear()
        _align_model = None
        _align_metadata = None
        _diarize_pipeline = None
        _base_batch_size = None  # Reset so batch size is re-computed for new device
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
        logger.info(f"Loading [{device}] {model_name} ({effective_compute})...")
        _models[key] = whisperx.load_model(
            model_name, device=device, compute_type=effective_compute, language=language,
            download_root=str(_MODEL_CACHE),
        )
        logger.info(f"Loaded [{device}] {model_name}")
    return _models[key]


def _load_align_model(language: str):
    """Load or return cached alignment model."""
    global _align_model, _align_metadata
    if _align_model is None:
        import whisperx
        device = _get_device()
        logger.info(f"Loading [{device}] alignment model...")
        _align_model, _align_metadata = whisperx.load_align_model(
            language_code=language, device=device,
        )
        logger.info(f"Loaded [{device}] alignment model")
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
        device = _get_device()
        logger.info(f"Loading [{device}] diarization model...")
        try:
            _diarize_pipeline = DiarizationPipeline(token=token, device=device)
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
        logger.info(f"Loaded [{device}] diarization model")
    return _diarize_pipeline


def preload(model_name: str = None, compute_type: str = None, language: str = None):
    """Preload models at startup so first transcription is fast."""
    cfg = config.load()
    model_name = model_name or cfg.get("dictation_model", cfg["model"])
    compute_type = compute_type or cfg["compute_type"]
    language = language or cfg["language"]

    # Detect GPU and set adaptive batch_size early so it's logged at startup
    _get_base_batch_size()

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

    batch = _resolve_batch_size(audio_np)
    logger.info(f"Transcribing fast [{_get_device()}] {model_name} (batch={batch})...")
    result = _transcribe_with_retry(model, audio_np, batch, language)

    return " ".join(seg.get("text", "") for seg in result.get("segments", [])).strip()


def transcribe(audio_path: str, diarize: bool = False, model_override: str = None) -> dict:
    """Transcribe audio file using in-process whisperX (monolithic, for backward compat).

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
    ctx = stage_prepare(audio_path, model_override)
    result = stage_transcribe(ctx)
    result = stage_align(ctx, result)
    diarize_segments = stage_diarize(ctx) if diarize else None
    return stage_finalize(ctx, result, diarize_segments)


# --- Staged pipeline (used by worker for inter-stage priority checks) ---

def stage_prepare(audio_path: str, model_override: str = None) -> dict:
    """Load audio and models. Returns context dict for subsequent stages."""
    import os
    import whisperx

    cfg = config.load()
    language = cfg["language"]
    compute_type = cfg["compute_type"]
    model_name = model_override or cfg["model"]

    # Normalize path for Windows — ffmpeg (used by diarization) needs native separators
    audio_path = os.path.normpath(audio_path)

    with _lock:
        model = _load_whisper_model(model_name, compute_type, language)
        align_model, align_metadata = _load_align_model(language)

    audio = whisperx.load_audio(audio_path)

    return {
        "audio": audio,
        "audio_path": audio_path,
        "model": model,
        "align_model": align_model,
        "align_metadata": align_metadata,
        "language": language,
        "model_name": model_name,
    }


def stage_transcribe(ctx: dict) -> dict:
    """Stage 1: Transcribe audio → segments. The longest stage."""
    batch = _resolve_batch_size(ctx["audio"])
    logger.info(f"Transcribing [{_get_device()}] {ctx['model_name']} (batch={batch})...")
    return _transcribe_with_retry(ctx["model"], ctx["audio"], batch, ctx["language"])


def stage_align(ctx: dict, result: dict) -> dict:
    """Stage 2: Align segments → word-level timestamps."""
    import whisperx
    logger.info("Aligning...")
    return whisperx.align(
        result["segments"], ctx["align_model"], ctx["align_metadata"],
        ctx["audio"], _get_device(), return_char_alignments=False,
    )


def _create_balanced_mono(audio_path: str) -> str | None:
    """Create an RMS-balanced mono WAV for diarization from stereo input.

    Normalizes each channel's speech energy independently before mixing,
    so PyAnnote can hear both speakers equally. Returns path to a temp
    mono WAV, or None if audio is mono or channels are identical.
    """
    import wave as _wave
    import tempfile

    try:
        with _wave.open(audio_path, "rb") as wf:
            n_channels = wf.getnchannels()
            if n_channels < 2:
                return None
            sample_rate = wf.getframerate()
            raw = wf.readframes(wf.getnframes())

        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float64)
        ch0 = samples[0::2]
        ch1 = samples[1::2]

        def voiced_rms(channel, frame_ms=30, voiced_percentile=70):
            frame_size = int(sample_rate * frame_ms / 1000)
            n_frames = len(channel) // frame_size
            if n_frames == 0:
                rms = np.sqrt(np.mean(channel ** 2))
                return rms if rms > 0 else 1.0
            truncated = channel[:n_frames * frame_size].reshape(n_frames, frame_size)
            frame_energies = np.sqrt(np.mean(truncated ** 2, axis=1))
            threshold = np.percentile(frame_energies, 100 - voiced_percentile)
            voiced = frame_energies[frame_energies >= threshold]
            return float(np.mean(voiced)) if len(voiced) > 0 else 1.0

        rms0 = voiced_rms(ch0)
        rms1 = voiced_rms(ch1)

        if min(rms0, rms1) / max(rms0, rms1) > 0.95:
            return None

        target_rms = max(rms0, rms1)
        gain0 = target_rms / rms0 if rms0 > 0 else 1.0
        gain1 = target_rms / rms1 if rms1 > 0 else 1.0

        logger.info(f"Stereo balance: ch0 gain={gain0:.2f}x, ch1 gain={gain1:.2f}x "
                     f"(RMS: {rms0:.0f}/{rms1:.0f})")

        mono = (ch0 * gain0 + ch1 * gain1) / 2.0
        mono = np.clip(mono, -32768, 32767).astype(np.int16)

        fd, tmp_path = tempfile.mkstemp(suffix=".wav")
        try:
            with _wave.open(tmp_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                wf.writeframes(mono.tobytes())
        except Exception:
            os.unlink(tmp_path)
            raise
        finally:
            os.close(fd)

        return tmp_path

    except Exception as e:
        logger.warning(f"Stereo balancing failed, using original: {e}")
        return None


def stage_diarize(ctx: dict) -> dict:
    """Stage 3: Run speaker diarization pipeline.

    For stereo recordings: per-channel transcription + confidence fusion (Tier 1).
    Falls back to balanced mono with PyAnnote model (Tier 2), then raw audio
    with PyAnnote model (Tier 3).
    """
    from .channel_merge import is_stereo, split_channels, load_channel_audio, merge_channel_results

    audio_path = ctx["audio_path"]

    # Check if stereo
    if not is_stereo(audio_path):
        # Mono: standard model-based diarization
        with _lock:
            pipeline = _load_diarize_pipeline()
        logger.info("Diarizing (mono)...")
        return pipeline(audio_path)

    # Tier 1: Per-channel transcription + confidence fusion
    logger.info("Stereo detected, running per-channel pipeline...")
    ch0_path = ch1_path = None
    try:
        ch0_path, ch1_path = split_channels(audio_path)
        ch0_audio, ch1_audio, sr = load_channel_audio(audio_path)

        # Get duration
        import wave as _wave
        with _wave.open(audio_path, "rb") as wf:
            duration = wf.getnframes() / wf.getframerate()

        # Run full pipeline on ch0
        logger.info("Processing channel 0 (mic)...")
        ctx_ch0 = stage_prepare(ch0_path, model_override=ctx.get("model_name"))
        result_ch0 = stage_transcribe(ctx_ch0)
        result_ch0 = stage_align(ctx_ch0, result_ch0)
        with _lock:
            pipeline = _load_diarize_pipeline()
        diarize_ch0 = pipeline(ch0_path)
        import whisperx
        result_ch0 = whisperx.assign_word_speakers(diarize_ch0, result_ch0)

        # Run full pipeline on ch1
        logger.info("Processing channel 1 (remote)...")
        ctx_ch1 = stage_prepare(ch1_path, model_override=ctx.get("model_name"))
        result_ch1 = stage_transcribe(ctx_ch1)
        result_ch1 = stage_align(ctx_ch1, result_ch1)
        diarize_ch1 = pipeline(ch1_path)
        result_ch1 = whisperx.assign_word_speakers(diarize_ch1, result_ch1)

        # Merge
        segments_ch0 = result_ch0.get("segments", [])
        segments_ch1 = result_ch1.get("segments", [])
        merged, quality_ok = merge_channel_results(
            segments_ch0, segments_ch1, ch0_audio, ch1_audio, sr, duration
        )

        if quality_ok:
            # Store merged segments in ctx for stage_finalize to use
            ctx["_per_channel_segments"] = merged
            # _per_channel_segments consumed by stage_finalize
            # Return a dummy diarize result; stage_finalize will use _per_channel_segments
            return diarize_ch0  # not actually used when _per_channel_segments is set
        else:
            logger.warning("Per-channel quality check failed, falling back to channel diarization")

    except Exception as e:
        logger.warning("Per-channel pipeline failed", exc_info=True)

    finally:
        # Clean up temp files
        for p in (ch0_path, ch1_path):
            if p and os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    # Tier 2: Balanced mono + PyAnnote model
    balanced_path = _create_balanced_mono(audio_path)
    diarize_path = balanced_path or audio_path
    try:
        with _lock:
            pipeline = _load_diarize_pipeline()
        logger.info("Diarizing (balanced mono fallback)...")
        return pipeline(diarize_path)
    finally:
        if balanced_path and os.path.exists(balanced_path):
            try:
                os.unlink(balanced_path)
            except OSError:
                pass


def stage_finalize(ctx: dict, result: dict, diarize_segments=None) -> dict:
    """Stage 4: Assign speakers, save JSON, build output dict."""
    import whisperx

    # Check if per-channel pipeline already produced merged segments
    per_channel = ctx.get("_per_channel_segments")
    if per_channel is not None:
        result["segments"] = per_channel
    elif diarize_segments is not None:
        result = whisperx.assign_word_speakers(diarize_segments, result)

    text = " ".join(seg.get("text", "") for seg in result.get("segments", []))
    output = {"text": text.strip()}

    if diarize_segments is not None:
        audio_p = Path(ctx["audio_path"])
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
