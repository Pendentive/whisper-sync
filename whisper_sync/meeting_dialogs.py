"""Meeting dialog builders - the Tk UI extracted from __main__.py.

Hardening round item 6 (architecture spec A2), extraction 2a. Every
dialog runs on the app's single DialogDispatcher thread (tk.Tk must
never be created on rotating worker threads - see dialog_dispatcher.py).
MeetingDialogs holds an app back-reference for the dispatcher and
config; the pure styling helpers and the ABORT sentinel are module
level so the remaining flows in __main__ share them.

Behavior is a direct move: dialog layouts, outcome logging, and the
abort-on-crash contract are unchanged.
"""

from pathlib import Path

from .executors import IO, submit_or_spawn
from .logger import logger
from .speakers import get_config_path

ABORT = object()
"""Sentinel a dialog returns when the user aborts (never confuse with None)."""


def sanitize_name(name: str) -> str:
    """Sanitize a meeting name for use as a folder name."""
    return "".join(c if c.isalnum() or c in " -_" else "" for c in name).strip().replace(" ", "-")

def center_window(root):
    """Center a tkinter window on screen."""
    root.update_idletasks()
    x = (root.winfo_screenwidth() - root.winfo_reqwidth()) // 2
    y = (root.winfo_screenheight() - root.winfo_reqheight()) // 2
    root.geometry(f"+{x}+{y}")

def run_modal(_proot, dlg):
    """Run a Toplevel dialog modally on the persistent root.

    Replaces the old per-dialog ``root.mainloop()``: the persistent
    root's event loop services the Toplevel while ``wait_window``
    blocks until the dialog is destroyed. grab_set makes it modal so
    stray clicks on other dialogs can't interleave.
    """
    dlg.update_idletasks()
    try:
        dlg.grab_set()
    except Exception:
        pass  # not viewable yet / grab unavailable - non-fatal
    _proot.wait_window(dlg)

def style_window(root):
    """Apply consistent modern styling to a tkinter window."""
    root.configure(bg="#1e1e2e")
    root.attributes("-topmost", True)
    root.resizable(False, False)

