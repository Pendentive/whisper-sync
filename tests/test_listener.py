"""Tests for whisper_sync.listener - the wake-word POC.

The decision logic (score threshold, refractory window, pause gating,
thread lifecycle) runs against fakes: no audio, no openwakeword. The
live audio loop is exercised manually for the POC and gets harness
coverage with the tier-2 splice.
"""

import time
import types
import unittest
from unittest import mock

try:
    import numpy as np
except ImportError:  # dependency-light system python (CI)
    np = None

from whisper_sync.listener import (
    WakeListener, strip_leading_phrase, RING_FRAMES,
)
from whisper_sync.state_manager import StateManager


class _FakeApp:
    def __init__(self):
        self.cfg = {"wake_listener": True, "wake_threshold": 0.5,
                    "sample_rate": 16000}
        self.state = StateManager(None, {})
        self.recorder = types.SimpleNamespace(is_recording=False)
        self.auto_sleep = types.SimpleNamespace(wake=mock.Mock())
        self.dictation = types.SimpleNamespace(
            begin_via_wake=mock.Mock(return_value=True))
        self.flashes = 0

    def _yellow_flash(self):
        self.flashes += 1


class _FakeModel:
    """Stands in for the openWakeWord model in frame-handling tests."""

    def __init__(self, scores=None, vad_score=1.0):
        self.scores = scores if scores is not None else {"hey_jarvis": 0.0}
        self.resets = 0
        self.predicted = []
        self.vad = types.SimpleNamespace(prediction_buffer=[vad_score])

    def predict(self, mono):
        self.predicted.append(mono)
        return dict(self.scores)

    def reset(self):
        self.resets += 1


class WakeDecisionTests(unittest.TestCase):
    def setUp(self):
        self.app = _FakeApp()
        self.fired = []
        self.listener = WakeListener(self.app,
                                     on_wake=lambda: self.fired.append(1))

    def test_score_below_threshold_does_not_fire(self):
        self.assertFalse(self.listener.process_scores({"hey_jarvis": 0.3}))
        self.assertEqual(self.fired, [])

    def test_score_at_threshold_fires(self):
        self.assertTrue(self.listener.process_scores({"hey_jarvis": 0.9}))
        self.assertEqual(self.fired, [1])

    def test_refractory_window_blocks_repeat_fires(self):
        # Scores stay elevated for several consecutive frames of one
        # utterance; one spoken phrase must fire exactly one wake.
        self.listener.process_scores({"hey_jarvis": 0.9})
        for _ in range(5):
            self.assertFalse(
                self.listener.process_scores({"hey_jarvis": 0.9}))
        self.assertEqual(self.fired, [1])

    def test_fires_again_after_the_refractory_window(self):
        self.listener.process_scores({"hey_jarvis": 0.9})
        self.listener._last_fire = time.monotonic() - 10
        self.assertTrue(self.listener.process_scores({"hey_jarvis": 0.9}))
        self.assertEqual(self.fired, [1, 1])

    def test_empty_scores_are_safe(self):
        self.assertFalse(self.listener.process_scores({}))
        self.assertFalse(self.listener.process_scores(None))

    def test_wake_action_exception_does_not_propagate(self):
        boom = WakeListener(self.app,
                            on_wake=mock.Mock(side_effect=RuntimeError))
        self.assertTrue(boom.process_scores({"x": 1.0}))

    def test_invalid_threshold_config_falls_back_to_half(self):
        self.app.cfg["wake_threshold"] = "high"
        self.assertTrue(self.listener.process_scores({"x": 0.6}))
        self.assertEqual(self.fired, [1])


