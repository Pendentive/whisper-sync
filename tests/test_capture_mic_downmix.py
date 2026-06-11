"""Tests for multi-channel mic downmix (laptop mic-array support).

Mic arrays (e.g. 4-mic laptop arrays) may reject a mono open entirely;
_open_input_stream falls back to the device's native channel count and
_mic_callback downmixes to mono (mean across channels) before resampling
and writing. These tests drive _mic_callback directly with synthetic
multi-channel audio and verify the downmix math.

Requires numpy/scipy (capture.py imports them at module level). Run under
the app venv:

    whisper-env/Scripts/python.exe -m unittest tests.test_capture_mic_downmix
"""

import unittest

try:
    import numpy as np
    import scipy.signal  # noqa: F401 -- capture.py imports it at module level
    _HAS_DEPS = True
except ImportError:
    _HAS_DEPS = False


@unittest.skipUnless(_HAS_DEPS, "requires numpy AND scipy (run under app venv)")
class MicDownmixTests(unittest.TestCase):
    def _make_recorder(self, channels=4, effective_rate=16000):
        from whisper_sync.capture import AudioRecorder
        rec = AudioRecorder(sample_rate=16000)
        rec._recording = True
        rec._mic_channels = channels
        rec._mic_effective_rate = effective_rate
        if effective_rate != 16000:
            from math import gcd
            g = gcd(16000, effective_rate)
            rec._mic_resample_up = 16000 // g
            rec._mic_resample_down = effective_rate // g
        return rec

    def test_four_channel_input_downmixes_to_channel_mean(self):
        rec = self._make_recorder(channels=4)
        # Channels carry constants 0.1, 0.2, 0.3, 0.4 -> mean 0.25.
        frames = 1024
        indata = np.tile(
            np.array([0.1, 0.2, 0.3, 0.4], dtype=np.float32), (frames, 1)
        )

        rec._mic_callback(indata, frames, None, None)

        self.assertEqual(len(rec._mic_data), 1)
        mono = rec._mic_data[0]
        self.assertEqual(mono.shape, (frames, 1), "output must be mono")
        np.testing.assert_allclose(
            mono, np.full((frames, 1), 0.25, dtype=np.float32), atol=1e-6,
        )

    def test_downmix_preserves_signal_content(self):
        # Same 440 Hz sine on all 4 channels: the mono mean must equal the
        # original sine (no attenuation, no phase change).
        rec = self._make_recorder(channels=4)
        frames = 1600
        t = np.arange(frames, dtype=np.float32) / 16000.0
        sine = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        indata = np.tile(sine.reshape(-1, 1), (1, 4))

        rec._mic_callback(indata, frames, None, None)

        mono = rec._mic_data[0].reshape(-1)
        np.testing.assert_allclose(mono, sine, atol=1e-6)

    def test_downmix_int16_input_normalizes_then_averages(self):
        rec = self._make_recorder(channels=2)
        frames = 64
        # ch0 = +16384 (0.5 after /32768), ch1 = -16384 (-0.5) -> mean 0.0
        indata = np.column_stack([
            np.full(frames, 16384, dtype=np.int16),
            np.full(frames, -16384, dtype=np.int16),
        ])

        rec._mic_callback(indata, frames, None, None)

        mono = rec._mic_data[0]
        np.testing.assert_allclose(
            mono, np.zeros((frames, 1), dtype=np.float32), atol=1e-6,
        )

    def test_downmix_composes_with_resampling(self):
        # 48 kHz 4-channel device: downmix to mono FIRST, then 3:1
        # decimation to 16 kHz. One second in -> 16000 mono frames out.
        rec = self._make_recorder(channels=4, effective_rate=48000)
        frames = 48000
        indata = np.full((frames, 4), 0.25, dtype=np.float32)

        rec._mic_callback(indata, frames, None, None)

        mono = rec._mic_data[0]
        self.assertEqual(mono.shape[1], 1, "output must be mono")
        self.assertEqual(len(mono), 16000, "48k input must resample to 16k")
        # DC level survives downmix + resample (ignore FIR edge transients).
        np.testing.assert_allclose(
            mono[100:-100].reshape(-1),
            np.full(len(mono) - 200, 0.25, dtype=np.float32),
            atol=1e-3,
        )

    def test_downmix_output_stays_float32(self):
        # Regression for Copilot review on PR #143: the downmix result
        # must remain float32 (the callback's normalization invariant) on
        # every input dtype, including a hypothetical float64 input.
        for in_dtype in (np.float32, np.int16, np.float64):
            rec = self._make_recorder(channels=4)
            frames = 64
            if in_dtype == np.int16:
                indata = np.full((frames, 4), 1000, dtype=in_dtype)
            else:
                indata = np.full((frames, 4), 0.25, dtype=in_dtype)
            rec._mic_callback(indata, frames, None, None)
            self.assertEqual(
                rec._mic_data[0].dtype, np.float32,
                f"downmix of {in_dtype.__name__} input must yield float32",
            )

    def test_mono_input_unaffected(self):
        # channels=1 sessions must behave exactly as before.
        rec = self._make_recorder(channels=1)
        frames = 256
        indata = np.full((frames, 1), 0.7, dtype=np.float32)

        rec._mic_callback(indata, frames, None, None)

        mono = rec._mic_data[0]
        np.testing.assert_allclose(
            mono, np.full((frames, 1), 0.7, dtype=np.float32), atol=1e-6,
        )

    def test_downmixed_audio_reaches_disk_writer(self):
        # Meeting path: multi-channel mic with disk streaming must write
        # MONO frames to the WAV writer (header says channels=1).
        class _FakeWriter:
            def __init__(self):
                self.writes = []

            def write(self, chunk):
                self.writes.append(chunk.copy())

        rec = self._make_recorder(channels=4)
        rec._disk_only = True
        writer = _FakeWriter()
        rec._mic_writer = writer

        frames = 512
        indata = np.full((frames, 4), 0.2, dtype=np.float32)
        rec._mic_callback(indata, frames, None, None)

        self.assertEqual(len(writer.writes), 1)
        self.assertEqual(writer.writes[0].shape, (frames, 1),
                         "disk writer must receive mono frames")


if __name__ == "__main__":
    unittest.main()
