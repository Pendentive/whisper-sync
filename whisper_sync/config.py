"""Configuration loader -- three-tier: defaults (shipped) + user overrides (config.json)."""

import json
from pathlib import Path

from .logger import logger
from .paths import get_config_path, get_legacy_config_path

_DIR = Path(__file__).parent
_DEFAULTS = _DIR / "config.defaults.json"

# Only these keys are persisted -- prevents stray objects from corrupting the file
_VALID_KEYS = {
    "hotkeys", "paste_method", "language", "model", "dictation_model",
    "compute_type", "output_dir", "mic_device", "speaker_device",
    "sample_rate", "use_system_devices", "left_click", "middle_click",
    "suppress_llm_warning", "github_repo", "github_poll_interval",
    "github_notifications", "log_window", "device", "incognito",
    "always_available_dictation", "backup_device", "backup_model",
}


def _deep_merge(base: dict, overrides: dict) -> dict:
    """Merge overrides into base (one level deep for nested dicts like hotkeys)."""
    merged = dict(base)
    for k, v in overrides.items():
        if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
            merged[k] = {**merged[k], **v}
        else:
            merged[k] = v
    return merged


def load() -> dict:
    """Load defaults, then overlay user overrides from config.json if present.

    Checks output_dir/.whispersync/config.json first, then falls back to the
    legacy whisper_sync/config.json for backwards compatibility.
    """
    with open(_DEFAULTS) as f:
        cfg = json.load(f)

    # New location: output_dir/.whispersync/config.json
    new_path = get_config_path()
    legacy_path = get_legacy_config_path()

    if new_path.exists():
        with open(new_path) as f:
            overrides = json.load(f)
        if overrides:
            cfg = _deep_merge(cfg, overrides)
    elif legacy_path.exists():
        with open(legacy_path) as f:
            overrides = json.load(f)
        if overrides:
            cfg = _deep_merge(cfg, overrides)

    return cfg


def save(cfg: dict) -> None:
    """Save user settings to output_dir/.whispersync/config.json."""
    # Filter to only valid, JSON-serializable keys
    clean = {}
    for k, v in cfg.items():
        if k not in _VALID_KEYS:
            continue
        if isinstance(v, (str, int, float, bool, dict, list, type(None))):
            clean[k] = v
        else:
            logger.warning(f"Skipping non-serializable config key '{k}': {type(v).__name__}")

    save_path = get_config_path()
    save_path.parent.mkdir(parents=True, exist_ok=True)
    with open(save_path, "w") as f:
        json.dump(clean, f, indent=2)
        f.write("\n")

    # Keep the legacy bootstrap pointer in sync so the app can find the
    # config after an output_dir change + restart.
    if "output_dir" in clean:
        legacy_path = get_legacy_config_path()
        try:
            bootstrap = {}
            if legacy_path.exists():
                with open(legacy_path) as f:
                    bootstrap = json.load(f)
            bootstrap["output_dir"] = clean["output_dir"]
            with open(legacy_path, "w") as f:
                json.dump(bootstrap, f, indent=2)
                f.write("\n")
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"Could not update legacy bootstrap pointer: {exc}")
