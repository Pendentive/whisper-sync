# Launch WhisperSync from the venv
# Usage: powershell -ExecutionPolicy Bypass -File scripts/whisper_sync/start.ps1
# Add -Watchdog to auto-restart on crash:
#   powershell -ExecutionPolicy Bypass -File scripts/whisper_sync/start.ps1 -Watchdog

param([switch]$Watchdog)

$VenvPython = "$PSScriptRoot\..\whisper-env\Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Host "WhisperX venv not found. Run setup first:" -ForegroundColor Red
    Write-Host "  powershell -ExecutionPolicy Bypass -File scripts/whisper_sync/setup-env.ps1" -ForegroundColor Yellow
    exit 1
}

# Kill any existing WhisperSync instances before starting a new one.
# Matches processes running whisper_sync or whisper_sync.watchdog from either
# the venv python or the system python. Skips unrelated python processes.
# Also kills orphan worker subprocesses spawned via multiprocessing.spawn —
# these don't contain 'whisper_sync' in their command line, so we find them
# by matching child processes whose parent is a whisper_sync process.
$VenvPythonFull = (Resolve-Path $VenvPython -ErrorAction SilentlyContinue).Path
$existing = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'whisper_sync' }
if ($existing) {
    $parentPids = $existing | ForEach-Object { $_.ProcessId }
    # Find orphan worker subprocesses (multiprocessing.spawn children of whisper_sync)
    $orphans = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'multiprocessing' -and $_.ParentProcessId -in $parentPids }
    $allToKill = @($existing) + @($orphans) | Select-Object -Unique -Property ProcessId
    $count = ($allToKill | Measure-Object).Count
    Write-Host "Killing $count existing WhisperSync process(es)..." -ForegroundColor Yellow
    foreach ($proc in $allToKill) {
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
}

Push-Location "$PSScriptRoot\.."
if ($Watchdog) {
    & $VenvPython -m whisper_sync.watchdog
} else {
    & $VenvPython -m whisper_sync
}
Pop-Location