class PauseGatingTests(unittest.TestCase):
    def setUp(self):
        self.app = _FakeApp()
        self.listener = WakeListener(self.app)

    def test_idle_app_is_not_paused(self):
        self.assertFalse(self.listener._paused())

    def test_whisper_mode_pauses(self):
        # Owner default until decided: no listening in whisper mode.
        self.app.cfg["incognito"] = True
        self.assertTrue(self.listener._paused())

    def test_recording_pauses(self):
        self.app.recorder.is_recording = True
        self.assertTrue(self.listener._paused())

    def test_active_mode_pauses(self):
        self.app.state.emit("dictation_started", mode="dictation")
        self.assertTrue(self.listener._paused())

    def test_done_and_error_modes_do_not_pause(self):
        self.app.state.emit("x", mode="done")
        self.assertFalse(self.listener._paused())
        self.app.state.emit("x", mode="error")
        self.assertFalse(self.listener._paused())


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.app = _FakeApp()
        self.listener = WakeListener(self.app)

    def test_disabled_start_spawns_no_thread(self):
        self.app.cfg["wake_listener"] = False
        self.listener.start()
        self.assertIsNone(self.listener._thread)

    def test_enabled_start_spawns_one_thread_and_stop_ends_it(self):
        with mock.patch.object(WakeListener, "_run",
                               lambda s, ev: ev.wait(5)):
            self.listener.start()
            first = self.listener._thread
            self.assertIsNotNone(first)
            self.listener.start()
            self.assertIs(self.listener._thread, first,
                          "second start must not spawn a duplicate")
            self.listener.stop()
            first.join(timeout=2)
            self.assertFalse(first.is_alive())

    def test_rapid_off_on_toggle_starts_a_fresh_generation(self):
        # Review catch: the stop event is bound per thread, so a quick
        # off -> on toggle must start a new generation instead of
        # short-circuiting on the old, stopping thread and leaving the
        # listener enabled-but-inert.
        with mock.patch.object(WakeListener, "_run",
                               lambda s, ev: ev.wait(5)):
            self.listener.start()
            first = self.listener._thread
            self.listener.stop()   # old generation begins exiting
            self.listener.start()  # immediate re-enable
            self.assertIsNot(self.listener._thread, first,
                             "a stopping thread must not block a fresh start")
            self.assertTrue(self.listener._thread.is_alive())
            self.listener.stop()
            self.listener._thread.join(timeout=2)
            first.join(timeout=2)

    def test_restart_if_toggled_reconciles_both_ways(self):
        with mock.patch.object(WakeListener, "_run",
                               lambda s, ev: ev.wait(5)):
            self.app.cfg["wake_listener"] = False
            self.listener.restart_if_toggled()
            self.assertIsNone(self.listener._thread)
            self.app.cfg["wake_listener"] = True
            self.listener.restart_if_toggled()
            self.assertTrue(self.listener._thread.is_alive())
            self.app.cfg["wake_listener"] = False
            self.listener.restart_if_toggled()
            self.listener._thread.join(timeout=2)
            self.assertFalse(self.listener._thread.is_alive())

class FrameHandlingTests(unittest.TestCase):
    """handle_frame drives the ring buffer and the pause latch."""

    def setUp(self):
        self.app = _FakeApp()
        self.listener = WakeListener(self.app, on_wake=lambda: None)
        self.model = _FakeModel()

    def test_frames_feed_ring_and_model(self):
        self.listener.handle_frame("f1", self.model)
        self.listener.handle_frame("f2", self.model)
        self.assertEqual(list(self.listener._ring), ["f1", "f2"])
        self.assertEqual(self.model.predicted, ["f1", "f2"])

    def test_ring_is_bounded(self):
        for i in range(RING_FRAMES + 5):
            self.listener.handle_frame(i, self.model)
        self.assertEqual(len(self.listener._ring), RING_FRAMES)
        self.assertEqual(self.listener._ring[0], 5,
                         "oldest frames must roll off")

    def test_pause_resets_model_once_and_clears_ring(self):
        self.listener.handle_frame("pre", self.model)
        self.app.cfg["incognito"] = True
        self.listener.handle_frame("p1", self.model)
        self.listener.handle_frame("p2", self.model)
        self.assertEqual(self.model.resets, 1,
                         "reset fires once on pause ENTRY, not per frame")
        self.assertEqual(len(self.listener._ring), 0,
                         "pre-pause audio must not survive the pause")
        self.assertEqual(self.model.predicted, ["pre"],
                         "no inference while paused")

    def test_resume_after_pause_feeds_again(self):
        self.app.cfg["incognito"] = True
        self.listener.handle_frame("p1", self.model)
        self.app.cfg["incognito"] = False
        self.listener.handle_frame("f1", self.model)
        self.assertEqual(list(self.listener._ring), ["f1"])
        self.assertEqual(self.model.predicted, ["f1"])


@unittest.skipIf(np is None, "numpy not installed (CI system python)")
class AssemblePrefixTests(unittest.TestCase):
    def setUp(self):
        self.listener = WakeListener(_FakeApp(), on_wake=lambda: None)

    def test_int16_frames_become_float32_column(self):
        frame = np.full(1280, 16384, dtype=np.int16)
        self.listener._ring.append(frame)
        self.listener._ring.append(frame)
        prefix = self.listener._assemble_prefix()
        self.assertEqual(prefix.shape, (2560, 1))
        self.assertEqual(prefix.dtype, np.float32)
        self.assertAlmostEqual(float(prefix[0][0]), 0.5, places=3)
        self.assertEqual(len(self.listener._ring), 0,
                         "assembly consumes the ring")

    def test_empty_ring_returns_none(self):
        self.assertIsNone(self.listener._assemble_prefix())


