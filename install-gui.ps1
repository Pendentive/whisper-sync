# WhisperSync GUI Installer launcher
# Usage: powershell -ExecutionPolicy Bypass -File install-gui.ps1

Push-Location $PSScriptRoot

# Probe python like install.ps1 does: py, python3, python
$pythonCmd = $null
foreach ($cmd in @("py", "python3", "python")) {
    $ver = & $cmd --version 2>&1
    if ($LASTEXITCODE -eq 0 -and $ver -match "Python (\d+)\.(\d+)") {
        $major = [int]$Matches[1]; $minor = [int]$Matches[2]
        if ($major -ge 3 -and $minor -ge 10) {
            $pythonCmd = $cmd; break
        }
    }
}

if (-not $pythonCmd) {
    Write-Host "Python 3.10+ not found. Install from https://python.org/downloads/" -ForegroundColor Red
    Pop-Location
    exit 1
}

& $pythonCmd -m whisper_sync.installer_gui

Pop-Location
