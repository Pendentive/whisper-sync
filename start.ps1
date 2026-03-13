# Launch WhisperSync from the venv
# Usage: powershell -ExecutionPolicy Bypass -File scripts/whisper_sync/start.ps1

$VenvPython = "$PSScriptRoot\..\whisper-env\Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Host "WhisperX venv not found. Run setup first:" -ForegroundColor Red
    Write-Host "  powershell -ExecutionPolicy Bypass -File scripts/whisper_sync/setup-env.ps1" -ForegroundColor Yellow
    exit 1
}

Push-Location "$PSScriptRoot\.."
& $VenvPython -m whisper_sync
Pop-Location
