"""WhisperSync entry point — tray icon + hotkey listener."""

import logging
import os
import sys
import threading
import warnings

# Set PYTHONWARNINGS env var so spawned subprocesses (multiprocessing "spawn"
# context) also suppress these warnings. The warnings module filters only
# apply to the current process; subprocesses start fresh Python interpreters.
os.environ["PYTHONWARNINGS"] = (
    "ignore::UserWarning:pyannote.audio.core.io,"
    "ignore::UserWarning:pyannote.audio.utils.reproducibility"
)

# Suppress known harmless warnings in the main process too
warnings.filterwarnings("ignore", message="torchcodec is not installed correctly",
                        category=UserWarning, module=r"pyannote\.audio\.core\.io")
warnings.filterwarnings("ignore", message="TensorFloat-32.*has been disabled",
                        module=r"pyannote\.audio\.utils\.reproducibility")
warnings.filterwarnings("ignore", message="std\\(\\): degrees of freedom is <= 0",
                        category=UserWarning)
logging.getLogger("lightning.pytorch.utilities.migration.utils").setLevel(logging.ERROR)
logging.getLogger("whisperx.vads.pyannote").setLevel(logging.WARNING)
logging.getLogger("whisperx.diarize").setLevel(logging.WARNING)
from pathlib import Path

import keyboard
import pystray

from . import config
from .config_store import ConfigStore
from .executors import IO, submit_or_spawn
from .idle_reset import schedule_idle_reset
from .capture import AudioRecorder
from .icons import (idle_icon, build_icon, resolve_icon_key, ICON_REGISTRY,
                     FlashController)
from .logger import logger, get_log_path, set_console_level
from .model_status import bootstrap_models
from .paths import (get_install_root,
                     get_data_dir, get_dictation_log_dir,
                     get_legacy_config_path, get_legacy_speaker_config_path,
                     get_legacy_dictation_log_dir, get_config_path as get_data_config_path,
                     get_speaker_config_path)
from .worker_manager import TranscriptionWorker
from .gpu_guard import GpuGuard
from .backup_worker import BackupTranscriber
from . import weekly_stats
from .streaming_wav import fix_orphan
from .crash_diagnostics import install_excepthook, check_previous_crash, install_faulthandler
from . import lifecycle
from .heartbeat import Heartbeat
from .notifications import notify, ToastListener
from .state_manager import StateManager
from .dictation_flow import DictationFlow
from .meeting_flow import MeetingFlow
from .tray_menu import TrayMenu
from .github_tray import GitHubTray
from .meeting_dialogs import MeetingDialogs
from .dialog_dispatcher import DialogDispatcher

def _get_cpu_name() -> str:
    """Get CPU model name via PowerShell CIM on Windows, platform.processor() fallback."""
    import platform
    if platform.system() == "Windows":
        try:
            import subprocess
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_Processor).Name"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                name = result.stdout.strip()
                # Strip trailing "Processor" if present
                if name.endswith(" Processor"):
                    name = name[:-len(" Processor")]
                return name
        except Exception:
            pass
    name = platform.processor()
    return name if name else "Unknown CPU"


