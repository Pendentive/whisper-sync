"""WhisperSync entry point — tray icon + hotkey listener."""

import sys
import threading
import tempfile
from datetime import datetime
from pathlib import Path

import keyboard
import pystray

from . import config
from .capture import AudioRecorder, get_default_devices, get_host_apis, list_devices, save_wav, save_stereo_wav
from .icons import idle_icon, recording_icon, dictation_icon, saving_icon, transcribing_icon, done_icon, error_icon
from .logger import logger, get_log_path
from .model_status import get_model_status, download_model, bootstrap_models
from .paste import paste
from .paths import get_install_root
from .transcribe import transcribe, transcribe_fast, preload
from . import dictation_log
from .streaming_wav import fix_orphan

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
        self._transcribing = False  # True while background transcription is running
        self.tray = None
        self._lock = threading.Lock()
        self._api_filter = "Windows WASAPI"  # None = show all

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
        icon, title = icons.get(self.mode, (idle_icon(), "Idle"))
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

    def _can_record(self) -> bool:
        """Can we start a new recording? Allowed if idle or just transcribing in background."""
        return self.mode is None or self.mode in ("transcribing", "done", "error")

    def toggle_dictation(self):
        with self._lock:
            if self.mode == "dictation":
                self._stop_dictation()
            elif self._can_record():
                self._start_dictation()

    def _start_dictation(self):
        self.mode = "dictation"
        self._update_icon()
        mic = self.cfg.get("mic_device")
        if self.cfg.get("use_system_devices", True):
            mic = None
        self.recorder.start(mic_device=mic)

    def _stop_dictation(self):
        audio = self.recorder.stop()

        if "mic" not in audio:
            self.mode = None
            self._update_icon()
            return

        self.mode = "transcribing"
        self._update_icon()

        dictation_model = self.cfg.get("dictation_model", self.cfg["model"])

        def _process():
            import time as _time
            t0 = _time.perf_counter()
            try:
                text = transcribe_fast(audio["mic"], model_override=dictation_model)
                t1 = _time.perf_counter()
                logger.info(f"transcribe_fast: {t1 - t0:.2f}s")
                if text:
                    paste(text, self.cfg["paste_method"])
                t2 = _time.perf_counter()
                logger.info(f"total (stop -> paste): {t2 - t0:.2f}s")
                if text:
                    dictation_log.append(text, t2 - t0)
                self.mode = "done"
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

    def _discard_dictation(self):
        """Discard current dictation — stop recording, throw away audio, return to idle."""
        with self._lock:
            if self.mode != "dictation":
                return
            self.recorder.stop()  # stop recording, discard the audio
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
        temp = self._meeting_temp_dir()
        mic_temp = temp / "mic-temp.wav"
        speaker_temp = temp / "speaker-temp.wav" if speaker is not None else None
        self.recorder.start_streaming(mic_temp, speaker_temp)

    _ABORT = object()  # Sentinel for abort

    def _ask_meeting_name(self):
        """Show a popup to name the meeting. Returns name string or _ABORT sentinel."""
        result = [self._ABORT]
        event = threading.Event()

        def _show_dialog():
            import tkinter as tk
            root = tk.Tk()
            root.title("WhisperSync")
            root.attributes("-topmost", True)
            root.geometry("350x150")
            root.resizable(False, False)

            tk.Label(root, text="Meeting name (leave blank for default):").pack(pady=(12, 4))
            entry = tk.Entry(root, width=40)
            entry.pack(pady=4)
            entry.focus_force()

            btn_frame = tk.Frame(root)
            btn_frame.pack(pady=8)

            def _submit(event=None):
                result[0] = entry.get()
                root.destroy()

            def _abort():
                result[0] = self._ABORT
                root.destroy()

            entry.bind("<Return>", _submit)
            entry.bind("<Escape>", lambda e: _abort())
            tk.Button(btn_frame, text="Save", command=_submit, width=10).pack(side=tk.LEFT, padx=4)
            tk.Button(btn_frame, text="Discard", command=_abort, width=10, fg="red").pack(side=tk.LEFT, padx=4)

            # Center on screen
            root.update_idletasks()
            x = (root.winfo_screenwidth() - root.winfo_reqwidth()) // 2
            y = (root.winfo_screenheight() - root.winfo_reqheight()) // 2
            root.geometry(f"+{x}+{y}")

            root.protocol("WM_DELETE_WINDOW", _abort)
            root.mainloop()
            event.set()

        t = threading.Thread(target=_show_dialog, daemon=True)
        t.start()
        event.wait(timeout=60)

        if result[0] is self._ABORT:
            return self._ABORT

        name = result[0] or ""
        return "".join(c if c.isalnum() or c in " -_" else "" for c in name).strip().replace(" ", "-")

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
            meeting_name = self._ask_meeting_name()

            if meeting_name is self._ABORT:
                logger.info("Recording discarded")
                self.recorder.discard_streaming()
                self.mode = None
                self._update_icon()
                return

            self.recorder.stop_streaming()
            try:
                now = datetime.now()
                date_str = now.strftime("%Y-%m-%d")
                year_str = now.strftime("%Y")
                folder_name = f"{date_str}_{meeting_name}" if meeting_name else f"{date_str}_meeting"
                meeting_dir = self._output_dir() / year_str / folder_name
                meeting_dir.mkdir(parents=True, exist_ok=True)

                wav_path = meeting_dir / "recording.wav"
                if "speaker" in audio:
                    save_stereo_wav(str(wav_path), audio["mic"], audio["speaker"], self.cfg["sample_rate"])
                else:
                    save_wav(str(wav_path), audio["mic"], self.cfg["sample_rate"])

                logger.info(f"WAV saved: {wav_path}")
                from .streaming_wav import cleanup_temp_files
                cleanup_temp_files(self._meeting_temp_dir())
                self.mode = "transcribing"
                self._update_icon()

                result = transcribe(str(wav_path), diarize=True)
                logger.info(f"Transcript saved: {result.get('json_path', wav_path)}")
                self.mode = "done"
                self._update_icon()
            except Exception as e:
                logger.error(f"Meeting transcription error: {e}")
                import traceback
                logger.debug(traceback.format_exc())
                self.mode = "error"
                self._update_icon()
            finally:
                if self.mode == "done":
                    self._schedule_idle(3, blink=True)
                elif self.mode == "error":
                    self._schedule_idle(3)

        threading.Thread(target=_post_record, daemon=True).start()

    def _output_dir(self) -> Path:
        p = Path(self.cfg["output_dir"])
        if not p.is_absolute():
            p = get_install_root() / p
        return p

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
        # If changing dictation model, preload it in background
        if key == "dictation_model":
            threading.Thread(target=lambda: preload(model_name=model_name), daemon=True).start()

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
        # Check for orphaned temp WAV files from a previous crash
        temp_dir = self._meeting_temp_dir()
        for name in ("mic-temp.wav", "speaker-temp.wav"):
            temp_path = temp_dir / name
            if temp_path.exists():
                dur = fix_orphan(temp_path)
                if dur is not None:
                    logger.warning(
                        f"Recovered {dur:.0f}s of {name.split('-')[0]} audio "
                        f"from previous crash — saved to {temp_dir}"
                    )
                else:
                    logger.info(f"Cleaned up stale temp file: {name}")

        # Bootstrap: ensure base models are cached, prompt for large ones
        bootstrap_models(self.cfg, on_large_model=self._prompt_large_download)

        keyboard.add_hotkey(
            self.cfg["hotkeys"]["dictation_toggle"],
            self.toggle_dictation,
            suppress=True,
        )
        keyboard.add_hotkey(
            self.cfg["hotkeys"]["meeting_toggle"],
            self.toggle_meeting,
            suppress=True,
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

        # Preload dictation model in background so first dictation is fast
        def _preload():
            try:
                preload(model_name=dictation_model)
                logger.info(f"Dictation model '{dictation_model}' ready")
            except Exception as e:
                logger.warning(f"Preload warning: {e}")

        threading.Thread(target=_preload, daemon=True).start()

        self.tray.run()


def main():
    try:
        logger.info("=== WhisperSync starting ===")
        app = WhisperSync()
        app.run()
    except Exception:
        import traceback
        logger.critical(f"FATAL CRASH:\n{traceback.format_exc()}")
        raise


if __name__ == "__main__":
    main()
