"""Benchmark: compare transcription speed across models for both use cases.

Runs two benchmarks:
  1. Meeting mode  — 30 minutes of audio, full pipeline (file I/O + alignment)
  2. Dictation mode — 45 seconds of audio, fast pipeline (numpy, no alignment)

Usage:
    python -m whisper_sync.benchmark [path_to_wav]

If no WAV is provided, scans for the most recent recording.wav.
The WAV is trimmed/looped to the target durations automatically.
"""

import sys
import time
import wave
from pathlib import Path

import numpy as np

# Add parent to path so we can import the package
sys.path.insert(0, str(Path(__file__).parent.parent))

from whisper_sync import config
from whisper_sync.paths import get_install_root
from whisper_sync.transcribe import transcribe, transcribe_fast, _load_whisper_model, _load_align_model

MODELS = ["tiny", "base", "small", "medium", "large-v2", "large-v3"]
SAMPLE_RATE = 16000
MEETING_DURATION_S = 30 * 60   # 30 minutes
DICTATION_DURATION_S = 45      # 45 seconds


def find_test_wav() -> str:
    """Find the most recent recording.wav in the output directory."""
    cfg = config.load()
    out_dir = Path(cfg["output_dir"])
    if not out_dir.is_absolute():
        out_dir = get_install_root() / out_dir
    wavs = sorted(out_dir.rglob("recording.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
    if wavs:
        return str(wavs[0])
    raise FileNotFoundError(f"No recording.wav found under {out_dir}. Pass a WAV path as argument.")


def load_wav_as_numpy(path: str) -> np.ndarray:
    with wave.open(path, "r") as wf:
        frames = wf.readframes(wf.getnframes())
        return np.frombuffer(frames, dtype=np.int16)


def make_duration(audio_np: np.ndarray, target_samples: int) -> np.ndarray:
    """Trim or loop audio to exactly target_samples."""
    if len(audio_np) >= target_samples:
        return audio_np[:target_samples]
    # Loop to fill
    repeats = (target_samples // len(audio_np)) + 1
    return np.tile(audio_np, repeats)[:target_samples]


def save_temp_wav(audio_np: np.ndarray, path: str):
    """Save int16 numpy array as mono 16kHz WAV."""
    with wave.open(path, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio_np.tobytes())


def get_gpu_name() -> str:
    try:
        import torch
        if torch.cuda.is_available():
            return torch.cuda.get_device_name(0)
    except Exception:
        pass
    return "CPU"


def run():
    import tempfile

    # Resolve source file
    if len(sys.argv) > 1:
        source_wav = sys.argv[1]
    else:
        source_wav = find_test_wav()

    cfg = config.load()
    language = cfg["language"]
    compute_type = cfg["compute_type"]
    gpu_name = get_gpu_name()

    print(f"Source file: {source_wav}")
    print(f"GPU: {gpu_name}")
    print(f"Compute: {compute_type} | Language: {language}")
    print("=" * 70)

    # Load source audio
    raw_audio = load_wav_as_numpy(source_wav)
    raw_duration = len(raw_audio) / SAMPLE_RATE
    print(f"Source audio: {raw_duration:.1f}s ({len(raw_audio)} samples)")

    # Prepare two test clips
    meeting_audio = make_duration(raw_audio, MEETING_DURATION_S * SAMPLE_RATE)
    dictation_audio = make_duration(raw_audio, DICTATION_DURATION_S * SAMPLE_RATE)

    meeting_float = meeting_audio.astype(np.float32) / 32768.0
    dictation_float = dictation_audio.astype(np.float32) / 32768.0

    # Save meeting WAV to temp file (needed for full pipeline)
    tmp_meeting = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_meeting.close()
    save_temp_wav(meeting_audio, tmp_meeting.name)

    print(f"Meeting test:   {MEETING_DURATION_S}s ({MEETING_DURATION_S/60:.0f} min)")
    print(f"Dictation test: {DICTATION_DURATION_S}s")

    # Filter to cached models
    from whisper_sync.model_status import is_model_cached
    available = [m for m in MODELS if is_model_cached(m)]
    skipped = [m for m in MODELS if not is_model_cached(m)]
    if skipped:
        print(f"\nSkipping (not downloaded): {', '.join(skipped)}")
    print(f"Testing: {', '.join(available)}\n")

    # Pre-load alignment model
    t0 = time.perf_counter()
    _load_align_model(language)
    print(f"Alignment model loaded: {time.perf_counter() - t0:.2f}s")

    # Pre-load all whisper models
    for model_name in available:
        t = time.perf_counter()
        _load_whisper_model(model_name, compute_type, language)
        print(f"Loaded {model_name}: {time.perf_counter() - t:.2f}s")
    print()

    # ── Meeting benchmark (full pipeline: file I/O + transcribe + align) ──

    print("=" * 70)
    print(f"MEETING MODE — {MEETING_DURATION_S/60:.0f}-minute audio (transcribe + alignment)")
    print("=" * 70)

    meeting_results = []
    for model_name in available:
        print(f"  {model_name}...", end=" ", flush=True)
        t = time.perf_counter()
        result = transcribe(tmp_meeting.name, diarize=False, model_override=model_name)
        elapsed = time.perf_counter() - t
        print(f"{elapsed:.2f}s")
        meeting_results.append((model_name, elapsed))

    print()
    print(f"{'Model':<12} {'Time':<10} {'Speed vs Audio':<18} {'Wait for 30 min':<16}")
    print("-" * 56)
    for name, t in meeting_results:
        ratio = MEETING_DURATION_S / t
        print(f"{name:<12} {t:<10.1f}s {ratio:<18.1f}x realtime ~{t:.0f}s wait")

    # ── Dictation benchmark (fast pipeline: numpy, no alignment) ──

    print()
    print("=" * 70)
    print(f"DICTATION MODE — {DICTATION_DURATION_S}s audio (fast transcribe, no alignment)")
    print("=" * 70)

    dictation_results = []
    for model_name in available:
        print(f"  {model_name}...", end=" ", flush=True)
        t = time.perf_counter()
        text = transcribe_fast(dictation_float, model_override=model_name)
        elapsed = time.perf_counter() - t
        print(f"{elapsed:.2f}s")
        dictation_results.append((model_name, elapsed))

    print()
    print(f"{'Model':<12} {'Time':<10} {'Feels Like':<16}")
    print("-" * 38)
    for name, t in dictation_results:
        if t < 1.0:
            feel = "instant"
        elif t < 2.0:
            feel = "snappy"
        elif t < 4.0:
            feel = "noticeable"
        else:
            feel = "slow"
        print(f"{name:<12} {t:<10.2f}s {feel}")

    # ── Cleanup ──

    Path(tmp_meeting.name).unlink(missing_ok=True)

    # ── Summary ──

    print()
    print("=" * 70)
    print(f"GPU: {gpu_name} | Compute: {compute_type}")
    print("Copy these results to update the README model comparison tables.")


if __name__ == "__main__":
    run()
