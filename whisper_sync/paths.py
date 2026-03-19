"""Central path resolution — standalone vs repo mode detection."""

from pathlib import Path

_PKG_DIR = Path(__file__).parent
_STANDALONE = (_PKG_DIR / ".standalone").exists()


def is_standalone() -> bool:
    """True when running from a distributed zip (not inside the git repo)."""
    return _STANDALONE


def get_install_root() -> Path:
    """Root directory for resolving relative paths.

    Repo mode:  two levels up (scripts/whisper_sync/ → repo root).
    Standalone: the package directory itself.
    """
    if _STANDALONE:
        return _PKG_DIR
    return _PKG_DIR.parent.parent


def get_model_cache() -> Path:
    """Directory for cached whisperX / HF models.

    Both modes: whisper_sync/models/
    """
    p = _PKG_DIR / "models"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_default_output_dir() -> Path:
    """Sensible default when output_dir is relative.

    Repo mode:  <repo_root>/meetings/local-transcriptions
    Standalone: ~/Documents/WhisperSync/transcriptions
    """
    if _STANDALONE:
        return Path.home() / "Documents" / "WhisperSync" / "transcriptions"
    return get_install_root() / "meetings" / "local-transcriptions"
