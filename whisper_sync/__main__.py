"""WhisperSync entry point — tray icon + hotkey listener."""

import faulthandler
import logging
import os
import queue
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
import tempfile
from datetime import datetime
from pathlib import Path

import keyboard
import pystray

from . import config
from .capture import AudioRecorder, get_default_devices, get_host_apis, list_devices, save_wav, save_stereo_wav
from .icons import (idle_icon, build_icon, resolve_icon_key, ICON_REGISTRY,
                     IconAnimator)
from .logger import logger, get_log_path, set_console_level, log_dictation_result, log_meeting_result, log_transcript_preview
from .model_status import get_model_status, download_model, bootstrap_models
from .paste import paste
from .paths import (get_install_root, get_default_output_dir,
                     get_data_dir, get_dictation_log_dir,
                     get_legacy_config_path, get_legacy_speaker_config_path,
                     get_legacy_dictation_log_dir, get_config_path as get_data_config_path,
                     get_speaker_config_path)
from .worker_manager import TranscriptionWorker, WorkerCrashedError
from .backup_worker import BackupTranscriber
from . import dictation_log
from . import feature_log
from . import weekly_stats
from .streaming_wav import fix_orphan
from .crash_diagnostics import install_excepthook, check_previous_crash
from .flatten import flatten as flatten_transcript
from .notifications import notify, ToastListener
from .state_manager import (
    StateManager, AppState,
    MEETING_STARTED, MEETING_STOPPED, MEETING_COMPLETED,
    DICTATION_STARTED, DICTATION_COMPLETED, DICTATION_DISCARDED,
    TRANSCRIPTION_STARTED, TRANSCRIPTION_PROGRESS, TRANSCRIPTION_COMPLETED,
    ERROR, MODEL_LOADING, MODEL_READY, MODEL_DOWNLOADING,
    PR_STATUS_CHANGED, SPEAKER_HEALTH_CHANGED, QUEUED, IDLE,
)
from .meeting_job import MeetingJob
from .rebuild_index import rebuild_root_index
from .speakers import identify_speakers, write_speaker_map, update_config, get_config_path

HOTKEY_OPTIONS = [
    "ctrl+shift+space",
    "ctrl+alt+space",
    "ctrl+shift+d",
    "ctrl+alt+d",
    "ctrl+shift+r",
    "ctrl+alt+r",
    "ctrl+shift+m",
    "ctrl+alt+m",
    "ctrl+shift+t",
    "ctrl+alt+t",
]

FEATURE_HOTKEY_OPTIONS = [
    "ctrl+shift+alt+f",
    "ctrl+shift+alt+s",
    "ctrl+shift+alt+r",
    "ctrl+alt+f",
]

PASTE_OPTIONS = ["clipboard", "keystrokes"]

MODEL_OPTIONS = {
    "tiny": "~75 MB",
    "base": "~150 MB",
    "small": "~500 MB",
    "medium": "~1.5 GB",
    "large-v2": "~3 GB",
    "large-v3": "~3 GB",
}

