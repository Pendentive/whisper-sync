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
from datetime import datetime
from pathlib import Path

import keyboard
import pystray

from . import config
from .config_store import ConfigStore
from .executors import DICTATION, IO, submit_or_spawn
from .idle_reset import schedule_idle_reset
from .capture import AudioRecorder, get_default_devices, get_host_apis, list_devices, save_wav, save_stereo_wav
from .icons import (idle_icon, build_icon, resolve_icon_key, ICON_REGISTRY,
                     IconAnimator)
from .logger import logger, get_log_path, set_console_level
from .model_status import get_model_status, download_model, bootstrap_models
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
from .flatten import flatten as flatten_transcript
from .notifications import notify, ToastListener
from .state_manager import (
    StateManager, AppState,
    MEETING_STARTED, MEETING_STOPPED, MEETING_COMPLETED,
    TRANSCRIPTION_STARTED, TRANSCRIPTION_PROGRESS, TRANSCRIPTION_COMPLETED,
    ERROR, MODEL_LOADING, MODEL_READY, MODEL_DOWNLOADING,
    PR_STATUS_CHANGED, SPEAKER_HEALTH_CHANGED, QUEUED, IDLE,
)
from .meeting_job import MeetingJob
from .dictation_flow import DictationFlow
from .meeting_dialogs import (MeetingDialogs, ABORT, sanitize_name,
                              style_window, flat_button, center_window,
                              run_modal)
