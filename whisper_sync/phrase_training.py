"""Phrase-training config and command construction - step 5, PR A.

The self-serve phrase manager (spec, fourth intake) trains custom
openWakeWord models from TYPED phrases via the official trainer
(``openwakeword.train`` driven by a YAML config; pipeline verified
against the upstream automatic_model_training notebook 2026-07-05).
This module owns the pure logic - workspace layout, config generation,
command lines - so it is unit-testable on the dependency-light system
python and reusable by the PR C background job. The heavyweight
execution lives in ``training/`` scripts and a separate trainer venv;
nothing here imports torch or openwakeword.

The config is written as JSON, which every YAML 1.2 parser (including
the trainer's ``yaml.load``) accepts - no yaml dependency here.

Values marked NOTEBOOK below are verbatim from the upstream notebook;
values marked PROVISIONAL are best-effort defaults for keys train.py
reads but the notebook summary did not pin - the first supervised
training run validates them (train.py fails fast on a missing or
malformed key, and the run is staged behind an explicit owner go).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# Workspace layout, all relative to one root (gitignored by default):
#   trainer-env/              separate venv (torch CUDA etc.)
#   piper-sample-generator/   TTS sample generator checkout + checkpoint
#   data/mit_rirs/            room impulse responses
#   data/audioset_16k/        background noise (AudioSet subset)
#   data/fma/                 background noise (FMA small subset)
#   features/*.npy            precomputed negative/validation features
#   output/<name>/            per-phrase training artifacts
#   phrases/<name>.onnx       finished models the listener loads
ACAV_FEATURES = "openwakeword_features_ACAV100M_2000_hrs_16bit.npy"
VALIDATION_FEATURES = "validation_set_features.npy"


def sanitize_model_name(phrase: str) -> str:
    """Typed phrase -> model/file name ("Hey Hal!" -> "hey_hal")."""
    name = re.sub(r"[^a-z0-9]+", "_", phrase.lower()).strip("_")
    if not name:
        raise ValueError(f"phrase {phrase!r} has no usable characters")
    return name


def build_training_config(phrase: str, root: Path | str,
                          overrides: dict | None = None) -> dict:
    """Full config dict for ``openwakeword.train`` for one phrase."""
    root = Path(root)
    name = sanitize_model_name(phrase)
    config = {
        # Verbatim NOTEBOOK values.
        "target_phrase": [phrase.lower()],
        "model_name": name,
        "n_samples": 1000,
        "n_samples_val": 1000,
        "steps": 10000,
        "target_accuracy": 0.6,
        "target_recall": 0.25,
        "background_paths": [str(root / "data" / "audioset_16k"),
                             str(root / "data" / "fma")],
        "false_positive_validation_data_path":
            str(root / "features" / VALIDATION_FEATURES),
        "feature_data_files":
            {"ACAV100M_sample": str(root / "features" / ACAV_FEATURES)},
        # Workspace paths.
        "rir_paths": [str(root / "data" / "mit_rirs")],
        "piper_sample_generator_path":
            str(root / "piper-sample-generator"),
        "output_dir": str(root / "output" / name),
        # PROVISIONAL defaults for the remaining keys train.py reads.
        "model_type": "dnn",
        "layer_size": 32,
        "total_length": 32000,
        "tts_batch_size": 50,
        "augmentation_batch_size": 16,
        "augmentation_rounds": 1,
        "custom_negative_phrases": [],
        "background_paths_duplication_rate": [1],
        "batch_n_per_class": {"ACAV100M_sample": 1024,
                              "adversarial_negative": 50,
                              "positive": 50},
        "max_negative_weight": 1500,
        "target_false_positives_per_hour": 0.2,
    }
    config.update(overrides or {})
    return config


def write_training_config(config: dict, path: Path | str) -> Path:
    """Write the config as JSON (valid YAML) for the trainer."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


def training_commands(config_path: Path | str,
                      trainer_python: Path | str) -> list[list[str]]:
    """The three trainer invocations, in order (notebook-verbatim
    staging: clip generation, augmentation, then model training)."""
    base = [str(trainer_python), "-m", "openwakeword.train",
            "--training_config", str(config_path)]
    return [base + ["--generate_clips"],
            base + ["--augment_clips"],
            base + ["--train_model"]]


def trained_model_path(config: dict) -> Path:
    """Where train.py leaves the .onnx for this config."""
    return Path(config["output_dir"]) / f"{config['model_name']}.onnx"