CLICK_ACTIONS = {
    "meeting": "Toggle Meeting",
    "dictation": "Toggle Dictation",
    "none": "None",
}


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
        self.cfg = config.load()
        set_console_level(self.cfg.get("log_window", "normal"))
        self.recorder = AudioRecorder(sample_rate=self.cfg["sample_rate"])
        self.tray = None
        self.state = None  # Initialized after tray creation in run()
        self._lock = threading.RLock()
        self._tray_lock = threading.Lock()  # Serialize all tray icon/title updates (pystray not thread-safe)
        self._api_filter = "Windows WASAPI"  # None = show all
        self._dictation_wav_path: Path | None = None
        self._meeting_start_time: datetime | None = None
        dictation_model = self.cfg.get("dictation_model", self.cfg["model"])
        self.worker = TranscriptionWorker(self.cfg, preload_model=dictation_model)
        self._backup = BackupTranscriber(self.cfg)
        self._overlay_recorder = None    # Separate AudioRecorder for dictation during meetings
        self._github_poller = None
        self._github_prs = []
        self._dictation_history = dictation_log.load_recent(10)  # persist across restarts
        self._feature_suggest_active = False  # routes dictation output to feature log
        self._post_queue = queue.Queue()  # Post-processing queue for completed meetings
        self._post_worker_thread = None  # Started in run()
        self._cpu_name = _get_cpu_name()
        # Session stats
        self._stats = {
            "dictations": 0,
            "meetings": 0,
            "feature_suggestions": 0,
            "total_dictation_chars": 0,
            "total_dictation_time": 0.0,
            "total_meeting_seconds": 0,
            "total_meeting_words": 0,
            "session_start": datetime.now(),
        }

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

    def _update_tray(self, icon=None, title=None):
        """Thread-safe tray icon/title update. Serializes all pystray mutations."""
        with self._tray_lock:
            if self.tray is None:
                return
            if icon is not None:
                self.tray.icon = icon
            if title is not None:
                self.tray.title = title

    def _yellow_flash(self):
        """Universal loading/queuing signal: two quick yellow flashes (150ms on/off/on)."""
        if getattr(self, '_flashing', False):
            return
        self._flashing = True
        animator = IconAnimator(self.tray, lock=self._tray_lock)
        animator.flash(count=2, interval_ms=150)
        # Reset flag after animation completes (~600ms)
        import time
        def _reset():
            time.sleep(0.7)
            self._flashing = False
        threading.Thread(target=_reset, daemon=True).start()

    # --- Click dispatch ---

    def _dispatch_action(self, action: str):
        if action == "meeting":
            self.toggle_meeting()
        elif action == "dictation":
            self.toggle_dictation()

    def _on_left_click(self):
        # Left-click while dictating = discard (stop recording, throw away audio)
        current = self.state.current if self.state else None
        mode = current.mode if current else None
        overlay = current.dictation_overlay if current else False
        if mode == "dictation" or overlay:
            self._discard_dictation()
            return
        self._dispatch_action(self.cfg.get("left_click", "meeting"))

    def _on_middle_click(self):
        self._dispatch_action(self.cfg.get("middle_click", "dictation"))

    # --- Recording modes ---

    def _schedule_idle(self, seconds: float, blink: bool = False):
        """Return to idle after a delay. If blink=True, blink done 3 times first.

        Only resets mode if it's still in a terminal state (done/error/None).
        If the user started a new recording during the delay, the mode will be
        'dictation' or 'meeting' and we must NOT overwrite it.
        """
        import time

        def _reset():
            if blink and self.state.current.mode == "done":
                for _ in range(3):
                    if self.state.current.mode not in ("done", None):
                        return  # User started something new - abort blink
                    self.state.emit(IDLE, mode="done")
                    time.sleep(0.4)
                    if self.state.current.mode not in ("done", None):
                        return
                    self.state.emit(IDLE, mode=None)
                    time.sleep(0.3)
            else:
                time.sleep(seconds)
            # Only reset to idle if mode is still in a terminal state
            if self.state.current.mode in ("done", "error", None):
                self.state.emit(IDLE, mode=None)
            else:
                logger.debug(f"_schedule_idle: skipped reset - mode is '{self.state.current.mode}' (not terminal)")

        threading.Thread(target=_reset, daemon=True).start()

    @staticmethod
    def _safe_unlink(path: Path, retries: int = 2, delay: float = 0.5):
        """Delete a file, retrying on PermissionError (Windows file locking)."""
        import time
        for attempt in range(retries + 1):
            try:
                if path and path.exists():
                    path.unlink(missing_ok=True)
                return
            except PermissionError:
                if attempt < retries:
                    time.sleep(delay)
                else:
                    logger.debug(f"Could not delete {path} — will clean up on next restart")

    def _flash_queued(self):
        """Rapid amber flash to indicate dictation is queued behind a meeting stage."""
        animator = IconAnimator(self.tray, lock=self._tray_lock)
        animator.flash_between("queued", "transcribing", count=2, interval_ms=150)

    def _can_record(self) -> bool:
        """Can we start a new recording? Allowed if idle or just transcribing in background."""
        mode = self.state.current.mode if self.state else None
        return mode is None or mode in ("transcribing", "done", "error")

    def toggle_dictation(self):
        with self._lock:
            current = self.state.current if self.state else None
            mode = current.mode if current else None
            overlay = current.dictation_overlay if current else False
            meeting_tx = current.meeting_transcribing if current else False

            # Handle overlay dictation during meetings
            if overlay:
                self._stop_overlay_dictation()
                return

            if mode == "dictation":
                self._stop_dictation()
            elif mode == "meeting" or (mode is None and meeting_tx):
                # Dictation during meeting recording or meeting transcription
                if self.cfg.get("always_available_dictation", True):
                    if self._backup.is_loading:
                        logger.debug("Backup model still loading, triggering yellow flash", extra={"secondary": True})
                        self._yellow_flash()
                        return
                    self._start_overlay_dictation()
                else:
                    logger.info("Dictation unavailable during meeting (always_available_dictation disabled)", extra={"secondary": True})
                    notify("Dictation unavailable", "Enable always-available dictation in settings")
            elif mode == "saving":
                logger.debug("Dictation ignored - meeting is saving")
            elif self._can_record():
                self._start_dictation()

    def toggle_feature_suggest(self):
        """Toggle feature suggestion recording (same as dictation but saves to feature log)."""
        with self._lock:
            current = self.state.current if self.state else None
            mode = current.mode if current else None
            overlay = current.dictation_overlay if current else False
            meeting_tx = current.meeting_transcribing if current else False

            # If already recording a feature suggestion, stop it
            if overlay and self._feature_suggest_active:
                self._stop_overlay_dictation()
                return
            if mode == "dictation" and self._feature_suggest_active:
                self._stop_dictation()
                return

            # Start feature suggestion (reuses dictation pipeline)
            if mode == "dictation" and not self._feature_suggest_active:
                # Already recording a normal dictation - ignore
                logger.debug("Feature suggest ignored - dictation in progress")
                return

            self._feature_suggest_active = True
            if mode == "meeting" or (mode is None and meeting_tx):
                if self.cfg.get("always_available_dictation", True):
                    if self._backup.is_loading:
                        self._feature_suggest_active = False
                        logger.debug("Backup model still loading, triggering yellow flash", extra={"secondary": True})
                        self._yellow_flash()
                        return
                    self._start_overlay_dictation()
                else:
                    self._feature_suggest_active = False
                    logger.info("Feature suggest unavailable during meeting (always_available_dictation disabled)", extra={"secondary": True})
                    notify("Feature suggest unavailable", "Enable always-available dictation in settings")
            elif mode == "saving":
                self._feature_suggest_active = False
                logger.debug("Feature suggest ignored - meeting is saving")
            elif self._can_record():
                self._start_dictation()
            else:
                self._feature_suggest_active = False

    def _dictation_log_dir(self) -> Path:
        return get_dictation_log_dir()

    def _start_dictation(self):
        _meeting_tx = self.state.current.meeting_transcribing if self.state else False
        if not self.worker.is_ready() and not (_meeting_tx and BackupTranscriber.is_enabled(self.cfg)):
            logger.warning("Worker not ready yet - ignoring dictation request")
            self._feature_suggest_active = False
            self._yellow_flash()
            return
        self.state.emit(DICTATION_STARTED, mode="dictation")
        mic = self.cfg.get("mic_device")
        if self.cfg.get("use_system_devices", True):
            mic = None
        self.recorder.start(mic_device=mic)
        # Stream to disk for crash recovery -- skip when incognito (RAM only)
        if self.cfg.get("incognito", False):
            self._dictation_wav_path = None
        else:
            log_dir = self._dictation_log_dir()
            log_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            prefix = "feature_" if self._feature_suggest_active else ""
            self._dictation_wav_path = log_dir / f"{prefix}{ts}.wav"
            self.recorder.start_streaming(self._dictation_wav_path)

    def _stop_dictation(self):
        audio = self.recorder.stop()
        self.recorder.stop_streaming()

        if "mic" not in audio:
            self._feature_suggest_active = False
            self.state.emit(IDLE, mode=None)
            return

        self.state.emit(TRANSCRIPTION_STARTED, mode="transcribing")

        dictation_model = self.cfg.get("dictation_model", self.cfg["model"])
        use_backup = self.state.current.meeting_transcribing and BackupTranscriber.is_enabled(self.cfg)
        # Capture feature flag under lock before spawning background thread
        with self._lock:
            is_feature = self._feature_suggest_active
            self._feature_suggest_active = False

        def _process():
            import time as _time

            t0 = _time.perf_counter()
            try:
                text = None
                used_backup = False
                if use_backup:
                    # Meeting is transcribing - use lightweight backup model
                    # instead of queuing on the busy worker
                    try:
                        text = self._backup.transcribe(audio["mic"])
                        used_backup = True
                        t1 = _time.perf_counter()
                        backup_model = self.cfg.get("backup_model", "base")
                        backup_device = self.cfg.get("backup_device", "cpu")
                        logger.info(
                            f"Dictation (backup, {backup_device} {backup_model}): "
                            f"{t1 - t0:.2f}s",
                            extra={"secondary": True},
                        )
                    except Exception as backup_err:
                        # Backup failed - fall back to queuing on the main worker
                        logger.warning(f"Backup transcriber failed: {backup_err}", extra={"secondary": True})
                        logger.info("Falling back to main worker (queued)", extra={"secondary": True})
                        self._flash_queued()
                        timeout = 180
                        text = self.worker.transcribe_fast(audio["mic"], model_override=dictation_model, timeout=timeout)
                        t1 = _time.perf_counter()
                        logger.debug(f"transcribe_fast (fallback): {t1 - t0:.2f}s")
                else:
                    # Normal path or backup disabled
                    if self.state.current.meeting_transcribing:
                        self._flash_queued()
                    timeout = 180 if self.state.current.meeting_transcribing else 60
                    text = self.worker.transcribe_fast(audio["mic"], model_override=dictation_model, timeout=timeout)
                    t1 = _time.perf_counter()
                    logger.debug(f"transcribe_fast: {t1 - t0:.2f}s")
                t2 = _time.perf_counter()
                char_count = len(text) if text else 0

                if is_feature:
                    # Feature suggestion mode
                    if not text:
                        logger.info("Feature suggestion discarded (no speech detected)")
                    elif self.cfg.get("incognito", False):
                        logger.info("Feature suggestion skipped: whisper mode enabled")
                        notify("Feature not saved", "Whisper mode is on; suggestions are not stored on disk")
                    else:
                        entry_id = feature_log.append_raw(text, t2 - t0)
                        logger.info(f"Feature suggestion saved: {char_count} chars in {t2 - t0:.2f}s")
                        notify("Feature saved", f"Suggestion recorded ({char_count} chars)")
                        self._stats["feature_suggestions"] += 1
                        weekly_stats.record_feature_suggestion()
                        # Format asynchronously via Claude CLI
                        threading.Thread(
                            target=self._format_feature_async,
                            args=(text, entry_id),
                            daemon=True,
                        ).start()
                else:
                    # Normal dictation mode: paste + log
                    if text:
                        paste(text, self.cfg["paste_method"], restore=not self.cfg.get("incognito", False))
                    delivery = "pasted" if self.cfg["paste_method"] == "keystrokes" else "clipboard"
                    if self.cfg.get("incognito"):
                        logger.info(f"Dictation: {t2 - t0:.2f}s -- {delivery} ({char_count} chars)")
                    else:
                        log_dictation_result(text or "", t2 - t0, delivery, char_count, secondary=used_backup)
                    effective_model = self.cfg.get("backup_model", "base") if used_backup else dictation_model
                    logger.debug(f"total (stop -> paste): {t2 - t0:.2f}s, model={effective_model}{' (backup)' if used_backup else ''}")
                    # Update session stats
                    self._stats["dictations"] += 1
                    self._stats["total_dictation_chars"] += char_count
                    self._stats["total_dictation_time"] += t2 - t0
                    weekly_stats.record_dictation(char_count, t2 - t0)
                    incognito = self.cfg.get("incognito", False)
                    if text and not incognito:
                        dictation_log.append(text, t2 - t0, model=effective_model)
                        self._dictation_history.append({
                            "text": text,
                            "timestamp": datetime.now().strftime("%H:%M"),
                            "chars": len(text),
                        })
                        if len(self._dictation_history) > 10:
                            self._dictation_history = self._dictation_history[-10:]
                        self._refresh_menu()
                # Success -- remove crash-safety WAV (text is in the log)
                self.recorder.stop_streaming()  # defensive: ensure writer closed
                if self._dictation_wav_path:
                    self._safe_unlink(self._dictation_wav_path)
                self.state.emit(DICTATION_COMPLETED, mode="done")
            except WorkerCrashedError:
                logger.error("Worker crashed during dictation -- respawning...")
                if self._dictation_wav_path:
                    logger.info(f"Dictation audio preserved at: {self._dictation_wav_path}")
                self.worker.restart()
                self.state.emit(ERROR, mode="error", data={"message": "Worker crashed during dictation", "recoverable": True})
            except Exception as e:
                logger.error(f"Dictation error: {e}")
                import traceback
                logger.debug(traceback.format_exc())
                self.state.emit(ERROR, mode="error", data={"message": str(e), "recoverable": False})
            finally:
                self._schedule_idle(2)

        threading.Thread(target=_process, daemon=True).start()

    # --- Overlay dictation (dictation during meeting recording/transcription) ---

    def _start_overlay_dictation(self):
        """Start dictation using a separate audio stream while meeting continues.

        Uses an independent AudioRecorder so the meeting recording is never
        interrupted. Transcription uses the backup model (CPU or secondary GPU).
        """
        # Note: always_available_dictation config flag already gates the call to
        # this method in toggle_dictation(), so no need to re-check is_enabled() here.

        meeting_state = "recording" if (self.state.current.mode if self.state else None) == "meeting" else "transcribing"
        backup_model = self.cfg.get("backup_model", "base")
        backup_device = self.cfg.get("backup_device", "cpu")
        logger.info(f"Dictation requested during meeting {meeting_state} (using backup model)", extra={"secondary": True})
        logger.info(f"Backup model: {backup_model} on {backup_device}", extra={"secondary": True})

        # Create a separate recorder for dictation audio (mic only)
        self._overlay_recorder = AudioRecorder(sample_rate=self.cfg["sample_rate"])
        mic = self.cfg.get("mic_device")
        if self.cfg.get("use_system_devices", True):
            mic = None
        self._overlay_recorder.start(mic_device=mic)
        logger.info("Dictation during meeting: recording started", extra={"secondary": True})
        self.state.emit(DICTATION_STARTED, dictation_overlay=True)

    def _stop_overlay_dictation(self):
        """Stop overlay dictation, transcribe with backup model, paste result."""
        if not self._overlay_recorder:
            self.state.emit(IDLE, dictation_overlay=False)
            return

        audio = self._overlay_recorder.stop()
        self.state.emit(DICTATION_COMPLETED, dictation_overlay=False)

        if "mic" not in audio:
            logger.debug("Overlay dictation stopped - no audio captured", extra={"secondary": True})
            self._overlay_recorder = None
            self._feature_suggest_active = False
            return

        overlay_audio = audio["mic"]
        self._overlay_recorder = None

        # Capture feature flag before spawning background thread
        with self._lock:
            is_feature = self._feature_suggest_active
            self._feature_suggest_active = False

        logger.info("Dictation during meeting: transcribing...", extra={"secondary": True})

        def _process_overlay():
            import time as _time

            t0 = _time.perf_counter()
            try:
                text = self._backup.transcribe(overlay_audio)
                t1 = _time.perf_counter()
                duration = t1 - t0
                char_count = len(text) if text else 0
                backup_model = self.cfg.get("backup_model", "base")
                backup_device = self.cfg.get("backup_device", "cpu")
                logger.info(
                    f"Dictation during meeting: {duration:.1f}s, {char_count} chars "
                    f"(backup {backup_device} {backup_model})",
                    extra={"secondary": True},
                )
                if is_feature:
                    if not text:
                        logger.info("Feature suggestion (overlay) discarded (no speech detected)", extra={"secondary": True})
                    elif self.cfg.get("incognito", False):
                        logger.info("Feature suggestion skipped: whisper mode enabled", extra={"secondary": True})
                        notify("Feature not saved", "Whisper mode is on; suggestions are not stored on disk")
                    else:
                        entry_id = feature_log.append_raw(text, duration)
                        logger.info(f"Feature suggestion (overlay) saved: {char_count} chars in {duration:.2f}s", extra={"secondary": True})
                        notify("Feature saved", f"Suggestion recorded ({char_count} chars)")
                        self._stats["feature_suggestions"] += 1
                        weekly_stats.record_feature_suggestion()
                        threading.Thread(
                            target=self._format_feature_async,
                            args=(text, entry_id),
                            daemon=True,
                        ).start()
                else:
                    if text:
                        paste(text, self.cfg["paste_method"], restore=not self.cfg.get("incognito", False))

                    # Update session stats
                    self._stats["dictations"] += 1
                    self._stats["total_dictation_chars"] += char_count
                    self._stats["total_dictation_time"] += duration
                    weekly_stats.record_dictation(char_count, duration)

                    incognito = self.cfg.get("incognito", False)
                    if text and not incognito:
                        dictation_log.append(text, duration, model=backup_model)
                        self._dictation_history.append({
                            "text": text,
                            "timestamp": datetime.now().strftime("%H:%M"),
                            "chars": len(text),
                        })
                        if len(self._dictation_history) > 10:
                            self._dictation_history = self._dictation_history[-10:]
                        self._refresh_menu()

                    delivery = "pasted" if self.cfg["paste_method"] == "keystrokes" else "clipboard"
                    if incognito:
                        logger.info(f"Overlay dictation: {duration:.2f}s - {delivery} ({char_count} chars)", extra={"secondary": True})
                    else:
                        log_dictation_result(text or "", duration, delivery, char_count, secondary=True)

            except Exception as e:
                logger.error(f"Overlay dictation error: {e}", extra={"secondary": True})
                import traceback
                logger.debug(traceback.format_exc())
                # Fall back to queuing on main worker if backup fails
                try:
                    logger.info("Falling back to main worker for overlay dictation", extra={"secondary": True})
                    dictation_model = self.cfg.get("dictation_model", self.cfg["model"])
                    timeout = 180
                    text = self.worker.transcribe_fast(overlay_audio, model_override=dictation_model, timeout=timeout)
                    if text:
                        paste(text, self.cfg["paste_method"], restore=not self.cfg.get("incognito", False))
                    t1 = _time.perf_counter()
                    duration = t1 - t0
                    char_count = len(text or "")
                    logger.info(f"Overlay dictation fallback: {duration:.2f}s, {char_count} chars", extra={"secondary": True})

                    # Post-dictation bookkeeping (same as the normal overlay path)
                    self._stats["dictations"] += 1
                    self._stats["total_dictation_chars"] += char_count
                    self._stats["total_dictation_time"] += duration
                    weekly_stats.record_dictation(char_count, duration)

                    incognito = self.cfg.get("incognito", False)
                    if text and not incognito:
                        dictation_log.append(text, duration, model=dictation_model)
                        self._dictation_history.append({
                            "text": text,
                            "timestamp": datetime.now().strftime("%H:%M"),
                            "chars": len(text),
                        })
                        if len(self._dictation_history) > 10:
                            self._dictation_history = self._dictation_history[-10:]
                        self._refresh_menu()

                    delivery = "pasted" if self.cfg["paste_method"] == "keystrokes" else "clipboard"
                    if incognito:
                        logger.info(f"Overlay dictation fallback: {duration:.2f}s - {delivery} ({char_count} chars)", extra={"secondary": True})
                    else:
                        log_dictation_result(text or "", duration, delivery, char_count, secondary=True)
                except Exception as fallback_err:
                    logger.error(f"Overlay dictation fallback also failed: {fallback_err}", extra={"secondary": True})

        threading.Thread(target=_process_overlay, daemon=True).start()

    def _recover_dictation(self, wav_path: str):
        """Transcribe a recovered dictation WAV from a previous crash.

        Puts the text on the clipboard (not auto-paste — wrong window may be focused
        after a restart). The user can Ctrl+V when ready.
        """
        import pyperclip
        logger.info(f"Recovering crashed dictation from: {wav_path}")
        try:
            import numpy as np
            import wave
            with wave.open(wav_path, "r") as wf:
                frames = wf.readframes(wf.getnframes())
                audio_np = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32767.0

            dictation_model = self.cfg.get("dictation_model", self.cfg["model"])
            text = self.worker.transcribe_fast(audio_np, model_override=dictation_model)
            if text:
                pyperclip.copy(text)
                dictation_log.append(text, 0, model=dictation_model)
                logger.info(f"Crash-recovered dictation copied to clipboard: {text[:80]}...")
                # #38: Toast with recovered text info and Copy button
                try:
                    _recovered_text = text
                    def _copy_recovered(t=_recovered_text):
                        import pyperclip as _pc
                        _pc.copy(t)
                    notify(
                        "Dictation recovered",
                        f"{len(text)} chars recovered from crash",
                        buttons=[{"label": "Copy to Clipboard", "action": _copy_recovered}],
                    )
                except Exception:
                    logger.debug("Recovery toast failed", exc_info=True)
            else:
                logger.info("Crash-recovered dictation produced no text")
            # Clean up the WAV now that text is on clipboard + in the .md log
            Path(wav_path).unlink(missing_ok=True)
        except Exception as e:
            logger.error(f"Failed to recover dictation: {e}")
            logger.info(f"Audio preserved at: {wav_path}")

    def _recover_feature(self, wav_path: str):
        """Recover a crashed feature suggestion with interactive notification.

        Unlike dictation recovery (clipboard only), feature recovery routes
        the transcription to the feature log and applies Claude formatting.
        Shows a toast with Recover / Cancel options.
        """
        logger.info(f"Recovering crashed feature suggestion from: {wav_path}")
        try:
            import numpy as np
            import wave
            with wave.open(wav_path, "r") as wf:
                frames = wf.readframes(wf.getnframes())
                audio_np = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32767.0

            dictation_model = self.cfg.get("dictation_model", self.cfg["model"])
            text = self.worker.transcribe_fast(audio_np, model_override=dictation_model)
            if not text:
                logger.info("Crashed feature suggestion produced no text, cleaning up")
                Path(wav_path).unlink(missing_ok=True)
                return

            logger.info(f"Recovered feature text ({len(text)} chars): {text[:80]}...")

            # Show interactive notification with Recover / Cancel options
            def _do_recover():
                entry_id = feature_log.append_raw(text, 0)
                logger.info(f"Feature suggestion recovered to feature log: {entry_id}")
                # Format via Claude CLI in background
                threading.Thread(
                    target=self._format_feature_async,
                    args=(text, entry_id),
                    daemon=True,
                ).start()
                notify("Feature recovered", f"{len(text)} chars saved to feature log")

            def _do_cancel():
                logger.info("Feature recovery cancelled by user")

            try:
                preview = f'"{text[:120]}..."' if len(text) > 120 else f'"{text}"'
                notify(
                    "Recover feature suggestion?",
                    preview,
                    buttons=[
                        {"label": "Recover", "action": _do_recover},
                        {"label": "Cancel", "action": _do_cancel},
                    ],
                )
            except Exception:
                # If notification fails, auto-recover silently
                logger.debug("Feature recovery notification failed, auto-recovering", exc_info=True)
                _do_recover()

            # Clean up WAV after showing notification (actions handle the rest)
            Path(wav_path).unlink(missing_ok=True)
        except Exception as e:
            logger.error(f"Failed to recover feature suggestion: {e}")
            logger.info(f"Audio preserved at: {wav_path}")

    def _recover_meetings(self):
        """Show a dialog for each recovered meeting WAV, let user name and place it."""
        for wav_path, duration in self._recovered_meeting_paths:
            mins = int(duration // 60)
            secs = int(duration % 60)
            name = self._ask_recovery_name(wav_path, f"{mins}m {secs}s")
            if name is self._ABORT:
                logger.info(f"Recovery skipped for {wav_path} — file preserved")
                continue
            # Move to local-transcriptions (month-based, MMDD_HHMM naming)
            from datetime import datetime as _dt, timedelta as _td
            mtime = _dt.fromtimestamp(Path(wav_path).stat().st_mtime)
            # Estimate start time from WAV duration
            try:
                import wave as _wave
                with _wave.open(wav_path, 'rb') as _wf:
                    _dur = _wf.getnframes() / _wf.getframerate()
                start = mtime - _td(seconds=_dur)
            except Exception:
                start = mtime
            week_dir = f"{start.strftime('%m')}-w{(start.day - 1) // 7 + 1}"
            date_time_str = start.strftime("%m%d_%H%M")
            folder_name = f"{date_time_str}_{name}" if name else f"{date_time_str}_recovered-meeting"
            meeting_dir = self._output_dir() / week_dir / folder_name
            meeting_dir.mkdir(parents=True, exist_ok=True)
            dest = meeting_dir / "recording.wav"
            Path(wav_path).rename(dest)
            logger.info(f"Recovered meeting moved to: {dest}")
            # Transcribe in background
            def _transcribe(path=str(dest)):
                try:
                    self.state.emit(TRANSCRIPTION_STARTED, meeting_transcribing=True)
                    result = self.worker.transcribe(path, diarize=True)
                    logger.info(f"Recovery transcript saved: {result.get('json_path', path)}")
                    try:
                        json_path = result.get('json_path')
                        if json_path:
                            flatten_transcript(json_path)
                    except Exception:
                        pass  # Non-fatal for recovery
                except Exception as e:
                    logger.error(f"Recovery transcription failed: {e}")
                    logger.info(f"Audio preserved at: {path}")
                finally:
                    if self.state.current.mode is None:
                        self.state.emit(MEETING_COMPLETED, meeting_transcribing=False, mode="done")
                        self._schedule_idle(3, blink=True)
                    else:
                        self.state.emit(IDLE, meeting_transcribing=False)
            threading.Thread(target=_transcribe, daemon=True).start()
        self._recovered_meeting_paths = []

    def __init_recovery_guard(self):
        """Initialize recovery guard set (called from __init__ or lazily)."""
        if not hasattr(self, "_recovering_meetings"):
            self._recovering_meetings: set[str] = set()

    def _recover_meeting_speakers(self, meeting_dir: Path):
        """Re-enter the speaker ID flow for a past meeting."""
        import json as _json
        from .speakers import identify_speakers, write_speaker_map, update_config, get_config_path, build_manual_stub
        from .flatten import flatten as flatten_transcript

        json_path = meeting_dir / "transcript.json"
        if not json_path.exists():
            logger.warning(f"No transcript.json in {meeting_dir}")
            return

        # Prevent duplicate clicks on the same meeting
        self.__init_recovery_guard()
        meeting_key = str(meeting_dir)
        if meeting_key in self._recovering_meetings:
            logger.info(f"Recovery already in progress for {meeting_dir.name}")
            return
        self._recovering_meetings.add(meeting_key)

        def _run():
            try:
                # Immediate feedback
                try:
                    from .notifications import notify
                    notify("Identifying speakers", f"Processing {meeting_dir.name}...")
                except Exception:
                    pass
                logger.info(f"Recovery: identifying speakers for {meeting_dir.name}")

                cfg_path = get_config_path()

                # Try Claude identification first
                id_result = None
                if self._is_claude_cli_available():
                    try:
                        id_result = identify_speakers(
                            str(json_path), cfg_path, meeting_dir.name
                        )
                    except Exception as e:
                        logger.warning(f"Speaker ID failed for recovery: {e}")

                # Fall back to manual stub
                if not id_result or not id_result.get("speaker_map"):
                    id_result = build_manual_stub(str(json_path))
                    if not id_result:
                        return

                if not id_result or not id_result.get("speaker_map"):
                    logger.warning("No speakers to identify")
                    return

                confirmed_map = self._ask_speaker_confirmation(id_result)
                if not confirmed_map:
                    logger.info("Recovery: speaker identification skipped by user")
                    return

                # Write speaker map
                write_speaker_map(str(json_path), confirmed_map)
                update_config(cfg_path, confirmed_map, id_result.get("config_updates"))
                logger.info(f"Recovery: speakers confirmed for {meeting_dir.name}: {confirmed_map}")

                # Re-flatten
                try:
                    flatten_transcript(str(json_path))
                    logger.info(f"Recovery: re-flattened {meeting_dir.name}")
                except Exception as e:
                    logger.warning(f"Recovery: flatten failed: {e}")

                # Regenerate minutes with updated speaker names
                readable_file = meeting_dir / "transcript-readable.txt"
                minutes_file = meeting_dir / "minutes.md"
                if readable_file.exists() and self._is_claude_cli_available():
                    try:
                        from .notifications import notify
                        notify(
                            f"Speakers updated: {meeting_dir.name}",
                            "Regenerating minutes with updated speaker names.",
                        )
                    except Exception:
                        pass
                    try:
                        self._generate_minutes(meeting_dir, readable_file, minutes_file)
                        logger.info(f"Recovery: minutes regenerated for {meeting_dir.name}")
                    except Exception as e:
                        logger.warning(f"Recovery: minutes generation failed: {e}")

                self._refresh_menu()

            finally:
                self._recovering_meetings.discard(meeting_key)

        threading.Thread(target=_run, daemon=True).start()

    def _ask_recovery_name(self, wav_path: str, duration_str: str):
        """Show a dialog to name a recovered meeting. Returns name string or _ABORT."""
        result = [self._ABORT]
        event = threading.Event()

        def _show():
            import tkinter as tk
            root = tk.Tk()
            root.title("WhisperSync: Recovered Meeting")
            root.attributes("-topmost", True)
            root.geometry("420x180")
            root.resizable(False, False)

            tk.Label(
                root,
                text=f"Recovered {duration_str} of meeting audio from a crash.",
                wraplength=380,
            ).pack(pady=(12, 4))
            tk.Label(root, text="Meeting name:").pack(pady=(4, 2))
            entry = tk.Entry(root, width=45)
            entry.pack(pady=4)
            entry.focus_force()

            btn_frame = tk.Frame(root)
            btn_frame.pack(pady=8)

            def _submit(event=None):
                result[0] = entry.get()
                root.destroy()

            def _skip():
                result[0] = self._ABORT
                root.destroy()

            entry.bind("<Return>", _submit)
            entry.bind("<Escape>", lambda e: _skip())
            tk.Button(btn_frame, text="Save & Transcribe", command=_submit, width=16).pack(side=tk.LEFT, padx=4)
            tk.Button(btn_frame, text="Skip", command=_skip, width=8).pack(side=tk.LEFT, padx=4)

            root.update_idletasks()
            x = (root.winfo_screenwidth() - root.winfo_reqwidth()) // 2
            y = (root.winfo_screenheight() - root.winfo_reqheight()) // 2
            root.geometry(f"+{x}+{y}")

            root.protocol("WM_DELETE_WINDOW", _skip)
            root.mainloop()
            event.set()

        t = threading.Thread(target=_show, daemon=True)
        t.start()
        event.wait(timeout=120)

        if result[0] is self._ABORT:
            return self._ABORT

        name = result[0] or ""
        return "".join(c if c.isalnum() or c in " -_" else "" for c in name).strip().replace(" ", "-")

    def _discard_dictation(self):
        """Discard current dictation - stop recording, throw away audio, return to idle."""
        with self._lock:
            # Handle overlay dictation discard
            _overlay = self.state.current.dictation_overlay if self.state else False
            if _overlay and self._overlay_recorder:
                self._overlay_recorder.stop()
                self._overlay_recorder = None
                logger.info("Overlay dictation discarded (left-click)", extra={"secondary": True})
                self.state.emit(DICTATION_DISCARDED, dictation_overlay=False)
                return

            if (self.state.current.mode if self.state else None) != "dictation":
                return
            self.recorder.stop()  # stop recording, discard the audio
            self.recorder.stop_streaming()
            if self._dictation_wav_path and self._dictation_wav_path.exists():
                self._dictation_wav_path.unlink(missing_ok=True)
            logger.info("Dictation discarded (left-click)")
            self.state.emit(DICTATION_DISCARDED, mode=None)

    def toggle_meeting(self):
        with self._lock:
            mode = self.state.current.mode if self.state else None
            if mode == "meeting":
                self._stop_meeting()
            elif self._can_record():
                self._start_meeting()

    def _start_meeting(self):
        self.state.emit(MEETING_STARTED, mode="meeting")
        self._meeting_start_time = datetime.now()
        mic = self.cfg.get("mic_device")
        speaker = self.cfg.get("speaker_device")
        if self.cfg.get("use_system_devices", True):
            mic = None
            defaults = get_default_devices()
            speaker = defaults["output"]
        elif speaker is None:
            defaults = get_default_devices()
            speaker = defaults["output"]
        self.recorder.start(mic_device=mic, speaker_device=speaker)
        if self.recorder.speaker_loopback_active:
            logger.info("Meeting started: mic + speaker loopback")
        else:
            logger.warning("Meeting started: mic only (speaker loopback unavailable)")
        temp = self._meeting_temp_dir()
        mic_temp = temp / "mic-temp.wav"
        self.recorder.start_streaming(mic_temp, disk_only=True)
        if self.cfg.get("always_available_dictation", True):
            self._backup.preload()

    _ABORT = object()  # Sentinel for abort

    @staticmethod
    def _sanitize_name(name: str) -> str:
        """Sanitize a meeting name for use as a folder name."""
        return "".join(c if c.isalnum() or c in " -_" else "" for c in name).strip().replace(" ", "-")

    @staticmethod
    def _center_window(root):
        """Center a tkinter window on screen."""
        root.update_idletasks()
        x = (root.winfo_screenwidth() - root.winfo_reqwidth()) // 2
        y = (root.winfo_screenheight() - root.winfo_reqheight()) // 2
        root.geometry(f"+{x}+{y}")

    @staticmethod
    def _style_window(root):
        """Apply consistent modern styling to a tkinter window."""
        root.configure(bg="#1e1e2e")
        root.attributes("-topmost", True)
        root.resizable(False, False)

    @staticmethod
    def _flat_button(parent, text, command, bg="#45475a", fg="#cdd6f4", hover_bg="#585b70",
                     font=("Segoe UI", 9), width=None, bold=False):
        """Create a flat, borderless button using a Label (avoids Windows tk.Button bevel).

        Returns the Label widget. Use pack/grid on the returned widget.
        """
        import tkinter as tk
        if bold:
            font = (font[0], font[1], "bold")
        lbl = tk.Label(parent, text=text, font=font, bg=bg, fg=fg,
                       padx=16, pady=5, cursor="hand2")
        if width:
            lbl.configure(width=width)
        lbl.bind("<Button-1>", lambda e: command())
        lbl.bind("<Enter>", lambda e: lbl.configure(bg=hover_bg))
        lbl.bind("<Leave>", lambda e: lbl.configure(bg=bg))
        return lbl

    def _is_claude_cli_available(self) -> bool:
        """Check if Claude CLI is available for minutes generation."""
        import subprocess as _sp
        try:
            r = _sp.run(["claude", "--version"], capture_output=True, text=True, timeout=5)
            return r.returncode == 0
        except (FileNotFoundError, _sp.TimeoutExpired):
            return False

    def _ask_meeting_name(self):
        """Show a popup to name the meeting and select diarization method.

        Returns:
            _ABORT: user clicked Discard
            (str, True, str|None): user clicked Save & Summarize (name, summarize, diarize_method)
            (str, False, str|None): user clicked Save (name, summarize, diarize_method)
        """
        result = [self._ABORT]
        event = threading.Event()

        def _show_dialog():
            import tkinter as tk

            from .transcribe import DIARIZE_METHODS

            root = tk.Tk()
            root.title("WhisperSync")
            self._style_window(root)
            root.geometry("440x210")

            bg = "#1e1e2e"
            fg = "#cdd6f4"
            fg_dim = "#6c7086"
            fg_muted = "#a6adc8"
            accent = "#89b4fa"
            danger = "#f38ba8"
            entry_bg = "#313244"

            # Title
            tk.Label(root, text="Save Meeting Recording", font=("Segoe UI", 11, "bold"),
                     bg=bg, fg=fg).pack(pady=(14, 2))

            # Meeting name entry
            tk.Label(root, text="Meeting name (leave blank for default):",
                     font=("Segoe UI", 9), bg=bg, fg=fg_dim).pack(pady=(2, 4))
            entry = tk.Entry(root, width=48, font=("Segoe UI", 10),
                             bg=entry_bg, fg=fg, insertbackground=fg,
                             relief="flat", highlightthickness=1, highlightcolor=accent)
            entry.pack(padx=20, ipady=4)
            entry.focus_force()

            # Diarization method selector
            method_frame = tk.Frame(root, bg=bg)
            method_frame.pack(pady=(8, 0), padx=20, fill=tk.X)

            tk.Label(method_frame, text="Diarization:", font=("Segoe UI", 9),
                     bg=bg, fg=fg_muted).pack(side=tk.LEFT)

            # Build method options: display labels and their config keys
            method_ids = list(DIARIZE_METHODS.keys())
            method_labels = list(DIARIZE_METHODS.values())
            primary = self.cfg.get("diarize_primary", "balanced_mix")
            default_idx = method_ids.index(primary) if primary in method_ids else 0

            selected_method = tk.StringVar(value=method_ids[default_idx])

            # Use flat label-buttons as a toggle group (consistent with dialog style)
            for i, (mid, mlabel) in enumerate(zip(method_ids, method_labels)):
                def _select(m=mid):
                    selected_method.set(m)
                    # Update visual selection
                    for child in method_frame.winfo_children():
                        if hasattr(child, '_method_id'):
                            if child._method_id == m:
                                child.configure(bg=accent, fg="#1e1e2e")
                            else:
                                child.configure(bg=entry_bg, fg=fg_muted)

                lbl = tk.Label(
                    method_frame, text=mlabel, font=("Segoe UI", 8),
                    bg=accent if mid == method_ids[default_idx] else entry_bg,
                    fg="#1e1e2e" if mid == method_ids[default_idx] else fg_muted,
                    padx=8, pady=2, cursor="hand2",
                )
                lbl._method_id = mid
                lbl.pack(side=tk.LEFT, padx=(6, 0))
                lbl.bind("<Button-1>", lambda e, m=mid: _select(m))

            # Buttons
            btn_frame = tk.Frame(root, bg=bg)
            btn_frame.pack(pady=(12, 10))

            def _sanitize():
                return self._sanitize_name(entry.get() or "")

            def _get_method():
                m = selected_method.get()
                # Return None if it matches the config default (no override needed)
                if m == self.cfg.get("diarize_primary", "balanced_mix"):
                    return None
                return m

            def _save_and_summarize(ev=None):
                result[0] = (_sanitize(), True, _get_method())
                root.destroy()

            def _save_only():
                result[0] = (_sanitize(), False, _get_method())
                root.destroy()

            def _abort():
                result[0] = self._ABORT
                root.destroy()

            entry.bind("<Return>", _save_and_summarize)
            entry.bind("<Escape>", lambda e: _abort())

            # Pack RIGHT to LEFT so rightmost button is packed first
            self._flat_button(btn_frame, "Save & Summarize", _save_and_summarize,
                              bg=accent, fg="#1e1e2e", hover_bg="#74c7ec", bold=True).pack(side=tk.RIGHT, padx=6)
            self._flat_button(btn_frame, "Save", _save_only).pack(side=tk.RIGHT, padx=6)
            self._flat_button(btn_frame, "Discard", _abort, fg=danger).pack(side=tk.RIGHT, padx=6)

            self._center_window(root)
            root.protocol("WM_DELETE_WINDOW", _abort)
            root.mainloop()
            event.set()

        t = threading.Thread(target=_show_dialog, daemon=True)
        t.start()
        event.wait(timeout=60)

        return result[0]

    def _generate_name_suggestions(self, summary: str, current_name: str) -> list[str]:
        """Generate 3 meeting name suggestions via Claude CLI.

        Falls back to simple word extraction if CLI fails.
        """
        import subprocess as _sp
        import re

        prompt = (
            "Generate exactly 3 short meeting folder names based on this meeting summary. "
            "Rules:\n"
            "- Each name should be 2-4 words, kebab-case (e.g., architecture-soft-reset, migration-go-live-planning)\n"
            "- Names should capture WHAT the meeting was about, not WHO was in it\n"
            "- No dates, no generic words like 'meeting' or 'discussion' or 'sync'\n"
            "- Think like a PM labeling a folder they'll scan later\n"
            "- Output ONLY the 3 names, one per line, nothing else\n\n"
            f"Current name: {current_name}\n"
            f"Summary: {summary}"
        )

        try:
            result = _sp.run(
                ["claude", "-p", "--model", "haiku"],
                input=prompt, capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0 and result.stdout.strip():
                lines = [l.strip().strip("-").strip() for l in result.stdout.strip().splitlines()]
                # Sanitize and filter
                suggestions = []
                for line in lines:
                    name = self._sanitize_name(line.lower())
                    if name and len(name) > 3 and name != current_name:
                        suggestions.append(name)
                if suggestions:
                    return suggestions[:3]
        except Exception:
            pass

        # Fallback: simple word extraction
        words = re.sub(r'[^a-zA-Z0-9\s]', '', summary).lower().split()
        stopwords = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
                     'of', 'with', 'by', 'is', 'was', 'are', 'were', 'be', 'been', 'has',
                     'had', 'have', 'that', 'this', 'from', 'not', 'all', 'can', 'will',
                     'meeting', 'discussion', 'sync', 'call'}
        meaningful = [w for w in words if w not in stopwords and len(w) > 2][:4]
        return ["-".join(meaningful)] if meaningful else [current_name]

    def _do_rename(self, meeting_dir: Path, date_time_str: str, new_name: str):
        """Perform the folder rename for a meeting directory."""
        import shutil
        new_folder_name = f"{date_time_str}_{new_name}"
        new_meeting_dir = meeting_dir.parent / new_folder_name
        if not new_meeting_dir.exists():
            shutil.move(str(meeting_dir), str(new_meeting_dir))
            logger.info(f"Renamed: {meeting_dir.name} -> {new_folder_name}")
            try:
                rebuild_root_index(self._output_dir())
            except Exception:
                pass
        else:
            logger.warning(f"Rename skipped - folder already exists: {new_folder_name}")

    def _ask_rename_suggestion(self, current_name: str, summary: str,
                               meeting_dir=None, date_time_str: str | None = None):
        """Offer a meeting rename via toast notification (no tkinter).

        Uses a toast with an Accept button for the top suggestion.
        Dismissing or ignoring the toast keeps the original name.
        """
        suggestions = self._generate_name_suggestions(summary, current_name)
        suggested = suggestions[0] if suggestions else current_name

        if not meeting_dir or not date_time_str:
            return None

        if suggested == current_name:
            logger.info("Suggested name matches current name, rename skipped")
            return None

        def _accept_rename():
            self._do_rename(Path(meeting_dir), date_time_str, suggested)

        notify(
            "Rename meeting?",
            f"Suggested: {suggested}",
            buttons=[
                {"label": "Accept", "action": _accept_rename},
            ],
        )
        return None

    def _show_llm_unavailable(self):
        """Show a dialog when Claude CLI is not available. Returns True if user checked 'don't show again'."""
        result = [False]
        event = threading.Event()

        def _show():
            import tkinter as tk

            root = tk.Tk()
            root.title("WhisperSync")
            self._style_window(root)
            root.geometry("420x160")

            bg = "#1e1e2e"
            fg = "#cdd6f4"
            fg_dim = "#6c7086"
            warn = "#f9e2af"

            tk.Label(root, text="LLM Not Available", font=("Segoe UI", 11, "bold"),
                     bg=bg, fg=warn).pack(pady=(14, 4))
            tk.Label(root, text="Claude CLI not found. Auto-summarize and rename\nrequire Claude Code to be installed.",
                     font=("Segoe UI", 9), bg=bg, fg=fg_dim, justify="center").pack(pady=(0, 8))

            dont_show = tk.BooleanVar(value=False)
            tk.Checkbutton(root, text="Don't show again", variable=dont_show,
                           font=("Segoe UI", 8), bg=bg, fg=fg_dim, selectcolor="#313244",
                           activebackground=bg, activeforeground=fg).pack(pady=(0, 8))

            def _ok():
                result[0] = dont_show.get()
                root.destroy()

            self._flat_button(root, "OK", _ok).pack()

            self._center_window(root)
            root.protocol("WM_DELETE_WINDOW", _ok)
            root.mainloop()
            event.set()

        t = threading.Thread(target=_show, daemon=True)
        t.start()
        event.wait(timeout=30)

        return result[0]

    def _ask_speaker_confirmation(self, identification_result: dict) -> dict | None:
        """Show speaker confirmation dialog. Returns confirmed speaker_map or None to skip."""
        speaker_map = identification_result.get("speaker_map", {})
        confidence = identification_result.get("confidence", {})
        reasoning = identification_result.get("reasoning", {})

        if not speaker_map:
            return None

        # Auto-confirm if single speaker with high confidence
        if len(speaker_map) == 1:
            sole_speaker = list(speaker_map.keys())[0]
            if confidence.get(sole_speaker) == "high":
                logger.info(f"Auto-confirmed single speaker: {speaker_map[sole_speaker]}")
                return speaker_map

        result = [None]
        event = threading.Event()

        # Load known speaker names from Known Speakers table ONLY (not Meeting Map)
        config_path = Path(get_config_path())
        known_names = []
        if config_path.exists():
            in_speakers_table = False
            for line in config_path.read_text(encoding="utf-8").splitlines():
                if "## Known Speakers" in line:
                    in_speakers_table = True
                    continue
                if in_speakers_table and line.startswith("##"):
                    break  # Hit next section — stop parsing
                if in_speakers_table and line.startswith("| ") and "ID" not in line and "---" not in line:
                    parts = [p.strip() for p in line.split("|") if p.strip()]
                    if len(parts) >= 2:
                        known_names.append(parts[1])

        def _show():
            import tkinter as tk

            root = tk.Tk()
            root.title("WhisperSync")
            self._style_window(root)

            bg = "#1e1e2e"
            fg = "#cdd6f4"
            fg_dim = "#6c7086"
            fg_muted = "#a6adc8"
            accent = "#89b4fa"
            card_bg = "#181825"
            green = "#a6e3a1"
            yellow = "#f9e2af"
            red = "#f38ba8"

            conf_colors = {"high": green, "medium": yellow, "low": red}

            num_speakers = len(speaker_map)
            # Each speaker row ~40px + reasoning line ~16px
            height = min(170 + (num_speakers * 58), 550)
            root.geometry(f"500x{height}")

            # Header
            header = tk.Frame(root, bg=bg)
            header.pack(fill="x", padx=24, pady=(14, 0))
            tk.Label(header, text="\U0001f3a4", font=("Segoe UI", 13), bg=bg).pack(side=tk.LEFT)
            tk.Label(header, text="  Identify Speakers", font=("Segoe UI", 11, "bold"),
                     bg=bg, fg=fg).pack(side=tk.LEFT)

            # Speaker rows
            dropdowns = {}
            rows_frame = tk.Frame(root, bg=bg)
            rows_frame.pack(fill="x", padx=24, pady=(12, 0))

            for spk_id, name in speaker_map.items():
                row = tk.Frame(rows_frame, bg=card_bg, highlightbackground="#313244", highlightthickness=1)
                row.pack(fill="x", pady=3, ipady=3)

                # Left side: speaker label + arrow + autocomplete entry
                left = tk.Frame(row, bg=card_bg)
                left.pack(side=tk.LEFT, padx=(10, 0), pady=4)

                tk.Label(left, text=spk_id, font=("Segoe UI", 9), bg=card_bg, fg=fg_dim,
                         width=11, anchor="w").pack(side=tk.LEFT)
                tk.Label(left, text="\u2192", font=("Segoe UI", 9), bg=card_bg, fg=fg_dim).pack(side=tk.LEFT, padx=4)

                # Autocomplete combo: Entry + ▼ button + floating Listbox
                entry_var = tk.StringVar(value=name)
                entry = tk.Entry(left, textvariable=entry_var, font=("Segoe UI", 9, "bold"),
                                 bg="#313244", fg=accent, insertbackground=fg,
                                 relief="flat", highlightthickness=1, highlightcolor=accent, width=14)
                entry.pack(side=tk.LEFT, padx=(4, 0), ipady=2)
                dropdowns[spk_id] = entry_var

                all_names = list(known_names) + (["Unknown"] if "Unknown" not in known_names else [])

                def _make_combo(ent, var, names, parent_row):
                    """Bind autocomplete + dropdown button to an entry widget."""
                    listbox_frame = [None]  # Mutable ref for the floating listbox

                    def _close_listbox():
                        if listbox_frame[0]:
                            listbox_frame[0].destroy()
                            listbox_frame[0] = None

                    def _show_listbox(filter_text=""):
                        _close_listbox()
                        # Position below the entry
                        x = ent.winfo_rootx() - root.winfo_rootx()
                        y = ent.winfo_rooty() - root.winfo_rooty() + ent.winfo_height()

                        frame = tk.Frame(root, bg="#313244", highlightbackground=accent, highlightthickness=1)
                        frame.place(x=x, y=y, width=ent.winfo_width() + 30)
                        listbox_frame[0] = frame

                        filtered = [n for n in names if filter_text.lower() in n.lower()] if filter_text else names
                        lb = tk.Listbox(frame, bg="#313244", fg=fg, selectbackground="#45475a",
                                        selectforeground=accent, font=("Segoe UI", 9),
                                        relief="flat", highlightthickness=0, height=min(len(filtered), 8))
                        lb.pack(fill="both", expand=True)
                        for n in filtered:
                            lb.insert(tk.END, n)

                        def _select(event=None):
                            sel = lb.curselection()
                            if sel:
                                var.set(lb.get(sel[0]))
                                ent.icursor(tk.END)
                            _close_listbox()
                            ent.focus_set()

                        lb.bind("<ButtonRelease-1>", _select)
                        lb.bind("<Return>", _select)

                    def _toggle_listbox():
                        if listbox_frame[0]:
                            _close_listbox()
                        else:
                            _show_listbox()

                    def _on_key(event):
                        if event.keysym == "Escape":
                            _close_listbox()
                            return
                        if event.keysym in ("Tab", "Return"):
                            current = var.get()
                            matches = [s for s in names if s.lower().startswith(current.lower())]
                            if matches and current.lower() != matches[0].lower():
                                var.set(matches[0])
                                ent.icursor(tk.END)
                            _close_listbox()
                            return "break"
                        elif event.keysym not in ("BackSpace", "Delete", "Left", "Right", "Home", "End"):
                            root.after(10, lambda: _autocomplete(ent, var, names))

                    def _autocomplete(ent, var, names):
                        if _closing[0]:
                            return
                        current = var.get()
                        if not current:
                            _close_listbox()
                            return
                        matches = [s for s in names if s.lower().startswith(current.lower()) and s.lower() != current.lower()]
                        if matches:
                            pos = ent.index(tk.INSERT)
                            var.set(matches[0])
                            ent.select_range(pos, tk.END)
                            ent.icursor(pos)
                        # Show filtered listbox while typing
                        if len(current) >= 1:
                            _show_listbox(current)
                        else:
                            _close_listbox()

                    ent.bind("<KeyRelease>", _on_key)

                    # ▼ button
                    btn = tk.Label(parent_row, text="\u25bc", font=("Segoe UI", 7), bg=card_bg,
                                   fg=fg_dim, cursor="hand2", padx=4)
                    btn.pack(in_=left, side=tk.LEFT, padx=(0, 4))
                    btn.bind("<Button-1>", lambda e: _toggle_listbox())

                _make_combo(entry, entry_var, all_names, row)

                # Confidence dot
                conf = confidence.get(spk_id, "low")
                color = conf_colors.get(conf, red)
                tk.Label(row, text="\u25cf", font=("Segoe UI", 11), bg=card_bg, fg=color).pack(side=tk.LEFT, padx=(8, 4))

                # Reasoning on its own line below
                reason = reasoning.get(spk_id, "")
                if reason:
                    reason_frame = tk.Frame(rows_frame, bg=bg)
                    reason_frame.pack(fill="x", padx=24, pady=(0, 2))
                    tk.Label(reason_frame, text=f"\u2514 {reason}", font=("Segoe UI", 7, "italic"),
                             bg=bg, fg=fg_dim, anchor="w", wraplength=400).pack(anchor="w")

            # Buttons
            btn_frame = tk.Frame(root, bg=bg)
            btn_frame.pack(pady=(14, 12))

            _closing = [False]

            def _confirm():
                if _closing[0]:
                    return
                _closing[0] = True
                try:
                    result[0] = {spk_id: var.get() for spk_id, var in dropdowns.items()}
                except Exception:
                    pass
                try:
                    root.destroy()
                except Exception as e:
                    logger.debug(f"Speaker confirm cleanup: {e}")

            def _skip():
                if _closing[0]:
                    return
                _closing[0] = True
                result[0] = None
                try:
                    root.destroy()
                except Exception as e:
                    logger.debug(f"Speaker dialog close: {e}")

            self._flat_button(btn_frame, "Confirm", _confirm,
                              bg=accent, fg="#1e1e2e", hover_bg="#74c7ec", bold=True).pack(side=tk.RIGHT, padx=8)
            self._flat_button(btn_frame, "Skip", _skip, fg=fg_muted).pack(side=tk.RIGHT, padx=8)

            self._center_window(root)
            root.protocol("WM_DELETE_WINDOW", _skip)
            root.mainloop()
            event.set()

        t = threading.Thread(target=_show, daemon=True)
        t.start()
        event.wait(timeout=120)

        return result[0]

    def _stop_meeting(self):
        audio = self.recorder.stop()

        if "mic" not in audio and "mic_path" not in audio:
            self.state.emit(IDLE, mode=None)
            return

        # Log meeting duration
        if self._meeting_start_time:
            _elapsed = (datetime.now() - self._meeting_start_time).total_seconds()
            _mins = int(_elapsed // 60)
            _secs = int(_elapsed % 60)
            logger.info(f"Meeting stopped: {_mins}m {_secs:02d}s recorded")

        # Stay in a processing state so clicks are ignored
        self.state.emit(MEETING_STOPPED, mode="saving")

        # Save WAV and enqueue for post-processing (never block recording thread)
        def _save_and_enqueue():
            dialog_result = self._ask_meeting_name()

            if dialog_result is self._ABORT:
                logger.info("Recording discarded")
                self.recorder.discard_streaming()
                self.state.emit(IDLE, mode=None)
                return

            meeting_name, do_summarize, diarize_method = dialog_result
            if diarize_method:
                from .transcribe import DIARIZE_METHODS
                logger.info(f"Meeting saved: {meeting_name or 'meeting'} (summarize={do_summarize}, diarize={DIARIZE_METHODS.get(diarize_method, diarize_method)})")
            else:
                logger.info(f"Meeting saved: {meeting_name or 'meeting'} (summarize={do_summarize})")

            self.recorder.stop_streaming()
            try:
                start = getattr(self, '_meeting_start_time', None) or datetime.now()
                week_dir = f"{start.strftime('%m')}-w{(start.day - 1) // 7 + 1}"
                date_time_str = start.strftime("%m%d_%H%M")
                folder_name = f"{date_time_str}_{meeting_name}" if meeting_name else f"{date_time_str}_meeting"
                meeting_dir = self._output_dir() / week_dir / folder_name
                meeting_dir.mkdir(parents=True, exist_ok=True)

                wav_path = meeting_dir / "recording.wav"
                if "mic_path" in audio:
                    # Disk-only mode: mic audio already on disk as streaming WAV
                    mic_wav_path = audio["mic_path"]
                    if "speaker" in audio:
                        from .streaming_wav import StreamingWavWriter
                        mic_array = StreamingWavWriter.read_audio_from(mic_wav_path)
                        save_stereo_wav(str(wav_path), mic_array.reshape(-1, 1), audio["speaker"], self.cfg["sample_rate"])
                    else:
                        import shutil
                        shutil.move(str(mic_wav_path), str(wav_path))
                elif "speaker" in audio:
                    save_stereo_wav(str(wav_path), audio["mic"], audio["speaker"], self.cfg["sample_rate"])
                else:
                    save_wav(str(wav_path), audio["mic"], self.cfg["sample_rate"])

                logger.info(f"WAV saved: {wav_path}")
                from .streaming_wav import cleanup_temp_files
                cleanup_temp_files(self._meeting_temp_dir())

                # Enqueue for post-processing (transcription, speaker ID, etc.)
                job = MeetingJob(
                    app=self,
                    wav_path=wav_path,
                    meeting_dir=meeting_dir,
                    name=meeting_name,
                    summarize=do_summarize,
                    date_time_str=date_time_str,
                    week_dir=week_dir,
                    folder_name=folder_name,
                    diarize_method=diarize_method,
                )
                self._post_queue.put(job)
                qsize = self._post_queue.qsize()
                logger.info(f"Meeting queued: {meeting_name or 'meeting'} ({qsize} in queue)")

                if qsize > 1:
                    notify("Meeting queued", f"'{meeting_name or 'meeting'}' will transcribe when the current meeting finishes")

                # Release mode so user can start another meeting or dictate
                if self.state.current.mode == "saving":
                    self.state.emit(IDLE, mode=None, meeting_transcribing=True)

            except Exception as e:
                logger.error(f"Failed to save meeting WAV: {e}")
                import traceback
                logger.debug(traceback.format_exc())
                self.state.emit(ERROR, meeting_transcribing=False, mode="error", data={"message": str(e), "recoverable": False})
                self._schedule_idle(3)

        threading.Thread(target=_save_and_enqueue, daemon=True).start()

    def _post_process_worker(self):
        """Process meeting job steps sequentially.

        Runs as a daemon thread started in run(). Each meeting is a
        MeetingJob with discrete steps. The worker pulls a job and
        executes steps one at a time. If a step fails, the job is
        abandoned but the worker continues to the next job.

        Recording start/stop is NEVER touched here.
        """
        while True:
            job = self._post_queue.get()
            if job is None:
                logger.info("Post-processing worker shutting down")
                break  # Shutdown signal
            try:
                self._run_meeting_job(job)
            except Exception as e:
                logger.error(f"Post-processing failed for {job.name}: {e}", exc_info=True)
            finally:
                self._post_queue.task_done()

    def _run_meeting_job(self, job: MeetingJob):
        """Execute all steps of a MeetingJob with error recovery.

        Each step is independent. If a step fails with a fatal error
        (WorkerCrashedError, PermissionError, FileNotFoundError), the
        job is abandoned and state is set to error. Non-fatal errors
        are handled within each step method.
        """
        from .worker_manager import WorkerCrashedError

        logger.info(f"Processing meeting: {job.name or 'meeting'}")

        try:
            while not job.is_complete:
                step_num = job._current_step + 1
                step_name = job.current_step_name
                logger.debug(
                    f"Step {step_num}/{job.total_steps}: {step_name} "
                    f"for {job.name or 'meeting'}"
                )
                job.execute_next_step()

            logger.info(f"Meeting done: {job.name or 'meeting'}")

        except WorkerCrashedError as e:
            logger.error("Worker crashed during meeting, respawning...")
            logger.info(f"Audio is preserved at: {job.wav_path}")
            self.worker.restart()
            self._emit_error_safe(str(e))
        except PermissionError as e:
            logger.error(str(e))
            self._show_error_popup("Diarization Model Access", str(e))
            self._emit_error_safe(str(e))
        except FileNotFoundError as e:
            logger.error(str(e))
            self._show_error_popup("Hugging Face Token Missing", str(e))
            self._emit_error_safe(str(e))
        except Exception as e:
            logger.error(f"Meeting transcription error: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            self._emit_error_safe(str(e))

    def _emit_error_safe(self, message: str):
        """Emit an error state, preserving mode if a recording is active."""
        current_mode = self.state.current.mode
        # If recording is active, keep the current mode
        safe_mode = current_mode if self.recorder.is_recording else "error"
        if safe_mode is None:
            safe_mode = "error"
        self.state.emit(
            ERROR,
            meeting_transcribing=False,
            mode=safe_mode,
            data={"message": message, "recoverable": False},
        )
        if safe_mode == "error":
            self._schedule_idle(3)

    @staticmethod
    def _truncate_path(p: Path, max_len: int = 40) -> str:
        """Truncate a path for display in menus."""
        s = str(p)
        if len(s) <= max_len:
            return s
        parts = p.parts
        if len(parts) <= 2:
            return s
        return f".../{'/'.join(parts[-2:])}"

    def _output_dir(self) -> Path:
        p = Path(self.cfg["output_dir"])
        if not p.is_absolute():
            # Relative paths resolve from repo root
            p = get_install_root() / p
        return p

    def _generate_minutes(self, meeting_dir: Path, readable_file: Path, minutes_file: Path):
        """Generate minutes.md via Claude CLI (claude -p) using the shared prompt template."""
        import json
        import subprocess as _sp

        prompt_file = Path(__file__).parent / "minutes_prompt.md"
        if not prompt_file.exists():
            logger.warning(f"Minutes prompt template not found: {prompt_file}")
            return

        prompt_text = prompt_file.read_text(encoding="utf-8")
        transcript_text = readable_file.read_text(encoding="utf-8")

        # Build speaker context from transcript.json speaker_map + config roles
        speaker_context = ""
        try:
            json_path = meeting_dir / "transcript.json"
            if json_path.exists():
                with open(json_path) as f:
                    tdata = json.load(f)
                smap = tdata.get("speaker_map", {})
                if smap:
                    cfg_path = Path(get_config_path())
                    roles = {}
                    if cfg_path.exists():
                        for line in cfg_path.read_text(encoding="utf-8").splitlines():
                            if line.startswith("| ") and " | " in line and "ID" not in line and "---" not in line:
                                parts = [p.strip() for p in line.split("|") if p.strip()]
                                if len(parts) >= 3:
                                    roles[parts[1].lower()] = parts[2]
                    ctx_lines = []
                    for spk_id, name in smap.items():
                        role = roles.get(name.lower(), "")
                        ctx_lines.append(f"  {spk_id} = {name}" + (f" ({role})" if role else ""))
                    speaker_context = "\n".join(ctx_lines)
        except Exception:
            pass

        # Inject speaker context into prompt
        prompt_text = prompt_text.replace(
            "{SPEAKER_CONTEXT}",
            speaker_context or "No speaker identification available — use context clues from the transcript."
        )

        # Build the full prompt: template + transcript
        full_prompt = (
            f"{prompt_text}\n\n"
            f"---\n\n"
            f"Meeting folder: {meeting_dir.name}\n\n"
            f"Transcript:\n\n{transcript_text}"
        )

        logger.info(f"Generating minutes via Claude CLI for: {meeting_dir.name}")
        try:
            result = _sp.run(
                ["claude", "-p", "--model", "sonnet"],
                input=full_prompt,
                capture_output=True,
                text=True,
                timeout=300,  # 5 minutes max
                cwd=str(Path(__file__).parent.parent.parent),  # repo root
            )
            if result.returncode == 0 and result.stdout.strip():
                minutes_file.write_text(result.stdout, encoding="utf-8")
                logger.info(f"Minutes saved: {minutes_file}")
            else:
                logger.warning(f"Claude CLI returned code {result.returncode}")
                if result.stderr:
                    logger.debug(f"stderr: {result.stderr[:500]}")
        except FileNotFoundError:
            logger.warning("Claude CLI not found — minutes generation skipped. Install: npm i -g @anthropic-ai/claude-code")
        except _sp.TimeoutExpired:
            logger.warning("Claude CLI timed out generating minutes (5 min limit)")

    def _format_feature_async(self, raw_text: str, entry_id: str):
        """Format a feature suggestion via Claude CLI (background thread)."""
        import subprocess as _sp

        prompt_file = Path(__file__).parent / "feature_prompt.md"
        if not prompt_file.exists():
            logger.warning(f"Feature prompt template not found: {prompt_file}")
            return

        prompt_text = prompt_file.read_text(encoding="utf-8")
        prompt_text = prompt_text.replace("{TRANSCRIPTION}", raw_text)

        logger.info("Formatting feature suggestion via Claude CLI...")
        try:
            result = _sp.run(
                ["claude", "-p", "--model", "haiku"],
                input=prompt_text,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0 and result.stdout.strip():
                feature_log.update_consolidated(entry_id, result.stdout.strip())
                logger.info("Feature suggestion formatted successfully")
                notify("Feature formatted", "Claude formatted your feature suggestion")
            else:
                logger.warning(f"Claude CLI returned code {result.returncode}")
                if result.stderr:
                    logger.debug(f"stderr: {result.stderr[:500]}")
        except FileNotFoundError:
            logger.warning("Claude CLI not found - raw feature saved without formatting")
        except _sp.TimeoutExpired:
            logger.warning("Claude CLI timed out formatting feature (60s limit)")

    def _meeting_temp_dir(self) -> Path:
        return Path(__file__).parent / "logs" / "data" / "meeting"

    # --- Menu ---

    def _fmt_hotkey(self, key: str) -> str:
        return key.replace("+", " + ").title()

    @staticmethod
    def _cb(fn, *bound_args):
        """Create a pystray-compatible callback (icon, item) that calls fn(*bound_args).

        pystray passes (icon, item) as 2 positional args which would override
        lambda default keyword args. This closure avoids that problem.
        """
        def _handler(_icon, _item):
            fn(*bound_args)
        return _handler

    def _copy_dictation(self, text: str):
        """Copy a dictation's full text to clipboard."""
        import pyperclip
        pyperclip.copy(text)

    def _open_dictation_logs(self):
        """Open the dictation logs folder in Explorer."""
        from .paths import get_dictation_log_dir
        log_dir = get_dictation_log_dir()
        if log_dir.exists():
            import subprocess
            subprocess.Popen(["explorer", str(log_dir)])
        else:
            logger.info("No dictation logs folder found")

    def _clear_dictation_history(self):
        """Clear the in-memory dictation history (menu only, logs on disk are preserved)."""
        self._dictation_history.clear()
        self._refresh_menu()

    def _build_recent_dictations_menu(self):
        """Build the Recent Dictations submenu items."""
        if not self._dictation_history:
            return pystray.Menu(
                pystray.MenuItem("No dictations yet", None, enabled=False),
            )
        items = []
        for entry in reversed(self._dictation_history):
            full_text = entry["text"]
            preview = full_text[:40]
            if len(full_text) > 40:
                preview += "..."
            label = f"[{entry['timestamp']}] {preview}\t{entry['chars']} chars"
            items.append(
                pystray.MenuItem(label, self._cb(self._copy_dictation, full_text))
            )
        items.append(pystray.Menu.SEPARATOR)
        items.append(
            pystray.MenuItem("Open Logs", self._cb(self._open_dictation_logs))
        )
        items.append(
            pystray.MenuItem("Clear History", self._cb(self._clear_dictation_history))
        )
        return pystray.Menu(*items)

    def _build_meetings_menu(self):
        """Build the Meetings submenu showing recent meetings with speaker status."""
        import json as _json

        output_dir = self._output_dir()
        meeting_folders = []

        # Scan all week folders for meeting directories with transcript.json
        for week_dir in sorted(output_dir.iterdir(), reverse=True):
            if not week_dir.is_dir() or week_dir.name.startswith("."):
                continue
            for meeting_dir in sorted(week_dir.iterdir(), reverse=True):
                if not meeting_dir.is_dir():
                    continue
                json_path = meeting_dir / "transcript.json"
                if json_path.exists():
                    # Check speaker_map status
                    try:
                        with open(json_path) as f:
                            data = _json.load(f)
                        smap = data.get("speaker_map", {})
                        names = [str(v).strip() for v in smap.values() if str(v).strip()]
                        if names:
                            status = ", ".join(sorted(set(names)))
                        else:
                            status = "No speakers"
                    except Exception:
                        status = "Error"
                    meeting_folders.append((meeting_dir, status))
                    if len(meeting_folders) >= 10:
                        break
            if len(meeting_folders) >= 10:
                break

        if not meeting_folders:
            return pystray.Menu(
                pystray.MenuItem("No meetings found", None, enabled=False),
            )

        items = []
        for meeting_dir, status in meeting_folders:
            label = f"{meeting_dir.name}\t{status}"
            items.append(
                pystray.MenuItem(
                    label,
                    self._cb(self._recover_meeting_speakers, meeting_dir),
                )
            )
        return pystray.Menu(*items)

    def _build_menu(self):
        devices = list_devices(api_filter=self._api_filter)
        dict_hk = self._fmt_hotkey(self.cfg["hotkeys"]["dictation_toggle"])
        meet_hk = self._fmt_hotkey(self.cfg["hotkeys"]["meeting_toggle"])
        use_sys = self.cfg.get("use_system_devices", True)

        # --- Resolve effective devices (config or system default) ---
        defaults = get_default_devices(api_filter=self._api_filter)
        eff_mic = defaults["input"] if use_sys else (self.cfg.get("mic_device") or defaults["input"])
        eff_spk = defaults["output"] if use_sys else (self.cfg.get("speaker_device") or defaults["output"])

        # --- Device submenus ---
        mic_items = [
            pystray.MenuItem(
                f"{d['name']} (system)" if d["id"] == defaults["input"] else d["name"],
                self._cb(self._set_device, "mic_device", d["id"]),
                checked=lambda item, d=d, em=eff_mic: d["id"] == em,
                radio=True,
                enabled=not use_sys,
            )
            for d in devices["inputs"]
        ]
        speaker_items = [
            pystray.MenuItem(
                f"{d['name']} (system)" if d["id"] == defaults["output"] else d["name"],
                self._cb(self._set_device, "speaker_device", d["id"]),
                checked=lambda item, d=d, es=eff_spk: d["id"] == es,
                radio=True,
                enabled=not use_sys,
            )
            for d in devices["outputs"]
        ]

        # --- Device filter submenu ---
        apis = get_host_apis()
        filter_label = f"Device Filter\t{self._api_filter or 'All'}"
        filter_items = [
            pystray.MenuItem(
                "All",
                self._cb(self._set_api_filter, None),
                checked=lambda item: self._api_filter is None,
                radio=True,
            )
        ] + [
            pystray.MenuItem(
                a["name"],
                self._cb(self._set_api_filter, a["name"]),
                checked=lambda item, a=a: self._api_filter == a["name"],
                radio=True,
            )
            for a in apis
        ]

        # --- Settings submenus ---
        dictation_hk_items = [
            pystray.MenuItem(
                hk,
                self._cb(self._set_hotkey, "dictation_toggle", hk),
                checked=lambda item, hk=hk: self.cfg["hotkeys"]["dictation_toggle"] == hk,
                radio=True,
            )
            for hk in HOTKEY_OPTIONS
        ]
        meeting_hk_items = [
            pystray.MenuItem(
                hk,
                self._cb(self._set_hotkey, "meeting_toggle", hk),
                checked=lambda item, hk=hk: self.cfg["hotkeys"]["meeting_toggle"] == hk,
                radio=True,
            )
            for hk in HOTKEY_OPTIONS
        ]
        feature_hk_items = [
            pystray.MenuItem(
                hk,
                self._cb(self._set_hotkey, "feature_suggest", hk),
                checked=lambda item, hk=hk: self.cfg["hotkeys"].get("feature_suggest", "ctrl+shift+alt+f") == hk,
                radio=True,
            )
            for hk in FEATURE_HOTKEY_OPTIONS
        ]
        paste_items = [
            pystray.MenuItem(
                method,
                self._cb(self._set_paste_method, method),
                checked=lambda item, m=method: self.cfg["paste_method"] == m,
                radio=True,
            )
            for method in PASTE_OPTIONS
        ]
        dictation_model_items = [
            pystray.MenuItem(
                f"{name} ({size})",
                self._cb(self._set_model, "dictation_model", name),
                checked=lambda item, n=name: self.cfg.get("dictation_model", self.cfg["model"]) == n,
                radio=True,
            )
            for name, size in MODEL_OPTIONS.items()
        ]
        meeting_model_items = [
            pystray.MenuItem(
                f"{name} ({size})",
                self._cb(self._set_model, "model", name),
                checked=lambda item, n=name: self.cfg["model"] == n,
                radio=True,
            )
            for name, size in MODEL_OPTIONS.items()
        ]
        left_click_items = [
            pystray.MenuItem(
                label,
                self._cb(self._set_click, "left_click", action),
                checked=lambda item, a=action: self.cfg.get("left_click", "meeting") == a,
                radio=True,
            )
            for action, label in CLICK_ACTIONS.items()
        ]
        middle_click_items = [
            pystray.MenuItem(
                label,
                self._cb(self._set_click, "middle_click", action),
                checked=lambda item, a=action: self.cfg.get("middle_click", "dictation") == a,
                radio=True,
            )
            for action, label in CLICK_ACTIONS.items()
        ]

        # Device (compute) selection
        current_device = self.cfg.get("device", "auto")
        # Build per-option labels with GPU name for auto
        device_options = []
        try:
            from .transcribe import get_gpu_name
            gpu_name = get_gpu_name()
        except Exception:
            gpu_name = None
        auto_suffix = f"\t{gpu_name}" if gpu_name else "\tCPU -- no GPU detected"
        device_options.append(("auto", f"Auto{auto_suffix}"))
        gpu_suffix = f"\t{gpu_name}" if gpu_name else "\tnot available"
        device_options.append(("gpu", f"GPU{gpu_suffix}"))
        device_options.append(("cpu", "CPU"))
        device_items = [
            pystray.MenuItem(
                label,
                self._cb(self._set_compute_device, dev),
                checked=lambda item, d=dev: self.cfg.get("device", "auto") == d,
                radio=True,
            )
            for dev, label in device_options
        ]

        # --- Always Available Dictation ---
        backup_device_cfg = self.cfg.get("backup_device", "auto")
        backup_model_cfg = self.cfg.get("backup_model", "base")
        backup_model_options = ["tiny", "base", "small"]
        backup_device_options = [
            ("auto", "Auto"),
            ("gpu", "GPU"),
            ("cpu", "CPU"),
        ]
        backup_device_items = [
            pystray.MenuItem(
                label,
                self._cb(self._set_backup_device, dev),
                checked=lambda item, d=dev: self.cfg.get("backup_device", "auto") == d,
                radio=True,
            )
            for dev, label in backup_device_options
        ]
        backup_model_items = [
            pystray.MenuItem(
                f"{name} ({MODEL_OPTIONS.get(name, '')})",
                self._cb(self._set_backup_model, name),
                checked=lambda item, n=name: self.cfg.get("backup_model", "base") == n,
                radio=True,
            )
            for name in backup_model_options
        ]

        # --- Notifications submenu ---
        from .notifications import DEFAULT_TOAST_EVENTS
        _toast_events = self.cfg.get("toast_events", list(DEFAULT_TOAST_EVENTS))
        _notification_options = [
            ("meeting_completed", "Meeting Complete"),
            ("error", "Errors"),
            ("pr_status_changed", "PR Status"),
            ("dictation_completed", "Dictation Complete"),
        ]
        notification_items = [
            pystray.MenuItem(
                label,
                self._cb(self._toggle_toast_event, evt),
                checked=lambda item, e=evt: e in self.cfg.get("toast_events", list(DEFAULT_TOAST_EVENTS)),
            )
            for evt, label in _notification_options
        ]

        # --- Diarization (Speaker Detection) submenu ---
        from .transcribe import DIARIZE_METHODS
        _diarize_slots = [
            ("diarize_primary", "Primary"),
            ("diarize_fallback", "Fallback"),
            ("diarize_last_resort", "Last Resort"),
        ]
        diarize_sub_items = []
        for slot_key, slot_label in _diarize_slots:
            current_method = self.cfg.get(slot_key, "balanced_mix")
            slot_items = [
                pystray.MenuItem(
                    DIARIZE_METHODS.get(method_id, method_id),
                    self._cb(self._set_diarize_method, slot_key, method_id),
                    checked=lambda item, m=method_id, sk=slot_key: self.cfg.get(sk, "balanced_mix") == m,
                    radio=True,
                )
                for method_id in DIARIZE_METHODS
            ]
            diarize_sub_items.append(
                pystray.MenuItem(
                    f"{slot_label}\t{DIARIZE_METHODS.get(current_method, current_method)}",
                    pystray.Menu(*slot_items),
                )
            )
        primary_method = self.cfg.get("diarize_primary", "balanced_mix")
        primary_label = DIARIZE_METHODS.get(primary_method, primary_method)

        # --- Whisper mode ---
        incognito_on = self.cfg.get("incognito", False)
        incognito_items = [
            pystray.MenuItem(
                "Whisper Mode",
                lambda: self._toggle_incognito(),
                checked=lambda item: self.cfg.get("incognito", False),
            ),
            pystray.MenuItem("  RAM only dictation, no disk, no logs", None, enabled=False),
        ]

        # Left-click fires the default menu item
        left_action = self.cfg.get("left_click", "meeting")
        return pystray.Menu(
            pystray.MenuItem("Meetings", self._build_meetings_menu()),
            pystray.MenuItem("Recent Dictations", self._build_recent_dictations_menu()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(f"Dictation\t{dict_hk}", lambda: self._on_left_click() if left_action == "dictation" else self.toggle_dictation(),
                             default=left_action == "dictation"),
            pystray.MenuItem(f"Meeting\t{meet_hk}", lambda: self._on_left_click() if left_action == "meeting" else self.toggle_meeting(),
                             default=left_action == "meeting"),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Mic Input\tsystem", None, enabled=False)
            if use_sys else
            pystray.MenuItem("Mic Input", pystray.Menu(*mic_items)),
            pystray.MenuItem("Speaker Output\tsystem", None, enabled=False)
            if use_sys else
            pystray.MenuItem("Speaker Output", pystray.Menu(*speaker_items)),
            pystray.MenuItem(
                "Always Use System Devices",
                self._cb(self._toggle_system_devices),
                checked=lambda item: self.cfg.get("use_system_devices", True),
            ),
            pystray.MenuItem(filter_label, pystray.Menu(*filter_items)),
            pystray.Menu.SEPARATOR,
            *self._github_menu_items(),
            pystray.MenuItem("Open Output Folder", lambda: self._open_output_folder()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Settings", pystray.Menu(
                pystray.MenuItem(f"Dictation Hotkey\t{self.cfg['hotkeys']['dictation_toggle']}",
                                 pystray.Menu(*dictation_hk_items)),
                pystray.MenuItem(f"Meeting Hotkey\t{self.cfg['hotkeys']['meeting_toggle']}",
                                 pystray.Menu(*meeting_hk_items)),
                pystray.MenuItem(f"Feature Suggest Hotkey\t{self.cfg['hotkeys'].get('feature_suggest', 'ctrl+shift+alt+f')}",
                                 pystray.Menu(*feature_hk_items)),
                pystray.MenuItem(f"Paste Method\t{self.cfg['paste_method']}",
                                 pystray.Menu(*paste_items)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(f"Left Click\t{CLICK_ACTIONS.get(self.cfg.get('left_click', 'meeting'), 'meeting')}",
                                 pystray.Menu(*left_click_items)),
                pystray.MenuItem(f"Middle Click\t{CLICK_ACTIONS.get(self.cfg.get('middle_click', 'dictation'), 'dictation')}",
                                 pystray.Menu(*middle_click_items)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(f"Dictation Model\t{self.cfg.get('dictation_model', self.cfg['model'])}",
                                 pystray.Menu(*dictation_model_items)),
                pystray.MenuItem(f"Meeting Model\t{self.cfg['model']}",
                                 pystray.Menu(*meeting_model_items)),
                pystray.MenuItem(f"Device\t{self._get_device_label()}",
                                 pystray.Menu(*device_items)),
                pystray.MenuItem("Always Available Dictation", pystray.Menu(
                    pystray.MenuItem(
                        "Enabled",
                        lambda: self._toggle_always_available_dictation(),
                        checked=lambda item: self.cfg.get("always_available_dictation", True),
                    ),
                    pystray.MenuItem(f"Backup Device\t{backup_device_cfg}",
                                     pystray.Menu(*backup_device_items)),
                    pystray.MenuItem(f"Backup Model\t{backup_model_cfg}",
                                     pystray.Menu(*backup_model_items)),
                )),
                pystray.MenuItem(f"Diarization (Speaker Detection)\t{primary_label}",
                                 pystray.Menu(*diarize_sub_items)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Change Output Folder...",
                                 lambda: self._change_output_folder()),
                pystray.MenuItem(f"  {self._truncate_path(self._output_dir())}",
                                 None, enabled=False),
                pystray.MenuItem(f"Log Window\t{self.cfg.get('log_window', 'normal')}", pystray.Menu(
                    pystray.MenuItem("Off",
                                     self._cb(self._set_log_level, "off"),
                                     checked=lambda item: self.cfg.get("log_window") == "off",
                                     radio=True),
                    pystray.MenuItem("Normal",
                                     self._cb(self._set_log_level, "normal"),
                                     checked=lambda item: self.cfg.get("log_window", "normal") == "normal",
                                     radio=True),
                    pystray.MenuItem("Detailed -- includes transcriptions",
                                     self._cb(self._set_log_level, "detailed"),
                                     checked=lambda item: self.cfg.get("log_window") == "detailed",
                                     radio=True),
                    pystray.MenuItem("Verbose -- full debug output",
                                     self._cb(self._set_log_level, "verbose"),
                                     checked=lambda item: self.cfg.get("log_window") == "verbose",
                                     radio=True),
                )),
                pystray.MenuItem("Weekly Stats", self._build_session_stats_menu()),
                pystray.MenuItem("Notifications", pystray.Menu(*notification_items)),
                pystray.Menu.SEPARATOR,
                *incognito_items,
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Update", pystray.Menu(
                    pystray.MenuItem("Stable\tmain", self._cb(self._update, "main")),
                    pystray.MenuItem("Labs\tdev", self._cb(self._update, "dev")),
                )),
                pystray.MenuItem("Restart", lambda: self._restart()),
                pystray.MenuItem("Quit", lambda: self.quit()),
            )),
        )

    # --- Actions ---

    def _toggle_toast_event(self, event_type: str):
        from .notifications import DEFAULT_TOAST_EVENTS
        events = self.cfg.get("toast_events", list(DEFAULT_TOAST_EVENTS))
        if event_type in events:
            events.remove(event_type)
        else:
            events.append(event_type)
        self.cfg["toast_events"] = events
        self._save_and_refresh()

    def _toggle_incognito(self):
        self.cfg["incognito"] = not self.cfg.get("incognito", False)
        state = "on" if self.cfg["incognito"] else "off"
        logger.info(f"Whisper mode: {state}")
        self._save_and_refresh()
        # #40: Toast warning when incognito toggles
        try:
            if self.cfg["incognito"]:
                notify(
                    "Whisper Mode Active",
                    "RAM only. No disk, no logs, no recovery.",
                )
            else:
                notify(
                    "Whisper Mode Off",
                    "Dictation data will be saved to disk",
                )
        except Exception:
            pass  # toast is best-effort

    def _refresh_menu(self):
        if self.tray:
            self.tray.menu = self._build_menu()
            self.tray.update_menu()

    def _save_and_refresh(self):
        config.save(self.cfg)
        self._refresh_menu()

    # --- GitHub PR Status ---

    def _start_github_poller(self):
        """Start the GitHub PR status poller if configured."""
        repo = self.cfg.get("github_repo")
        if not repo:
            return

        from .github_status import GitHubPoller
        interval = self.cfg.get("github_poll_interval", 300)

        def _on_change(old_prs, new_prs):
            self._github_prs = new_prs
            self._refresh_menu()
            if not self.cfg.get("github_notifications", True):
                return
            # Notify on actionable changes
            repo = self.cfg.get("github_repo", "")
            old_map = {pr.number: pr.review_state for pr in old_prs}
            for pr in new_prs:
                old_state = old_map.get(pr.number)
                if old_state == pr.review_state:
                    continue
                if pr.review_state == "clean":
                    self._notify(
                        f"PR #{pr.number} ready to merge",
                        pr.title,
                        buttons=[
                            {"label": "Merge", "action": lambda _pr=pr: self._merge_pr(repo, _pr.number)},
                            {"label": "View on GitHub", "action": lambda _pr=pr: self._open_pr_url(_pr.url)},
                        ],
                    )
                elif pr.review_state == "suggestions":
                    self._notify(
                        f"PR #{pr.number}: {pr.suggestion_count} suggestion(s)",
                        pr.title,
                        buttons=[
                            {"label": "View on GitHub", "action": lambda _pr=pr: self._open_pr_url(_pr.url)},
                        ],
                    )
                elif pr.review_state == "human-review":
                    self._notify(
                        f"PR #{pr.number} flagged for human review",
                        pr.title,
                        buttons=[
                            {"label": "View on GitHub", "action": lambda _pr=pr: self._open_pr_url(_pr.url)},
                        ],
                    )

        def _on_initial_poll(old_prs, new_prs):
            """First poll — update menu regardless of change detection."""
            _on_change(old_prs, new_prs)

        def _on_feature_scan(open_prs, merged_prs):
            from . import feature_lifecycle
            feature_lifecycle.scan_open_prs(open_prs)
            feature_lifecycle.scan_merged_prs(merged_prs)

        self._github_poller = GitHubPoller(
            repo=repo, interval=interval, on_change=_on_change,
            on_feature_scan=_on_feature_scan,
        )
        self._github_poller.start()
        if self._github_poller.state.available:
            # Refresh menu after first poll completes
            def _wait_first_poll():
                import time
                for _ in range(30):  # Wait up to 30s for first poll
                    time.sleep(1)
                    if self._github_poller.state.last_poll > 0:
                        self._github_prs = self._github_poller.state.prs
                        self._refresh_menu()
                        break
            threading.Thread(target=_wait_first_poll, daemon=True).start()

    def _notify(self, title: str, body: str = "", *, buttons=None, on_click=None):
        """Show a Windows toast notification via windows-toasts."""
        notify(title, body, buttons=buttons, on_click=on_click)

    def _github_menu_items(self) -> list:
        """Build menu items for GitHub PR status."""
        repo = self.cfg.get("github_repo")
        if not repo or not self._github_poller or not self._github_poller.state.available:
            return []

        prs = self._github_prs
        if not prs:
            # No PRs — clicking opens GitHub pulls page
            return [pystray.MenuItem(
                "GitHub\tno open PRs",
                self._cb(self._open_pr_url, f"https://github.com/{repo}/pulls"),
            )]

        label = f"GitHub\t{len(prs)} open PR{'s' if len(prs) != 1 else ''}"
        pr_items = []
        for pr in prs:
            status_label = {
                "pending": "awaiting review",
                "clean": "ready to merge",
                "suggestions": f"{pr.suggestion_count} suggestion{'s' if pr.suggestion_count != 1 else ''}",
                "human-review": "needs review",
            }.get(pr.review_state, "unknown")

            pr_display = f"#{pr.number}: {pr.title[:35]} — {status_label}"

            # Build submenu based on state
            sub = [pystray.MenuItem("View on GitHub", self._cb(self._open_pr_url, pr.url))]

            if pr.review_state == "clean":
                sub.append(pystray.MenuItem("Merge", self._cb(self._merge_pr, repo, pr.number)))
            elif pr.review_state == "suggestions":
                sub.append(pystray.MenuItem("View Suggestions", self._cb(self._open_pr_url, pr.url)))

            pr_items.append(pystray.MenuItem(pr_display, pystray.Menu(*sub)))

        pr_items.append(pystray.Menu.SEPARATOR)
        pr_items.append(pystray.MenuItem("Check now", lambda: self._github_poller.poll_now()))

        return [pystray.MenuItem(label, pystray.Menu(*pr_items))]

    def _open_pr_url(self, url: str):
        """Open a GitHub URL in the default browser."""
        import webbrowser
        if url:
            webbrowser.open(url)

    def _merge_pr(self, repo: str, pr_number: int):
        """Merge a PR via gh CLI.

        NOTE: This method may be called from a toast notification thread
        (via notifications.py button callbacks). The threading is handled
        in notifications.py -- this method itself is blocking.
        """
        import subprocess as _sp
        try:
            result = _sp.run(
                ["gh", "pr", "merge", str(pr_number), "--repo", repo,
                 "--squash", "--delete-branch"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                logger.info(f"PR #{pr_number} merged successfully")
                self._notify("PR merged", f"PR #{pr_number} merged to main")
                # Refresh after merge
                if self._github_poller:
                    self._github_poller.poll_now()
            else:
                logger.warning(f"PR #{pr_number} merge failed: {result.stderr.strip()}")
                self._notify("Merge failed", f"PR #{pr_number} merge failed -- check logs")
        except Exception as e:
            logger.warning(f"PR merge error: {e}")

    def _show_error_popup(self, title: str, message: str):
        """Show a tkinter error dialog with the given message."""
        def _show():
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(f"WhisperSync: {title}", message)
            root.destroy()
        threading.Thread(target=_show, daemon=True).start()

    def _open_output_folder(self):
        import subprocess
        out = self._output_dir()
        out.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["explorer.exe", str(out)])

    def _change_output_folder(self):
        """Show folder picker, optionally move existing files, update config."""
        import shutil

        current = self._output_dir()
        result = [None]  # None=cancelled, (Path, bool)=(new_path, move_files)
        event = threading.Event()

        def _show():
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)

            new_dir = filedialog.askdirectory(
                title="Choose output folder for recordings",
                initialdir=str(current) if current.exists() else str(Path.home()),
            )
            root.destroy()

            if not new_dir or Path(new_dir) == current:
                event.set()
                return

            new_path = Path(new_dir)
            # Check if current folder has files to move
            has_files = current.exists() and any(current.iterdir())

            if not has_files:
                result[0] = (new_path, False)
                event.set()
                return

            # Ask about moving files
            move_result = [None]
            move_event = threading.Event()

            def _show_move_dialog():
                dlg = tk.Tk()
                dlg.title("WhisperSync")
                self._style_window(dlg)
                dlg.geometry("440x150")

                bg = "#1e1e2e"
                fg = "#cdd6f4"
                fg_dim = "#6c7086"
                accent = "#89b4fa"

                tk.Label(dlg, text="Move Existing Recordings?",
                         font=("Segoe UI", 11, "bold"), bg=bg, fg=fg).pack(pady=(14, 4))
                tk.Label(dlg, text=f"Move files from current folder to new location?",
                         font=("Segoe UI", 9), bg=bg, fg=fg_dim).pack(pady=(0, 4))
                tk.Label(dlg, text=f"{current}",
                         font=("Segoe UI", 8), bg=bg, fg=fg_dim).pack()
                tk.Label(dlg, text=f"→ {new_path}",
                         font=("Segoe UI", 8), bg=bg, fg=accent).pack(pady=(0, 6))

                btn_frame = tk.Frame(dlg, bg=bg)
                btn_frame.pack(pady=(6, 10))

                def _move():
                    move_result[0] = True
                    dlg.destroy()

                def _keep():
                    move_result[0] = False
                    dlg.destroy()

                def _cancel():
                    move_result[0] = None
                    dlg.destroy()

                dlg.bind("<Escape>", lambda e: _cancel())

                self._flat_button(btn_frame, "Move Files", _move,
                                  bg=accent, fg="#1e1e2e", hover_bg="#74c7ec",
                                  bold=True).pack(side=tk.RIGHT, padx=6)
                self._flat_button(btn_frame, "Keep in Place", _keep).pack(side=tk.RIGHT, padx=6)
                self._flat_button(btn_frame, "Cancel", _cancel,
                                  fg="#f38ba8").pack(side=tk.RIGHT, padx=6)

                self._center_window(dlg)
                dlg.protocol("WM_DELETE_WINDOW", _cancel)
                dlg.mainloop()
                move_event.set()

            t2 = threading.Thread(target=_show_move_dialog, daemon=True)
            t2.start()
            move_event.wait(timeout=60)

            if move_result[0] is None:
                event.set()
                return

            result[0] = (new_path, move_result[0])
            event.set()

        t = threading.Thread(target=_show, daemon=True)
        t.start()
        event.wait(timeout=120)

        if result[0] is None:
            return

        new_path, move_files = result[0]

        if move_files:
            try:
                new_path.mkdir(parents=True, exist_ok=True)
                for item in current.iterdir():
                    dest = new_path / item.name
                    if not dest.exists():
                        shutil.move(str(item), str(dest))
                    else:
                        logger.warning(f"Skipped (already exists): {item.name}")
                logger.info(f"Moved recordings from {current} → {new_path}")
            except Exception as e:
                logger.error(f"Failed to move files: {e}")
                self._show_error_popup("Move Failed", f"Could not move files:\n{e}")
                return

        self.cfg["output_dir"] = str(new_path)
        self._save_and_refresh()
        logger.info(f"Output folder changed to: {new_path}")

    def _build_session_stats_menu(self):
        """Build weekly stats submenu with today and wk columns."""
        s = self._stats
        uptime = datetime.now() - s["session_start"]
        hours, remainder = divmod(int(uptime.total_seconds()), 3600)
        minutes = remainder // 60
        avg_dict_time = s["total_dictation_time"] / s["dictations"] if s["dictations"] else 0

        week = weekly_stats.get_current_week()
        lifetime = weekly_stats.get_lifetime()
        weekly_avg = weekly_stats.get_weekly_average("total_dictation_time")

        def _row(label, today_val, week_val):
            """Format with tab for Windows menu column alignment."""
            return f"{label}\t{today_val} - {week_val}"

        items = [
            pystray.MenuItem(f"Uptime\t{hours}h {minutes}m", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(_row("Dictations", s['dictations'], week.get('dictations', 0)), None, enabled=False),
            pystray.MenuItem(_row("Avg dictation", f"{avg_dict_time:.1f}s", f"{weekly_avg:.1f}s"), None, enabled=False),
            pystray.MenuItem(_row("Chars", f"{s['total_dictation_chars']:,}", f"{week.get('total_dictation_chars', 0):,}"), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(_row("Meetings", s['meetings'], week.get('meetings', 0)), None, enabled=False),
            pystray.MenuItem(_row("Meeting time", f"{s['total_meeting_seconds'] // 60}m", f"{week.get('total_meeting_seconds', 0) // 60}m"), None, enabled=False),
            pystray.MenuItem(_row("Meeting words", f"{s['total_meeting_words']:,}", f"{week.get('total_meeting_words', 0):,}"), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(_row("Features", s['feature_suggestions'], week.get('feature_suggestions', 0)), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(f"Lifetime dictations\t{lifetime.get('dictations', 0):,}", None, enabled=False),
            pystray.MenuItem(f"Lifetime meetings\t{lifetime.get('meetings', 0):,}", None, enabled=False),
        ]
        return pystray.Menu(*items)

    def _set_log_level(self, tier: str):
        self.cfg["log_window"] = tier
        set_console_level(tier)
        self._save_and_refresh()
        logger.info(f"Log window set to: {tier}")

    def _set_api_filter(self, api_name: str | None):
        self._api_filter = api_name
        self._refresh_menu()

    def _set_device(self, key: str, device_id: int):
        self.cfg[key] = device_id
        self._save_and_refresh()

    def _toggle_system_devices(self):
        self.cfg["use_system_devices"] = not self.cfg.get("use_system_devices", True)
        self._save_and_refresh()

    def _set_hotkey(self, key: str, hotkey: str):
        old = self.cfg["hotkeys"].get(key)
        if old == hotkey:
            return
        self.cfg["hotkeys"][key] = hotkey
        self._save_and_refresh()
        self._restart()

    def _set_paste_method(self, method: str):
        self.cfg["paste_method"] = method
        self._save_and_refresh()

    def _set_click(self, key: str, action: str):
        self.cfg[key] = action
        self._save_and_refresh()

    def _set_compute_device(self, device: str):
        """Switch compute device (auto/gpu/cpu) and restart the worker."""
        old = self.cfg.get("device", "auto")
        if old == device:
            return

        # Check if the resolved device is actually changing
        # e.g. Auto->GPU when auto already uses GPU = no restart needed
        def _resolve(d):
            if d in ("gpu", "cuda"):
                return "cuda"
            if d == "cpu":
                return "cpu"
            # auto: check if GPU available
            try:
                import torch
                return "cuda" if torch.cuda.is_available() else "cpu"
            except ImportError:
                return "cpu"

        old_resolved = _resolve(old)
        new_resolved = _resolve(device)

        self.cfg["device"] = device
        self._save_and_refresh()

        if old_resolved == new_resolved:
            logger.info(f"Device setting: {old} -> {device} (same hardware, no restart)")
            return

        logger.info(f"Switching device: {old} -> {device} ({old_resolved} -> {new_resolved})")
        self.worker.update_config(dict(self.cfg))
        _previous_device = old
        def _do_restart():
            self.worker.restart()
            logger.info(f"Worker restarted on {new_resolved}")
            # #39: Toast confirming device switch with Switch Back button
            try:
                def _switch_back(prev=_previous_device):
                    self._set_compute_device(prev)
                notify(
                    "Device switched",
                    f"Now using {new_resolved}",
                    buttons=[{"label": "Switch Back", "action": _switch_back}],
                )
            except Exception:
                pass  # toast is best-effort
        threading.Thread(target=_do_restart, daemon=True).start()

    def _get_device_label(self) -> str:
        """Return display string for the active resolved device."""
        device_setting = self.cfg.get("device", "auto")
        if device_setting == "cpu":
            return "CPU"
        elif device_setting in ("gpu", "cuda"):
            try:
                from .transcribe import get_gpu_name
                gpu_name = get_gpu_name()
                return gpu_name if gpu_name else "GPU"
            except Exception:
                return "GPU"
        else:  # auto
            try:
                from .transcribe import get_gpu_name
                gpu_name = get_gpu_name()
                if gpu_name:
                    return f"Auto ({gpu_name})"
                return "Auto (CPU)"
            except Exception:
                return "Auto"

    def _toggle_always_available_dictation(self):
        self.cfg["always_available_dictation"] = not self.cfg.get("always_available_dictation", True)
        state = "enabled" if self.cfg["always_available_dictation"] else "disabled"
        logger.info(f"Always Available Dictation: {state}")
        if not self.cfg["always_available_dictation"]:
            self._backup.stop()
        self._save_and_refresh()

    def _set_backup_device(self, device: str):
        if self.cfg.get("backup_device", "auto") == device:
            return
        self.cfg["backup_device"] = device
        logger.info(f"Backup device: {device}", extra={"secondary": True})
        self._backup.stop()
        self._backup.preload()
        self._save_and_refresh()

    def _set_backup_model(self, model_name: str):
        if self.cfg.get("backup_model", "base") == model_name:
            return
        self.cfg["backup_model"] = model_name
        logger.info(f"Backup model: {model_name}", extra={"secondary": True})
        self._backup.stop()
        self._backup.preload()
        self._save_and_refresh()

    def _set_diarize_method(self, slot_key: str, method_id: str):
        """Set a diarization slot, swapping with any slot that already has this method."""
        from .transcribe import DIARIZE_METHODS
        current = self.cfg.get(slot_key, "balanced_mix")
        if current == method_id:
            return
        # Find if another slot already uses this method and swap
        all_slots = ["diarize_primary", "diarize_fallback", "diarize_last_resort"]
        for other_slot in all_slots:
            if other_slot != slot_key and self.cfg.get(other_slot, "balanced_mix") == method_id:
                self.cfg[other_slot] = current  # swap
                break
        self.cfg[slot_key] = method_id
        label = DIARIZE_METHODS.get(method_id, method_id)
        logger.info(f"Diarization {slot_key}: {label}", extra={"secondary": True})
        self._save_and_refresh()

    def _set_model(self, key: str, model_name: str):
        logger.info(f"Setting {key} = {model_name}")
        if self.cfg.get(key) == model_name:
            return
        self.cfg[key] = model_name
        self._save_and_refresh()
        # Reload model in the appropriate worker subprocess
        if key == "dictation_model":
            threading.Thread(
                target=lambda: self.worker.reload_model(model_name),
                daemon=True,
            ).start()

    def _model_menu_items(self) -> list:
        """Build model status menu items."""
        meeting_status = get_model_status(self.cfg["model"])
        dict_model = self.cfg.get("dictation_model", self.cfg["model"])
        dict_status = get_model_status(dict_model)
        items = []

        # Meeting model
        m_label = f"Meeting Model: {self.cfg['model']}"
        if meeting_status["model_downloaded"]:
            m_label += f" ({meeting_status['model_size']})"
        else:
            m_label += " (not downloaded)"
        items.append(pystray.MenuItem(m_label, None, enabled=False))

        # Dictation model
        d_label = f"Dictation Model: {dict_model}"
        if dict_status["model_downloaded"]:
            d_label += f" ({dict_status['model_size']})"
        else:
            d_label += " (not downloaded)"
        items.append(pystray.MenuItem(d_label, None, enabled=False))

        # Word timing model (used to sync words to exact timestamps)
        align_label = "Word Timing: " + ("ready" if meeting_status["alignment_downloaded"] else "not downloaded")
        items.append(pystray.MenuItem(align_label, None, enabled=False))

        # GPU / Device
        device_pref = self.cfg.get("device", "auto")
        if device_pref == "cpu":
            gpu_label = "Device: CPU (forced)"
        elif meeting_status["cuda_available"]:
            gpu_label = f"GPU: {meeting_status['cuda_device']}"
        else:
            gpu_label = "GPU: None (CPU mode)"
        items.append(pystray.MenuItem(gpu_label, None, enabled=False))
        items.append(pystray.MenuItem(f"CPU: {self._cpu_name}", None, enabled=False))

        # Download if missing
        needs_download = (
            not meeting_status["model_downloaded"]
            or not dict_status["model_downloaded"]
            or not meeting_status["alignment_downloaded"]
        )
        if needs_download:
            items.append(pystray.MenuItem(
                "Download Models Now",
                self._cb(self._download_model),
            ))

        return items

    def _download_model(self):
        """Download model in background thread with icon feedback."""
        if self.state.current.mode is not None:
            return

        self.state.emit(MODEL_DOWNLOADING, mode="transcribing", data={"model_name": self.cfg["model"]})

        def _do_download():
            try:
                ok = download_model(self.cfg["model"])
                if ok:
                    logger.info("Model download complete")
                    self.state.emit(MODEL_READY, mode="done", data={"model_name": self.cfg["model"]})
                else:
                    logger.error("Model download failed")
                    self.state.emit(ERROR, mode="error", data={"message": "Model download failed"})
            except Exception as e:
                logger.error(f"Model download error: {e}")
                self.state.emit(ERROR, mode="error", data={"message": "Model download failed"})
            self._schedule_idle(3)
            self._refresh_menu()

        threading.Thread(target=_do_download, daemon=True).start()

    _updating = False  # Guard against concurrent update clicks

    def _update(self, branch="dev"):
        """Pull latest code from a branch and restart if updated."""
        if self._updating:
            logger.debug("Update already in progress, ignoring")
            return
        self._updating = True

        def _do_update():
            import subprocess as _sp
            repo_root = str(get_install_root())

            try:
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
            self._post_queue.put(None)
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
            os._exit(0)

        threading.Thread(target=_do_restart, daemon=True).start()

    def quit(self):
        """Quit WhisperSync. Deferred like _restart for the same reason."""
        def _do_quit():
            import time
            time.sleep(self._CALLBACK_DEFER_SECS)
            self._cleanup()
            if self.tray:
                self.tray.stop()
        threading.Thread(target=_do_quit, daemon=True).start()

    @staticmethod
    def _prompt_large_download(model_name: str, size: str) -> bool:
        """Show a tkinter dialog asking if user wants to download a large model."""
        result = [False]
        event = threading.Event()

        def _show():
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            answer = messagebox.askyesno(
                "WhisperSync: Large Download",
                f"Model '{model_name}' ({size}) is not cached locally.\n\n"
                f"Download now?\n\n"
                f"Warning: This is a large download.\n"
                f"Skip if you are on mobile data.",
            )
            result[0] = answer
            root.destroy()
            event.set()

        t = threading.Thread(target=_show, daemon=True)
        t.start()
        event.wait(timeout=120)
        return result[0]

    def run(self):
        # Clear any orphaned keyboard hooks from a previous crash
        # This prevents the stuck-Ctrl-key bug when restarting after abnormal exit
        try:
            keyboard.unhook_all()
        except Exception:
            pass

        # Check for orphaned temp WAV files from a previous crash (meeting)
        self._recovered_meeting_paths = []
        temp_dir = self._meeting_temp_dir()
        for name in ("mic-temp.wav", "speaker-temp.wav"):
            temp_path = temp_dir / name
            if temp_path.exists():
                dur = fix_orphan(temp_path)
                if dur is not None:
                    logger.warning(
                        f"Recovered {dur:.0f}s of {name.split('-')[0]} audio "
                        f"from previous crash — file at {temp_dir / name}"
                    )
                    if name == "mic-temp.wav":
                        self._recovered_meeting_paths.append((str(temp_path), dur))
                else:
                    logger.info(f"Cleaned up stale temp file: {name}")

        # Check for orphaned dictation WAVs from a previous crash
        # (successful dictations delete the WAV, so any .wav here = crash)
        self._recovered_dictation_paths = []
        dict_log_dir = self._dictation_log_dir()
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

        keyboard.add_hotkey(
            self.cfg["hotkeys"]["dictation_toggle"],
            self.toggle_dictation,
            suppress=False,
        )
        keyboard.add_hotkey(
            self.cfg["hotkeys"]["meeting_toggle"],
            self.toggle_meeting,
            suppress=False,
        )
        feature_hk = self.cfg["hotkeys"].get("feature_suggest", "ctrl+shift+alt+f")
        if feature_hk:
            keyboard.add_hotkey(
                feature_hk,
                self.toggle_feature_suggest,
                suppress=False,
            )

        self.tray = pystray.Icon(
            "whisper-sync",
            idle_icon(),
            "WhisperSync: Idle",
            menu=self._build_menu(),
        )

        self.state = StateManager(self.tray, self.cfg)

        # Start the post-processing worker (single thread, sequential meeting processing)
        self._post_worker_thread = threading.Thread(
            target=self._post_process_worker, daemon=True, name="post-process-worker")
        self._post_worker_thread.start()
        logger.info("Post-processing worker started")

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

        dictation_model = self.cfg.get("dictation_model", self.cfg["model"])
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
                # Recover any crashed dictations/features found at startup
                for wav_path in self._recovered_dictation_paths:
                    if Path(wav_path).name.startswith("feature_"):
                        self._recover_feature(wav_path)
                    else:
                        self._recover_dictation(wav_path)
                self._recovered_dictation_paths = []
                # Recover any crashed meetings — show dialog for naming
                if self._recovered_meeting_paths:
                    self._recover_meetings()
            else:
                logger.warning("Transcription worker failed to start — dictation may not work")

        threading.Thread(target=_wait_worker, daemon=True).start()

        # Start GitHub PR status polling if configured
        self._start_github_poller()

        # Periodic flush for persistent weekly stats
        self._stats_flush_stop = threading.Event()
        def _stats_flush_loop():
            while not self._stats_flush_stop.wait(weekly_stats._flush_interval):
                try:
                    weekly_stats.flush()
                except Exception:
                    pass
        threading.Thread(target=_stats_flush_loop, daemon=True).start()

        try:
            self.tray.run()
        finally:
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
            if self._github_poller:
                self._github_poller.stop()


def main():
    # Enable faulthandler FIRST so native segfaults get logged to the log file
    try:
        faulthandler.enable(file=open(get_log_path(), "a"), all_threads=True)
    except Exception:
        faulthandler.enable()  # fallback to stderr
    try:
        logger.info("=== WhisperSync starting ===")
        install_excepthook(logger)
        check_previous_crash(logger)
        app = WhisperSync()
        app.run()
    except Exception:
        import traceback
        logger.critical(f"FATAL CRASH:\n{traceback.format_exc()}")
        # Last-resort hook cleanup — prevents stuck Ctrl key on crash
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
