"""Self-serve phrase training - step 5, PR C (fourth intake).

The owner types a phrase into a dialog; a background job trains a
custom openWakeWord model on the dGPU (tens of minutes), the tray
shows status while it runs, and on success the phrase auto-registers
in ``wake_phrases`` (active, correct role) and the listener reloads -
no CLI, no speaking, no manual registry edit.

Composition follows the meeting_job/gpu_guard pattern: this component
owns the behavior, the app is the wiring surface. The heavyweight
pipeline itself is the PR A machinery (whisper_sync.phrase_training
for config/commands, training/setup_trainer.py for the one-time
environment); this module only orchestrates subprocesses in a daemon
thread and never imports torch or openwakeword.

GPU posture: starting a job is an explicit owner action (they typed
the phrase and clicked Start), but the job still refuses while the
app is busy (recording/transcribing) or the model is deliberately
asleep (the gaming signal) - training must never grab the GPU out
from under either. Known limitation, recorded here on purpose: the
training subprocess is not killed if the app exits mid-run; the
finished .onnx lands in the workspace and the NEXT successful in-app
run (or a manual registry entry) picks it up.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import config
from .auto_sleep import app_busy
from .logger import logger
from .notifications import notify

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKSPACE = REPO_ROOT / "training" / "workspace"
TYPICAL_MINUTES = "20-40"

# Stage labels for the three official trainer invocations, in order.
STAGES = ("generating samples", "augmenting", "training")


class PhraseTrainer:
    """Owns the typed-phrase dialog and the background training job."""

    def __init__(self, app, workspace: Path | None = None):
        self.app = app
        self.workspace = Path(workspace or DEFAULT_WORKSPACE)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._phrase = ""
        self._stage = ""
        self._started = 0.0
        self._last_error: str | None = None

    # -- Status (menu surface) ------------------------------------------------

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def status_line(self) -> str | None:
        """One line for the tray menu; None when idle and clean."""
        if self.running:
            minutes = int((time.monotonic() - self._started) / 60)
            return (f"Training '{self._phrase}': {self._stage}, "
                    f"{minutes} min (typically {TYPICAL_MINUTES})")
        if self._last_error:
            return f"Last training failed: {self._last_error}"
        return None

    # -- Entry points ----------------------------------------------------------

    def ask_new_phrase(self, role: str) -> None:
        """Open the typed-phrase dialog on the dialog dispatcher."""
        from .meeting_dialogs import (center_window, flat_button,
                                      run_modal, style_window)

        kind = "wake" if role == "wake" else "outro"

        def _show(_proot):
            import tkinter as tk

            dlg = tk.Toplevel(_proot)
            style_window(dlg)
            dlg.title(f"New {kind} phrase")
            tk.Label(dlg, text=f"Type the new {kind} phrase exactly as "
                               "you would say it:",
                     bg="#1e1e2e", fg="#cdd6f4").pack(padx=16, pady=(14, 4))
            entry = tk.Entry(dlg, width=32, bg="#313244", fg="#cdd6f4",
                             insertbackground="#cdd6f4", relief="flat")
            entry.pack(padx=16, pady=4)
            entry.focus_set()
            tk.Label(dlg, text="Training runs on the GPU for "
                               f"{TYPICAL_MINUTES} minutes in the "
                               "background; the phrase activates "
                               "automatically when it finishes.",
                     bg="#1e1e2e", fg="#a6adc8",
                     wraplength=300, justify="left").pack(padx=16, pady=4)

            def _start(_ev=None):
                phrase = entry.get().strip()
                if not phrase:
                    return
                dlg.destroy()
                self.start(phrase, role)

            row = tk.Frame(dlg, bg="#1e1e2e")
            row.pack(pady=(6, 14))
            flat_button(row, "Start Training", _start).pack(
                side="left", padx=6)
            flat_button(row, "Cancel", dlg.destroy).pack(
                side="left", padx=6)
            entry.bind("<Return>", _start)
            center_window(dlg)
            run_modal(_proot, dlg)

        self.app._dialog_dispatcher.run(_show, label="set_phrase",
                                        wants_root=True)

    def start(self, phrase: str, role: str) -> bool:
        """Validate and launch the background job. Returns True when
        the job actually started."""
        from .phrase_training import sanitize_model_name

        if role not in ("wake", "outro"):
            # Anything else would register an entry the listener never
            # loads (review catch) - refuse loudly instead.
            notify("Phrase not started", f"Unknown phrase role {role!r}.")
            return False
        try:
            name = sanitize_model_name(phrase)
        except ValueError:
            notify("Phrase not usable",
                   "The phrase needs letters or digits.")
            return False
        with self._lock:
            if self.running:
                notify("Training already running",
                       f"'{self._phrase}' is still training; one job "
                       "at a time.")
                return False
            problem = self._not_ready_reason()
            if problem:
                notify("Training not started", problem)
                logger.info(f"Phrase training refused: {problem}")
                return False
            self._phrase = phrase
            self._stage = "starting"
            self._started = time.monotonic()
            self._last_error = None
            self._thread = threading.Thread(
                target=self._run, args=(phrase, name, role),
                daemon=True, name="phrase-trainer")
            self._thread.start()
        logger.info(f"Phrase training started: '{phrase}' ({role})")
        notify("Training started",
               f"'{phrase}' is training in the background "
               f"(typically {TYPICAL_MINUTES} min).")
        self.app._refresh_menu()
        return True

    def _not_ready_reason(self) -> str | None:
        """Why a job cannot start right now (None = good to go)."""
        trainer_python = (self.workspace / "trainer-env" / "Scripts"
                          / "python.exe")
        if not trainer_python.exists():
            return ("The training environment is not set up - run "
                    "training/setup_trainer.py --yes once (~20 GB).")
        current = self.app.state.current if self.app.state else None
        if current is not None and current.sleeping:
            return ("The model is asleep (gaming?). Wake the app "
                    "before training - it needs the GPU.")
        if app_busy(self.app):
            return "The app is busy recording or transcribing."
        return None

    # -- The job ---------------------------------------------------------------

    def _run(self, phrase: str, name: str, role: str) -> None:
        from .phrase_training import (build_training_config,
                                      trained_model_path,
                                      training_commands,
                                      write_training_config)

        trainer_python = (self.workspace / "trainer-env" / "Scripts"
                          / "python.exe")
        try:
            cfg = build_training_config(phrase, self.workspace)
            config_path = write_training_config(
                cfg, self.workspace / "output" / name / f"{name}.yml")
            log_path = config_path.with_name("training.log")
            with open(log_path, "a", encoding="utf-8") as log_file:
                for stage, cmd in zip(STAGES,
                                      training_commands(config_path,
                                                        trainer_python)):
                    self._stage = stage
                    self.app._refresh_menu()
                    result = subprocess.run(
                        cmd, cwd=self.workspace, stdout=log_file,
                        stderr=subprocess.STDOUT,
                        # Tray context: trainer pythons must not pop
                        # console windows (vram_probe precedent).
                        creationflags=getattr(subprocess,
                                              "CREATE_NO_WINDOW", 0))
                    if result.returncode != 0:
                        raise RuntimeError(
                            f"{stage} failed (exit {result.returncode}); "
                            f"log: {log_path}")
            produced = trained_model_path(cfg)
            if not produced.exists():
                raise RuntimeError(
                    f"trainer finished but {produced.name} is missing; "
                    f"log: {log_path}")
            phrases_dir = self.workspace / "phrases"
            phrases_dir.mkdir(parents=True, exist_ok=True)
            final = phrases_dir / produced.name
            shutil.copy2(produced, final)
            self._register(name, final, role)
            logger.info(f"Phrase '{phrase}' trained and activated: {final}")
            notify("Phrase ready",
                   f"'{phrase}' is trained and ACTIVE as a {role} "
                   "phrase.")
        except Exception as exc:
            self._last_error = str(exc)
            logger.warning(f"Phrase training failed: {exc}", exc_info=True)
            notify("Training failed", f"'{phrase}': {exc}")
        finally:
            self.app._refresh_menu()

    def _register(self, name: str, path: Path, role: str) -> None:
        """Add the trained phrase to wake_phrases, active, and reload
        the listener (same malformed-registry tolerance as the tray)."""
        cfg = self.app.cfg
        # transaction(): the tray's Saved Phrases toggle does the same
        # read-modify-write; without the lock held across it, whichever
        # writer finished last would silently drop the other's change
        # (review catch).
        with cfg.transaction():
            raw = cfg.get("wake_phrases", {})
            phrases = dict(raw) if isinstance(raw, dict) else {}
            phrases[name] = {"path": str(path), "role": role,
                             "active": True}
            cfg["wake_phrases"] = phrases
        config.save(cfg.snapshot())
        listener = getattr(self.app, "wake_listener", None)
        if listener is not None:
            listener.stop()
            listener.start()