class WhisperSync:
    def __init__(self):
        self._migrate_data()
        self.cfg = ConfigStore(config.load())
        set_console_level(self.cfg.get("log_window", "normal"))
        self.recorder = AudioRecorder(sample_rate=self.cfg["sample_rate"])
        self.tray = None
        self.state = None  # Initialized after tray creation in run()
        self._menu_refresher = None  # MenuRefresher, created in run()
        self._lock = threading.RLock()
        self._tray_lock = threading.Lock()  # Serialize all tray icon/title updates (pystray not thread-safe)
        self._gpu_guard = GpuGuard(self.cfg, notify=notify)
        dictation_model = self._gpu_guard.effective_model(
            self.cfg.get("dictation_model", self.cfg["model"]))
        self.worker = TranscriptionWorker(self.cfg, preload_model=dictation_model)
        self._backup = BackupTranscriber(self.cfg)
        # Single long-lived thread for ALL tkinter dialogs. tk.Tk() must not
        # be created on rotating worker threads; doing so corrupts Win32 heap
        # state and crashes the next GC cycle (fatal exception 0x80000003).
        # See whisper_sync/dialog_dispatcher.py for the rationale.
        self._dialog_dispatcher = DialogDispatcher()
        self._dialog_dispatcher.start()
        self._cpu_name = _get_cpu_name()
        # Session stats: lock-guarded; mutated from dictation/overlay/meeting
        # worker threads concurrently (see session_stats.py).
        from .session_stats import SessionStats
        self._stats = SessionStats()
        # Re-entry-gated icon flashes (icons.FlashController); flows call
        # the _yellow_flash/_flash_queued delegates below.
        self._flash = FlashController(lambda: self.tray, self._tray_lock)
        # Dictation workflow component (dictation_flow.py): dictation,
        # overlay, feature-suggest, discard, recovery, history. The app
        # remains the wiring surface (hardening item 6).
        self.dictation = DictationFlow(self)
        # Meeting dialog component (meeting_dialogs.py): Tk builders run
        # on the dialog dispatcher; behavior extracted from this class.
        self.dialogs = MeetingDialogs(self)
        # Meeting workflow component (meeting_flow.py): record/save/
        # post-process pipeline, recovery, rename and minutes helpers.
        self.meetings = MeetingFlow(self)
        # Tray menu + settings component and GitHub PR status glue.
        self.menu = TrayMenu(self)
        self.github = GitHubTray(self)

    @staticmethod
    def _migrate_data():
        """Migrate user data files from legacy locations to output_dir/.whispersync/.

        Copies (not moves) files so the legacy locations remain valid until
        the user explicitly cleans them up.
        """
        import shutil

        data_dir = get_data_dir()  # creates .whispersync/ if needed

        # 1. config.json
        legacy_cfg = get_legacy_config_path()
        new_cfg = get_data_config_path()
        if legacy_cfg.exists() and not new_cfg.exists():
            shutil.copy2(legacy_cfg, new_cfg)
            logger.info(f"Migrated config.json -> {new_cfg}")

        # 2. transcription-config.md
        legacy_speaker = get_legacy_speaker_config_path()
        new_speaker = get_speaker_config_path()
        if legacy_speaker.exists() and not new_speaker.exists():
            shutil.copy2(legacy_speaker, new_speaker)
            logger.info(f"Migrated transcription-config.md -> {new_speaker}")

        # 3. dictation-logs/
        legacy_dict_dir = get_legacy_dictation_log_dir()
        new_dict_dir = get_dictation_log_dir()
        if legacy_dict_dir.exists() and any(legacy_dict_dir.iterdir()):
            if not new_dict_dir.exists() or not any(new_dict_dir.iterdir()):
                new_dict_dir.mkdir(parents=True, exist_ok=True)
                for f in legacy_dict_dir.iterdir():
                    if f.is_file():
                        dest = new_dict_dir / f.name
                        if not dest.exists():
                            shutil.copy2(f, dest)
                logger.info(f"Migrated dictation logs -> {new_dict_dir}")

    def _update_tray(self, icon=None, title=None, menu=None):
        """Thread-safe tray update under _tray_lock.

        Every pystray mutation must hold _tray_lock: either via this
        method or, on the animation hot path, IconAnimator's direct
        icon/title writes (icons.py) which take the same lock. Menu
        swaps specifically must come through here (via MenuRefresher) —
        assigning tray.menu from arbitrary threads raced the Win32 pump
        and corrupted the heap (2026-05-07 crash: _build_menu <-
        _refresh_menu <- _process_overlay).
        """
        with self._tray_lock:
            if self.tray is None:
                return
            if icon is not None:
                self.tray.icon = icon
            if title is not None:
                self.tray.title = title
            if menu is not None:
                self.tray.menu = menu
                self.tray.update_menu()

    def _yellow_flash(self):
        self._flash.yellow()

    def _flash_queued(self):
        self._flash.queued()

    # --- Click dispatch ---

    def _dispatch_action(self, action: str):
        if action == "meeting":
            self.meetings.toggle()
        elif action == "dictation":
            self.dictation.toggle()

    def _on_left_click(self):
        # Left-click while dictating = discard (stop recording, throw away audio)
        current = self.state.current if self.state else None
        mode = current.mode if current else None
        overlay = current.dictation_overlay if current else False
        if mode == "dictation" or overlay:
            self.dictation.discard()
            return
        self._dispatch_action(self.cfg.get("left_click", "meeting"))

    def _on_middle_click(self):
        self._dispatch_action(self.cfg.get("middle_click", "dictation"))

    # --- Recording modes ---

    def _schedule_idle(self, seconds: float, blink: bool = False):
        """Return to idle after a delay. If blink=True, blink done 3 times first.

        Delegates to idle_reset.schedule_idle_reset (scheduler jobs, no
        per-event thread). Mode is only reset if still terminal.
        """
        schedule_idle_reset(self.state, seconds, blink)

    def _can_record(self) -> bool:
        """Can we start a new recording? Allowed if idle or just transcribing in background."""
        mode = self.state.current.mode if self.state else None
        return mode is None or mode in ("transcribing", "done", "error")

    def _output_dir(self) -> Path:
        p = Path(self.cfg["output_dir"])
        if not p.is_absolute():
            # Relative paths resolve from repo root
            p = get_install_root() / p
        return p

    def _refresh_menu(self):
        """Request a debounced menu rebuild. Safe from any thread.

        The actual rebuild runs on the scheduler thread via MenuRefresher
        and the swap goes through _update_tray under _tray_lock. Burst
        requests (dictation completion fires history + stats + state
        refreshes back-to-back) coalesce into one rebuild.
        """
        refresher = getattr(self, "_menu_refresher", None)
        if refresher is not None and self.tray is not None:
            refresher.request()

    _updating = False  # Guard against concurrent update clicks

    def _update(self, branch="dev"):
        """Pull latest code from a branch and restart if updated."""
        if self._updating:
            logger.debug("Update already in progress, ignoring")
            return
        self._updating = True

        def _do_update():
            import subprocess as _sp
            from .executors import native_call
            repo_root = str(get_install_root())

            # One native-call span for the whole git sequence; over-marking
            # is safe (idle GC just skips a tick), under-marking is not.
            try:
                with native_call("git-update"):
                    self._run_update_steps(_sp, repo_root, branch)
            except FileNotFoundError:
                logger.error("git not found on PATH")
                notify("Update failed", "git not found, check installation")
            except _sp.TimeoutExpired:
                logger.error("git command timed out during update")
                notify("Update failed", "git timed out, check network")
            except Exception as e:
                logger.error(f"Update failed: {e}")
                notify("Update failed", "Unexpected error, check console")
            finally:
                self._updating = False

        threading.Thread(target=_do_update, daemon=True).start()

    def _run_update_steps(self, _sp, repo_root: str, branch: str):
        """Run the git fetch/checkout/pull sequence for self-update."""
        notify("Updating WhisperSync...", f"Pulling latest from {branch}")

                # Check for uncommitted changes
        status = _sp.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root, capture_output=True, text=True, timeout=10
        )
        if status.stdout.strip():
            logger.warning(f"Uncommitted changes detected:\n{status.stdout.strip()}")

        # Fetch
        fetch = _sp.run(
            ["git", "fetch", "origin", branch],
            cwd=repo_root, capture_output=True, text=True, timeout=30
        )
        if fetch.returncode != 0:
            logger.error(f"git fetch failed: {fetch.stderr}")
            notify("Update failed", "git fetch failed, check console")
            return

        # Check if there are updates
        diff = _sp.run(
            ["git", "rev-list", f"HEAD..origin/{branch}", "--count"],
            cwd=repo_root, capture_output=True, text=True, timeout=10
        )
        count_str = (diff.stdout or "").strip()
        commit_count = int(count_str) if count_str.isdigit() else 0

        if commit_count == 0:
            notify("Already up to date", f"No new changes on {branch}")
            return

        # Checkout the target branch if not already on it
        current = _sp.run(
            ["git", "branch", "--show-current"],
            cwd=repo_root, capture_output=True, text=True, timeout=10
        )
        if current.stdout.strip() != branch:
            checkout = _sp.run(
                ["git", "checkout", branch],
                cwd=repo_root, capture_output=True, text=True, timeout=15
            )
            if checkout.returncode != 0:
                logger.error(f"git checkout {branch} failed: {checkout.stderr}")
                notify("Update failed", f"Could not switch to {branch}")
                return

        pull = _sp.run(
            ["git", "pull", "origin", branch],
            cwd=repo_root, capture_output=True, text=True, timeout=60
        )
        if pull.returncode != 0:
            logger.error(f"git pull failed: {pull.stderr}")
            notify("Update failed", "git pull failed, check console")
            return

        logger.info(f"Updated from {branch}: {commit_count} new commit(s)")
        notify("Updated!", f"{commit_count} commit(s) from {branch}. Restarting...")

        import time
        time.sleep(2)  # Let the notification display
        self._restart()

    # Empirically chosen delay (seconds) to let pystray menu callbacks
    # return before we call tray.stop(). Without this, WM_QUIT is posted
    # but can't be processed while the callback is still on the stack.
    _CALLBACK_DEFER_SECS = 0.3

    def _cleanup(self):
        """Shared shutdown sequence for restart and quit."""
        try:
            weekly_stats.flush()
        except Exception:
            logger.debug("Stats flush failed during shutdown", exc_info=True)
        # Signal post-processing worker to shut down
        try:
            self.meetings.shutdown_post_worker()
        except Exception:
            logger.debug("Post-queue shutdown signal failed", exc_info=True)
        try:
            if self.recorder.is_recording:
                self.recorder.stop()
            self.worker.stop()
            self._backup.stop()
            keyboard.unhook_all()
        except Exception:
            logger.debug("Cleanup error during shutdown", exc_info=True)

    def _restart(self):
        """Restart WhisperSync by cleaning up, stopping tray, then spawning.

        Deferred to a background thread because pystray menu callbacks
        run inside the Win32 message pump. tray.stop() posts WM_QUIT
        but can't process it until the callback returns.
        """
        def _do_restart():
            import subprocess
            import time
            lifecycle.record_exit_reason(lifecycle.REASON_USER_RESTART)
            time.sleep(self._CALLBACK_DEFER_SECS)
            self._cleanup()
            # Spawn new process first so it starts loading immediately
            subprocess.Popen(
                [sys.executable, "-m", "whisper_sync"],
                cwd=str(Path(__file__).parent.parent),
            )
            # Stop tray icon, then force-exit. os._exit() is needed because
            # daemon threads (worker subprocesses, keyboard listener, etc.)
            # can keep the process alive after tray.stop() returns.
            if self.tray:
                self.tray.stop()
            time.sleep(0.2)
            lifecycle.log_exit_banner(logger)
            os._exit(0)

        threading.Thread(target=_do_restart, daemon=True).start()

    def quit(self):
        """Quit WhisperSync. Deferred like _restart for the same reason."""
        def _do_quit():
            import time
            lifecycle.record_exit_reason(lifecycle.REASON_USER_QUIT)
            time.sleep(self._CALLBACK_DEFER_SECS)
            self._cleanup()
            if self.tray:
                self.tray.stop()
            # Explicit banner for symmetry with _restart. In theory the
            # atexit hook emits one when the interpreter shuts down, but
            # daemon threads keeping the interpreter alive can swallow
            # atexit; better to log here and let atexit be a no-op via
            # the first-wins rule in record_exit_reason.
            lifecycle.log_exit_banner(logger)
        threading.Thread(target=_do_quit, daemon=True).start()

    def _prompt_large_download(self, model_name: str, size: str) -> bool:
        """Show a tkinter dialog asking if user wants to download a large model."""
        result = [False]

        def _show(_proot):
            from tkinter import messagebox
            result[0] = messagebox.askyesno(
                "WhisperSync: Large Download",
                f"Model '{model_name}' ({size}) is not cached locally.\n\n"
                f"Download now?\n\n"
                f"Warning: This is a large download.\n"
                f"Skip if you are on mobile data.",
                parent=_proot,
            )

        try:
            self._dialog_dispatcher.run(_show, label="prompt_large_download", wants_root=True)
        except Exception:
            logger.exception("dialog crashed: _prompt_large_download")

        return result[0]

    def run(self):
        # Clear any orphaned keyboard hooks from a previous crash
        # This prevents the stuck-Ctrl-key bug when restarting after abnormal exit
        try:
            keyboard.unhook_all()
        except Exception:
            pass

        # Check for orphaned meeting temp WAVs from a previous crash
        self.meetings.scan_recovered_temp()

        # Check for orphaned dictation WAVs from a previous crash
        # (successful dictations delete the WAV, so any .wav here = crash)
        self._recovered_dictation_paths = []
        dict_log_dir = get_dictation_log_dir()
        if dict_log_dir.exists():
            for wav in sorted(dict_log_dir.glob("*.wav")):
                dur = fix_orphan(wav)
                if dur is not None:
                    logger.warning(f"Recovered {dur:.0f}s dictation from crash: {wav.name}")
                    self._recovered_dictation_paths.append(str(wav))
                else:
                    logger.info(f"Cleaned up stale dictation WAV: {wav.name}")

        # Bootstrap: ensure base models are cached, prompt for large ones
        bootstrap_models(self.cfg, on_large_model=self._prompt_large_download)

        def _guarded(fn, label):
            # Any exception that escapes a hotkey callback propagates to
            # keyboard._generic.process and kills the single dispatcher
            # thread, bricking ALL hotkeys for the rest of the session.
            # This wrapper is the last line of defense; individual handlers
            # also try/except their own failures.
            def _inner():
                try:
                    return fn()
                except Exception as e:
                    logger.error("%s hotkey handler crashed: %s", label, e, exc_info=True)
                    try:
                        notify("WhisperSync error", f"{label}: {e}")
                    except Exception:
                        pass
            return _inner

        keyboard.add_hotkey(
            self.cfg["hotkeys"]["dictation_toggle"],
            _guarded(self.dictation.toggle, "dictation"),
            suppress=False,
        )
        keyboard.add_hotkey(
            self.cfg["hotkeys"]["meeting_toggle"],
            _guarded(self.meetings.toggle, "meeting"),
            suppress=False,
        )
        feature_hk = self.cfg["hotkeys"].get("feature_suggest", "ctrl+shift+alt+f")
        if feature_hk:
            keyboard.add_hotkey(
                feature_hk,
                _guarded(self.dictation.toggle_feature_suggest, "feature-suggest"),
                suppress=False,
            )

        self.tray = pystray.Icon(
            "whisper-sync",
            idle_icon(),
            "WhisperSync: Idle",
            menu=self.menu.build(),
        )

        # Debounced single-owner menu refresh (see tray_refresh.py). All
        # _refresh_menu() calls from any thread coalesce here; the rebuild
        # runs on the scheduler thread and swaps under _tray_lock.
        from .tray_refresh import MenuRefresher
        self._menu_refresher = MenuRefresher(
            build_menu=self.menu.build,
            apply_menu=lambda m: self._update_tray(menu=m),
        )

        self.state = StateManager(self.tray, self.cfg)

        # Start the post-processing worker (single thread, sequential
        # meeting processing) owned by the meeting flow.
        self.meetings.start_post_worker()

        # Register icon updater as a global listener on all state events
        def _on_state_change(event):
            if self.tray is None:
                return
            speaker_ok = getattr(self.recorder, "speaker_loopback_active", True) if hasattr(self, "recorder") else True
            s = event.new_state
            key = resolve_icon_key(
                mode=s.mode,
                meeting_transcribing=s.meeting_transcribing,
                dictation_overlay=s.dictation_overlay,
                speaker_ok=speaker_ok,
            )
            spec = ICON_REGISTRY[key]
            progress = s.progress
            icon = build_icon(spec, progress=progress)
            self._update_tray(icon=icon, title=f"WhisperSync: {spec.tooltip}")
        self.state.on_any(_on_state_change)

        # Register toast notification listener
        toast_listener = ToastListener(self.cfg)
        self.state.on_any(toast_listener)

        dictation_model = self._gpu_guard.effective_model(
            self.cfg.get("dictation_model", self.cfg["model"]))
        logger.info("WhisperSync running. Hotkeys:")
        logger.info(f"  Dictation: {self.cfg['hotkeys']['dictation_toggle']} (model: {dictation_model})")
        logger.info(f"  Meeting:   {self.cfg['hotkeys']['meeting_toggle']} (model: {self.cfg['model']})")
        logger.info(f"  Feature:   {self.cfg['hotkeys'].get('feature_suggest', 'ctrl+shift+alt+f')}")
        logger.info(f"  Left-click: {self.cfg.get('left_click', 'meeting')}")
        logger.info(f"  Middle-click: {self.cfg.get('middle_click', 'dictation')}")
        logger.info(f"Log file: {get_log_path()}")
        logger.info(f"CPU: {self._cpu_name}")
        if self.cfg.get("incognito"):
            logger.info("Whisper mode active - dictation data not stored on disk")
        if BackupTranscriber.is_enabled(self.cfg):
            backup_model = self.cfg.get("backup_model", "base")
            backup_device = self.cfg.get("backup_device", "cpu")
            logger.info(f"Always Available Dictation: on (backup model: {backup_model}, device: {backup_device})", extra={"secondary": True})
        logger.info("Right-click tray icon for menu.")

        # Startup toast notification
        compute = self.cfg.get("compute_type", "float16")
        notify(
            "WhisperSync running",
            f"Model: {dictation_model} | Compute: {compute}",
        )

        # Start transcription worker subprocess (loads models in isolation)
        self.worker.start()

        def _wait_worker():
            if self.worker.wait_ready(timeout=120):
                logger.info(f"Dictation model '{dictation_model}' ready (worker pid={self.worker._process.pid})")
                # Refresh menu now that worker has reported GPU name
                self._refresh_menu()
                # Recover any crashed dictations/features found at startup
                for wav_path in self._recovered_dictation_paths:
                    if Path(wav_path).name.startswith("feature_"):
                        self.dictation.recover_feature(wav_path)
                    else:
                        self.dictation.recover_dictation(wav_path)
                self._recovered_dictation_paths = []
                # Recover any crashed meetings — show dialog for naming
                self.meetings.recover_meetings()
            else:
                logger.warning("Transcription worker failed to start — dictation may not work")

        threading.Thread(target=_wait_worker, daemon=True).start()

        # Start GitHub PR status polling if configured
        self.github.start()

        # Periodic flush for persistent weekly stats
        self._stats_flush_stop = threading.Event()
        def _stats_flush_loop():
            while not self._stats_flush_stop.wait(weekly_stats._flush_interval):
                try:
                    weekly_stats.flush()
                except Exception:
                    pass
                # NOTE: do NOT call gc.collect() here. It is process-wide
                # and crashes (0x80000003) when any other thread is mid
                # native C call (PR #134 regression, removed in #135).
                # Cycle collection now happens via IdleCollector below,
                # which only collects when the app is provably quiescent.
        threading.Thread(target=_stats_flush_loop, daemon=True).start()

        # Provable-idle cycle collection. gc is disabled process-wide (see
        # main()); without periodic collection, reference cycles leak
        # permanently (menu rebuilds alone create closure-heavy graphs on
        # every refresh). The collector only runs when executors are idle,
        # zero native calls are in flight, the meeting pipeline is empty,
        # nothing is recording, and mode is terminal — the exact safety
        # condition the removed #134 checkpoints could not prove.
        from .idle_gc import IdleCollector
        self._idle_collector = IdleCollector(
            is_pipeline_idle=self.meetings.pipeline_idle,
            is_recording=lambda: (
                self.recorder.is_recording or self.dictation.overlay_active
            ),
            is_mode_terminal=lambda: (
                self.state is not None
                and self.state.current.mode in (None, "done", "error")
                and not self.state.current.meeting_transcribing
                and not self.state.current.dictation_overlay
            ),
        )
        self._idle_collector.start()

        # GPU guard: VRAM watermark polling + downgrade ladder (spec B1-B3).
        from .scheduler import scheduler as _sched
        self._gpu_guard.start(_sched, IO)

        # Suspend/resume awareness (hardware-resilience spec H1): both
        # transitions land in gpu-guard.jsonl for crash-time correlation,
        # and resume verifies the CUDA worker survived sleep, restarting
        # it off-thread if not. Callbacks run on an OS thread: keep them
        # to a log write + an IO offload.
        from .power_events import PowerEventListener

        def _on_suspend():
            self._gpu_guard.log_external_event(
                "system_suspend",
                recording=self.recorder.is_recording,
                transcribing=bool(self.state and self.state.current.meeting_transcribing),
            )

        def _on_resume():
            def _check():
                self._gpu_guard.log_external_event("system_resume")
                if not self.worker.is_alive():
                    logger.warning("Worker did not survive suspend/resume; restarting")
                    self.worker.restart()
                    notify("WhisperSync recovered",
                           "Transcription engine restarted after sleep.")
            submit_or_spawn(IO, "resume-check", _check)

        self._power_listener = PowerEventListener(
            on_suspend=_on_suspend, on_resume=_on_resume)
        self._power_listener.start()

        # Mic stall monitor (hardware-resilience H2): while recording, a
        # device that silently stops delivering buffers (USB unplug,
        # Bluetooth drop) previously produced an empty recording with no
        # warning until stop. Detection notifies immediately; the stream
        # keeps running in case the device returns (a mid-recording
        # reopen is deferred - see the hardening plan).
        _MIC_STALL_S = 5.0

        def _mic_stall_check():
            try:
                age = self.recorder.seconds_since_last_mic_buffer()
                if age is not None and age > _MIC_STALL_S:
                    if not getattr(self, "_mic_stall_notified", False):
                        self._mic_stall_notified = True
                        logger.warning(
                            f"Mic delivered no audio for {age:.0f}s while recording "
                            "(device lost?)"
                        )
                        notify("Microphone stopped",
                               "No audio is arriving from the mic. Check the device; "
                               "the recording is still open.")
                        self._gpu_guard.log_external_event("mic_stall", age_s=round(age, 1))
                else:
                    # Healthy delivery OR not recording (age None): both
                    # re-arm the notification for the next stall (review:
                    # a stall in one recording must not mute the next).
                    self._mic_stall_notified = False
            except Exception:
                logger.debug("mic stall check failed", exc_info=True)

        _sched.call_every(2.0, _mic_stall_check, label="mic-stall-check")

        try:
            self.tray.run()
        finally:
            # Unregister power notifications first: teardown must not
            # race an OS callback firing into half-shutdown state.
            try:
                self._power_listener.stop()
            except Exception:
                logger.debug("power listener stop failed", exc_info=True)
            # Flush persistent stats before shutdown
            try:
                self._stats_flush_stop.set()
                weekly_stats.flush()
            except Exception:
                pass
            # Always release keyboard hooks to prevent stuck modifier keys
            keyboard.unhook_all()
            self.worker.stop()
            self._backup.stop()
            self.github.stop()
            try:
                if getattr(self, "_idle_collector", None) is not None:
                    self._idle_collector.stop()
                from .scheduler import scheduler as _sched
                _sched.shutdown(timeout=1.0)
            except Exception:
                pass
            try:
                self._dialog_dispatcher.shutdown(timeout=2.0)
            except Exception:
                pass


