"""WhisperSync Wizard Installer - multi-page tkinter installer for Windows."""

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, ttk
from pathlib import Path

from .paths import get_default_output_dir

VERSION = "1.0"

# -- Colors (dark theme) --
BG = "#1a1a2e"
BG_LIGHT = "#16213e"
FG = "#e0e0e0"
FG_DIM = "#8888aa"
ACCENT = "#4fc3f7"
GREEN = "#66bb6a"
RED = "#ef5350"
YELLOW = "#ffd54f"
MAGENTA = "#ce93d8"
ENTRY_BG = "#0f3460"
ENTRY_FG = "#e0e0e0"
BTN_BG = "#4fc3f7"
BTN_FG = "#1a1a2e"

SPINNER_CHARS = ["|", "/", "-", "\\"]
TOTAL_INSTALL_STEPS = 7

WIN_W = 550
WIN_H = 650


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

def detect_gpu():
    """Detect NVIDIA GPU via nvidia-smi.

    Returns (gpu_name, cuda_version, cuda_label) or (None, None, None).
    """
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            name = result.stdout.strip().split("\n")[0].strip()
            if re.search(r"RTX\s*50[0-9]{2}|RTX\s*5[0-9]{3}|Blackwell", name):
                return name, "cu128", "CUDA 12.8 (RTX 50-series)"
            elif re.search(
                r"RTX\s*[2-4]0[0-9]{2}|RTX\s*[2-4][0-9]{3}|A[0-9]{3,4}|L[0-9]{2}",
                name,
            ):
                return name, "cu124", "CUDA 12.4 (RTX 20/30/40-series)"
            elif re.search(r"GTX\s*1[0-9]{3}|GTX\s*9[0-9]{2}", name):
                return name, "cu118", "CUDA 11.8 (GTX 10/9-series)"
            else:
                return name, "cu124", "CUDA 12.4 (default)"
    except Exception:
        pass
    return None, None, None


