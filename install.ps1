# WhisperSync installer - creates venv, detects GPU, installs CUDA PyTorch + dependencies
# Usage: powershell -ExecutionPolicy Bypass -File install.ps1

$ScriptRoot = $PSScriptRoot
$PkgDir = "$ScriptRoot\whisper_sync"
$VenvPath = "$ScriptRoot\whisper-env"
$RequirementsFile = "$ScriptRoot\requirements.txt"

# ── Style helpers ──

function Step($num, $text) { Write-Host "  [$num] " -NoNewline -ForegroundColor DarkCyan; Write-Host $text -ForegroundColor White }
function Ok($text) { Write-Host "      " -NoNewline; Write-Host "[OK] " -NoNewline -ForegroundColor Green; Write-Host $text -ForegroundColor Gray }
function Warn($text) { Write-Host "      " -NoNewline; Write-Host "[!]  " -NoNewline -ForegroundColor Yellow; Write-Host $text -ForegroundColor Yellow }
function Info($text) { Write-Host "      $text" -ForegroundColor DarkGray }
function Prompt($text) { return (Read-Host "      $text") }
function Section($text) {
    Write-Host ""
    Write-Host "  --- " -NoNewline -ForegroundColor DarkGray; Write-Host $text -NoNewline -ForegroundColor Cyan; Write-Host " ---" -ForegroundColor DarkGray
    Write-Host ""
}
function NoBOM($path, $content) {
    # Write file without UTF-8 BOM (Python/JSON can't parse BOM)
    [System.IO.File]::WriteAllText($path, $content, (New-Object System.Text.UTF8Encoding $false))
}
function RunWithSpinner($label, $exe, $arguments) {
    # Run a native command with an animated spinner using .NET Process.
    # Bypasses all PowerShell quirks (ErrorActionPreference, LASTEXITCODE, stderr).
    # Env vars inherited automatically. WorkingDirectory set explicitly.
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $exe
    $psi.Arguments = $arguments
    $psi.WorkingDirectory = $ScriptRoot
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    $proc = [System.Diagnostics.Process]::Start($psi)
    # Drain stdout/stderr async to prevent buffer deadlock
    $outTask = $proc.StandardOutput.ReadToEndAsync()
    $errTask = $proc.StandardError.ReadToEndAsync()
    $spinner = @('|', '/', '-', '\')
    $i = 0
    while (-not $proc.HasExited) {
        Write-Host "`r      $($spinner[$i % 4]) $label..." -NoNewline
        Start-Sleep -Milliseconds 250
        $i++
    }
    $proc.WaitForExit()
    $outTask.Wait(); $errTask.Wait()
    if ($proc.ExitCode -ne 0) {
        Write-Host "`r      " -NoNewline; Write-Host "[!] $label failed              " -ForegroundColor Red
        $errText = $errTask.Result
        if ($errText -and $errText.Trim()) { Write-Host $errText -ForegroundColor Red }
        throw "$label failed (exit code $($proc.ExitCode))"
    }
    Write-Host "`r      " -NoNewline; Write-Host "[OK] " -NoNewline -ForegroundColor Green; Write-Host "$label              " -ForegroundColor Gray
}

try {
$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "  =============================================" -ForegroundColor DarkCyan
Write-Host "           " -NoNewline; Write-Host "WhisperSync Installer" -ForegroundColor Cyan
Write-Host "       Local speech-to-text for Windows" -ForegroundColor DarkGray
Write-Host "  =============================================" -ForegroundColor DarkCyan
Write-Host ""

# ── Step 1: Check Python ──

Step 1 "Checking Python..."

$pythonCmd = $null
$ErrorActionPreference = "Continue"
foreach ($cmd in @("python", "python3", "py")) {
    $ver = & $cmd --version 2>&1
    if ($LASTEXITCODE -eq 0 -and $ver -match "Python (\d+)\.(\d+)") {
        $major = [int]$Matches[1]; $minor = [int]$Matches[2]
        if ($major -ge 3 -and $minor -ge 10) {
            $pythonCmd = $cmd; Ok "$ver"; break
        }
    }
}
$ErrorActionPreference = "Stop"

if (-not $pythonCmd) {
    Warn "Python 3.10+ not found!"
    Info "Install from https://python.org/downloads/"
    Info "Make sure to check 'Add to PATH' during installation."
    exit 1
}

# ── Step 2: Detect GPU ──

Step 2 "Detecting GPU..."

$cudaVersion = $null; $gpuName = "Unknown"
$ErrorActionPreference = "Continue"
$nvOutput = & nvidia-smi --query-gpu=name --format=csv,noheader 2>&1
if ($LASTEXITCODE -eq 0) {
    $gpuName = ($nvOutput | Select-Object -First 1).Trim()
    Ok "$gpuName"
    # Map GPU family to CUDA version (Python 3.13+ needs cu124+)
    if ($gpuName -match "RTX\s*50[0-9]{2}|RTX\s*5[0-9]{3}|Blackwell") {
        $cudaVersion = "cu128"; $cudaLabel = "CUDA 12.8 (RTX 50-series)"
    } elseif ($gpuName -match "RTX\s*[2-4]0[0-9]{2}|RTX\s*[2-4][0-9]{3}|A[0-9]{3,4}|L[0-9]{2}") {
        $cudaVersion = "cu124"; $cudaLabel = "CUDA 12.4 (RTX 20/30/40-series)"
    } elseif ($gpuName -match "GTX\s*1[0-9]{3}|GTX\s*9[0-9]{2}") {
        $cudaVersion = "cu118"; $cudaLabel = "CUDA 11.8 (GTX 10/9-series)"
    } else {
        $cudaVersion = "cu124"; $cudaLabel = "CUDA 12.4 (default)"
    }
    Info "Selected: $cudaLabel"
    $confirm = Prompt "Press Enter to confirm, or type a version (cu118/cu124/cu128)"
    if ($confirm -and $confirm -match "^cu\d+$") { $cudaVersion = $confirm; Warn "Manual override: $cudaVersion" }
} else {
    Warn "nvidia-smi not found - no GPU detected"
    Info "WhisperSync will run in CPU mode (slower)."
}
$ErrorActionPreference = "Stop"

# ── Step 3: Create venv ──

Step 3 "Setting up virtual environment..."

if (Test-Path $VenvPath) {
    Warn "Venv already exists"
    $recreate = Prompt "Delete and recreate? (y/N)"
    if ($recreate -eq "y") { Remove-Item -Recurse -Force $VenvPath; Ok "Old venv removed" }
    else { Info "Keeping existing venv" }
}
if (-not (Test-Path $VenvPath)) {
    $ErrorActionPreference = "Continue"
    & $pythonCmd -m venv $VenvPath 2>&1 | Out-Null
    $ErrorActionPreference = "Stop"
    Ok "Virtual environment created"
}

# ── Step 4: Install dependencies ──

$VenvPython = "$VenvPath\Scripts\python.exe"
$VenvPip = "$VenvPath\Scripts\pip.exe"

Step 4 "Installing dependencies..."

# Suppress git credential popups during pip
$prevGitPrompt = $env:GIT_TERMINAL_PROMPT; $prevGitAskpass = $env:GIT_ASKPASS
$env:GIT_TERMINAL_PROMPT = "0"; $env:GIT_ASKPASS = ""

RunWithSpinner "Upgrading pip" $VenvPython "-m pip install --upgrade pip -qq"
RunWithSpinner "Installing dependencies" $VenvPip "install -r $RequirementsFile -qq"
Ok "Dependencies installed"

# ── Step 5: Install CUDA PyTorch ──

if ($cudaVersion) {
    Step 5 "Installing PyTorch ($cudaVersion)..."
    RunWithSpinner "Installing PyTorch" $VenvPip "install torch torchaudio --index-url https://download.pytorch.org/whl/$cudaVersion --force-reinstall --no-deps -qq"
    Ok "PyTorch GPU installed"
} else {
    Step 5 "Skipping GPU PyTorch (no GPU detected)"
}
$env:GIT_TERMINAL_PROMPT = $prevGitPrompt; $env:GIT_ASKPASS = $prevGitAskpass

# ── Step 6: Standalone marker ──

$markerPath = "$PkgDir\.standalone"
if (-not (Test-Path $markerPath)) { "" | Out-File -Encoding ASCII $markerPath }

# ── Step 7: Verify ──

Step 6 "Verifying installation..."

$ErrorActionPreference = "Continue"
$cudaCheck = & $VenvPython -c "import torch; avail = torch.cuda.is_available(); name = torch.cuda.get_device_name(0) if avail else 'N/A'; print('CUDA:', avail, ' Device:', name)" 2>&1
if ("$cudaCheck" -match "True") { Ok "$cudaCheck" } else { Warn "$cudaCheck" }

$wxCheck = & $VenvPython -c "import whisperx; print('OK')" 2>&1
if ("$wxCheck" -match "OK") { Ok "whisperX ready" } else { Warn "whisperX: $wxCheck" }

$sdCheck = & $VenvPython -c "import sounddevice; print('OK')" 2>&1
if ("$sdCheck" -match "OK") { Ok "Audio capture ready" } else { Warn "sounddevice: $sdCheck" }
$ErrorActionPreference = "Stop"

# ── Step 8: HF token ──

Section "Speaker Identification Setup"

$hfTokenDir = "$env:USERPROFILE\.huggingface"
$hfTokenFile = "$hfTokenDir\token"
if (Test-Path $hfTokenFile) {
    Ok "Hugging Face token found"
} else {
    Info "Meeting mode can identify who said what (speaker diarization)."
    Info "This requires a free Hugging Face token. Skip if you only need dictation."
    Write-Host ""
    Info "To get a token:"
    Info "  1. Create account: https://huggingface.co/join"
    Info "  2. Accept BOTH model licenses (click 'Agree' on each):"
    Write-Host "         " -NoNewline; Write-Host "https://huggingface.co/pyannote/segmentation-3.0" -ForegroundColor DarkYellow
    Write-Host "         " -NoNewline; Write-Host "https://huggingface.co/pyannote/speaker-diarization-3.1" -ForegroundColor DarkYellow
    Info "  3. Generate token: https://huggingface.co/settings/tokens"
    Info "     (New token -> any name -> Read access)"
    Write-Host ""
    $tokenInput = Prompt "Paste your token here (or Enter to skip)"
    if ($tokenInput -and $tokenInput.Trim()) {
        New-Item -ItemType Directory -Path $hfTokenDir -Force | Out-Null
        $tokenInput.Trim() | Out-File -Encoding ASCII $hfTokenFile -NoNewline
        Ok "Token saved"
    } else {
        Warn "Skipped - meeting mode won't identify speakers"
        Info "You can add it later - see README.md"
    }
}

# ── Step 9: Output folder ──

Section "Recording Output"

$docsFolder = [Environment]::GetFolderPath("MyDocuments")
$defaultOut = "$docsFolder\whispersync-meetings"
Info "Where should recordings and transcriptions be saved?"
Write-Host ""
Write-Host "      Default: " -NoNewline -ForegroundColor DarkGray; Write-Host $defaultOut -ForegroundColor White
Write-Host ""
$outputDir = $null
while (-not $outputDir) {
    $customOut = Prompt "Press Enter for default, or paste a full path"
    if (-not $customOut -or -not $customOut.Trim()) {
        $candidate = $defaultOut
    } elseif ([System.IO.Path]::IsPathRooted($customOut.Trim())) {
        $candidate = $customOut.Trim()
    } else {
        Warn "'$($customOut.Trim())' is not a full path - use something like C:\..."
        continue
    }
    try {
        if (-not (Test-Path $candidate)) { New-Item -ItemType Directory -Path $candidate -Force -ErrorAction Stop | Out-Null }
        if (Test-Path $candidate -PathType Container) { $outputDir = $candidate }
        else { Warn "That path exists but is not a folder" }
    } catch { Warn "Can't use that path: $_"; Info "Try a different folder" }
}
$configJson = "{`n  `"output_dir`": `"$($outputDir -replace '\\', '/')`"`n}"
NoBOM "$PkgDir\config.json" $configJson
Ok "Recordings will save to $outputDir"

# ── Step 10: Shortcuts ──

Section "Shortcuts"

$LauncherPath = "$ScriptRoot\start.ps1"
$IconPath = "$PkgDir\whisper-capture.ico"

function CreateShortcut($lnkPath, $label) {
    # Remove existing shortcut if present (avoids locked-file errors on re-install)
    if (Test-Path $lnkPath) { Remove-Item $lnkPath -Force -ErrorAction SilentlyContinue }
    $VbsTemp = "$env:TEMP\wc-shortcut.vbs"
    @"
Set WshShell = WScript.CreateObject("WScript.Shell")
Set lnk = WshShell.CreateShortcut("$lnkPath")
lnk.TargetPath = "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
lnk.Arguments = "-ExecutionPolicy Bypass -WindowStyle Minimized -File ""$LauncherPath"" -Watchdog"
lnk.WorkingDirectory = "$ScriptRoot"
lnk.WindowStyle = 7
lnk.Description = "WhisperSync - local speech-to-text"
lnk.IconLocation = "$IconPath, 0"
lnk.Save
"@ | Out-File -Encoding ASCII $VbsTemp
    $ErrorActionPreference = "Continue"
    cscript //nologo $VbsTemp 2>&1 | Out-Null
    $ErrorActionPreference = "Stop"
    Remove-Item $VbsTemp -ErrorAction SilentlyContinue
    Ok "$label created"
}

$createDesktop = Prompt "Create a Desktop shortcut? (Y/n)"
if ($createDesktop -ne "n") {
    CreateShortcut "$([Environment]::GetFolderPath('Desktop'))\WhisperSync.lnk" "Desktop shortcut"
}

$createStartup = Prompt "Auto-launch on login? (Y/n)"
if ($createStartup -ne "n") {
    CreateShortcut "$([Environment]::GetFolderPath('Startup'))\WhisperSync.lnk" "Startup shortcut"
}

# ── Step 11: Bootstrap models ──

Section "Downloading Models"

$bootstrapScript = "$env:TEMP\ws-bootstrap.py"
$bootstrapCode = @"
import warnings, os
warnings.filterwarnings('ignore')
os.environ['HF_HUB_DISABLE_SYMLINKS_WARNING'] = '1'
from whisper_sync.model_status import bootstrap_models
from whisper_sync import config
bootstrap_models(config.load())
"@
NoBOM $bootstrapScript $bootstrapCode
RunWithSpinner "Downloading models" $VenvPython $bootstrapScript
Remove-Item $bootstrapScript -ErrorAction SilentlyContinue
Ok "Models cached"

# ══════════════════════════════════════════════
#  COMPLETION SCREEN
# ══════════════════════════════════════════════

Write-Host ""
Write-Host ""
Write-Host "  =============================================" -ForegroundColor DarkCyan
Write-Host "       " -NoNewline; Write-Host "WhisperSync" -ForegroundColor Cyan -NoNewline; Write-Host " installed successfully!" -ForegroundColor Green
Write-Host "  =============================================" -ForegroundColor DarkCyan
Write-Host ""
Write-Host "  " -NoNewline; Write-Host "LAUNCH" -ForegroundColor Yellow
Write-Host "      Double-click " -NoNewline -ForegroundColor DarkGray; Write-Host "WhisperSync" -NoNewline -ForegroundColor Cyan; Write-Host " on your Desktop" -ForegroundColor DarkGray
Write-Host "      Or: " -NoNewline -ForegroundColor DarkGray; Write-Host "powershell -File start.ps1" -ForegroundColor DarkYellow
Write-Host ""
Write-Host "  Recordings: " -NoNewline -ForegroundColor DarkGray; Write-Host "$outputDir" -ForegroundColor Cyan
Write-Host ""
Write-Host "  " -NoNewline; Write-Host "100% LOCAL" -ForegroundColor Green -NoNewline; Write-Host "                " -NoNewline; Write-Host "CLOUD (optional)" -ForegroundColor DarkYellow
Write-Host "    Dictation              " -NoNewline -ForegroundColor Gray; Write-Host "Meeting minutes" -ForegroundColor DarkGray
Write-Host "    Transcription          " -NoNewline -ForegroundColor Gray; Write-Host "(via Claude CLI)" -ForegroundColor DarkGray
Write-Host "    Speaker ID" -ForegroundColor Gray
Write-Host "    Audio capture" -ForegroundColor Gray
Write-Host ""
Write-Host "  " -NoNewline; Write-Host "Audio never leaves your machine." -ForegroundColor Green
Write-Host ""

# ── Get Started (always shown) ──

Write-Host "  =============================================" -ForegroundColor DarkCyan
Write-Host "  " -NoNewline; Write-Host "GET STARTED" -ForegroundColor Cyan -NoNewline; Write-Host "  Record your first dictation and meeting!" -ForegroundColor White
Write-Host "  =============================================" -ForegroundColor DarkCyan
Write-Host ""
Write-Host "  " -NoNewline; Write-Host "Try Dictation:" -ForegroundColor Magenta
Write-Host ""
Write-Host "    " -NoNewline; Write-Host "1." -ForegroundColor Cyan -NoNewline; Write-Host " Click any text box" -NoNewline -ForegroundColor White; Write-Host " (Notepad, browser, chat)" -ForegroundColor DarkGray
Write-Host "    " -NoNewline; Write-Host "2." -ForegroundColor Cyan -NoNewline; Write-Host " Press " -NoNewline -ForegroundColor DarkGray; Write-Host "Ctrl+Shift+Space" -ForegroundColor Yellow -NoNewline; Write-Host " and say your favorite quote" -ForegroundColor White
Write-Host "    " -NoNewline; Write-Host "3." -ForegroundColor Cyan -NoNewline; Write-Host " Press " -NoNewline -ForegroundColor DarkGray; Write-Host "Ctrl+Shift+Space" -ForegroundColor Yellow -NoNewline; Write-Host " again" -ForegroundColor DarkGray
Write-Host "    " -NoNewline; Write-Host "4." -ForegroundColor Cyan -NoNewline; Write-Host " Voila! " -NoNewline -ForegroundColor Green; Write-Host "Your words appear right where you clicked." -ForegroundColor DarkGray
Write-Host ""
Write-Host "  " -NoNewline; Write-Host "Try a Meeting:" -ForegroundColor Magenta
Write-Host ""
Write-Host "    " -NoNewline; Write-Host "1." -ForegroundColor Cyan -NoNewline; Write-Host " Find the " -NoNewline -ForegroundColor DarkGray; Write-Host "gray circle" -NoNewline -ForegroundColor White; Write-Host " in your system tray (bottom-right)" -ForegroundColor DarkGray
Write-Host "    " -NoNewline; Write-Host "2." -ForegroundColor Cyan -NoNewline; Write-Host " Left-click it " -NoNewline -ForegroundColor Yellow; Write-Host "(it turns " -NoNewline -ForegroundColor DarkGray; Write-Host "red" -NoNewline -ForegroundColor Red; Write-Host " - you're recording!)" -ForegroundColor DarkGray
Write-Host "    " -NoNewline; Write-Host "3." -ForegroundColor Cyan -NoNewline; Write-Host " Talk, play a video, join a call - it hears everything" -ForegroundColor White
Write-Host "    " -NoNewline; Write-Host "4." -ForegroundColor Cyan -NoNewline; Write-Host " Left-click again" -NoNewline -ForegroundColor Yellow; Write-Host " and follow the save dialog" -ForegroundColor DarkGray
Write-Host "    " -NoNewline; Write-Host "5." -ForegroundColor Cyan -NoNewline; Write-Host " Done! " -NoNewline -ForegroundColor Green; Write-Host "Transcript with speaker names in your folder." -ForegroundColor DarkGray
Write-Host ""
Write-Host "    " -NoNewline; Write-Host "   " -NoNewline; Write-Host "*" -ForegroundColor DarkYellow -NoNewline; Write-Host " With " -NoNewline -ForegroundColor DarkCyan; Write-Host "Cloud AI" -NoNewline -ForegroundColor Cyan; Write-Host " (Claude CLI), minutes are automatically" -ForegroundColor DarkCyan
Write-Host "    " -NoNewline; Write-Host "     generated with " -NoNewline -ForegroundColor DarkCyan; Write-Host "action items" -NoNewline -ForegroundColor White; Write-Host " and " -NoNewline -ForegroundColor DarkCyan; Write-Host "summaries" -NoNewline -ForegroundColor White; Write-Host " " -NoNewline; Write-Host "*" -ForegroundColor DarkYellow
Write-Host ""

# ── Benchmark (gated) ──

Write-Host "  =============================================" -ForegroundColor DarkCyan
Write-Host "  " -NoNewline; Write-Host "BENCHMARK" -ForegroundColor Cyan -NoNewline; Write-Host "  Let's compare models on your GPU!" -ForegroundColor White
Write-Host "  =============================================" -ForegroundColor DarkCyan
Write-Host ""
Write-Host "  Each model turns your speech into text at different" -ForegroundColor DarkGray
Write-Host "  speeds. Faster models give quicker results, larger" -ForegroundColor DarkGray
Write-Host "  models are more accurate. Let's see how they perform" -ForegroundColor DarkGray
Write-Host "  on your hardware." -ForegroundColor DarkGray
Write-Host ""
$runBenchmark = Prompt "Ready? (Y/n)"
if ($runBenchmark -ne "n") {
    Write-Host ""
    Step 12 "Running benchmark..."
    $benchScript = "$env:TEMP\ws-bench.py"
    $benchCode = @"
import warnings, os, time, numpy as np
warnings.filterwarnings('ignore')
os.environ['HF_HUB_DISABLE_SYMLINKS_WARNING'] = '1'
from whisper_sync.transcribe import _load_whisper_model, _get_device
from whisper_sync import config
cfg = config.load()
device = _get_device()
audio = np.zeros(16000 * 5, dtype=np.float32)
models_to_test = []
from whisper_sync.model_status import is_model_cached
for m in ['tiny', 'base', 'large-v3']:
    if is_model_cached(m):
        models_to_test.append(m)
print()
for name in models_to_test:
    compute = 'int8' if device == 'cpu' else cfg.get('compute_type', 'float16')
    model = _load_whisper_model(name, compute, 'en')
    model.transcribe(audio, batch_size=4, language='en')
    t0 = time.perf_counter()
    for _ in range(3):
        model.transcribe(audio, batch_size=4, language='en')
    avg = (time.perf_counter() - t0) / 3
    quality = {'tiny': 'Basic', 'base': 'Good', 'large-v3': 'Best'}
    bar = '#' * max(1, int(20 - avg * 10))
    print(f'    {name:<10} {avg:.2f}s  {bar:<20}  ({quality.get(name, "")})')
print()
"@
    NoBOM $benchScript $benchCode
    # Benchmark runs with visible output (user wants to see results)
    $ErrorActionPreference = "Continue"
    & $VenvPython $benchScript 2>&1 | Where-Object { $_ -notmatch "warning|Warning|torchcodec|Lightning|Xet Storage" }
    $ErrorActionPreference = "Stop"
    Remove-Item $benchScript -ErrorAction SilentlyContinue
    Ok "Benchmark complete"
    Write-Host ""
    Write-Host "  I recommend always using the " -NoNewline -ForegroundColor DarkGray; Write-Host "best model for meetings" -NoNewline -ForegroundColor White; Write-Host " -" -ForegroundColor DarkGray
    Write-Host "  accuracy matters. For dictation, use what feels right" -ForegroundColor DarkGray
    Write-Host "  based on the speeds above." -ForegroundColor DarkGray
    Write-Host ""
    $setModel = Prompt "Set both to large-v3? (Y/n)"
    if ($setModel -ne "n") {
        $existingCfg = @{}
        $cfgPath = "$PkgDir\config.json"
        if (Test-Path $cfgPath) {
            $raw = Get-Content $cfgPath -Raw
            $parsed = $raw | ConvertFrom-Json
            $parsed.PSObject.Properties | ForEach-Object { $existingCfg[$_.Name] = $_.Value }
        }
        $existingCfg["model"] = "large-v3"
        $existingCfg["dictation_model"] = "large-v3"
        NoBOM $cfgPath ($existingCfg | ConvertTo-Json)
        Ok "Models set to large-v3"
    }
}

# ── How It Works (gated) ──

Write-Host ""
Write-Host "  =============================================" -ForegroundColor DarkCyan
Write-Host ""
$showHow = Prompt "See how it all works? (Y/n)"
if ($showHow -ne "n") {
    Write-Host ""
    Write-Host "  =============================================" -ForegroundColor DarkCyan
    Write-Host "  " -NoNewline; Write-Host "HOW IT WORKS" -ForegroundColor Cyan
    Write-Host "  =============================================" -ForegroundColor DarkCyan
    Write-Host ""
    Write-Host "  " -NoNewline; Write-Host "DICTATION" -ForegroundColor Magenta -NoNewline; Write-Host "  voice-to-text anywhere" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "      " -NoNewline; Write-Host "Start recording    " -ForegroundColor White; Write-Host "      " -NoNewline; Write-Host "Ctrl+Shift+Space" -ForegroundColor Yellow
    Write-Host "      " -NoNewline; Write-Host "Stop & paste       " -ForegroundColor White; Write-Host "      " -NoNewline; Write-Host "Ctrl+Shift+Space" -ForegroundColor Yellow -NoNewline; Write-Host " (same key)" -ForegroundColor DarkGray
    Write-Host "      " -NoNewline; Write-Host "Cancel             " -ForegroundColor White; Write-Host "      " -NoNewline; Write-Host "Left-click" -ForegroundColor Yellow -NoNewline; Write-Host " the tray icon" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "      Text goes into the " -NoNewline -ForegroundColor DarkGray; Write-Host "focused text field" -NoNewline -ForegroundColor White; Write-Host " (editor," -ForegroundColor DarkGray
    Write-Host "      browser, chat, etc). If nothing is focused, it's" -ForegroundColor DarkGray
    Write-Host "      copied to your " -NoNewline -ForegroundColor DarkGray; Write-Host "clipboard" -ForegroundColor White -NoNewline; Write-Host " instead." -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "  - - - - - - - - - - - - - - - - - - - - - -" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "  " -NoNewline; Write-Host "MEETING" -ForegroundColor Magenta -NoNewline; Write-Host "    record, transcribe, identify speakers" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "      " -NoNewline; Write-Host "Start recording    " -ForegroundColor White; Write-Host "      " -NoNewline; Write-Host "Ctrl+Shift+M" -ForegroundColor Yellow
    Write-Host "      " -NoNewline; Write-Host "    or             " -ForegroundColor DarkGray; Write-Host "      " -NoNewline; Write-Host "Left-click" -ForegroundColor Yellow -NoNewline; Write-Host " the tray icon" -ForegroundColor DarkGray
    Write-Host "      " -NoNewline; Write-Host "Stop & save        " -ForegroundColor White; Write-Host "      " -NoNewline; Write-Host "Ctrl+Shift+M" -ForegroundColor Yellow -NoNewline; Write-Host " (same key)" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "      Records " -NoNewline -ForegroundColor DarkGray; Write-Host "your mic + system audio" -NoNewline -ForegroundColor White; Write-Host " (what you hear)." -ForegroundColor DarkGray
    Write-Host "      Works with any meeting on your computer:" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "        " -NoNewline; Write-Host "Zoom" -NoNewline -ForegroundColor White; Write-Host ", " -NoNewline -ForegroundColor DarkGray; Write-Host "Google Meet" -NoNewline -ForegroundColor White; Write-Host ", " -NoNewline -ForegroundColor DarkGray; Write-Host "Teams" -NoNewline -ForegroundColor White; Write-Host ", " -NoNewline -ForegroundColor DarkGray; Write-Host "phone calls" -ForegroundColor White
    Write-Host "        " -NoNewline; Write-Host "In-person" -NoNewline -ForegroundColor White; Write-Host " (picks up your mic)" -ForegroundColor DarkGray
    Write-Host "        " -NoNewline; Write-Host "Any audio" -NoNewline -ForegroundColor White; Write-Host " playing through your speakers" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "      After you stop, you name the meeting and get a full" -ForegroundColor DarkGray
    Write-Host "      transcript with " -NoNewline -ForegroundColor DarkGray; Write-Host "speaker labels" -NoNewline -ForegroundColor White; Write-Host ". WhisperSync " -NoNewline -ForegroundColor DarkGray; Write-Host "learns" -ForegroundColor Green
    Write-Host "      " -NoNewline; Write-Host "speakers over time" -NoNewline -ForegroundColor Green; Write-Host " - the more you use it, the better" -ForegroundColor DarkGray
    Write-Host "      it gets at recognizing who's talking." -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "  - - - - - - - - - - - - - - - - - - - - - -" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "  " -NoNewline; Write-Host "TRAY ICON" -ForegroundColor Magenta
    Write-Host ""
    Write-Host "      " -NoNewline; Write-Host "Gray " -ForegroundColor Gray -NoNewline; Write-Host " Ready" -ForegroundColor DarkGray -NoNewline; Write-Host "         " -NoNewline; Write-Host "Red " -ForegroundColor Red -NoNewline; Write-Host " Recording (live!)" -ForegroundColor DarkGray
    Write-Host "      " -NoNewline; Write-Host "Amber" -ForegroundColor DarkYellow -NoNewline; Write-Host " Transcribing" -ForegroundColor DarkGray -NoNewline; Write-Host "   " -NoNewline; Write-Host "Green " -ForegroundColor Green -NoNewline; Write-Host "Done" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "      " -NoNewline; Write-Host "Left-click " -ForegroundColor Yellow -NoNewline; Write-Host "= start/cancel meeting" -ForegroundColor DarkGray
    Write-Host "      " -NoNewline; Write-Host "Right-click" -ForegroundColor Yellow -NoNewline; Write-Host " = settings, model downloads, hotkeys" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "  =============================================" -ForegroundColor DarkCyan
}

Write-Host ""

} catch {
    Write-Host ""
    Write-Host "  =============================================" -ForegroundColor Red
    Write-Host "         INSTALLATION FAILED" -ForegroundColor Red
    Write-Host "  =============================================" -ForegroundColor Red
    Write-Host ""
    Write-Host "  Error: " -NoNewline -ForegroundColor Red; Write-Host "$_" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Screenshot this and send to Colby." -ForegroundColor DarkGray
    Write-Host ""
}

Write-Host "  Press any key to close..." -ForegroundColor DarkGray
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