from .rebuild_index import rebuild_root_index
from .speakers import identify_speakers, write_speaker_map, update_config, get_config_path
from .dialog_dispatcher import DialogDispatcher

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
        self.cfg = ConfigStore(config.load())
        set_console_level(self.cfg.get("log_window", "normal"))
        self.recorder = AudioRecorder(sample_rate=self.cfg["sample_rate"])
        self.tray = None
        self.state = None  # Initialized after tray creation in run()
        self._menu_refresher = None  # MenuRefresher, created in run()
        self._lock = threading.RLock()
        self._tray_lock = threading.Lock()  # Serialize all tray icon/title updates (pystray not thread-safe)
        self._api_filter = "Windows WASAPI"  # None = show all
        self._meeting_start_time: datetime | None = None
        self._gpu_guard = GpuGuard(self.cfg, notify=notify)
        dictation_model = self._gpu_guard.effective_model(
            self.cfg.get("dictation_model", self.cfg["model"]))
        self.worker = TranscriptionWorker(self.cfg, preload_model=dictation_model)
        self._backup = BackupTranscriber(self.cfg)
        self._github_poller = None
        self._github_prs = []
        self._post_queue = queue.Queue()  # Post-processing queue for completed meetings
        self._post_worker_thread = None  # Started in run()
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
        # Flash gate: lock-guarded check-and-set (Event.is_set()+set() alone
        # is not atomic; two hotkey threads could both observe False).
        self._flash_lock = threading.Lock()
        self._flash_active = threading.Event()
        # Dictation workflow component (dictation_flow.py): dictation,
        # overlay, feature-suggest, discard, recovery, history. The app
        # remains the wiring surface (hardening item 6).
        self.dictation = DictationFlow(self)
        # Meeting dialog component (meeting_dialogs.py): Tk builders run
        # on the dialog dispatcher; behavior extracted from this class.
        self.dialogs = MeetingDialogs(self)

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
        """Universal loading/queuing signal: two quick yellow flashes (150ms on/off/on)."""
        # Lock-guarded check-and-set: the old getattr-default pattern (and
        # a bare Event is_set/set pair) raced concurrent hotkey threads -
        # both could observe "not flashing" and both start animations.
        with self._flash_lock:
            if self._flash_active.is_set():
                return
            self._flash_active.set()
        animator = IconAnimator(self.tray, lock=self._tray_lock)
        animator.flash(count=2, interval_ms=150)
        # Reset after the animation completes (~600ms) on the shared
        # scheduler instead of spawning a sleep thread per flash.
        from .scheduler import scheduler
        scheduler.call_later(0.7, self._flash_active.clear, label="flash-reset")

    # --- Click dispatch ---

    def _dispatch_action(self, action: str):
        if action == "meeting":
            self.toggle_meeting()
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

    def _flash_queued(self):
        """Rapid amber flash to indicate dictation is queued behind a meeting stage."""
        animator = IconAnimator(self.tray, lock=self._tray_lock)
        animator.flash_between("queued", "transcribing", count=2, interval_ms=150)

    def _can_record(self) -> bool:
        """Can we start a new recording? Allowed if idle or just transcribing in background."""
        mode = self.state.current.mode if self.state else None
        return mode is None or mode in ("transcribing", "done", "error")

    def _recover_meetings(self):
        """Show a dialog for each recovered meeting WAV, let user name and place it."""
        for wav_path, duration in self._recovered_meeting_paths:
            mins = int(duration // 60)
            secs = int(duration % 60)
            name = self.dialogs.ask_recovery_name(wav_path, f"{mins}m {secs}s")
            if name is ABORT:
                logger.info(f"Recovery skipped for {wav_path} - file preserved")
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
                # Load transcript.json ONCE at thread entry, before any other
                # allocations or GC pressure. write_speaker_map requires a
                # pre-parsed dict on background threads to avoid the Windows
                # json.load / GC interleave crash (0x80000003). See
                # speakers.write_speaker_map docstring for the full rationale.
                transcript_data = None
                try:
                    with open(json_path) as _f:
                        transcript_data = _json.load(_f)
                except Exception as e:
                    logger.warning(
                        f"Recovery: could not pre-load transcript.json for {meeting_dir.name}: {e}"
                    )
                    return

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

                self._current_meeting_json_path = str(json_path)
                confirmed_map = self.dialogs.ask_speaker_confirmation(id_result)
                if not confirmed_map:
                    logger.info("Recovery: speaker identification skipped by user")
                    return

                # Write speaker map. Pass the pre-parsed dict so write_speaker_map
                # does not perform json.load on this background thread.
                write_speaker_map(str(json_path), confirmed_map, transcript_data=transcript_data)
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

    def toggle_meeting(self):
        with self._lock:
            mode = self.state.current.mode if self.state else None
            if mode == "meeting":
                self._stop_meeting()
            elif self._can_record():
                self._start_meeting()

    def _start_meeting(self):
        # Atomic claim, same rationale as _start_dictation.
        if not self.state.try_transition(
            (None, "transcribing", "done", "error"),
            MEETING_STARTED, mode="meeting",
        ):
            logger.debug("meeting start rejected: mode changed concurrently")
            return
        self._meeting_start_time = datetime.now()
        mic = self.cfg.get("mic_device")
        speaker = self.cfg.get("speaker_device")
        if self.cfg.get("use_system_devices", True):
            defaults = get_default_devices()
            # Route mic through WASAPI too (not sd.default.device[0]'s MME)
            mic = defaults.get("input")
            speaker = defaults["output"]
        elif speaker is None:
            defaults = get_default_devices()
            speaker = defaults["output"]
        try:
            self.recorder.start(mic_device=mic, speaker_device=speaker)
        except Exception as e:
            logger.error("Failed to start mic for meeting: %s", e, exc_info=True)
            notify("Meeting unavailable", f"Mic could not be opened: {e}")
            self.state.emit(IDLE, mode=None)
            return
        if self.recorder.speaker_loopback_active:
            logger.info("Meeting started: mic + speaker loopback")
        else:
            logger.warning("Meeting started: mic only (speaker loopback unavailable)")
        temp = self._meeting_temp_dir()
        mic_temp = temp / "mic-temp.wav"
        try:
            self.recorder.start_streaming(mic_temp, disk_only=True)
        except Exception as e:
            logger.warning("Meeting streaming disabled: %s", e)
        if self.cfg.get("always_available_dictation", True):
            self._backup.preload()

    def _is_claude_cli_available(self) -> bool:
        """Check if Claude CLI is available for minutes generation."""
        import subprocess as _sp
        from .executors import native_call
        try:
            with native_call("claude-version"):
                r = _sp.run(["claude", "--version"], capture_output=True, text=True, timeout=5)
            return r.returncode == 0
        except (FileNotFoundError, _sp.TimeoutExpired):
            return False

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
            from .executors import native_call
            with native_call("claude-name-suggest"):
                result = _sp.run(
                    ["claude", "-p", "--model", "haiku"],
                    input=prompt, capture_output=True, text=True, timeout=30,
                )
            if result.returncode == 0 and result.stdout.strip():
                lines = [l.strip().strip("-").strip() for l in result.stdout.strip().splitlines()]
                # Sanitize and filter
                suggestions = []
                for line in lines:
                    name = sanitize_name(line.lower())
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
            dialog_result = self.dialogs.ask_meeting_name()

            if dialog_result is ABORT:
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

                # Speaker channel may arrive as a disk path (disk-streamed
                # at target rate — the flat-RAM path) or as an in-memory
                # array (legacy/RAM fallback). Normalize to an array here;
                # this read is the only transient large allocation left.
                speaker_arr = None
                if "speaker_path" in audio:
                    from .streaming_wav import StreamingWavWriter
                    speaker_arr = StreamingWavWriter.read_audio_from(
                        audio["speaker_path"]
                    ).reshape(-1, 1)
                elif "speaker" in audio:
                    speaker_arr = audio["speaker"]

                if "mic_path" in audio:
                    # Disk-only mode: mic audio already on disk as streaming WAV
                    mic_wav_path = audio["mic_path"]
                    if speaker_arr is not None:
                        from .streaming_wav import StreamingWavWriter
                        mic_array = StreamingWavWriter.read_audio_from(mic_wav_path)
                        save_stereo_wav(str(wav_path), mic_array.reshape(-1, 1), speaker_arr, self.cfg["sample_rate"])
                    else:
                        import shutil
                        shutil.move(str(mic_wav_path), str(wav_path))
                elif speaker_arr is not None:
                    save_stereo_wav(str(wav_path), audio["mic"], speaker_arr, self.cfg["sample_rate"])
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

        submit_or_spawn(IO, "meeting-save-enqueue", _save_and_enqueue)

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
                # NOTE: do NOT call gc.collect() here. The cycle collector
                # is process-wide and runs on the calling thread's stack;
                # it can race with native C calls in any OTHER thread
                # (subprocess.communicate for Claude CLI, multiprocessing
                # queue feeders, pystray menu build) and crash with
                # 0x80000003. See PR #134 which introduced this regression.

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
            self._gpu_guard.note_pressure_trigger("worker_crash_meeting")
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
            err_str = str(e).lower()
            if "winerror 2" in err_str or "ffmpeg" in err_str:
                import shutil
                if not shutil.which("ffmpeg"):
                    self._show_error_popup(
                        "FFmpeg Not Found",
                        "FFmpeg is required for audio processing but is not installed or not on PATH.\n\n"
                        "Install with: winget install Gyan.FFmpeg\n"
                        "Then restart WhisperSync.",
                    )
                else:
                    self._show_error_popup("File Not Found", str(e))
            elif "hf" in err_str or "huggingface" in err_str or "token" in err_str:
                self._show_error_popup("Hugging Face Token Missing", str(e))
            else:
                self._show_error_popup("File Not Found", str(e))
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

    def _generate_minutes(
        self,
        meeting_dir: Path,
        readable_file: Path,
        minutes_file: Path,
        transcript_data: dict | None = None,
    ):
        """Generate minutes.md via Claude CLI (claude -p) using the shared prompt template.

        When ``transcript_data`` is provided, the parsed dict is used for the
        speaker_map lookup and ``json.load`` is skipped. This avoids a Windows
        fatal exception (``0x80000003``) that fires when ``json.load`` runs on
        a background thread (the post-processing pipeline thread) and CPython's
        GC interleaves with the C-level decoder.
        """
        import json
        import subprocess as _sp

        prompt_file = Path(__file__).parent / "minutes_prompt.md"
        if not prompt_file.exists():
            logger.warning(f"Minutes prompt template not found: {prompt_file}")
            return

        prompt_text = prompt_file.read_text(encoding="utf-8")
        transcript_text = readable_file.read_text(encoding="utf-8")

        # Build speaker context from transcript.json speaker_map + config roles.
        # Use the in-memory dict when supplied; otherwise fall back to a disk
        # read (e.g., when called from the recovery flow on a fresh thread).
        speaker_context = ""
        try:
            if transcript_data is not None:
                tdata = transcript_data
                smap = tdata.get("speaker_map", {})
            else:
                json_path = meeting_dir / "transcript.json"
                if json_path.exists():
                    with open(json_path) as f:
                        tdata = json.load(f)
                    smap = tdata.get("speaker_map", {})
                else:
                    smap = {}
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
            from .executors import native_call
            with native_call("claude-minutes"):
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

    def _build_recent_dictations_menu(self):
        """Build the Recent Dictations submenu items."""
        history = self.dictation.recent_history()
        if not history:
            return pystray.Menu(
                pystray.MenuItem("No dictations yet", None, enabled=False),
            )
        items = []
        for entry in reversed(history):
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
            pystray.MenuItem("Clear History", self._cb(self.dictation.clear_history))
        )
        return pystray.Menu(*items)

    def _build_meetings_menu(self):
        """Build the Meetings submenu showing recent meetings with speaker status."""
        import json as _json

        output_dir = self._output_dir()
        meeting_folders = []

        # Scan all week folders for meeting directories with transcript.json
        # NOTE: Do NOT parse transcript.json here. Reading JSON files during
        # menu builds (which run on background threads) triggers fatal access
        # violations when Python's garbage collector runs concurrently.
        # Instead, check for transcript-readable.txt as a lightweight indicator.
        for week_dir in sorted(output_dir.iterdir(), reverse=True):
            if not week_dir.is_dir() or week_dir.name.startswith("."):
                continue
            for meeting_dir in sorted(week_dir.iterdir(), reverse=True):
                if not meeting_dir.is_dir():
                    continue
                json_path = meeting_dir / "transcript.json"
                if json_path.exists():
                    readable = meeting_dir / "transcript-readable.txt"
                    minutes = meeting_dir / "minutes.md"
                    if minutes.exists():
                        status = "Complete"
                    elif readable.exists():
                        status = "Transcribed"
                    else:
                        status = "Processing"
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
        # Build per-option labels with GPU name from worker (avoids torch import in main process)
        device_options = []
        gpu_name = self.worker.gpu_name if self.worker else None
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
            pystray.MenuItem(f"Dictation\t{dict_hk}", lambda: self._on_left_click() if left_action == "dictation" else self.dictation.toggle(),
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
        """Request a debounced menu rebuild. Safe from any thread.

        The actual rebuild runs on the scheduler thread via MenuRefresher
        and the swap goes through _update_tray under _tray_lock. Burst
        requests (dictation completion fires history + stats + state
        refreshes back-to-back) coalesce into one rebuild.
        """
        refresher = getattr(self, "_menu_refresher", None)
        if refresher is not None and self.tray is not None:
            refresher.request()

    def _save_and_refresh(self):
        config.save(self.cfg.snapshot())
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
            # Refresh menu after the first poll completes: a scheduler
            # step chain (1s cadence, 30 tries) instead of a sleeping
            # thread. Each step is a cheap flag check.
            from .scheduler import scheduler as _sched

            def _first_poll_step(remaining: int):
                if self._github_poller.state.last_poll > 0:
                    self._github_prs = self._github_poller.state.prs
                    self._refresh_menu()
                    return
                if remaining > 0:
                    _sched.call_later(1.0, lambda: _first_poll_step(remaining - 1),
                                      label="github-first-poll")

            _first_poll_step(30)

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
        from .executors import native_call
        try:
            with native_call("gh-merge"):
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
        """Show a tkinter error dialog with the given message.

        Fire-and-forget: callers don't wait for the popup to close. We still
        run on the dispatcher thread to keep all tk.Tk() creation on a single
        consistent thread; we just don't block our caller. A small worker
        thread bridges the call so this method returns immediately as before.
        """
        def _show(_proot):
            from tkinter import messagebox
            # parent=persistent root: no Tk/Tcl interpreter churn.
            messagebox.showerror(f"WhisperSync: {title}", message, parent=_proot)

        def _dispatch():
            try:
                self._dialog_dispatcher.run(_show, label="error_popup", wants_root=True)
            except Exception:
                logger.exception("error popup failed: %s", title)

        threading.Thread(target=_dispatch, daemon=True).start()

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

        def _show(_proot):
            import tkinter as tk
            from tkinter import filedialog

            new_dir = filedialog.askdirectory(
                title="Choose output folder for recordings",
                initialdir=str(current) if current.exists() else str(Path.home()),
                parent=_proot,
            )

            if not new_dir or Path(new_dir) == current:
                return

            new_path = Path(new_dir)
            # Check if current folder has files to move
            has_files = current.exists() and any(current.iterdir())

            if not has_files:
                result[0] = (new_path, False)
                return

            # Ask about moving files. Run inline (we are already on the
            # dispatcher thread, so the dispatcher's reentrancy guard makes
            # this a same-thread call rather than a deadlock).
            move_result = [None]

            def _show_move_dialog(_mroot):
                dlg = tk.Toplevel(_mroot)
                dlg.title("WhisperSync")
                style_window(dlg)
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

                flat_button(btn_frame, "Move Files", _move,
                                  bg=accent, fg="#1e1e2e", hover_bg="#74c7ec",
                                  bold=True).pack(side=tk.RIGHT, padx=6)
                flat_button(btn_frame, "Keep in Place", _keep).pack(side=tk.RIGHT, padx=6)
                flat_button(btn_frame, "Cancel", _cancel,
                                  fg="#f38ba8").pack(side=tk.RIGHT, padx=6)

                center_window(dlg)
                dlg.protocol("WM_DELETE_WINDOW", _cancel)
                run_modal(_mroot, dlg)

            # Same-thread call via dispatcher (reentrancy guard runs inline).
            try:
                self._dialog_dispatcher.run(_show_move_dialog, label="change_output_move_dialog", wants_root=True)
            except Exception:
                logger.exception("change-output move dialog crashed")
                move_result[0] = None

            if move_result[0] is None:
                return

            result[0] = (new_path, move_result[0])

        try:
            self._dialog_dispatcher.run(_show, label="change_output_folder", wants_root=True)
        except Exception:
            logger.exception("change-output dialog crashed")
            return

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
        s = self._stats.snapshot()
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
        self.cfg.set_nested("hotkeys", key, hotkey)
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
        self.worker.update_config(self.cfg)
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
        gpu = self.worker.gpu_name if self.worker else None
        if device_setting == "cpu":
            return "CPU"
        elif device_setting in ("gpu", "cuda"):
            return gpu if gpu else "GPU"
        else:  # auto
            if gpu:
                return f"Auto ({gpu})"
            return "Auto (CPU)"

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
        with self.cfg.transaction():
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
            # DICTATION lane: a reload and a dictation cannot run
            # concurrently anyway, so serializing them is the semantics.
            submit_or_spawn(
                DICTATION, "dictation-model-reload",
                lambda m=model_name: self.worker.reload_model(m),
                native=True,
            )

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
            _guarded(self.toggle_meeting, "meeting"),
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
            menu=self._build_menu(),
        )

        # Debounced single-owner menu refresh (see tray_refresh.py). All
        # _refresh_menu() calls from any thread coalesce here; the rebuild
        # runs on the scheduler thread and swaps under _tray_lock.
        from .tray_refresh import MenuRefresher
        self._menu_refresher = MenuRefresher(
            build_menu=self._build_menu,
            apply_menu=lambda m: self._update_tray(menu=m),
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
            is_pipeline_idle=lambda: self._post_queue.unfinished_tasks == 0,
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
            if self._github_poller:
                self._github_poller.stop()
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
