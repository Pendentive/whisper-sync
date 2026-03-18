"""WhisperSync entry point — tray icon + hotkey listener."""

import faulthandler
import sys
import threading
import tempfile
from datetime import datetime
from pathlib import Path

import keyboard
import pystray

from . import config
from .capture import AudioRecorder, get_default_devices, get_host_apis, list_devices, save_wav, save_stereo_wav
from .icons import idle_icon, recording_icon, dictation_icon, saving_icon, transcribing_icon, done_icon, queued_icon, error_icon
from .logger import logger, get_log_path
from .model_status import get_model_status, download_model, bootstrap_models
from .paste import paste
from .paths import get_install_root, get_default_output_dir, is_standalone
from .worker_manager import TranscriptionWorker, WorkerCrashedError
from . import dictation_log
from .streaming_wav import fix_orphan
from .crash_diagnostics import install_excepthook, check_previous_crash
from .flatten import flatten as flatten_transcript
from .rebuild_index import rebuild_root_index

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



class WhisperSync:
    def __init__(self):
        self.cfg = config.load()
        self.recorder = AudioRecorder(sample_rate=self.cfg["sample_rate"])
        self.mode = None  # None, "dictation", "meeting", "saving", "transcribing", "done", "error"
        self._meeting_transcribing = False  # True while meeting transcription runs in background
        self.tray = None
        self._lock = threading.Lock()
        self._api_filter = "Windows WASAPI"  # None = show all
        self._dictation_wav_path: Path | None = None
        self._meeting_start_time: datetime | None = None
        dictation_model = self.cfg.get("dictation_model", self.cfg["model"])
        self.worker = TranscriptionWorker(self.cfg, preload_model=dictation_model)

    def _update_icon(self):
        if self.tray is None:
            return
        icons = {
            "meeting": (recording_icon(), "Recording meeting..."),
            "dictation": (dictation_icon(), "Dictating..."),
            "saving": (saving_icon(), "Saving audio..."),
            "transcribing": (transcribing_icon(), "Transcribing..."),
            "done": (done_icon(), "Done!"),
            "error": (error_icon(), "Error — check console"),
        }
        if self.mode:
            icon, title = icons[self.mode]
        elif self._meeting_transcribing:
            icon, title = transcribing_icon(), "Transcribing meeting..."
        else:
            icon, title = idle_icon(), "Idle"
        self.tray.icon = icon
        self.tray.title = f"WhisperSync: {title}"

    # --- Click dispatch ---

    def _dispatch_action(self, action: str):
        if action == "meeting":
            self.toggle_meeting()
        elif action == "dictation":
            self.toggle_dictation()

    def _on_left_click(self):
        # Left-click while dictating = discard (stop recording, throw away audio)
        if self.mode == "dictation":
            self._discard_dictation()
            return
        self._dispatch_action(self.cfg.get("left_click", "meeting"))

    def _on_middle_click(self):
        self._dispatch_action(self.cfg.get("middle_click", "dictation"))

    # --- Recording modes ---

    def _schedule_idle(self, seconds: float, blink: bool = False):
        """Return to idle after a delay. If blink=True, blink done 3 times first."""
        import time

        def _reset():
            if blink and self.mode == "done":
                for _ in range(3):
                    self.mode = "done"
                    self._update_icon()
                    time.sleep(0.4)
                    self.mode = None
                    self._update_icon()
                    time.sleep(0.3)
            else:
                time.sleep(seconds)
            self.mode = None
            self._update_icon()

        threading.Thread(target=_reset, daemon=True).start()

    def _flash_queued(self):
        """Rapid amber flash to indicate dictation is queued behind a meeting stage."""
        import time
        for _ in range(2):
            self.tray.icon = queued_icon()
            self.tray.title = "WhisperSync: Queued..."
            time.sleep(0.15)
            self.tray.icon = transcribing_icon()
            self.tray.title = "WhisperSync: Transcribing..."
            time.sleep(0.15)

    def _can_record(self) -> bool:
        """Can we start a new recording? Allowed if idle or just transcribing in background."""
        return self.mode is None or self.mode in ("transcribing", "done", "error")

    def toggle_dictation(self):
        with self._lock:
            if self.mode == "dictation":
                self._stop_dictation()
            elif self._can_record():
                self._start_dictation()

    def _dictation_log_dir(self) -> Path:
        return Path(__file__).parent / "logs" / "data" / "dictation"

    def _start_dictation(self):
        if not self.worker.is_ready():
            logger.warning("Worker not ready yet — ignoring dictation request")
            return
        self.mode = "dictation"
        self._update_icon()
        mic = self.cfg.get("mic_device")
        if self.cfg.get("use_system_devices", True):
            mic = None
        self.recorder.start(mic_device=mic)
        # Stream to disk in the daily dictation log folder so audio survives a crash
        log_dir = self._dictation_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self._dictation_wav_path = log_dir / f"{ts}.wav"
        self.recorder.start_streaming(self._dictation_wav_path)

    def _stop_dictation(self):
        audio = self.recorder.stop()
        self.recorder.stop_streaming()

        if "mic" not in audio:
            self.mode = None
            self._update_icon()
            return

        self.mode = "transcribing"
        self._update_icon()

        dictation_model = self.cfg.get("dictation_model", self.cfg["model"])

        def _process():
            import time as _time

            # If meeting is transcribing, flash queued icon — worker handles
            # dictation between meeting stages but there may be a brief wait
            if self._meeting_transcribing:
                self._flash_queued()

            t0 = _time.perf_counter()
            try:
                # Longer timeout when queued behind a meeting stage
                timeout = 180 if self._meeting_transcribing else 60
                text = self.worker.transcribe_fast(audio["mic"], model_override=dictation_model, timeout=timeout)
                t1 = _time.perf_counter()
                logger.info(f"transcribe_fast: {t1 - t0:.2f}s")
                if text:
                    paste(text, self.cfg["paste_method"])
                t2 = _time.perf_counter()
                logger.info(f"total (stop -> paste): {t2 - t0:.2f}s")
                if text:
                    dictation_log.append(text, t2 - t0)
                # Success — remove crash-safety WAV (text is in the .md log)
                if self._dictation_wav_path and self._dictation_wav_path.exists():
                    self._dictation_wav_path.unlink(missing_ok=True)
                self.mode = "done"
                self._update_icon()
            except WorkerCrashedError:
                logger.error("Worker crashed during dictation — respawning...")
                logger.info(f"Dictation audio preserved at: {self._dictation_wav_path}")
                self.worker.restart()
                self.mode = "error"
                self._update_icon()
            except Exception as e:
                logger.error(f"Dictation error: {e}")
                import traceback
                logger.debug(traceback.format_exc())
                self.mode = "error"
                self._update_icon()
            finally:
                self._schedule_idle(2)

        threading.Thread(target=_process, daemon=True).start()

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
                dictation_log.append(text, 0)
                logger.info(f"Crash-recovered dictation copied to clipboard: {text[:80]}...")
            else:
                logger.info("Crash-recovered dictation produced no text")
            # Clean up the WAV now that text is on clipboard + in the .md log
            Path(wav_path).unlink(missing_ok=True)
        except Exception as e:
            logger.error(f"Failed to recover dictation: {e}")
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
                    self._meeting_transcribing = True
                    self._update_icon()
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
                    self._meeting_transcribing = False
                    if self.mode is None:
                        self.mode = "done"
                        self._update_icon()
                        self._schedule_idle(3, blink=True)
                    else:
                        self._update_icon()
            threading.Thread(target=_transcribe, daemon=True).start()
        self._recovered_meeting_paths = []

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
        """Discard current dictation — stop recording, throw away audio, return to idle."""
        with self._lock:
            if self.mode != "dictation":
                return
            self.recorder.stop()  # stop recording, discard the audio
            self.recorder.stop_streaming()
            if self._dictation_wav_path and self._dictation_wav_path.exists():
                self._dictation_wav_path.unlink(missing_ok=True)
            logger.info("Dictation discarded (left-click)")
            self.mode = None
            self._update_icon()

    def toggle_meeting(self):
        with self._lock:
            if self.mode == "meeting":
                self._stop_meeting()
            elif self._can_record():
                self._start_meeting()

    def _start_meeting(self):
        self.mode = "meeting"
        self._meeting_start_time = datetime.now()
        self._update_icon()
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
            logger.info("Meeting recording: mic + speaker loopback (stereo)")
        else:
            logger.warning("Meeting recording: mic only (speaker loopback unavailable)")
        temp = self._meeting_temp_dir()
        mic_temp = temp / "mic-temp.wav"
        self.recorder.start_streaming(mic_temp)

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

    def _is_claude_cli_available(self) -> bool:
        """Check if Claude CLI is available for minutes generation."""
        import subprocess as _sp
        try:
            r = _sp.run(["claude", "--version"], capture_output=True, text=True, timeout=5)
            return r.returncode == 0
        except (FileNotFoundError, _sp.TimeoutExpired):
            return False

    def _ask_meeting_name(self):
        """Show a popup to name the meeting.

        Returns:
            _ABORT: user clicked Discard
            (str, True): user clicked Save & Summarize
            (str, False): user clicked Save
        """
        result = [self._ABORT]
        event = threading.Event()

        def _show_dialog():
            import tkinter as tk
            from tkinter import ttk

            root = tk.Tk()
            root.title("WhisperSync")
            self._style_window(root)
            root.geometry("440x170")

            bg = "#1e1e2e"
            fg = "#cdd6f4"
            fg_dim = "#6c7086"
            accent = "#89b4fa"
            danger = "#f38ba8"
            entry_bg = "#313244"

            # Title
            tk.Label(root, text="Save Meeting Recording", font=("Segoe UI", 11, "bold"),
                     bg=bg, fg=fg).pack(pady=(14, 2))

            # Entry
            tk.Label(root, text="Meeting name (leave blank for default):",
                     font=("Segoe UI", 9), bg=bg, fg=fg_dim).pack(pady=(2, 4))
            entry = tk.Entry(root, width=48, font=("Segoe UI", 10),
                             bg=entry_bg, fg=fg, insertbackground=fg,
                             relief="flat", highlightthickness=1, highlightcolor=accent)
            entry.pack(padx=20, ipady=4)
            entry.focus_force()

            # Buttons — order: Discard (left), Save (middle), Save & Summarize (right)
            btn_frame = tk.Frame(root, bg=bg)
            btn_frame.pack(pady=(12, 10))

            def _sanitize():
                return self._sanitize_name(entry.get() or "")

            def _save_and_summarize(ev=None):
                result[0] = (_sanitize(), True)
                root.destroy()

            def _save_only():
                result[0] = (_sanitize(), False)
                root.destroy()

            def _abort():
                result[0] = self._ABORT
                root.destroy()

            entry.bind("<Return>", _save_and_summarize)
            entry.bind("<Escape>", lambda e: _abort())

            # Discard (left)
            tk.Button(btn_frame, text="Discard", command=_abort, width=10,
                      font=("Segoe UI", 9), bg="#45475a", fg=danger, activebackground="#585b70",
                      activeforeground=danger, relief="flat", cursor="hand2").pack(side=tk.LEFT, padx=6)
            # Save (middle)
            tk.Button(btn_frame, text="Save", command=_save_only, width=10,
                      font=("Segoe UI", 9), bg="#45475a", fg=fg, activebackground="#585b70",
                      activeforeground=fg, relief="flat", cursor="hand2").pack(side=tk.LEFT, padx=6)
            # Save & Summarize (right, primary)
            tk.Button(btn_frame, text="Save & Summarize", command=_save_and_summarize, width=16,
                      font=("Segoe UI", 9, "bold"), bg=accent, fg="#1e1e2e", activebackground="#74c7ec",
                      activeforeground="#1e1e2e", relief="flat", cursor="hand2").pack(side=tk.LEFT, padx=6)

            self._center_window(root)
            root.protocol("WM_DELETE_WINDOW", _abort)
            root.mainloop()
            event.set()

        t = threading.Thread(target=_show_dialog, daemon=True)
        t.start()
        event.wait(timeout=60)

        return result[0]

    def _ask_rename_suggestion(self, current_name: str, summary: str):
        """Show a popup suggesting a better meeting name based on minutes summary.

        Returns the chosen name string, or None to skip rename.
        """
        import re

        # Generate 3 suggestions from summary
        words = re.sub(r'[^a-zA-Z0-9\s]', '', summary).lower().split()
        stopwords = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
                     'of', 'with', 'by', 'is', 'was', 'are', 'were', 'be', 'been', 'has',
                     'had', 'have', 'that', 'this', 'from', 'not', 'all', 'can', 'will'}
        meaningful = [w for w in words if w not in stopwords and len(w) > 2]

        suggestions = []
        if len(meaningful) >= 5:
            suggestions.append("-".join(meaningful[:5]))  # First 5 words
        if len(meaningful) >= 3:
            suggestions.append("-".join(meaningful[:3]))  # Short version
        if len(meaningful) >= 7:
            suggestions.append("-".join(meaningful[2:7]))  # Mid-section
        # Fallback
        if not suggestions:
            suggestions = ["-".join(meaningful) if meaningful else current_name]
        # Ensure unique
        seen = set()
        suggestions = [s for s in suggestions if s not in seen and not seen.add(s)]

        result = [None]
        event = threading.Event()

        def _show():
            import tkinter as tk

            root = tk.Tk()
            root.title("WhisperSync: Rename Meeting")
            self._style_window(root)

            bg = "#1e1e2e"
            fg = "#cdd6f4"
            fg_dim = "#6c7086"
            accent = "#89b4fa"
            entry_bg = "#313244"
            card_bg = "#181825"

            # Calculate height based on number of suggestions
            height = 260 + (len(suggestions) * 28)
            root.geometry(f"520x{height}")

            # Title
            tk.Label(root, text="Rename Meeting", font=("Segoe UI", 11, "bold"),
                     bg=bg, fg=fg).pack(pady=(12, 2))

            # Summary preview
            summary_text = summary[:120] + ("..." if len(summary) > 120 else "")
            tk.Label(root, text=summary_text, wraplength=480,
                     font=("Segoe UI", 8), bg=bg, fg=fg_dim, justify="left").pack(padx=20, pady=(2, 8))

            # Current name
            current_frame = tk.Frame(root, bg=card_bg, highlightbackground="#313244", highlightthickness=1)
            current_frame.pack(fill="x", padx=20, pady=(0, 8))
            tk.Label(current_frame, text="Current:", font=("Segoe UI", 8),
                     bg=card_bg, fg=fg_dim).pack(anchor="w", padx=8, pady=(4, 0))
            tk.Label(current_frame, text=current_name, font=("Segoe UI Semibold", 9),
                     bg=card_bg, fg=fg).pack(anchor="w", padx=8, pady=(0, 4))

            # Suggestion buttons
            tk.Label(root, text="Suggestions (click to use):", font=("Segoe UI", 8),
                     bg=bg, fg=fg_dim).pack(anchor="w", padx=20, pady=(0, 2))

            entry = tk.Entry(root, width=55, font=("Segoe UI", 10),
                             bg=entry_bg, fg=fg, insertbackground=fg,
                             relief="flat", highlightthickness=1, highlightcolor=accent)
            entry.pack(padx=20, ipady=4)
            entry.insert(0, suggestions[0])
            entry.select_range(0, tk.END)
            entry.focus_force()

            # Clickable suggestion chips
            chip_frame = tk.Frame(root, bg=bg)
            chip_frame.pack(padx=20, pady=(4, 0), anchor="w")
            for i, sug in enumerate(suggestions):
                def _use_suggestion(s=sug):
                    entry.delete(0, tk.END)
                    entry.insert(0, s)
                    entry.select_range(0, tk.END)
                chip = tk.Label(chip_frame, text=sug, font=("Segoe UI", 8),
                                bg="#313244", fg=accent, padx=8, pady=2, cursor="hand2")
                chip.pack(side=tk.LEFT, padx=(0, 6), pady=2)
                chip.bind("<Button-1>", lambda e, s=sug: _use_suggestion(s))

            # Buttons
            btn_frame = tk.Frame(root, bg=bg)
            btn_frame.pack(pady=(12, 10))

            def _accept(ev=None):
                name = entry.get().strip()
                if name:
                    result[0] = self._sanitize_name(name)
                root.destroy()

            def _skip():
                result[0] = None
                root.destroy()

            entry.bind("<Return>", _accept)
            entry.bind("<Escape>", lambda e: _skip())

            tk.Button(btn_frame, text="Keep Original", command=_skip, width=14,
                      font=("Segoe UI", 9), bg="#45475a", fg=fg, activebackground="#585b70",
                      activeforeground=fg, relief="flat", cursor="hand2").pack(side=tk.LEFT, padx=6)
            tk.Button(btn_frame, text="Rename", command=_accept, width=14,
                      font=("Segoe UI", 9, "bold"), bg=accent, fg="#1e1e2e", activebackground="#74c7ec",
                      activeforeground="#1e1e2e", relief="flat", cursor="hand2").pack(side=tk.LEFT, padx=6)

            self._center_window(root)
            root.protocol("WM_DELETE_WINDOW", _skip)
            root.mainloop()
            event.set()

        t = threading.Thread(target=_show, daemon=True)
        t.start()
        event.wait(timeout=120)

        return result[0]

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

            tk.Button(root, text="OK", command=_ok, width=10,
                      font=("Segoe UI", 9), bg="#45475a", fg=fg, activebackground="#585b70",
                      activeforeground=fg, relief="flat", cursor="hand2").pack()

            self._center_window(root)
            root.protocol("WM_DELETE_WINDOW", _ok)
            root.mainloop()
            event.set()

        t = threading.Thread(target=_show, daemon=True)
        t.start()
        event.wait(timeout=30)

        return result[0]

    def _stop_meeting(self):
        audio = self.recorder.stop()

        if "mic" not in audio:
            self.mode = None
            self._update_icon()
            return

        # Stay in a processing state so clicks are ignored
        self.mode = "saving"
        self._update_icon()

        # Run the entire post-recording flow in a thread so we release the lock
        def _post_record():
            dialog_result = self._ask_meeting_name()

            if dialog_result is self._ABORT:
                logger.info("Recording discarded")
                self.recorder.discard_streaming()
                self.mode = None
                self._update_icon()
                return

            meeting_name, do_summarize = dialog_result
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
                if "speaker" in audio:
                    save_stereo_wav(str(wav_path), audio["mic"], audio["speaker"], self.cfg["sample_rate"])
                else:
                    save_wav(str(wav_path), audio["mic"], self.cfg["sample_rate"])

                logger.info(f"WAV saved: {wav_path}")
                from .streaming_wav import cleanup_temp_files
                cleanup_temp_files(self._meeting_temp_dir())

                # Release mode so user can dictate while meeting transcribes
                self.mode = None
                self._meeting_transcribing = True
                self._update_icon()

                if not self.worker.is_alive():
                    logger.warning("Worker not alive — restarting...")
                    self.worker.restart()
                    if not self.worker.wait_ready(timeout=120):
                        raise RuntimeError("Worker failed to restart")
                result = self.worker.transcribe(str(wav_path), diarize=True)
                logger.info(f"Transcript saved: {result.get('json_path', wav_path)}")
                # Auto-flatten transcript for immediate readability
                try:
                    json_path = result.get('json_path')
                    if json_path:
                        readable_path = flatten_transcript(json_path)
                        if readable_path:
                            logger.info(f"Flattened transcript: {readable_path}")
                except Exception as e:
                    logger.warning(f"Auto-flatten failed (non-fatal): {e}")
                # Auto-generate minutes + rename suggestion (only if Save & Summarize)
                if do_summarize:
                    # Check LLM availability
                    llm_available = self._is_claude_cli_available()
                    if not llm_available:
                        suppress = self.cfg.get("suppress_llm_warning", False)
                        if not suppress:
                            dont_show = self._show_llm_unavailable()
                            if dont_show:
                                self.cfg["suppress_llm_warning"] = True
                                config.save(self.cfg)
                        logger.warning("Claude CLI not available — skipping summarize")
                    else:
                        # Step 1: Generate minutes if they don't already exist
                        try:
                            readable_file = meeting_dir / "transcript-readable.txt"
                            minutes_file = meeting_dir / "minutes.md"
                            if readable_file.exists() and not minutes_file.exists():
                                self._generate_minutes(meeting_dir, readable_file, minutes_file)
                        except Exception as e:
                            logger.warning(f"Auto-minutes failed (non-fatal): {e}")

                        # Step 2: Always offer rename if we have minutes with a summary
                        try:
                            minutes_file = meeting_dir / "minutes.md"
                            if minutes_file.exists():
                                summary = None
                                for line in minutes_file.read_text(encoding="utf-8").splitlines():
                                    if line.startswith("> Summary:"):
                                        summary = line[len("> Summary:"):].strip()
                                        break
                                if summary:
                                    new_name = self._ask_rename_suggestion(meeting_name or "meeting", summary)
                                    if new_name and new_name != meeting_name:
                                        new_folder_name = f"{date_time_str}_{new_name}"
                                        new_meeting_dir = meeting_dir.parent / new_folder_name
                                        if not new_meeting_dir.exists():
                                            import shutil
                                            shutil.move(str(meeting_dir), str(new_meeting_dir))
                                            logger.info(f"Renamed: {folder_name} -> {new_folder_name}")
                                            meeting_dir = new_meeting_dir
                                        else:
                                            logger.warning(f"Rename skipped — folder already exists: {new_folder_name}")
                                else:
                                    logger.info("No > Summary: line found in minutes — rename skipped")
                            else:
                                logger.info("No minutes.md found — rename skipped")
                        except Exception as e:
                            logger.warning(f"Rename suggestion failed (non-fatal): {e}")
                # Rebuild week + root INDEX.md
                try:
                    rebuild_root_index(self._output_dir())
                except Exception as e:
                    logger.warning(f"Index rebuild failed (non-fatal): {e}")
                self._meeting_transcribing = False
                # Flash "done" only if user is idle (not mid-dictation)
                if self.mode is None:
                    self.mode = "done"
                    self._update_icon()
                    self._schedule_idle(3, blink=True)
                else:
                    self._update_icon()
            except WorkerCrashedError:
                logger.error("Worker crashed during meeting — respawning...")
                logger.info(f"Audio is preserved at: {wav_path}")
                self.worker.restart()
                self._meeting_transcribing = False
                if self.mode is None:
                    self.mode = "error"
                    self._update_icon()
                    self._schedule_idle(3)
                else:
                    self._update_icon()
            except PermissionError as e:
                # Gated model access — show user the acceptance URLs
                logger.error(str(e))
                self._show_error_popup("Diarization Model Access", str(e))
                self._meeting_transcribing = False
                if self.mode is None:
                    self.mode = "error"
                    self._update_icon()
                    self._schedule_idle(3)
                else:
                    self._update_icon()
            except FileNotFoundError as e:
                # Missing HF token
                logger.error(str(e))
                self._show_error_popup("Hugging Face Token Missing", str(e))
                self._meeting_transcribing = False
                if self.mode is None:
                    self.mode = "error"
                    self._update_icon()
                    self._schedule_idle(3)
                else:
                    self._update_icon()
            except Exception as e:
                logger.error(f"Meeting transcription error: {e}")
                import traceback
                logger.debug(traceback.format_exc())
                self._meeting_transcribing = False
                if self.mode is None:
                    self.mode = "error"
                    self._update_icon()
                    self._schedule_idle(3)
                else:
                    self._update_icon()

        threading.Thread(target=_post_record, daemon=True).start()

    def _output_dir(self) -> Path:
        p = Path(self.cfg["output_dir"])
        if not p.is_absolute():
            if is_standalone():
                # Standalone: relative paths resolve under ~/Documents/WhisperSync/
                p = get_default_output_dir().parent / p
            else:
                # Repo mode: relative paths resolve from repo root
                p = get_install_root() / p
        return p

    def _generate_minutes(self, meeting_dir: Path, readable_file: Path, minutes_file: Path):
        """Generate minutes.md via Claude CLI (claude -p) using the shared prompt template."""
        import subprocess as _sp

        prompt_file = Path(__file__).parent / "minutes_prompt.md"
        if not prompt_file.exists():
            logger.warning(f"Minutes prompt template not found: {prompt_file}")
            return

        prompt_text = prompt_file.read_text(encoding="utf-8")
        transcript_text = readable_file.read_text(encoding="utf-8")

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
        filter_label = f"Device Filter ({self._api_filter or 'All'})"
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

        # Left-click fires the default menu item
        left_action = self.cfg.get("left_click", "meeting")
        return pystray.Menu(
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
            pystray.MenuItem("Open Output Folder", lambda: self._open_output_folder()),
            pystray.MenuItem("Settings", pystray.Menu(
                pystray.MenuItem(f"Dictation Hotkey ({self.cfg['hotkeys']['dictation_toggle']})",
                                 pystray.Menu(*dictation_hk_items)),
                pystray.MenuItem(f"Meeting Hotkey ({self.cfg['hotkeys']['meeting_toggle']})",
                                 pystray.Menu(*meeting_hk_items)),
                pystray.MenuItem(f"Paste Method ({self.cfg['paste_method']})",
                                 pystray.Menu(*paste_items)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(f"Left Click ({CLICK_ACTIONS.get(self.cfg.get('left_click', 'meeting'), 'meeting')})",
                                 pystray.Menu(*left_click_items)),
                pystray.MenuItem(f"Middle Click ({CLICK_ACTIONS.get(self.cfg.get('middle_click', 'dictation'), 'dictation')})",
                                 pystray.Menu(*middle_click_items)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(f"Dictation Model ({self.cfg.get('dictation_model', self.cfg['model'])})",
                                 pystray.Menu(*dictation_model_items)),
                pystray.MenuItem(f"Meeting Model ({self.cfg['model']})",
                                 pystray.Menu(*meeting_model_items)),
                pystray.Menu.SEPARATOR,
                *self._model_menu_items(),
            )),
            pystray.MenuItem("Restart", lambda: self._restart()),
            pystray.MenuItem("Quit", lambda: self.quit()),
        )

    # --- Actions ---

    def _refresh_menu(self):
        if self.tray:
            self.tray.menu = self._build_menu()
            self.tray.update_menu()

    def _save_and_refresh(self):
        config.save(self.cfg)
        self._refresh_menu()

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
        old = self.cfg["hotkeys"][key]
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

        # GPU
        if meeting_status["cuda_available"]:
            gpu_label = f"GPU: {meeting_status['cuda_device']}"
        else:
            gpu_label = "GPU: None (CPU mode)"
        items.append(pystray.MenuItem(gpu_label, None, enabled=False))

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
        if self.mode is not None:
            return

        self.mode = "transcribing"
        self.tray.title = "WhisperSync: Downloading model..."
        self._update_icon()

        def _do_download():
            try:
                ok = download_model(self.cfg["model"])
                if ok:
                    logger.info("Model download complete")
                    self.mode = "done"
                else:
                    logger.error("Model download failed")
                    self.mode = "error"
            except Exception as e:
                logger.error(f"Model download error: {e}")
                self.mode = "error"
            self._update_icon()
            self._schedule_idle(3)
            self._refresh_menu()

        threading.Thread(target=_do_download, daemon=True).start()

    def _restart(self):
        import subprocess
        if self.recorder.is_recording:
            self.recorder.stop()
        self.worker.stop()
        keyboard.unhook_all()
        subprocess.Popen(
            [sys.executable, "-m", "whisper_sync"],
            cwd=str(Path(__file__).parent.parent),
        )
        if self.tray:
            self.tray.stop()

    def quit(self):
        if self.recorder.is_recording:
            self.recorder.stop()
        self.worker.stop()
        keyboard.unhook_all()
        if self.tray:
            self.tray.stop()

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

        self.tray = pystray.Icon(
            "whisper-sync",
            idle_icon(),
            "WhisperSync: Idle",
            menu=self._build_menu(),
        )

        dictation_model = self.cfg.get("dictation_model", self.cfg["model"])
        logger.info("WhisperSync running. Hotkeys:")
        logger.info(f"  Dictation: {self.cfg['hotkeys']['dictation_toggle']} (model: {dictation_model})")
        logger.info(f"  Meeting:   {self.cfg['hotkeys']['meeting_toggle']} (model: {self.cfg['model']})")
        logger.info(f"  Left-click: {self.cfg.get('left_click', 'meeting')}")
        logger.info(f"  Middle-click: {self.cfg.get('middle_click', 'dictation')}")
        logger.info(f"Log file: {get_log_path()}")
        logger.info("Right-click tray icon for menu.")

        # Start transcription worker subprocess (loads models in isolation)
        self.worker.start()

        def _wait_worker():
            if self.worker.wait_ready(timeout=120):
                logger.info(f"Dictation model '{dictation_model}' ready (worker pid={self.worker._process.pid})")
                # Recover any crashed dictations found at startup
                for wav_path in self._recovered_dictation_paths:
                    self._recover_dictation(wav_path)
                self._recovered_dictation_paths = []
                # Recover any crashed meetings — show dialog for naming
                if self._recovered_meeting_paths:
                    self._recover_meetings()
            else:
                logger.warning("Transcription worker failed to start — dictation may not work")

        threading.Thread(target=_wait_worker, daemon=True).start()

        try:
            self.tray.run()
        finally:
            # Always release keyboard hooks to prevent stuck modifier keys
            keyboard.unhook_all()
            self.worker.stop()


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
