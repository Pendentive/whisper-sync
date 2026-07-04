"""Tests for whisper_sync.app_control.AppControl (extraction 4, final).

Pins the update guard fold (AppState.updating), the git step sequence
decisions (up-to-date vs update-and-restart), and the shutdown order
shared by restart and quit. Threads run inline; os._exit, subprocess,
and sleeps are stubbed - a real exit would kill the test runner.
"""

import sys
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from whisper_sync import app_control
from whisper_sync.app_control import AppControl
from whisper_sync.state_manager import StateManager


class _InlineThread:
    def __init__(self, target=None, daemon=None, name=None):
        self._target = target

    def start(self):
        self._target()


class _FakeRecorder:
    is_recording = False
    stopped = 0

    def stop(self):
        self.stopped += 1


class _FakeApp:
    def __init__(self):
        self.state = StateManager(None, {})
        self.tray = types.SimpleNamespace(stop=mock.Mock())
        self.recorder = _FakeRecorder()
        self.worker = types.SimpleNamespace(stop=mock.Mock())
        self._backup = types.SimpleNamespace(stop=mock.Mock())
        self.meetings = types.SimpleNamespace(shutdown_post_worker=mock.Mock())


class _FakeRun:
    """Records git commands; returns per-command canned results."""

    def __init__(self, rev_count="0"):
        self.commands = []
        self.rev_count = rev_count

    def __call__(self, cmd, cwd=None, capture_output=None, text=None, timeout=None):
        self.commands.append(cmd[:3])
        out = ""
        if cmd[:2] == ["git", "rev-list"]:
            out = self.rev_count
        elif cmd[:2] == ["git", "branch"]:
            out = "dev"
        return types.SimpleNamespace(returncode=0, stdout=out, stderr="")


class _ControlHarness(unittest.TestCase):
    def setUp(self):
        self.app = _FakeApp()
        self.control = AppControl(self.app)
        patches = [
            mock.patch.object(app_control.threading, "Thread", _InlineThread),
            mock.patch.object(app_control, "notify"),
            mock.patch.dict(sys.modules, {"keyboard": types.ModuleType("keyboard")}),
            mock.patch.object(app_control.weekly_stats, "flush"),
            mock.patch.object(app_control.lifecycle, "record_exit_reason"),
            mock.patch.object(app_control.lifecycle, "log_exit_banner"),
            mock.patch("time.sleep"),
            mock.patch("os._exit"),
        ]
        sys.modules_backup = None
        self.mocks = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        self.notify = self.mocks[1]
        self.os_exit = self.mocks[-1]
        sys.modules["keyboard"].unhook_all = mock.Mock()


class UpdateTests(_ControlHarness):
    def test_update_folds_guard_into_appstate(self):
        seen = []
        with mock.patch.object(AppControl, "_run_update_steps",
                               lambda s, sp, root, br: seen.append(
                                   self.app.state.current.updating)):
            self.control.update("dev")
        self.assertEqual(seen, [True], "updating must be True during the run")
        self.assertFalse(self.app.state.current.updating,
                         "updating must clear when the run ends")

    def test_second_update_ignored_while_running(self):
        calls = []

        def _steps(s, sp, root, br):
            calls.append(br)
            self.control.update("dev")  # re-entrant click mid-update

        with mock.patch.object(AppControl, "_run_update_steps", _steps):
            self.control.update("dev")
        self.assertEqual(calls, ["dev"], "concurrent update must be a no-op")

    def test_update_ignored_before_run(self):
        self.app.state = None
        self.control.update("dev")  # must not raise

    def test_guard_clears_after_git_failure(self):
        with mock.patch.object(AppControl, "_run_update_steps",
                               side_effect=RuntimeError("boom")):
            self.control.update("dev")
        self.assertFalse(self.app.state.current.updating)
        self.assertIn("Update failed", self.notify.call_args[0][0])


class UpdateStepsTests(_ControlHarness):
    def test_up_to_date_does_not_restart(self):
        run = _FakeRun(rev_count="0")
        fake_sp = types.SimpleNamespace(run=run)
        with mock.patch.object(AppControl, "restart") as restart:
            self.control._run_update_steps(fake_sp, "root", "dev")
        restart.assert_not_called()
        self.assertTrue(any("up to date" in str(c).lower()
                            for c in self.notify.call_args_list))

    def test_new_commits_pull_then_restart(self):
        run = _FakeRun(rev_count="3")
        fake_sp = types.SimpleNamespace(run=run)
        with mock.patch.object(AppControl, "restart") as restart:
            self.control._run_update_steps(fake_sp, "root", "dev")
        restart.assert_called_once()
        self.assertIn(["git", "pull", "origin"], run.commands)


class ShutdownTests(_ControlHarness):
    def test_cleanup_stops_everything(self):
        self.app.recorder.is_recording = True
        self.control._cleanup()
        self.assertEqual(self.app.recorder.stopped, 1)
        self.app.worker.stop.assert_called_once()
        self.app._backup.stop.assert_called_once()
        self.app.meetings.shutdown_post_worker.assert_called_once()
        sys.modules["keyboard"].unhook_all.assert_called_once()

    def test_quit_stops_tray_and_records_reason(self):
        self.control.quit()
        self.app.tray.stop.assert_called_once()
        app_control.lifecycle.record_exit_reason.assert_called_once_with(
            app_control.lifecycle.REASON_USER_QUIT)
        self.os_exit.assert_not_called()

    def test_restart_spawns_new_process_then_exits(self):
        with mock.patch("subprocess.Popen") as popen:
            self.control.restart()
        popen.assert_called_once()
        self.assertIn("whisper_sync", popen.call_args[0][0])
        self.app.tray.stop.assert_called_once()
        self.os_exit.assert_called_once_with(0)
        app_control.lifecycle.record_exit_reason.assert_called_once_with(
            app_control.lifecycle.REASON_USER_RESTART)


if __name__ == "__main__":
    unittest.main()