def main():
    # Install faulthandler FIRST so native segfaults are persisted. The
    # helper retains the file handle module-scope so it cannot be GC'd.
    install_faulthandler(get_log_path())

    # Disable the CPython cycle collector process-wide. Refcounting still
    # cleans up ~99% of objects; only true reference cycles need this. The
    # cycle collector is what causes the recurring 0x80000003 / STATUS_BREAKPOINT
    # crashes: it can fire on ANY background thread mid-allocation inside a
    # native C extension, corrupting the heap. We've already had to patch
    # json.load (PR #131, #132) one site at a time as crashes landed in:
    #   - speakers.write_speaker_map (json.load)
    #   - flatten/_generate_minutes (json.load)
    #   - worker_manager._wait_response (multiprocessing.Queue.get)
    #   - pystray._build_menu (pystray MenuItem __init__)
    # The pattern is general, not call-specific. Disable auto-GC and run
    # gc.collect() at safe checkpoints (between meetings, on idle) instead.
    import gc
    gc.disable()
    logger.info("Cycle GC disabled; explicit gc.collect() at safe checkpoints")

    # Single-instance guard + orphan reaping (gpu-guard spec B4). Runs
    # before any model/worker spawn so a duplicate launch never doubles
    # VRAM load and a previous crash's worker never survives us starting.
    from .instance_guard import acquire_single_instance, reap_orphans
    if config.load().get("gpu_guard_single_instance", True):
        if not acquire_single_instance():
            logger.info("Another WhisperSync instance is already running; exiting")
            try:
                notify("WhisperSync already running", "This launch will exit; the existing tray instance keeps running.")
            except Exception:
                pass
            return
        orphans = reap_orphans()
        if orphans:
            logger.info(f"Reaped {len(orphans)} orphan worker process(es): {orphans}")

    heartbeat = Heartbeat(logger, interval=60.0)
    try:
        lifecycle.log_startup_banner(logger)
        lifecycle.install(logger)
        install_excepthook(logger)
        check_previous_crash(logger)
        heartbeat.start()
        app = WhisperSync()
        app.run()
    except SystemExit:
        lifecycle.record_exit_reason(lifecycle.REASON_SYSTEM_EXIT)
        raise
    except Exception:
        import traceback
        lifecycle.record_exit_reason(
            lifecycle.REASON_EXCEPTION,
            {"type": type(sys.exc_info()[1]).__name__},
        )
        logger.critical(f"FATAL CRASH:\n{traceback.format_exc()}")
        # Last-resort hook cleanup — prevents stuck Ctrl key on crash
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        raise
    finally:
        heartbeat.stop(timeout=1.0)


if __name__ == "__main__":
    main()