class WakeActionSpliceTests(unittest.TestCase):
    """The default wake action is the tier-2 splice now."""

    def setUp(self):
        self.app = _FakeApp()
        self.listener = WakeListener(self.app)

    def test_wake_hands_prefix_to_dictation(self):
        with mock.patch.object(self.listener, "_assemble_prefix",
                               return_value="PREFIX"):
            self.listener._default_wake_action()
        self.app.dictation.begin_via_wake.assert_called_once_with("PREFIX")
        self.assertEqual(self.app.flashes, 0)

    def test_busy_dictation_flashes(self):
        self.app.dictation.begin_via_wake.return_value = False
        with mock.patch.object(self.listener, "_assemble_prefix",
                               return_value=None):
            self.listener._default_wake_action()
        self.assertEqual(self.app.flashes, 1)

    def test_foreign_sample_rate_skips_prefix_and_clears_ring(self):
        # The ring is 16 kHz; splicing it into a recorder configured for
        # another rate would time-stretch the prefix audio.
        self.app.cfg["sample_rate"] = 48000
        self.listener._ring.append("stale")
        self.listener._default_wake_action()
        self.app.dictation.begin_via_wake.assert_called_once_with(None)
        self.assertEqual(len(self.listener._ring), 0)


class WakeSessionTests(unittest.TestCase):
    """During a wake-initiated dictation the listener keeps running and
    watches for the outro phrase and sustained silence."""

    def setUp(self):
        self.app = _FakeApp()
        self.app.dictation.toggle = mock.Mock()
        self.listener = WakeListener(self.app)
        self.app.state.emit("x", mode="dictation")
        self.listener._wake_session_active = True
        # Well past the grace window, voice heard just now.
        self.listener._session_started = time.monotonic() - 10
        self.listener._last_voice = time.monotonic()

    def test_wake_action_arms_the_session(self):
        listener = WakeListener(self.app)
        with mock.patch.object(listener, "_assemble_prefix",
                               return_value=None):
            listener._default_wake_action()
        self.assertTrue(listener._wake_session_active)

    def test_refused_wake_does_not_arm_the_session(self):
        self.app.dictation.begin_via_wake.return_value = False
        listener = WakeListener(self.app)
        with mock.patch.object(listener, "_assemble_prefix",
                               return_value=None):
            listener._default_wake_action()
        self.assertFalse(listener._wake_session_active)

    def test_outro_phrase_stops_the_dictation(self):
        self.app.cfg["wake_outro_model"] = "thats_all"
        model = _FakeModel(scores={"hey_jarvis": 0.0, "thats_all": 0.9})
        self.listener.handle_frame("f", model)
        self.app.dictation.toggle.assert_called_once()
        self.assertFalse(self.listener._wake_session_active)
        self.assertEqual(model.resets, 1)

    def test_outro_within_grace_window_is_ignored(self):
        # An outro model acoustically close to the wake phrase must not
        # end the dictation on the same utterance that started it.
        self.app.cfg["wake_outro_model"] = "thats_all"
        self.listener._session_started = time.monotonic()
        model = _FakeModel(scores={"thats_all": 0.9})
        self.listener.handle_frame("f", model)
        self.app.dictation.toggle.assert_not_called()
        self.assertTrue(self.listener._wake_session_active)

    def test_without_outro_config_high_foreign_score_is_ignored(self):
        model = _FakeModel(scores={"thats_all": 0.9})
        self.listener.handle_frame("f", model)
        self.app.dictation.toggle.assert_not_called()

    def test_silence_stops_after_the_configured_limit(self):
        model = _FakeModel(vad_score=0.0)
        self.listener._last_voice = time.monotonic() - 9  # default 8s
        self.listener.handle_frame("f", model)
        self.app.dictation.toggle.assert_called_once()
        self.assertFalse(self.listener._wake_session_active)

    def test_voice_refreshes_the_silence_clock(self):
        model = _FakeModel(vad_score=0.9)
        self.listener._last_voice = time.monotonic() - 9
        self.listener.handle_frame("f", model)
        self.app.dictation.toggle.assert_not_called()
        self.assertTrue(self.listener._wake_session_active)

    def test_silence_stop_disabled_by_zero(self):
        self.app.cfg["wake_silence_stop_s"] = 0
        model = _FakeModel(vad_score=0.0)
        self.listener._last_voice = time.monotonic() - 100
        self.listener.handle_frame("f", model)
        self.app.dictation.toggle.assert_not_called()

    def test_invalid_silence_config_falls_back_to_default_not_disabled(self):
        # Review catch: `or 0` treated None/"" as an explicit disable.
        # Only 0 disables; junk falls back to the 8s default.
        for bad in (None, "", "soon"):
            self.app.cfg["wake_silence_stop_s"] = bad
            self.listener._wake_session_active = True
            self.app.dictation.toggle.reset_mock()
            model = _FakeModel(vad_score=0.0)
            self.listener._last_voice = time.monotonic() - 9
            self.listener.handle_frame("f", model)
            self.app.dictation.toggle.assert_called_once()

    def test_missing_vad_counts_as_voice_and_never_silence_stops(self):
        model = _FakeModel(vad_score=0.0)
        del model.vad
        self.listener._last_voice = time.monotonic() - 100
        self.listener.handle_frame("f", model)
        self.app.dictation.toggle.assert_not_called()

    def test_session_ends_when_the_dictation_ended_elsewhere(self):
        # Hotkey stop, discard, or the max-minutes cap: back to normal
        # listening without touching the (already ended) dictation.
        self.app.state.emit("x", mode=None)
        model = _FakeModel()
        self.listener.handle_frame("f", model)
        self.assertFalse(self.listener._wake_session_active)
        self.assertEqual(model.resets, 1)
        self.assertEqual(model.predicted, [],
                         "no inference on the cleanup frame")
        self.app.dictation.toggle.assert_not_called()

    def test_incognito_ends_the_session_without_stopping_dictation(self):
        self.app.cfg["incognito"] = True
        self.listener.handle_frame("f", _FakeModel())
        self.assertFalse(self.listener._wake_session_active)
        self.app.dictation.toggle.assert_not_called()

    def test_outro_key_never_fires_a_wake_when_idle(self):
        # Saying the outro phrase while nothing is recording must not
        # START a dictation.
        self.app.cfg["wake_outro_model"] = "thats_all"
        listener = WakeListener(self.app, on_wake=mock.Mock())
        self.assertFalse(listener.process_scores({"thats_all": 0.9}))
        self.assertTrue(listener.process_scores({"hey_jarvis": 0.9}))


