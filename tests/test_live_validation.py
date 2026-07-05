"""Opt-in LIVE validation: real hardware, real models, no human.

Automates the owner-test-checklist items a machine can measure (owner
directive 2026-07-05: verify outcomes with typical software tests
instead of hand-testing). Everything here is deliberately
non-disruptive: shared-mode mic streams, read-only registry reads, a
CPU-only worker spawn, and synthetic Windows-TTS audio fed DIRECTLY to
models - nothing is played out loud and no user file is touched.

    set WS_LIVE=1
    whisper-env/Scripts/python.exe -m pytest tests/test_live_validation.py -v

Runtime: ~1-3 minutes (the CPU failover transcription dominates).
Never runs in CI (hardware-dependent; gated like WS_E2E).

What each class proves, mapped to docs/owner-test-checklist.md:
- LiveMicTests: the listener's shared stream coexists with the
  recorder on the real default mic (the splice's core hardware claim).
- LiveConsentStoreTests: meeting auto-record's detection source is
  readable on this machine and has real entries.
- LiveWakePipelineTests: a spoken wake phrase (synthesized speech)
  fires exactly one wake through the REAL openWakeWord model and the
  REAL decision logic, hands a well-formed ring-buffer prefix to the
  dictation seam, and unrelated speech never fires. Also proves the
  silero VAD signal that drives the silence auto-stop.
- LiveFailoverTests: with the GPU probe reporting the device gone,
  the guard pins the spawn to cpu and the REAL worker still
  transcribes speech correctly - the failover outcome end to end.
  A second test proves the healthy-probe path on the real GPU.
- LiveRealMeetingTests: the full production pipeline (GPU model,
  align, diarize) on the pinned real-meeting fixture
  (tests/fixtures/real-meeting/, machine-local - see the fixtures
  README), judged against the reference transcript's word volume and
  speaker count. The heavyweight test: ~3-6 minutes on the GPU.
"""

import os
import subprocess
import tempfile
import time
import types
import unittest
import wave
from pathlib import Path
from unittest import mock

_LIVE = os.environ.get("WS_LIVE") == "1"

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    np = None
    _HAS_NUMPY = False

# onnxruntime must initialize BEFORE windows_toasts' WinRT bindings
# enter the process (bisected 2026-07-05): loaded later, its pybind11
# DLL init fails on Windows. Import here at collection time;
# openwakeword picks up the already-loaded module. Harmless if absent.
# The app applies the same preload at the top of __main__.py.
try:
    import onnxruntime
    _ORT_PRELOADED = bool(onnxruntime)
except Exception:
    _ORT_PRELOADED = False


def _tts_wav(text: str, dest: Path) -> bool:
    """Synthesize 16 kHz mono speech via Windows SAPI (no audio out)."""
    if os.name != "nt":
        return False
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo("
        "16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, "
        "[System.Speech.AudioFormat.AudioChannel]::Mono); "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.Rate = -1; "
        f"$s.SetOutputToWaveFile('{dest}', $fmt); "
        f"$s.Speak('{text}'); $s.SetOutputToNull(); $s.Dispose()"
    )
    # Two attempts: a cold PowerShell + .NET assembly load can
    # occasionally blow the first timeout on a busy machine.
    for _ in range(2):
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", script],
                capture_output=True, timeout=120)
            if result.returncode == 0 and dest.exists():
                return True
        except Exception:
            pass
    return False


def _wav_frames(path: Path, pad_s: float = 1.0):
    """A wav as 80ms int16 frames with silence padding either side."""
    with wave.open(str(path)) as wf:
        assert wf.getframerate() == 16000 and wf.getnchannels() == 1
        data = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    pad = np.zeros(int(16000 * pad_s), dtype=np.int16)
    data = np.concatenate([pad, data, pad])
    return [data[i:i + 1280] for i in range(0, len(data) - 1279, 1280)]


def _wav_float32(path: Path):
    """A wav as the float32 mono array transcribe_fast expects."""
    with wave.open(str(path)) as wf:
        data = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    return data.astype(np.float32) / 32768.0


