"""Tray menu construction and settings actions - extracted from __main__.py.

Hardening round item 6 (architecture spec A2), extraction 3. TrayMenu
owns the full right-click menu build (device pickers, models, hotkeys,
session stats, recent dictations, meetings, GitHub section via
app.github), every settings setter, and the settings-adjacent dialogs
(error popup, output folder move). Menu swaps still go through the
app's MenuRefresher -> _update_tray path under the tray lock; this
component only BUILDS menus and mutates config.

Behavior is a direct move. menu_callback (formerly WhisperSync._cb)
is module level so GitHubTray shares it.
"""

import threading
from datetime import datetime
from pathlib import Path

from . import config
from . import weekly_stats
from .executors import DICTATION, submit_or_spawn
from .logger import logger, set_console_level
from .meeting_dialogs import (style_window, flat_button, center_window,
                              run_modal)
from .notifications import notify
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


def menu_callback(fn, *bound_args):
    """Bind a callback for a pystray MenuItem (swallows the item arg)."""
    def _inner(icon=None, item=None):
        return fn(*bound_args)
    return _inner


class TrayMenu:
    """Owns menu building + settings; reads services through ``app``."""

    def __init__(self, app):
        self.app = app
        self._api_filter = "Windows WASAPI"  # None = show all

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

    def _fmt_hotkey(self, key: str) -> str:
        return key.replace("+", " + ").title()

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
        import pystray  # lazy: not installed on the CI system python
        """Build the Recent Dictations submenu items."""
        history = self.app.dictation.recent_history()
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
                pystray.MenuItem(label, menu_callback(self._copy_dictation, full_text))
            )
        items.append(pystray.Menu.SEPARATOR)
        items.append(
            pystray.MenuItem("Open Logs", menu_callback(self._open_dictation_logs))
        )
        items.append(
            pystray.MenuItem("Clear History", menu_callback(self.app.dictation.clear_history))
        )
        return pystray.Menu(*items)

    def _build_meetings_menu(self):
        import pystray  # lazy: not installed on the CI system python
        """Build the Meetings submenu showing recent meetings with speaker status."""
        output_dir = self.app._output_dir()
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
                    menu_callback(self.app.meetings.recover_meeting_speakers, meeting_dir),
                )
            )
        return pystray.Menu(*items)

    def build(self):
        """Build the full right-click tray menu (pystray.Menu)."""
        import pystray  # lazy: not installed on the CI system python
        # Lazy: capture imports numpy (system-python testability).
        from .capture import get_default_devices, get_host_apis, list_devices

        devices = list_devices(api_filter=self._api_filter)
        dict_hk = self._fmt_hotkey(self.app.cfg["hotkeys"]["dictation_toggle"])
        meet_hk = self._fmt_hotkey(self.app.cfg["hotkeys"]["meeting_toggle"])
        use_sys = self.app.cfg.get("use_system_devices", True)

        # --- Resolve effective devices (config or system default) ---
        defaults = get_default_devices(api_filter=self._api_filter)
        eff_mic = defaults["input"] if use_sys else (self.app.cfg.get("mic_device") or defaults["input"])
        eff_spk = defaults["output"] if use_sys else (self.app.cfg.get("speaker_device") or defaults["output"])

        # --- Device submenus ---
        mic_items = [
            pystray.MenuItem(
                f"{d['name']} (system)" if d["id"] == defaults["input"] else d["name"],
                menu_callback(self._set_device, "mic_device", d["id"]),
                checked=lambda item, d=d, em=eff_mic: d["id"] == em,
                radio=True,
                enabled=not use_sys,
            )
            for d in devices["inputs"]
        ]
        speaker_items = [
            pystray.MenuItem(
                f"{d['name']} (system)" if d["id"] == defaults["output"] else d["name"],
                menu_callback(self._set_device, "speaker_device", d["id"]),
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
                menu_callback(self._set_api_filter, None),
                checked=lambda item: self._api_filter is None,
                radio=True,
            )
        ] + [
            pystray.MenuItem(
                a["name"],
                menu_callback(self._set_api_filter, a["name"]),
                checked=lambda item, a=a: self._api_filter == a["name"],
                radio=True,
            )
            for a in apis
        ]

        # --- Settings submenus ---
        dictation_hk_items = [
            pystray.MenuItem(
                hk,
                menu_callback(self._set_hotkey, "dictation_toggle", hk),
                checked=lambda item, hk=hk: self.app.cfg["hotkeys"]["dictation_toggle"] == hk,
                radio=True,
            )
            for hk in HOTKEY_OPTIONS
        ]
        meeting_hk_items = [
            pystray.MenuItem(
                hk,
                menu_callback(self._set_hotkey, "meeting_toggle", hk),
                checked=lambda item, hk=hk: self.app.cfg["hotkeys"]["meeting_toggle"] == hk,
                radio=True,
            )
            for hk in HOTKEY_OPTIONS
        ]
        feature_hk_items = [
            pystray.MenuItem(
                hk,
                menu_callback(self._set_hotkey, "feature_suggest", hk),
                checked=lambda item, hk=hk: self.app.cfg["hotkeys"].get("feature_suggest", "ctrl+shift+alt+f") == hk,
                radio=True,
            )
            for hk in FEATURE_HOTKEY_OPTIONS
        ]
        paste_items = [
            pystray.MenuItem(
                method,
                menu_callback(self._set_paste_method, method),
                checked=lambda item, m=method: self.app.cfg["paste_method"] == m,
                radio=True,
            )
            for method in PASTE_OPTIONS
        ]
        dictation_model_items = [
            pystray.MenuItem(
                f"{name} ({size})",
                menu_callback(self._set_model, "dictation_model", name),
                checked=lambda item, n=name: self.app.cfg.get("dictation_model", self.app.cfg["model"]) == n,
                radio=True,
            )
            for name, size in MODEL_OPTIONS.items()
        ]
        meeting_model_items = [
            pystray.MenuItem(
                f"{name} ({size})",
                menu_callback(self._set_model, "model", name),
                checked=lambda item, n=name: self.app.cfg["model"] == n,
                radio=True,
            )
            for name, size in MODEL_OPTIONS.items()
        ]
        left_click_items = [
            pystray.MenuItem(
                label,
                menu_callback(self._set_click, "left_click", action),
                checked=lambda item, a=action: self.app.cfg.get("left_click", "meeting") == a,
                radio=True,
            )
            for action, label in CLICK_ACTIONS.items()
        ]
        middle_click_items = [
            pystray.MenuItem(
                label,
                menu_callback(self._set_click, "middle_click", action),
                checked=lambda item, a=action: self.app.cfg.get("middle_click", "dictation") == a,
                radio=True,
            )
            for action, label in CLICK_ACTIONS.items()
        ]

        # Device (compute) selection
        # Build per-option labels with GPU name from worker (avoids torch import in main process)
        device_options = []
        gpu_name = self.app.worker.gpu_name if self.app.worker else None
        auto_suffix = f"\t{gpu_name}" if gpu_name else "\tCPU -- no GPU detected"
        device_options.append(("auto", f"Auto{auto_suffix}"))
        gpu_suffix = f"\t{gpu_name}" if gpu_name else "\tnot available"
        device_options.append(("gpu", f"GPU{gpu_suffix}"))
        device_options.append(("cpu", "CPU"))
        device_items = [
            pystray.MenuItem(
                label,
                menu_callback(self._set_compute_device, dev),
                checked=lambda item, d=dev: self.app.cfg.get("device", "auto") == d,
                radio=True,
            )
            for dev, label in device_options
        ]

        # --- Always Available Dictation ---
        backup_device_cfg = self.app.cfg.get("backup_device", "auto")
        backup_model_cfg = self.app.cfg.get("backup_model", "base")
        backup_model_options = ["tiny", "base", "small"]
        backup_device_options = [
            ("auto", "Auto"),
            ("gpu", "GPU"),
            ("cpu", "CPU"),
        ]
        backup_device_items = [
            pystray.MenuItem(
                label,
                menu_callback(self._set_backup_device, dev),
                checked=lambda item, d=dev: self.app.cfg.get("backup_device", "auto") == d,
                radio=True,
            )
            for dev, label in backup_device_options
        ]
        backup_model_items = [
            pystray.MenuItem(
                f"{name} ({MODEL_OPTIONS.get(name, '')})",
                menu_callback(self._set_backup_model, name),
                checked=lambda item, n=name: self.app.cfg.get("backup_model", "base") == n,
                radio=True,
            )
            for name in backup_model_options
        ]

        # --- Notifications submenu ---
        from .notifications import DEFAULT_TOAST_EVENTS
        _notification_options = [
            ("meeting_completed", "Meeting Complete"),
            ("error", "Errors"),
            ("pr_status_changed", "PR Status"),
            ("dictation_completed", "Dictation Complete"),
        ]
        notification_items = [
            pystray.MenuItem(
                label,
                menu_callback(self._toggle_toast_event, evt),
                checked=lambda item, e=evt: e in self.app.cfg.get("toast_events", list(DEFAULT_TOAST_EVENTS)),
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
            current_method = self.app.cfg.get(slot_key, "balanced_mix")
            slot_items = [
                pystray.MenuItem(
                    DIARIZE_METHODS.get(method_id, method_id),
                    menu_callback(self._set_diarize_method, slot_key, method_id),
                    checked=lambda item, m=method_id, sk=slot_key: self.app.cfg.get(sk, "balanced_mix") == m,
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
        primary_method = self.app.cfg.get("diarize_primary", "balanced_mix")
        primary_label = DIARIZE_METHODS.get(primary_method, primary_method)

        # --- Whisper mode ---
        incognito_items = [
            pystray.MenuItem(
                "Whisper Mode",
                lambda: self._toggle_incognito(),
                checked=lambda item: self.app.cfg.get("incognito", False),
            ),
            pystray.MenuItem("  RAM only dictation, no disk, no logs", None, enabled=False),
        ]

        # Left-click fires the default menu item
        left_action = self.app.cfg.get("left_click", "meeting")
        return pystray.Menu(
            pystray.MenuItem("Meetings", self._build_meetings_menu()),
            pystray.MenuItem("Recent Dictations", self._build_recent_dictations_menu()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(f"Dictation\t{dict_hk}", lambda: self.app._on_left_click() if left_action == "dictation" else self.app.dictation.toggle(),
                             default=left_action == "dictation"),
            pystray.MenuItem(f"Meeting\t{meet_hk}", lambda: self.app._on_left_click() if left_action == "meeting" else self.app.meetings.toggle(),
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
                menu_callback(self._toggle_system_devices),
                checked=lambda item: self.app.cfg.get("use_system_devices", True),
            ),
            pystray.MenuItem(filter_label, pystray.Menu(*filter_items)),
            pystray.Menu.SEPARATOR,
            *self.app.github.menu_items(),
            pystray.MenuItem("Open Output Folder", lambda: self._open_output_folder()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Settings", pystray.Menu(
                pystray.MenuItem(f"Dictation Hotkey\t{self.app.cfg['hotkeys']['dictation_toggle']}",
                                 pystray.Menu(*dictation_hk_items)),
                pystray.MenuItem(f"Meeting Hotkey\t{self.app.cfg['hotkeys']['meeting_toggle']}",
                                 pystray.Menu(*meeting_hk_items)),
                pystray.MenuItem(f"Feature Suggest Hotkey\t{self.app.cfg['hotkeys'].get('feature_suggest', 'ctrl+shift+alt+f')}",
                                 pystray.Menu(*feature_hk_items)),
                pystray.MenuItem(f"Paste Method\t{self.app.cfg['paste_method']}",
                                 pystray.Menu(*paste_items)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(f"Left Click\t{CLICK_ACTIONS.get(self.app.cfg.get('left_click', 'meeting'), 'meeting')}",
                                 pystray.Menu(*left_click_items)),
                pystray.MenuItem(f"Middle Click\t{CLICK_ACTIONS.get(self.app.cfg.get('middle_click', 'dictation'), 'dictation')}",
                                 pystray.Menu(*middle_click_items)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(f"Dictation Model\t{self.app.cfg.get('dictation_model', self.app.cfg['model'])}",
                                 pystray.Menu(*dictation_model_items)),
                pystray.MenuItem(f"Meeting Model\t{self.app.cfg['model']}",
                                 pystray.Menu(*meeting_model_items)),
                pystray.MenuItem(f"Device\t{self._get_device_label()}",
                                 pystray.Menu(*device_items)),
                pystray.MenuItem("Always Available Dictation", pystray.Menu(
                    pystray.MenuItem(
                        "Enabled",
                        lambda: self._toggle_always_available_dictation(),
                        checked=lambda item: self.app.cfg.get("always_available_dictation", True),
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
                pystray.MenuItem(f"  {self._truncate_path(self.app._output_dir())}",
                                 None, enabled=False),
                pystray.MenuItem(f"Log Window\t{self.app.cfg.get('log_window', 'normal')}", pystray.Menu(
                    pystray.MenuItem("Off",
                                     menu_callback(self._set_log_level, "off"),
                                     checked=lambda item: self.app.cfg.get("log_window") == "off",
                                     radio=True),
                    pystray.MenuItem("Normal",
                                     menu_callback(self._set_log_level, "normal"),
                                     checked=lambda item: self.app.cfg.get("log_window", "normal") == "normal",
                                     radio=True),
                    pystray.MenuItem("Detailed -- includes transcriptions",
                                     menu_callback(self._set_log_level, "detailed"),
                                     checked=lambda item: self.app.cfg.get("log_window") == "detailed",
                                     radio=True),
                    pystray.MenuItem("Verbose -- full debug output",
                                     menu_callback(self._set_log_level, "verbose"),
                                     checked=lambda item: self.app.cfg.get("log_window") == "verbose",
                                     radio=True),
                )),
                pystray.MenuItem("Weekly Stats", self._build_session_stats_menu()),
                pystray.MenuItem("Notifications", pystray.Menu(*notification_items)),
                pystray.Menu.SEPARATOR,
                *incognito_items,
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Update", pystray.Menu(
                    pystray.MenuItem("Stable\tmain", menu_callback(self.app._update, "main")),
                    pystray.MenuItem("Labs\tdev", menu_callback(self.app._update, "dev")),
                )),
                pystray.MenuItem("Restart", lambda: self.app._restart()),
                pystray.MenuItem("Quit", lambda: self.app.quit()),
            )),
        )

    def _toggle_toast_event(self, event_type: str):
        from .notifications import DEFAULT_TOAST_EVENTS
        events = self.app.cfg.get("toast_events", list(DEFAULT_TOAST_EVENTS))
        if event_type in events:
            events.remove(event_type)
        else:
            events.append(event_type)
        self.app.cfg["toast_events"] = events
        self._save_and_refresh()

    def _toggle_incognito(self):
        self.app.cfg["incognito"] = not self.app.cfg.get("incognito", False)
        state = "on" if self.app.cfg["incognito"] else "off"
        logger.info(f"Whisper mode: {state}")
        self._save_and_refresh()
        # #40: Toast warning when incognito toggles
        try:
            if self.app.cfg["incognito"]:
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

    def _save_and_refresh(self):
        config.save(self.app.cfg.snapshot())
        self.app._refresh_menu()

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
                self.app._dialog_dispatcher.run(_show, label="error_popup", wants_root=True)
            except Exception:
                logger.exception("error popup failed: %s", title)

        threading.Thread(target=_dispatch, daemon=True).start()

    def _open_output_folder(self):
        import subprocess
        out = self.app._output_dir()
        out.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["explorer.exe", str(out)])

    def _change_output_folder(self):
        """Show folder picker, optionally move existing files, update config."""
        import shutil

        current = self.app._output_dir()
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
                self.app._dialog_dispatcher.run(_show_move_dialog, label="change_output_move_dialog", wants_root=True)
            except Exception:
                logger.exception("change-output move dialog crashed")
                move_result[0] = None

            if move_result[0] is None:
                return

            result[0] = (new_path, move_result[0])

        try:
            self.app._dialog_dispatcher.run(_show, label="change_output_folder", wants_root=True)
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

        self.app.cfg["output_dir"] = str(new_path)
        self._save_and_refresh()
        logger.info(f"Output folder changed to: {new_path}")

    def _build_session_stats_menu(self):
        import pystray  # lazy: not installed on the CI system python
        """Build weekly stats submenu with today and wk columns."""
        s = self.app._stats.snapshot()
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
        self.app.cfg["log_window"] = tier
        set_console_level(tier)
        self._save_and_refresh()
        logger.info(f"Log window set to: {tier}")

    def _set_api_filter(self, api_name: str | None):
        self._api_filter = api_name
        self.app._refresh_menu()

    def _set_device(self, key: str, device_id: int):
        self.app.cfg[key] = device_id
        self._save_and_refresh()

    def _toggle_system_devices(self):
        self.app.cfg["use_system_devices"] = not self.app.cfg.get("use_system_devices", True)
        self._save_and_refresh()

    def _set_hotkey(self, key: str, hotkey: str):
        old = self.app.cfg["hotkeys"].get(key)
        if old == hotkey:
            return
        self.app.cfg.set_nested("hotkeys", key, hotkey)
        self._save_and_refresh()
        self.app._restart()

    def _set_paste_method(self, method: str):
        self.app.cfg["paste_method"] = method
        self._save_and_refresh()

    def _set_click(self, key: str, action: str):
        self.app.cfg[key] = action
        self._save_and_refresh()

    def _set_compute_device(self, device: str):
        """Switch compute device (auto/gpu/cpu) and restart the worker."""
        old = self.app.cfg.get("device", "auto")
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

        self.app.cfg["device"] = device
        self._save_and_refresh()

        if old_resolved == new_resolved:
            logger.info(f"Device setting: {old} -> {device} (same hardware, no restart)")
            return

        logger.info(f"Switching device: {old} -> {device} ({old_resolved} -> {new_resolved})")
        self.app.worker.update_config(self.app.cfg)
        _previous_device = old
        def _do_restart():
            self.app.worker.restart()
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
        device_setting = self.app.cfg.get("device", "auto")
        gpu = self.app.worker.gpu_name if self.app.worker else None
        if device_setting == "cpu":
            return "CPU"
        elif device_setting in ("gpu", "cuda"):
            return gpu if gpu else "GPU"
        else:  # auto
            if gpu:
                return f"Auto ({gpu})"
            return "Auto (CPU)"

    def _toggle_always_available_dictation(self):
        self.app.cfg["always_available_dictation"] = not self.app.cfg.get("always_available_dictation", True)
        state = "enabled" if self.app.cfg["always_available_dictation"] else "disabled"
        logger.info(f"Always Available Dictation: {state}")
        if not self.app.cfg["always_available_dictation"]:
            self.app._backup.stop()
        self._save_and_refresh()

    def _set_backup_device(self, device: str):
        if self.app.cfg.get("backup_device", "auto") == device:
            return
        self.app.cfg["backup_device"] = device
        logger.info(f"Backup device: {device}", extra={"secondary": True})
        self.app._backup.stop()
        self.app._backup.preload()
        self._save_and_refresh()

    def _set_backup_model(self, model_name: str):
        if self.app.cfg.get("backup_model", "base") == model_name:
            return
        self.app.cfg["backup_model"] = model_name
        logger.info(f"Backup model: {model_name}", extra={"secondary": True})
        self.app._backup.stop()
        self.app._backup.preload()
        self._save_and_refresh()

    def _set_diarize_method(self, slot_key: str, method_id: str):
        """Set a diarization slot, swapping with any slot that already has this method."""
        from .transcribe import DIARIZE_METHODS
        with self.app.cfg.transaction():
            current = self.app.cfg.get(slot_key, "balanced_mix")
            if current == method_id:
                return
            # Find if another slot already uses this method and swap
            all_slots = ["diarize_primary", "diarize_fallback", "diarize_last_resort"]
            for other_slot in all_slots:
                if other_slot != slot_key and self.app.cfg.get(other_slot, "balanced_mix") == method_id:
                    self.app.cfg[other_slot] = current  # swap
                    break
            self.app.cfg[slot_key] = method_id
        label = DIARIZE_METHODS.get(method_id, method_id)
        logger.info(f"Diarization {slot_key}: {label}", extra={"secondary": True})
        self._save_and_refresh()

    def _set_model(self, key: str, model_name: str):
        logger.info(f"Setting {key} = {model_name}")
        if self.app.cfg.get(key) == model_name:
            return
        self.app.cfg[key] = model_name
        self._save_and_refresh()
        # Reload model in the appropriate worker subprocess
        if key == "dictation_model":
            # DICTATION lane: a reload and a dictation cannot run
            # concurrently anyway, so serializing them is the semantics.
            submit_or_spawn(
                DICTATION, "dictation-model-reload",
                lambda m=model_name: self.app.worker.reload_model(m),
                native=True,
            )
