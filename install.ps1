# WhisperSync installer - creates venv, detects GPU, installs CUDA PyTorch + dependencies
# Usage: powershell -ExecutionPolicy Bypass -File install.ps1

$ScriptRoot = $PSScriptRoot
$PkgDir = "$ScriptRoot\whisper_sync"
$VenvPath = "$ScriptRoot\whisper-env"
$RequirementsFile = "$ScriptRoot\requirements.txt"

try {
$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "=== WhisperSync Installer ===" -ForegroundColor Cyan
Write-Host ""

# ── Step 1: Check Python ──

$pythonCmd = $null
foreach ($cmd in @("python", "python3", "py")) {
    try {
        $ver = & $cmd --version 2>&1
        if ($ver -match "Python (\d+)\.(\d+)") {
            $major = [int]$Matches[1]
            $minor = [int]$Matches[2]
            if ($major -ge 3 -and $minor -ge 10) {
                $pythonCmd = $cmd
                Write-Host "[OK] Found $ver" -ForegroundColor Green
                break
            }
        }
    } catch {}
}

if (-not $pythonCmd) {
    Write-Host "[ERROR] Python 3.10+ not found. Install from https://python.org/downloads/" -ForegroundColor Red
    Write-Host "        Make sure to check 'Add to PATH' during installation." -ForegroundColor Yellow
    exit 1
}

# ── Step 2: Detect GPU + CUDA version ──

Write-Host ""
Write-Host "Detecting GPU..." -ForegroundColor Cyan

$cudaVersion = $null
$gpuName = "Unknown"

try {
    $nvOutput = & nvidia-smi --query-gpu=name --format=csv,noheader 2>&1
    if ($LASTEXITCODE -eq 0) {
        $gpuName = ($nvOutput | Select-Object -First 1).Trim()
        Write-Host "[OK] GPU: $gpuName" -ForegroundColor Green

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

        Write-Host "     Selected: $cudaLabel" -ForegroundColor Green
        $confirm = Read-Host "     Press Enter to confirm, or type a CUDA version (cu118/cu124/cu128)"
        if ($confirm -and $confirm -match "^cu\d+$") {
            $cudaVersion = $confirm
            Write-Host "     Using: $cudaVersion (manual override)" -ForegroundColor Yellow
        }
    }
} catch {
    Write-Host "[WARNING] nvidia-smi not found. No GPU detected." -ForegroundColor Yellow
    Write-Host "          WhisperSync will run in CPU mode (much slower)." -ForegroundColor Yellow
    Write-Host "          Install NVIDIA drivers from https://nvidia.com/drivers" -ForegroundColor Yellow
}

# ── Step 3: Create venv ──

Write-Host ""
if (Test-Path $VenvPath) {
    Write-Host "[INFO] Venv already exists at $VenvPath" -ForegroundColor Yellow
    $recreate = Read-Host "       Delete and recreate? (y/N)"
    if ($recreate -eq "y") {
        Remove-Item -Recurse -Force $VenvPath
    } else {
        Write-Host "       Keeping existing venv. Skipping to dependency check..." -ForegroundColor Yellow
    }
}

if (-not (Test-Path $VenvPath)) {
    Write-Host "Creating virtual environment..." -ForegroundColor Green
    & $pythonCmd -m venv $VenvPath
}

# ── Step 4: Install dependencies ──

$VenvPython = "$VenvPath\Scripts\python.exe"
$VenvPip = "$VenvPath\Scripts\pip.exe"

Write-Host "Upgrading pip..." -ForegroundColor Green
& $VenvPython -m pip install --upgrade pip --quiet

Write-Host "Installing dependencies..." -ForegroundColor Green
& $VenvPip install -r $RequirementsFile

# ── Step 5: Install CUDA PyTorch ──

if ($cudaVersion) {
    Write-Host ""
    Write-Host "Installing PyTorch with $cudaVersion..." -ForegroundColor Green
    Write-Host "(This overrides the CPU-only torch that whisperX installs)" -ForegroundColor Gray
    & $VenvPip install torch torchaudio --index-url "https://download.pytorch.org/whl/$cudaVersion" --force-reinstall --no-deps
}

# ── Step 6: Create .standalone marker ──

$markerPath = "$PkgDir\.standalone"
if (-not (Test-Path $markerPath)) {
    "" | Out-File -Encoding ASCII $markerPath
}

# ── Step 7: Verify installation ──

Write-Host ""
Write-Host "=== Verification ===" -ForegroundColor Cyan

$cudaCheck = & $VenvPython -c "import torch; avail = torch.cuda.is_available(); name = torch.cuda.get_device_name(0) if avail else 'N/A'; print('CUDA:', avail, '  Device:', name)" 2>&1
Write-Host "  $cudaCheck"

$wxCheck = & $VenvPython -c "import whisperx; print('whisperX: OK')" 2>&1
Write-Host "  $wxCheck"

$sdCheck = & $VenvPython -c "import sounddevice; print('sounddevice: OK')" 2>&1
Write-Host "  $sdCheck"

# ── Step 8: Check HF token ──

Write-Host ""
$hfTokenDir = "$env:USERPROFILE\.huggingface"
$hfTokenFile = "$hfTokenDir\token"
if (Test-Path $hfTokenFile) {
    Write-Host "[OK] Hugging Face token found" -ForegroundColor Green
} else {
    Write-Host "=== Hugging Face Token Setup ===" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Meeting mode uses speaker diarization (identifies who said what)." -ForegroundColor White
    Write-Host "This requires a free Hugging Face token. Skip if you only need dictation." -ForegroundColor White
    Write-Host ""
    Write-Host "To get a token:" -ForegroundColor Yellow
    Write-Host "  1. Create account: https://huggingface.co/join" -ForegroundColor Gray
    Write-Host "  2. Accept BOTH model licenses (click 'Agree' on each):" -ForegroundColor Gray
    Write-Host "     https://huggingface.co/pyannote/segmentation-3.0" -ForegroundColor Gray
    Write-Host "     https://huggingface.co/pyannote/speaker-diarization-3.1" -ForegroundColor Gray
    Write-Host "  3. Generate token: https://huggingface.co/settings/tokens" -ForegroundColor Gray
    Write-Host "     (New token -> any name -> Read access)" -ForegroundColor Gray
    Write-Host ""
    $tokenInput = Read-Host "Paste your Hugging Face token here (or press Enter to skip)"
    if ($tokenInput -and $tokenInput.Trim()) {
        New-Item -ItemType Directory -Path $hfTokenDir -Force | Out-Null
        $tokenInput.Trim() | Out-File -Encoding ASCII $hfTokenFile -NoNewline
        Write-Host "[OK] Token saved to $hfTokenFile" -ForegroundColor Green
    } else {
        Write-Host "[SKIP] No token entered. Meeting mode will not have speaker identification." -ForegroundColor Yellow
        Write-Host "       You can add it later - see README.md for instructions." -ForegroundColor Yellow
    }
}

# ── Step 9: Configure output folder ──

Write-Host ""
Write-Host "=== Recording Output ===" -ForegroundColor Cyan
$defaultOut = "$ScriptRoot\transcriptions"
Write-Host "Where should meeting recordings be saved?" -ForegroundColor White
Write-Host "  Default: $defaultOut" -ForegroundColor Gray
$customOut = Read-Host "  Press Enter for default, or paste an absolute path"
if ($customOut -and $customOut.Trim()) {
    $outputDir = $customOut.Trim()
} else {
    $outputDir = $defaultOut
}
# Write config.json with the chosen output_dir
$configPath = "$PkgDir\config.json"
$configContent = @{ output_dir = $outputDir } | ConvertTo-Json
$configContent | Out-File -Encoding UTF8 $configPath
Write-Host "[OK] Recordings will save to: $outputDir" -ForegroundColor Green

# ── Step 10: Shortcuts ──

$LauncherPath = "$ScriptRoot\start.ps1"
$IconPath = "$PkgDir\whisper-capture.ico"

# Desktop shortcut (always offered first - this is how users launch the app)
Write-Host ""
$createDesktop = Read-Host "Create a Desktop shortcut to launch WhisperSync? (Y/n)"
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
    Write-Host "[OK] Desktop shortcut created" -ForegroundColor Green
}