def find_python():
    """Find a suitable Python 3.10+ executable.

    Returns (cmd, version_string) or (None, None).
    """
    for cmd in ["python", "python3", "py"]:
        try:
            result = subprocess.run(
                [cmd, "--version"], capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                version_text = result.stdout + result.stderr
                m = re.search(r"Python (\d+)\.(\d+)\.?(\d*)", version_text)
                if m and int(m.group(1)) >= 3 and int(m.group(2)) >= 10:
                    return cmd, m.group(0)
        except Exception:
            continue
    return None, None


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class InstallerApp:
    """Multi-page wizard installer for WhisperSync."""

    def __init__(self, root):
        self.root = root
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Center window
        sx = (self.root.winfo_screenwidth() - WIN_W) // 2
        sy = (self.root.winfo_screenheight() - WIN_H) // 2
        self.root.geometry(f"{WIN_W}x{WIN_H}+{sx}+{sy}")

        # Paths
        self.script_root = Path(__file__).parent.parent
        self.pkg_dir = self.script_root / "whisper_sync"
        self.venv_path = self.script_root / "whisper-env"

        # State
        self.current_page = 0
        self.installing = False
        self.install_complete = False
        self._spinner_after_id = None
        self._spinner_line_index = None
        self._spinner_tick = 0
        self._spinner_label = ""
        self._spinner_step = 0
        self.output_dir_result = ""  # set after install

        # Detect hardware before building UI
        self.gpu_name, self.cuda_version, self.cuda_label = detect_gpu()
        self.python_cmd, self.python_version = find_python()

        # Build styles and structure
        self._build_styles()
        self._build_skeleton()
        self._build_pages()
        self._show_page(0)

    # ------------------------------------------------------------------
    # Styles
    # ------------------------------------------------------------------

    def _build_styles(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Dark.TFrame", background=BG)
        style.configure("DarkLight.TFrame", background=BG_LIGHT)
        style.configure("Dark.TLabel", background=BG, foreground=FG,
                         font=("Segoe UI", 10))
        style.configure("Header.TLabel", background=BG, foreground=ACCENT,
                         font=("Segoe UI", 18, "bold"))
        style.configure("SubHeader.TLabel", background=BG, foreground=FG_DIM,
                         font=("Segoe UI", 10))
        style.configure("Section.TLabel", background=BG, foreground=ACCENT,
                         font=("Segoe UI", 11, "bold"))
        style.configure("Dark.TCheckbutton", background=BG, foreground=FG,
                         font=("Segoe UI", 10))
        style.map("Dark.TCheckbutton",
                   background=[("active", BG), ("!active", BG)],
                   foreground=[("active", FG), ("!active", FG)])
        style.configure("Dark.Horizontal.TProgressbar",
                         troughcolor=BG_LIGHT, background=ACCENT)

    # ------------------------------------------------------------------
    # Skeleton: container frame + bottom nav bar
    # ------------------------------------------------------------------

    def _build_skeleton(self):
        # Container for page content
        self.container = tk.Frame(self.root, bg=BG)
        self.container.pack(fill="both", expand=True)

        # Bottom navigation bar
        self.nav_bar = tk.Frame(self.root, bg=BG_LIGHT, height=52)
        self.nav_bar.pack(fill="x", side="bottom")
        self.nav_bar.pack_propagate(False)

        nav_inner = tk.Frame(self.nav_bar, bg=BG_LIGHT)
        nav_inner.pack(expand=True)

        self.back_btn = tk.Button(
            nav_inner, text="Back", bg=BG, fg=FG,
            font=("Segoe UI", 10), relief="flat", bd=0,
            padx=16, pady=6, activebackground=ACCENT, activeforeground=BTN_FG,
            command=self._on_back,
        )
        self.back_btn.pack(side="left", padx=(0, 12))

        self.next_btn = tk.Button(
            nav_inner, text="Next", bg=BTN_BG, fg=BTN_FG,
            font=("Segoe UI", 11, "bold"), relief="flat", bd=0,
            padx=24, pady=6, activebackground=GREEN, activeforeground=BTN_FG,
            command=self._on_next,
        )
        self.next_btn.pack(side="left")

        # Status label (left side of nav bar)
        self.status_var = tk.StringVar(value="")
        self.status_label = tk.Label(
            self.nav_bar, textvariable=self.status_var,
            bg=BG_LIGHT, fg=FG_DIM, font=("Segoe UI", 9),
            anchor="w", padx=12,
        )
        self.status_label.place(x=0, rely=0.5, anchor="w")

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------

    def _build_pages(self):
        self.pages = []

        # Page 0: Welcome
        p0 = tk.Frame(self.container, bg=BG)
        self._build_welcome_page(p0)
        self.pages.append(p0)

        # Page 1: Configuration
        p1 = tk.Frame(self.container, bg=BG)
        self._build_config_page(p1)
        self.pages.append(p1)

        # Page 2: Installation Progress
        p2 = tk.Frame(self.container, bg=BG)
        self._build_progress_page(p2)
        self.pages.append(p2)

        # Page 3: Get Started
        p3 = tk.Frame(self.container, bg=BG)
        self._build_getstarted_page(p3)
        self.pages.append(p3)

    def _show_page(self, index):
        # Hide all pages
        for p in self.pages:
            p.place_forget()

        self.current_page = index
        self.pages[index].place(x=0, y=0, relwidth=1, relheight=1)

        # Update title
        self.root.title(f"WhisperSync Installer - Step {index + 1} of 4")

        # Update nav buttons
        self._update_nav()

    def _update_nav(self):
        page = self.current_page

        # Back button visibility
        if page == 0 or page == 2 or page == 3:
            self.back_btn.pack_forget()
        else:
            self.back_btn.pack(side="left", padx=(0, 12))

        # Next button text and state
        if page == 0:
            self.next_btn.configure(text="Next", bg=BTN_BG, state="normal")
            if not self.python_cmd:
                self.next_btn.configure(state="disabled")
        elif page == 1:
            self.next_btn.configure(text="Install", bg=BTN_BG, state="normal")
        elif page == 2:
            if self.installing:
                self.next_btn.configure(
                    text="Installing...", bg=BG_LIGHT, state="disabled",
                )
            elif self.install_complete:
                self.next_btn.configure(
                    text="Next", bg=GREEN, state="normal",
                )
            else:
                self.next_btn.configure(
                    text="Install", bg=BTN_BG, state="normal",
                )
        elif page == 3:
            self.next_btn.configure(
                text="Launch WhisperSync", bg=GREEN, state="normal",
            )

    # ------------------------------------------------------------------
    # Page 0: Welcome
    # ------------------------------------------------------------------

    def _build_welcome_page(self, parent):
        pad = tk.Frame(parent, bg=BG)
        pad.pack(fill="both", expand=True, padx=28, pady=20)

        # Title
        ttk.Label(pad, text="WhisperSync", style="Header.TLabel").pack(
            anchor="w", pady=(20, 0),
        )
        tk.Label(
            pad, text=f"v{VERSION}", bg=BG, fg=FG_DIM,
            font=("Segoe UI", 10),
        ).pack(anchor="w", pady=(0, 16))

        # Description
        desc = tk.Label(
            pad,
            text="Local speech-to-text for Windows.\n\n"
                 "Dictation, meeting transcription, and speaker\n"
                 "identification - all running on your machine.\n"
                 "Audio never leaves your computer.",
            bg=BG, fg=FG, font=("Segoe UI", 10),
            justify="left", anchor="w",
        )
        desc.pack(anchor="w", pady=(0, 24))

        # Separator
        tk.Frame(pad, bg=FG_DIM, height=1).pack(fill="x", pady=(0, 16))

        # Auto-detected info
        ttk.Label(pad, text="Detected Hardware", style="Section.TLabel").pack(
            anchor="w", pady=(0, 10),
        )

        # Python
        py_frame = tk.Frame(pad, bg=BG)
        py_frame.pack(fill="x", pady=(0, 6))
        tk.Label(
            py_frame, text="Python:", bg=BG, fg=FG_DIM,
            font=("Segoe UI", 10), width=10, anchor="w",
        ).pack(side="left")
        if self.python_cmd:
            tk.Label(
                py_frame, text=self.python_version, bg=BG, fg=GREEN,
                font=("Segoe UI", 10),
            ).pack(side="left")
        else:
            tk.Label(
                py_frame,
                text="Not found - install Python 3.10+ from python.org",
                bg=BG, fg=RED, font=("Segoe UI", 10),
            ).pack(side="left")

        # GPU
        gpu_frame = tk.Frame(pad, bg=BG)
        gpu_frame.pack(fill="x", pady=(0, 6))
        tk.Label(
            gpu_frame, text="GPU:", bg=BG, fg=FG_DIM,
            font=("Segoe UI", 10), width=10, anchor="w",
        ).pack(side="left")
        if self.gpu_name:
            tk.Label(
                gpu_frame, text=self.gpu_name, bg=BG, fg=GREEN,
                font=("Segoe UI", 10),
            ).pack(side="left")
        else:
            tk.Label(
                gpu_frame, text="No GPU detected - CPU mode",
                bg=BG, fg=YELLOW, font=("Segoe UI", 10),
            ).pack(side="left")

        # CUDA version
        if self.cuda_label:
            cuda_frame = tk.Frame(pad, bg=BG)
            cuda_frame.pack(fill="x", pady=(0, 6))
            tk.Label(
                cuda_frame, text="CUDA:", bg=BG, fg=FG_DIM,
                font=("Segoe UI", 10), width=10, anchor="w",
            ).pack(side="left")
            tk.Label(
                cuda_frame, text=self.cuda_label, bg=BG, fg=FG,
                font=("Segoe UI", 10),
            ).pack(side="left")

        # Python not found warning
        if not self.python_cmd:
            tk.Frame(pad, bg=FG_DIM, height=1).pack(fill="x", pady=(16, 12))
            tk.Label(
                pad,
                text="Python 3.10+ is required. Install it and restart the installer.",
                bg=BG, fg=RED, font=("Segoe UI", 10),
            ).pack(anchor="w")

    # ------------------------------------------------------------------
    # Page 1: Configuration
    # ------------------------------------------------------------------

    def _build_config_page(self, parent):
        pad = tk.Frame(parent, bg=BG)
        pad.pack(fill="both", expand=True, padx=28, pady=20)

        ttk.Label(pad, text="Configuration", style="Header.TLabel").pack(
            anchor="w", pady=(10, 4),
        )
        ttk.Label(
            pad, text="Set your preferences before installing.",
            style="SubHeader.TLabel",
        ).pack(anchor="w", pady=(0, 20))

        # -- Output folder --
        ttk.Label(pad, text="Output folder", style="Section.TLabel").pack(
            anchor="w", pady=(0, 4),
        )
        tk.Label(
            pad,
            text="Where recordings and transcriptions will be saved.",
            bg=BG, fg=FG_DIM, font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(0, 4))

        folder_frame = tk.Frame(pad, bg=BG)
        folder_frame.pack(fill="x", pady=(0, 16))

        default_out = str(get_default_output_dir())

        self.output_var = tk.StringVar(value=default_out)
        self.output_entry = tk.Entry(
            folder_frame, textvariable=self.output_var,
            bg=ENTRY_BG, fg=ENTRY_FG, insertbackground=FG,
            font=("Segoe UI", 9), relief="flat", bd=4,
        )
        self.output_entry.pack(side="left", fill="x", expand=True)

        browse_btn = tk.Button(
            folder_frame, text="Browse", bg=BG_LIGHT, fg=FG,
            font=("Segoe UI", 9), relief="flat", bd=2,
            activebackground=ACCENT, activeforeground=BTN_FG,
            command=self._browse_folder,
        )
        browse_btn.pack(side="left", padx=(6, 0))

        # -- HuggingFace token --
        ttk.Label(
            pad, text="HuggingFace token", style="Section.TLabel",
        ).pack(anchor="w", pady=(0, 4))
        tk.Label(
            pad,
            text="Optional - enables speaker identification in meetings.\n"
                 "Get a free token at huggingface.co/settings/tokens",
            bg=BG, fg=FG_DIM, font=("Segoe UI", 9), justify="left",
        ).pack(anchor="w", pady=(0, 4))

        self.hf_var = tk.StringVar()
        # Pre-fill if token exists
        hf_token_file = Path.home() / ".huggingface" / "token"
        if hf_token_file.exists():
            try:
                existing_token = hf_token_file.read_text(encoding="utf-8").strip()
                if existing_token:
                    self.hf_var.set(existing_token)
            except OSError:
                pass

        self.hf_entry = tk.Entry(
            pad, textvariable=self.hf_var, show="*",
            bg=ENTRY_BG, fg=ENTRY_FG, insertbackground=FG,
            font=("Segoe UI", 9), relief="flat", bd=4,
        )
        self.hf_entry.pack(fill="x", pady=(0, 16))

        # -- Shortcut options --
        ttk.Label(pad, text="Shortcuts", style="Section.TLabel").pack(
            anchor="w", pady=(0, 6),
        )

        self.desktop_var = tk.BooleanVar(value=True)
        self.startup_var = tk.BooleanVar(value=False)

        ttk.Checkbutton(
            pad, text="Create Desktop shortcut",
            variable=self.desktop_var, style="Dark.TCheckbutton",
        ).pack(anchor="w", pady=(0, 2))
        ttk.Checkbutton(
            pad, text="Auto-launch on login",
            variable=self.startup_var, style="Dark.TCheckbutton",
        ).pack(anchor="w")

    # ------------------------------------------------------------------
    # Page 2: Installation Progress
    # ------------------------------------------------------------------

    def _build_progress_page(self, parent):
        pad = tk.Frame(parent, bg=BG)
        pad.pack(fill="both", expand=True, padx=28, pady=20)

        ttk.Label(pad, text="Installing", style="Header.TLabel").pack(
            anchor="w", pady=(10, 4),
        )
        self.progress_subtitle = tk.Label(
            pad, text="Preparing installation...",
            bg=BG, fg=FG_DIM, font=("Segoe UI", 10),
        )
        self.progress_subtitle.pack(anchor="w", pady=(0, 12))

        # Log widget with scrollbar
        log_frame = tk.Frame(pad, bg=BG_LIGHT)
        log_frame.pack(fill="both", expand=True, pady=(0, 8))

        self.log_text = tk.Text(
            log_frame, bg=BG_LIGHT, fg=FG,
            font=("Consolas", 9), relief="flat", bd=6,
            wrap="word", state="disabled", insertbackground=FG,
        )
        log_scroll = tk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)

        # Text tags
        self.log_text.tag_configure("ok", foreground=GREEN)
        self.log_text.tag_configure("warn", foreground=YELLOW)
        self.log_text.tag_configure("error", foreground=RED)
        self.log_text.tag_configure("step", foreground=ACCENT)
        self.log_text.tag_configure("info", foreground=FG_DIM)

        # Progress bar
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(
            pad, variable=self.progress_var, maximum=100,
            style="Dark.Horizontal.TProgressbar",
        )
        self.progress_bar.pack(fill="x", pady=(0, 4))

    # ------------------------------------------------------------------
    # Page 3: Get Started
    # ------------------------------------------------------------------

    def _build_getstarted_page(self, parent):
        pad = tk.Frame(parent, bg=BG)
        pad.pack(fill="both", expand=True, padx=28, pady=20)

        # Scrollable content
        canvas = tk.Canvas(pad, bg=BG, highlightthickness=0)
        scrollbar = tk.Scrollbar(pad, orient="vertical", command=canvas.yview)
        self.gs_frame = tk.Frame(canvas, bg=BG)

        self.gs_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=self.gs_frame, anchor="nw",
                             width=WIN_W - 56 - 16)
        canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        # Mousewheel scrolling
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        self._populate_getstarted()

        # Extra buttons at bottom (outside scroll)
        btn_frame = tk.Frame(parent, bg=BG)
        btn_frame.pack(fill="x", padx=28, pady=(0, 4))

        self.bench_btn = tk.Button(
            btn_frame, text="Run Benchmark", bg=BG_LIGHT, fg=FG,
            font=("Segoe UI", 10), relief="flat", bd=0,
            padx=14, pady=5, activebackground=ACCENT, activeforeground=BTN_FG,
            command=self._on_benchmark,
        )
        self.bench_btn.pack(side="left", padx=(0, 8))

        self.howworks_btn = tk.Button(
            btn_frame, text="See How It Works", bg=BG_LIGHT, fg=FG,
            font=("Segoe UI", 10), relief="flat", bd=0,
            padx=14, pady=5, activebackground=ACCENT, activeforeground=BTN_FG,
            command=self._on_howworks,
        )
        self.howworks_btn.pack(side="left")

    def _populate_getstarted(self):
        f = self.gs_frame

        # Success header - cyan H1, not green
        tk.Label(
            f, text="Installation Complete",
            bg=BG, fg=ACCENT, font=("Segoe UI", 14, "bold"),
        ).pack(anchor="w", pady=(10, 2))
        tk.Label(
            f, text="WhisperSync is ready to use.",
            bg=BG, fg=FG, font=("Segoe UI", 10),
        ).pack(anchor="w", pady=(0, 14))

        # Feature comparison - horizontal layout
        tk.Label(
            f, text="What runs where",
            bg=BG, fg=ACCENT, font=("Segoe UI", 11, "bold"),
        ).pack(anchor="w", pady=(0, 6))

        features = [
            ("Dictation transcription", "Local", GREEN),
            ("Meeting transcription", "Local", GREEN),
            ("Speaker identification", "Local", GREEN),
            ("Audio recording", "Local", GREEN),
            ("Meeting minutes", "Cloud (Claude CLI)", ACCENT),
        ]
        for feat_name, location, color in features:
            row = tk.Frame(f, bg=BG)
            row.pack(fill="x", pady=1)
            tk.Label(
                row, text=feat_name, bg=BG, fg=FG,
                font=("Segoe UI", 9), anchor="w",
            ).pack(side="left")
            tk.Label(
                row, text=location, bg=BG, fg=color,
                font=("Segoe UI", 9), anchor="e",
            ).pack(side="right")

        tk.Label(
            f, text="All audio stays on your machine.",
            bg=BG, fg=FG_DIM, font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(6, 14))

        # Separator
        tk.Frame(f, bg=FG_DIM, height=1).pack(fill="x", pady=(0, 12))

        # GET STARTED header - cyan H1
        tk.Label(
            f, text="Get Started",
            bg=BG, fg=ACCENT, font=("Segoe UI", 11, "bold"),
        ).pack(anchor="w", pady=(0, 2))
        tk.Label(
            f, text="Launch WhisperSync, then try these:",
            bg=BG, fg=FG_DIM, font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(0, 10))

        # Try Dictation - white H2
        tk.Label(
            f, text="Dictation", bg=BG, fg=FG,
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w", pady=(0, 3))
        dictation_steps = [
            ("1. Click any text box (Notepad, browser, chat)", FG_DIM),
            ("2. Press ", FG_DIM, "Ctrl+Shift+Space", ACCENT),
            ("3. Speak, then press the same hotkey again", FG_DIM),
            ("4. Text appears where your cursor is", FG_DIM),
        ]
        for item in dictation_steps:
            if len(item) == 2:
                tk.Label(
                    f, text=f"    {item[0]}", bg=BG, fg=item[1],
                    font=("Segoe UI", 9), anchor="w",
                ).pack(anchor="w")
            else:
                row = tk.Frame(f, bg=BG)
                row.pack(anchor="w")
                tk.Label(
                    row, text=f"    {item[0]}", bg=BG, fg=item[1],
                    font=("Segoe UI", 9), anchor="w",
                ).pack(side="left")
                tk.Label(
                    row, text=item[2], bg=BG, fg=item[3],
                    font=("Segoe UI", 9, "bold"), anchor="w",
                ).pack(side="left")

        tk.Label(f, text="", bg=BG).pack()  # spacer

        # Try a Meeting - white H2
        tk.Label(
            f, text="Meetings", bg=BG, fg=FG,
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w", pady=(0, 3))
        meeting_steps = [
            ("1. Press ", FG_DIM, "Ctrl+Shift+M", ACCENT),
            ("2. The tray icon turns red - recording", FG_DIM),
            ("3. Talk, join a call, play a video", FG_DIM),
            ("4. Press the same hotkey to stop and save", FG_DIM),
            ("5. Transcript with speaker names saved to your folder", FG_DIM),
        ]
        for item in meeting_steps:
            if len(item) == 2:
                tk.Label(
                    f, text=f"    {item[0]}", bg=BG, fg=item[1],
                    font=("Segoe UI", 9), anchor="w",
                ).pack(anchor="w")
            else:
                row = tk.Frame(f, bg=BG)
                row.pack(anchor="w")
                tk.Label(
                    row, text=f"    {item[0]}", bg=BG, fg=item[1],
                    font=("Segoe UI", 9), anchor="w",
                ).pack(side="left")
                tk.Label(
                    row, text=item[2], bg=BG, fg=item[3],
                    font=("Segoe UI", 9, "bold"), anchor="w",
                ).pack(side="left")

    # ------------------------------------------------------------------
    # Navigation handlers
    # ------------------------------------------------------------------

    def _on_back(self):
        if self.current_page == 1:
            self._show_page(0)

    def _on_next(self):
        page = self.current_page
        if page == 0:
            self._show_page(1)
        elif page == 1:
            self._start_install()
        elif page == 2:
            if self.install_complete:
                self._show_page(3)
        elif page == 3:
            self._launch()

    def _on_close(self):
        if self.installing:
            import tkinter.messagebox as mb
            if not mb.askokcancel(
                "Installation Running",
                "Installation is in progress. Closing may leave "
                "a partial install.\n\nClose anyway?",
            ):
                return
        self.root.destroy()

    # ------------------------------------------------------------------
    # Browse folder
    # ------------------------------------------------------------------

    def _browse_folder(self):
        folder = filedialog.askdirectory(
            title="Choose output folder",
            initialdir=self.output_var.get(),
        )
        if folder:
            self.output_var.set(folder)

    # ------------------------------------------------------------------
    # Log helpers (thread-safe)
    # ------------------------------------------------------------------

    def _log(self, text, tag=None):
        """Append a line to the log widget. Thread-safe."""
        def _do():
            self.log_text.configure(state="normal")
            if tag:
                self.log_text.insert("end", text + "\n", tag)
            else:
                self.log_text.insert("end", text + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.root.after(0, _do)

    def _log_step(self, num, text):
        self._log(f"[{num}/{TOTAL_INSTALL_STEPS}] {text}", "step")

    def _log_ok(self, text):
        self._log(f"      [OK] {text}", "ok")

    def _log_warn(self, text):
        self._log(f"      [!]  {text}", "warn")

    def _log_info(self, text):
        self._log(f"      {text}", "info")

    def _set_progress(self, value):
        self.root.after(0, lambda: self.progress_var.set(value))

    def _set_subtitle(self, text):
        self.root.after(0, lambda: self.progress_subtitle.configure(text=text))

    # ------------------------------------------------------------------
    # Spinner animation
    # ------------------------------------------------------------------

    def _start_spinner(self, step_num, label):
        """Start an animated spinner on a log line. Call from worker thread."""
        self._spinner_tick = 0
        self._spinner_label = label
        self._spinner_step = step_num

        def _insert():
            self.log_text.configure(state="normal")
            line_text = f"[{step_num}/{TOTAL_INSTALL_STEPS}] {label} {SPINNER_CHARS[0]}"
            self.log_text.insert("end", line_text + "\n", "step")
            self._spinner_line_index = int(
                self.log_text.index("end-2l").split(".")[0]
            )
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

        self.root.after(0, _insert)
        self.root.after(100, self._animate_spinner)

    def _animate_spinner(self):
        if self._spinner_line_index is None:
            return
        self._spinner_tick += 1
        char = SPINNER_CHARS[self._spinner_tick % len(SPINNER_CHARS)]
        line_idx = self._spinner_line_index
        prefix = f"[{self._spinner_step}/{TOTAL_INSTALL_STEPS}] {self._spinner_label} "

        def _update():
            self.log_text.configure(state="normal")
            self.log_text.delete(f"{line_idx}.0", f"{line_idx}.end")
            self.log_text.insert(f"{line_idx}.0", f"{prefix}{char}", "step")
            self.log_text.configure(state="disabled")

        self.root.after(0, _update)
        self._spinner_after_id = self.root.after(300, self._animate_spinner)

    def _stop_spinner(self, ok_text=None, warn_text=None):
        """Stop spinner and replace line with result. Call from worker thread."""
        if self._spinner_after_id is not None:
            self.root.after_cancel(self._spinner_after_id)
            self._spinner_after_id = None

        line_idx = self._spinner_line_index
        self._spinner_line_index = None

        if ok_text:
            final_text = f"[OK] {ok_text}"
            tag = "ok"
        elif warn_text:
            final_text = f"[!]  {warn_text}"
            tag = "warn"
        else:
            return

        def _update():
            self.log_text.configure(state="normal")
            if line_idx:
                self.log_text.delete(f"{line_idx}.0", f"{line_idx}.end")
                self.log_text.insert(f"{line_idx}.0", f"      {final_text}", tag)
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

        self.root.after(0, _update)

    # ------------------------------------------------------------------
    # Silent subprocess runner
    # ------------------------------------------------------------------

    def _run_silent(self, args, env=None):
        """Run subprocess silently. Returns (success, elapsed_secs, stderr)."""
        merged_env = os.environ.copy()
        merged_env["GIT_TERMINAL_PROMPT"] = "0"
        merged_env["GIT_ASKPASS"] = ""
        merged_env["PYTHONWARNINGS"] = (
            "ignore::UserWarning:pyannote.audio.core.io,"
            "ignore::UserWarning:pyannote.audio.utils.reproducibility"
        )
        if env:
            merged_env.update(env)
        try:
            t0 = time.perf_counter()
            proc = subprocess.run(
                args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env=merged_env, cwd=str(self.script_root),
            )
            elapsed = time.perf_counter() - t0
            if proc.returncode != 0:
                return False, elapsed, proc.stderr
            return True, elapsed, ""
        except Exception as e:
            return False, 0, str(e)

    # ------------------------------------------------------------------
    # Installation
    # ------------------------------------------------------------------

    def _start_install(self):
        """Validate config page inputs and begin installation."""
        output_dir = self.output_var.get().strip()
        if not output_dir:
            import tkinter.messagebox as mb
            mb.showwarning("Missing field", "Please enter an output folder.")
            return
        if not os.path.isabs(output_dir):
            import tkinter.messagebox as mb
            mb.showwarning(
                "Invalid path",
                "Please enter a full path (e.g. C:\\Users\\...).",
            )
            return

        # Switch to progress page
        self._show_page(2)
        self.installing = True
        self._update_nav()

        # Disable config inputs
        self.output_entry.configure(state="disabled")
        self.hf_entry.configure(state="disabled")

        thread = threading.Thread(target=self._run_install, daemon=True)
        thread.start()

    def _run_install(self):
        """Execute all installation steps in a background thread."""
        python = self.python_cmd
        venv_python = str(self.venv_path / "Scripts" / "python.exe")
        venv_pip = str(self.venv_path / "Scripts" / "pip.exe")
        requirements = str(self.script_root / "requirements.txt")
        output_dir = self.output_var.get().strip()
        current_step = 0

        def advance(label):
            nonlocal current_step
            current_step += 1
            pct = (current_step - 1) / TOTAL_INSTALL_STEPS * 100
            self._set_progress(pct)
            self._set_subtitle(f"Step {current_step}/{TOTAL_INSTALL_STEPS}: {label}")
            self.root.after(
                0,
                lambda: self.status_var.set(
                    f"Step {current_step}/{TOTAL_INSTALL_STEPS}: {label}"
                ),
            )
            return current_step

        try:
            # -- Step 1: Create virtual environment --
            n = advance("Creating virtual environment")
            self._log_step(n, "Creating virtual environment...")
            if self.venv_path.exists():
                self._log_warn("Venv already exists, reusing it")
            else:
                self._start_spinner(n, "Creating virtual environment...")
                ok, elapsed, err = self._run_silent(
                    [python, "-m", "venv", str(self.venv_path)],
                )
                if not ok:
                    self._stop_spinner(warn_text=f"Failed: {err[:120]}")
                    self._fail("Failed to create virtual environment")
                    return
                self._stop_spinner(
                    ok_text=f"Virtual environment created ({int(elapsed)}s)",
                )
                time.sleep(0.1)
            self._log_ok("Virtual environment ready")

            # -- Step 2: Install dependencies --
            n = advance("Installing dependencies")
            self._log_step(n, "Installing dependencies...")

            self._start_spinner(n, "Upgrading pip...")
            ok, elapsed, err = self._run_silent(
                [venv_python, "-m", "pip", "install", "--upgrade", "pip", "-qq"],
            )
            if not ok:
                self._stop_spinner(warn_text=f"pip upgrade failed: {err[:120]}")
                self._fail("Failed to upgrade pip")
                return
            self._stop_spinner(ok_text=f"Upgrading pip ({int(elapsed)}s)")
            time.sleep(0.1)

            self._start_spinner(n, "Installing dependencies...")
            ok, elapsed, err = self._run_silent(
                [venv_pip, "install", "-r", requirements, "-qq"],
            )
            if not ok:
                self._stop_spinner(warn_text=f"Dependencies failed: {err[:200]}")
                self._fail("Failed to install dependencies")
                return
            self._stop_spinner(ok_text=f"Dependencies installed ({int(elapsed)}s)")
            time.sleep(0.1)

            # -- Step 3: Install PyTorch --
            n = advance("Installing PyTorch")
            if self.cuda_version:
                self._log_step(n, f"Installing PyTorch ({self.cuda_version})...")
                torch_url = (
                    f"https://download.pytorch.org/whl/{self.cuda_version}"
                )
                self._start_spinner(n, "Installing PyTorch...")
                ok, elapsed, err = self._run_silent(
                    [
                        venv_pip, "install", "torch", "torchaudio",
                        "--index-url", torch_url,
                        "--force-reinstall", "--no-deps", "-qq",
                    ],
                )
                if not ok:
                    self._stop_spinner(
                        warn_text="GPU PyTorch failed, falling back to CPU",
                    )
                else:
                    self._stop_spinner(
                        ok_text=f"PyTorch GPU installed ({int(elapsed)}s)",
                    )
                time.sleep(0.1)
            else:
                self._log_step(n, "Skipping GPU PyTorch (no GPU detected)")
                self._log_ok("Using CPU PyTorch (included in dependencies)")

            # -- Step 4: Downloading models --
            n = advance("Downloading models")
            self._log_step(n, "Downloading models...")

            # Standalone marker
            marker = self.pkg_dir / ".standalone"
            if not marker.exists():
                marker.write_text("")

            fd, bootstrap_path = tempfile.mkstemp(
                suffix=".py", prefix="ws-bootstrap-",
            )
            os.close(fd)
            bootstrap_script = Path(bootstrap_path)
            bootstrap_code = (
                "import warnings, os, sys\n"
                "warnings.filterwarnings('ignore', message='torchcodec', category=UserWarning, module='pyannote')\n"
                "warnings.filterwarnings('ignore', message='TensorFloat', category=UserWarning, module='pyannote')\n"
                "os.environ['HF_HUB_DISABLE_SYMLINKS_WARNING'] = '1'\n"
                f"sys.path.insert(0, {repr(str(self.script_root))})\n"
                "from whisper_sync.model_status import bootstrap_models\n"
                "from whisper_sync import config\n"
                "bootstrap_models(config.load())\n"
            )
            bootstrap_script.write_text(bootstrap_code, encoding="utf-8")

            self._start_spinner(n, "Downloading models...")
            ok, elapsed, err = self._run_silent(
                [venv_python, str(bootstrap_script)],
            )
            bootstrap_script.unlink(missing_ok=True)
            if not ok:
                self._stop_spinner(
                    warn_text="Model download had issues - download later via tray menu",
                )
            else:
                self._stop_spinner(ok_text=f"Models cached ({int(elapsed)}s)")
            time.sleep(0.1)

            # -- Step 5: Writing configuration --
            n = advance("Writing configuration")
            self._log_step(n, "Writing configuration...")

            output_path = Path(output_dir)
            ws_dir = output_path / ".whispersync"
            if ws_dir.exists() and ws_dir.is_file():
                self._log_warn(".whispersync exists as a file, removing")
                ws_dir.unlink()
            try:
                output_path.mkdir(parents=True, exist_ok=True)
                ws_dir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                self._fail(f"Cannot create output folder: {e}")
                return

            config_path = ws_dir / "config.json"

            # Preserve existing config
            existing_cfg = {}
            if config_path.exists():
                try:
                    existing_cfg = json.loads(
                        config_path.read_text(encoding="utf-8"),
                    )
                    self._log_ok("Found existing config, preserving settings")
                except (json.JSONDecodeError, OSError):
                    self._log_warn("Existing config was corrupt, starting fresh")

            existing_cfg["output_dir"] = output_dir.replace("\\", "/")
            config_path.write_text(
                json.dumps(existing_cfg, indent=2), encoding="utf-8",
            )

            # Bootstrap pointer (legacy location)
            legacy_cfg = self.pkg_dir / "config.json"
            bootstrap_json = json.dumps(
                {"output_dir": output_dir.replace("\\", "/")}, indent=2,
            )
            legacy_cfg.write_text(bootstrap_json, encoding="utf-8")
            self._log_ok(f"Recordings will save to {output_dir}")

            # HuggingFace token
            hf_token = self.hf_var.get().strip()
            if hf_token:
                hf_dir = Path.home() / ".huggingface"
                hf_dir.mkdir(parents=True, exist_ok=True)
                (hf_dir / "token").write_text(hf_token, encoding="ascii")
                self._log_ok("HuggingFace token saved")
            else:
                hf_token_file = Path.home() / ".huggingface" / "token"
                if hf_token_file.exists():
                    self._log_ok("HuggingFace token found (existing)")
                else:
                    self._log_warn(
                        "No HF token - meeting mode won't identify speakers",
                    )
                    self._log_info("You can add it later via the tray menu")

            # -- Step 6: Creating shortcuts --
            n = advance("Creating shortcuts")
            self._log_step(n, "Creating shortcuts...")
            launcher = self.script_root / "start.ps1"
            icon_path = self.pkg_dir / "whisper-capture.ico"

            if self.desktop_var.get():
                desktop = Path(
                    os.path.join(
                        os.environ.get("USERPROFILE", ""), "Desktop",
                    ),
                )
                try:
                    self._create_shortcut(
                        desktop / "WhisperSync.lnk", launcher, icon_path,
                    )
                    self._log_ok("Desktop shortcut created")
                except Exception as e:
                    self._log_warn(f"Desktop shortcut failed: {e}")

            if self.startup_var.get():
                startup_dir = (
                    Path(os.environ.get("APPDATA", ""))
                    / "Microsoft" / "Windows" / "Start Menu"
                    / "Programs" / "Startup"
                )
                try:
                    self._create_shortcut(
                        startup_dir / "WhisperSync.lnk", launcher, icon_path,
                    )
                    self._log_ok("Startup shortcut created")
                except Exception as e:
                    self._log_warn(f"Startup shortcut failed: {e}")

            if not self.desktop_var.get() and not self.startup_var.get():
                self._log_info("No shortcuts requested, skipping")

            # -- Step 7: Verifying installation --
            n = advance("Verifying installation")
            self._log_step(n, "Verifying installation...")

            # Check CUDA
            try:
                verify = subprocess.run(
                    [
                        venv_python, "-c",
                        "import torch; a = torch.cuda.is_available(); "
                        "n = torch.cuda.get_device_name(0) if a else 'N/A'; "
                        "print(f'CUDA: {a}  Device: {n}')",
                    ],
                    capture_output=True, text=True, timeout=30,
                    cwd=str(self.script_root),
                )
                if verify.returncode == 0:
                    cuda_line = verify.stdout.strip()
                    if "True" in cuda_line:
                        self._log_ok(cuda_line)
                    else:
                        self._log_warn(cuda_line)
            except Exception:
                self._log_warn("Could not verify CUDA")

            # Check whisperx
            try:
                wx = subprocess.run(
                    [venv_python, "-c", "import whisperx; print('OK')"],
                    capture_output=True, text=True, timeout=15,
                    cwd=str(self.script_root),
                )
                if wx.returncode == 0 and "OK" in wx.stdout:
                    self._log_ok("whisperX ready")
                else:
                    self._log_warn("whisperX import issue (may still work)")
            except Exception:
                self._log_warn("whisperX check timed out")

            # Check sounddevice
            try:
                sd = subprocess.run(
                    [venv_python, "-c", "import sounddevice; print('OK')"],
                    capture_output=True, text=True, timeout=15,
                    cwd=str(self.script_root),
                )
                if sd.returncode == 0 and "OK" in sd.stdout:
                    self._log_ok("Audio capture ready")
                else:
                    self._log_warn("sounddevice import issue")
            except Exception:
                self._log_warn("sounddevice check timed out")

            # -- Complete --
            self._set_progress(100)
            self._set_subtitle("Installation complete!")
            self.root.after(
                0, lambda: self.status_var.set("Installation complete!"),
            )
            self._log("")
            self._log("WhisperSync installed successfully!", "ok")
            self._log(f"Recordings will save to: {output_dir}", "ok")

            self.output_dir_result = output_dir
            self.installing = False
            self.install_complete = True
            self.root.after(0, self._update_nav)

        except Exception as e:
            self._fail(f"Unexpected error: {e}")

    def _fail(self, message):
        """Handle installation failure."""
        self._log(f"\n{message}", "error")
        self._set_subtitle("Installation failed")
        self.root.after(
            0, lambda: self.status_var.set("Installation failed"),
        )
        self.installing = False

        def _reenable():
            self.output_entry.configure(state="normal")
            self.hf_entry.configure(state="normal")
            self.next_btn.configure(
                text="Retry", bg=RED, state="normal",
                command=lambda: self._show_page(1),
            )
        self.root.after(0, _reenable)

    # ------------------------------------------------------------------
    # Shortcut creation
    # ------------------------------------------------------------------

    def _create_shortcut(self, lnk_path, launcher_path, icon_path):
        """Create a Windows shortcut using VBScript."""
        if lnk_path.exists():
            lnk_path.unlink()

        fd, vbs_path = tempfile.mkstemp(suffix=".vbs", prefix="ws-shortcut-")
        os.close(fd)
        vbs_file = Path(vbs_path)
        vbs_content = (
            'Set WshShell = WScript.CreateObject("WScript.Shell")\n'
            f'Set lnk = WshShell.CreateShortcut("{lnk_path}")\n'
            'lnk.TargetPath = '
            '"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"\n'
            'lnk.Arguments = "-ExecutionPolicy Bypass -WindowStyle Minimized '
            f'-File ""{launcher_path}"" -Watchdog"\n'
            f'lnk.WorkingDirectory = "{self.script_root}"\n'
            'lnk.WindowStyle = 7\n'
            'lnk.Description = "WhisperSync - local speech-to-text"\n'
            f'lnk.IconLocation = "{icon_path}, 0"\n'
            'lnk.Save\n'
        )
        vbs_file.write_text(vbs_content, encoding="ascii")
        try:
            result = subprocess.run(
                ["cscript", "//nologo", str(vbs_file)],
                capture_output=True, timeout=10,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"cscript failed: {result.stderr.strip()}"
                )
        finally:
            vbs_file.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Post-install actions
    # ------------------------------------------------------------------

    def _on_benchmark(self):
        """Open a benchmark modal with a live-updating table."""
        self.bench_btn.configure(state="disabled")

        win = tk.Toplevel(self.root)
        win.title("Model Benchmark")
        win.configure(bg=BG)
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()

        bw, bh = 420, 380
        bx = self.root.winfo_x() + (WIN_W - bw) // 2
        by = self.root.winfo_y() + (WIN_H - bh) // 2
        win.geometry(f"{bw}x{bh}+{bx}+{by}")

        # Dark title bar
        try:
            import ctypes
            win.update()
            hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 20,
                ctypes.byref(ctypes.c_int(1)),
                ctypes.sizeof(ctypes.c_int),
            )
        except Exception:
            pass

        tk.Label(
            win, text="Model Benchmark", bg=BG, fg=ACCENT,
            font=("Segoe UI", 14, "bold"),
        ).pack(anchor="w", padx=16, pady=(12, 4))
        tk.Label(
            win, text="Testing cached models on your hardware.",
            bg=BG, fg=FG_DIM, font=("Segoe UI", 9),
        ).pack(anchor="w", padx=16, pady=(0, 10))

        # Table header
        table = tk.Frame(win, bg=BG)
        table.pack(fill="x", padx=16, pady=(0, 8))

        headers = [("Model", 10, "w"), ("Speed", 8, "w"),
                    ("Quality", 8, "w"), ("Status", 12, "w")]
        for col, (text, width, anchor) in enumerate(headers):
            tk.Label(
                table, text=text, bg=BG, fg=ACCENT,
                font=("Segoe UI", 9, "bold"), width=width, anchor=anchor,
            ).grid(row=0, column=col, sticky="w", padx=(0, 4))

        tk.Frame(table, bg=FG_DIM, height=1).grid(
            row=1, column=0, columnspan=4, sticky="ew", pady=4,
        )

        models = ["tiny", "base", "large-v3"]
        quality_map = {"tiny": "Basic", "base": "Good", "large-v3": "Best"}
        row_labels = {}

        for i, model_name in enumerate(models):
            row_idx = i + 2
            tk.Label(
                table, text=model_name, bg=BG, fg=FG,
                font=("Segoe UI", 9), width=10, anchor="w",
            ).grid(row=row_idx, column=0, sticky="w", padx=(0, 4))
            speed_lbl = tk.Label(
                table, text="-", bg=BG, fg=FG_DIM,
                font=("Segoe UI", 9), width=8, anchor="w",
            )
            speed_lbl.grid(row=row_idx, column=1, sticky="w", padx=(0, 4))
            tk.Label(
                table, text=quality_map.get(model_name, ""),
                bg=BG, fg=FG_DIM, font=("Segoe UI", 9),
                width=8, anchor="w",
            ).grid(row=row_idx, column=2, sticky="w", padx=(0, 4))
            status_lbl = tk.Label(
                table, text="Waiting...", bg=BG, fg=FG_DIM,
                font=("Segoe UI", 9), width=12, anchor="w",
            )
            status_lbl.grid(row=row_idx, column=3, sticky="w", padx=(0, 4))
            row_labels[model_name] = (speed_lbl, status_lbl)

        # Recommendation area
        rec_label = tk.Label(
            win, text="", bg=BG, fg=FG_DIM,
            font=("Segoe UI", 9), wraplength=bw - 40, justify="left",
        )
        rec_label.pack(anchor="w", padx=16, pady=(4, 0))

        # Close button
        close_btn = tk.Button(
            win, text="Close", bg=BG_LIGHT, fg=FG,
            font=("Segoe UI", 10), relief="flat", bd=0,
            padx=16, pady=4,
            activebackground=ACCENT, activeforeground=BTN_FG,
            command=win.destroy,
        )
        close_btn.pack(side="bottom", pady=(0, 12))

        def _on_modal_close():
            self.bench_btn.configure(state="normal")
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", _on_modal_close)
        close_btn.configure(command=_on_modal_close)

        def _run():
            self._run_benchmark_live(
                models, row_labels, rec_label, win,
            )
            self.root.after(
                0, lambda: self.bench_btn.configure(state="normal"),
            )

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

    def _run_benchmark_live(self, models, row_labels, rec_label, win):
        """Run benchmark per model, updating the table live."""
        venv_python = str(self.venv_path / "Scripts" / "python.exe")
        results = []

        for model_name in models:
            speed_lbl, status_lbl = row_labels[model_name]

            # Check if model is cached
            fd, check_path = tempfile.mkstemp(
                suffix=".py", prefix="ws-bench-check-",
            )
            os.close(fd)
            check_script = Path(check_path)
            check_script.write_text(
                "import warnings, os, sys\n"
                "warnings.filterwarnings('ignore', message='torchcodec', category=UserWarning, module='pyannote')\n"
                "warnings.filterwarnings('ignore', message='TensorFloat', category=UserWarning, module='pyannote')\n"
                f"sys.path.insert(0, {repr(str(self.script_root))})\n"
                "from whisper_sync.model_status import is_model_cached\n"
                f"print('yes' if is_model_cached({repr(model_name)}) "
                "else 'no')\n",
                encoding="utf-8",
            )
            try:
                r = subprocess.run(
                    [venv_python, str(check_script)],
                    capture_output=True, text=True, timeout=15,
                    cwd=str(self.script_root),
                )
                cached = r.returncode == 0 and "yes" in r.stdout
            except Exception:
                cached = False
            finally:
                check_script.unlink(missing_ok=True)

            if not cached:
                self.root.after(0, lambda s=status_lbl: s.configure(
                    text="Not cached", fg=FG_DIM,
                ))
                continue

            # Update status to testing
            self.root.after(0, lambda s=status_lbl: s.configure(
                text="Testing...", fg=YELLOW,
            ))

            # Run the single-model benchmark
            fd, bench_path = tempfile.mkstemp(
                suffix=".py", prefix="ws-bench-",
            )
            os.close(fd)
            bench_script = Path(bench_path)
            bench_script.write_text(
                "import warnings, os, sys, time, json, numpy as np\n"
                "warnings.filterwarnings('ignore', message='torchcodec', category=UserWarning, module='pyannote')\n"
                "warnings.filterwarnings('ignore', message='TensorFloat', category=UserWarning, module='pyannote')\n"
                "os.environ['HF_HUB_DISABLE_SYMLINKS_WARNING'] = '1'\n"
                f"sys.path.insert(0, {repr(str(self.script_root))})\n"
                "from whisper_sync.transcribe import "
                "_load_whisper_model, _get_device\n"
                "from whisper_sync import config\n"
                "cfg = config.load()\n"
                "device = _get_device()\n"
                "audio = np.zeros(16000 * 5, dtype=np.float32)\n"
                f"name = {repr(model_name)}\n"
                "compute = 'int8' if device == 'cpu' else "
                "cfg.get('compute_type', 'float16')\n"
                "model = _load_whisper_model(name, compute, 'en')\n"
                "model.transcribe(audio, batch_size=4, language='en')\n"
                "t0 = time.perf_counter()\n"
                "for _ in range(3):\n"
                "    model.transcribe(audio, batch_size=4, language='en')\n"
                "avg = (time.perf_counter() - t0) / 3\n"
                "print(json.dumps({'model': name, 'time': round(avg, 2)}))\n",
                encoding="utf-8",
            )

            try:
                merged_env = os.environ.copy()
                merged_env["PYTHONWARNINGS"] = (
                    "ignore::UserWarning:pyannote.audio.core.io,"
                    "ignore::UserWarning:pyannote.audio.utils.reproducibility"
                )
                proc = subprocess.run(
                    [venv_python, str(bench_script)],
                    capture_output=True, text=True,
                    cwd=str(self.script_root), env=merged_env,
                )
                if proc.returncode == 0:
                    # Parse JSON from last non-empty line
                    for line in reversed(proc.stdout.splitlines()):
                        line = line.strip()
                        if line.startswith("{"):
                            data = json.loads(line)
                            avg_time = data["time"]
                            results.append((model_name, avg_time))
                            self.root.after(0, lambda s=speed_lbl, t=avg_time:
                                s.configure(text=f"{t:.2f}s", fg=FG))
                            self.root.after(0, lambda s=status_lbl:
                                s.configure(text="Done", fg=GREEN))
                            break
                    else:
                        self.root.after(0, lambda s=status_lbl:
                            s.configure(text="No output", fg=RED))
                else:
                    self.root.after(0, lambda s=status_lbl:
                        s.configure(text="Failed", fg=RED))
            except Exception:
                self.root.after(0, lambda s=status_lbl:
                    s.configure(text="Error", fg=RED))
            finally:
                bench_script.unlink(missing_ok=True)

        # Show recommendation
        if results:
            fast = [n for n, t in results if t < 0.3]
            all_fast = all(t < 0.5 for _, t in results)
            if all_fast and any(n == "large-v3" for n, _ in results):
                rec = ("Your GPU handles large-v3 well - use it for "
                       "both dictation and meetings.")
            elif fast:
                rec = (f"Recommended: {fast[0]} for dictation (fast), "
                       f"{results[-1][0]} for meetings (accurate).")
            else:
                rec = ("Speed for dictation, accuracy for meetings. "
                       "Pick the tradeoff that fits your workflow.")
        else:
            rec = "No cached models found. Download models first."

        self.root.after(0, lambda: rec_label.configure(text=rec))

    def _on_howworks(self):
        """Show a How It Works modal window."""
        win = tk.Toplevel(self.root)
        win.title("How WhisperSync Works")
        win.configure(bg=BG)
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()

        bw, bh = 450, 500
        bx = self.root.winfo_x() + (WIN_W - bw) // 2
        by = self.root.winfo_y() + (WIN_H - bh) // 2
        win.geometry(f"{bw}x{bh}+{bx}+{by}")

        # Dark title bar
        try:
            import ctypes
            win.update()
            hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 20,
                ctypes.byref(ctypes.c_int(1)),
                ctypes.sizeof(ctypes.c_int),
            )
        except Exception:
            pass

        # Scrollable content
        outer = tk.Frame(win, bg=BG)
        outer.pack(fill="both", expand=True, padx=16, pady=(12, 0))

        canvas = tk.Canvas(outer, bg=BG, highlightthickness=0)
        scrollbar = tk.Scrollbar(outer, orient="vertical",
                                  command=canvas.yview)
        inner = tk.Frame(canvas, bg=BG)
        inner.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=inner, anchor="nw",
                             width=bw - 44)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        def _heading(text):
            tk.Label(
                inner, text=text, bg=BG, fg=ACCENT,
                font=("Segoe UI", 11, "bold"),
            ).pack(anchor="w", pady=(10, 4))

        def _hotkey_row(action, keys):
            row = tk.Frame(inner, bg=BG)
            row.pack(anchor="w", fill="x")
            tk.Label(
                row, text=f"  {action}", bg=BG, fg=FG_DIM,
                font=("Segoe UI", 9), width=22, anchor="w",
            ).pack(side="left")
            tk.Label(
                row, text=keys, bg=BG, fg=ACCENT,
                font=("Segoe UI", 9, "bold"), anchor="w",
            ).pack(side="left")

        def _body(text):
            tk.Label(
                inner, text=text, bg=BG, fg=FG_DIM,
                font=("Segoe UI", 9), justify="left",
                wraplength=bw - 60, anchor="w",
            ).pack(anchor="w", padx=(4, 0), pady=(2, 0))

        def _separator():
            tk.Frame(inner, bg=FG_DIM, height=1).pack(
                fill="x", pady=(8, 0),
            )

        # -- Dictation section --
        _heading("Dictation")
        _hotkey_row("Start recording", "Ctrl+Shift+Space")
        _hotkey_row("Stop and paste", "Ctrl+Shift+Space")
        _hotkey_row("Cancel", "Left-click tray icon")
        _body(
            "Text goes into the focused text field (editor, browser, "
            "chat). If nothing is focused, it copies to your clipboard."
        )

        _separator()

        # -- Meeting section --
        _heading("Meetings")
        _hotkey_row("Start recording", "Ctrl+Shift+M")
        _hotkey_row("Stop and save", "Ctrl+Shift+M")
        _hotkey_row("Alt start/stop", "Left-click tray icon")
        _body(
            "Records your mic + system audio (what you hear). Works "
            "with Zoom, Meet, Teams, phone calls, in-person, and any "
            "audio through your speakers."
        )
        _body(
            "After you stop, name the meeting and get a full transcript "
            "with speaker labels. WhisperSync learns speakers over time "
            "- the more you use it, the better it gets."
        )

        _separator()

        # -- Tray icon section --
        _heading("Tray Icon")

        colors = [
            ("Gray", "Ready", FG_DIM),
            ("Red", "Recording", RED),
            ("Amber", "Transcribing", YELLOW),
            ("Green", "Done", GREEN),
        ]
        for color_name, state_text, color in colors:
            row = tk.Frame(inner, bg=BG)
            row.pack(anchor="w", fill="x")
            tk.Label(
                row, text=f"  {color_name}", bg=BG, fg=color,
                font=("Segoe UI", 9, "bold"), width=10, anchor="w",
            ).pack(side="left")
            tk.Label(
                row, text=state_text, bg=BG, fg=FG_DIM,
                font=("Segoe UI", 9), anchor="w",
            ).pack(side="left")

        tk.Label(inner, text="", bg=BG).pack()  # spacer
        _hotkey_row("Left-click", "Start/cancel meeting")
        _hotkey_row("Right-click", "Settings, models, hotkeys")

        # Close button
        tk.Button(
            win, text="Close", bg=BG_LIGHT, fg=FG,
            font=("Segoe UI", 10), relief="flat", bd=0,
            padx=16, pady=4,
            activebackground=ACCENT, activeforeground=BTN_FG,
            command=win.destroy,
        ).pack(pady=(8, 12))

    # ------------------------------------------------------------------
    # Launch
    # ------------------------------------------------------------------

    def _launch(self):
        self.root.after(
            0, lambda: self.status_var.set("Launching WhisperSync..."),
        )
        launcher = self.script_root / "start.ps1"
        subprocess.Popen(
            [
                "powershell", "-ExecutionPolicy", "Bypass",
                "-WindowStyle", "Minimized",
                "-File", str(launcher), "-Watchdog",
            ],
            cwd=str(self.script_root),
        )
        self.root.after(1500, self.root.destroy)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    root = tk.Tk()

    # Dark title bar via Windows DWM API
    try:
        import ctypes
        root.update()
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
            ctypes.byref(ctypes.c_int(1)), ctypes.sizeof(ctypes.c_int),
        )
    except Exception:
        pass

    InstallerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
