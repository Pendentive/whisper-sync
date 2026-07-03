"""Tests for whisper_sync.instance_guard - single-instance lock + orphan reaping.

The mutex tests use a real named Win32 mutex (unique per test run) on
Windows and are skipped elsewhere. The reaping tests replace the
process-inspection seams so no real process is ever touched.
"""

import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from whisper_sync import instance_guard
from whisper_sync.instance_guard import (
    acquire_single_instance, register_worker_pid, unregister_worker_pid,
    reap_orphans,
)


class MutexTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "named mutex is Windows-only")
    def test_second_acquire_of_same_name_fails(self):
        name = f"Global\\WhisperSyncTest-{uuid.uuid4()}"
        self.assertTrue(acquire_single_instance(name), "first acquire must win")
        self.assertFalse(acquire_single_instance(name), "second acquire must lose")

    def test_non_windows_degrades_to_acquired(self):
        with mock.patch.object(os, "name", "posix"):
            self.assertTrue(acquire_single_instance("irrelevant"))


class _RegistryHarness(unittest.TestCase):
    """Points the registry at a temp file for each test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.reg_path = Path(self._tmp.name) / "worker-pids.json"
        patcher = mock.patch.object(
            instance_guard, "_registry_path", lambda: self.reg_path
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _registry(self):
        if not self.reg_path.exists():
            return {}
        return json.loads(self.reg_path.read_text())


class RegistryTests(_RegistryHarness):
    def test_register_and_unregister_roundtrip(self):
        register_worker_pid(12345, kind="transcription")
        self.assertEqual(self._registry()["12345"]["kind"], "transcription")
        self.assertEqual(self._registry()["12345"]["parent"], os.getpid())
        unregister_worker_pid(12345)
        self.assertNotIn("12345", self._registry())

    def test_corrupt_registry_treated_as_empty(self):
        self.reg_path.write_text("{not json")
        register_worker_pid(1, kind="x")  # must not raise
        self.assertIn("1", self._registry())


class ReapTests(_RegistryHarness):
    def _seed(self, pid, parent):
        register_worker_pid(pid, kind="transcription")
        data = self._registry()
        data[str(pid)]["parent"] = parent
        self.reg_path.write_text(json.dumps(data))

    def test_reaps_alive_python_orphan_from_dead_parent(self):
        self._seed(111, parent=999999)  # not our pid
        killed = []
        with mock.patch.object(instance_guard, "_pid_alive", lambda p: True), \
             mock.patch.object(instance_guard, "_is_python_process", lambda p: True), \
             mock.patch.object(instance_guard, "_terminate",
                               lambda p: killed.append(p) or True):
            reaped = reap_orphans()
        self.assertEqual(reaped, [111])
        self.assertEqual(killed, [111])
        self.assertNotIn("111", self._registry(), "reaped entry must be removed")

    def test_skips_own_children(self):
        self._seed(222, parent=os.getpid())
        with mock.patch.object(instance_guard, "_terminate",
                               lambda p: self.fail("must not touch own child")):
            reaped = reap_orphans()
        self.assertEqual(reaped, [])
        self.assertIn("222", self._registry(), "own child entry must survive")

    def test_drops_dead_pid_without_terminating(self):
        self._seed(333, parent=999999)
        with mock.patch.object(instance_guard, "_pid_alive", lambda p: False), \
             mock.patch.object(instance_guard, "_terminate",
                               lambda p: self.fail("dead pid must not be terminated")):
            reaped = reap_orphans()
        self.assertEqual(reaped, [])
        self.assertNotIn("333", self._registry(), "stale entry must be dropped")

    def test_pid_reuse_guard_refuses_non_python_process(self):
        self._seed(444, parent=999999)
        with mock.patch.object(instance_guard, "_pid_alive", lambda p: True), \
             mock.patch.object(instance_guard, "_is_python_process", lambda p: False), \
             mock.patch.object(instance_guard, "_terminate",
                               lambda p: self.fail("reused pid must not be terminated")):
            reaped = reap_orphans()
        self.assertEqual(reaped, [])
        self.assertNotIn("444", self._registry(), "reused-pid entry must be dropped")

    def test_unkillable_pid_stays_registered_for_next_run(self):
        self._seed(555, parent=999999)
        with mock.patch.object(instance_guard, "_pid_alive", lambda p: True), \
             mock.patch.object(instance_guard, "_is_python_process", lambda p: True), \
             mock.patch.object(instance_guard, "_terminate", lambda p: False):
            reaped = reap_orphans()
        self.assertEqual(reaped, [])
        self.assertIn("555", self._registry(), "unkillable pid must be retried next start")


if __name__ == "__main__":
    unittest.main()
