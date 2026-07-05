"""Tests for whisper_sync.phrase_trainer - the step 5 PR C component.

The trainer subprocesses are faked; these pin the orchestration: the
readiness gates (workspace, sleeping, busy, already-running), the
success path (registry auto-registration + listener reload + save),
the failure path (honest status, registry untouched), and the status
line. All CI-safe: no torch, no openwakeword, no Tk.
"""

import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from whisper_sync import config
from whisper_sync import phrase_trainer as trainer_mod
from whisper_sync.config_store import ConfigStore
from whisper_sync.phrase_trainer import PhraseTrainer
from whisper_sync.state_manager import StateManager, SLEEP_STARTED


class _FakeApp:
    def __init__(self):
        self.cfg = ConfigStore(config.load())
        self.state = StateManager(None, {})
        self.wake_listener = types.SimpleNamespace(
            stop=mock.Mock(), start=mock.Mock())
        self._dialog_dispatcher = mock.Mock()
        self.refreshes = 0

    def _refresh_menu(self):
        self.refreshes += 1


def _provision(workspace: Path):
    """Create the marker file the readiness gate checks."""
    py = workspace / "trainer-env" / "Scripts" / "python.exe"
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_bytes(b"")


class _Harness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name)
        self.app = _FakeApp()
        self.trainer = PhraseTrainer(self.app, workspace=self.workspace)
        patches = [
            mock.patch.object(trainer_mod, "notify"),
            mock.patch.object(trainer_mod.config, "save"),
            mock.patch.object(trainer_mod, "app_busy", return_value=False),
        ]
        self.notify, self.save, self.busy = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)

    def _join(self):
        thread = self.trainer._thread
        if thread is not None:
            thread.join(timeout=10)


class ReadinessGateTests(_Harness):
    def test_refuses_without_the_trainer_environment(self):
        self.assertFalse(self.trainer.start("hey hal", "wake"))
        self.assertIn("not set up", self.notify.call_args[0][1])

    def test_refuses_while_the_model_sleeps(self):
        _provision(self.workspace)
        self.app.state.emit(SLEEP_STARTED, sleeping=True)
        self.assertFalse(self.trainer.start("hey hal", "wake"))
        self.assertIn("asleep", self.notify.call_args[0][1])

    def test_refuses_while_the_app_is_busy(self):
        _provision(self.workspace)
        self.busy.return_value = True
        self.assertFalse(self.trainer.start("hey hal", "wake"))
        self.assertIn("busy", self.notify.call_args[0][1])

    def test_refuses_a_second_concurrent_job(self):
        _provision(self.workspace)
        release = threading.Event()
        self.trainer._phrase = "first"
        self.trainer._thread = threading.Thread(target=release.wait,
                                                daemon=True)
        self.trainer._thread.start()
        try:
            self.assertFalse(self.trainer.start("hey hal", "wake"))
            self.assertIn("already running", self.notify.call_args[0][0])
        finally:
            release.set()

    def test_refuses_an_unusable_phrase(self):
        _provision(self.workspace)
        self.assertFalse(self.trainer.start("!!!", "wake"))
        self.assertIn("not usable", self.notify.call_args[0][0])

    def test_refuses_an_unknown_role(self):
        # Review catch: any other role would register an entry the
        # listener never loads.
        _provision(self.workspace)
        self.assertFalse(self.trainer.start("hey hal", "command"))
        self.assertIn("role", self.notify.call_args[0][1])


class JobOutcomeTests(_Harness):
    def _fake_run(self, create_model=True):
        workspace = self.workspace

        def _run(cmd, **kw):
            if create_model and cmd[-1] == "--train_model":
                onnx = workspace / "output" / "hey_hal" / "hey_hal.onnx"
                onnx.parent.mkdir(parents=True, exist_ok=True)
                onnx.write_bytes(b"model")
            return types.SimpleNamespace(returncode=0)
        return _run

    def test_success_registers_active_phrase_and_reloads_listener(self):
        _provision(self.workspace)
        with mock.patch.object(trainer_mod.subprocess, "run",
                               side_effect=self._fake_run()):
            self.assertTrue(self.trainer.start("hey hal", "wake"))
            self._join()
        entry = self.app.cfg["wake_phrases"]["hey_hal"]
        self.assertTrue(entry["active"])
        self.assertEqual(entry["role"], "wake")
        self.assertTrue(entry["path"].endswith("hey_hal.onnx"))
        self.assertIn("phrases", entry["path"],
                      "model must be copied to the phrases dir")
        self.app.wake_listener.stop.assert_called_once()
        self.app.wake_listener.start.assert_called_once()
        self.save.assert_called()
        self.assertIn("Phrase ready", self.notify.call_args[0][0])
        self.assertIsNone(self.trainer.status_line())

    def test_stage_failure_reports_and_leaves_registry_untouched(self):
        _provision(self.workspace)

        def _fail(cmd, **kw):
            return types.SimpleNamespace(returncode=3)
        with mock.patch.object(trainer_mod.subprocess, "run",
                               side_effect=_fail):
            self.assertTrue(self.trainer.start("hey hal", "wake"))
            self._join()
        self.assertNotIn("hey_hal",
                         self.app.cfg.get("wake_phrases", {}) or {})
        self.assertIn("Training failed", self.notify.call_args[0][0])
        self.assertIn("failed", self.trainer.status_line())

    def test_missing_output_model_is_a_failure(self):
        _provision(self.workspace)
        with mock.patch.object(trainer_mod.subprocess, "run",
                               side_effect=self._fake_run(
                                   create_model=False)):
            self.trainer.start("hey hal", "wake")
            self._join()
        self.assertIn("missing", self.trainer.status_line())


