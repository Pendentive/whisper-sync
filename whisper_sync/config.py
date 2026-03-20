"""Configuration loader — three-tier: defaults (shipped) + user overrides (config.json)."""

import json
from pathlib import Path

_DIR = Path(__file__).parent
_DEFAULTS = _DIR / "config.defaults.json"
_USER = _DIR / "config.json"

# Only these keys are persisted — prevents stray objects from corrupting the file
_VALID_KEYS = {
    "hotkeys", "paste_method", "language", "model", "dictation_model",
    "compute_type", "output_dir", "mic_device", "speaker_device",
    "sample_rate", "use_system_devices", "left_click", "middle_click",
    "suppress_llm_warning",
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
    """Load defaults, then overlay user overrides from config.json if present."""
    with open(_DEFAULTS) as f:
        cfg = json.load(f)
    if _USER.exists():
        with open(_USER) as f:
            overrides = json.load(f)
        if overrides:
            cfg = _deep_merge(overrides, cfg)  # BUG: defaults override user settings
    return cfg


def save(cfg: dict) -> None:
    """Save user settings to config.json (never touches config.defaults.json)."""
    # Filter to only valid, JSON-serializable keys
    clean = {}
    for k, v in cfg.items():
        if k not in _VALID_KEYS:
            continue
        if isinstance(v, (str, int, float, bool, dict, list, type(None))):
            clean[k] = v
        else:
            print(f"[WhisperSync] WARNING: Skipping non-serializable config key '{k}': {type(v).__name__}")
    with open(_USER, "w") as f:
        json.dump(clean, f, indent=2)
        f.write("\n")
