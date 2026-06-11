"""Opt-in END-TO-END test: real worker subprocess, real meeting audio.

This is the "actually use the software" test: it spawns the production
TranscriptionWorker subprocess (whisperX + CTranslate2 + CUDA/CPU), feeds
it a REAL meeting recording from the user's meetings repo, and asserts the
full staged pipeline (prepare -> transcribe -> align -> diarize ->
finalize) completes without crashing and produces structurally valid
output that flatten() can consume.

It loads ~3 GB of models and transcribes real audio, so it is opt-in:

    set WS_E2E=1
    whisper-env/Scripts/python.exe -m unittest tests.test_e2e_real_transcription -v

Expected runtime: ~2-6 minutes depending on GPU. Designed for overnight /
pre-release validation runs, not CI.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

_E2E = os.environ.get("WS_E2E") == "1"

try:
    import numpy  # noqa: F401
    import scipy.signal  # noqa: F401
    _HAS_DEPS = True
except ImportError:
    _HAS_DEPS = False


def _meetings_root():
    env = os.environ.get("WS_MEETINGS_DIR")
    if env:
        p = Path(env)
        return p if p.exists() else None
    candidate = (
        Path(__file__).resolve().parents[1].parent.parent
        / "icustomer" / "ic-product-mgmt" / "meetings" / "in-house"
    )
    return candidate if candidate.exists() else None


def _smallest_recording(root: Path, max_mb: int = 80):
    """Smallest real meeting recording (with an existing transcript.json,
    so we know the audio is transcribable)."""
    best = None
    best_size = max_mb * 1024 * 1024
    for week_dir in root.iterdir():
        if not week_dir.is_dir() or week_dir.name.startswith("."):
            continue
        for mdir in week_dir.iterdir():
            wav = mdir / "recording.wav"
            if wav.exists() and (mdir / "transcript.json").exists():
                size = wav.stat().st_size
                if size < best_size:
                    best, best_size = wav, size
    return best


_ROOT = _meetings_root()
_WAV = _smallest_recording(_ROOT) if (_ROOT and _E2E) else None


@unittest.skipUnless(_E2E, "set WS_E2E=1 to run the end-to-end transcription test")
@unittest.skipUnless(_HAS_DEPS, "requires numpy/scipy (run under app venv)")
@unittest.skipUnless(_WAV, "no real recording.wav found (set WS_MEETINGS_DIR)")
class RealTranscriptionE2E(unittest.TestCase):
    maxDiff = None

    def test_full_pipeline_on_real_recording(self):
        from whisper_sync import config as ws_config
        from whisper_sync.worker_manager import TranscriptionWorker

        cfg = ws_config.load()
        worker = TranscriptionWorker(cfg, preload_model=cfg.get("model", "large-v3"))

        with tempfile.TemporaryDirectory() as tmp:
            wav_copy = Path(tmp) / "recording.wav"
            shutil.copy2(_WAV, wav_copy)

            worker.start()
            try:
                self.assertTrue(
                    worker.wait_ready(timeout=300),
                    "worker subprocess must load models and report ready",
                )

                result = worker.transcribe(
                    str(wav_copy), diarize=True, timeout=1800
                )

                # --- Structural validation against the real pipeline contract
                self.assertIn("json_path", result, f"result keys: {list(result)}")
                json_path = Path(result["json_path"])
                self.assertTrue(json_path.exists(), "transcript.json must be written")

                data = json.loads(json_path.read_text(encoding="utf-8"))
                segments = data.get("segments", [])
                self.assertGreater(len(segments), 0, "real audio must yield segments")
                texts = [s.get("text", "").strip() for s in segments]
                self.assertTrue(any(texts), "segments must contain actual text")
                speakers = {s.get("speaker") for s in segments if s.get("speaker")}
                self.assertGreaterEqual(
                    len(speakers), 1, "diarization must assign at least one speaker"
                )
                self.assertGreater(result.get("word_count", 0), 0)
                self.assertGreater(result.get("duration", 0), 0)

                # --- The downstream consumer must accept this output
                from whisper_sync.flatten import flatten
                readable = flatten(str(json_path), transcript_data=data)
                self.assertTrue(readable, "flatten must produce readable output")
                content = Path(readable).read_text(encoding="utf-8")
                self.assertIn("Duration:", content.splitlines()[0])

                # --- Worker survived the whole job (no crash/respawn)
                self.assertTrue(
                    worker.is_alive(),
                    "worker must still be alive after a full real meeting",
                )
            finally:
                worker.stop()


if __name__ == "__main__":
    unittest.main()
