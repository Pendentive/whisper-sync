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

from whisper_sync import listener as listener_mod
from whisper_sync.listener import WakeListener
from whisper_sync.state_manager import StateManager, SLEEP_STARTED


class _FakeApp:
    def __init__(self):
        self.cfg = {"wake_listener": True, "wake_threshold": 0.5}
        self.state = StateManager(None, {})
        self.recorder = types.SimpleNamespace(is_recording=False)
        self.auto_sleep = types.SimpleNamespace(wake=mock.Mock())
        self.flashes = 0

    def _yellow_flash(self):
        self.flashes += 1


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
                               lambda s: s._stop_event.wait(5)):
            self.listener.start()
            first = self.listener._thread
            self.assertIsNotNone(first)
            self.listener.start()
            self.assertIs(self.listener._thread, first,
                          "second start must not spawn a duplicate")
            self.listener.stop()
            first.join(timeout=2)
            self.assertFalse(first.is_alive())

    def test_restart_if_toggled_reconciles_both_ways(self):
        with mock.patch.object(WakeListener, "_run",
                               lambda s: s._stop_event.wait(5)):
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

    def test_default_wake_action_wakes_a_sleeping_model(self):
        self.app.state.emit(SLEEP_STARTED, sleeping=True)
        with mock.patch.object(listener_mod, "notify"):
            self.listener._default_wake_action()
        self.app.auto_sleep.wake.assert_called_once()
        self.assertEqual(self.app.flashes, 0,
                         "wake() owns the flash when asleep")

    def test_default_wake_action_flashes_when_awake(self):
        with mock.patch.object(listener_mod, "notify"):
            self.listener._default_wake_action()
        self.assertEqual(self.app.flashes, 1)
        self.app.auto_sleep.wake.assert_not_called()


if __name__ == "__main__":
    unittest.main()