class _ListenerApp:
    """Minimal real-config app surface for WakeListener decisions."""

    def __init__(self):
        from whisper_sync.state_manager import StateManager
        self.cfg = {"wake_listener": True, "wake_threshold": 0.5,
                    "sample_rate": 16000, "wake_phrases": {}}
        self.state = StateManager(None, {})
        self.recorder = types.SimpleNamespace(is_recording=False)
        self.auto_sleep = types.SimpleNamespace(wake=mock.Mock())
        self.dictation = types.SimpleNamespace(
            begin_via_wake=mock.Mock(return_value=True))
        self.flashes = 0

    def _yellow_flash(self):
        self.flashes += 1


@unittest.skipUnless(_LIVE, "set WS_LIVE=1 to run live hardware validation")
@unittest.skipUnless(_HAS_NUMPY, "requires the app venv (numpy)")
class LiveMicTests(unittest.TestCase):
    def test_listener_stream_and_recorder_share_the_real_mic(self):
        # The splice's hardware claim: the always-on listener stream is
        # shared-mode and never blocks the dictation recorder from
        # opening the same device.
        import sounddevice as sd
        from whisper_sync.capture import AudioRecorder, get_default_devices

        mic = get_default_devices().get("input")
        recorder = AudioRecorder(sample_rate=16000)
        with sd.InputStream(samplerate=16000, channels=1, dtype="int16",
                            blocksize=1280) as stream:
            frame, _ = stream.read(1280)
            self.assertEqual(frame.shape[0], 1280)
            self.assertEqual(frame.dtype, np.int16)

            recorder.start(mic_device=mic)
            try:
                time.sleep(0.5)  # let callbacks deliver
                frame, _ = stream.read(1280)  # listener still fed
                self.assertEqual(frame.shape[0], 1280)
            finally:
                audio = recorder.stop()
        self.assertIn("mic", audio, "recorder captured nothing while "
                                    "the listener stream was open")
        self.assertGreater(len(audio["mic"]), 0)

    def test_recorder_prefix_lands_ahead_of_real_capture(self):
        # PR 1's splice seam on real hardware: prefix first, live
        # capture after.
        from whisper_sync.capture import AudioRecorder, get_default_devices

        prefix = np.full((1600, 1), 0.123, dtype=np.float32)
        recorder = AudioRecorder(sample_rate=16000)
        recorder.start(mic_device=get_default_devices().get("input"),
                       prefix_audio=prefix)
        try:
            time.sleep(0.4)
        finally:
            audio = recorder.stop()
        self.assertIn("mic", audio)
        self.assertGreater(len(audio["mic"]), len(prefix),
                           "live capture must follow the prefix")
        np.testing.assert_array_equal(audio["mic"][:len(prefix)], prefix)


@unittest.skipUnless(_LIVE, "set WS_LIVE=1 to run live hardware validation")
class LiveConsentStoreTests(unittest.TestCase):
    def test_consent_store_is_readable_and_populated(self):
        # Meeting auto-record's detection source (read-only registry).
        from whisper_sync.meeting_watch import read_mic_entries

        entries = read_mic_entries()
        if entries is None:
            self.skipTest("consent store unreadable (non-Windows or "
                          "registry access denied)")
        if not entries:
            self.skipTest("no mic-use history on this profile yet - "
                          "use any mic app once, then rerun")
        for key, value in entries.items():
            self.assertEqual(key, key.lower())
            self.assertIsInstance(value, bool)


