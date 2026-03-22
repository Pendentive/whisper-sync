# Launch WhisperSync from the venv
# Usage: powershell -ExecutionPolicy Bypass -File scripts/whisper_sync/start.ps1
# Add -Watchdog to auto-restart on crash:
#   powershell -ExecutionPolicy Bypass -File scripts/whisper_sync/start.ps1 -Watchdog

param([switch]$Watchdog)

# Find venv: check same dir (standalone), then parent (embedded scripts/whisper_sync/),
# then grandparent (legacy layout)
$VenvPython = "$PSScriptRoot\whisper-env\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    $VenvPython = "$PSScriptRoot\..\whisper-env\Scripts\python.exe"
}
if (-not (Test-Path $VenvPython)) {
    $VenvPython = "$PSScriptRoot\..\..\whisper-env\Scripts\python.exe"
}

if (-not (Test-Path $VenvPython)) {
    Write-Host "WhisperX venv not found. Run install.ps1 first." -ForegroundColor Red
    exit 1
}

# Kill any existing WhisperSync instances before starting a new one.
# Two passes:
#   1. Processes with 'whisper_sync' in the command line (main + watchdog)
#   2. Orphan multiprocessing.spawn workers - these survive after the parent dies
#      and hold GPU/MKL memory. We match them by parent PID (if parent is alive)
#      OR by the venv python path (catches orphans whose parent already died).
$VenvPythonFull = (Resolve-Path $VenvPython -ErrorAction SilentlyContinue).Path
$allPython = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue

$existing = $allPython | Where-Object { $_.CommandLine -match 'whisper_sync' }
$parentPids = @()
if ($existing) {
    $parentPids = $existing | ForEach-Object { $_.ProcessId }
}

# Find orphan workers: multiprocessing.spawn children of whisper_sync parents,
# OR multiprocessing.spawn processes using the whisper-env python (parent may be dead),
# OR multiprocessing.spawn processes whose parent PID no longer exists (dead parent = orphan).
# Note: Windows multiprocessing.spawn uses the system python, not the venv python,
# so we can't rely on the venv path alone.
$orphans = $allPython | Where-Object {
    $_.CommandLine -match 'multiprocessing\.spawn' -and (
        $_.ParentProcessId -in $parentPids -or
        $_.CommandLine -match [regex]::Escape($VenvPythonFull) -or
        (-not (Get-Process -Id $_.ParentProcessId -ErrorAction SilentlyContinue))
    )
}

$allToKill = @($existing) + @($orphans) | Where-Object { $_ -ne $null } |
    Select-Object -Unique -Property ProcessId
if ($allToKill) {
    $count = ($allToKill | Measure-Object).Count
    Write-Host "Killing $count existing WhisperSync process(es)..." -ForegroundColor Yellow
    foreach ($proc in $allToKill) {
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 3
}

# Determine working directory: if whisper_sync/ is in $PSScriptRoot (standalone repo),
# use $PSScriptRoot. If it's in the parent (embedded in scripts/whisper_sync/), use parent.
if (Test-Path "$PSScriptRoot\whisper_sync\__init__.py") {
    $WorkDir = $PSScriptRoot
} else {
    $WorkDir = "$PSScriptRoot\.."
}

Push-Location $WorkDir
# Ensure gh CLI is in PATH for GitHub status polling
if (Test-Path "C:\Program Files\GitHub CLI") {
    $env:PATH = "C:\Program Files\GitHub CLI;$env:PATH"
}
# Suppress pyannote's torchcodec warning (fires at import time, can't be filtered in code).
# torchcodec is optional — pyannote falls back to torchaudio which whisperX uses.
# Also suppress the Lightning checkpoint upgrade nag.
$env:PYTHONWARNINGS = "ignore::UserWarning:pyannote.audio.core.io,ignore::UserWarning:pyannote.audio.utils.reproducibility"

if ($Watchdog) {
    & $VenvPython -m whisper_sync.watchdog
} else {
    & $VenvPython -m whisper_sync
}
Pop-Location
