"""Tests for speaker-loopback disk streaming (flat-RAM meetings).

Requires numpy/scipy (capture.py imports them at module level), so the
whole module skips on interpreters without them — same convention as the
other capture tests. Run with the app venv:

    whisper-env/Scripts/python.exe -m unittest tests.test_capture_speaker_streaming
"""

import unittest

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False


class _FakeWriter:
    """Records write() calls; mimics StreamingWavWriter surface."""

    def __init__(self, path="fake/speaker-temp.wav"):
        from pathlib import Path
        self.path = Path(path)
        self.writes = []
        self.closed = False

    def write(self, chunk):
        self.writes.append(chunk.copy())

    def close(self):
        self.closed = True


@unittest.skipUnless(_HAS_NUMPY, "requires numpy/scipy (run under app venv)")
class SpeakerStreamingTests(unittest.TestCase):
    def _make_recorder(self):
        from whisper_sync.capture import AudioRecorder
        return AudioRecorder(sample_rate=16000)

    def test_chunks_stream_to_writer_not_ram(self):
        rec = self._make_recorder()
        rec._speaker_native_rate = 48000
        writer = _FakeWriter()
        rec._speaker_writer = writer

        # Feed 2 seconds of 48 kHz audio in 1000-frame chunks.
        chunk = np.zeros((1000, 1), dtype=np.float32)
        for _ in range(96):
            rec._ingest_speaker_chunk(chunk)

        self.assertEqual(
            rec._speaker_data, [],
            "disk-streaming mode must not accumulate chunks in RAM",
        )
        self.assertGreaterEqual(len(writer.writes), 1, "blocks must reach the writer")
        total_frames = sum(len(w) for w in writer.writes)
        # 96k frames @48k resampled to 16k -> ~32k frames written so far
        # (the tail below one block may still be buffered).
        self.assertGreater(total_frames, 16000)

    def test_resampled_to_target_rate(self):
        rec = self._make_recorder()
        rec._speaker_native_rate = 48000
        writer = _FakeWriter()
        rec._speaker_writer = writer

        # Exactly 1 second @48k triggers exactly one block flush.
        chunk = np.ones((48000, 1), dtype=np.float32) * 0.5
        rec._ingest_speaker_chunk(chunk)

        self.assertEqual(len(writer.writes), 1)
        # 48000 native frames -> 16000 target frames (3:1 decimation)
        self.assertEqual(len(writer.writes[0]), 16000)

    def test_ram_fallback_without_writer(self):
        rec = self._make_recorder()
        rec._speaker_native_rate = 48000
        rec._speaker_writer = None

        chunk = np.zeros((1000, 1), dtype=np.float32)
        rec._ingest_speaker_chunk(chunk)
        self.assertEqual(len(rec._speaker_data), 1, "no writer -> RAM as before")

    def test_stop_flushes_tail_and_returns_speaker_path(self):
        rec = self._make_recorder()
        rec._speaker_native_rate = 48000
        writer = _FakeWriter()
        rec._speaker_writer = writer

        # Half a second buffered — below the 1s flush threshold.
        chunk = np.zeros((24000, 1), dtype=np.float32)
        rec._ingest_speaker_chunk(chunk)
        self.assertEqual(writer.writes, [], "tail below 1s stays buffered")

        result = rec.stop()

        self.assertTrue(writer.closed, "stop() must finalize the speaker WAV")
        self.assertEqual(len(writer.writes), 1, "stop() must flush the tail block")
        self.assertEqual(len(writer.writes[0]), 8000)  # 24000 @48k -> 8000 @16k
        self.assertEqual(result.get("speaker_path"), writer.path)
        self.assertNotIn("speaker", result, "disk path replaces in-memory array")
        self.assertIsNone(rec._speaker_writer)

    def test_start_streaming_opens_speaker_writer_when_loopback_active(self):
        from whisper_sync import capture
        rec = self._make_recorder()
        rec._speaker_pa_stream = object()  # loopback "active"
        rec._speaker_native_rate = 48000

        created = []

        class _FakeWavWriter(_FakeWriter):
            def __init__(self, path, channels=1, rate=16000):
                super().__init__(path)
                self.channels = channels
                self.rate = rate
                created.append(self)

        orig = capture.StreamingWavWriter
        capture.StreamingWavWriter = _FakeWavWriter
        try:
            rec.start_streaming("fake/mic-temp.wav", disk_only=True)
        finally:
            capture.StreamingWavWriter = orig

        self.assertEqual(len(created), 2, "mic AND speaker writers must open")
        speaker = created[1]
        self.assertEqual(speaker.path.name, "speaker-temp.wav")
        self.assertEqual(
            speaker.rate, 16000,
            "speaker WAV must be written at target rate (post-resample)",
        )
        self.assertIs(rec._speaker_writer, speaker)

    def test_start_streaming_ram_fallback_when_speaker_writer_fails(self):
        from whisper_sync import capture
        rec = self._make_recorder()
        rec._speaker_pa_stream = object()

        calls = []

        class _FlakyWriter(_FakeWriter):
            def __init__(self, path, channels=1, rate=16000):
                calls.append(path)
                if len(calls) == 2:  # mic ok, speaker fails
                    raise OSError("disk full")
                super().__init__(path)

        orig = capture.StreamingWavWriter
        capture.StreamingWavWriter = _FlakyWriter
        try:
            rec.start_streaming("fake/mic-temp.wav", disk_only=True)
        finally:
            capture.StreamingWavWriter = orig

        self.assertIsNone(rec._speaker_writer, "failed open must fall back to RAM")
        # And the RAM path still works
        chunk = np.zeros((100, 1), dtype=np.float32)
        rec._ingest_speaker_chunk(chunk)
        self.assertEqual(len(rec._speaker_data), 1)


if __name__ == "__main__":
    unittest.main()
