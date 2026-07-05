"""One-time trainer environment setup for the phrase manager (step 5).

Creates a SEPARATE venv with the openWakeWord training stack (torch
CUDA and friends stay out of whisper-env), clones piper-sample-
generator with its TTS checkpoint, and downloads the training datasets.
Sources are verbatim from the upstream openWakeWord
automatic_model_training notebook (verified 2026-07-05).

Idempotent: every phase skips work whose output already exists, so
rerunning after a failed or interrupted download continues instead of
starting over (a file is only skipped once fully downloaded - partial
downloads restart that one file).

DISK WARNING: the datasets total roughly 12-18 GB. The script prints
the plan and refuses to download without --yes.
GPU note: setup itself never touches the GPU; training does (see
train_phrase.py).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROOT = Path(__file__).resolve().parent / "workspace"

PIPER_REPO = "https://github.com/rhasspy/piper-sample-generator"
# openwakeword's train.py does `from generate_samples import ...` -
# the v1/v2 top-level-script layout. v3 refactored into a package and
# breaks that import, so the clone is PINNED to the tag that matches
# both the layout and the v2.0.0 checkpoint below.
PIPER_REF = "v2.0.0"
PIPER_CHECKPOINT = ("https://github.com/rhasspy/piper-sample-generator/"
                    "releases/download/v2.0.0/en_US-libritts_r-medium.pt")

FEATURES_BASE = ("https://huggingface.co/datasets/davidscripka/"
                 "openwakeword_features/resolve/main/")
FEATURE_FILES = [
    # (~10 GB) precomputed negative features, 2000 hrs
    "openwakeword_features_ACAV100M_2000_hrs_16bit.npy",
    # (~2 GB) false-positive validation features
    "validation_set_features.npy",
]

# The notebook's bal_train09.tar 404s: agkphysics/AudioSet was
# restructured (2026) into parquet shards under data/bal_train/. One
# ~700 MB shard carries the same balanced-train audio volume the
# notebook's tar did.
AUDIOSET_PARQUET = ("https://huggingface.co/datasets/agkphysics/AudioSet/"
                    "resolve/main/data/bal_train/09.parquet")

# Training deps per the upstream notebook, minus the tensorflow/tflite
# export chain (the listener loads onnx; train.py's import scan shows
# no unconditional tensorflow import). torch is installed separately
# with the CUDA index.
TRAINER_PACKAGES = [
    "openwakeword",
    # upstream piper-phonemize ships no Windows wheels at all; the
    # -fix rebuild provides the same piper_phonemize module (verified
    # importable on py3.11 2026-07-05)
    "piper-phonemize-fix",
    "setuptools<82",  # torch cu128 constraint; -fix pulls in 83
    # upstream webrtcvad is sdist-only on Windows (needs MSVC); the
    # -wheels fork ships the same module prebuilt
    "webrtcvad-wheels",
    "mutagen==1.47.0",
    "torchinfo==1.8.0",
    "torchmetrics==1.2.0",
    "speechbrain==0.5.14",
    "audiomentations==0.33.0",
    "torch-audiomentations==0.11.0",
    "acoustics==0.2.6",
    "pronouncing==0.2.0",
    "datasets==2.14.6",
    # datasets 2.14 uses pa.PyExtensionType, removed in pyarrow 17
    "pyarrow<17",
    "deep-phonemizer==0.0.19",
]

# Runs inside the trainer venv (it has datasets/scipy): materialize a
# HF dataset - or a local directory of audio files ("local" source) -
# as 16 kHz mono int16 wavs, the layout train.py expects.
# argv: <source> <config-or-dir> <split> <out_dir> ("local" uses
# config-or-dir as the directory to glob; "-" config means None).
HF_TO_WAVS_SNIPPET = r"""
import sys
from pathlib import Path
import numpy as np
import scipy.io.wavfile
from datasets import load_dataset, Audio, Dataset

source, config, split, out_dir = (sys.argv[1], sys.argv[2], sys.argv[3],
                                  Path(sys.argv[4]))
out_dir.mkdir(parents=True, exist_ok=True)
if source == "local":
    files = [str(p) for p in Path(config).glob("**/*")
             if p.suffix.lower() in (".flac", ".wav", ".mp3", ".ogg")]
    ds = Dataset.from_dict({"audio": files}).cast_column("audio", Audio())
elif source == "parquet":
    ds = load_dataset("parquet", data_files=config, split="train")
else:
    ds = load_dataset(source, config if config != "-" else None,
                      split=split, streaming=False)
ds = ds.cast_column("audio", Audio(sampling_rate=16000))
n = 0
for row in ds:
    arr = np.clip(np.asarray(row["audio"]["array"]), -1.0, 1.0)
    data = (arr * 32767).astype(np.int16)
    scipy.io.wavfile.write(out_dir / f"{n:06d}.wav", 16000, data)
    n += 1