class StripTrailingPhraseTests(unittest.TestCase):
    def test_strips_outro_and_trailing_punctuation(self):
        from whisper_sync.listener import strip_trailing_phrase
        self.assertEqual(
            strip_trailing_phrase("Take a note. That's all.", "thats_all"),
            "Take a note.")
        self.assertEqual(
            strip_trailing_phrase("Take a note, thats all", "thats_all"),
            "Take a note,")

    def test_outro_mid_sentence_is_preserved(self):
        from whisper_sync.listener import strip_trailing_phrase
        text = "That's all I know about the demo, plus one more thing"
        self.assertEqual(strip_trailing_phrase(text, "thats_all"), text)

    def test_only_the_last_occurrence_is_stripped(self):
        from whisper_sync.listener import strip_trailing_phrase
        self.assertEqual(
            strip_trailing_phrase("That's all that matters. That's all.",
                                  "thats_all"),
            "That's all that matters.")

    def test_no_match_returns_text_unchanged(self):
        from whisper_sync.listener import strip_trailing_phrase
        self.assertEqual(
            strip_trailing_phrase("Take a note about the demo.",
                                  "thats_all"),
            "Take a note about the demo.")

    def test_only_the_phrase_yields_empty_string(self):
        from whisper_sync.listener import strip_trailing_phrase
        self.assertEqual(strip_trailing_phrase("That's all.", "thats_all"),
                         "")


class StripLeadingPhraseTests(unittest.TestCase):
    def test_strips_phrase_and_punctuation(self):
        self.assertEqual(
            strip_leading_phrase("Hey, Jarvis. Take a note.", "hey_jarvis"),
            "Take a note.")
        self.assertEqual(
            strip_leading_phrase("Hey Jarvis take a note", "hey_jarvis"),
            "take a note")

    def test_strips_room_audio_lead_in_before_the_phrase(self):
        # The 2.5s ring can start mid-sentence of ambient speech; the
        # summons and everything before it belong to the wake, not the
        # dictation.
        self.assertEqual(
            strip_leading_phrase("so anyway hey jarvis note this",
                                 "hey_jarvis"),
            "note this")

    def test_no_phrase_returns_text_unchanged(self):
        self.assertEqual(
            strip_leading_phrase("Take a note about the demo.",
                                 "hey_jarvis"),
            "Take a note about the demo.")

    def test_phrase_beyond_search_window_is_preserved(self):
        text = ("The quick brown fox jumps over the lazy sleeping dog "
                "and then says hey jarvis at the end")
        self.assertEqual(strip_leading_phrase(text, "hey_jarvis"), text)

    def test_only_the_phrase_yields_empty_string(self):
        self.assertEqual(strip_leading_phrase("Hey Jarvis.", "hey_jarvis"),
                         "")

    def test_versioned_model_name_tokens(self):
        self.assertEqual(
            strip_leading_phrase("Hey Jarvis, okay.", "hey_jarvis_v0.1"),
            "okay.")

    def test_single_token_phrase_respects_word_boundaries(self):
        self.assertEqual(
            strip_leading_phrase("Alexander wrote this down.", "alexa"),
            "Alexander wrote this down.")

    def test_empty_text_is_safe(self):
        self.assertEqual(strip_leading_phrase("", "hey_jarvis"), "")


if __name__ == "__main__":
    unittest.main()
