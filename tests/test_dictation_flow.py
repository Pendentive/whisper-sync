"""Tests for whisper_sync.dictation_flow.DictationFlow.

The extraction target (hardening item 6): dictation behavior must be
testable without the tray, hotkeys, or a real worker. Everything the
flow reaches through the app back-reference is faked; async work runs
inline via a patched submit_or_spawn.

Also pins the AppState.feature_suggest fold: feature intent enters
state atomically with DICTATION_STARTED and always clears on the way
out, including failure and no-audio paths.
"""

import sys
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from whisper_sync import dictation_flow
from whisper_sync.dictation_flow import DictationFlow, safe_unlink
from whisper_sync.state_manager import StateManager, MEETING_STARTED


class _FakeRecorder:
    def __init__(self):
        self.is_recording = False
        self.streaming_path = None
        self.audio = {"mic": "AUDIO"}
        self.stop_streaming_calls = 0
        self.fail_start = False

    def start(self, mic_device=None):
        if self.fail_start:
            raise RuntimeError("no mic")
        self.is_recording = True

    def start_streaming(self, path):
        self.streaming_path = path

    def stop(self):
        self.is_recording = False
        return self.audio

    def stop_streaming(self):
        self.stop_streaming_calls += 1


class _FakeWorker:
    def __init__(self):
        self.ready = True
        self.calls = []
        self.wait_calls = 0
        self.loads_on_wait = True  # model finishes loading when waited on

    def is_ready(self):
        return self.ready

    def wait_ready(self, timeout=None):
        self.wait_calls += 1
        if self.loads_on_wait:
            self.ready = True
        return self.ready

    def transcribe_fast(self, audio, model_override=None, timeout=None):
        self.calls.append(model_override)
        return "hello world"


class _FakeBackup:
    is_loading = False

    def __init__(self):
        self.calls = 0

    def transcribe(self, audio):
        self.calls += 1
        return "overlay text"


class _FakeGuard:
    def effective_model(self, model):
        return model

    def note_pressure_trigger(self, reason):
        pass


class _FakeStats:
    def __init__(self):
        self.dictation_chars = []
        self.features = 0

    def record_dictation(self, chars, duration):
        self.dictation_chars.append(chars)

    def record_feature_suggestion(self):
        self.features += 1


class _FakeControl:
    """Records guard-aware respawns (app_control.restart_worker)."""

    def __init__(self):
        self.respawns = []

    def restart_worker(self, reason):
        self.respawns.append(reason)
        return True


class _FakeApp:
    def __init__(self):
        self.cfg = {
            "sample_rate": 16000,
            "model": "large-v3",
            "paste_method": "clipboard",
            "incognito": False,
            "always_available_dictation": True,
            "use_system_devices": False,
            "mic_device": None,
            "dictation_max_minutes": 30,
        }
        self._lock = threading.RLock()
        self.state = StateManager(None, {})
        self.recorder = _FakeRecorder()
        self.worker = _FakeWorker()
        self._backup = _FakeBackup()
        self._gpu_guard = _FakeGuard()
        self._stats = _FakeStats()
        self.control = _FakeControl()
        self.flashes = 0
        self.refreshes = 0

    def _yellow_flash(self):
        self.flashes += 1

    def _flash_queued(self):
        pass

    def _schedule_idle(self, seconds, blink=False):
        pass

    def _refresh_menu(self):
        self.refreshes += 1

    def _can_record(self):
        mode = self.state.current.mode
        return mode is None or mode in ("transcribing", "done", "error")


def _inline(lane, label, fn, native=False):
    fn()