class StatusAndRegistryTests(_Harness):
    def test_status_line_while_running(self):
        self.trainer._phrase = "hey hal"
        self.trainer._stage = "training"
        self.trainer._started = time.monotonic() - 300
        with mock.patch.object(PhraseTrainer, "running",
                               new_callable=mock.PropertyMock,
                               return_value=True):
            line = self.trainer.status_line()
        self.assertIn("hey hal", line)
        self.assertIn("training", line)
        self.assertIn("5 min", line)

    def test_register_repairs_a_malformed_registry(self):
        self.app.cfg["wake_phrases"] = ["not", "a", "dict"]
        self.trainer._register("hey_hal", Path("C:/p/hey_hal.onnx"),
                               "outro")
        entry = self.app.cfg["wake_phrases"]["hey_hal"]
        self.assertEqual(entry["role"], "outro")
        self.assertTrue(entry["active"])

    def test_ask_new_phrase_routes_to_the_dialog_dispatcher(self):
        self.trainer.ask_new_phrase("wake")
        self.app._dialog_dispatcher.run.assert_called_once()
        self.assertEqual(
            self.app._dialog_dispatcher.run.call_args.kwargs["label"],
            "set_phrase")


def _load_setup_trainer():
    """setup_trainer.py is a script under training/, not a package
    module; load it by path. Its top level imports only stdlib, so
    this stays CI-safe (no torch)."""
    import importlib.util
    path = (Path(__file__).resolve().parent.parent / "training" /
            "setup_trainer.py")
    spec = importlib.util.spec_from_file_location("setup_trainer", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class PatchFileTests(unittest.TestCase):
    """_patch_file backs the Windows/torch-2.x compat patches applied
    to the pinned training stack (first supervised run, 2026-07-05):
    it must apply once, skip when already applied, and fail loudly
    when the pinned code drifts from the patch anchor."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.target = Path(self._tmp.name) / "mod.py"
        self.st = _load_setup_trainer()

    def test_applies_the_patch(self):
        self.target.write_text("a = 1\nb = 2\n", encoding="utf-8")
        self.st._patch_file(
            self.target, "b = 2",
            "# Patched by setup_trainer.py\nb = 3")
        self.assertIn("b = 3", self.target.read_text(encoding="utf-8"))

    def test_skips_when_already_patched(self):
        content = "# Patched by setup_trainer.py\nb = 3\n"
        self.target.write_text(content, encoding="utf-8")
        self.st._patch_file(self.target, "b = 2",
                            "# Patched by setup_trainer.py\nb = 3")
        self.assertEqual(
            self.target.read_text(encoding="utf-8"), content)

    def test_second_patch_applies_to_an_already_patched_file(self):
        # A file can carry several patches; an earlier patch must not
        # block a later one (idempotence is per-patch, not per-file).
        self.target.write_text(
            "# Patched by setup_trainer.py\nb = 3\nz = 1\n",
            encoding="utf-8")
        self.st._patch_file(self.target, "z = 1", "z = 2")
        text = self.target.read_text(encoding="utf-8")
        self.assertIn("z = 2", text)
        self.assertIn("b = 3", text)

    def test_fails_loudly_when_the_anchor_drifted(self):
        self.target.write_text("something else entirely\n",
                               encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.st._patch_file(self.target, "b = 2", "b = 3")

    def test_fails_loudly_when_the_anchor_is_ambiguous(self):
        self.target.write_text("b = 2\nc = 9\nb = 2\n", encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.st._patch_file(self.target, "b = 2", "b = 3")

    def test_fails_loudly_when_the_target_is_missing(self):
        with self.assertRaises(SystemExit):
            self.st._patch_file(self.target.with_name("gone.py"),
                                "b = 2", "b = 3")

    def test_shim_constant_compiles(self):
        compile(self.st.SITECUSTOMIZE_SHIM, "sitecustomize.py", "exec")

    def test_verify_sha256_accepts_match_and_rejects_mismatch(self):
        import hashlib
        blob = self.target.with_suffix(".bin")
        blob.write_bytes(b"checkpoint bytes")
        good = hashlib.sha256(b"checkpoint bytes").hexdigest()
        self.st._verify_sha256(blob, good)  # must not raise
        with self.assertRaises(SystemExit):
            self.st._verify_sha256(blob, "0" * 64)


if __name__ == "__main__":
    unittest.main()
