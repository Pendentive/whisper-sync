"""Tests for whisper_sync.auto_sleep - AutoSleep and ClickRouter.

Pins the VRAM lifecycle contract: the idle checker only sleeps a fully
idle app after the configured window, manual sleep refuses while work
is active, waking flashes and respawns the worker, and the double-click
router never fires both actions for one gesture.
"""

import time
import types
import unittest
from unittest import mock

from whisper_sync import auto_sleep as auto_sleep_mod
from whisper_sync.auto_sleep import AutoSleep, ClickRouter
from whisper_sync.state_manager import (
    StateManager, DICTATION_COMPLETED, MEETING_STARTED,
)


def _inline(lane, label, fn, native=False):
    fn()


class _FakeWorker:
    def __init__(self):
        self.running = True
        self.stops = 0
        self.starts = 0

    def stop(self):
        self.running = False
        self.stops += 1

    def start(self):
        self.running = True
        self.starts += 1

    def wait_ready(self, timeout=None):
        return self.running


class _FakeControl:
    """Guard-aware respawn stand-in: wake routes through this, never a
    bare worker.start() (the spawn must be pinnable to cpu)."""

    def __init__(self, app):
        self.app = app
        self.respawns = []

    def restart_worker(self, reason):
        self.respawns.append(reason)
        self.app.worker.start()
        return self.app.worker.wait_ready(120)


class _FakeApp:
    def __init__(self):
        self.cfg = {"auto_sleep_minutes": 30}
        self.state = StateManager(None, {})
        self.worker = _FakeWorker()
        self.recorder = types.SimpleNamespace(is_recording=False)
        self._gpu_guard = types.SimpleNamespace(
            log_external_event=mock.Mock())
        self.control = _FakeControl(self)
        self.flashes = 0
        self.refreshes = 0

    def _yellow_flash(self):
        self.flashes += 1

    def _refresh_menu(self):
        self.refreshes += 1


class _SleepHarness(unittest.TestCase):
    def setUp(self):
        self.app = _FakeApp()
        self.auto = AutoSleep(self.app)
        patches = [
            mock.patch.object(auto_sleep_mod, "submit_or_spawn", _inline),
            mock.patch.object(auto_sleep_mod, "notify"),
        ]
        for p in patches:
            self.notify = p.start()
            self.addCleanup(p.stop)


class IdleCheckerTests(_SleepHarness):
    def _age(self, minutes):
        self.auto._last_activity = time.monotonic() - minutes * 60

    def test_sleeps_after_idle_window(self):
        self._age(31)
        self.auto._check()
        self.assertTrue(self.app.state.current.sleeping)
        self.assertEqual(self.app.worker.stops, 1)

    def test_stays_awake_inside_window(self):
        self._age(29)
        self.auto._check()
        self.assertFalse(self.app.state.current.sleeping)
        self.assertEqual(self.app.worker.stops, 0)

    def test_disabled_never_sleeps(self):
        self.app.cfg["auto_sleep_minutes"] = 0
        self._age(500)
        self.auto._check()
        self.assertFalse(self.app.state.current.sleeping)

    def test_busy_app_resets_the_clock_instead_of_sleeping(self):
        self.app.state.emit(MEETING_STARTED, mode="meeting")
        self._age(90)
        self.auto._check()
        self.assertFalse(self.app.state.current.sleeping)
        self.assertLess(time.monotonic() - self.auto._last_activity, 5,
                        "busy check must re-arm the idle clock")

    def test_activity_events_bump_the_clock(self):
        self.auto.start(scheduler=types.SimpleNamespace(
            call_every=lambda *a, **k: None))
        self._age(90)
        self.app.state.emit(DICTATION_COMPLETED, mode="done")
        self.assertLess(time.monotonic() - self.auto._last_activity, 5)

    def test_background_transcription_blocks_sleep(self):
        self.app.state.emit(MEETING_STARTED, mode=None,
                            meeting_transcribing=True)
        self._age(90)
        self.auto._check()
        self.assertFalse(self.app.state.current.sleeping)


