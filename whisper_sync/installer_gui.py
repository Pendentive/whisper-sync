"""WhisperSync GUI Installer - tkinter-based visual installer for Windows."""

import json
import os
import subprocess
import sys
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
ENTRY_BG = "#0f3460"
ENTRY_FG = "#e0e0e0"
BTN_BG = "#4fc3f7"
BTN_FG = "#1a1a2e"


def detect_gpu():
    """Detect NVIDIA GPU via nvidia-smi. Returns (gpu_name, cuda_version) or (None, None)."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            name = result.stdout.strip().split("\n")[0].strip()
            # Pick CUDA version based on GPU family
            import re
            if re.search(r"RTX\s*50[0-9]{2}|RTX\s*5[0-9]{3}|Blackwell", name):
                return name, "cu128"
            elif re.search(r"RTX\s*[2-4]0[0-9]{2}|RTX\s*[2-4][0-9]{3}|A[0-9]{3,4}|L[0-9]{2}", name):
                return name, "cu124"
            elif re.search(r"GTX\s*1[0-9]{3}|GTX\s*9[0-9]{2}", name):
                return name, "cu118"
            else:
                return name, "cu124"
    except Exception:
        pass
    return None, None


def find_python():
    """Find a suitable Python 3.10+ executable."""
    for cmd in ["python", "python3", "py"]:
        try:
            result = subprocess.run(
                [cmd, "--version"], capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                import re
                m = re.search(r"Python (\d+)\.(\d+)", result.stdout)
                if m and int(m.group(1)) >= 3 and int(m.group(2)) >= 10:
                    return cmd
        except Exception:
            continue
    return None


class InstallerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("WhisperSync Installer")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)

        # Center window
        w, h = 520, 640
        sx = (self.root.winfo_screenwidth() - w) // 2
        sy = (self.root.winfo_screenheight() - h) // 2
        self.root.geometry(f"{w}x{h}+{sx}+{sy}")

        self.script_root = Path(__file__).parent.parent
        self.pkg_dir = self.script_root / "whisper_sync"
        self.venv_path = self.script_root / "whisper-env"
        self.installing = False
        self.install_complete = False

        # Detect GPU before building UI
        self.gpu_name, self.cuda_version = detect_gpu()
        self.python_cmd = find_python()

        self._build_ui()

    def _build_ui(self):
        # Configure ttk styles
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
        gpu_frame.pack(fill="x", pady=(0, 8))
        ttk.Label(gpu_frame, text="GPU:", style="Dark.TLabel").pack(side="left")
        if self.gpu_name:
            gpu_text = f"  {self.gpu_name} (CUDA {self.cuda_version.replace('cu', '')})"
            gpu_color = GREEN
        else:
            gpu_text = "  No GPU detected - CPU mode (slower)"
            gpu_color = YELLOW
        gpu_label = tk.Label(gpu_frame, text=gpu_text, bg=BG, fg=gpu_color,
                             font=("Segoe UI", 10))
        gpu_label.pack(side="left")

        # -- Python Detection --
        py_frame = ttk.Frame(main, style="Dark.TFrame")
        py_frame.pack(fill="x", pady=(0, 12))
        ttk.Label(py_frame, text="Python:", style="Dark.TLabel").pack(side="left")
        if self.python_cmd:
            py_color = GREEN
            py_text = f"  {self.python_cmd} found"
        else:
            py_color = RED
            py_text = "  Not found - install Python 3.10+ from python.org"
        tk.Label(py_frame, text=py_text, bg=BG, fg=py_color, font=("Segoe UI", 10)).pack(side="left")

        # -- Output Folder --
        ttk.Label(main, text="Output folder:", style="Dark.TLabel").pack(anchor="w")
        folder_frame = ttk.Frame(main, style="Dark.TFrame")
        folder_frame.pack(fill="x", pady=(2, 8))

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

        # -- Options --
        opts_frame = ttk.Frame(main, style="Dark.TFrame")
        opts_frame.pack(fill="x", pady=(0, 4))

        self.desktop_var = tk.BooleanVar(value=True)
        self.startup_var = tk.BooleanVar(value=False)

        ttk.Checkbutton(opts_frame, text="Create Desktop shortcut",
                         variable=self.desktop_var, style="Dark.TCheckbutton").pack(anchor="w")
        ttk.Checkbutton(opts_frame, text="Auto-launch on login",
                         variable=self.startup_var, style="Dark.TCheckbutton").pack(anchor="w")

        # -- HuggingFace Token --
        ttk.Label(main, text="HuggingFace token (optional, for speaker ID in meetings):",
                  style="Dark.TLabel").pack(anchor="w", pady=(6, 0))
        self.hf_var = tk.StringVar()
        self.hf_entry = tk.Entry(main, textvariable=self.hf_var, show="*",
                                 bg=ENTRY_BG, fg=ENTRY_FG, insertbackground=FG,
                                 font=("Segoe UI", 9), relief="flat", bd=4)
        self.hf_entry.pack(fill="x", pady=(2, 8))

        # -- Progress Area --
        ttk.Label(main, text="Progress:", style="Dark.TLabel").pack(anchor="w")

        self.log_text = tk.Text(main, height=10, bg=BG_LIGHT, fg=FG,
                                font=("Consolas", 9), relief="flat", bd=4,
                                wrap="word", state="disabled", insertbackground=FG)
        self.log_text.pack(fill="both", expand=True, pady=(2, 6))

        # Tag configs for colored log output
        self.log_text.tag_configure("ok", foreground=GREEN)
        self.log_text.tag_configure("warn", foreground=YELLOW)
        self.log_text.tag_configure("error", foreground=RED)
        self.log_text.tag_configure("step", foreground=ACCENT)

        # -- Progress Bar --
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(main, variable=self.progress_var,
                                             maximum=100,
                                             style="Dark.Horizontal.TProgressbar")
        self.progress_bar.pack(fill="x", pady=(0, 8))

        # -- Install / Launch Button --
        self.action_btn = tk.Button(main, text="Install", bg=BTN_BG, fg=BTN_FG,
                                    font=("Segoe UI", 12, "bold"), relief="flat",
                                    bd=0, padx=20, pady=6,
                                    activebackground=GREEN, activeforeground=BTN_FG,
                                    command=self._on_action)
        self.action_btn.pack(pady=(0, 6))

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

    def _set_status(self, text):
        self.root.after(0, lambda: self.status_var.set(text))

    def _set_progress(self, value):
        self.root.after(0, lambda: self.progress_var.set(value))

    def _on_action(self):
        if self.install_complete:
            self._launch()
            return
        if self.installing:
            return
        self.installing = True
        self.action_btn.configure(state="disabled", text="Installing...")
        self.output_entry.configure(state="disabled")
        self.hf_entry.configure(state="disabled")
        thread = threading.Thread(target=self._run_install, daemon=True)
        thread.start()

    def _run_command(self, args, label="Running command", env=None):
        """Run a subprocess and stream output to the log. Returns True on success."""
        self._log(f"  > {' '.join(str(a) for a in args)}", "step")
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        # Suppress git credential popups during pip
        merged_env["GIT_TERMINAL_PROMPT"] = "0"
        merged_env["GIT_ASKPASS"] = ""
        try:
            proc = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, env=merged_env, cwd=str(self.script_root),
            )
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    self._log(f"  {line}")
            proc.wait()
            if proc.returncode != 0:
                self._log(f"  {label} failed (exit code {proc.returncode})", "error")
                return False
            return True
        except Exception as e:
            self._log(f"  Error: {e}", "error")
            return False

    def _run_install(self):
        """Execute all installation steps in a background thread."""
        total_steps = 7
        current_step = 0

        def step(label):
            nonlocal current_step
            current_step += 1
            pct = (current_step - 1) / total_steps * 100
            self._set_progress(pct)
            self._set_status(f"Step {current_step}/{total_steps}: {label}")
            self._log(f"\n[{current_step}/{total_steps}] {label}", "step")

        python = self.python_cmd
        venv_python = str(self.venv_path / "Scripts" / "python.exe")
        venv_pip = str(self.venv_path / "Scripts" / "pip.exe")
        requirements = str(self.script_root / "requirements.txt")
        output_dir = self.output_var.get().strip()

        try:
            # -- Step 1: Create venv --
            step("Creating virtual environment")
            if self.venv_path.exists():
                self._log("  Venv already exists, reusing it", "warn")
            else:
                ok = self._run_command([python, "-m", "venv", str(self.venv_path)],
                                       label="Create venv")
                if not ok:
                    self._fail("Failed to create virtual environment")
                    return
            self._log("  Virtual environment ready", "ok")

            # -- Step 2: Install dependencies --
            step("Installing dependencies")
            ok = self._run_command([venv_python, "-m", "pip", "install", "--upgrade", "pip", "-q"],
                                   label="Upgrade pip")
            if not ok:
                self._fail("Failed to upgrade pip")
                return

            ok = self._run_command([venv_pip, "install", "-r", requirements, "-q"],
                                   label="Install requirements")
            if not ok:
                self._fail("Failed to install dependencies")
                return
            self._log("  Dependencies installed", "ok")

            # -- Step 3: Install PyTorch with CUDA --
            step("Installing PyTorch")
            if self.cuda_version:
                torch_url = f"https://download.pytorch.org/whl/{self.cuda_version}"
                ok = self._run_command(
                    [venv_pip, "install", "torch", "torchaudio",
                     "--index-url", torch_url, "--force-reinstall", "--no-deps", "-q"],
                    label="Install PyTorch (GPU)",
                )
                if not ok:
                    self._log("  GPU PyTorch failed, falling back to CPU", "warn")
                else:
                    self._log(f"  PyTorch installed with {self.cuda_version}", "ok")
            else:
                self._log("  No GPU, using CPU PyTorch (included in dependencies)", "ok")

            # -- Step 4: Download base models --
            step("Downloading models")
            # Write standalone marker
            marker = self.pkg_dir / ".standalone"
            if not marker.exists():
                marker.write_text("")

            bootstrap_script = self.script_root / "ws-bootstrap-gui.py"
            bootstrap_code = (
                "import warnings, os, sys\n"
                "warnings.filterwarnings('ignore')\n"
                "os.environ['HF_HUB_DISABLE_SYMLINKS_WARNING'] = '1'\n"
                "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n"
                "from whisper_sync.model_status import bootstrap_models\n"
                "from whisper_sync import config\n"
                "bootstrap_models(config.load())\n"
            )
            bootstrap_script.write_text(bootstrap_code, encoding="utf-8")
            ok = self._run_command([venv_python, str(bootstrap_script)],
                                   label="Download models")
            bootstrap_script.unlink(missing_ok=True)
            if not ok:
                self._log("  Model download had issues, you can download later via the tray menu", "warn")
            else:
                self._log("  Models cached", "ok")

            # -- Step 5: Write config --
            step("Writing configuration")
            output_path = Path(output_dir)
            output_path.mkdir(parents=True, exist_ok=True)
            ws_dir = output_path / ".whispersync"
            ws_dir.mkdir(parents=True, exist_ok=True)
            config_path = ws_dir / "config.json"

            # Preserve existing config if present
            existing_cfg = {}
            if config_path.exists():
                try:
                    existing_cfg = json.loads(config_path.read_text(encoding="utf-8"))
                    self._log("  Found existing config, preserving settings", "ok")
                except (json.JSONDecodeError, OSError):
                    pass

            existing_cfg["output_dir"] = output_dir.replace("\\", "/")
            config_path.write_text(json.dumps(existing_cfg, indent=2),
                                   encoding="utf-8")

            # Write bootstrap pointer (legacy location)
            legacy_cfg = self.pkg_dir / "config.json"
            bootstrap_json = json.dumps({"output_dir": output_dir.replace("\\", "/")}, indent=2)
            legacy_cfg.write_text(bootstrap_json, encoding="utf-8")

            self._log(f"  Config saved to {config_path}", "ok")

            # -- Step 6: HuggingFace token --
            hf_token = self.hf_var.get().strip()
            if hf_token:
                hf_dir = Path.home() / ".huggingface"
                hf_dir.mkdir(parents=True, exist_ok=True)
                (hf_dir / "token").write_text(hf_token, encoding="ascii")
                self._log("  HuggingFace token saved", "ok")

            # -- Step 7: Create shortcuts --
            step("Creating shortcuts")
            launcher = self.script_root / "start.ps1"
            icon_path = self.pkg_dir / "whisper-capture.ico"

            if self.desktop_var.get():
                desktop = Path(os.path.join(os.environ.get("USERPROFILE", ""), "Desktop"))
                self._create_shortcut(desktop / "WhisperSync.lnk", launcher, icon_path)
                self._log("  Desktop shortcut created", "ok")

            if self.startup_var.get():
                startup_dir = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
                self._create_shortcut(startup_dir / "WhisperSync.lnk", launcher, icon_path)
                self._log("  Startup shortcut created", "ok")

            if not self.desktop_var.get() and not self.startup_var.get():
                self._log("  No shortcuts requested, skipping", "ok")

            # -- Step 7: Verify --
            step("Verifying installation")
            # Check CUDA
            verify_result = subprocess.run(
                [venv_python, "-c",
                 "import torch; a = torch.cuda.is_available(); "
                 "n = torch.cuda.get_device_name(0) if a else 'N/A'; "
                 "print(f'CUDA: {a}, Device: {n}')"],
                capture_output=True, text=True, timeout=30,
            )
            if verify_result.returncode == 0:
                cuda_line = verify_result.stdout.strip()
                if "True" in cuda_line:
                    self._log(f"  {cuda_line}", "ok")
                else:
                    self._log(f"  {cuda_line}", "warn")

            # Check whisperx
            wx_result = subprocess.run(
                [venv_python, "-c", "import whisperx; print('OK')"],
                capture_output=True, text=True, timeout=15,
            )
            if wx_result.returncode == 0 and "OK" in wx_result.stdout:
                self._log("  whisperX ready", "ok")
            else:
                self._log("  whisperX import issue (may still work)", "warn")

            # Check sounddevice
            sd_result = subprocess.run(
                [venv_python, "-c", "import sounddevice; print('OK')"],
                capture_output=True, text=True, timeout=15,
            )
            if sd_result.returncode == 0 and "OK" in sd_result.stdout:
                self._log("  Audio capture ready", "ok")
            else:
                self._log("  sounddevice import issue", "warn")

            # -- Done --
            self._set_progress(100)
            self._set_status("Installation complete")
            self._log("\nWhisperSync installed successfully!", "ok")
            self._log(f"Recordings will save to: {output_dir}", "ok")
            self.install_complete = True
            self.root.after(0, lambda: self.action_btn.configure(
                state="normal", text="Launch WhisperSync",
                bg=GREEN, activebackground=ACCENT,
            ))

        except Exception as e:
            self._fail(f"Unexpected error: {e}")

    def _fail(self, message):
        """Handle installation failure."""
        self._log(f"\n{message}", "error")
        self._set_status("Installation failed")
        self.installing = False
        self.root.after(0, lambda: self.action_btn.configure(
            state="normal", text="Retry Install",
            bg=RED, activebackground=YELLOW,
        ))

    def _create_shortcut(self, lnk_path, launcher_path, icon_path):
        """Create a Windows shortcut using VBScript."""
        # Remove existing
        if lnk_path.exists():
            lnk_path.unlink()

        vbs_path = Path(os.environ.get("TEMP", "/tmp")) / "ws-shortcut.vbs"
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
            subprocess.run(["cscript", "//nologo", str(vbs_path)],
                           capture_output=True, timeout=10)
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
    # Set dark title bar on Windows (optional, may not work on all versions)
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
