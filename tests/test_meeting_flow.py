"""Tests for whisper_sync.meeting_flow.MeetingFlow (extraction 2b).

Exercises the flow logic without a tray, dialogs, worker, or numpy:
capture/streaming_wav are stubbed via sys.modules (the flow imports
them lazily), dialogs are faked on the app, and async save work runs
inline. The post-processing error contract (_run_meeting_job) and the
save/abort/no-audio stop paths are the load-bearing surfaces.
"""

import sys
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from whisper_sync import meeting_flow
from whisper_sync.meeting_flow import MeetingFlow
from whisper_sync.meeting_dialogs import ABORT
from whisper_sync.state_manager import StateManager
from whisper_sync.worker_manager import WorkerCrashedError


class _FakeRecorder:
    def __init__(self):
        self.is_recording = False
        self.speaker_loopback_active = True
        self.audio = {"mic": "MIC-AUDIO"}
        self.discarded = 0
        self.streaming_args = None

    def start(self, mic_device=None, speaker_device=None):
        self.is_recording = True

    def start_streaming(self, path, disk_only=False):
        self.streaming_args = (path, disk_only)

    def stop(self):
        self.is_recording = False
        return self.audio

    def stop_streaming(self):
        pass

    def discard_streaming(self):
        self.discarded += 1


class _FakeWorker:
    def __init__(self):
        self.restarts = 0

    def restart(self):
        self.restarts += 1


class _FakeControl:
    """Records guard-aware respawns (app_control.restart_worker)."""

    def __init__(self, app):
        self.app = app
        self.respawns = []

    def restart_worker(self, reason):
        self.respawns.append(reason)
        self.app.worker.restart()
        return True


class _FakeGuard:
    def __init__(self):
        self.triggers = []

    def note_pressure_trigger(self, reason):
        self.triggers.append(reason)


class _FakeBackup:
    def __init__(self):
        self.preloads = 0

    def preload(self):
        self.preloads += 1


class _FakeDialogs:
    def __init__(self):
        self.answer = ("standup", False, None)

    def ask_meeting_name(self):
        return self.answer


class _FakeApp:
    def __init__(self, out_dir: Path):
        self.cfg = {
            "sample_rate": 16000,
            "use_system_devices": False,
            "mic_device": None,
            "speaker_device": 1,
            "always_available_dictation": False,
        }
        self._lock = threading.RLock()
        self.state = StateManager(None, {})
        self.recorder = _FakeRecorder()
        self.worker = _FakeWorker()
        self._gpu_guard = _FakeGuard()
        self._backup = _FakeBackup()
        self.dialogs = _FakeDialogs()
        self.control = _FakeControl(self)
        self.out = out_dir
        self.popups = []
        self.idles = []

    def _output_dir(self):
        return self.out

    def _can_record(self):
        mode = self.state.current.mode
        return mode is None or mode in ("transcribing", "done", "error")

    def _schedule_idle(self, seconds, blink=False):
        self.idles.append(seconds)

    def _refresh_menu(self):
        pass

    def _show_error_popup(self, title, message):
        self.popups.append((title, message))


def _inline(lane, label, fn, native=False):
    fn()


