"""Central path resolution -- standalone vs repo mode detection."""

import json
from pathlib import Path

_PKG_DIR = Path(__file__).parent
_STANDALONE = (_PKG_DIR / ".standalone").exists()


def is_standalone() -> bool:
    """True when running from a distributed zip (not inside the git repo)."""
    return _STANDALONE


def get_install_root() -> Path:
    """Root directory for resolving relative paths.

    Repo mode:  two levels up (scripts/whisper_sync/ -> repo root).
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


# ---------------------------------------------------------------------------
# User data directory -- output_dir/.whispersync/
# ---------------------------------------------------------------------------

def _resolve_output_dir() -> Path:
    """Resolve output_dir using a two-phase bootstrap.

    Phase 1: Try output_dir/.whispersync/config.json (new location).
    Phase 2: Fall back to whisper_sync/config.json (legacy location).
    Phase 3: Use defaults from config.defaults.json.

    Returns the resolved absolute output_dir.
    """
    defaults_path = _PKG_DIR / "config.defaults.json"
    with open(defaults_path) as f:
        defaults = json.load(f)

    # Helper to resolve a potentially-relative output_dir string
    def _resolve(raw: str) -> Path:
        p = Path(raw)
        if p.is_absolute():
            return p
        if _STANDALONE:
            return Path.home() / "Documents" / "WhisperSync" / p
        return get_install_root() / p

    # Phase 1: Check if we already know the output_dir from legacy config
    legacy_config = _PKG_DIR / "config.json"
    output_dir_str = defaults.get("output_dir", "transcriptions")

    if legacy_config.exists():
        try:
            with open(legacy_config) as f:
                legacy = json.load(f)
            if legacy and "output_dir" in legacy:
                output_dir_str = legacy["output_dir"]
        except (json.JSONDecodeError, OSError):
            pass

    output_dir = _resolve(output_dir_str)

    # Phase 2: Check if .whispersync/config.json exists and has output_dir
    new_config = output_dir / ".whispersync" / "config.json"
    if new_config.exists():
        try:
            with open(new_config) as f:
                new_cfg = json.load(f)
            if new_cfg and "output_dir" in new_cfg:
                output_dir = _resolve(new_cfg["output_dir"])
        except (json.JSONDecodeError, OSError):
            pass

    return output_dir


def get_data_dir() -> Path:
    """Return output_dir/.whispersync/, creating it if needed.

    This is the single source of truth for where user data lives.
    """
    p = _resolve_output_dir() / ".whispersync"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_config_path() -> Path:
    """Return the path to the user config file."""
    return get_data_dir() / "config.json"


def get_speaker_config_path() -> Path:
    """Return the path to transcription-config.md."""
    return get_data_dir() / "transcription-config.md"


def get_dictation_log_dir() -> Path:
    """Return the directory for dictation history logs."""
    return get_data_dir() / "dictation-logs"


def get_stats_dir() -> Path:
    """Return the directory for persistent stats."""
    return get_data_dir() / "stats"


def get_feature_log_dir() -> Path:
    """Return the directory for feature suggestion logs."""
    return get_data_dir() / "feature-suggestions"


def get_legacy_config_path() -> Path:
    """Return the legacy config.json path (inside code repo)."""
    return _PKG_DIR / "config.json"


def get_legacy_speaker_config_path() -> Path:
    """Return the legacy transcription-config.md path."""
    return get_install_root() / ".claude" / "workflows" / "transcription-config.md"


def get_legacy_dictation_log_dir() -> Path:
    """Return the legacy dictation log directory."""
    return _PKG_DIR / "logs" / "data" / "dictation"
