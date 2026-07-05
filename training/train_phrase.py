"""Headless phrase training CLI - step 5, PR A.

Trains ONE custom openWakeWord phrase model from typed text via the
official trainer, using the workspace prepared by setup_trainer.py.
The finished .onnx lands in <root>/phrases/, ready to be set as
wake_phrase_model or wake_outro_model (the PR B registry and tray
surface automate that).

GPU PROTOCOL: training occupies the dGPU for tens of minutes. The
command refuses to run without --go, and the owner must be warned
before any run (see feedback_warn-before-gpu-heavy-tests in the PM
memory; the PR C background job adds the app-side busy/asleep checks).

Config generation lives in whisper_sync.phrase_training (CI-tested);
this file is the thin subprocess wrapper around the trainer venv.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROOT = Path(__file__).resolve().parent / "workspace"
sys.path.insert(0, str(REPO_ROOT))

from whisper_sync.phrase_training import (  # noqa: E402
    build_training_config, write_training_config, training_commands,
    trained_model_path, sanitize_model_name,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phrase", help='the phrase, e.g. "hey hal"')
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT,
                        help="workspace root (default: training/workspace)")
    parser.add_argument("--overrides", default="{}",
                        help="JSON dict of training-config overrides")
    parser.add_argument("--config-only", action="store_true",
                        help="write the config and exit (no training)")
    parser.add_argument("--go", action="store_true",
                        help="confirm the GPU-heavy run (tens of minutes)")
    args = parser.parse_args()

    root = args.root.resolve()
    name = sanitize_model_name(args.phrase)
    config = build_training_config(args.phrase, root,
                                   json.loads(args.overrides))
    config_path = write_training_config(
        config, root / "output" / name / f"{name}.yml")
    print(f"[train] config: {config_path}")
    if args.config_only:
        return 0

    trainer_python = root / "trainer-env" / "Scripts" / "python.exe"
    if not trainer_python.exists():
        print("[train] trainer venv missing - run setup_trainer.py first")
        return 1
    if not args.go:
        print("[train] NOT RUNNING. Training occupies the dGPU for tens "
              "of minutes; make sure the owner has been warned, then "
              "rerun with --go.")
        return 1

    for cmd in training_commands(config_path, trainer_python):
        print("[train] run: " + " ".join(cmd), flush=True)
        result = subprocess.run(cmd, cwd=root)
        if result.returncode != 0:
            print(f"[train] step failed (exit {result.returncode}); "
                  "artifacts kept for inspection in "
                  f"{config['output_dir']}")
            return result.returncode

    produced = trained_model_path(config)
    if not produced.exists():
        print(f"[train] trainer reported success but {produced} is "
              "missing - check the output dir")
        return 1
    phrases_dir = root / "phrases"
    phrases_dir.mkdir(parents=True, exist_ok=True)
    final = phrases_dir / produced.name
    shutil.copy2(produced, final)
    print(f"[train] done: {final}")
    print("[train] set it as wake_phrase_model or wake_outro_model "
          "(full path) and restart the listener toggle to load it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
