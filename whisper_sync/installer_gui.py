"""WhisperSync GUI Installer - tkinter-based visual installer for Windows."""

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, ttk
from pathlib import Path

VERSION = "1.0"

# -- Colors --
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


def detect_gpu():
    """Detect NVIDIA GPU via nvidia-smi. Returns (gpu_name, cuda_version, cuda_label) or (None, None, None)."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            name = result.stdout.strip().split("\n")[0].strip()
            if re.search(r"RTX\s*50[0-9]{2}|RTX\s*5[0-9]{3}|Blackwell", name):
                return name, "cu128", "CUDA 12.8 (RTX 50-series)"
            elif re.search(r"RTX\s*[2-4]0[0-9]{2}|RTX\s*[2-4][0-9]{3}|A[0-9]{3,4}|L[0-9]{2}", name):
                return name, "cu124", "CUDA 12.4 (RTX 20/30/40-series)"
            elif re.search(r"GTX\s*1[0-9]{3}|GTX\s*9[0-9]{2}", name):
                return name, "cu118", "CUDA 11.8 (GTX 10/9-series)"
            else:
                return name, "cu124", "CUDA 12.4 (default)"
    except Exception:
        pass
    return None, None, None


def find_python():
    """Find a suitable Python 3.10+ executable. Returns (cmd, version_string) or (None, None)."""
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


class InstallerApp:
    TOTAL_STEPS = 9

    def __init__(self, root):
        self.root = root
        self.root.title("WhisperSync Installer")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Center window
        w, h = 560, 660
        sx = (self.root.winfo_screenwidth() - w) // 2
        sy = (self.root.winfo_screenheight() - h) // 2
        self.root.geometry(f"{w}x{h}+{sx}+{sy}")

        self.script_root = Path(__file__).parent.parent
        self.pkg_dir = self.script_root / "whisper_sync"
        self.venv_path = self.script_root / "whisper-env"
        self.installing = False
        self.install_complete = False
        self._spinner_after_id = None
        self._spinner_line_index = None
        self._spinner_tick = 0
        self._spinner_label = ""

        # Detect GPU and Python before building UI
        self.gpu_name, self.cuda_version, self.cuda_label = detect_gpu()
        self.python_cmd, self.python_version = find_python()

        self._build_ui()

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Dark.TFrame", background=BG)
        style.configure("Dark.TLabel", background=BG, foreground=FG, font=("Segoe UI", 10))
        style.configure("Header.TLabel", background=BG, foreground=ACCENT, font=("Segoe UI", 16, "bold"))
        style.configure("Sub.TLabel", background=BG, foreground=FG_DIM, font=("Segoe UI", 9))
        style.configure("Status.TLabel", background=BG_LIGHT, foreground=FG_DIM, font=("Segoe UI", 9))
        style.configure("Accent.TButton", font=("Segoe UI", 11, "bold"))
        style.configure("Dark.TCheckbutton", background=BG, foreground=FG, font=("Segoe UI", 10))
        style.map("Dark.TCheckbutton",
                   background=[("active", BG), ("!active", BG)],
                   foreground=[("active", FG), ("!active", FG)])
        style.configure("Dark.Horizontal.TProgressbar",
                         troughcolor=BG_LIGHT, background=ACCENT)

        main = ttk.Frame(self.root, style="Dark.TFrame")
        main.pack(fill="both", expand=True, padx=20, pady=15)

        # -- Header --
        ttk.Label(main, text="WhisperSync Installer", style="Header.TLabel").pack(anchor="w")
        ttk.Label(main, text=f"v{VERSION}  -  Local speech-to-text for Windows", style="Sub.TLabel").pack(anchor="w", pady=(0, 10))

        # -- GPU Detection --
        gpu_frame = ttk.Frame(main, style="Dark.TFrame")
        gpu_frame.pack(fill="x", pady=(0, 4))
        ttk.Label(gpu_frame, text="GPU:", style="Dark.TLabel").pack(side="left")
        if self.gpu_name:
            gpu_text = f"  {self.gpu_name}"
            gpu_color = GREEN
        else:
            gpu_text = "  No GPU detected - CPU mode (slower)"
            gpu_color = YELLOW
        tk.Label(gpu_frame, text=gpu_text, bg=BG, fg=gpu_color,
                 font=("Segoe UI", 10)).pack(side="left")

        if self.cuda_label:
            cuda_frame = ttk.Frame(main, style="Dark.TFrame")
            cuda_frame.pack(fill="x", pady=(0, 4))
            tk.Label(cuda_frame, text=f"         Selected: {self.cuda_label}",
                     bg=BG, fg=FG_DIM, font=("Segoe UI", 9)).pack(side="left")

        # -- Python Detection --
        py_frame = ttk.Frame(main, style="Dark.TFrame")
        py_frame.pack(fill="x", pady=(0, 10))
        ttk.Label(py_frame, text="Python:", style="Dark.TLabel").pack(side="left")
        if self.python_cmd:
            py_color = GREEN
            py_text = f"  {self.python_version}"
        else:
            py_color = RED
            py_text = "  Not found - install Python 3.10+ from python.org"
        tk.Label(py_frame, text=py_text, bg=BG, fg=py_color, font=("Segoe UI", 10)).pack(side="left")

        # -- Output Folder --
        ttk.Label(main, text="Output folder:", style="Dark.TLabel").pack(anchor="w")
        folder_frame = ttk.Frame(main, style="Dark.TFrame")
        folder_frame.pack(fill="x", pady=(2, 6))

        docs_folder = Path(os.path.expanduser("~/Documents"))
        default_out = str(docs_folder / "whispersync-meetings")

        self.output_var = tk.StringVar(value=default_out)
        self.output_entry = tk.Entry(folder_frame, textvariable=self.output_var,
                                     bg=ENTRY_BG, fg=ENTRY_FG, insertbackground=FG,
                                     font=("Segoe UI", 9), relief="flat", bd=4)
        self.output_entry.pack(side="left", fill="x", expand=True)

        browse_btn = tk.Button(folder_frame, text="Browse", bg=BG_LIGHT, fg=FG,
                               font=("Segoe UI", 9), relief="flat", bd=2,
                               activebackground=ACCENT, activeforeground=BTN_FG,
                               command=self._browse_folder)
        browse_btn.pack(side="left", padx=(6, 0))

        # -- HuggingFace Token --
        hf_label_frame = ttk.Frame(main, style="Dark.TFrame")
        hf_label_frame.pack(fill="x", anchor="w", pady=(4, 0))
        ttk.Label(hf_label_frame, text="HuggingFace token", style="Dark.TLabel").pack(side="left")
        tk.Label(hf_label_frame, text="  (optional - for speaker ID in meetings)",
                 bg=BG, fg=FG_DIM, font=("Segoe UI", 9)).pack(side="left")

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

        self.hf_entry = tk.Entry(main, textvariable=self.hf_var, show="*",
                                 bg=ENTRY_BG, fg=ENTRY_FG, insertbackground=FG,
                                 font=("Segoe UI", 9), relief="flat", bd=4)
        self.hf_entry.pack(fill="x", pady=(2, 6))

        # -- Options --
        opts_frame = ttk.Frame(main, style="Dark.TFrame")
        opts_frame.pack(fill="x", pady=(0, 6))

        self.desktop_var = tk.BooleanVar(value=True)
        self.startup_var = tk.BooleanVar(value=False)

        ttk.Checkbutton(opts_frame, text="Create Desktop shortcut",
                         variable=self.desktop_var, style="Dark.TCheckbutton").pack(anchor="w")
        ttk.Checkbutton(opts_frame, text="Auto-launch on login",
                         variable=self.startup_var, style="Dark.TCheckbutton").pack(anchor="w")

        # -- Log Area --
        log_frame = tk.Frame(main, bg=BG_LIGHT)
        log_frame.pack(fill="both", expand=True, pady=(2, 6))
        self.log_text = tk.Text(log_frame, height=12, bg=BG_LIGHT, fg=FG,
                                font=("Consolas", 9), relief="flat", bd=6,
                                wrap="word", state="disabled", insertbackground=FG)
        log_scroll = tk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)

        # Tag configs for colored output
        self.log_text.tag_configure("ok", foreground=GREEN)
        self.log_text.tag_configure("warn", foreground=YELLOW)
        self.log_text.tag_configure("error", foreground=RED)
        self.log_text.tag_configure("step", foreground=ACCENT)
        self.log_text.tag_configure("info", foreground=FG_DIM)
        self.log_text.tag_configure("magenta", foreground=MAGENTA)
        self.log_text.tag_configure("highlight", foreground=YELLOW)
        self.log_text.tag_configure("green_bold", foreground=GREEN, font=("Consolas", 9, "bold"))
        self.log_text.tag_configure("accent_bold", foreground=ACCENT, font=("Consolas", 9, "bold"))

        # -- Progress Bar --
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(main, variable=self.progress_var,
                                             maximum=100,
                                             style="Dark.Horizontal.TProgressbar")
        self.progress_bar.pack(fill="x", pady=(0, 8))

        # -- Buttons Frame --
        btn_frame = ttk.Frame(main, style="Dark.TFrame")
        btn_frame.pack(pady=(0, 6))

        self.action_btn = tk.Button(btn_frame, text="Install", bg=BTN_BG, fg=BTN_FG,
                                    font=("Segoe UI", 12, "bold"), relief="flat",
                                    bd=0, padx=20, pady=6,
                                    activebackground=GREEN, activeforeground=BTN_FG,
                                    command=self._on_action)
        self.action_btn.pack(side="left", padx=(0, 8))

        # Hidden until install completes
        self.bench_btn = tk.Button(btn_frame, text="Run Benchmark", bg=BG_LIGHT, fg=FG,
                                   font=("Segoe UI", 10), relief="flat",
                                   bd=0, padx=14, pady=6,
                                   activebackground=ACCENT, activeforeground=BTN_FG,
                                   command=self._on_benchmark)

        self.details_btn = tk.Button(btn_frame, text="How It Works", bg=BG_LIGHT, fg=FG,
                                     font=("Segoe UI", 10), relief="flat",
                                     bd=0, padx=14, pady=6,
                                     activebackground=ACCENT, activeforeground=BTN_FG,
                                     command=self._on_details)

        if not self.python_cmd:
            self.action_btn.configure(state="disabled")

        # -- Status Bar --
        self.status_var = tk.StringVar(value="Ready")
        status_bar = tk.Label(self.root, textvariable=self.status_var,
                              bg=BG_LIGHT, fg=FG_DIM, font=("Segoe UI", 9),
                              anchor="w", padx=10, pady=3)
        status_bar.pack(fill="x", side="bottom")

    def _browse_folder(self):
        folder = filedialog.askdirectory(title="Choose output folder",
                                          initialdir=self.output_var.get())
        if folder:
            self.output_var.set(folder)

    def _log(self, text, tag=None):
        """Append text to the log area (thread-safe via after)."""
        def _do():
            self.log_text.configure(state="normal")
            if tag:
                self.log_text.insert("end", text + "\n", tag)
            else:
                self.log_text.insert("end", text + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.root.after(0, _do)

    def _log_multi(self, lines):
        """Append multiple (text, tag) tuples as lines. Thread-safe."""
        def _do():
            self.log_text.configure(state="normal")
            for text, tag in lines:
                if tag:
                    self.log_text.insert("end", text + "\n", tag)
                else:
                    self.log_text.insert("end", text + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.root.after(0, _do)

    def _clear_log(self):
        """Clear the entire log area. Thread-safe."""
        def _do():
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.configure(state="disabled")
        self.root.after(0, _do)

    def _set_status(self, text):
        self.root.after(0, lambda: self.status_var.set(text))

    def _set_progress(self, value):
        self.root.after(0, lambda: self.progress_var.set(value))

    def _start_spinner(self, step_num, label):
        """Start an animated spinner on a log line. Call from worker thread."""
        self._spinner_tick = 0
        self._spinner_label = label
        self._spinner_step = step_num

        def _insert():
            self.log_text.configure(state="normal")
            line_text = f"[{step_num}/{self.TOTAL_STEPS}] {label} {SPINNER_CHARS[0]}"
            self.log_text.insert("end", line_text + "\n", "step")
            # Track the line index (1-based, last line before the trailing newline)
            self._spinner_line_index = int(self.log_text.index("end-2l").split(".")[0])
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

        self.root.after(0, _insert)
        # Small delay to let the insert happen before starting animation
        self.root.after(100, self._animate_spinner)

    def _animate_spinner(self):
        """Update the spinner character on the current spinner line."""
        if self._spinner_line_index is None:
            return
        self._spinner_tick += 1
        char = SPINNER_CHARS[self._spinner_tick % len(SPINNER_CHARS)]
        line_idx = self._spinner_line_index
        prefix = f"[{self._spinner_step}/{self.TOTAL_STEPS}] {self._spinner_label} "

        def _update():
            self.log_text.configure(state="normal")
            self.log_text.delete(f"{line_idx}.0", f"{line_idx}.end")
            self.log_text.insert(f"{line_idx}.0", f"{prefix}{char}", "step")
            self.log_text.configure(state="disabled")

        self.root.after(0, _update)
        self._spinner_after_id = self.root.after(300, self._animate_spinner)

    def _stop_spinner(self, ok_text=None, warn_text=None):
        """Stop the spinner and replace the line with a result. Call from worker thread."""
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

    def _log_step(self, num, text):
        """Log a step header like the PS installer's Step function."""
        self._log(f"[{num}/{self.TOTAL_STEPS}] {text}", "step")

    def _log_ok(self, text):
        """Log an [OK] result like the PS installer."""
        self._log(f"      [OK] {text}", "ok")

    def _log_warn(self, text):
        """Log a [!] warning like the PS installer."""
        self._log(f"      [!]  {text}", "warn")

    def _log_info(self, text):
        """Log an info line like the PS installer."""
        self._log(f"      {text}", "info")

    def _on_close(self):
        if self.installing:
            import tkinter.messagebox as mb
            if not mb.askokcancel("Installation Running",
                                  "Installation is in progress. Closing may leave a partial install.\n\nClose anyway?"):
                return
        self.root.destroy()

    def _on_action(self):
        if self.install_complete:
            self._launch()
            return
        if self.installing:
            return
        # Validate output directory
        output_dir = self.output_var.get().strip()
        if not output_dir:
            self._log("Please enter an output folder.", "error")
            return
        if not os.path.isabs(output_dir):
            self._log("Please enter a full path (e.g. C:\\...).", "error")
            return
        self.installing = True
        self.action_btn.configure(state="disabled", text="Installing...")
        self.output_entry.configure(state="disabled")
        self.hf_entry.configure(state="disabled")
        thread = threading.Thread(target=self._run_install, daemon=True)
        thread.start()

    def _run_silent(self, args, label="Running", env=None):
        """Run a subprocess silently (no output streaming). Returns (success, elapsed_seconds, stderr)."""
        import time
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        merged_env["GIT_TERMINAL_PROMPT"] = "0"
        merged_env["GIT_ASKPASS"] = ""
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

    def _run_install(self):
        """Execute all installation steps in a background thread."""
        import time

        python = self.python_cmd
        venv_python = str(self.venv_path / "Scripts" / "python.exe")
        venv_pip = str(self.venv_path / "Scripts" / "pip.exe")
        requirements = str(self.script_root / "requirements.txt")
        output_dir = self.output_var.get().strip()
        current_step = 0

        def advance(label):
            nonlocal current_step
            current_step += 1
            pct = (current_step - 1) / self.TOTAL_STEPS * 100
            self._set_progress(pct)
            self._set_status(f"Step {current_step}/{self.TOTAL_STEPS}: {label}")
            return current_step

        try:
            # ---- Step 1: Check Python ----
            n = advance("Checking Python")
            self._log_step(n, "Checking Python...")
            if self.python_cmd:
                self._log_ok(self.python_version)
            else:
                self._log_warn("Python 3.10+ not found!")
                self._log_info("Install from https://python.org/downloads/")
                self._fail("Python not found")
                return

            # ---- Step 2: Detect GPU ----
            n = advance("Detecting GPU")
            self._log_step(n, "Detecting GPU...")
            if self.gpu_name:
                self._log_ok(self.gpu_name)
                self._log_info(f"Selected: {self.cuda_label}")
            else:
                self._log_warn("nvidia-smi not found - no GPU detected")
                self._log_info("WhisperSync will run in CPU mode (slower).")

            # ---- Step 3: Create venv ----
            n = advance("Creating virtual environment")
            self._log_step(n, "Setting up virtual environment...")
            if self.venv_path.exists():
                self._log_warn("Venv already exists, reusing it")
            else:
                self._start_spinner(n, "Creating virtual environment...")
                ok, elapsed, err = self._run_silent(
                    [python, "-m", "venv", str(self.venv_path)],
                    label="Create venv",
                )
                if not ok:
                    self._stop_spinner(warn_text=f"Failed to create venv: {err}")
                    self._fail("Failed to create virtual environment")
                    return
                self._stop_spinner(ok_text=f"Virtual environment created ({int(elapsed)}s)")
                time.sleep(0.1)  # Let UI update

            # ---- Step 4: Install dependencies ----
            n = advance("Installing dependencies")
            self._log_step(n, "Installing dependencies...")

            # Upgrade pip
            self._start_spinner(n, "Upgrading pip...")
            ok, elapsed, err = self._run_silent(
                [venv_python, "-m", "pip", "install", "--upgrade", "pip", "-qq"],
                label="Upgrade pip",
            )
            if not ok:
                self._stop_spinner(warn_text=f"pip upgrade failed: {err}")
                self._fail("Failed to upgrade pip")
                return
            self._stop_spinner(ok_text=f"Upgrading pip ({int(elapsed)}s)")
            time.sleep(0.1)

            # Install requirements
            self._start_spinner(n, "Installing dependencies...")
            ok, elapsed, err = self._run_silent(
                [venv_pip, "install", "-r", requirements, "-qq"],
                label="Install requirements",
            )
            if not ok:
                self._stop_spinner(warn_text=f"Dependencies failed: {err}")
                self._fail("Failed to install dependencies")
                return
            self._stop_spinner(ok_text=f"Dependencies installed ({int(elapsed)}s)")
            time.sleep(0.1)

            # ---- Step 5: Install CUDA PyTorch ----
            n = advance("Installing PyTorch")
            if self.cuda_version:
                self._log_step(n, f"Installing PyTorch ({self.cuda_version})...")
                torch_url = f"https://download.pytorch.org/whl/{self.cuda_version}"
                self._start_spinner(n, "Installing PyTorch...")
                ok, elapsed, err = self._run_silent(
                    [venv_pip, "install", "torch", "torchaudio",
                     "--index-url", torch_url, "--force-reinstall", "--no-deps", "-qq"],
                    label="Install PyTorch (GPU)",
                )
                if not ok:
                    self._stop_spinner(warn_text="GPU PyTorch failed, falling back to CPU")
                else:
                    self._stop_spinner(ok_text=f"PyTorch GPU installed ({int(elapsed)}s)")
                time.sleep(0.1)
            else:
                self._log_step(n, "Skipping GPU PyTorch (no GPU detected)")

            # ---- Step 6: Verify installation ----
            n = advance("Verifying installation")
            self._log_step(n, "Verifying installation...")

            # Standalone marker
            marker = self.pkg_dir / ".standalone"
            if not marker.exists():
                marker.write_text("")

            # Check CUDA
            try:
                verify_result = subprocess.run(
                    [venv_python, "-c",
                     "import torch; a = torch.cuda.is_available(); "
                     "n = torch.cuda.get_device_name(0) if a else 'N/A'; "
                     "print(f'CUDA: {a}  Device: {n}')"],
                    capture_output=True, text=True, timeout=30,
                    cwd=str(self.script_root),
                )
                if verify_result.returncode == 0:
                    cuda_line = verify_result.stdout.strip()
                    if "True" in cuda_line:
                        self._log_ok(cuda_line)
                    else:
                        self._log_warn(cuda_line)
            except Exception:
                self._log_warn("Could not verify CUDA")

            # Check whisperx
            try:
                wx_result = subprocess.run(
                    [venv_python, "-c", "import whisperx; print('OK')"],
                    capture_output=True, text=True, timeout=15,
                    cwd=str(self.script_root),
                )
                if wx_result.returncode == 0 and "OK" in wx_result.stdout:
                    self._log_ok("whisperX ready")
                else:
                    self._log_warn("whisperX import issue (may still work)")
            except Exception:
                self._log_warn("whisperX check timed out")

            # Check sounddevice
            try:
                sd_result = subprocess.run(
                    [venv_python, "-c", "import sounddevice; print('OK')"],
                    capture_output=True, text=True, timeout=15,
                    cwd=str(self.script_root),
                )
                if sd_result.returncode == 0 and "OK" in sd_result.stdout:
                    self._log_ok("Audio capture ready")
                else:
                    self._log_warn("sounddevice import issue")
            except Exception:
                self._log_warn("sounddevice check timed out")

            # ---- Step 7: Download models ----
            n = advance("Downloading models")
            self._log_step(n, "Downloading models...")

            fd, bootstrap_path = tempfile.mkstemp(suffix=".py", prefix="ws-bootstrap-")
            os.close(fd)
            bootstrap_script = Path(bootstrap_path)
            bootstrap_code = (
                "import warnings, os, sys\n"
                "warnings.filterwarnings('ignore')\n"
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
                label="Download models",
            )
            bootstrap_script.unlink(missing_ok=True)
            if not ok:
                self._stop_spinner(warn_text="Model download had issues - you can download later via tray menu")
            else:
                self._stop_spinner(ok_text=f"Models cached ({int(elapsed)}s)")
            time.sleep(0.1)

            # ---- Step 8: Write config + HF token ----
            n = advance("Writing configuration")
            self._log_step(n, "Saving configuration...")

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

            # Preserve existing config if present
            existing_cfg = {}
            if config_path.exists():
                try:
                    existing_cfg = json.loads(config_path.read_text(encoding="utf-8"))
                    self._log_ok("Found existing config, preserving settings")
                except (json.JSONDecodeError, OSError):
                    self._log_warn("Existing config was corrupt, starting fresh")

            existing_cfg["output_dir"] = output_dir.replace("\\", "/")
            config_path.write_text(json.dumps(existing_cfg, indent=2), encoding="utf-8")

            # Bootstrap pointer (legacy location)
            legacy_cfg = self.pkg_dir / "config.json"
            bootstrap_json = json.dumps({"output_dir": output_dir.replace("\\", "/")}, indent=2)
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
                    self._log_warn("No HF token - meeting mode won't identify speakers")
                    self._log_info("You can add it later via the tray menu")

            # ---- Step 9: Create shortcuts ----
            n = advance("Creating shortcuts")
            self._log_step(n, "Creating shortcuts...")
            launcher = self.script_root / "start.ps1"
            icon_path = self.pkg_dir / "whisper-capture.ico"

            if self.desktop_var.get():
                desktop = Path(os.path.join(os.environ.get("USERPROFILE", ""), "Desktop"))
                try:
                    self._create_shortcut(desktop / "WhisperSync.lnk", launcher, icon_path)
                    self._log_ok("Desktop shortcut created")
                except Exception as e:
                    self._log_warn(f"Desktop shortcut failed: {e}")

            if self.startup_var.get():
                startup_dir = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
                try:
                    self._create_shortcut(startup_dir / "WhisperSync.lnk", launcher, icon_path)
                    self._log_ok("Startup shortcut created")
                except Exception as e:
                    self._log_warn(f"Startup shortcut failed: {e}")

            if not self.desktop_var.get() and not self.startup_var.get():
                self._log_info("No shortcuts requested, skipping")

            # ---- Completion ----
            self._set_progress(100)
            self._set_status("Installation complete!")
            self.installing = False
            self.install_complete = True

            # Clear log and show completion screen
            time.sleep(0.3)
            self._clear_log()
            time.sleep(0.1)
            self._show_completion_screen(output_dir)

            # Update buttons
            def _update_buttons():
                self.action_btn.configure(
                    state="normal", text="Launch WhisperSync",
                    bg=GREEN, activebackground=ACCENT,
                )
                self.bench_btn.pack(side="left", padx=(0, 8))
                self.details_btn.pack(side="left")
            self.root.after(0, _update_buttons)

        except Exception as e:
            self._fail(f"Unexpected error: {e}")

    def _show_completion_screen(self, output_dir):
        """Show the GET STARTED completion content, matching install.ps1."""
        lines = [
            ("  =============================================", "step"),
            ("       WhisperSync installed successfully!", "ok"),
            ("  =============================================", "step"),
            ("", None),
            ("  LAUNCH", "highlight"),
            ("      Double-click WhisperSync on your Desktop", "info"),
            ("      Or: powershell -File start.ps1", "warn"),
            ("", None),
            (f"  Recordings: {output_dir}", "step"),
            ("", None),
            ("  100% LOCAL              CLOUD (optional)", None),
            ("    Dictation              Meeting minutes", "info"),
            ("    Transcription          (via Claude CLI)", "info"),
            ("    Speaker ID", "info"),
            ("    Audio capture", "info"),
            ("", None),
            ("  Audio never leaves your machine.", "ok"),
            ("", None),
            ("  =============================================", "step"),
            ("  GET STARTED  Record your first dictation and meeting!", None),
            ("  =============================================", "step"),
            ("", None),
            ("  Try Dictation:", "magenta"),
            ("", None),
            ("    1. Click any text box (Notepad, browser, chat)", None),
            ("    2. Press Ctrl+Shift+Space and say your favorite quote", None),
            ("    3. Press Ctrl+Shift+Space again", None),
            ("    4. Voila! Your words appear right where you clicked.", "ok"),
            ("", None),
            ("  Try a Meeting:", "magenta"),
            ("", None),
            ("    1. Find the gray circle in your system tray (bottom-right)", None),
            ("    2. Left-click it (it turns red - you're recording!)", None),
            ("    3. Talk, play a video, join a call - it hears everything", None),
            ("    4. Left-click again and follow the save dialog", None),
            ("    5. Done! Transcript with speaker names in your folder.", "ok"),
            ("", None),
            ("       * With Cloud AI (Claude CLI), minutes are automatically", "info"),
            ("         generated with action items and summaries *", "info"),
            ("", None),
        ]
        self._log_multi(lines)

    def _on_benchmark(self):
        """Run benchmark in background thread."""
        if self.installing:
            return
        self.installing = True
        self.bench_btn.configure(state="disabled")
        self.action_btn.configure(state="disabled")
        thread = threading.Thread(target=self._run_benchmark, daemon=True)
        thread.start()

    def _run_benchmark(self):
        """Execute the benchmark script, matching install.ps1."""
        import time

        venv_python = str(self.venv_path / "Scripts" / "python.exe")

        self._log("")
        self._log("  =============================================", "step")
        self._log("  BENCHMARK  Let's compare models on your GPU!", None)
        self._log("  =============================================", "step")
        self._log("")
        self._log("  Each model turns your speech into text at different", "info")
        self._log("  speeds. Faster models give quicker results, larger", "info")
        self._log("  models are more accurate. Let's see how they perform", "info")
        self._log("  on your hardware.", "info")
        self._log("")

        fd, bench_path = tempfile.mkstemp(suffix=".py", prefix="ws-bench-")
        os.close(fd)
        bench_script = Path(bench_path)
        bench_code = (
            "import warnings, os, sys, time, numpy as np\n"
            "warnings.filterwarnings('ignore')\n"
            "os.environ['HF_HUB_DISABLE_SYMLINKS_WARNING'] = '1'\n"
            f"sys.path.insert(0, {repr(str(self.script_root))})\n"
            "from whisper_sync.transcribe import _load_whisper_model, _get_device\n"
            "from whisper_sync import config\n"
            "cfg = config.load()\n"
            "device = _get_device()\n"
            "audio = np.zeros(16000 * 5, dtype=np.float32)\n"
            "models_to_test = []\n"
            "from whisper_sync.model_status import is_model_cached\n"
            "for m in ['tiny', 'base', 'large-v3']:\n"
            "    if is_model_cached(m):\n"
            "        models_to_test.append(m)\n"
            "results = []\n"
            "for name in models_to_test:\n"
            "    compute = 'int8' if device == 'cpu' else cfg.get('compute_type', 'float16')\n"
            "    model = _load_whisper_model(name, compute, 'en')\n"
            "    model.transcribe(audio, batch_size=4, language='en')\n"
            "    t0 = time.perf_counter()\n"
            "    for _ in range(3):\n"
            "        model.transcribe(audio, batch_size=4, language='en')\n"
            "    avg = (time.perf_counter() - t0) / 3\n"
            "    results.append((name, avg))\n"
            "quality = {'tiny': 'Basic', 'base': 'Good', 'large-v3': 'Best'}\n"
            "fast_models = [n for n, t in results if t < 0.3]\n"
            "all_fast = all(t < 0.5 for _, t in results)\n"
            "largest_cached = results[-1][0] if results else None\n"
            "print()\n"
            "print(f'{\"Model\":<13}{\"Time\":<9}{\"Quality\":<12}{\"Recommendation\"}')\n"
            "print(f'{\"-------\":<13}{\"------\":<9}{\"---------\":<12}{\"---------------\"}')\n"
            "for name, avg in results:\n"
            "    rec = ''\n"
            "    if all_fast and name == 'large-v3':\n"
            "        rec = '<< your GPU is fast enough for large-v3 everywhere'\n"
            "    else:\n"
            "        parts = []\n"
            "        if name in fast_models:\n"
            "            parts.append('<< recommended for dictation')\n"
            "        if name == 'large-v3' or (name == largest_cached and 'large-v3' not in [n for n, _ in results]):\n"
            "            parts.append('<< recommended for meetings')\n"
            "        rec = '  '.join(parts)\n"
            "    print(f'{name:<13}{avg:.2f}s    {quality.get(name, \"\"):<12}{rec}')\n"
            "print()\n"
        )
        bench_script.write_text(bench_code, encoding="utf-8")

        self._start_spinner(0, "Running benchmark...")
        # Override step display for benchmark
        self._spinner_label = "Running benchmark..."

        try:
            merged_env = os.environ.copy()
            proc = subprocess.run(
                [venv_python, str(bench_script)],
                capture_output=True, text=True,
                cwd=str(self.script_root), env=merged_env,
            )
            self._stop_spinner(ok_text="Benchmark complete")
            time.sleep(0.1)

            if proc.returncode == 0:
                # Show results, filtering warnings
                for line in proc.stdout.splitlines():
                    if line.strip() and not any(w in line.lower() for w in ["warning", "torchcodec", "lightning", "xet storage"]):
                        self._log(f"    {line}")
                self._log("")
                self._log("  See the recommendations above - accuracy matters for", "info")
                self._log("  meetings, speed matters for dictation.", "info")
            else:
                self._log_warn(f"Benchmark failed: {proc.stderr[:200]}")
        except Exception as e:
            self._stop_spinner(warn_text=f"Benchmark error: {e}")
        finally:
            bench_script.unlink(missing_ok=True)
            self.installing = False
            self.root.after(0, lambda: self.bench_btn.configure(state="normal"))
            self.root.after(0, lambda: self.action_btn.configure(state="normal"))

    def _on_details(self):
        """Show HOW IT WORKS section in the log."""
        lines = [
            ("", None),
            ("  =============================================", "step"),
            ("  HOW IT WORKS", "accent_bold"),
            ("  =============================================", "step"),
            ("", None),
            ("  DICTATION  voice-to-text anywhere", "magenta"),
            ("", None),
            ("      Start recording       Ctrl+Shift+Space", None),
            ("      Stop & paste          Ctrl+Shift+Space (same key)", None),
            ("      Cancel                Left-click the tray icon", None),
            ("", None),
            ("      Text goes into the focused text field (editor,", "info"),
            ("      browser, chat, etc). If nothing is focused, it's", "info"),
            ("      copied to your clipboard instead.", "info"),
            ("", None),
            ("  - - - - - - - - - - - - - - - - - - - - - -", "info"),
            ("", None),
            ("  MEETING    record, transcribe, identify speakers", "magenta"),
            ("", None),
            ("      Start recording       Ctrl+Shift+M", None),
            ("          or                Left-click the tray icon", None),
            ("      Stop & save           Ctrl+Shift+M (same key)", None),
            ("", None),
            ("      Records your mic + system audio (what you hear).", "info"),
            ("      Works with any meeting on your computer:", "info"),
            ("", None),
            ("        Zoom, Google Meet, Teams, phone calls", None),
            ("        In-person (picks up your mic)", None),
            ("        Any audio playing through your speakers", None),
            ("", None),
            ("      After you stop, you name the meeting and get a full", "info"),
            ("      transcript with speaker labels. WhisperSync learns", "info"),
            ("      speakers over time - the more you use it, the better", "info"),
            ("      it gets at recognizing who's talking.", "info"),
            ("", None),
            ("  - - - - - - - - - - - - - - - - - - - - - -", "info"),
            ("", None),
            ("  TRAY ICON", "magenta"),
            ("", None),
            ("      Gray  Ready           Red  Recording (live!)", None),
            ("      Amber Transcribing    Green Done", None),
            ("", None),
            ("      Left-click  = start/cancel meeting", None),
            ("      Right-click = settings, model downloads, hotkeys", None),
            ("", None),
            ("  =============================================", "step"),
        ]
        self._log_multi(lines)

    def _fail(self, message):
        """Handle installation failure. Re-enables all inputs for retry."""
        self._log(f"\n{message}", "error")
        self._set_status("Installation failed")
        self.installing = False

        def _reenable():
            self.action_btn.configure(state="normal", text="Retry Install",
                                      bg=RED, activebackground=YELLOW)
            self.output_entry.configure(state="normal")
            self.hf_entry.configure(state="normal")
        self.root.after(0, _reenable)

    def _create_shortcut(self, lnk_path, launcher_path, icon_path):
        """Create a Windows shortcut using VBScript."""
        if lnk_path.exists():
            lnk_path.unlink()

        fd, vbs_path = tempfile.mkstemp(suffix=".vbs", prefix="ws-shortcut-")
        os.close(fd)
        vbs_path = Path(vbs_path)
        vbs_content = (
            'Set WshShell = WScript.CreateObject("WScript.Shell")\n'
            f'Set lnk = WshShell.CreateShortcut("{lnk_path}")\n'
            'lnk.TargetPath = "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"\n'
            f'lnk.Arguments = "-ExecutionPolicy Bypass -WindowStyle Minimized -File ""{launcher_path}"" -Watchdog"\n'
            f'lnk.WorkingDirectory = "{self.script_root}"\n'
            'lnk.WindowStyle = 7\n'
            'lnk.Description = "WhisperSync - local speech-to-text"\n'
            f'lnk.IconLocation = "{icon_path}, 0"\n'
            'lnk.Save\n'
        )
        vbs_path.write_text(vbs_content, encoding="ascii")
        try:
            result = subprocess.run(["cscript", "//nologo", str(vbs_path)],
                                    capture_output=True, timeout=10)
            if result.returncode != 0:
                raise RuntimeError(f"cscript failed: {result.stderr.strip()}")
        finally:
            vbs_path.unlink(missing_ok=True)

    def _launch(self):
        """Launch WhisperSync after installation."""
        self._set_status("Launching WhisperSync...")
        launcher = self.script_root / "start.ps1"
        subprocess.Popen(
            ["powershell", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Minimized",
             "-File", str(launcher), "-Watchdog"],
            cwd=str(self.script_root),
        )
        self.root.after(1500, self.root.destroy)


def main():
    root = tk.Tk()
    # Set dark title bar on Windows
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
