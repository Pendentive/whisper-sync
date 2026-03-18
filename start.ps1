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
$VenvPythonFull = (Resolve-Path $VenvPython -ErrorAction SilentlyContinue).Path
$existing = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'whisper_sync' }
if ($existing) {
    $count = ($existing | Measure-Object).Count
    Write-Host "Killing $count existing WhisperSync process(es)..." -ForegroundColor Yellow
    foreach ($proc in $existing) {
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
