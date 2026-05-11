"""Tests for capture.AudioRecorder input-stream open behavior.

The mic stream is opened inside ``start()`` via ``sd.InputStream(...)``.
On Windows, ``device=None`` selects the MME default device which often
rejects ``float32 @ 16000 Hz`` with ``PaErrorCode -9999 / MME error 32``.
Before this change the error propagated up to the keyboard dispatcher
thread and killed it, bricking every hotkey.

``_open_input_stream`` encapsulates the retry ladder:
  1. Try the caller-provided ``samplerate`` / ``dtype``.
  2. On ``PortAudioError``, retry at the device's native samplerate.
  3. On ``PortAudioError`` again, retry at native rate + ``int16``.

The helper returns the opened stream plus the effective samplerate so
callers can resample downstream if needed.
"""

import types
import unittest
from unittest import mock


class _FakePortAudioError(Exception):
    """Stand-in for sounddevice.PortAudioError."""


class OpenInputStreamLadderTests(unittest.TestCase):
    def _install_fake_sd(self):
        """Install a fake ``sd`` module inside capture for a single test."""
        fake = types.SimpleNamespace()
        fake.PortAudioError = _FakePortAudioError

        self.calls = []

        def _fake_input_stream(**kwargs):
            self.calls.append(kwargs)
            # Behavior is driven by the test: self._responder is swapped in.
            return self._responder(kwargs)

        fake.InputStream = _fake_input_stream
        fake.query_devices = lambda device: {"default_samplerate": 48000.0}

        from whisper_sync import capture
        self._orig_sd = capture.sd
        capture.sd = fake
        self.addCleanup(lambda: setattr(capture, "sd", self._orig_sd))

    def setUp(self):
        self._install_fake_sd()
        # Clear the per-device cache so prior test state doesn't bleed in.
        from whisper_sync import capture
        with capture._MIC_FORMAT_CACHE_LOCK:
            capture._MIC_FORMAT_CACHE.clear()

    def test_first_attempt_succeeds_returns_requested_rate(self):
        self._responder = lambda kwargs: mock.Mock(name="stream")
        from whisper_sync.capture import _open_input_stream
        stream, rate = _open_input_stream(
            device=7, target_samplerate=16000, channels=1,
            dtype="float32", callback=lambda *_: None,
        )
        self.assertIsNotNone(stream)
        self.assertEqual(rate, 16000)
        self.assertEqual(len(self.calls), 1)

    def test_falls_back_to_native_samplerate_on_portaudio_error(self):
        responses = [
            _FakePortAudioError("MME error 32"),  # first try
            mock.Mock(name="stream"),             # second try ok
        ]

        def _responder(kwargs):
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        self._responder = _responder
        from whisper_sync.capture import _open_input_stream
        stream, rate = _open_input_stream(
            device=7, target_samplerate=16000, channels=1,
            dtype="float32", callback=lambda *_: None,
        )
        self.assertIsNotNone(stream)
        self.assertEqual(rate, 48000)  # device default from fake query_devices
        # First attempt at 16000, second at 48000
        self.assertEqual(self.calls[0]["samplerate"], 16000)
        self.assertEqual(self.calls[1]["samplerate"], 48000)

    def test_falls_back_to_int16_when_float32_and_native_both_fail(self):
        responses = [
            _FakePortAudioError("float32/16k rejected"),
            _FakePortAudioError("float32/48k rejected"),
            mock.Mock(name="stream"),
        ]

        def _responder(kwargs):
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        self._responder = _responder
        from whisper_sync.capture import _open_input_stream
        stream, rate = _open_input_stream(
            device=7, target_samplerate=16000, channels=1,
            dtype="float32", callback=lambda *_: None,
        )
        self.assertIsNotNone(stream)
        self.assertEqual(rate, 48000)
        self.assertEqual(self.calls[-1]["dtype"], "int16")

    def test_reraises_if_all_attempts_fail(self):
        def _responder(kwargs):
            raise _FakePortAudioError("always fails")
        self._responder = _responder
        from whisper_sync.capture import _open_input_stream
        with self.assertRaises(_FakePortAudioError):
            _open_input_stream(
                device=7, target_samplerate=16000, channels=1,
                dtype="float32", callback=lambda *_: None,
            )

    def test_cache_skips_probe_on_second_open(self):
        # First call: 16000 fails, 48000 succeeds -> cache (48000, float32)
        # for this device. Second call MUST hit the cache and open at 48000
        # directly with NO probe of 16000.
        responses = [
            _FakePortAudioError("16k rejected"),
            mock.Mock(name="stream-1"),
            mock.Mock(name="stream-2"),
        ]

        def _responder(kwargs):
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        self._responder = _responder
        from whisper_sync.capture import _open_input_stream

        # First open: probes both rates.
        _open_input_stream(
            device=7, target_samplerate=16000, channels=1,
            dtype="float32", callback=lambda *_: None,
        )
        first_calls = list(self.calls)
        self.assertEqual(len(first_calls), 2)

        # Second open: cache hit, single attempt at 48000.
        _open_input_stream(
            device=7, target_samplerate=16000, channels=1,
            dtype="float32", callback=lambda *_: None,
        )
        cache_calls = self.calls[len(first_calls):]
        self.assertEqual(
            len(cache_calls), 1,
            "second open should skip the probe and only call InputStream once",
        )
        self.assertEqual(cache_calls[0]["samplerate"], 48000)
        self.assertEqual(cache_calls[0]["dtype"], "float32")

    def test_cache_evicts_and_reprobes_when_cached_format_fails(self):
        # Prime cache with a known-good (48000, float32) for device 7.
        from whisper_sync import capture
        from whisper_sync.capture import _open_input_stream, _MIC_FORMAT_CACHE
        cache_key = (7, 16000, "float32", 1)
        with capture._MIC_FORMAT_CACHE_LOCK:
            _MIC_FORMAT_CACHE[cache_key] = (48000, "float32")

        # Now simulate the device suddenly rejecting the cached format.
        # The helper must evict the stale cache entry and re-probe.
        responses = [
            _FakePortAudioError("cached rate now rejected"),  # cached attempt
            _FakePortAudioError("16k still rejected"),         # probe 16k
            mock.Mock(name="recovered-stream"),                # probe 48k succeeds
        ]

        def _responder(kwargs):
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        self._responder = _responder
        stream, rate = _open_input_stream(
            device=7, target_samplerate=16000, channels=1,
            dtype="float32", callback=lambda *_: None,
        )
        self.assertIsNotNone(stream)
        self.assertEqual(rate, 48000)
        # Cache should have been re-populated with the now-working entry.
        with capture._MIC_FORMAT_CACHE_LOCK:
            self.assertEqual(
                _MIC_FORMAT_CACHE.get(cache_key), (48000, "float32"),
                "cache should refresh after eviction",
            )


if __name__ == "__main__":
    unittest.main()