class ManualToggleTests(_SleepHarness):
    def test_toggle_sleeps_then_wakes_with_flash(self):
        self.auto.toggle()
        self.assertTrue(self.app.state.current.sleeping)
        self.assertEqual(self.app.worker.stops, 1)

        self.auto.toggle()
        self.assertFalse(self.app.state.current.sleeping)
        self.assertEqual(self.app.worker.starts, 1)
        self.assertEqual(self.app.flashes, 1, "wake shows the loading flash")

    def test_manual_sleep_refused_while_recording(self):
        self.app.recorder.is_recording = True
        self.auto.sleep(reason="double_click")
        self.assertFalse(self.app.state.current.sleeping)
        self.assertEqual(self.app.worker.stops, 0)
        self.notify.assert_called_once()

    def test_idle_sleep_refusal_is_silent(self):
        self.app.recorder.is_recording = True
        self.auto.sleep(reason="idle")
        self.notify.assert_not_called()

    def test_sleep_and_wake_land_on_the_correlation_timeline(self):
        self.auto.toggle()
        self.auto.toggle()
        events = [c[0][0] for c in
                  self.app._gpu_guard.log_external_event.call_args_list]
        self.assertEqual(events, ["model_sleep", "model_wake"])

    def test_double_sleep_is_a_noop(self):
        self.auto.sleep(reason="double_click")
        self.auto.sleep(reason="double_click")
        self.assertEqual(self.app.worker.stops, 1)

    def test_wake_resets_the_idle_clock(self):
        # Review catch: without this, a manual wake after a long idle
        # stretch is auto-slept again on the next minute tick.
        self.auto.toggle()  # sleep
        self.auto._last_activity = time.monotonic() - 90 * 60
        self.auto.toggle()  # wake
        self.assertLess(time.monotonic() - self.auto._last_activity, 5)
        self.auto._check()
        self.assertFalse(self.app.state.current.sleeping,
                         "freshly woken model must not instantly re-sleep")

    def test_wake_when_awake_is_a_noop(self):
        self.auto.wake(reason="hotkey")
        self.assertEqual(self.app.worker.starts, 0)
        self.assertEqual(self.app.flashes, 0)

    def test_wake_routes_through_the_failover_respawn(self):
        # The wake spawn must consult the guard: if the dGPU was
        # powered off while asleep (the gaming scenario), the respawn
        # is pinned to cpu instead of loading cuda on a dead device.
        self.auto.toggle()  # sleep
        self.auto.toggle()  # wake
        self.assertEqual(self.app.control.respawns, ["wake"])


class ClickRouterTests(unittest.TestCase):
    def setUp(self):
        self.fired = []

    def _router(self, window=0.15):
        return ClickRouter(single=lambda: self.fired.append("single"),
                           double=lambda: self.fired.append("double"),
                           window_s=window)

    def test_single_click_fires_after_window(self):
        router = self._router()
        router.click()
        self.assertEqual(self.fired, [], "single must be deferred")
        time.sleep(0.35)
        self.assertEqual(self.fired, ["single"])

    def test_double_click_fires_double_only(self):
        router = self._router()
        router.click()
        router.click()
        time.sleep(0.35)
        self.assertEqual(self.fired, ["double"],
                         "the deferred single must be cancelled")

    def test_stale_pending_handle_never_fakes_a_double(self):
        # Review catch: if the scheduler is shut down, call_later returns
        # a handle that never fires, so _pending never clears. The
        # deadline guard must treat a click after the window as a fresh
        # single, not a phantom double.
        router = self._router(window=0.05)
        dead_handle = types.SimpleNamespace(cancel=lambda: None)
        with mock.patch("whisper_sync.scheduler.scheduler.call_later",
                        return_value=dead_handle):
            router.click()          # pending stored, never fires
            time.sleep(0.2)         # window expires
            router.click()          # must NOT be a double
        self.assertEqual(self.fired, [],
                         "no action may fire through a dead scheduler")

    def test_two_slow_clicks_are_two_singles(self):
        router = self._router(window=0.05)
        router.click()
        time.sleep(0.25)
        router.click()
        time.sleep(0.25)
        self.assertEqual(self.fired, ["single", "single"])


class IconTests(unittest.TestCase):
    def test_sleep_icon_key_and_spec(self):
        from whisper_sync.icons import resolve_icon_key, ICON_REGISTRY
        self.assertEqual(resolve_icon_key(mode=None, sleeping=True), "sleep")
        self.assertEqual(resolve_icon_key(mode=None, sleeping=False), "idle")
        # Sleep must never mask real activity.
        self.assertNotEqual(
            resolve_icon_key(mode="meeting", sleeping=True), "sleep")
        spec = ICON_REGISTRY["sleep"]
        self.assertEqual(spec.outer, "#808080", "outer ring stays normal gray")
        self.assertNotEqual(spec.middle, ICON_REGISTRY["idle"].middle,
                            "middle must be the deeper sleep gray")

    def test_sleep_icon_builds(self):
        from whisper_sync.icons import build_icon, ICON_REGISTRY
        img = build_icon(ICON_REGISTRY["sleep"])
        self.assertEqual(img.size, (64, 64))


class ConfigTests(unittest.TestCase):
    def test_auto_sleep_minutes_in_defaults_and_valid_keys(self):
        import json
        from pathlib import Path
        from whisper_sync import config
        defaults = json.loads(
            (Path(config.__file__).parent / "config.defaults.json").read_text())
        self.assertEqual(defaults.get("auto_sleep_minutes"), 30)
        self.assertIn("auto_sleep_minutes", config._VALID_KEYS)


if __name__ == "__main__":
    unittest.main()