def flat_button(parent, text, command, bg="#45475a", fg="#cdd6f4", hover_bg="#585b70",
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


class MeetingDialogs:
    """Owns the meeting-related Tk dialogs; reads services through ``app``.

    Each public method blocks the calling (worker) thread until the user
    answers, exactly like the methods it replaces on WhisperSync.
    """

    def __init__(self, app):
        self.app = app
        self._deep_id_json_path = None  # set per speaker-confirmation dialog

    def ask_meeting_name(self):
        """Show a popup to name the meeting and select diarization method.

        Returns:
            _ABORT: user clicked Discard
            (str, True, str|None): user clicked Save & Summarize (name, summarize, diarize_method)
            (str, False, str|None): user clicked Save (name, summarize, diarize_method)
        """
        import time as _time
        dialog_started = _time.monotonic()
        logger.info("dialog open: _ask_meeting_name")
        result = [ABORT]

        def _show_dialog(_proot):
            import tkinter as tk

            from .transcribe import DIARIZE_METHODS

            root = tk.Toplevel(_proot)
            root.title("WhisperSync")
            style_window(root)
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
            primary = self.app.cfg.get("diarize_primary", "balanced_mix")
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
                return sanitize_name(entry.get() or "")

            def _get_method():
                m = selected_method.get()
                # Return None if it matches the config default (no override needed)
                if m == self.app.cfg.get("diarize_primary", "balanced_mix"):
                    return None
                return m

            def _save_and_summarize(ev=None):
                result[0] = (_sanitize(), True, _get_method())
                root.destroy()

            def _save_only():
                result[0] = (_sanitize(), False, _get_method())
                root.destroy()

            def _abort():
                result[0] = ABORT
                root.destroy()

            entry.bind("<Return>", _save_and_summarize)
            entry.bind("<Escape>", lambda e: _abort())

            # Pack RIGHT to LEFT so rightmost button is packed first
            flat_button(btn_frame, "Save & Summarize", _save_and_summarize,
                              bg=accent, fg="#1e1e2e", hover_bg="#74c7ec", bold=True).pack(side=tk.RIGHT, padx=6)
            flat_button(btn_frame, "Save", _save_only).pack(side=tk.RIGHT, padx=6)
            flat_button(btn_frame, "Discard", _abort, fg=danger).pack(side=tk.RIGHT, padx=6)

            center_window(root)
            root.protocol("WM_DELETE_WINDOW", _abort)
            run_modal(_proot, root)

        # Run on the shared dialog dispatcher thread. The dispatcher catches
        # exceptions and re-raises them in our thread, so we mirror the prior
        # "abort on crash" behavior with a try/except here. This preserves
        # the prior contract: _save_and_enqueue's caller never sees a hang
        # if Tk init or geometry blows up mid-dialog.
        try:
            self.app._dialog_dispatcher.run(_show_dialog, label="ask_meeting_name", wants_root=True)
        except Exception:
            logger.exception("dialog crashed: _ask_meeting_name")
            result[0] = ABORT

        # Report dialog outcome with the actual returned value so forensic
        # logs can distinguish ABORT vs save vs timeout vs garbage.
        elapsed = _time.monotonic() - dialog_started
        outcome = result[0]
        if outcome is ABORT:
            logger.info("dialog close: _ask_meeting_name outcome=abort elapsed=%.1fs", elapsed)
        elif isinstance(outcome, tuple) and len(outcome) == 3:
            name, summarize, method = outcome
            logger.info(
                "dialog close: _ask_meeting_name outcome=save name=%r summarize=%s method=%s elapsed=%.1fs",
                name or "", summarize, method, elapsed,
            )
        else:
            logger.warning(
                "dialog close: _ask_meeting_name outcome=unexpected value=%r elapsed=%.1fs",
                outcome, elapsed,
            )

        return result[0]

    def show_llm_unavailable(self):
        """Show a dialog when Claude CLI is not available. Returns True if user checked 'don't show again'."""
        result = [False]

        def _show(_proot):
            import tkinter as tk

            root = tk.Toplevel(_proot)
            root.title("WhisperSync")
            style_window(root)
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

            flat_button(root, "OK", _ok).pack()

            center_window(root)
            root.protocol("WM_DELETE_WINDOW", _ok)
            run_modal(_proot, root)

        try:
            self.app._dialog_dispatcher.run(_show, label="show_llm_unavailable", wants_root=True)
        except Exception:
            logger.exception("dialog crashed: _show_llm_unavailable")

        return result[0]

    def ask_speaker_confirmation(self, identification_result: dict) -> dict | None:
        """Show speaker confirmation dialog.

        Returns:
            None: user skipped
            dict: confirmed speaker_map (no boundaries detected)
            tuple[dict, list]: (speaker_map, boundaries) when deep identify found meeting splits
        """
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

        # Store json_path for deep identify button access
        self._deep_id_json_path = getattr(self.app, '_current_meeting_json_path', None)

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

        def _show(_proot):
            import tkinter as tk

            root = tk.Toplevel(_proot)
            root.title("WhisperSync")
            style_window(root)

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
            screen_h = root.winfo_screenheight()
            max_h = min(700, int(screen_h * 0.8))
            # Size to content, capped at max_h. When content exceeds the cap,
            # the middle content scrolls via the inline Canvas/Scrollbar setup,
            # and the bottom button bar remains visible because it is packed
            # with side=BOTTOM BEFORE the scrollable middle.
            height = min(180 + (num_speakers * 70), max_h)
            root.geometry(f"500x{height}")
            root.minsize(500, 260)  # guarantee the buttons are always reachable

            # Header: packed FIRST (top)
            header = tk.Frame(root, bg=bg)
            header.pack(side=tk.TOP, fill="x", padx=24, pady=(14, 0))
            tk.Label(header, text="\U0001f3a4", font=("Segoe UI", 13), bg=bg).pack(side=tk.LEFT)
            tk.Label(header, text="  Identify Speakers", font=("Segoe UI", 11, "bold"),
                     bg=bg, fg=fg).pack(side=tk.LEFT)

            # Bottom panel: progress + boundary notice + buttons. These are
            # packed NOW (before the scrollable middle) with side=BOTTOM so
            # Tk's pack algorithm reserves space for them regardless of how
            # many speakers are added; the Confirm button never slides off.
            bottom_panel = tk.Frame(root, bg=bg)
            bottom_panel.pack(side=tk.BOTTOM, fill="x")

            # Scrollable middle: contains speaker rows + reasoning text.
            # Tk does not have a native scrollable frame; the idiom is a
            # Canvas + Scrollbar + inner Frame. The inner frame behaves like
            # a normal Frame you can pack into.
            middle = tk.Frame(root, bg=bg)
            middle.pack(side=tk.TOP, fill="both", expand=True, padx=0, pady=(12, 0))
            _scroll_canvas = tk.Canvas(middle, bg=bg, highlightthickness=0)
            _scroll_canvas.pack(side=tk.LEFT, fill="both", expand=True)
            _scrollbar = tk.Scrollbar(middle, orient="vertical", command=_scroll_canvas.yview)
            _scrollbar.pack(side=tk.RIGHT, fill="y")
            _scroll_canvas.configure(yscrollcommand=_scrollbar.set)

            rows_frame = tk.Frame(_scroll_canvas, bg=bg)
            _rows_window = _scroll_canvas.create_window((0, 0), window=rows_frame, anchor="nw")

            def _on_rows_configure(_event):
                _scroll_canvas.configure(scrollregion=_scroll_canvas.bbox("all"))
            rows_frame.bind("<Configure>", _on_rows_configure)

            def _on_canvas_configure(event):
                # Make the inner frame match the canvas width so content wraps
                # correctly instead of being clipped horizontally.
                _scroll_canvas.itemconfigure(_rows_window, width=event.width)
            _scroll_canvas.bind("<Configure>", _on_canvas_configure)

            def _on_mousewheel(event):
                # Windows: event.delta is a multiple of 120 per notch.
                _scroll_canvas.yview_scroll(int(-event.delta / 120), "units")
            # Bind only when the mouse is over the canvas so we don't steal
            # wheel events from child dropdowns.
            _scroll_canvas.bind("<Enter>", lambda _e: _scroll_canvas.bind_all("<MouseWheel>", _on_mousewheel))
            _scroll_canvas.bind("<Leave>", lambda _e: _scroll_canvas.unbind_all("<MouseWheel>"))

            dropdowns = {}

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

            # Progress bar (hidden initially): lives in bottom_panel so it
            # sits above the button row and never disappears behind scroll.
            progress_frame = tk.Frame(bottom_panel, bg=bg)
            progress_frame.pack(fill="x", padx=24, pady=(4, 0))
            progress_canvas = tk.Canvas(progress_frame, height=4, bg="#313244",
                                         highlightthickness=0)
            progress_canvas.pack(fill="x")
            progress_bar = progress_canvas.create_rectangle(0, 0, 0, 4, fill=accent, width=0)
            progress_label = tk.Label(progress_frame, text="", font=("Segoe UI", 7),
                                       bg=bg, fg=fg_dim)
            progress_label.pack(anchor="w")
            progress_frame.pack_forget()  # hidden until needed

            # Boundary notice (hidden initially): also in bottom_panel.
            boundary_frame = tk.Frame(bottom_panel, bg=bg)
            boundary_label = tk.Label(boundary_frame, text="", font=("Segoe UI", 8),
                                       bg=bg, fg=yellow, wraplength=440)
            boundary_label.pack(side=tk.LEFT, padx=(24, 8))
            boundary_frame.pack_forget()  # hidden until boundaries detected

            _boundaries = [None]

            # Buttons: permanently visible at the bottom.
            btn_frame = tk.Frame(bottom_panel, bg=bg)
            btn_frame.pack(pady=(14, 12))

            _closing = [False]
            _deep_running = [False]

            def _update_progress(phase, pct):
                def _do():
                    if _closing[0]:
                        return
                    progress_label.configure(text=phase)
                    canvas_width = progress_canvas.winfo_width() or 440
                    progress_canvas.coords(progress_bar, 0, 0, canvas_width * pct, 4)
                root.after(0, _do)

            def _deep_identify():
                if _closing[0] or _deep_running[0]:
                    return
                _deep_running[0] = True

                progress_frame.pack(fill="x", padx=24, pady=(4, 0))
                for child in btn_frame.winfo_children():
                    try:
                        child.unbind("<Button-1>")
                    except Exception:
                        pass

                def _run_deep():
                    from .speakers import deep_identify_speakers, get_config_path
                    try:
                        cfg_path = get_config_path()
                        json_path = self._deep_id_json_path
                        if not json_path:
                            raise ValueError("No transcript path available for deep identification")
                        deep_result = deep_identify_speakers(
                            json_path, cfg_path,
                            Path(json_path).parent.name,
                            progress_callback=_update_progress,
                        )
                        if deep_result and deep_result.get("speaker_map"):
                            def _apply():
                                for spk_id, var in dropdowns.items():
                                    new_name = deep_result["speaker_map"].get(spk_id, "")
                                    if new_name:
                                        var.set(new_name)

                                raw_bounds = deep_result.get("meeting_boundaries", [])
                                bounds = []
                                if isinstance(raw_bounds, list):
                                    for boundary in raw_bounds:
                                        if not isinstance(boundary, dict):
                                            continue
                                        try:
                                            split_secs = int(float(boundary.get("split_seconds", 0)))
                                        except (TypeError, ValueError):
                                            continue
                                        normalized = dict(boundary)
                                        normalized["split_seconds"] = split_secs
                                        bounds.append(normalized)

                                if bounds:
                                    _boundaries[0] = bounds
                                    pts = ", ".join(
                                        f"{int(b['split_seconds']) // 60}:{int(b['split_seconds']) % 60:02d}"
                                        for b in bounds
                                    )
                                    boundary_label.configure(
                                        text=f"Detected {len(bounds) + 1} meetings (split at {pts}). Use Meetings menu to split."
                                    )
                                    boundary_frame.pack(fill="x", pady=(4, 0))

                                _rebind_buttons()
                                _deep_running[0] = False

                            root.after(0, _apply)
                        else:
                            def _fail():
                                progress_label.configure(text="Deep identification returned no results")
                                _rebind_buttons()
                                _deep_running[0] = False
                            root.after(0, _fail)

                    except Exception as e:
                        logger.warning(f"Deep identify failed: {e}")
                        def _err():
                            progress_label.configure(text=f"Failed: {str(e)[:60]}")
                            _rebind_buttons()
                            _deep_running[0] = False
                        root.after(0, _err)

                submit_or_spawn(IO, "speaker-id-deep", _run_deep, native=True)

            def _confirm():
                if _closing[0]:
                    return
                _closing[0] = True
                try:
                    confirmed = {spk_id: var.get() for spk_id, var in dropdowns.items()}
                    if _boundaries[0]:
                        result[0] = (confirmed, _boundaries[0])
                    else:
                        result[0] = confirmed
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

            # Create buttons - store references for rebinding
            _confirm_btn = flat_button(btn_frame, "Confirm", _confirm,
                              bg=accent, fg="#1e1e2e", hover_bg="#74c7ec", bold=True)
            _confirm_btn.pack(side=tk.RIGHT, padx=8)
            _skip_btn = flat_button(btn_frame, "Skip", _skip, fg=fg_muted)
            _skip_btn.pack(side=tk.RIGHT, padx=8)
            _deep_btn = flat_button(btn_frame, "Deep Identify", _deep_identify,
                              bg="#45475a", fg=fg, hover_bg="#585b70")
            _deep_btn.pack(side=tk.RIGHT, padx=8)

            def _rebind_buttons():
                """Re-bind button clicks after deep identify completes."""
                _confirm_btn.bind("<Button-1>", lambda e: _confirm())
                _skip_btn.bind("<Button-1>", lambda e: _skip())
                _deep_btn.bind("<Button-1>", lambda e: _deep_identify())

            center_window(root)
            root.protocol("WM_DELETE_WINDOW", _skip)
            run_modal(_proot, root)

        # Run on the shared dialog dispatcher thread. Previously this method
        # spawned a fresh daemon thread per call and ran tk.Tk() there. That
        # short-lived-thread pattern corrupted Win32/heap state and surfaced
        # later as STATUS_BREAKPOINT (0x80000003) at speakers.py:541 inside
        # json.load, on the post-processing worker thread.
        try:
            self.app._dialog_dispatcher.run(_show, label="ask_speaker_confirmation", wants_root=True)
        except Exception:
            logger.exception("speaker dialog crashed")
            result[0] = None

        return result[0]

    def ask_recovery_name(self, wav_path: str, duration_str: str):
        """Show a dialog to name a recovered meeting. Returns name string or _ABORT."""
        result = [ABORT]

        def _show(_proot):
            import tkinter as tk
            # Toplevel child of the persistent hidden root: no Tk/Tcl
            # interpreter churn per dialog (stability rebuild Phase 3).
            root = tk.Toplevel(_proot)
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
                result[0] = ABORT
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
            run_modal(_proot, root)

        try:
            self.app._dialog_dispatcher.run(_show, label="ask_recovery_name", wants_root=True)
        except Exception:
            logger.exception("dialog crashed: _ask_recovery_name")
            return ABORT

        if result[0] is ABORT:
            return ABORT

        name = result[0] or ""
        return "".join(c if c.isalnum() or c in " -_" else "" for c in name).strip().replace(" ", "-")