@unittest.skipUnless(_LIVE, "set WS_LIVE=1 to run live hardware validation")
@unittest.skipUnless(_HAS_NUMPY, "requires the app venv (numpy)")
class LiveWakePipelineTests(unittest.TestCase):
    """Synthesized speech through the REAL model + REAL decision loop."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        cls.wake_wav = tmp / "wake.wav"
        cls.other_wav = tmp / "other.wav"
        if not (_tts_wav("hey jarvis", cls.wake_wav)
                and _tts_wav("the weather is quite nice today",
                             cls.other_wav)):
            raise unittest.SkipTest("Windows TTS unavailable")
        try:
            from openwakeword.model import Model
        except ImportError as exc:
            raise unittest.SkipTest(f"openwakeword unavailable: {exc}")
        cls.Model = Model

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _model(self):
        return self.Model(wakeword_models=["hey_jarvis"],
                          inference_framework="onnx", vad_threshold=0.5)

    def test_spoken_phrase_fires_exactly_one_wake_with_prefix(self):
        from whisper_sync.listener import WakeListener

        app = _ListenerApp()
        listener = WakeListener(app)
        model = self._model()
        for frame in _wav_frames(self.wake_wav):
            listener.handle_frame(frame, model)
            # A successful wake arms the session watcher; drop back to
            # normal listening (mode never becomes "dictation" here)
            # so the refractory window is what prevents double fires.
        begin = app.dictation.begin_via_wake
        self.assertEqual(begin.call_count, 1,
                         "one spoken phrase = exactly one wake")
        prefix = begin.call_args[0][0]
        self.assertIsNotNone(prefix, "ring buffer must produce a prefix")
        self.assertEqual(prefix.dtype, np.float32)
        self.assertEqual(prefix.shape[1], 1)
        self.assertLessEqual(float(np.abs(prefix).max()), 1.0)
        self.assertGreater(len(prefix), 16000,
                           "prefix should hold >1s of pre-wake audio")

    def test_unrelated_speech_never_wakes(self):
        from whisper_sync.listener import WakeListener

        app = _ListenerApp()
        listener = WakeListener(app)
        model = self._model()
        for frame in _wav_frames(self.other_wav):
            listener.handle_frame(frame, model)
        app.dictation.begin_via_wake.assert_not_called()

    def test_vad_distinguishes_speech_from_silence(self):
        # The signal behind wake_silence_stop_s, on the real silero VAD:
        # speech frames must score as voice, silence must not.
        from whisper_sync.listener import VAD_VOICE_THRESHOLD

        model = self._model()
        speech_top = 0.0
        for frame in _wav_frames(self.wake_wav, pad_s=0.0):
            model.predict(frame)
            speech_top = max(speech_top,
                             float(model.vad.prediction_buffer[-1]))
        self.assertGreaterEqual(speech_top, VAD_VOICE_THRESHOLD,
                                "speech must register as voice")

        model = self._model()
        for frame in [np.zeros(1280, dtype=np.int16)] * 25:  # 2s silence
            model.predict(frame)
        self.assertLess(float(model.vad.prediction_buffer[-1]),
                        VAD_VOICE_THRESHOLD,
                        "silence must not register as voice")


@unittest.skipUnless(_LIVE, "set WS_LIVE=1 to run live hardware validation")
@unittest.skipUnless(_HAS_NUMPY, "requires the app venv (numpy)")
class LiveFailoverTests(unittest.TestCase):
    """GPU power-state failover measured end to end, CPU-only."""

    def test_lost_device_pins_spawn_to_cpu_and_transcribes(self):
        # The failover OUTCOME: with the dGPU unreachable (probe
        # returns nothing), the guard pins the spawn to cpu + fallback
        # model and a real worker still transcribes speech correctly.
        # Uses the tiny TTS clip - light, CPU-only, no VRAM touched.
        from whisper_sync import config as ws_config
        from whisper_sync.gpu_guard import GpuGuard
        from whisper_sync.worker_manager import TranscriptionWorker

        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "wake.wav"
            if not _tts_wav("hey jarvis how are you", wav):
                raise unittest.SkipTest("Windows TTS unavailable")

            cfg = ws_config.load()
            # event_path pinned to the temp dir: the guard must not
            # write test events into the production gpu-guard.jsonl.
            guard = GpuGuard(cfg, probe=lambda: None, probe_name="dead",
                             event_path=Path(tmp) / "gpu-guard.jsonl")
            guard.prime()
            self.assertTrue(guard.device_lost,
                            "a dead probe at startup must declare loss")
            overlay = guard.respawn_overlay()
            self.assertEqual(overlay["device"], "cpu")
            self.assertEqual(overlay["model"], cfg.get("cpu_fallback_model",
                                                       "base"))

            worker_cfg = {**cfg, **overlay}
            worker = TranscriptionWorker(worker_cfg,
                                         preload_model=overlay["model"])
            worker.start()
            try:
                self.assertTrue(worker.wait_ready(timeout=300),
                                "cpu fallback worker must come up")
                text = worker.transcribe_fast(
                    _wav_float32(wav), model_override=overlay["model"],
                    timeout=120)
                self.assertTrue(text and "jarvis" in text.lower(),
                                f"cpu transcription must be usable, "
                                f"got: {text!r}")
            finally:
                worker.stop()

    def test_healthy_real_probe_reports_gpu_present(self):
        # The other half of the switch: on THIS machine the real probe
        # must see the GPU, so the guard never fails over spuriously.
        from whisper_sync import config as ws_config
        from whisper_sync.gpu_guard import GpuGuard

        with tempfile.TemporaryDirectory() as tmp:
            guard = GpuGuard(ws_config.load(),
                             event_path=Path(tmp) / "gpu-guard.jsonl")
            guard.prime()
        self.assertFalse(guard.device_lost,
                         "real probe must reach the GPU on this machine")
        self.assertIsNone(guard.respawn_overlay(),
                          "healthy device -> live config, no pinning")


_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "real-meeting"


@unittest.skipUnless(_LIVE, "set WS_LIVE=1 to run live hardware validation")
@unittest.skipUnless(_HAS_NUMPY, "requires the app venv (numpy)")
@unittest.skipUnless(
    (_FIXTURE_DIR / "recording.wav").exists()
    and (_FIXTURE_DIR / "reference-transcript.json").exists(),
    "real-meeting fixture missing - see tests/fixtures/README.md")
class LiveRealMeetingTests(unittest.TestCase):
    """The production pipeline on the pinned real meeting (GPU)."""

    def test_full_pipeline_matches_reference_quality(self):
        import json
        import shutil
        from whisper_sync import config as ws_config
        from whisper_sync.worker_manager import TranscriptionWorker

        reference = json.loads(
            (_FIXTURE_DIR / "reference-transcript.json")
            .read_text(encoding="utf-8"))
        ref_segments = reference.get("segments", [])
        ref_words = sum(len(s.get("text", "").split()) for s in ref_segments)
        ref_speakers = {s.get("speaker") for s in ref_segments
                        if s.get("speaker")}
        self.assertGreater(ref_words, 100, "fixture sanity")

        cfg = ws_config.load()
        worker = TranscriptionWorker(cfg,
                                     preload_model=cfg.get("model",
                                                           "large-v3"))
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "recording.wav"
            shutil.copy2(_FIXTURE_DIR / "recording.wav", wav)
            worker.start()
            try:
                self.assertTrue(worker.wait_ready(timeout=300),
                                "production model must load")
                result = worker.transcribe(str(wav), diarize=True,
                                           timeout=1800)
                json_path = Path(result["json_path"])
                data = json.loads(json_path.read_text(encoding="utf-8"))
            finally:
                worker.stop()

        segments = data.get("segments", [])
        self.assertGreater(len(segments), 20,
                           "a 6-minute conversation yields many segments")
        words = sum(len(s.get("text", "").split()) for s in segments)
        self.assertGreaterEqual(
            words, int(ref_words * 0.6),
            f"word volume collapsed vs reference ({words}/{ref_words})")
        speakers = {s.get("speaker") for s in segments if s.get("speaker")}
        self.assertGreaterEqual(
            len(speakers), 2,
            f"diarization must separate real speakers "
            f"(reference had {len(ref_speakers)})")
        last_end = max((s.get("end", 0) for s in segments), default=0)
        self.assertGreater(last_end, 250,
                           "transcription must cover most of the meeting")


if __name__ == "__main__":
    unittest.main()
