# WhisperX venv bootstrap - creates venv, installs CUDA PyTorch + whisperX + capture deps
# Usage: powershell -ExecutionPolicy Bypass -File scripts/whisper_sync/setup-env.ps1

$ErrorActionPreference = "Stop"
$VenvPath = "$PSScriptRoot\..\whisper-env"
$RequirementsFile = "$PSScriptRoot\requirements.txt"

Write-Host "=== WhisperX Venv Setup ===" -ForegroundColor Cyan

# Step 1: Create venv
if (Test-Path $VenvPath) {
    Write-Host "Venv already exists at $VenvPath. Delete it first to recreate." -ForegroundColor Yellow
    exit 1
}
Write-Host "Creating venv..." -ForegroundColor Green
python -m venv $VenvPath

# Step 2: Activate
$ActivateScript = "$VenvPath\Scripts\Activate.ps1"
. $ActivateScript

# Step 3: Upgrade pip
Write-Host "Upgrading pip..." -ForegroundColor Green
python -m pip install --upgrade pip

# Step 4: Install whisperX + capture deps (BEFORE CUDA torch - whisperX pulls CPU torch)
Write-Host "Installing whisperX and capture dependencies..." -ForegroundColor Green
pip install -r $RequirementsFile

# Step 5: Install PyTorch with CUDA 12.8 (RTX 5070 Ti) - AFTER whisperX to override CPU torch
Write-Host "Installing PyTorch with CUDA 12.8 (overriding CPU torch from whisperX)..." -ForegroundColor Green
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128 --force-reinstall --no-deps

# Step 6: Verify
Write-Host "`n=== Verification ===" -ForegroundColor Cyan

$cudaCheck = python -c "import torch; print(f'CUDA available: {torch.cuda.is_available()}, Device: {torch.cuda.get_device_name(0) if torch.cuda.is_available() else \"N/A\"}')"
Write-Host $cudaCheck

$whisperxCheck = python -c "import whisperx; print(f'whisperX imported OK')" 2>&1
Write-Host $whisperxCheck

$sdCheck = python -c "import sounddevice; print(f'sounddevice imported OK')" 2>&1
Write-Host $sdCheck

# Step 7: Check HF token
$hfToken = "$env:USERPROFILE\.huggingface\token"
if (Test-Path $hfToken) {
    Write-Host "HF token found at $hfToken" -ForegroundColor Green
} else {
    Write-Host "WARNING: No HF token at $hfToken. Required for speaker diarization." -ForegroundColor Yellow
    Write-Host "Run: pm-get-secret hugging-face_read" -ForegroundColor Yellow
}

# Step 8: Create startup shortcut
Write-Host "`nCreating startup shortcut..." -ForegroundColor Green
$StartupFolder = [Environment]::GetFolderPath("Startup")
$ShortcutPath = "$StartupFolder\whisper-sync.lnk"
$VbsTemp = "$env:TEMP\create-shortcut.vbs"
$LauncherPath = "$PSScriptRoot\start.ps1"
@"
Set WshShell = WScript.CreateObject("WScript.Shell")
Set lnk = WshShell.CreateShortcut("$ShortcutPath")
lnk.TargetPath = "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
lnk.Arguments = "-ExecutionPolicy Bypass -WindowStyle Hidden -File ""$LauncherPath"""
lnk.WorkingDirectory = "$PSScriptRoot"
lnk.WindowStyle = 7
lnk.Description = "WhisperSync tray app"
lnk.Save
"@ | Out-File -Encoding ASCII $VbsTemp
cscript //nologo $VbsTemp
Remove-Item $VbsTemp -ErrorAction SilentlyContinue
Write-Host "Startup shortcut created at: $ShortcutPath" -ForegroundColor Green

# Step 9: Bootstrap models (auto-download tiny + base, prompt for large-v3)
Write-Host "`n=== Downloading Base Models ===" -ForegroundColor Cyan
Write-Host "Caching tiny + base models locally (first-run requirement)..." -ForegroundColor Green
python -c "from whisper_sync.model_status import bootstrap_models; from whisper_sync import config; bootstrap_models(config.load())"

Write-Host "`n=== Setup Complete ===" -ForegroundColor Cyan
Write-Host "WhisperSync will auto-start on login."
Write-Host "To launch now: powershell -ExecutionPolicy Bypass -File $LauncherPath"
