"""Tests for AudioRecorder state transitions.

These tests drive the recorder directly (without opening a real audio
device) to pin behavior that review surfaced:

  * start() must not leave ``_recording=True`` if the mic open fails
    (Copilot review comment #3 on PR 125).
  * start_streaming() must not flip ``_disk_only`` if the WAV writer
    fails to open - otherwise the callback would silently skip both
    RAM accumulation and disk write (comment #2).
  * _mic_callback must normalize int16 samples to [-1, 1] BEFORE
    resampling (comment #1).
"""

import types
import unittest
from unittest import mock

import numpy as np


class _FakePortAudioError(Exception):
    pass


def _make_recorder(install_fake_sd=True):
    """Build an AudioRecorder with a fake sd module swapped in."""
    from whisper_sync import capture
    recorder = capture.AudioRecorder(sample_rate=16000)
    if install_fake_sd:
        fake = types.SimpleNamespace()
        fake.PortAudioError = _FakePortAudioError
        fake.InputStream = mock.Mock()
        fake.query_devices = lambda device: {"default_samplerate": 48000.0}
        capture.sd = fake
    return recorder, capture


class StartTransactionalTests(unittest.TestCase):
    def test_recording_stays_false_when_open_fails(self):
        recorder, capture = _make_recorder()
        self.addCleanup(lambda: setattr(capture, "sd",
                                        __import__("sounddevice")))
        capture.sd.InputStream.side_effect = _FakePortAudioError("always fail")

        with self.assertRaises(_FakePortAudioError):
            recorder.start(mic_device=7)

        self.assertFalse(recorder._recording)
        self.assertIsNone(recorder._mic_stream)

    def test_open_stream_is_closed_if_start_fails(self):
        recorder, capture = _make_recorder()
        self.addCleanup(lambda: setattr(capture, "sd",
                                        __import__("sounddevice")))
        stream = mock.Mock()
        stream.start.side_effect = RuntimeError("start fails")
        capture.sd.InputStream.return_value = stream

        with self.assertRaises(RuntimeError):
            recorder.start(mic_device=7)

        stream.close.assert_called_once()
        self.assertFalse(recorder._recording)
        self.assertIsNone(recorder._mic_stream)


class StartStreamingTransactionalTests(unittest.TestCase):
    def test_disk_only_not_flipped_when_writer_fails(self):
        recorder, capture = _make_recorder(install_fake_sd=False)
        recorder._disk_only = False  # baseline

        with mock.patch.object(capture, "StreamingWavWriter",
                               side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                recorder.start_streaming(mic_path="/tmp/x.wav", disk_only=True)

        self.assertFalse(recorder._disk_only,
                         "_disk_only must stay False when writer open fails")
        self.assertIsNone(recorder._mic_writer)


class MicCallbackNormalizationTests(unittest.TestCase):
    def test_int16_samples_are_scaled_to_minus_one_to_one(self):
        from whisper_sync.capture import AudioRecorder
        recorder = AudioRecorder(sample_rate=16000)
        recorder._recording = True
        # Same-rate path (no resample). int16 input must be normalized.
        recorder._mic_resample_up = 1
        recorder._mic_resample_down = 1

        int16_block = np.array([[32767], [-32768], [0]], dtype=np.int16)
        recorder._mic_callback(int16_block, 3, None, None)

        self.assertEqual(len(recorder._mic_data), 1)
        out = recorder._mic_data[0]
        self.assertEqual(out.dtype, np.float32)
        # 32767/32768.0 ~= 1.0; -32768/32768.0 == -1.0; 0 == 0
        self.assertAlmostEqual(float(out[0, 0]), 1.0, places=3)
        self.assertAlmostEqual(float(out[1, 0]), -1.0, places=6)
        self.assertAlmostEqual(float(out[2, 0]), 0.0, places=6)

    def test_int16_with_resample_stays_normalized(self):
        # Resample + int16 path was the original bug: values stayed in
        # the [-32768, 32767] range and would clip catastrophically.
        from whisper_sync.capture import AudioRecorder
        recorder = AudioRecorder(sample_rate=16000)
        recorder._recording = True
        recorder._mic_effective_rate = 48000
        recorder._mic_resample_up = 1
        recorder._mic_resample_down = 3

        # A 48-sample int16 buffer at full positive scale.
        block = np.full((48, 1), 32767, dtype=np.int16)
        recorder._mic_callback(block, 48, None, None)

        self.assertEqual(len(recorder._mic_data), 1)
        out = recorder._mic_data[0]
        self.assertEqual(out.dtype, np.float32)
        # After resampling a constant signal, values should still be ~1.0,
        # nowhere near 32767. Accept a generous upper bound.
        peak = float(np.max(np.abs(out)))
        self.assertLess(peak, 1.1,
                        f"int16 was not normalized before resampling; peak={peak}")

    def test_float32_passthrough_same_rate(self):
        from whisper_sync.capture import AudioRecorder
        recorder = AudioRecorder(sample_rate=16000)
        recorder._recording = True
        recorder._mic_resample_up = 1
        recorder._mic_resample_down = 1

        block = np.array([[0.5], [-0.25], [0.0]], dtype=np.float32)
        recorder._mic_callback(block, 3, None, None)

        out = recorder._mic_data[0]
        self.assertEqual(out.dtype, np.float32)
        np.testing.assert_array_equal(out, block)


if __name__ == "__main__":
    unittest.main()
