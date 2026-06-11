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
        # Return a stable device name keyed off the requested device id, so
        # the per-device cache key is deterministic across test calls.
        fake.query_devices = lambda device: {
            "default_samplerate": 48000.0,
            "name": f"FakeMic-{device}",
            "max_input_channels": 4,
        }

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
        stream, rate, channels = _open_input_stream(
            device=7, target_samplerate=16000, channels=1,
            dtype="float32", callback=lambda *_: None,
        )
        self.assertIsNotNone(stream)
        self.assertEqual(rate, 16000)
        self.assertEqual(len(self.calls), 1)

    def test_falls_back_to_native_samplerate_on_portaudio_error(self):
        # Device rejects ANYTHING at 16 kHz (rate problem, like MME error
        # 32) regardless of channel count; accepts native 48 kHz.
        def _responder(kwargs):
            if kwargs["samplerate"] == 16000:
                raise _FakePortAudioError("MME error 32")
            return mock.Mock(name="stream")

        self._responder = _responder
        from whisper_sync.capture import _open_input_stream
        stream, rate, channels = _open_input_stream(
            device=7, target_samplerate=16000, channels=1,
            dtype="float32", callback=lambda *_: None,
        )
        self.assertIsNotNone(stream)
        self.assertEqual(rate, 48000)  # device default from fake query_devices
        self.assertEqual(channels, 1, "rate fallback should keep mono")
        self.assertEqual(self.calls[0]["samplerate"], 16000)
        self.assertEqual(self.calls[-1]["samplerate"], 48000)

    def test_falls_back_to_int16_when_float32_and_native_both_fail(self):
        # Device only accepts int16 (any rate/channels).
        def _responder(kwargs):
            if kwargs["dtype"] != "int16":
                raise _FakePortAudioError("float32 rejected")
            return mock.Mock(name="stream")

        self._responder = _responder
        from whisper_sync.capture import _open_input_stream
        stream, rate, channels = _open_input_stream(
            device=7, target_samplerate=16000, channels=1,
            dtype="float32", callback=lambda *_: None,
        )
        self.assertIsNotNone(stream)
        self.assertEqual(rate, 48000)
        self.assertEqual(self.calls[-1]["dtype"], "int16")

    def test_mic_array_rejecting_mono_falls_back_to_native_channels(self):
        # The user-reported case: a laptop 4-mic array refuses ANY mono
        # open but accepts its native 4-channel format. Previously every
        # rung used channels=1, all failed, and the app was unusable with
        # the built-in mic.
        def _responder(kwargs):
            if kwargs["channels"] == 1:
                raise _FakePortAudioError("mono not supported by mic array")
            return mock.Mock(name="stream")

        self._responder = _responder
        from whisper_sync.capture import _open_input_stream
        stream, rate, channels = _open_input_stream(
            device=7, target_samplerate=16000, channels=1,
            dtype="float32", callback=lambda *_: None,
        )
        self.assertIsNotNone(stream)
        self.assertEqual(channels, 4, "must fall back to native channel count")
        self.assertEqual(rate, 16000, "rate should stay at target when only channels failed")
        # Mono was attempted FIRST (preferred), 4ch second.
        self.assertEqual(self.calls[0]["channels"], 1)
        self.assertEqual(self.calls[1]["channels"], 4)

    def test_channel_fallback_caches_effective_channels(self):
        from whisper_sync import capture
        from whisper_sync.capture import _open_input_stream, _MIC_FORMAT_CACHE

        def _responder(kwargs):
            if kwargs["channels"] == 1:
                raise _FakePortAudioError("mono not supported")
            return mock.Mock(name="stream")

        self._responder = _responder
        _open_input_stream(
            device=7, target_samplerate=16000, channels=1,
            dtype="float32", callback=lambda *_: None,
        )
        with capture._MIC_FORMAT_CACHE_LOCK:
            cached = _MIC_FORMAT_CACHE[("FakeMic-7", 16000, "float32", 1)]
        self.assertEqual(cached, (16000, "float32", 4))

        # Second open: single cache-hit call straight at 4 channels.
        first_calls = len(self.calls)
        _open_input_stream(
            device=7, target_samplerate=16000, channels=1,
            dtype="float32", callback=lambda *_: None,
        )
        cache_calls = self.calls[first_calls:]
        self.assertEqual(len(cache_calls), 1)
        self.assertEqual(cache_calls[0]["channels"], 4)

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

    def test_cache_keyed_by_device_name_not_raw_arg(self):
        # device=None and device=7 must produce DIFFERENT cache entries
        # even though both could resolve to "the default mic" at different
        # times. The fake query_devices returns "FakeMic-None" for None
        # and "FakeMic-7" for 7, so the keys differ.
        from whisper_sync import capture
        from whisper_sync.capture import _open_input_stream, _MIC_FORMAT_CACHE

        responses = [
            _FakePortAudioError("16k rejected"),
            mock.Mock(name="stream-A"),  # device=None succeeds at 48k
            _FakePortAudioError("16k rejected again"),
            mock.Mock(name="stream-B"),  # device=7 succeeds at 48k
        ]

        def _responder(kwargs):
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        self._responder = _responder

        _open_input_stream(
            device=None, target_samplerate=16000, channels=1,
            dtype="float32", callback=lambda *_: None,
        )
        _open_input_stream(
            device=7, target_samplerate=16000, channels=1,
            dtype="float32", callback=lambda *_: None,
        )

        with capture._MIC_FORMAT_CACHE_LOCK:
            keys = list(_MIC_FORMAT_CACHE.keys())
        device_names = sorted(k[0] for k in keys)
        self.assertEqual(
            device_names, ["FakeMic-7", "FakeMic-None"],
            "cache must be keyed by resolved device name, not raw device arg",
        )

    def test_cache_skips_probe_on_second_open(self):
        # First open: device rejects 16 kHz (any channels), accepts 48 kHz
        # mono -> cache (48000, float32, 1). Second open MUST hit the
        # cache: a single InputStream call at 48 kHz, no 16 kHz probe.
        def _responder(kwargs):
            if kwargs["samplerate"] == 16000:
                raise _FakePortAudioError("16k rejected")
            return mock.Mock(name="stream")

        self._responder = _responder
        from whisper_sync.capture import _open_input_stream

        _open_input_stream(
            device=7, target_samplerate=16000, channels=1,
            dtype="float32", callback=lambda *_: None,
        )
        first_calls = len(self.calls)
        self.assertGreater(first_calls, 1, "first open must probe")

        _open_input_stream(
            device=7, target_samplerate=16000, channels=1,
            dtype="float32", callback=lambda *_: None,
        )
        cache_calls = self.calls[first_calls:]
        self.assertEqual(
            len(cache_calls), 1,
            "second open should skip the probe and only call InputStream once",
        )
        self.assertEqual(cache_calls[0]["samplerate"], 48000)
        self.assertEqual(cache_calls[0]["dtype"], "float32")
        self.assertEqual(cache_calls[0]["channels"], 1)

    def test_cache_evicts_and_reprobes_when_cached_format_fails(self):
        # Prime cache with a known-good (48000, float32, 1ch) for device 7.
        # The cache key uses the resolved device NAME (from
        # sd.query_devices), not the raw device id, so changing the OS
        # default mic invalidates the entry automatically.
        from whisper_sync import capture
        from whisper_sync.capture import _open_input_stream, _MIC_FORMAT_CACHE
        cache_key = ("FakeMic-7", 16000, "float32", 1)
        with capture._MIC_FORMAT_CACHE_LOCK:
            _MIC_FORMAT_CACHE[cache_key] = (48000, "float32", 1)

        # Device state changed: 48 kHz now rejected, only 16 kHz works.
        # The helper must evict the stale entry, re-probe, and re-cache.
        def _responder(kwargs):
            if kwargs["samplerate"] == 48000:
                raise _FakePortAudioError("48k now rejected")
            return mock.Mock(name="recovered-stream")

        self._responder = _responder
        stream, rate, channels = _open_input_stream(
            device=7, target_samplerate=16000, channels=1,
            dtype="float32", callback=lambda *_: None,
        )
        self.assertIsNotNone(stream)
        self.assertEqual(rate, 16000)
        self.assertEqual(channels, 1)
        # First call was the (failed) cached 48 kHz attempt.
        self.assertEqual(self.calls[0]["samplerate"], 48000)
        # Cache re-populated with the now-working entry.
        with capture._MIC_FORMAT_CACHE_LOCK:
            self.assertEqual(
                _MIC_FORMAT_CACHE.get(cache_key), (16000, "float32", 1),
                "cache should refresh after eviction",
            )


if __name__ == "__main__":
    unittest.main()
