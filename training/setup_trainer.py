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
# restructured (2026) into parquet shards under data/bal_train/
# (500 ten-second clips per ~700 MB shard). Two shards give ~2.8
# hours of background noise alongside FMA.
AUDIOSET_PARQUETS = [
    ("https://huggingface.co/datasets/agkphysics/AudioSet/"
     "resolve/main/data/bal_train/08.parquet"),
    ("https://huggingface.co/datasets/agkphysics/AudioSet/"
     "resolve/main/data/bal_train/09.parquet"),
]

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

# Runs inside the trainer venv: materialize audio as 16 kHz mono int16
# wavs, the layout train.py expects.
# argv: <source> <config-or-dir> <split> <out_dir>
#   local:   config = directory to glob for audio files
#   parquet: config = a parquet file with an audio{bytes,path} column,
#            decoded directly via pyarrow + soundfile (datasets 2.14.6
#            cannot read modern HF parquet - dataclass TypeError)
#   else:    config = HF dataset config name ("-" = None)
HF_TO_WAVS_SNIPPET = r"""
import io
import sys
from math import gcd
from pathlib import Path
import numpy as np
import scipy.io.wavfile

source, config, split, out_dir = (sys.argv[1], sys.argv[2], sys.argv[3],
                                  Path(sys.argv[4]))
out_dir.mkdir(parents=True, exist_ok=True)
# Continue numbering after existing wavs so multiple source shards
# can feed one directory without overwriting each other.
n = len(list(out_dir.glob("*.wav")))
start = n


def write_16k(arr, rate):
    global n
    if arr.ndim > 1:
        arr = arr.mean(axis=1)
    if rate != 16000:
        from scipy.signal import resample_poly
        g = gcd(16000, int(rate))
        arr = resample_poly(arr, 16000 // g, int(rate) // g)
    arr = np.clip(np.asarray(arr, dtype=np.float32), -1.0, 1.0)
    scipy.io.wavfile.write(out_dir / f"{n:06d}.wav", 16000,
                           (arr * 32767).astype(np.int16))
    n += 1


if source == "parquet":
    import pyarrow.parquet as pq
    import soundfile as sf
    skipped = 0
    # Batched read: a whole ~700MB shard materialized at once doubles
    # peak memory for no benefit.
    pf = pq.ParquetFile(config)
    for batch in pf.iter_batches(columns=["audio"], batch_size=32):
        for item in batch.column("audio").to_pylist():
            try:
                arr, rate = sf.read(io.BytesIO(item["bytes"]),
                                    dtype="float32")
            except Exception:
                skipped += 1  # one bad clip must not kill the batch
                continue
            write_16k(arr, rate)
    if skipped:
        print(f"skipped {skipped} undecodable clips")
    if n == 0:
        # Nothing decoded = schema mismatch or wholly bad shard; the
        # caller must see a failure, not an empty success.
        sys.exit(f"no clips decoded from {config}")
else:
    from datasets import load_dataset, Audio, Dataset
    if source == "local":
        files = [str(p) for p in Path(config).glob("**/*")
                 if p.suffix.lower() in (".flac", ".wav", ".mp3", ".ogg")]
        ds = Dataset.from_dict({"audio": files}).cast_column("audio",
                                                             Audio())
    else:
        ds = load_dataset(source, config if config != "-" else None,
                          split=split, streaming=False)
    ds = ds.cast_column("audio", Audio(sampling_rate=16000))
    skipped = 0
    # Index access instead of iteration: audio decodes lazily when a
    # row materializes, so a corrupt clip raises during the for-row
    # yield itself, where a loop-body try cannot catch it. FMA ships a
    # few undecodable mp3s; one bad clip must not kill the conversion
    # (observed live 2026-07-05: LibsndfileError at clip ~3495).
    for i in range(len(ds)):
        try:
            arr = np.asarray(ds[i]["audio"]["array"])
        except Exception:
            skipped += 1
            continue
        write_16k(arr, 16000)
    if skipped:
        print(f"skipped {skipped} undecodable clips")
    if n == start:
        # Nothing decoded = wholly bad source; the caller must see a
        # failure, not an empty success.
        sys.exit(f"no clips decoded from {source}")
print(f"wrote {n - start} wavs to {out_dir} ({n} total)")
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

    def _has_wavs(d: Path) -> bool:
        # Content-based skip guard: a crashed conversion can leave an
        # EMPTY directory behind, and a bare exists() check would then
        # skip the phase forever.
        return any(d.glob("*.wav"))

    rirs = data / "mit_rirs"
    if not _has_wavs(rirs):
        run([py, snippet,
             "davidscripka/MIT_environmental_impulse_responses", "-",
             "train", rirs])
    else:
        log("mit_rirs already converted, skipping")

    audioset = data / "audioset_16k"
    if not _has_wavs(audioset):
        for url in AUDIOSET_PARQUETS:
            parquet = data / url.rsplit("/", 2)[-1].replace(
                ".parquet", "_bal_train.parquet")
            download(url, parquet)
            run([py, snippet, "parquet", parquet, "train", audioset])
    else:
        log("audioset_16k already converted, skipping")

    fma = data / "fma"
    if not _has_wavs(fma):
        run([py, snippet, "rudraml/fma", "small", "train", fma])
    else:
        log("fma already converted, skipping")


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