class _FlowHarness(unittest.TestCase):
    """Fakes everything the flow reaches: app services via _FakeApp, and
    the numpy/pyperclip-dependent modules (capture, backup_worker, paste)
    via sys.modules stubs - the flow imports those lazily so the system
    suite runs without the heavyweight venv."""

    def setUp(self):
        self.app = _FakeApp()
        # load_recent reads the (isolated) dictation log dir; start empty.
        with mock.patch.object(dictation_flow.dictation_log, "load_recent",
                               return_value=[]):
            self.flow = DictationFlow(self.app)

        self.paste = mock.Mock()
        fake_paste = types.ModuleType("whisper_sync.paste")
        fake_paste.paste = self.paste

        fake_backup_mod = types.ModuleType("whisper_sync.backup_worker")

        class _FakeBackupCls:
            @staticmethod
            def is_enabled(cfg=None):
                return (cfg or {}).get("always_available_dictation", True)
        fake_backup_mod.BackupTranscriber = _FakeBackupCls

        self.fake_capture = types.ModuleType("whisper_sync.capture")
        self.fake_capture.get_default_devices = lambda: {"input": None}
        self.fake_capture.AudioRecorder = _FakeRecorder

        patches = [
            mock.patch.dict(sys.modules, {
                "whisper_sync.paste": fake_paste,
                "whisper_sync.backup_worker": fake_backup_mod,
                "whisper_sync.capture": self.fake_capture,
            }),
            mock.patch.object(dictation_flow, "submit_or_spawn", _inline),
            mock.patch.object(dictation_flow, "notify"),
            mock.patch.object(dictation_flow.dictation_log, "append"),
            mock.patch.object(dictation_flow.weekly_stats, "record_dictation"),
            mock.patch.object(dictation_flow.weekly_stats, "record_feature_suggestion"),
            mock.patch.object(DictationFlow, "_format_feature_async"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


class NormalDictationTests(_FlowHarness):
    def test_toggle_starts_then_stops_and_pastes(self):
        self.flow.toggle()
        self.assertEqual(self.app.state.current.mode, "dictation")
        self.assertTrue(self.app.recorder.is_recording)
        self.assertIsNotNone(self.flow._wav_path, "disk-first streaming path armed")

        self.flow.toggle()
        self.paste.assert_called_once()
        self.assertEqual(self.paste.call_args[0][0], "hello world")
        self.assertEqual(self.app.state.current.mode, "done")
        self.assertEqual(self.app._stats.dictation_chars, [len("hello world")])
        history = self.flow.recent_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["text"], "hello world")

    def test_cap_armed_on_start_and_cancelled_on_stop(self):
        self.flow.toggle()
        self.assertIsNotNone(self.flow._cap_handle)
        self.flow.toggle()
        self.assertIsNone(self.flow._cap_handle)

    def test_worker_not_ready_still_records_disk_first(self):
        # App startup / model loading: dictation always works now; only
        # the transcription waits (yellow state lasts longer).
        self.app.worker.ready = False
        self.flow.toggle()
        self.assertEqual(self.app.flashes, 0)
        self.assertEqual(self.app.state.current.mode, "dictation")
        self.assertTrue(self.app.recorder.is_recording)

    def test_worker_crash_routes_through_failover_respawn(self):
        # Respawn must go through the guard-aware path, never a bare
        # worker.restart() (which would retry CUDA on a lost dGPU).
        from whisper_sync.worker_manager import WorkerCrashedError

        def _boom(audio, model_override=None, timeout=None):
            raise WorkerCrashedError("boom")

        self.flow.toggle()
        self.app.worker.transcribe_fast = _boom
        self.flow.toggle()
        self.assertEqual(self.app.control.respawns, ["worker_crash_dictation"])
        self.assertEqual(self.app.state.current.mode, "error")

    def test_mic_failure_returns_to_idle_and_clears_feature(self):
        self.app.recorder.fail_start = True
        self.flow.toggle_feature_suggest()
        self.assertIsNone(self.app.state.current.mode)
        self.assertFalse(self.app.state.current.feature_suggest)

    def test_no_audio_clears_feature_flag(self):
        self.flow.toggle_feature_suggest()
        self.assertTrue(self.app.state.current.feature_suggest)
        self.app.recorder.audio = {}  # mic delivered nothing
        self.flow.toggle_feature_suggest()
        self.assertIsNone(self.app.state.current.mode)
        self.assertFalse(self.app.state.current.feature_suggest)

    def test_concurrent_mode_change_rejects_start(self):
        # try_transition must reject when mode moved to a non-startable
        # value between the toggle's check and the start.
        self.app.state.emit(MEETING_STARTED, mode="saving")
        self.flow._start(feature=False)
        self.assertFalse(self.app.recorder.is_recording)

    def test_toggle_wakes_sleeping_model_and_records_immediately(self):
        # Dictation is disk-first, so sleep must not block recording:
        # wake the model AND start capturing in the same gesture.
        from whisper_sync.state_manager import SLEEP_STARTED
        self.app.auto_sleep = mock.Mock()
        self.app.worker.ready = False  # sleeping worker is not ready
        self.app.state.emit(SLEEP_STARTED, sleeping=True)
        self.flow.toggle()
        self.app.auto_sleep.wake.assert_called_once()
        self.assertTrue(self.app.recorder.is_recording,
                        "recording must start while the model loads")

    def test_sleeping_toggle_in_whisper_mode_wakes_but_does_not_record(self):
        # RAM-only capture has no crash net; the old refuse stands.
        from whisper_sync.state_manager import SLEEP_STARTED
        self.app.cfg["incognito"] = True
        self.app.auto_sleep = mock.Mock()
        self.app.worker.ready = False
        self.app.state.emit(SLEEP_STARTED, sleeping=True)
        self.flow.toggle()
        self.app.auto_sleep.wake.assert_called_once()
        self.assertFalse(self.app.recorder.is_recording)
        self.assertEqual(self.app.flashes, 1)

    def test_stop_waits_for_model_then_pastes(self):
        # Start while loading, stop before ready: the process step must
        # block on wait_ready and then transcribe normally.
        self.app.worker.ready = False
        self.flow.toggle()
        self.assertTrue(self.app.recorder.is_recording)
        self.flow.toggle()
        self.assertEqual(self.app.worker.wait_calls, 1)
        self.paste.assert_called_once()
        self.assertEqual(self.app.state.current.mode, "done")

    def test_stop_preserves_audio_when_model_never_loads(self):
        self.app.worker.ready = False
        self.app.worker.loads_on_wait = False
        self.flow.toggle()
        wav = self.flow._wav_path
        self.flow.toggle()
        self.paste.assert_not_called()
        self.assertEqual(self.app.state.current.mode, "error")
        self.assertEqual(self.flow._wav_path, wav,
                         "crash-safety WAV must not be deleted on failure")

    def test_discard_throws_audio_away(self):
        self.flow.toggle()
        self.flow.discard()
        self.assertIsNone(self.app.state.current.mode)
        self.assertFalse(self.app.recorder.is_recording)
        self.paste.assert_not_called()

    def test_incognito_skips_disk_streaming(self):
        self.app.cfg["incognito"] = True
        self.flow.toggle()
        self.assertIsNone(self.flow._wav_path)
        self.assertIsNone(self.app.recorder.streaming_path)


class FeatureSuggestTests(_FlowHarness):
    def test_feature_routes_to_feature_log_not_paste(self):
        with mock.patch.object(dictation_flow.feature_log, "append_raw",
                               return_value="id-1") as append_raw:
            self.flow.toggle_feature_suggest()
            self.assertTrue(self.app.state.current.feature_suggest)
            self.assertEqual(self.app.state.current.mode, "dictation")
            self.assertTrue(str(self.flow._wav_path.name).startswith("feature_"))

            self.flow.toggle_feature_suggest()
            append_raw.assert_called_once()
        self.paste.assert_not_called()
        self.assertFalse(self.app.state.current.feature_suggest)
        self.assertEqual(self.app._stats.features, 1)

    def test_normal_toggle_also_stops_a_feature_recording(self):
        # The dictation hotkey stops whatever dictation is active; the
        # feature routing must follow the state, not the hotkey used.
        with mock.patch.object(dictation_flow.feature_log, "append_raw",
                               return_value="id-1") as append_raw:
            self.flow.toggle_feature_suggest()
            self.flow.toggle()  # normal hotkey stops it
            append_raw.assert_called_once()
        self.paste.assert_not_called()

    def test_feature_hotkey_ignored_during_normal_dictation(self):
        self.flow.toggle()
        self.flow.toggle_feature_suggest()
        # Still recording the normal dictation; no state change
        self.assertEqual(self.app.state.current.mode, "dictation")
        self.assertFalse(self.app.state.current.feature_suggest)


class OverlayDictationTests(_FlowHarness):
    def setUp(self):
        super().setUp()
        self.app.state.emit(MEETING_STARTED, mode="meeting")
        self.overlay_recorder = _FakeRecorder()
        self.fake_capture.AudioRecorder = (
            lambda sample_rate=None: self.overlay_recorder)

    def test_toggle_starts_overlay_and_stop_uses_backup(self):
        self.flow.toggle()
        self.assertTrue(self.app.state.current.dictation_overlay)
        self.assertTrue(self.flow.overlay_active)
        self.assertTrue(self.overlay_recorder.is_recording)
        self.assertEqual(self.app.state.current.mode, "meeting",
                         "meeting recording must be untouched")

        self.flow.toggle()
        self.assertFalse(self.app.state.current.dictation_overlay)
        self.assertEqual(self.app._backup.calls, 1)
        self.paste.assert_called_once_with(
            "overlay text", "clipboard", restore=True)
        self.assertEqual(self.flow.recent_history()[0]["text"], "overlay text")

    def test_overlay_feature_suggest_routes_to_feature_log(self):
        with mock.patch.object(dictation_flow.feature_log, "append_raw",
                               return_value="id-2") as append_raw:
            self.flow.toggle_feature_suggest()
            self.assertTrue(self.app.state.current.feature_suggest)
            self.assertTrue(self.flow._overlay_wav_path.name.startswith("overlay_feature_"))
            self.flow.toggle_feature_suggest()
            append_raw.assert_called_once()
        self.assertFalse(self.app.state.current.feature_suggest)
        self.paste.assert_not_called()

    def test_backup_loading_flashes_instead_of_starting(self):
        self.app._backup.is_loading = True
        self.flow.toggle()
        self.assertEqual(self.app.flashes, 1)
        self.assertFalse(self.flow.overlay_active)

    def test_backup_failure_falls_back_to_main_worker(self):
        self.app._backup.transcribe = mock.Mock(side_effect=RuntimeError("boom"))
        self.flow.toggle()
        self.flow.toggle()
        self.assertEqual(self.app.worker.calls, ["large-v3"],
                         "fallback must queue on the main worker")
        self.paste.assert_called_once()

    def test_feature_hotkey_ignored_during_normal_overlay(self):
        # Review catch: starting a feature overlay while a NORMAL overlay
        # dictation records would overwrite _overlay_recorder and
        # double-open the mic. The hotkey must be a no-op instead.
        self.flow.toggle()  # normal overlay recording
        first_recorder = self.flow._overlay_recorder
        self.flow.toggle_feature_suggest()
        self.assertIs(self.flow._overlay_recorder, first_recorder,
                      "overlay recorder must not be replaced")
        self.assertTrue(first_recorder.is_recording)
        self.assertFalse(self.app.state.current.feature_suggest)

    def test_discard_overlay(self):
        self.flow.toggle()
        self.flow.discard()
        self.assertFalse(self.app.state.current.dictation_overlay)
        self.assertFalse(self.flow.overlay_active)
        self.assertFalse(self.app.state.current.feature_suggest)
        self.paste.assert_not_called()


class HistoryTests(_FlowHarness):
    def test_history_trims_to_limit(self):
        for i in range(15):
            self.flow._record_history(f"text {i}")
        history = self.flow.recent_history()
        self.assertEqual(len(history), dictation_flow.HISTORY_LIMIT)
        self.assertEqual(history[-1]["text"], "text 14")
        self.assertEqual(history[0]["text"], "text 5")

    def test_clear_history_refreshes_menu(self):
        self.flow._record_history("x")
        before = self.app.refreshes
        self.flow.clear_history()
        self.assertEqual(self.flow.recent_history(), [])
        self.assertGreater(self.app.refreshes, before)


class SafeUnlinkTests(unittest.TestCase):
    def test_missing_file_is_a_noop(self):
        safe_unlink(Path("Z:/does/not/exist.wav"))

    def test_none_is_a_noop(self):
        safe_unlink(None)


if __name__ == "__main__":
    unittest.main()