# Startup shortcut (auto-launch on login)
$createStartup = Read-Host "Also launch WhisperSync automatically when you log in? (Y/n)"
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
    Write-Host "[OK] Startup shortcut created (launches on login)" -ForegroundColor Green
}

# ── Step 11: Bootstrap models ──

Write-Host ""
Write-Host "=== Downloading Base Models ===" -ForegroundColor Cyan
Write-Host "Caching tiny + base models (~225 MB total)..." -ForegroundColor Green
& $VenvPython -c "from whisper_sync.model_status import bootstrap_models; from whisper_sync import config; bootstrap_models(config.load())"

# ── Done ──

Write-Host ""
Write-Host "=== Installation Complete ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "To launch WhisperSync:" -ForegroundColor White
Write-Host "  Double-click the WhisperSync shortcut on your Desktop" -ForegroundColor Yellow
Write-Host "  Or run: powershell -ExecutionPolicy Bypass -File start.ps1" -ForegroundColor Gray
Write-Host ""

} catch {
    Write-Host ""
    Write-Host "=== INSTALLATION FAILED ===" -ForegroundColor Red
    Write-Host ""
    Write-Host "Error: $_" -ForegroundColor Red
    Write-Host ""
    Write-Host "If this keeps happening, screenshot this window and send it to Colby." -ForegroundColor Yellow
    Write-Host ""
}

Write-Host "Press any key to close..." -ForegroundColor Gray
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
