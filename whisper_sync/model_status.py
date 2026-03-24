"""Check and download whisperX model + alignment dependencies."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .logger import logger
from .paths import get_model_cache

_VENV_SCRIPTS = Path(sys.executable).parent

# Local model cache (resolved by paths.py for repo vs standalone mode)
_HF_CACHE = get_model_cache()
_TORCH_CACHE = _HF_CACHE / "torch" / "hub" / "checkpoints"

# Map config model names to HF repo IDs
_MODEL_REPOS = {
    "large-v3": "Systran--faster-whisper-large-v3",
    "large-v2": "Systran--faster-whisper-large-v2",
    "medium": "Systran--faster-whisper-medium",
    "small": "Systran--faster-whisper-small",
    "base": "Systran--faster-whisper-base",
    "tiny": "Systran--faster-whisper-tiny",
}

_ALIGNMENT_MODEL = "wav2vec2_fairseq_base_ls960_asr_ls960.pth"

# Models that auto-download without prompting (small enough for any connection)
_AUTO_DOWNLOAD_MODELS = {"tiny", "base"}
# Models that require user confirmation (large downloads, risky on mobile data)
_PROMPT_DOWNLOAD_MODELS = {"large-v3", "large-v2", "medium", "small"}


def is_model_cached(model_name: str) -> bool:
    """Check if a whisper model is already in the local cache."""
    repo_id = _MODEL_REPOS.get(model_name)
    if not repo_id:
        return False
    model_dir = _HF_CACHE / f"models--{repo_id}"
    return model_dir.exists()


def bootstrap_models(cfg: dict, on_large_model=None):
    """Ensure required models are cached locally.

    Auto-downloads tiny + base silently.
    For large models (large-v3 etc), calls on_large_model(name, size_str)
    which should return True to proceed or False to skip.
    """
    needed = set()

    # Always ensure tiny + base are available
    for name in _AUTO_DOWNLOAD_MODELS:
        if not is_model_cached(name):
            needed.add(name)

    # Check configured models
    meeting_model = cfg.get("model", "large-v3")
    dictation_model = cfg.get("dictation_model", meeting_model)

    for model_name in {meeting_model, dictation_model}:
        if not is_model_cached(model_name):
            needed.add(model_name)

    if not needed:
        return

    # Download auto models silently
    auto = needed & _AUTO_DOWNLOAD_MODELS
    prompt = needed - _AUTO_DOWNLOAD_MODELS

    if auto:
        logger.info(f"Auto-downloading base models: {', '.join(sorted(auto))}...")
        for name in sorted(auto):
            _download_whisper_model(name, silent=True)

    # Prompt for large models
    for name in sorted(prompt):
        size = _estimate_download_size(name)
        if on_large_model:
            if on_large_model(name, size):
                _download_whisper_model(name)
            else:
                logger.info(f"Skipped {name} download")
        else:
            logger.info(f"Model {name} ({size}) not cached -- download via Settings menu")

    # Ensure alignment model (wav2vec2) is cached — needed for word timing
    _bootstrap_alignment_model()


def _estimate_download_size(model_name: str) -> str:
    """Return human-readable download size estimate."""
    sizes = {
        "tiny": "~75 MB",
        "base": "~150 MB",
        "small": "~500 MB",
        "medium": "~1.5 GB",
        "large-v2": "~3 GB",
        "large-v3": "~3 GB",
    }
    return sizes.get(model_name, "unknown size")


def _bootstrap_alignment_model():
    """Download the wav2vec2 alignment model if not already cached.

    This model provides word-level timing accuracy. Without it, the first
    meeting transcription triggers a download, which is a bad first-run experience.
    """
    alignment_path = _TORCH_CACHE / _ALIGNMENT_MODEL
    if alignment_path.exists():
        return
    try:
        logger.info("Downloading word timing model (wav2vec2)...")
        import whisperx
        # Loading the alignment model triggers the download
        whisperx.load_align_model(language_code="en", device="cpu")
        logger.info("Word timing model cached successfully")
    except Exception as e:
        logger.warning(f"Failed to download alignment model: {e}")
        logger.warning("Word timing will download on first use instead")


def _download_whisper_model(model_name: str, silent: bool = False):
    """Download a whisper model by loading it once via whisperx.

    Args:
        model_name: Model identifier (e.g. "large-v3", "tiny").
        silent: If True, skip toast notifications (used for auto-download of small models).
    """
    from .notifications import notify

    size = _estimate_download_size(model_name)
    if not silent:
        notify("Downloading model", f"{model_name} ({size}), please wait...")

    try:
        import whisperx
        logger.info(f"Downloading {model_name}...")
        whisperx.load_model(
            model_name, device="cpu", compute_type="int8", language="en",
            download_root=str(_HF_CACHE),
        )
        logger.info(f"{model_name} cached successfully")
        if not silent:
            notify("Model downloaded", f"{model_name} is ready to use")
    except Exception as e:
        logger.error(f"Failed to download {model_name}: {e}")
        if not silent:
            notify("Download failed", f"{model_name}: {e}")


def get_model_status(model_name: str) -> dict:
    """Check if whisperX model and alignment model are downloaded.

    Returns dict with:
        'model_downloaded': bool
        'alignment_downloaded': bool
        'model_path': str or None
        'model_size': str (human readable)
        'cuda_available': bool
        'cuda_device': str or None
    """
    repo_id = _MODEL_REPOS.get(model_name)
    model_dir = _HF_CACHE / f"models--{repo_id}" if repo_id else None
    model_downloaded = model_dir is not None and model_dir.exists()

    alignment_path = _TORCH_CACHE / _ALIGNMENT_MODEL
    alignment_downloaded = alignment_path.exists()

    # Get model size on disk
    model_size = ""
    if model_downloaded and model_dir:
        total = sum(f.stat().st_size for f in model_dir.rglob("*") if f.is_file())
        if total > 1_000_000_000:
            model_size = f"{total / 1_000_000_000:.1f} GB"
        else:
            model_size = f"{total / 1_000_000:.0f} MB"

    # Check CUDA (in-process)
    cuda_available = False
    cuda_device = None
    try:
        import torch
        cuda_available = torch.cuda.is_available()
        cuda_device = torch.cuda.get_device_name(0) if cuda_available else None
    except Exception:
        pass

    return {
        "model_downloaded": model_downloaded,
        "alignment_downloaded": alignment_downloaded,
        "model_path": str(model_dir) if model_dir else None,
        "model_size": model_size,
        "cuda_available": cuda_available,
        "cuda_device": cuda_device,
    }


def download_model(model_name: str) -> bool:
    """Download whisperX model by running a short transcription of silence.

    Returns True on success.
    """
    from .notifications import notify

    import tempfile
    import wave
    import numpy as np

    size = _estimate_download_size(model_name)
    notify("Downloading model", f"{model_name} ({size}), please wait...")

    # Create a 1-second silent WAV
    fd, tmp_wav_str = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    tmp_wav = Path(tmp_wav_str)
    tmp_out = Path(tempfile.mkdtemp())
    try:
        silence = np.zeros(16000, dtype=np.int16)
        with wave.open(str(tmp_wav), "w") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(silence.tobytes())

        whisperx_bin = shutil.which("whisperx", path=str(_VENV_SCRIPTS)) or "whisperx"
        proc = subprocess.run(
            [whisperx_bin, str(tmp_wav),
             "--model", model_name,
             "--language", "en",
             "--compute_type", "float16",
             "--output_format", "json",
             "--output_dir", str(tmp_out)],
            capture_output=True, text=True, timeout=600,
        )
        success = proc.returncode == 0
        if success:
            notify("Model downloaded", f"{model_name} is ready to use")
        else:
            notify("Download failed", f"{model_name} download did not complete")
        return success
    except Exception:
        logger.exception(f"Model download failed for {model_name}")
        notify("Download failed", f"{model_name} download encountered an error")
        return False
    finally:
        tmp_wav.unlink(missing_ok=True)
        for f in tmp_out.glob("*"):
            f.unlink(missing_ok=True)
        tmp_out.rmdir()
