# Phrase training - trainer environment and headless CLI

Step 5 (self-serve phrase manager) PR A: everything needed to train a
custom openWakeWord phrase model from TYPED text, no speaking required.
Sources and pipeline are verbatim from the upstream openWakeWord
automatic_model_training notebook (verified 2026-07-05); the plan and
PR sketch live in `docs/plans/2026-07-04-assistant-build-round.md`.

## Why a separate environment

The training stack (CUDA torch, torchinfo, torchmetrics, speechbrain,
audiomentations, piper-sample-generator, HF datasets) is an order of
magnitude heavier than the app runtime and must never bloat
whisper-env. Everything lives in a self-contained workspace
(`training/workspace/` by default, gitignored).

## One-time setup

```
python training/setup_trainer.py --yes
```

- Creates `workspace/trainer-env` (separate venv, CUDA torch).
- Clones piper-sample-generator + its libritts TTS checkpoint (the
  synthetic voice that "speaks" your typed phrase thousands of times).
- Downloads the datasets: MIT room impulse responses, AudioSet +
  FMA background noise (converted to 16 kHz wavs), precomputed
  negative features and validation features (.npy).

**Disk warning: roughly 12-18 GB total.** Without `--yes` the script
prints the plan and exits. Idempotent - rerun after any interruption
and it continues (finished files are skipped).

## Train a phrase

```
python training/train_phrase.py "hey hal" --go
```

Generates the training config (JSON, which the trainer's YAML loader
accepts), then runs the three official stages in the trainer venv:
`--generate_clips` (synthetic TTS samples), `--augment_clips` (RIR +
noise augmentation), `--train_model`. The finished model lands at
`workspace/phrases/hey_hal.onnx`; point `wake_phrase_model` or
`wake_outro_model` at that path and toggle the listener off/on.

**GPU protocol: training occupies the dGPU for tens of minutes.** The
CLI refuses without `--go`, and the owner must be warned before any
run. `--config-only` writes the config without training (used by the
tests and for inspection). Config values the notebook did not pin are
marked PROVISIONAL in `whisper_sync/phrase_training.py`; the first
supervised run validates them (train.py fails fast on a bad key).

## What the later PRs add

- **PR B**: saved-phrase registry (name -> path + active + wake/outro
  role), listener loading of all active models, tray Saved Phrases
  surface with checkmarks.
- **PR C**: Set Phrase dialog + background training job with tray
  status/ETA and completion toast (app-side busy/asleep GPU checks).