class _FlowHarness(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.app = _FakeApp(Path(self._tmp.name))
        self.flow = MeetingFlow(self.app)

        self.saved = []
        fake_capture = types.ModuleType("whisper_sync.capture")
        fake_capture.get_default_devices = lambda: {"input": None, "output": 2}
        fake_capture.save_wav = lambda path, audio, rate: self.saved.append(("mono", path))
        fake_capture.save_stereo_wav = lambda path, mic, spk, rate: self.saved.append(("stereo", path))
        fake_sw = types.ModuleType("whisper_sync.streaming_wav")
        fake_sw.cleanup_temp_files = lambda d: None
        fake_sw.StreamingWavWriter = None  # only needed for disk-path audio shapes

        patches = [
            mock.patch.dict(sys.modules, {
                "whisper_sync.capture": fake_capture,
                "whisper_sync.streaming_wav": fake_sw,
            }),
            mock.patch.object(meeting_flow, "submit_or_spawn", _inline),
            mock.patch.object(meeting_flow, "notify"),
        ]
        for p in patches:
            self.mocked = p.start()
            self.addCleanup(p.stop)
        self.notify = self.mocked  # last patch is notify


class StartStopTests(_FlowHarness):
    def test_toggle_starts_meeting(self):
        self.flow.toggle()
        self.assertEqual(self.app.state.current.mode, "meeting")
        self.assertTrue(self.app.recorder.is_recording)
        path, disk_only = self.app.recorder.streaming_args
        self.assertTrue(disk_only, "meeting streams to disk only")
        self.assertTrue(str(path).endswith("mic-temp.wav"))

    def test_toggle_wakes_sleeping_model_and_starts_recording(self):
        from whisper_sync.state_manager import SLEEP_STARTED
        self.app.auto_sleep = mock.Mock()
        self.app.state.emit(SLEEP_STARTED, sleeping=True)
        self.flow.toggle()
        self.app.auto_sleep.wake.assert_called_once()
        self.assertTrue(self.app.recorder.is_recording,
                        "recording must start immediately; transcription "
                        "waits for the worker at its own step")

    def test_start_rejected_when_mode_not_startable(self):
        self.app.state.emit("meeting_started", mode="saving")
        self.flow._start()
        self.assertFalse(self.app.recorder.is_recording)

    def test_abort_recording_discards_without_dialog(self):
        # The auto-record opt-in toast's "Don't record" action: no save
        # dialog, temp streams discarded, straight back to idle.
        self.flow.toggle()
        self.assertEqual(self.app.state.current.mode, "meeting")
        self.flow.abort_recording(reason="test")
        self.assertIsNone(self.app.state.current.mode)
        self.assertFalse(self.app.recorder.is_recording)
        self.assertEqual(self.app.recorder.discarded, 1)
        self.assertEqual(len(self.saved), 0, "nothing may be written")

    def test_abort_when_not_recording_is_a_noop(self):
        self.flow.abort_recording(reason="test")
        self.assertEqual(self.app.recorder.discarded, 0)

    def test_stop_saves_and_enqueues_job(self):
        self.app.recorder.audio = {"mic": "MIC-AUDIO", "speaker": "SPK-AUDIO"}
        self.flow.toggle()
        self.flow.toggle()  # stop; _save_and_enqueue runs inline
        self.assertEqual(len(self.saved), 1)
        kind, path = self.saved[0]
        self.assertEqual(kind, "stereo", "speaker array present -> stereo save")
        self.assertIn("standup", str(path))
        self.assertEqual(self.flow._post_queue.qsize(), 1)
        job = self.flow._post_queue.get_nowait()
        self.assertEqual(job.name, "standup")
        self.assertIs(job.app, self.app,
                      "job must get the app, not the flow (review catch)")
        state = self.app.state.current
        self.assertIsNone(state.mode, "mode released for the next recording")
        self.assertTrue(state.meeting_transcribing)

    def test_stop_abort_discards(self):
        self.app.dialogs.answer = ABORT
        self.flow.toggle()
        self.flow.toggle()
        self.assertEqual(self.app.recorder.discarded, 1)
        self.assertIsNone(self.app.state.current.mode)
        self.assertEqual(self.flow._post_queue.qsize(), 0)

    def test_stop_without_audio_goes_idle(self):
        self.flow.toggle()
        self.app.recorder.audio = {}
        self.flow.toggle()
        self.assertIsNone(self.app.state.current.mode)
        self.assertEqual(len(self.saved), 0)

    def test_mono_save_when_no_speaker_channel(self):
        self.app.recorder.audio = {"mic": "MIC-AUDIO"}
        self.flow.toggle()
        self.flow.toggle()
        self.assertEqual(self.saved[0][0], "mono")


class RunJobErrorTests(_FlowHarness):
    class _Job:
        name = "j"
        wav_path = Path("x.wav")
        total_steps = 1
        current_step_name = "step"
        _current_step = 0

        def __init__(self, exc=None):
            self.exc = exc
            self.ran = 0

        @property
        def is_complete(self):
            return self.ran > 0

        def execute_next_step(self):
            self.ran += 1
            if self.exc:
                raise self.exc

    def test_success_path_runs_all_steps(self):
        job = self._Job()
        self.flow._run_meeting_job(job)
        self.assertEqual(job.ran, 1)
        self.assertEqual(self.app.popups, [])

    def test_worker_crash_feeds_guard_and_restarts(self):
        job = self._Job(WorkerCrashedError("boom"))
        self.flow._run_meeting_job(job)
        self.assertEqual(self.app._gpu_guard.triggers, ["worker_crash_meeting"])
        # Respawn must route through the guard-aware path, never a bare
        # worker.restart() (which would retry CUDA on a lost dGPU).
        self.assertEqual(self.app.control.respawns, ["worker_crash_meeting"])
        self.assertEqual(self.app.worker.restarts, 1)
        self.assertEqual(self.app.state.current.mode, "error")

    def test_ffmpeg_missing_gets_actionable_popup(self):
        job = self._Job(FileNotFoundError("[WinError 2] ffmpeg not found"))
        with mock.patch("shutil.which", return_value=None):
            self.flow._run_meeting_job(job)
        self.assertTrue(any("FFmpeg" in t for t, _ in self.app.popups))

    def test_emit_error_safe_preserves_mode_while_recording(self):
        self.app.state.emit("meeting_started", mode="meeting")
        self.app.recorder.is_recording = True
        self.flow._emit_error_safe("late failure")
        self.assertEqual(self.app.state.current.mode, "meeting",
                         "active recording must not be knocked into error mode")


class PipelineLifecycleTests(_FlowHarness):
    def test_pipeline_idle_tracks_queue(self):
        self.assertTrue(self.flow.pipeline_idle())
        self.flow._post_queue.put(object())
        self.assertFalse(self.flow.pipeline_idle())

    def test_shutdown_sends_sentinel(self):
        self.flow.shutdown_post_worker()
        self.assertIsNone(self.flow._post_queue.get_nowait())


class RenameTests(_FlowHarness):
    def test_do_rename_moves_folder(self):
        src = self.app.out / "07-w1" / "0703_0900_old-name"
        src.mkdir(parents=True)
        (src / "minutes.md").write_text("x")
        with mock.patch.object(meeting_flow, "rebuild_root_index"):
            self.flow._do_rename(src, "0703_0900", "new-name")
        dest = self.app.out / "07-w1" / "0703_0900_new-name"
        self.assertTrue((dest / "minutes.md").exists())
        self.assertFalse(src.exists())

    def test_do_rename_refuses_to_clobber(self):
        src = self.app.out / "07-w1" / "0703_0900_old"
        src.mkdir(parents=True)
        dest = self.app.out / "07-w1" / "0703_0900_new"
        dest.mkdir(parents=True)
        (dest / "keep.txt").write_text("existing")
        self.flow._do_rename(src, "0703_0900", "new")
        self.assertTrue(src.exists())
        self.assertTrue((dest / "keep.txt").exists())

    def test_rename_suggestion_skipped_when_name_matches(self):
        with mock.patch.object(MeetingFlow, "_generate_name_suggestions",
                               return_value=["same-name"]):
            out = self.flow.ask_rename_suggestion(
                "same-name", "summary", meeting_dir="d", date_time_str="0703_0900")
        self.assertIsNone(out)
        self.notify.assert_not_called()

    def test_rename_suggestion_offers_toast(self):
        with mock.patch.object(MeetingFlow, "_generate_name_suggestions",
                               return_value=["better-name"]):
            self.flow.ask_rename_suggestion(
                "old-name", "summary", meeting_dir="d", date_time_str="0703_0900")
        self.notify.assert_called_once()
        self.assertIn("better-name", self.notify.call_args[0][1])

    def test_name_suggestion_fallback_without_cli(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            out = self.flow._generate_name_suggestions(
                "Discussed the migration go live planning for Q3", "old")
        self.assertEqual(len(out), 1)
        self.assertNotIn(" ", out[0])
        self.assertIn("migration", out[0])


if __name__ == "__main__":
    unittest.main()
