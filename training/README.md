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

## torch 2.x / Windows compatibility (first supervised run, 2026-07-05)

The upstream notebook targets Linux and a torch 1.x-era stack; this
machine forces CUDA torch 2.x (RTX 5070 Ti is sm_120, cu128 wheels
only) on Windows. setup_trainer.py adapts the pinned stack rather than
upgrading it (newer package majors change APIs the notebook was
verified against):

- **sitecustomize shim** in the trainer venv restores the removed
  `torchaudio.set/get_audio_backend` no-ops (speechbrain 0.5.14 and
  torch-audiomentations 0.11.0 call them at import) and routes
  `torchaudio.load/info` through soundfile (torchaudio 2.9+ needs
  TorchCodec + shared FFmpeg; every training input here is plain WAV).
- **scipy<1.17** pin: acoustics 0.2.6 imports `sph_harm`, removed in
  scipy 1.17.
- **In-place patches** (`phase_package_patches`, idempotent, fail
  loudly if the pinned code drifts): openwakeword's memmap trim closes
  handles before remove/rename (Windows locks open files); train.py
  uses an in-process DataLoader on Windows (spawned workers cannot
  pickle the generator's lambdas); piper's `torch.load` passes
  `weights_only=False` for the pinned trusted checkpoint (torch 2.6
  default flip).
- **`download_models()`** step: openwakeword ships without its
  melspectrogram/embedding feature extractors; the augment stage needs
  them.
- **PYTHONUTF8=1** for trainer subprocesses: torch's onnx exporter
  prints emoji progress marks, which raise UnicodeEncodeError on the
  cp1252 console and kill the export after a successful run.

Rerunning `setup_trainer.py` applies all of this to an existing
workspace (every step is idempotent).

## What the later PRs add

- **PR B**: saved-phrase registry (name -> path + active + wake/outro
  role), listener loading of all active models, tray Saved Phrases
  surface with checkmarks.
- **PR C**: Set Phrase dialog + background training job with tray
  status/ETA and completion toast (app-side busy/asleep GPU checks).