print(f"wrote {n} wavs to {out_dir}")
"""


def log(msg: str) -> None:
    print(f"[setup] {msg}", flush=True)


def run(cmd: list, **kw) -> None:
    log("run: " + " ".join(str(c) for c in cmd))
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def download(url: str, dest: Path) -> None:
    """Streamed download; skip when the finished file already exists."""
    if dest.exists():
        log(f"exists, skipping: {dest.name}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    log(f"downloading {url} -> {dest}")
    with urllib.request.urlopen(url) as resp, open(part, "wb") as fh:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
    part.rename(dest)


def trainer_python(root: Path) -> Path:
    return root / "trainer-env" / "Scripts" / "python.exe"


def _require_py311(python_exe: str) -> None:
    """Fail fast on the wrong interpreter: piper-phonemize wheels stop
    at 3.11, and a mismatched venv only fails later, mid-pip, with a
    far less actionable error."""
    out = subprocess.run(
        [python_exe, "-c",
         "import sys; print('%d.%d' % sys.version_info[:2])"],
        capture_output=True, text=True, check=True)
    version = out.stdout.strip()
    if version != "3.11":
        raise SystemExit(
            f"[setup] base python is {version}; the trainer venv MUST "
            "be 3.11 (piper-phonemize ships no newer wheels). Pass "
            "--python <path to a 3.11 interpreter> (py -3.11).")


def phase_venv(root: Path, base_python: str, torch_index: str) -> None:
    py = trainer_python(root)
    if py.exists():
        log("trainer venv exists, skipping create")
        _require_py311(str(py))
    else:
        _require_py311(base_python)
        run([base_python, "-m", "venv", root / "trainer-env"])
    run([py, "-m", "pip", "install", "--upgrade", "pip"])
    # CUDA torch first (its own index), then the notebook stack.
    # Default cu128: matches the torch build proven in whisper-env on
    # this machine (RTX 5070 Ti is Blackwell/sm_120 - older cu121
    # wheels neither support the GPU nor Python 3.13).
    run([py, "-m", "pip", "install", "torch",
         "--index-url", torch_index])
    run([py, "-m", "pip", "install"] + TRAINER_PACKAGES)


def phase_piper(root: Path) -> None:
    piper = root / "piper-sample-generator"
    if not piper.exists():
        run(["git", "clone", "--branch", PIPER_REF, "--depth", "1",
             PIPER_REPO, piper])
    else:
        log("piper-sample-generator exists, skipping clone")
    download(PIPER_CHECKPOINT, piper / "models" / "en_US-libritts_r-medium.pt")


def phase_datasets(root: Path) -> None:
    py = trainer_python(root)
    features = root / "features"
    for fname in FEATURE_FILES:
        download(FEATURES_BASE + fname, features / fname)

    data = root / "data"
    snippet = root / "_hf_to_wavs.py"
    snippet.write_text(HF_TO_WAVS_SNIPPET, encoding="utf-8")

    rirs = data / "mit_rirs"
    if not rirs.exists():
        run([py, snippet,
             "davidscripka/MIT_environmental_impulse_responses", "-",
             "train", rirs])
    else:
        log("mit_rirs exists, skipping")

    audioset = data / "audioset_16k"
    if not audioset.exists():
        parquet = data / "audioset_bal_train_09.parquet"
        download(AUDIOSET_PARQUET, parquet)
        run([py, snippet, "parquet", parquet, "-", audioset])
    else:
        log("audioset_16k exists, skipping")

    fma = data / "fma"
    if not fma.exists():
        run([py, snippet, "rudraml/fma", "small", "train", fma])
    else:
        log("fma exists, skipping")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT,
                        help="workspace root (default: training/workspace)")
    parser.add_argument("--python", default=sys.executable,
                        help="base python for the trainer venv. MUST be "
                             "3.11: piper-phonemize (needed by the v2 "
                             "sample generator) ships no newer wheels")
    parser.add_argument("--torch-index",
                        default="https://download.pytorch.org/whl/cu128",
                        help="pytorch wheel index (CUDA build)")
    parser.add_argument("--yes", action="store_true",
                        help="confirm the 12-18 GB dataset download")
    parser.add_argument("--skip-datasets", action="store_true",
                        help="only set up the venv and piper generator")
    args = parser.parse_args()

    root = args.root.resolve()
    log(f"workspace: {root}")
    phase_venv(root, args.python, args.torch_index)
    phase_piper(root)
    if args.skip_datasets:
        log("datasets skipped (--skip-datasets)")
        return 0
    if not args.yes:
        log("DATASETS NOT DOWNLOADED. The feature/noise/RIR datasets "
            "total roughly 12-18 GB on disk. Rerun with --yes to "
            "proceed (idempotent; finished files are skipped).")
        return 1
    phase_datasets(root)
    log("setup complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
