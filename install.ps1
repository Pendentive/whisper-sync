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
function RunWithProgress($activity, $command, $arguments) {
    # Run a command silently while showing a PowerShell progress bar
    $proc = Start-Process -FilePath $command -ArgumentList $arguments `
        -NoNewWindow -RedirectStandardOutput "$env:TEMP\ws-stdout.log" `
        -RedirectStandardError "$env:TEMP\ws-stderr.log" -PassThru
    $spinner = @("|", "/", "-", "\")
    $i = 0
    while (-not $proc.HasExited) {
        $pct = [math]::Min(95, $i * 2)  # creep toward 95%, never hit 100 until done
        Write-Progress -Activity $activity -Status "$($spinner[$i % 4]) Installing..." -PercentComplete $pct
        Start-Sleep -Milliseconds 500
        $i++
    }
    Write-Progress -Activity $activity -Status "Done" -PercentComplete 100 -Completed
    if ($proc.ExitCode -ne 0) {
        $errLog = Get-Content "$env:TEMP\ws-stderr.log" -Raw -ErrorAction SilentlyContinue
        $outLog = Get-Content "$env:TEMP\ws-stdout.log" -Raw -ErrorAction SilentlyContinue
        if ($errLog) { Write-Host $errLog -ForegroundColor Red }
        if ($outLog -and $outLog -match "ERROR") { Write-Host $outLog -ForegroundColor Red }
        throw "Command failed with exit code $($proc.ExitCode)"
    }
    Remove-Item "$env:TEMP\ws-stdout.log", "$env:TEMP\ws-stderr.log" -ErrorAction SilentlyContinue
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
foreach ($cmd in @("python", "python3", "py")) {
    try {
        $ver = & $cmd --version 2>&1
        if ($ver -match "Python (\d+)\.(\d+)") {
            $major = [int]$Matches[1]
            $minor = [int]$Matches[2]
            if ($major -ge 3 -and $minor -ge 10) {
                $pythonCmd = $cmd
                Ok "$ver"
                break
            }
        }
    } catch {}
}

if (-not $pythonCmd) {
    Write-Host ""
    Warn "Python 3.10+ not found!"
    Info "Install from https://python.org/downloads/"
    Info "Make sure to check 'Add to PATH' during installation."
    exit 1
}

# ── Step 2: Detect GPU ──

Step 2 "Detecting GPU..."

$cudaVersion = $null
$gpuName = "Unknown"

try {
    $nvOutput = & nvidia-smi --query-gpu=name --format=csv,noheader 2>&1
    if ($LASTEXITCODE -eq 0) {
        $gpuName = ($nvOutput | Select-Object -First 1).Trim()
        Ok "$gpuName"

        # Map GPU family to CUDA version
        # Note: Python 3.13+ only has PyTorch wheels for cu124+, not cu121
        if ($gpuName -match "RTX\s*50[0-9]{2}|RTX\s*5[0-9]{3}|Blackwell") {
            $cudaVersion = "cu128"
            $cudaLabel = "CUDA 12.8 (RTX 50-series)"
        } elseif ($gpuName -match "RTX\s*[2-4]0[0-9]{2}|RTX\s*[2-4][0-9]{3}|A[0-9]{3,4}|L[0-9]{2}") {
            $cudaVersion = "cu124"
            $cudaLabel = "CUDA 12.4 (RTX 20/30/40-series)"
        } elseif ($gpuName -match "GTX\s*1[0-9]{3}|GTX\s*9[0-9]{2}") {
            $cudaVersion = "cu118"
            $cudaLabel = "CUDA 11.8 (GTX 10/9-series)"
        } else {
            $cudaVersion = "cu124"
            $cudaLabel = "CUDA 12.4 (default)"
        }

        Info "Selected: $cudaLabel"
        $confirm = Prompt "Press Enter to confirm, or type a version (cu118/cu124/cu128)"
        if ($confirm -and $confirm -match "^cu\d+$") {
            $cudaVersion = $confirm
            Warn "Manual override: $cudaVersion"
        }
    }
} catch {
    Warn "nvidia-smi not found - no GPU detected"
    Info "WhisperSync will run in CPU mode (slower)."
    Info "Install NVIDIA drivers from https://nvidia.com/drivers"
}

# ── Step 3: Create venv ──

Step 3 "Setting up virtual environment..."

if (Test-Path $VenvPath) {
    Warn "Venv already exists"
    $recreate = Prompt "Delete and recreate? (y/N)"
    if ($recreate -eq "y") {
        Remove-Item -Recurse -Force $VenvPath
        Ok "Old venv removed"
    } else {
        Info "Keeping existing venv"
    }
}

if (-not (Test-Path $VenvPath)) {
    & $pythonCmd -m venv $VenvPath
    Ok "Virtual environment created"
}

# ── Step 4: Install dependencies ──

$VenvPython = "$VenvPath\Scripts\python.exe"
$VenvPip = "$VenvPath\Scripts\pip.exe"

Step 4 "Installing dependencies..."

& $VenvPython -m pip install --upgrade pip --quiet 2>&1 | Out-Null

# Suppress git credential popups during pip - pip installs from PyPI, not git repos.
$prevGitPrompt = $env:GIT_TERMINAL_PROMPT
$prevGitAskpass = $env:GIT_ASKPASS
$env:GIT_TERMINAL_PROMPT = "0"
$env:GIT_ASKPASS = ""
RunWithProgress "Installing Python dependencies" $VenvPip "install -r $RequirementsFile -qq"
Ok "Dependencies installed"

# ── Step 5: Install CUDA PyTorch ──

if ($cudaVersion) {
    Step 5 "Installing PyTorch ($cudaVersion)..."
    RunWithProgress "Installing PyTorch (GPU)" $VenvPip "install torch torchaudio --index-url https://download.pytorch.org/whl/$cudaVersion --force-reinstall --no-deps -qq"
    Ok "PyTorch GPU installed"
} else {
    Step 5 "Skipping GPU PyTorch (no GPU detected)"
}
# Restore git env
$env:GIT_TERMINAL_PROMPT = $prevGitPrompt
$env:GIT_ASKPASS = $prevGitAskpass

# ── Step 6: Standalone marker ──

$markerPath = "$PkgDir\.standalone"
if (-not (Test-Path $markerPath)) {
    "" | Out-File -Encoding ASCII $markerPath
}

# ── Step 7: Verify ──

Step 6 "Verifying installation..."

$cudaCheck = & $VenvPython -c "import torch; avail = torch.cuda.is_available(); name = torch.cuda.get_device_name(0) if avail else 'N/A'; print('CUDA:', avail, ' Device:', name)" 2>&1
if ($cudaCheck -match "True") { Ok "CUDA: $cudaCheck" } else { Warn "CUDA: $cudaCheck" }

$wxCheck = & $VenvPython -c "import whisperx; print('OK')" 2>&1
if ($wxCheck -match "OK") { Ok "whisperX ready" } else { Warn "whisperX: $wxCheck" }

$sdCheck = & $VenvPython -c "import sounddevice; print('OK')" 2>&1
if ($sdCheck -match "OK") { Ok "Audio capture ready" } else { Warn "sounddevice: $sdCheck" }

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
    # Validate we can create/access the folder
    try {
        New-Item -ItemType Directory -Path $candidate -Force -ErrorAction Stop | Out-Null
        $outputDir = $candidate
    } catch {
        Warn "Can't use that path: $_"
        Info "Try a different folder (e.g. C:\Users\$env:USERNAME\Documents\whispersync-meetings)"
    }
}
$configPath = "$PkgDir\config.json"
$configJson = "{`n  `"output_dir`": `"$($outputDir -replace '\\', '/')`"`n}"
[System.IO.File]::WriteAllText($configPath, $configJson, (New-Object System.Text.UTF8Encoding $false))
Ok "Recordings will save to $outputDir"

# ── Step 10: Shortcuts ──

Section "Shortcuts"

$LauncherPath = "$ScriptRoot\start.ps1"
$IconPath = "$PkgDir\whisper-capture.ico"

$createDesktop = Prompt "Create a Desktop shortcut? (Y/n)"
if ($createDesktop -ne "n") {
    $DesktopFolder = [Environment]::GetFolderPath("Desktop")
    $DesktopShortcut = "$DesktopFolder\WhisperSync.lnk"
    $VbsTemp = "$env:TEMP\wc-shortcut.vbs"
    @"
Set WshShell = WScript.CreateObject("WScript.Shell")
Set lnk = WshShell.CreateShortcut("$DesktopShortcut")
lnk.TargetPath = "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
lnk.Arguments = "-ExecutionPolicy Bypass -WindowStyle Minimized -File ""$LauncherPath"" -Watchdog"
lnk.WorkingDirectory = "$ScriptRoot"
lnk.WindowStyle = 7
lnk.Description = "WhisperSync - local speech-to-text"
lnk.IconLocation = "$IconPath, 0"
lnk.Save
"@ | Out-File -Encoding ASCII $VbsTemp
    cscript //nologo $VbsTemp
    Remove-Item $VbsTemp -ErrorAction SilentlyContinue
    Ok "Desktop shortcut created"
}

$createStartup = Prompt "Auto-launch on login? (Y/n)"
if ($createStartup -ne "n") {
    $StartupFolder = [Environment]::GetFolderPath("Startup")
    $StartupShortcut = "$StartupFolder\WhisperSync.lnk"
    $VbsTemp = "$env:TEMP\wc-shortcut.vbs"
    @"
Set WshShell = WScript.CreateObject("WScript.Shell")
Set lnk = WshShell.CreateShortcut("$StartupShortcut")
lnk.TargetPath = "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
lnk.Arguments = "-ExecutionPolicy Bypass -WindowStyle Minimized -File ""$LauncherPath"" -Watchdog"
lnk.WorkingDirectory = "$ScriptRoot"
lnk.WindowStyle = 7
lnk.Description = "WhisperSync - local speech-to-text"
lnk.IconLocation = "$IconPath, 0"
lnk.Save
"@ | Out-File -Encoding ASCII $VbsTemp
    cscript //nologo $VbsTemp
    Remove-Item $VbsTemp -ErrorAction SilentlyContinue
    Ok "Startup shortcut created"
}

# ── Step 11: Bootstrap models ──

Section "Downloading Models"

RunWithProgress "Downloading transcription models" $VenvPython "-c ""from whisper_sync.model_status import bootstrap_models; from whisper_sync import config; bootstrap_models(config.load())"""
Ok "Models cached"

# ── Done ──

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
Write-Host "  ---------------------------------------------" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  " -NoNewline; Write-Host "DICTATION" -ForegroundColor Magenta -NoNewline; Write-Host "  speak -> text in any app" -ForegroundColor DarkGray
Write-Host "      Start/Stop   " -NoNewline -ForegroundColor DarkGray; Write-Host "Ctrl+Shift+Space" -ForegroundColor Yellow
Write-Host "      Cancel       " -NoNewline -ForegroundColor DarkGray; Write-Host "Left-click tray icon" -ForegroundColor Yellow
Write-Host "      " -NoNewline; Write-Host "Talk, press again. Text pastes at your cursor." -ForegroundColor DarkGray
Write-Host ""
Write-Host "  " -NoNewline; Write-Host "MEETING" -ForegroundColor Magenta -NoNewline; Write-Host "    record everything, get a transcript" -ForegroundColor DarkGray
Write-Host "      Start/Stop   " -NoNewline -ForegroundColor DarkGray; Write-Host "Ctrl+Shift+M" -ForegroundColor Yellow -NoNewline; Write-Host "  or  " -ForegroundColor DarkGray -NoNewline; Write-Host "Left-click" -ForegroundColor Yellow
Write-Host "      " -NoNewline; Write-Host "Records mic + system audio. Name it, get a transcript." -ForegroundColor DarkGray
Write-Host ""
Write-Host "  " -NoNewline; Write-Host "TRAY ICON" -ForegroundColor Magenta
Write-Host "      " -NoNewline; Write-Host "Gray " -ForegroundColor Gray -NoNewline; Write-Host " Ready" -ForegroundColor DarkGray -NoNewline; Write-Host "     " -NoNewline; Write-Host "Red " -ForegroundColor Red -NoNewline; Write-Host " Recording" -ForegroundColor DarkGray
Write-Host "      " -NoNewline; Write-Host "Amber" -ForegroundColor DarkYellow -NoNewline; Write-Host " Working" -ForegroundColor DarkGray -NoNewline; Write-Host "   " -NoNewline; Write-Host "Green " -ForegroundColor Green -NoNewline; Write-Host "Done" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  ---------------------------------------------" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  " -NoNewline; Write-Host "Right-click tray icon" -ForegroundColor White -NoNewline; Write-Host " for settings & model downloads" -ForegroundColor DarkGray
Write-Host "  Recordings: " -NoNewline -ForegroundColor DarkGray; Write-Host "$outputDir" -ForegroundColor Cyan
Write-Host ""
Write-Host "  =============================================" -ForegroundColor DarkCyan
Write-Host ""
Write-Host "  " -NoNewline; Write-Host "100% LOCAL" -ForegroundColor Green -NoNewline; Write-Host "                " -NoNewline; Write-Host "CLOUD (optional)" -ForegroundColor DarkYellow
Write-Host "    Dictation              " -NoNewline -ForegroundColor Gray; Write-Host "Meeting minutes" -ForegroundColor DarkGray
Write-Host "    Transcription          " -NoNewline -ForegroundColor Gray; Write-Host "(via Claude CLI)" -ForegroundColor DarkGray
Write-Host "    Speaker ID" -ForegroundColor Gray
Write-Host "    Audio capture" -ForegroundColor Gray
Write-Host ""
Write-Host "  " -NoNewline; Write-Host "Audio never leaves your machine." -ForegroundColor Green
Write-Host ""
Write-Host "  =============================================" -ForegroundColor DarkCyan
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
